import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update

from app.db import utcnow
from app.models import Channel, ChannelMetric, CollectionRun, Post, PostMetric
from app.telegram import SourceError

logger = logging.getLogger(__name__)


class Collector:
    def __init__(self, sessions, source, interval=3600):
        self.sessions, self.source, self.interval = sessions, source, interval
        self.wake = asyncio.Event()
        self.local_lock = asyncio.Lock()

    async def collect(self, channel_id: int) -> bool:
        now, token = utcnow(), str(uuid4())
        with self.sessions.begin() as db:
            claimed = db.execute(
                update(Channel)
                .where(
                    Channel.id == channel_id,
                    Channel.next_collection_at <= now,
                    or_(Channel.lease_until.is_(None), Channel.lease_until < now),
                )
                .values(
                    status="collecting",
                    lease_token=token,
                    lease_until=now + timedelta(minutes=5),
                    last_attempt_at=now,
                )
            ).rowcount
            if not claimed:
                return False
            db.execute(
                update(CollectionRun)
                .where(
                    CollectionRun.channel_id == channel_id,
                    CollectionRun.status == "collecting",
                )
                .values(
                    status="interrupted", finished_at=now, error="Previous worker lease expired."
                )
            )
            db.add(CollectionRun(channel_id=channel_id, token=token))
            channel = db.get(Channel, channel_id)
            username, highwater, cursor = (
                channel.username,
                channel.last_message_id,
                channel.backfill_before,
            )
            target = channel.catchup_target
        try:
            # Every pass refreshes current posts, even during downtime catch-up.
            latest = await self.source.fetch(username)
            posts = {p.message_id: p for p in latest.posts}
            target = target or (max(posts) if posts else highwater)
            page = latest
            if highwater and page.posts:
                before = cursor or min(posts)
                needs_more = bool(cursor) or min(posts) > highwater
                for _ in range(3):
                    if not needs_more:
                        break
                    await asyncio.sleep(0.5)
                    page = await self.source.fetch(username, before)
                    if not page.posts:
                        needs_more = False
                        break
                    next_before = min(p.message_id for p in page.posts)
                    if next_before >= before:
                        raise SourceError("Telegram pagination did not advance; retrying later.")
                    posts.update({p.message_id: p for p in page.posts})
                    before = next_before
                    needs_more = before > highwater
                cursor = before if needs_more else None
            else:
                cursor = None
            with self.sessions.begin() as db:
                # Fencing: a stale worker can never publish after another worker takes over.
                fenced = db.execute(
                    update(Channel)
                    .where(
                        Channel.id == channel_id,
                        Channel.lease_token == token,
                        Channel.lease_until > utcnow(),
                    )
                    .values(lease_token=None, lease_until=None)
                ).rowcount
                if not fenced:
                    return False
                channel = db.get(Channel, channel_id)
                captured = utcnow()
                channel.title, channel.description = latest.title, latest.description
                channel.subscribers, channel.last_error = latest.subscribers, None
                channel.status = "catching_up" if cursor else "healthy"
                channel.last_success_at = captured
                channel.backfill_before = cursor
                channel.catchup_target = target if cursor else None
                if not cursor and posts:
                    # Advance only to the head at the START of a paginated catch-up.
                    # New arrivals during catch-up are scanned again on the next pass,
                    # so a busy channel cannot create a gap between preview pages.
                    channel.last_message_id = max(highwater, target)
                channel.next_collection_at = captured + timedelta(
                    seconds=60 if cursor else self.interval
                )
                for parsed in posts.values():
                    post = db.scalar(
                        select(Post).where(
                            Post.channel_id == channel_id, Post.message_id == parsed.message_id
                        )
                    )
                    if post is None:
                        post = Post(channel_id=channel_id, message_id=parsed.message_id)
                        db.add(post)
                    post.text, post.published_at, post.url = (
                        parsed.text,
                        parsed.published_at,
                        parsed.url,
                    )
                    post.views, post.reactions, post.updated_at = (
                        parsed.views,
                        parsed.reactions,
                        captured,
                    )
                    db.flush()
                    db.add(
                        PostMetric(
                            post_id=post.id,
                            views=parsed.views,
                            reactions=parsed.reactions,
                            captured_at=captured,
                        )
                    )
                db.add(
                    ChannelMetric(
                        channel_id=channel_id, subscribers=latest.subscribers, captured_at=captured
                    )
                )
                run = db.scalar(select(CollectionRun).where(CollectionRun.token == token))
                run.status, run.finished_at, run.collected_count = "success", captured, len(posts)
            logger.info("collection_success channel_id=%s posts=%s", channel_id, len(posts))
            return True
        except Exception as exc:
            error = (
                str(exc) if isinstance(exc, SourceError) else "Collection failed; retry scheduled."
            )
            # Do not log exception bodies: drivers/upstreams may embed credentials or content.
            logger.warning(
                "collection_failed channel_id=%s type=%s", channel_id, type(exc).__name__
            )
            with self.sessions.begin() as db:
                db.execute(
                    update(Channel)
                    .where(
                        Channel.id == channel_id,
                        Channel.lease_token == token,
                    )
                    .values(
                        status="error",
                        last_error=error,
                        lease_token=None,
                        lease_until=None,
                        next_collection_at=utcnow() + timedelta(minutes=15),
                    )
                )
                db.execute(
                    update(CollectionRun)
                    .where(CollectionRun.token == token)
                    .values(status="error", error=error, finished_at=utcnow())
                )
            return False

    async def collect_due(self):
        if self.local_lock.locked():
            return {"busy": True, "collected": 0}
        async with self.local_lock:
            with self.sessions() as db:
                ids = list(
                    db.scalars(
                        select(Channel.id)
                        .where(
                            Channel.next_collection_at <= utcnow(),
                            or_(Channel.lease_until.is_(None), Channel.lease_until < utcnow()),
                        )
                        .order_by(Channel.next_collection_at)
                        .limit(30)
                    )
                )
            count = 0
            for channel_id in ids:
                count += await self.collect(channel_id)
            return {"busy": False, "collected": count}

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.collect_due()
            except Exception as exc:
                logger.error("worker_cycle_failed type=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=15)
            except TimeoutError:
                pass
