"""Where alerts go: console, Telegram, a Discord webhook, or your own signed webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict
from typing import Protocol

import httpx2 as httpx

from paywatch.evm import short
from paywatch.store import Payment

SIGNATURE_HEADER = "X-Paywatch-Signature"
ID_HEADER = "X-Paywatch-Id"


class NotifyError(Exception):
    """An alert could not be delivered. It stays in the outbox and is retried."""


class Notifier(Protocol):
    name: str

    def send(self, payment: Payment) -> None: ...


def format_message(payment: Payment) -> str:
    return (
        f"Payment confirmed: {payment.amount} {payment.symbol} on {payment.chain_name}\n"
        f"To {short(payment.recipient)} from {short(payment.sender)}\n"
        f"{payment.explorer_url}"
    )


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """For webhook receivers: check the signature header against the raw request body."""
    return hmac.compare_digest(sign(secret, body), header)


def _post(client: httpx.Client, target: str, url: str, **request: object) -> None:
    # `target` names the destination in errors. The URL itself may hold a token and never appears.
    try:
        response = client.post(url, **request)
    except httpx.HTTPError as exc:
        raise NotifyError(f"{target} unreachable ({type(exc).__name__})") from None
    if response.status_code >= 400:
        raise NotifyError(f"{target} answered HTTP {response.status_code}")


class ConsoleNotifier:
    name = "console"

    def send(self, payment: Payment) -> None:
        print(format_message(payment), flush=True)


class TelegramNotifier:
    name = "telegram"

    def __init__(
        self, token: str, chat_id: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._client = httpx.Client(timeout=15.0, transport=transport)

    def send(self, payment: Payment) -> None:
        body = {
            "chat_id": self._chat_id,
            "text": format_message(payment),
            "disable_web_page_preview": True,
        }
        _post(self._client, "Telegram", self._url, json=body)


class DiscordNotifier:
    name = "discord"

    def __init__(self, webhook_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._url = webhook_url
        self._client = httpx.Client(timeout=15.0, transport=transport)

    def send(self, payment: Payment) -> None:
        _post(self._client, "Discord webhook", self._url, json={"content": format_message(payment)})


class WebhookNotifier:
    """POST the payment as JSON, signed with HMAC-SHA256 over the exact bytes sent."""

    name = "webhook"

    def __init__(
        self, url: str, secret: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._url = url
        self._secret = secret
        self._client = httpx.Client(timeout=15.0, transport=transport)

    def send(self, payment: Payment) -> None:
        payload = {"event": "payment.confirmed", "id": payment.key, **asdict(payment)}
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(self._secret, body),
            # Delivery is at-least-once. The receiver drops an id it has already processed.
            ID_HEADER: payment.key,
        }
        _post(self._client, "Webhook", self._url, content=body, headers=headers)
