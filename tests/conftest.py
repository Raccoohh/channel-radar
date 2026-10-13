import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.db import Base, make_database, utcnow
from app.models import Control
from app.telegram import parse_channel

# Windows asyncio creates a loopback socketpair for its internal wake-up pipe.
# Create that pipe before pytest-socket guards begin; test bodies still cannot
# create sockets or resolve hosts. No real HTTP servers or network requests are used.
_offline_loop = asyncio.new_event_loop()


def _get_offline_loop():
    return _offline_loop


def pytest_asyncio_loop_factories():
    return {"offline": _get_offline_loop}


@pytest.fixture
def sessions(tmp_path):
    engine, factory = make_database(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with factory.begin() as db:
        db.add(
            Control(
                id=1,
                next_add_at=utcnow() - timedelta(days=1),
                next_ai_at=utcnow() - timedelta(days=1),
            )
        )
    yield factory
    engine.dispose()


@pytest.fixture
def html():
    return Path("tests/fixtures/channel.html").read_text(encoding="utf-8")


@pytest.fixture
def source(html):
    parsed = parse_channel(html, "durov")
    for post in parsed.posts:
        post.published_at = utcnow() - timedelta(hours=2)
    return AsyncMock(fetch=AsyncMock(return_value=parsed))
