"""SQLite state: how far each wallet was scanned, payments seen, alerts still to deliver.

`commit` writes a scanned block range in one transaction: the new payments, one outbox
row per payment and notifier, and the new cursor. So after a crash the process either
re-scans a range (and the unique key drops the duplicates) or finds the alerts waiting
in the outbox. A payment is never lost and never queued twice.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (
    watch TEXT PRIMARY KEY,
    block INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT NOT NULL,
    chain_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    amount TEXT NOT NULL,
    value TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    log_index INTEGER NOT NULL,
    block_number INTEGER NOT NULL,
    explorer_url TEXT NOT NULL,
    UNIQUE (chain, tx_hash, log_index)
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id INTEGER NOT NULL REFERENCES payments (id),
    notifier TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    delivered INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE (payment_id, notifier)
);
"""
_PAYMENT_COLUMNS = (
    "chain, chain_name, symbol, amount, value, sender, recipient, tx_hash, log_index, "
    "block_number, explorer_url"
)


@dataclass(frozen=True, slots=True)
class Payment:
    chain: str
    chain_name: str
    symbol: str
    amount: str  # formatted for people, e.g. "60.00"
    value: str  # exact integer units as text: a uint256 does not fit an SQLite integer
    sender: str
    recipient: str
    tx_hash: str
    log_index: int
    block_number: int
    explorer_url: str

    @property
    def key(self) -> str:
        """Stable id of this payment. Webhook receivers use it to drop a repeated delivery."""
        return f"{self.chain}:{self.tx_hash}:{self.log_index}"


@dataclass(frozen=True, slots=True)
class Pending:
    outbox_id: int
    notifier: str
    attempts: int
    payment: Payment


class Store:
    def __init__(self, path: str | Path) -> None:
        self._db = sqlite3.connect(str(path))
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def cursor(self, watch: str) -> int | None:
        row = self._db.execute("SELECT block FROM cursors WHERE watch = ?", (watch,)).fetchone()
        return row[0] if row else None

    def commit(
        self, watch: str, block: int, payments: Sequence[Payment], notifiers: Sequence[str]
    ) -> list[Payment]:
        """Record a scanned range atomically. Returns the payments not seen before."""
        fresh: list[Payment] = []
        with self._db:
            for payment in payments:
                inserted = self._db.execute(
                    f"INSERT OR IGNORE INTO payments ({_PAYMENT_COLUMNS}) "  # noqa: S608
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        payment.chain,
                        payment.chain_name,
                        payment.symbol,
                        payment.amount,
                        payment.value,
                        payment.sender,
                        payment.recipient,
                        payment.tx_hash,
                        payment.log_index,
                        payment.block_number,
                        payment.explorer_url,
                    ),
                )
                if inserted.rowcount == 0:
                    continue
                fresh.append(payment)
                self._db.executemany(
                    "INSERT INTO outbox (payment_id, notifier) VALUES (?, ?)",
                    [(inserted.lastrowid, notifier) for notifier in notifiers],
                )
            self._db.execute(
                "INSERT INTO cursors (watch, block) VALUES (?, ?) "
                "ON CONFLICT (watch) DO UPDATE SET block = excluded.block",
                (watch, block),
            )
        return fresh

    def due(self, now: float) -> list[Pending]:
        rows = self._db.execute(
            f"SELECT outbox.id, notifier, attempts, {_PAYMENT_COLUMNS} "  # noqa: S608
            "FROM outbox JOIN payments ON payments.id = outbox.payment_id "
            "WHERE delivered = 0 AND next_attempt_at <= ? ORDER BY outbox.id",
            (now,),
        )
        return [Pending(row[0], row[1], row[2], Payment(*row[3:])) for row in rows]

    def mark_delivered(self, outbox_id: int) -> None:
        with self._db:
            self._db.execute("UPDATE outbox SET delivered = 1 WHERE id = ?", (outbox_id,))

    def mark_failed(self, outbox_id: int, error: str, next_attempt_at: float) -> None:
        with self._db:
            self._db.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ?, next_attempt_at = ? "
                "WHERE id = ?",
                (error[:300], next_attempt_at, outbox_id),
            )

    def undelivered(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM outbox WHERE delivered = 0").fetchone()[0]
