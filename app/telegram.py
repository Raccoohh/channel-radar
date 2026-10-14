import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup


class SourceError(Exception):
    """Safe, user-facing source failure; never contains raw upstream responses."""


def normalize_username(value: str) -> str:
    value = value.strip()
    if value.lower().startswith(("t.me/", "telegram.me/")):
        value = "https://" + value
    if value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        if parsed.netloc.lower() not in {"t.me", "telegram.me"} or parsed.query or parsed.fragment:
            raise ValueError("Use a public Telegram username or t.me channel URL.")
        value = parsed.path.strip("/")
        if value.startswith("s/"):
            value = value[2:]
    value = value.removeprefix("@").lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{3,30}[a-z0-9]", value):
        raise ValueError("Username must contain 5–32 letters, digits or underscores.")
    return value


def parse_count(value: str | None) -> int | None:
    if not value:
        return None
    value = re.sub(r"[\s\u00a0\u202f]", "", value).upper()
    if re.fullmatch(r"\d{1,3}(,\d{3})+", value):
        return int(value.replace(",", ""))
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)([KMB]?)", value)
    if not match:
        return None
    number, suffix = match.groups()
    number = number.replace(",", ".")
    return int(Decimal(number) * {"": 1, "K": 1000, "M": 1000000, "B": 1000000000}[suffix])


@dataclass
class ParsedPost:
    message_id: int
    text: str
    published_at: datetime
    url: str
    views: int | None
    reactions: int | None


@dataclass
class ParsedChannel:
    title: str
    description: str
    subscribers: int | None
    posts: list[ParsedPost]


def parse_channel(html: str, username: str) -> ParsedChannel:
    soup = BeautifulSoup(html, "html.parser")
    info = soup.select_one(".tgme_channel_info")
    if not info or not soup.select_one(".tgme_channel_history"):
        raise SourceError(
            "Public preview unavailable: channel may be private, missing or restricted."
        )
    title = info.select_one(".tgme_channel_info_header_title")
    description = info.select_one(".tgme_channel_info_description")
    subscribers = None
    for counter in info.select(".tgme_channel_info_counter"):
        kind = counter.select_one(".counter_type")
        value = counter.select_one(".counter_value")
        if kind and "subscriber" in kind.get_text().lower() and value:
            subscribers = parse_count(value.get_text())
    posts = {}
    nodes = soup.select(".tgme_channel_history .tgme_widget_message")
    for node in nodes:
        identity = node.get("data-post", "").split("/")
        if len(identity) == 2 and identity[0].lower() != username:
            continue
        if len(identity) != 2 or not identity[1].isdigit():
            raise SourceError("Telegram preview format changed: invalid message identity.")
        timestamp = node.select_one(".tgme_widget_message_date time[datetime]")
        if timestamp is None:
            raise SourceError("Telegram preview format changed: message timestamp missing.")
        try:
            published = datetime.fromisoformat(timestamp["datetime"].replace("Z", "+00:00"))
            if published.tzinfo is None:
                raise ValueError("Missing timezone")
        except ValueError as exc:
            raise SourceError(
                "Telegram preview format changed: invalid message timestamp."
            ) from exc
        content = node.select_one(".tgme_widget_message_text")
        if content:
            for br in content.find_all("br"):
                br.replace_with("\n")
            for unsafe in content.select("script, style"):
                unsafe.decompose()
        views = node.select_one(".tgme_widget_message_views")
        reaction_values = []
        for reaction in node.select(".tgme_reaction:not(.tgme_reaction_paid)"):
            count = reaction.select_one(".count")
            label = count.get_text() if count else reaction.get_text()
            if count:
                reaction_values.append(parse_count(label))
            else:
                match = re.search(r"(\d[\d.,\s]*[KMB]?)\s*$", label, re.IGNORECASE)
                reaction_values.append(parse_count(match.group(1)) if match else None)
        reactions = (
            sum(reaction_values)
            if reaction_values and all(v is not None for v in reaction_values)
            else None
        )
        message_id = int(identity[1])
        posts[message_id] = ParsedPost(
            message_id,
            content.get_text().strip() if content else "",
            published.astimezone(UTC),
            f"https://t.me/{username}/{message_id}",
            parse_count(views.get_text() if views else None),
            reactions,
        )
    if nodes and not posts:
        raise SourceError("Telegram preview format changed: messages could not be parsed.")
    return ParsedChannel(
        title.get_text(" ", strip=True) if title else username,
        description.get_text("\n", strip=True) if description else "",
        subscribers,
        sorted(posts.values(), key=lambda p: p.message_id),
    )


class TelegramClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def fetch(self, username: str, before: int | None = None) -> ParsedChannel:
        username = normalize_username(username)
        for attempt in range(3):
            try:
                async with self.client.stream(
                    "GET",
                    f"https://t.me/s/{username}",
                    params={"before": before} if before else {},
                    timeout=12,
                    follow_redirects=False,
                    headers={"User-Agent": "ChannelRadar/0.1 public-preview analytics"},
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < 2:
                            delay = response.headers.get("retry-after", "2")
                            await asyncio.sleep(min(int(delay), 5) if delay.isdigit() else 2)
                            continue
                        raise SourceError(
                            "Telegram is busy or rate limiting requests. Retry later."
                        )
                    if response.status_code != 200:
                        raise SourceError(
                            "Public preview unavailable or access refused by Telegram."
                        )
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 2_000_000:
                            raise SourceError("Telegram preview exceeds the safe response size.")
                    return parse_channel(chunks.decode("utf-8", errors="replace"), username)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 2:
                    raise SourceError("Telegram connection timed out or is unavailable.") from exc
                await asyncio.sleep(attempt + 1)
        raise SourceError("Telegram unavailable.")
