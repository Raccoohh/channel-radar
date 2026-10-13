import httpx
import pytest

from app.telegram import SourceError, TelegramClient, normalize_username, parse_channel, parse_count


@pytest.mark.parametrize("raw", ["@Durov", "durov", "https://t.me/durov/", "https://t.me/s/durov"])
def test_normalization(raw):
    assert normalize_username(raw) == "durov"


@pytest.mark.parametrize(
    "raw",
    [
        "abc",
        "@",
        "https://evil.test/durov",
        "https://t.me/+invite",
        "https://t.me/durov/1",
        "https://t.me/durov?x=1",
        "a b c",
        "http://t.me@127.0.0.1/durov",
        "1channel",
        "channel_",
    ],
)
def test_invalid_names(raw):
    with pytest.raises(ValueError):
        normalize_username(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.2K", 1200),
        ("10.4M", 10400000),
        ("1,234", 1234),
        ("1\u202f234", 1234),
        ("0", 0),
        (None, None),
        ("?", None),
    ],
)
def test_counts(raw, expected):
    assert parse_count(raw) == expected


def test_fixture(html):
    channel = parse_channel(html, "durov")
    assert channel.subscribers == 10400000
    post, media = channel.posts
    assert post.text == "Привіт світе!\nДругий рядок 🚀"
    assert post.views == 1200
    assert post.reactions == 3512  # Paid Stars excluded.
    assert post.published_at.hour == 7
    assert media.text == ""
    assert media.views is None and media.reactions is None


@pytest.mark.parametrize("html", ["", "<html>broken", "<div>Contact @private</div>"])
def test_unavailable(html):
    with pytest.raises(SourceError):
        parse_channel(html, "durov")


def test_valid_empty_channel():
    channel = parse_channel(
        '<div class="tgme_channel_info"></div><section class="tgme_channel_history"></section>',
        "durov",
    )
    assert channel.posts == []
    assert channel.subscribers is None


def test_malformed_timestamps_detect_drift(html):
    with pytest.raises(SourceError, match="format changed"):
        parse_channel(html.replace("datetime=", "bad-date="), "durov")


@pytest.mark.parametrize("status", [302, 403, 404, 429, 500])
async def test_http_failure(status, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("app.telegram.asyncio.sleep", AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, headers={"retry-after": "0"})
        )
    ) as http:
        with pytest.raises(SourceError):
            await TelegramClient(http).fetch("durov")


async def test_timeout_and_recovery(html, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("app.telegram.asyncio.sleep", AsyncMock())
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await TelegramClient(http).fetch("durov")
    assert len(calls) == 2
    assert len(result.posts) == 2
