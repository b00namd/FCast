"""Notification channels. ntfy is the default; others can implement `Notifier`."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    """ntfy priorities (1 = min, 5 = max)."""

    MIN = 1
    LOW = 2
    DEFAULT = 3
    HIGH = 4
    URGENT = 5


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    priority: Priority = Priority.DEFAULT
    tags: tuple[str, ...] = ()
    click_url: str | None = None
    # (label, url) buttons shown with the notification
    actions: tuple[tuple[str, str], ...] = field(default_factory=tuple)


class NotifierError(Exception):
    pass


class Notifier(ABC):
    name: str

    @abstractmethod
    async def send(self, notification: Notification) -> None:
        """Deliver the notification. Raises `NotifierError` on failure."""

    async def aclose(self) -> None:  # noqa: B027  (optional hook, default no-op)
        """Release resources."""


class LogNotifier(Notifier):
    """Fallback when no channel is configured: alerts only appear in the log."""

    name = "log"

    async def send(self, notification: Notification) -> None:
        logger.info("ALERT %s: %s", notification.title, notification.message.replace("\n", " | "))


class NtfyNotifier(Notifier):
    """Publishes via ntfy's JSON API (UTF-8 safe, supports action buttons)."""

    name = "ntfy"

    def __init__(
        self,
        base_url: str,
        topic: str,
        token: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.topic = topic
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(headers=headers, timeout=timeout, transport=transport)

    def payload(self, notification: Notification) -> dict[str, Any]:
        body: dict[str, Any] = {
            "topic": self.topic,
            "title": notification.title,
            "message": notification.message,
            "priority": int(notification.priority),
        }
        if notification.tags:
            body["tags"] = list(notification.tags)
        if notification.click_url:
            body["click"] = notification.click_url
        if notification.actions:
            body["actions"] = [
                {"action": "view", "label": label, "url": url, "clear": True}
                for label, url in notification.actions
            ]
        return body

    async def send(self, notification: Notification) -> None:
        try:
            response = await self._client.post(self.base_url, json=self.payload(notification))
        except httpx.HTTPError as exc:
            raise NotifierError(f"ntfy not reachable: {exc}") from exc
        if response.status_code >= 400:
            raise NotifierError(f"ntfy answered HTTP {response.status_code}: {response.text[:200]}")

    async def aclose(self) -> None:
        await self._client.aclose()
