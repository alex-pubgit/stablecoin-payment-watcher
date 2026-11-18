"""Shared test doubles. Nothing in the default suite touches the network."""

from __future__ import annotations

from typing import Any

import pytest

from paywatch.chains import CHAINS
from paywatch.evm import TRANSFER_TOPIC, address_topic
from paywatch.rpc import NodeRefused, RpcError
from paywatch.store import Payment, Store

BSC = CHAINS["bsc"]
USDT = BSC.tokens["USDT"]
WALLET = "0x4620f5c127a6055fbebb1f71327d60ff079c9060"
PAYER = "0x5836abdf71efa0e5dd5a590aba36ed5ae75730ac"
LOOKALIKE = "0x00000000000000000000000000000000000000aa"

# A real USDT transfer on BNB Smart Chain, exactly as a node returned it.
REAL_TX = "0xdc1fe171f6a09571188607de09f71a401c50783d7bceee99fd72cf6d275ddebc"
REAL_LOG = {
    "address": "0x55d398326f99059ff775485246999027b3197955",
    "topics": [
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
        "0x0000000000000000000000005836abdf71efa0e5dd5a590aba36ed5ae75730ac",
        "0x0000000000000000000000004620f5c127a6055fbebb1f71327d60ff079c9060",
    ],
    "data": "0x0000000000000000000000000000000000000000000000348b0d0359459bc8b7",
    "blockNumber": "0x800bf23",
    "blockHash": "0xe4178317362c39dc6e968f4c9bde5d256b0eb607dadb55d5601b5ce467ead165",
    "transactionHash": REAL_TX,
    "logIndex": "0x1",
    "removed": False,
}
REAL_VALUE = 969_250_360_260_820_715_703
REAL_BLOCK = 0x800BF23


def make_log(
    block: int,
    value: int,
    *,
    to: str = WALLET,
    sender: str = PAYER,
    token: str = USDT.address,
    index: int = 0,
) -> dict[str, Any]:
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, address_topic(sender), address_topic(to)],
        "data": hex(value),
        "blockNumber": hex(block),
        "transactionHash": "0x" + f"{block:x}{index:x}".rjust(64, "0"),
        "logIndex": hex(index),
        "removed": False,
    }


class FakeNode:
    """An in-memory chain that answers the three calls the tool makes."""

    def __init__(self, head: int = 1000) -> None:
        self.head = head
        self.logs: list[dict[str, Any]] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        self.max_range: int | None = None  # refuse eth_getLogs over more blocks than this
        self.down = False
        self.honest = True  # False: ignore the filter, like a misbehaving endpoint
        self.ranges: list[tuple[int, int]] = []

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def call(self, method: str, params: list[Any]) -> Any:
        if self.down:
            raise RpcError("no endpoint answered")
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        query = params[0]
        first, last = int(query["fromBlock"], 16), int(query["toBlock"], 16)
        if self.max_range is not None and last - first + 1 > self.max_range:
            raise NodeRefused(-32602, f"eth_getLogs is limited to {self.max_range} blocks")
        self.ranges.append((first, last))
        in_range = [log for log in self.logs if first <= int(log["blockNumber"], 16) <= last]
        if not self.honest:
            return in_range
        return [
            log
            for log in in_range
            if log["address"] in query["address"] and log["topics"][2] == query["topics"][2]
        ]


class Collector:
    name = "collector"

    def __init__(self) -> None:
        self.sent: list[Payment] = []

    def send(self, payment: Payment) -> None:
        self.sent.append(payment)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def store() -> Store:
    return Store(":memory:")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def payment() -> Payment:
    return Payment(
        chain="bsc",
        chain_name="BNB Smart Chain",
        symbol="USDT",
        amount="60.00",
        value=str(60 * 10**18),
        sender=PAYER,
        recipient=WALLET,
        tx_hash=REAL_TX,
        log_index=1,
        block_number=REAL_BLOCK,
        explorer_url=BSC.tx_url(REAL_TX),
    )
