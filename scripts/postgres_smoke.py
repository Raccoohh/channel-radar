"""CI-only integration check against disposable PostgreSQL. Never part of offline pytest."""

import asyncio
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from app.collection import Collector
from app.config import Settings
from app.db import make_database, utcnow
from app.models import Channel, Post, PostMetric
from app.telegram import ParsedChannel, ParsedPost


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
    engine.dispose()
    print("PostgreSQL collection, idempotency and snapshot smoke passed")


if __name__ == "__main__":
    asyncio.run(main())
