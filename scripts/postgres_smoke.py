"""CI-only integration check against disposable PostgreSQL. Never part of offline pytest."""

import asyncio
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import func, select

from app.ai import AIUnavailable, DigestService
from app.collection import Collector
from app.config import Settings
from app.db import make_database, utcnow
from app.main import create_app
from app.models import Channel, Digest, Post, PostMetric
from app.telegram import ParsedChannel, ParsedPost, SourceError
from scripts.scheduled_collect import run_from_env


async def main():
    engine, sessions = make_database(Settings().database_url.get_secret_value())
    assert engine.dialect.name == "postgresql"
    with sessions.begin() as db:
        channel = Channel(username="ci_smoke")
        db.add(channel)
        db.flush()
        cid = channel.id
    source = AsyncMock(
        fetch=AsyncMock(
            return_value=ParsedChannel(
                "CI",
                "",
                10,
                [ParsedPost(1, "CI fixture", utcnow(), "https://t.me/ci_smoke/1", 20, None)],
            )
        )
    )
    collector = Collector(sessions, source)
    results = await asyncio.gather(collector.collect(cid), Collector(sessions, source).collect(cid))
    assert results.count(True) == 1
    with sessions.begin() as db:
        db.get(Channel, cid).next_collection_at = utcnow()
    assert await collector.collect(cid)
    with sessions() as db:
        assert db.scalar(select(func.count(Post.id))) == 1
        assert db.scalar(select(func.count(PostMetric.id))) == 2
    with sessions.begin() as db:
        db.get(Channel, cid).next_collection_at = utcnow()
        db.add(Channel(username="ci_failure"))
    parsed = source.fetch.return_value

    async def fetch(username, before=None):
        if username == "ci_failure":
            raise SourceError("Synthetic source failure")
        return parsed

    source.fetch.side_effect = fetch
    result = await collector.collect_due()
    assert result["attempted"] == 2 and result["succeeded"] == result["failed"] == 1
    with sessions.begin() as db:
        db.get(Channel, cid).next_collection_at = utcnow()
    parsed.posts[0].published_at = None  # Force a publish failure after lease acquisition.
    assert not await collector.collect(cid)
    with sessions() as db:
        assert db.scalar(select(func.count(PostMetric.id))) == 3
        assert db.scalar(select(Post)).published_at is not None

    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(context):
        entered.set()
        await release.wait()
        return "Synthetic digest [1]"

    provider = AsyncMock(generate=AsyncMock(side_effect=generate))
    service = DigestService(sessions, provider, "ci-model")
    task = asyncio.create_task(service.generate(cid, 7))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        try:
            await DigestService(sessions, provider, "ci-model").generate(cid, 7)
        except AIUnavailable as exc:
            assert "cooling" in str(exc)
        else:
            raise AssertionError("Concurrent AI admission was not blocked")
        release.set()
        ready = await task
        assert (await service.generate(cid, 7)).id == ready.id
        provider.generate.assert_awaited_once()
        with sessions() as db:
            assert db.scalar(select(func.count(Digest.id))) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)

    # Use the actual ASGI status contract and scheduler client against PostgreSQL.
    parsed.posts[0].published_at = utcnow()
    for should_fail in (False, True):
        with sessions.begin() as db:
            db.get(Channel, cid).next_collection_at = utcnow()
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_fetch(*args, entered=entered, release=release, should_fail=should_fail):
            entered.set()
            await release.wait()
            if should_fail:
                raise SourceError("Synthetic scheduled failure")
            return parsed

        source.fetch.side_effect = delayed_fetch
        app = create_app(
            Settings(_env_file=None, worker_enabled=False, scheduler_token="ci-only"),
            sessions,
            source,
            provider,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://smoke.test"
            ) as client,
        ):
            task = asyncio.create_task(app.state.collector.collect_due())

            async def release_cycle(_, release=release, task=task):
                release.set()
                await task

            try:
                await asyncio.wait_for(entered.wait(), 5)
                code = await run_from_env(
                    {"RADAR_URL": "https://smoke.test", "SCHEDULER_TOKEN": "ci-only"},
                    client,
                    sleep=release_cycle,
                )
                assert code == int(should_fail)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
    with sessions() as db:
        assert db.scalar(select(func.count(Post.id))) == 1
        assert db.scalar(select(func.count(PostMetric.id))) == 4
    engine.dispose()
    print(
        "PostgreSQL concurrency, snapshots, rollback, AI cache and scheduler receipts smoke passed"
    )


if __name__ == "__main__":
    asyncio.run(main())
