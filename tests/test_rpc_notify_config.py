from __future__ import annotations

import json

import httpx2 as httpx
import pytest

from conftest import WALLET
from paywatch.chains import ConfigError
from paywatch.config import load_config
from paywatch.notify import (
    ID_HEADER,
    SIGNATURE_HEADER,
    DiscordNotifier,
    NotifyError,
    TelegramNotifier,
    WebhookNotifier,
    format_message,
    sign,
    verify_signature,
)
from paywatch.rpc import LogScanner, NodeRefused, RpcClient, RpcError

GOOD, BAD = "https://good.test", "https://bad.test"


def client(handler, urls=(BAD, GOOD), sleeps=None) -> RpcClient:
    record = sleeps.append if sleeps is not None else (lambda _seconds: None)
    return RpcClient(urls, transport=httpx.MockTransport(handler), sleep=record)


def result(value) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": value})


def refusal(code: int, message: str) -> httpx.Response:
    return httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}
    )


@pytest.mark.parametrize(
    "broken",
    [
        httpx.Response(503, text="overloaded"),
        httpx.Response(200, text="<html>rate limited</html>"),
        httpx.Response(200, json="Unauthorized"),
        refusal(-32005, "limit exceeded"),
    ],
)
def test_next_endpoint_is_used_when_one_cannot_serve_the_call(broken):
    def handler(request: httpx.Request) -> httpx.Response:
        return broken if request.url.host == "bad.test" else result("0x10")

    assert client(handler).block_number() == 16


def test_connection_failure_also_falls_over():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bad.test":
            raise httpx.ConnectError("refused", request=request)
        return result("0x10")

    assert client(handler).block_number() == 16


def test_refusal_from_every_endpoint_is_raised_at_once_with_its_code():
    sleeps: list[float] = []
    rpc = client(lambda _request: refusal(-32602, "range too large"), sleeps=sleeps)
    with pytest.raises(NodeRefused) as raised:
        rpc.call("eth_getLogs", [{}])
    assert raised.value.code == -32602
    assert sleeps == []  # waiting would not change a deterministic answer


def test_unreachable_endpoints_are_retried_with_backoff_then_reported():
    sleeps: list[float] = []
    rpc = client(lambda _request: httpx.Response(502), sleeps=sleeps)
    with pytest.raises(RpcError, match="rpc_urls"):
        rpc.block_number()
    assert sleeps == [1.0, 2.0]


class RangeLimited:
    """A node that refuses eth_getLogs over more than `limit` blocks."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.served: list[int] = []

    def call(self, method: str, params: list) -> list:
        size = int(params[0]["toBlock"], 16) - int(params[0]["fromBlock"], 16) + 1
        if size > self.limit:
            raise NodeRefused(-32602, "too many blocks")
        self.served.append(size)
        return []


def test_scanner_halves_a_refused_range_and_covers_every_block_once():
    node = RangeLimited(60)
    chunks = [last for last, _logs in LogScanner(node, chunk=500).scan([], [], 1, 300)]
    assert sum(node.served) == 300
    assert chunks[-1] == 300
    assert max(node.served) <= 60


def test_scanner_grows_the_range_again_after_success():
    node = RangeLimited(10_000)
    list(LogScanner(node, chunk=100, max_chunk=400).scan([], [], 1, 2000))
    assert node.served[:4] == [100, 200, 400, 400]


def test_scanner_gives_up_when_even_one_block_is_refused():
    with pytest.raises(NodeRefused):
        list(LogScanner(RangeLimited(0), chunk=8).scan([], [], 1, 20))


def test_alert_text_has_amount_parties_and_link(payment):
    assert format_message(payment).splitlines() == [
        "Payment confirmed: 60.00 USDT on BNB Smart Chain",
        "To 0x4620...9060 from 0x5836...30ac",
        payment.explorer_url,
    ]


def test_webhook_is_signed_over_the_exact_body_and_carries_an_id(payment):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"], seen["headers"] = request.content, request.headers
        return httpx.Response(204)

    WebhookNotifier(
        "https://shop.test/hook", "s3cret", transport=httpx.MockTransport(handler)
    ).send(payment)

    assert verify_signature("s3cret", seen["body"], seen["headers"][SIGNATURE_HEADER])
    assert not verify_signature("other", seen["body"], seen["headers"][SIGNATURE_HEADER])
    assert not verify_signature("s3cret", seen["body"] + b" ", seen["headers"][SIGNATURE_HEADER])
    assert seen["headers"][ID_HEADER] == payment.key
    body = json.loads(seen["body"])
    assert (body["event"], body["id"], body["amount"]) == (
        "payment.confirmed",
        payment.key,
        "60.00",
    )
    assert body["value"] == str(60 * 10**18)


def test_signature_matches_a_known_hmac_value():
    assert sign("key", b"The quick brown fox jumps over the lazy dog") == (
        "sha256=f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
    )


def test_telegram_alert_goes_to_the_chat_and_a_failure_never_shows_the_token(payment):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    TelegramNotifier("123:TOKEN", "-1001", transport=httpx.MockTransport(handler)).send(payment)
    assert seen["path"] == "/bot123:TOKEN/sendMessage"
    assert seen["body"]["chat_id"] == "-1001"
    assert seen["body"]["text"].startswith("Payment confirmed: 60.00 USDT")

    failing = httpx.MockTransport(lambda _request: httpx.Response(401))
    with pytest.raises(NotifyError) as raised:
        TelegramNotifier("123:TOKEN", "-1001", transport=failing).send(payment)
    assert str(raised.value) == "Telegram answered HTTP 401"


def test_discord_alert_is_posted_as_message_content(payment):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    DiscordNotifier(
        "https://discord.test/api/webhooks/1/x", transport=httpx.MockTransport(handler)
    ).send(payment)
    assert seen["body"] == {"content": format_message(payment)}


def write_config(tmp_path, text: str):
    path = tmp_path / "paywatch.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_config_builds_watches_with_per_token_minimums(tmp_path):
    path = write_config(
        tmp_path,
        f'[[watch]]\nchain = "bsc"\naddress = "{WALLET}"\ntokens = ["USDT"]\nmin_amount = "1.5"\n'
        f'[[watch]]\nchain = "ethereum"\naddress = "{WALLET}"\ntokens = ["USDT", "USDC"]\n',
    )
    config = load_config(path, env={})
    bsc, ethereum = config.watches
    assert bsc.min_value[bsc.tokens[0].address] == 15 * 10**17  # 18 decimals on BNB Smart Chain
    assert bsc.confirmations == 15
    assert [token.decimals for token in ethereum.tokens] == [6, 6]
    assert [notifier.name for notifier in config.notifiers] == ["console"]


def test_config_accepts_a_custom_token_and_your_own_rpc(tmp_path):
    path = write_config(
        tmp_path,
        '[rpc]\npolygon = ["https://my-node.test"]\n'
        '[[token]]\nchain = "polygon"\nsymbol = "usdt0"\n'
        'address = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"\ndecimals = 6\n'
        f'[[watch]]\nchain = "polygon"\naddress = "{WALLET}"\ntokens = ["USDT0"]\n',
    )
    config = load_config(path, env={})
    assert config.watches[0].tokens[0].symbol == "USDT0"
    assert config.watches[0].chain.rpc_urls == ("https://my-node.test",)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[[watch]]\nchain = "bsc"\naddress = "0xYourWalletAddress"\n', "[[watch]] #1"),
        (f'[[watch]]\nchain = "tron"\naddress = "{WALLET}"\n', "Unknown chain 'tron'"),
        (
            f'[[watch]]\nchain = "polygon"\naddress = "{WALLET}"\ntokens = ["USDT"]\n',
            "Token 'USDT'",
        ),
        (f'[[watch]]\nchain = "bsc"\naddress = "{WALLET}"\nmin_amount = "lots"\n', "min_amount"),
        ("poll_seconds = 15\n", "no [[watch]] entry"),
        ("this is not toml", "not valid TOML"),
        (
            f'[[watch]]\nchain = "bsc"\naddress = "{WALLET}"\n[notify]\ntelegram_chat_id = "1"\n',
            "PAYWATCH_TELEGRAM_TOKEN",
        ),
    ],
)
def test_config_mistake_is_explained(tmp_path, text, message):
    with pytest.raises(ConfigError) as raised:
        load_config(write_config(tmp_path, text), env={})
    assert message in str(raised.value)


def test_example_config_fails_only_on_its_placeholder_address():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "examples" / "paywatch.example.toml"
    with pytest.raises(ConfigError, match="0xYourWalletAddress"):
        load_config(example, env={})
