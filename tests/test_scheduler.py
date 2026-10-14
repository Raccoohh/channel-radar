import asyncio
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.collection import Collector
from app.config import Settings
from app.db import utcnow
from app.main import create_app
from app.models import Channel, CollectionRun, Post, PostMetric
from app.telegram import SourceError
from scripts.scheduled_collect import run_from_env

AUTH = {"Authorization": "Bearer test-token"}
ENV = {"RADAR_URL": "https://radar.test", "SCHEDULER_TOKEN": "test-token"}


def summary(n=0, failed=0, skipped=0):
    return dict(
        attempted=n + failed, succeeded=n, failed=failed, skipped=skipped, collected=n, busy=False
    )


class Clock:
    def __init__(self):
        self.now = 0
        self.delays = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds


async def scripted(replies, **options):
    calls, logs = [], []
    clock = Clock()

    def handler(request):
        calls.append((request.method, request.url.path))
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(clock, request)
        return reply

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        code = await run_from_env(
            ENV, client, log=lambda **x: logs.append(x), clock=clock, sleep=clock.sleep, **options
        )
    return code, calls, logs, clock


def response(code=200, data=None):
    return httpx.Response(code, json={"status": "ok"} if data is None else data)


@pytest.mark.parametrize("first", [503, 502, 429, 408, "timeout", "connection"])
async def test_health_retries_safe_get_and_logs_without_secrets(first):
    error = (
        httpx.ReadTimeout("test-token private body")
        if first == "timeout"
        else httpx.ConnectError("test-token private body")
        if first == "connection"
        else response(first)
    )
    code, calls, logs, clock = await scripted([error, response(), response(data=summary())])
    assert code == 0 and calls == [("GET", "/healthz")] * 2 + [("POST", "/internal/collect")]
    assert clock.delays == [2]
    assert all("duration" in item and "status" in item for item in logs if "method" in item)
    assert "test-token" not in json.dumps(logs) and "private body" not in json.dumps(logs)


async def test_slow_wake_and_shared_deadline_budget():
    def slow(clock, request):
        clock.now += 25
        return response(503)

    code, calls, _, clock = await scripted([slow, slow, response(), response(data=summary(2))])
    assert code == 0 and clock.now == 56 and clock.delays == [2, 4]
    assert sum(method == "POST" for method, _ in calls) == 1


@pytest.mark.parametrize("total,readiness", [(5, 100), (100, 5)])
async def test_health_deadline_no_post(total, readiness):
    code, calls, logs, clock = await scripted(
        [response(503)] * 20, total=total, readiness=readiness
    )
    assert code == 1 and clock.now == 5
    assert all(method == "GET" for method, _ in calls)
    assert logs[-1] == {"result": "failure", "category": "deadline"}


@pytest.mark.parametrize("status", [301, 401, 403, 404])
async def test_health_permanent_failure_is_not_retried(status):
    code, calls, _, _ = await scripted([response(status)])
    assert code == 1 and calls == [("GET", "/healthz")]


@pytest.mark.parametrize("state,expected", [("success", 0), ("failed", 1), ("interrupted", 1)])
async def test_busy_observes_exact_cycle_until_terminal(state, expected):
    rid = str(uuid4())
    code, calls, logs, _ = await scripted(
        [
            response(),
            response(409, {**summary(), "busy": True, "run_id": rid}),
            response(503),
            httpx.ReadTimeout("private"),
            response(data={**summary(), "run_id": rid, "state": "running"}),
            response(data={**summary(2), "run_id": rid, "state": state}),
        ]
    )
    assert code == expected
    assert calls[1] == ("POST", "/internal/collect")
    assert all(call == ("GET", f"/internal/collect/{rid}") for call in calls[2:])
    assert logs[-1]["result"] == ("success" if expected == 0 else "failure")


async def test_busy_timeout_does_not_repeat_post():
    rid = str(uuid4())
    code, calls, logs, clock = await scripted(
        [
            response(),
            response(409, {**summary(), "busy": True, "run_id": rid}),
            *[response(data={**summary(), "run_id": rid, "state": "running"}) for _ in range(10)],
        ],
        total=7,
    )
    assert code == 1 and clock.now == 7 and logs[-1]["category"] == "deadline"
    assert sum(method == "POST" for method, _ in calls) == 1


@pytest.mark.parametrize("result", ["restart", "wrong_id", "lying_success", "missing_id"])
async def test_unknown_or_mismatched_receipt_never_succeeds(result):
    rid = str(uuid4())
    busy = response(409, {**summary(), "busy": True, "run_id": rid})
    final = response(data={**summary(), "state": "success", "run_id": rid})
    if result == "restart":
        final = response(404)
    elif result == "wrong_id":
        final = response(data={**summary(), "state": "success", "run_id": str(uuid4())})
    elif result == "lying_success":
        final = response(data={**summary(failed=1), "state": "success", "run_id": rid})
    else:
        busy = response(409, {**summary(), "busy": True})
    code, calls, _, _ = await scripted([response(), busy, final])
    assert code == 1 and sum(method == "POST" for method, _ in calls) == 1


@pytest.mark.parametrize(
    "reply",
    [
        response(503),
        response(500),
        httpx.ReadTimeout("private"),
        httpx.ConnectError("private"),
        response(302),
    ],
)
async def test_post_failure_or_ambiguity_is_never_retried(reply):
    code, calls, _, _ = await scripted([response(), reply])
    assert code == 1 and calls == [("GET", "/healthz"), ("POST", "/internal/collect")]


@pytest.mark.parametrize("data", [summary(), summary(2)])
async def test_no_due_or_completed_collection_success(data):
    code, calls, _, _ = await scripted([response(), response(data=data)])
    assert code == 0 and len(calls) == 2


@pytest.mark.parametrize(
    "data",
    [summary(failed=1), summary(skipped=1), {**summary(), "succeeded": True}, {"status": "ok"}, []],
)
async def test_invalid_or_unconfirmed_summary_fails(data):
    code, _, _, _ = await scripted([response(), response(data=data)])
    assert code == 1


async def test_wall_deadline_cancels_stuck_request():
    async def handler(request):
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        logs = []
        async with asyncio.timeout(1):
            assert await run_from_env(ENV, client, total=0.02, log=lambda **x: logs.append(x)) == 1
        assert logs[-1]["category"] == "deadline"


async def test_total_deadline_includes_health_and_busy_polling():
    rid = str(uuid4())

    def slow_health(clock, request):
        clock.now += 6
        return response()

    code, calls, logs, clock = await scripted(
        [
            slow_health,
            response(409, {**summary(), "busy": True, "run_id": rid}),
            *[response(data={**summary(), "run_id": rid, "state": "running"}) for _ in range(10)],
        ],
        total=10,
    )
    assert code == 1 and clock.now == 10 and logs[-1]["category"] == "deadline"
    assert sum(method == "POST" for method, _ in calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://radar.test",
        "https://user:secret@radar.test",
        "https://radar.test/path",
        "https://radar.test/?secret=x",
        "",
    ],
)
async def test_configuration_rejects_unsafe_origin_without_network(url):
    calls, logs = [], []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(r))
    ) as client:
        code = await run_from_env({**ENV, "RADAR_URL": url}, client, log=lambda **x: logs.append(x))
    assert code == 1 and not calls
    assert logs == [{"result": "failure", "category": "configuration"}]


async def test_status_auth_and_get_never_expose_secrets():
    rid = str(uuid4())
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/healthz":
            assert "authorization" not in request.headers
            return response()
        assert request.headers["authorization"] == "Bearer test-token"
        if request.method == "POST":
            return response(409, {**summary(), "busy": True, "run_id": rid})
        return response(data={**summary(1), "run_id": rid, "state": "success"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await run_from_env(ENV, client) == 0
    assert len(calls) == 3


async def test_malformed_response_is_failure_not_traceback():
    code, calls, logs, _ = await scripted([httpx.Response(200, text="private not JSON")])
    assert code == 1 and len(calls) == 1
    assert logs[-1]["category"] == "invalid_response"
    assert "private" not in json.dumps(logs)


@pytest.fixture
async def web(sessions, source):
    app = create_app(
        Settings(_env_file=None, worker_enabled=False, scheduler_token="test-token"),
        sessions,
        source,
        AsyncMock(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://radar.test"
        ) as client,
    ):
        yield app, client


def add(sessions, username="durov"):
    with sessions.begin() as db:
        c = Channel(username=username)
        db.add(c)
        db.flush()
        return c.id


@pytest.mark.parametrize("fail", [False, True])
async def test_real_busy_receipt_tracks_whole_cycle_and_preserves_history(
    web, sessions, source, fail
):
    app, client = web
    add(sessions)
    add(sessions, "telegram")
    entered, release = asyncio.Event(), asyncio.Event()
    parsed = source.fetch.return_value

    async def fetch(username, before=None):
        entered.set()
        await release.wait()
        if fail and username == "telegram":
            raise SourceError("Synthetic Telegram failure")
        return parsed

    source.fetch.side_effect = fetch
    task = asyncio.create_task(app.state.collector.collect_due())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        busy = await client.post("/internal/collect", headers=AUTH)
        assert busy.status_code == 409
        rid = busy.json()["run_id"]
        path = f"/internal/collect/{rid}"
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=AUTH)).json()["state"] == "running"
        with sessions() as db:
            assert db.scalar(select(func.count(CollectionRun.id))) == 1
        release.set()
        result = await task
        final = await client.get(path, headers=AUTH)
        assert final.headers["cache-control"] == "no-store"
        assert final.json() == {**result, "run_id": rid, "state": "failed" if fail else "success"}
        # A later no-op must never replace the exact result the observer is polling.
        assert (await client.post("/internal/collect", headers=AUTH)).json()["attempted"] == 0
        assert (await client.get(path, headers=AUTH)).json() == final.json()
        with sessions() as db:
            assert db.scalar(select(func.count(CollectionRun.id))) == 2
            assert db.scalar(select(func.count(Post.id))) == (2 if fail else 4)
            assert db.scalar(select(func.count(PostMetric.id))) == (2 if fail else 4)
            assert all(c.lease_token is None for c in db.scalars(select(Channel)))
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("fail", [False, True])
async def test_scheduler_client_against_real_asgi_busy_run(web, sessions, source, fail):
    app, client = web
    add(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    parsed = source.fetch.return_value

    async def fetch(*args):
        entered.set()
        await release.wait()
        if fail:
            raise SourceError("Synthetic")
        return parsed

    async def sleep(_):
        release.set()
        await task

    source.fetch.side_effect = fetch
    task = asyncio.create_task(app.state.collector.collect_due())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        logs = []
        assert await run_from_env(ENV, client, sleep=sleep, log=lambda **x: logs.append(x)) == int(
            fail
        )
        assert logs[-1]["result"] == ("failure" if fail else "success")
        source.fetch.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_status_cancelled_cycle_is_failed(web, sessions, source):
    app, client = web
    add(sessions)
    entered = asyncio.Event()

    async def fetch(*args):
        entered.set()
        await asyncio.Event().wait()

    source.fetch.side_effect = fetch
    task = asyncio.create_task(app.state.collector.collect_due())
    await asyncio.wait_for(entered.wait(), 2)
    rid = app.state.collector.active_cycle
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    data = (await client.get(f"/internal/collect/{rid}", headers=AUTH)).json()
    assert data["state"] == "failed" and app.state.collector.active_cycle is None


async def test_status_unknown_expired_bounded_and_readonly(web, monkeypatch):
    app, client = web
    collector = app.state.collector
    assert (await client.get(f"/internal/collect/{uuid4()}", headers=AUTH)).status_code == 404
    await collector.collect_due()
    first = next(iter(collector.cycles))
    for _ in range(260):
        await collector.collect_due()
    assert len(collector.cycles) <= 256 and collector.cycle_status(first) is None
    last = next(reversed(collector.cycles))
    assert Collector(collector.sessions, collector.source).cycle_status(last) is None
    monkeypatch.setattr(
        "app.collection.monotonic", lambda: collector.cycles[last]["updated"] + 1801
    )
    assert (await client.get(f"/internal/collect/{last}", headers=AUTH)).status_code == 404


async def test_lease_takeover_does_not_turn_fenced_skip_into_success(web, sessions, source):
    app, client = web
    cid = add(sessions)
    entered, release = asyncio.Event(), asyncio.Event()
    parsed = source.fetch.return_value

    async def fetch(*args):
        entered.set()
        await release.wait()
        return parsed

    source.fetch.side_effect = fetch
    task = asyncio.create_task(app.state.collector.collect_due())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        rid = app.state.collector.active_cycle
        with sessions.begin() as db:
            db.get(Channel, cid).lease_until = utcnow() - timedelta(seconds=1)
        other = Collector(sessions, AsyncMock(fetch=AsyncMock(return_value=parsed)))
        assert await other.collect(cid)
        release.set()
        assert (await task)["skipped"] == 1
        assert (await client.get(f"/internal/collect/{rid}", headers=AUTH)).json()[
            "state"
        ] == "failed"
        with sessions() as db:
            assert db.scalar(select(func.count(Post.id))) == 2
            assert db.scalar(select(func.count(PostMetric.id))) == 2
            assert sorted(db.scalars(select(CollectionRun.status))) == ["interrupted", "success"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_workflow_executes_tested_client_and_propagates_exit():
    workflow = Path(".github/workflows/collect.yml").read_text()
    assert "run: python -m scripts.scheduled_collect" in workflow
    assert "continue-on-error" not in workflow and "|| true" not in workflow
    assert "cancel-in-progress: false" in workflow
