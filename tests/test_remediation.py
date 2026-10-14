import asyncio
import json
import threading
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import event, func, select

from app.ai import AIUnavailable, DigestService, GeminiProvider
from app.collection import Collector
from app.config import Settings
from app.db import run_db, utcnow
from app.main import create_app
from app.models import Channel, CollectionRun, Control, Digest, Post, PostMetric
from app.telegram import SourceError, normalize_username, parse_channel


def add(sessions, username="durov"):
    with sessions.begin() as db:
        channel = Channel(username=username)
        db.add(channel)
        db.flush()
        return channel.id


def due(sessions, cid):
    with sessions.begin() as db:
        db.get(Channel, cid).next_collection_at = utcnow() - timedelta(seconds=1)


@pytest.fixture
async def web(sessions, source):
    app = create_app(
        Settings(_env_file=None, worker_enabled=False, scheduler_token="test-token"),
        sessions,
        source,
        AsyncMock(generate=AsyncMock(return_value="Digest [101]")),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield app, client


@pytest.mark.parametrize(
    "raw",
    ["@Durov", "durov", "https://t.me/durov", "t.me/durov", "T.ME/Durov", "telegram.me/durov"],
)
async def test_normalization_and_html_form(web, raw):
    assert normalize_username(raw) == "durov"
    _, client = web
    response = await client.post("/channels", data={"username": raw})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert (await client.get("/api/channels")).json()[0]["username"] == "durov"


@pytest.mark.parametrize(
    "raw",
    [
        "t.me/durov?x=1",
        "t.me/durov#x",
        "t.me.evil/durov",
        "t.me@127.0.0.1/durov",
        "https://t.me:443/durov",
        "//t.me/durov",
        "ftp://t.me/durov",
        "t.me/+invite",
        "t.me/durov/1",
        "https://user@t.me/durov",
        "t.me/%64urov",
        "t.me/../durov",
    ],
)
def test_url_allowlist_and_paths(raw):
    with pytest.raises(ValueError):
        normalize_username(raw)


@pytest.mark.parametrize("failures,expected", [(0, 200), (1, 503), (2, 503)])
async def test_scheduler_outcomes(web, sessions, source, failures, expected):
    _, client = web
    add(sessions)
    add(sessions, "telegram")
    parsed = source.fetch.return_value
    source.fetch.side_effect = [SourceError("Unavailable")] * failures + [parsed] * (2 - failures)
    response = await client.post(
        "/internal/collect", headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == expected
    assert response.json() == dict(
        attempted=2,
        succeeded=2 - failures,
        failed=failures,
        skipped=0,
        busy=False,
        collected=2 - failures,
    )
    # Cooldown/no work is genuinely successful, but must not hide the previous failed call.
    response = await client.post(
        "/internal/collect", headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 200 and response.json()["attempted"] == 0


async def test_scheduler_busy_and_skipped(web, sessions, monkeypatch):
    app, client = web
    async with app.state.collector.local_lock:
        response = await client.post(
            "/internal/collect", headers={"Authorization": "Bearer test-token"}
        )
    assert response.status_code == 409
    assert response.json()["busy"] is True and response.json()["attempted"] == 0
    cid = add(sessions)
    with sessions.begin() as db:
        db.get(Channel, cid).lease_until = utcnow() + timedelta(minutes=5)
    # Simulate a lease acquired by a different process after this process enumerated due IDs.
    monkeypatch.setattr(app.state.collector, "_due_ids", lambda: [cid])
    result = await app.state.collector.collect_due()
    assert result["skipped"] == 1 and result["attempted"] == result["succeeded"] == 0


@pytest.mark.parametrize(
    "length", ["invalid", "-1", "4097", "9" * 5000], ids=["text", "negative", "oversize", "huge"]
)
async def test_malformed_or_large_content_length(web, length):
    _, client = web
    response = await client.post("/api/channels", content=b"{}", headers={"Content-Length": length})
    assert response.status_code == 413


@pytest.mark.parametrize("padding,expected", [(0, 201), (4000, 201), (6000, 413)])
@pytest.mark.parametrize("declared", [False, True])
async def test_stream_actual_byte_limit(web, sessions, padding, expected, declared):
    _, client = web
    body = json.dumps({"username": "durov", "padding": "x" * padding}).encode()
    consumed = 0

    async def chunks():
        nonlocal consumed
        for start in range(0, len(body), 100):
            consumed += 1
            yield body[start : start + 100]

    headers = {"Content-Type": "application/json"}
    if declared:
        headers["Content-Length"] = "2"  # A dishonest header must not bypass actual counting.
    request = client.build_request("POST", "/api/channels", content=chunks(), headers=headers)
    if not declared:
        assert "content-length" not in request.headers
    assert (await client.send(request)).status_code == expected
    if expected == 413:
        assert consumed == 41  # Stops at the first oversized chunk, before parsing JSON.
        with sessions() as db:
            assert db.scalar(select(func.count(Channel.id))) == 0


@pytest.mark.parametrize("replacement", ["bad", "2026-10-12T10:00:00", ""])
def test_partial_timestamp_rejected(html, replacement):
    changed = html.replace("2026-10-12T10:00:00+03:00", replacement)
    with pytest.raises(SourceError, match="timestamp"):
        parse_channel(changed, "durov")


def test_real_message_missing_identity_or_timestamp_rejected(html):
    for changed in [
        html.replace(' data-post="durov/101"', ""),
        html.replace("datetime=", "bad-date=", 1),
    ]:
        with pytest.raises(SourceError):
            parse_channel(changed, "durov")
    service = '<div class="tgme_widget_message_service">Service notice</div>'
    assert (
        len(parse_channel(html.replace("</section>", service + "</section>"), "durov").posts) == 2
    )


@pytest.mark.parametrize(
    "count,expected",
    [("1 234", 1234), ("1\u00a0234", 1234), ("1\u202f234", 1234), ("1.2K", 1200), ("?", None)],
)
@pytest.mark.parametrize("wrapped", [True, False])
def test_full_reaction_counts(html, count, expected, wrapped):
    label = f'<span class="count">{count}</span>' if wrapped else count
    changed = html.replace('<tg-emoji emoji-id="123"></tg-emoji>3.5K', "👍" + label)
    assert parse_channel(changed, "durov").posts[0].reactions == (
        expected + 12 if expected is not None else None
    )


async def test_partial_parse_failure_preserves_boundary_and_recovers(
    sessions, source, html, monkeypatch
):
    monkeypatch.setattr("app.collection.asyncio.sleep", AsyncMock())
    cid = add(sessions)
    with sessions.begin() as db:
        db.get(Channel, cid).last_message_id = 100
    parsed = source.fetch.return_value
    broken = html.replace("datetime=", "bad-date=", 1)

    async def fetch(username, before=None):
        if before:
            return parse_channel(broken, username)
        return parsed

    source.fetch.side_effect = fetch
    collector = Collector(sessions, source)
    assert not await collector.collect(cid)
    with sessions() as db:
        assert db.get(Channel, cid).last_message_id == 100
        assert db.scalar(select(func.count(Post.id))) == 0
        assert db.scalar(select(func.count(PostMetric.id))) == 0
    older = parse_channel(
        html.replace("durov/101", "durov/99").replace("durov/102", "durov/100"), "durov"
    )
    source.fetch.side_effect = lambda username, before=None: older if before else parsed
    due(sessions, cid)
    assert await collector.collect(cid)
    due(sessions, cid)
    assert await collector.collect(cid)
    with sessions() as db:
        assert db.get(Channel, cid).last_message_id == 102
        assert db.scalar(select(func.count(Post.id))) == 4
        assert db.scalar(select(func.count(PostMetric.id))) == 6


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"candidates": 7},
        {"candidates": [7]},
        {"candidates": [None]},
        {"candidates": [{"content": []}]},
        {"candidates": [{"content": {"parts": {}}}]},
        {"candidates": [{"content": {"parts": [7]}}]},
        {"candidates": [{"content": {"parts": [{"text": 7}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "bad", "thought": "false"}]}}]},
        {"candidates": [{"finishReason": [], "content": {"parts": [{"text": "bad"}]}}]},
    ],
)
async def test_malformed_gemini_types(data):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))
    ) as http:
        with pytest.raises(AIUnavailable):
            await GeminiProvider(http, "dummy", "model").generate("{}")


async def test_unexpected_ai_failure_terminal_and_dashboard_works(web, sessions):
    app, client = web
    cid = add(sessions)
    assert await app.state.collector.collect(cid)
    app.state.digests.provider.generate.side_effect = AttributeError("private provider response")
    response = await client.post(f"/api/channels/{cid}/digest")
    assert response.status_code == 503 and "private provider" not in response.text
    with sessions() as db:
        digest = db.scalar(select(Digest))
        assert digest.status == "error" and "private provider" not in digest.content
        assert db.get(Control, 1).next_ai_at > utcnow()
    assert (await client.get(f"/channels/{cid}")).status_code == 200
    assert (await client.get("/healthz")).status_code == 200


async def test_publish_failure_rolls_back_entire_observation(sessions, source):
    cid = add(sessions)
    source.fetch.return_value.posts[1].published_at = None
    assert not await Collector(sessions, source).collect(cid)
    with sessions() as db:
        assert db.scalar(select(func.count(Post.id))) == 0
        assert db.scalar(select(func.count(PostMetric.id))) == 0
        assert db.get(Channel, cid).last_message_id == 0
        assert db.get(Channel, cid).status == "error"
        assert db.scalar(select(CollectionRun)).status == "error"


@pytest.mark.parametrize("operation", ["collection", "add", "bad_form", "digest"])
async def test_database_does_not_block_event_loop(web, sessions, operation):
    app, client = web
    cid = add(sessions)
    assert await app.state.collector.collect(cid)
    due(sessions, cid)
    engine = sessions.kw["bind"]
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    seen = []

    def block(*args):
        seen.append(threading.get_ident())
        if not entered.is_set():
            entered.set()
            assert release.wait(3), "Event loop did not release the DB thread"

    event.listen(engine, "before_cursor_execute", block)
    if operation == "collection":
        action = app.state.collector.collect(cid)
    elif operation == "add":
        action = client.post("/api/channels", json={"username": "durov"})
    elif operation == "bad_form":
        action = client.post("/channels", data={"username": "bad"})
    else:
        action = client.post(f"/api/channels/{cid}/digest")
    task = asyncio.create_task(action)
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        assert not task.done()
        release.set()
        result = await task
        assert result is True or result.status_code in {200, 422}
        assert seen and loop_thread not in seen
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", block)


async def test_run_db_cancellation_waits_for_session_close(sessions):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def unit():
        try:
            with sessions.begin() as db:
                db.add(Channel(username="durov"))
                db.flush()
                entered.set()
                assert release.wait(3)
                raise ValueError("rollback")
        finally:
            closed.set()

    task = asyncio.create_task(run_db(unit))
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and not closed.is_set()
        task.cancel()  # Repeated cancellation must not abandon an active thread.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        with sessions() as db:
            assert db.scalar(select(func.count(Channel.id))) == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stage", ["claim", "fetch", "publish"])
async def test_collection_cancellation_preserves_terminal_state(
    sessions, source, monkeypatch, stage
):
    cid = add(sessions)
    collector = Collector(sessions, source)
    entered, release = threading.Event(), threading.Event()
    if stage == "fetch":

        async def fetch(*args):
            entered.set()
            await asyncio.Event().wait()

        source.fetch.side_effect = fetch
    else:
        original = getattr(collector, "_" + stage)

        def block(*args):
            result = original(*args)
            entered.set()
            assert release.wait(3)
            return result

        monkeypatch.setattr(collector, "_" + stage, block)
    task = asyncio.create_task(collector.collect(cid))
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with sessions() as db:
            channel = db.get(Channel, cid)
            assert channel.lease_token is None
            assert channel.status == ("healthy" if stage == "publish" else "error")
            assert db.scalar(select(CollectionRun)).status == (
                "success" if stage == "publish" else "error"
            )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stage", ["prepare", "provider", "finish"])
async def test_ai_cancellation_terminal_state(sessions, source, monkeypatch, stage):
    cid = add(sessions)
    assert await Collector(sessions, source).collect(cid)
    provider = AsyncMock(generate=AsyncMock(return_value="Digest [101]"))
    service = DigestService(sessions, provider, "model")
    entered, release = threading.Event(), threading.Event()
    if stage == "provider":

        async def generate(*args):
            entered.set()
            await asyncio.Event().wait()

        provider.generate.side_effect = generate
    else:
        original = getattr(service, "_" + stage)

        def block(*args):
            result = original(*args)
            entered.set()
            assert release.wait(3)
            return result

        monkeypatch.setattr(service, "_" + stage, block)
    task = asyncio.create_task(service.generate(cid, 7))
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.005)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with sessions() as db:
            assert db.scalar(select(Digest)).status == ("ready" if stage == "finish" else "error")
            assert db.get(Control, 1).next_ai_at > utcnow()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("size,status", [(4096, 201), (4097, 413)])
async def test_exact_body_boundary(web, size, status):
    _, client = web
    prefix = b'{"username":"durov","padding":"'
    body = prefix + b"x" * (size - len(prefix) - 2) + b'"}'

    async def chunks():
        yield body[:4096]
        yield body[4096:]

    response = await client.post(
        "/api/channels", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == status


async def test_duplicate_content_length_rejected(web):
    _, client = web
    response = await client.post(
        "/api/channels", content=b"{}", headers=[("Content-Length", "2"), ("Content-Length", "2")]
    )
    assert response.status_code == 413


async def test_malformed_provider_attempt_terminal(web, sessions):
    app, client = web
    cid = add(sessions)
    assert await app.state.collector.collect(cid)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"candidates": [7]}))
    ) as http:
        app.state.digests.provider = GeminiProvider(http, "dummy", "model")
        assert (await client.post(f"/api/channels/{cid}/digest")).status_code == 503
    with sessions() as db:
        assert db.scalar(select(Digest)).status == "error"
    assert (await client.get(f"/channels/{cid}")).status_code == 200


async def test_concurrent_digest_admission_remains_serial(sessions, source):
    cid = add(sessions)
    assert await Collector(sessions, source).collect(cid)
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(context):
        entered.set()
        await release.wait()
        return "Digest [101]"

    provider = AsyncMock(generate=AsyncMock(side_effect=generate))
    first = DigestService(sessions, provider, "model")
    second = DigestService(sessions, provider, "model")
    task = asyncio.create_task(first.generate(cid, 7))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(AIUnavailable, match="cooling"):
            await second.generate(cid, 7)
        release.set()
        ready = await task
        cached = await second.generate(cid, 7)
        assert ready.id == cached.id
        provider.generate.assert_awaited_once()
        with sessions() as db:
            assert db.scalar(select(func.count(Digest.id))) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_worker_startup_shutdown_releases_active_claim(sessions, source):
    cid = add(sessions)
    entered = asyncio.Event()

    async def fetch(*args):
        entered.set()
        await asyncio.Event().wait()

    source.fetch.side_effect = fetch
    app = create_app(Settings(_env_file=None, worker_enabled=True), sessions, source, AsyncMock())
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(entered.wait(), 2)
    with sessions() as db:
        assert db.get(Channel, cid).lease_token is None
        assert db.scalar(select(CollectionRun)).status == "error"
