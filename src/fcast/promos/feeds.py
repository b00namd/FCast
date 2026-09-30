"""Leak inbox: RSS feeds of FUT news sites (meant for feed readers; robots.txt allows them)."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from defusedxml import ElementTree
from selectolax.parser import HTMLParser

from fcast.sources.base import SourceError

# Only FC / Ultimate Team articles that hint at upcoming content are kept.
_CONTEXT = ("fc 27", "fc27", "ultimate team", " fut ", "fut ", "totw", "team of the week")
_TOPIC = (
    "leak", "promo", "event", "team of the week", "totw", "coming", "release date",
    "revealed", "campaign", "loading screen", "teaser", "confirmed",
)  # fmt: skip
# Guides and tips are not about upcoming content.
_NOISE = ("guide", "how to", "best ", "tips", "explained", "tier list", "faq")


@dataclass(frozen=True)
class FeedItem:
    source: str
    guid: str
    title: str
    url: str
    published: datetime | None
    summary: str

    @property
    def is_leak(self) -> bool:
        return "leak" in self.title.lower()


def _text(element: object, tag: str) -> str:
    node = element.find(tag)  # type: ignore[attr-defined]
    return (node.text or "").strip() if node is not None else ""


def _plain(html: str, limit: int = 600) -> str:
    text = HTMLParser(f"<div>{html}</div>").text(separator=" ") if html else ""
    return re.sub(r"\s+", " ", text).strip()[:limit]


def parse_rss(xml: str, source: str) -> list[FeedItem]:
    """Items of an RSS 2.0 feed (defusedxml protects against XML bombs)."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise SourceError(f"invalid feed from {source}: {exc}") from exc
    items = []
    for item in root.iter("item"):
        title = _plain(_text(item, "title"), 300)
        url = _text(item, "link")
        if not title or not url:
            continue
        published = None
        raw_date = _text(item, "pubDate")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date).astimezone(UTC)
            except (TypeError, ValueError):
                published = None
        items.append(
            FeedItem(
                source=source,
                guid=_text(item, "guid") or url,
                title=title,
                url=url,
                published=published,
                summary=_plain(_text(item, "description")),
            )
        )
    return items


def is_relevant_text(title: str, summary: str = "") -> bool:
    """FC / Ultimate Team article about upcoming content (not a guide)."""
    head = f" {title} ".lower()
    text = f" {title} {summary} ".lower()
    if any(noise in head for noise in _NOISE):
        return False
    return any(c in text for c in _CONTEXT) and any(t in head for t in _TOPIC)


def is_relevant(item: FeedItem) -> bool:
    return is_relevant_text(item.title, item.summary)


def parse_feed_setting(value: str) -> list[tuple[str, str]]:
    """'name=url,name2=url2' -> [(name, url), ...]."""
    feeds = []
    for part in value.split(","):
        name, _, url = part.partition("=")
        if name.strip() and url.strip().startswith("http"):
            feeds.append((name.strip(), url.strip()))
    return feeds
