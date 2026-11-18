"""Scan for confirmed incoming transfers and deliver an alert for each one.

What is promised, precisely:

* Only blocks at least `confirmations` deep are ever read, so an alert means the
  transfer had that many confirmations when it was seen. A reorganization deeper than
  that is not detected and an alert cannot be taken back.
* Each payment is queued once, keyed by chain, transaction hash and log index.
* Delivery is at-least-once per notifier: a crash between sending and recording the
  send repeats that alert after restart. Webhook receivers get an id to drop repeats.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from paywatch.chains import Chain, Token
from paywatch.evm import TRANSFER_TOPIC, address_topic, decode_transfer, format_amount
from paywatch.notify import Notifier, NotifyError
from paywatch.rpc import LogScanner, RpcClient, RpcError
from paywatch.store import Payment, Store

logger = logging.getLogger(__name__)

RETRY_BASE_SECONDS = 30.0
RETRY_MAX_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class Watch:
    chain: Chain
    address: str
    tokens: tuple[Token, ...]
    # Smallest value that counts, per token contract. Zero-value transfers never count:
    # they are how address-poisoning spam gets into a wallet's history.
    min_value: Mapping[str, int]
    confirmations: int

    @property
    def key(self) -> str:
        return f"{self.chain.key}:{self.address}"


class Watcher:
    def __init__(
        self,
        watches: Sequence[Watch],
        clients: Mapping[str, RpcClient],
        store: Store,
        notifiers: Sequence[Notifier],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._watches = list(watches)
        self._clients = clients
        self._scanners = {key: LogScanner(client) for key, client in clients.items()}
        self._store = store
        self._notifiers = {notifier.name: notifier for notifier in notifiers}
        self._clock = clock

    def scan_once(self) -> list[Payment]:
        """Read every watch up to its newest confirmed block. Returns the new payments."""
        found: list[Payment] = []
        for watch in self._watches:
            try:
                found.extend(self._scan(watch))
            except RpcError as exc:
                # The cursor only moves with a committed range, so the next cycle resumes here.
                logger.warning("Scan of %s interrupted: %s", watch.key, exc)
        return found

    def _scan(self, watch: Watch) -> list[Payment]:
        head = self._clients[watch.chain.key].block_number()
        newest_confirmed = head - watch.confirmations + 1
        cursor = self._store.cursor(watch.key)
        # A first run starts at the present. History is read with `--from-block`.
        first = newest_confirmed if cursor is None else cursor + 1
        if first > newest_confirmed:
            return []

        tokens = {token.address: token for token in watch.tokens}
        topics = [TRANSFER_TOPIC, None, address_topic(watch.address)]
        found: list[Payment] = []
        scanner = self._scanners[watch.chain.key]
        for last_block, logs in scanner.scan(list(tokens), topics, first, newest_confirmed):
            payments = []
            for log in logs:
                transfer = decode_transfer(log)
                # The node already filtered by contract and recipient. Checking again costs
                # nothing and means a misbehaving endpoint cannot inject a payment.
                if transfer is None or transfer.token not in tokens:
                    continue
                if transfer.recipient != watch.address:
                    continue
                if transfer.value < max(1, watch.min_value.get(transfer.token, 1)):
                    continue
                token = tokens[transfer.token]
                payments.append(
                    Payment(
                        chain=watch.chain.key,
                        chain_name=watch.chain.name,
                        symbol=token.symbol,
                        amount=format_amount(transfer.value, token.decimals),
                        value=str(transfer.value),
                        sender=transfer.sender,
                        recipient=transfer.recipient,
                        tx_hash=transfer.tx_hash,
                        log_index=transfer.log_index,
                        block_number=transfer.block_number,
                        explorer_url=watch.chain.tx_url(transfer.tx_hash),
                    )
                )
            found.extend(self._store.commit(watch.key, last_block, payments, list(self._notifiers)))
        return found

    def deliver(self) -> int:
        """Send every alert that is due. Returns how many were delivered."""
        delivered = 0
        for pending in self._store.due(self._clock()):
            notifier = self._notifiers.get(pending.notifier)
            if notifier is None:
                continue  # configured in an earlier run and removed since
            try:
                notifier.send(pending.payment)
            except NotifyError as exc:
                delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2**pending.attempts)
                self._store.mark_failed(pending.outbox_id, str(exc), self._clock() + delay)
                logger.warning("%s alert failed, retry in %.0fs: %s", notifier.name, delay, exc)
                continue
            self._store.mark_delivered(pending.outbox_id)
            delivered += 1
        return delivered

    def run(self, poll_seconds: float, stop: threading.Event) -> None:
        while not stop.is_set():
            self.scan_once()
            self.deliver()
            stop.wait(poll_seconds)

    def set_start_block(self, watch: Watch, block: int) -> None:
        """Make the next scan of `watch` begin at `block` (used to read history)."""
        self._store.commit(watch.key, block - 1, [], [])
