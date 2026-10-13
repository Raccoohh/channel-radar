import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.collection import Collector
from app.db import utcnow
from app.models import Channel, ChannelMetric, CollectionRun, Post, PostMetric
from app.telegram import SourceError


def add(sessions):
    with sessions.begin() as db:
        channel = Channel(username="durov")
        db.add(channel)
        db.flush()
        return channel.id


def make_due(sessions, channel_id):
    with sessions.begin() as db:
        db.get(Channel, channel_id).next_collection_at = utcnow() - timedelta(seconds=1)


async def test_idempotent_collection_preserves_history(sessions, source):
    channel_id = add(sessions)
    collector = Collector(sessions, source)
    assert await collector.collect(channel_id)
    assert not await collector.collect(channel_id)  # Enforces the collection cooldown.
    source.fetch.return_value.posts[0].views = 1500
    make_due(sessions, channel_id)
    assert await collector.collect(channel_id)
    with sessions() as db:
        assert db.scalar(select(func.count(Post.id))) == 2
        assert db.scalar(select(func.count(PostMetric.id))) == 4
        assert db.scalar(select(func.count(ChannelMetric.id))) == 2
        post = db.scalar(select(Post).where(Post.message_id == 101))
        history = db.scalars(
            select(PostMetric.views).where(PostMetric.post_id == post.id).order_by(PostMetric.id)
        ).all()
        assert history == [1200, 1500]
        assert db.get(Channel, channel_id).status == "healthy"


async def test_concurrent_collectors_only_one_claims(sessions, source):
    channel_id = add(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    parsed = source.fetch.return_value

    async def fetch(*args):
        entered.set()
        await release.wait()
        return parsed

    source.fetch.side_effect = fetch
    first = asyncio.create_task(Collector(sessions, source).collect(channel_id))
    await entered.wait()
    assert not await Collector(sessions, source).collect(channel_id)
    release.set()
    assert await first
    assert source.fetch.call_count == 1


async def test_recovery_and_safe_failure(sessions, source):
    channel_id = add(sessions)
    with sessions.begin() as db:
        channel = db.get(Channel, channel_id)
        channel.status, channel.lease_token = "collecting", "old"
        channel.lease_until = utcnow() - timedelta(seconds=1)
        db.add(CollectionRun(channel_id=channel_id, token="old"))
    collector = Collector(sessions, source)
    assert await collector.collect(channel_id)
    with sessions() as db:
        assert (
            db.scalar(select(CollectionRun).where(CollectionRun.token == "old")).status
            == "interrupted"
        )
    make_due(sessions, channel_id)
    source.fetch.side_effect = SourceError("Channel is private.")
    assert not await collector.collect(channel_id)
    with sessions() as db:
        assert db.get(Channel, channel_id).status == "error"
        assert db.get(Channel, channel_id).last_success_at
        assert db.scalar(select(func.count(Post.id))) == 2


async def test_database_unique_constraint(sessions, source):
    channel_id = add(sessions)
    await Collector(sessions, source).collect(channel_id)
    with pytest.raises(IntegrityError), sessions.begin() as db:
        original = db.scalar(select(Post))
        db.add(
            Post(
                channel_id=channel_id,
                message_id=original.message_id,
                text="duplicate",
                published_at=utcnow(),
                url=original.url,
            )
        )


async def test_stale_worker_is_fenced(sessions, source):
    channel_id = add(sessions)
    parsed = source.fetch.return_value

    async def replace_lease(*args):
        with sessions.begin() as db:
            db.get(Channel, channel_id).lease_token = "replacement"
        return parsed

    source.fetch.side_effect = replace_lease
    assert not await Collector(sessions, source).collect(channel_id)
    with sessions() as db:
        assert db.scalar(select(func.count(Post.id))) == 0


async def test_bounded_catchup_resumes(sessions, source, monkeypatch):
    from dataclasses import replace

    from app.telegram import ParsedChannel

    monkeypatch.setattr("app.collection.asyncio.sleep", AsyncMock())
    channel_id = add(sessions)
    template = source.fetch.return_value.posts[0]
    with sessions.begin() as db:
        db.get(Channel, channel_id).last_message_id = 10

    def batch(ids):
        return ParsedChannel(
            "Channel",
            "",
            100,
            [replace(template, message_id=n, url=f"https://t.me/durov/{n}") for n in ids],
        )

    pages = {
        None: batch([99, 100]),
        99: batch([80, 90]),
        80: batch([60, 70]),
        60: batch([40, 50]),
        40: batch([10, 30]),
    }
    source.fetch.side_effect = lambda username, before=None: pages[before]
    collector = Collector(sessions, source)
    assert await collector.collect(channel_id)
    with sessions() as db:
        c = db.get(Channel, channel_id)
        assert c.backfill_before == 40 and c.last_message_id == 10
    make_due(sessions, channel_id)
    assert await collector.collect(channel_id)
    with sessions() as db:
        c = db.get(Channel, channel_id)
        assert c.backfill_before is None and c.last_message_id == 100
        assert db.scalar(select(func.count(Post.id))) == 10
