"""JSON-RPC over HTTPS with endpoint failover, and log scanning that adapts its range.

Two kinds of failure are kept apart because they need different reactions:

* The endpoint could not be used (timeout, connection error, HTTP error, not JSON).
  Another endpoint is tried, and after a full round the call is retried with backoff.
* The node answered with a JSON-RPC error. For ``eth_getLogs`` that almost always
  means "this range is too large for me", and public nodes disagree wildly about what
  too large is. So the scanner halves the range and tries again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx2 as httpx

logger = logging.getLogger(__name__)


class RpcError(Exception):
    """No configured endpoint could serve the call."""


class NodeRefused(RpcError):  # noqa: N818 - reads better than NodeRefusedError at call sites
    """Every endpoint answered the call with a JSON-RPC error."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(f"node error {code}: {message}")
        self.code = code


class RpcClient:
    def __init__(
        self,
        urls: Sequence[str],
        *,
        timeout: float = 15.0,
        rounds: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not urls:
            raise ValueError("at least one RPC URL is required")
        self._urls = list(urls)
        self._rounds = rounds
        self._sleep = sleep
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def call(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        refusal: NodeRefused | None = None
        for attempt in range(self._rounds):
            for url in self._urls:
                try:
                    response = self._http.post(url, json=payload)
                    response.raise_for_status()
                    body = response.json()
                    error = body.get("error")
                    if error:
                        # Another endpoint may have a higher limit, so keep going down the list.
                        refusal = NodeRefused(error.get("code"), str(error.get("message"))[:200])
                        continue
                    return body["result"]
                except (httpx.HTTPError, ValueError, KeyError, AttributeError) as exc:
                    # URLs can embed provider keys, so only the host is ever logged.
                    host = httpx.URL(url).host
                    logger.warning("%s failed on %s: %s", method, host, type(exc).__name__)
            if refusal:
                # A JSON-RPC error is deterministic. Waiting and asking again will not help.
                raise refusal
            if attempt + 1 < self._rounds:
                self._sleep(2.0**attempt)
        raise RpcError(
            f"No RPC endpoint answered {method} after {self._rounds} rounds. "
            "Check your network, or set your own rpc_urls in the config file."
        )

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)


class LogScanner:
    """Read logs over a block range in chunks sized to what the nodes accept."""

    def __init__(self, client: RpcClient, *, chunk: int = 500, max_chunk: int = 2000) -> None:
        self._client = client
        self._chunk = chunk
        self._max_chunk = max_chunk

    def scan(
        self, contracts: Sequence[str], topics: Sequence[Any], first: int, last: int
    ) -> Iterator[tuple[int, list[dict[str, Any]]]]:
        """Yield ``(last block of the chunk, logs)`` for consecutive chunks of [first, last]."""
        start = first
        while start <= last:
            end = min(start + self._chunk - 1, last)
            query = {
                "address": list(contracts),
                "topics": list(topics),
                "fromBlock": hex(start),
                "toBlock": hex(end),
            }
            try:
                logs = self._client.call("eth_getLogs", [query])
            except NodeRefused:
                if end == start:
                    raise
                self._chunk = max(1, (end - start + 1) // 2)
                logger.info("Nodes refused the range, retrying with %d blocks", self._chunk)
                continue
            yield end, logs
            start = end + 1
            # Grow back slowly: a node that refused 500 blocks may refuse 400 as well.
            self._chunk = min(self._max_chunk, self._chunk * 2)
