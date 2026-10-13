from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.ai import AIUnavailable
from app.analytics import summary
from app.config import Settings
from app.db import utcnow
from app.main import create_app
from app.models import Channel, Control, Post


@pytest.fixture
async def client(sessions, source):
    settings = Settings(_env_file=None, worker_enabled=False, scheduler_token="unit-test-token")
    app = create_app(
        settings,
        sessions,
        source,
        AsyncMock(generate=AsyncMock(side_effect=AIUnavailable("AI unavailable"))),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


async def test_full_add_collect_details_flow(client):
    response = await client.post("/api/channels", json={"username": "@durov"})
    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    cid = response.json()["id"]
    assert (await client.post("/api/channels", json={"username": "Durov"})).status_code == 200
    assert (await client.post("/api/channels", json={"username": "bad"})).status_code == 422
    assert (await client.post("/internal/collect")).status_code == 401
    assert (
        await client.post("/internal/collect", headers={"Authorization": "Bearer unit-test-token"})
    ).json()["collected"] == 1
    channels = (await client.get("/api/channels")).json()
    assert len(channels) == 1 and channels[0]["status"] == "healthy"
    assert channels[0]["posts"] == 2
    for path in [
        "/",
        f"/channels/{cid}",
        "/posts/1",
        "/api/posts/1/trend",
        f"/api/channels/{cid}/trend",
        "/healthz",
    ]:
        assert (await client.get(path)).status_code == 200
    assert (await client.post(f"/api/channels/{cid}/digest")).status_code == 503
    assert (await client.get(f"/channels/{cid}")).status_code == 200
    assert (await client.get(f"/channels/{cid}?days=0")).status_code == 422
    assert (await client.get("/posts/9000")).status_code == 404


async def test_error_status(client, source):
    from app.telegram import SourceError

    source.fetch.side_effect = SourceError("Private or nonexistent channel")
    await client.post("/api/channels", json={"username": "@durov"})
    await client.post("/internal/collect", headers={"Authorization": "Bearer unit-test-token"})
    assert (await client.get("/api/channels")).json()[0]["status"] == "error"
    assert "Private or nonexistent" in (await client.get("/")).text


async def test_html_escapes_content_and_blocks_cross_origin(client, sessions):
    await client.post("/api/channels", json={"username": "@durov"})
    await client.post("/internal/collect", headers={"Authorization": "Bearer unit-test-token"})
    with sessions.begin() as db:
        db.scalar(select(Post)).text = '<script>alert("xss")</script>'
    response = await client.get("/posts/1")
    assert "&lt;script&gt;" in response.text
    assert '<script>alert("xss")' not in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert (
        await client.post(
            "/api/channels", json={"username": "channel"}, headers={"Origin": "https://evil.test"}
        )
    ).status_code == 403


def test_analytics_missing_values_and_period(sessions):
    with sessions.begin() as db:
        channel = Channel(username="durov")
        db.add(channel)
        db.flush()
        cid = channel.id
        for mid, views, days in [(1, 100, 1), (2, 300, 2), (3, None, 3), (4, 9999, 40)]:
            db.add(
                Post(
                    channel_id=cid,
                    message_id=mid,
                    text="text",
                    url="https://t.me/durov/1",
                    published_at=utcnow() - timedelta(days=days),
                    views=views,
                )
            )
    with sessions() as db:
        stats = summary(db, cid, 7)
        assert stats["posts"] == 3
        assert stats["views"] == 400 and stats["average_views"] == 200
        assert stats["known_views"] == 2
        assert stats["reactions"] is None
        assert summary(db, 999, 7)["average_views"] is None


async def test_admission_limits(client, sessions):
    await client.post("/api/channels", json={"username": "@durov"})
    assert (await client.post("/api/channels", json={"username": "telegram"})).status_code == 429
    with sessions.begin() as db:
        db.get(Control, 1).next_add_at = utcnow() - timedelta(seconds=1)
        for index in range(9):
            db.add(Channel(username=f"channel{index}"))
    assert (await client.post("/api/channels", json={"username": "telegram"})).status_code == 409


def test_production_config_rejects_unsafe_defaults():
    with pytest.raises(ValueError, match="PostgreSQL"):
        Settings(_env_file=None, environment="production")
