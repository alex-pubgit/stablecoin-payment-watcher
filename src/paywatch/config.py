"""Load the TOML config file. Secrets come from environment variables, never from the file."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from paywatch.chains import CHAINS, Chain, ConfigError, Token, chain
from paywatch.evm import AddressError, normalize_address, parse_amount
from paywatch.notify import (
    ConsoleNotifier,
    DiscordNotifier,
    Notifier,
    TelegramNotifier,
    WebhookNotifier,
)
from paywatch.watcher import Watch


@dataclass(frozen=True, slots=True)
class Config:
    database: Path
    poll_seconds: float
    chains: dict[str, Chain]
    watches: tuple[Watch, ...]
    notifiers: tuple[Notifier, ...]


def _address(value: Any, where: str) -> str:
    try:
        return normalize_address(str(value))
    except AddressError as exc:
        raise ConfigError(f"{where}: {exc}") from None


def _secret(env: Mapping[str, str], name: str, needed_for: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{needed_for} needs the environment variable {name}.")
    return value


def _chains(data: Mapping[str, Any]) -> dict[str, Chain]:
    chains = dict(CHAINS)
    for key, urls in data.get("rpc", {}).items():
        if not isinstance(urls, list) or not urls:
            raise ConfigError(f"[rpc] {key} must be a non-empty list of URLs.")
        chains[key] = replace(chain(key, chains), rpc_urls=tuple(urls))
    for index, entry in enumerate(data.get("token", []), start=1):
        where = f"[[token]] #{index}"
        try:
            target = chain(entry["chain"], chains)
            token = Token(
                str(entry["symbol"]).upper(),
                _address(entry["address"], where),
                int(entry["decimals"]),
            )
        except KeyError as exc:
            raise ConfigError(f"{where} is missing {exc}.") from None
        chains[target.key] = target.with_token(token)
    return chains


def _watches(data: Mapping[str, Any], chains: dict[str, Chain]) -> tuple[Watch, ...]:
    watches = []
    for index, entry in enumerate(data.get("watch", []), start=1):
        where = f"[[watch]] #{index}"
        if "chain" not in entry or "address" not in entry:
            raise ConfigError(f"{where} needs both 'chain' and 'address'.")
        target = chain(entry["chain"], chains)
        tokens = tuple(target.token(symbol) for symbol in entry.get("tokens", ["USDT"]))
        try:
            minimum = {
                token.address: parse_amount(str(entry.get("min_amount", "0")), token.decimals)
                for token in tokens
            }
        except ValueError as exc:
            raise ConfigError(f"{where} min_amount: {exc}") from None
        confirmations = int(entry.get("confirmations", target.confirmations))
        if confirmations < 1:
            raise ConfigError(f"{where} confirmations must be at least 1.")
        watches.append(
            Watch(target, _address(entry["address"], where), tokens, minimum, confirmations)
        )
    if not watches:
        raise ConfigError("The config file has no [[watch]] entry, so there is nothing to watch.")
    return tuple(watches)


def _notifiers(data: Mapping[str, Any], env: Mapping[str, str]) -> tuple[Notifier, ...]:
    settings = data.get("notify", {"console": True})
    notifiers: list[Notifier] = []
    if settings.get("console"):
        notifiers.append(ConsoleNotifier())
    if "telegram_chat_id" in settings:
        token = _secret(env, "PAYWATCH_TELEGRAM_TOKEN", "notify.telegram_chat_id")
        notifiers.append(TelegramNotifier(token, str(settings["telegram_chat_id"])))
    if settings.get("discord"):
        notifiers.append(
            DiscordNotifier(_secret(env, "PAYWATCH_DISCORD_WEBHOOK", "notify.discord"))
        )
    if "webhook_url" in settings:
        secret = _secret(env, "PAYWATCH_WEBHOOK_SECRET", "notify.webhook_url")
        notifiers.append(WebhookNotifier(str(settings["webhook_url"]), secret))
    if not notifiers:
        raise ConfigError("[notify] enables no notifier. Set at least console = true.")
    return tuple(notifiers)


def load_config(path: Path, env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigError(f"Cannot read the config file '{path}': {exc.strerror}.") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"'{path}' is not valid TOML: {exc}.") from None
    chains = _chains(data)
    poll_seconds = float(data.get("poll_seconds", 15))
    if poll_seconds < 1:
        raise ConfigError("poll_seconds must be at least 1.")
    return Config(
        database=Path(data.get("database", "paywatch.sqlite3")),
        poll_seconds=poll_seconds,
        chains=chains,
        watches=_watches(data, chains),
        notifiers=_notifiers(data, env),
    )
