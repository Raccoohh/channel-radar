from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from app.ai import AIUnavailable, DigestService, GeminiProvider
from app.collection import Collector
from app.db import utcnow
from app.models import Channel, Control, Digest


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_provider_http_failure(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as http:
        with pytest.raises(AIUnavailable):
            await GeminiProvider(http, "test-key", "test-model").generate("{}")


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"candidates": []},
        {"candidates": [{"content": {"parts": []}}]},
        {"candidates": [{"finishReason": "MAX_TOKENS"}]},
    ],
)
async def test_invalid_response(data):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    ) as http:
        with pytest.raises(AIUnavailable):
            await GeminiProvider(http, "test-key", "test-model").generate("{}")


async def test_timeout():
    def handle(request):
        raise httpx.ReadTimeout("timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(AIUnavailable):
            await GeminiProvider(http, "test-key", "test-model").generate("{}")


async def test_missing_key_no_request():
    client = AsyncMock()
    with pytest.raises(AIUnavailable, match="not been configured"):
        await GeminiProvider(client, "", "model").generate("{}")
    client.post.assert_not_called()


async def test_success():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "candidates": [
                        {"finishReason": "STOP", "content": {"parts": [{"text": "Факти [101]"}]}}
                    ]
                },
            )
        )
    ) as http:
        assert await GeminiProvider(http, "test-key", "test-model").generate("{}") == "Факти [101]"


async def test_cache_budget_and_failure(sessions, source):
    with sessions.begin() as db:
        channel = Channel(username="durov")
        db.add(channel)
        db.flush()
        channel_id = channel.id
    await Collector(sessions, source).collect(channel_id)
    provider = AsyncMock(generate=AsyncMock(return_value="Факти з дописів [101]"))
    service = DigestService(sessions, provider, "model", daily_limit=2)
    first = await service.generate(channel_id, 7)
    second = await service.generate(channel_id, 7)
    assert first.id == second.id
    assert provider.generate.call_count == 1
    with pytest.raises(AIUnavailable, match="cooling"):
        await service.generate(channel_id, 30)
    with sessions.begin() as db:
        db.get(Control, 1).next_ai_at = utcnow() - timedelta(seconds=1)
    provider.generate.side_effect = AIUnavailable("Quota reached")
    with pytest.raises(AIUnavailable, match="Quota"):
        await service.generate(channel_id, 30)
    with sessions.begin() as db:
        assert db.scalar(select(func.count(Digest.id))) == 2
        db.get(Control, 1).next_ai_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(AIUnavailable, match="Daily"):
        await service.generate(channel_id, 90)
    assert (await service.generate(channel_id, 7)).content == first.content


async def test_empty_period_does_not_call_provider(sessions):
    provider = AsyncMock()
    with pytest.raises(AIUnavailable, match="no collected"):
        await DigestService(sessions, provider, "model").generate(1, 7)
    provider.generate.assert_not_called()
