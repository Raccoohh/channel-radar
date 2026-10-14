"""Bounded scheduler client. GETs may retry; collection POST is sent at most once."""

import asyncio
import json
import os
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx


class SchedulerFailure(Exception):
    """A safe, fixed category suitable for logs (never a remote body or secret)."""


def emit(**fields):
    print(json.dumps(fields, sort_keys=True), flush=True)


def summary_ok(data):
    if not isinstance(data, dict) or data.get("busy") is not False:
        return False
    counts = [data.get(k) for k in ("attempted", "succeeded", "failed", "skipped", "collected")]
    if any(type(v) is not int or v < 0 for v in counts):
        return False
    attempted, succeeded, failed, skipped, collected = counts
    return attempted == succeeded == collected and failed == skipped == 0


def payload(response):
    try:
        data = response.json()
    except (ValueError, UnicodeError) as exc:
        raise SchedulerFailure("invalid_response") from exc
    if not isinstance(data, dict):
        raise SchedulerFailure("invalid_response")
    return data


async def collect(
    client,
    url,
    token,
    *,
    total=660,
    readiness=300,
    clock=time.monotonic,
    sleep=asyncio.sleep,
    log=emit,
):
    started = clock()
    deadline = started + total

    async def request(method, path, phase, until, cap):
        remaining = min(deadline, until) - clock()
        if remaining <= 0:
            raise SchedulerFailure("deadline")
        begin = clock()
        status, category = None, "transport"
        try:
            # httpx timeouts bound inactivity, wait_for also bounds a slow trickle.
            budget = min(cap, remaining)
            response = await asyncio.wait_for(
                client.request(
                    method,
                    url + path,
                    headers={"Authorization": f"Bearer {token}"} if phase != "health" else {},
                    timeout=httpx.Timeout(budget, connect=min(10, budget)),
                    follow_redirects=False,
                ),
                timeout=budget,
            )
            status, category = response.status_code, "http"
            return response
        except (TimeoutError, httpx.TimeoutException):
            category = "timeout"
            return None
        except httpx.TransportError:
            return None
        except asyncio.CancelledError:
            category = "cancelled"
            raise
        finally:
            log(
                phase=phase,
                method=method,
                status=status,
                category=category,
                duration=round(clock() - begin, 3),
                elapsed=round(clock() - started, 3),
            )

    async def pause(delay, until):
        remaining = min(deadline, until) - clock()
        if remaining <= 0:
            raise SchedulerFailure("deadline")
        await sleep(min(delay, remaining))

    async def execute():
        health_deadline = min(deadline, started + readiness)
        delay = 2
        while True:
            response = await request("GET", "/healthz", "health", health_deadline, 30)
            if response is not None:
                if response.status_code == 200:
                    if payload(response).get("status") != "ok":
                        raise SchedulerFailure("invalid_health")
                    break
                if response.status_code < 500 and response.status_code not in {408, 429}:
                    raise SchedulerFailure("health_http_error")
            await pause(delay, health_deadline)
            delay = min(15, delay * 2)

        response = await request("POST", "/internal/collect", "collect", deadline, 480)
        if response is None:
            # The POST may have reached the server. Never try it again blindly.
            raise SchedulerFailure("post_result_unknown")
        if response.status_code == 200:
            if not summary_ok(payload(response)):
                raise SchedulerFailure("unconfirmed_collection")
            return
        if response.status_code != 409:
            raise SchedulerFailure("collection_http_error")
        data = payload(response)
        try:
            run_id = str(UUID(data["run_id"]))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SchedulerFailure("missing_correlation") from exc
        if data.get("busy") is not True:
            raise SchedulerFailure("invalid_busy")
        log(phase="observe", run_id=run_id, state="running")
        delay = 2
        while True:
            response = await request("GET", f"/internal/collect/{run_id}", "observe", deadline, 30)
            if response is not None:
                if response.status_code == 200:
                    data = payload(response)
                    if data.get("run_id") != run_id:
                        raise SchedulerFailure("correlation_mismatch")
                    state = data.get("state")
                    if state == "success":
                        if not summary_ok(data):
                            raise SchedulerFailure("unconfirmed_collection")
                        log(phase="observe", run_id=run_id, state="success")
                        return
                    if state != "running":
                        raise SchedulerFailure("collection_failed")
                elif response.status_code < 500 and response.status_code not in {408, 429}:
                    # In particular 404 after restart/expiry is not a successful run.
                    raise SchedulerFailure("status_unavailable")
            await pause(delay, deadline)
            delay = min(15, delay * 2)

    try:
        async with asyncio.timeout(total):
            await execute()
    except TimeoutError as exc:
        raise SchedulerFailure("deadline") from exc


async def run_from_env(env=None, client=None, log=emit, **options):
    env = os.environ if env is None else env
    url, token = env.get("RADAR_URL", "").rstrip("/"), env.get("SCHEDULER_TOKEN", "")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not token
        ):
            raise SchedulerFailure("configuration")
        if client is None:
            async with httpx.AsyncClient() as http:
                await collect(http, url, token, log=log, **options)
        else:
            await collect(client, url, token, log=log, **options)
    except SchedulerFailure as exc:
        log(result="failure", category=str(exc))
        return 1
    except Exception:
        # No exception repr: a transport may include headers, URL or response content.
        log(result="failure", category="unexpected")
        return 1
    log(result="success")
    return 0


def main():
    return asyncio.run(run_from_env())


if __name__ == "__main__":
    raise SystemExit(main())
