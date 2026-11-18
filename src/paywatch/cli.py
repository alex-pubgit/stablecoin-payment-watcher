"""Command line: ``paywatch check-tx | watch | verify-presets``."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from paywatch import __version__
from paywatch.chains import CHAINS, ConfigError, chain
from paywatch.checktx import check_tx
from paywatch.config import load_config
from paywatch.evm import AddressError, decode_string, normalize_address, parse_amount
from paywatch.rpc import RpcClient, RpcError
from paywatch.store import Store
from paywatch.watcher import Watcher

SYMBOL_SELECTOR = "0x95d89b41"  # symbol()
DECIMALS_SELECTOR = "0x313ce567"  # decimals()


def _check_tx(args: argparse.Namespace) -> int:
    target = chain(args.chain)
    token = target.token(args.token)
    client = RpcClient(args.rpc or target.rpc_urls)
    verdict = check_tx(
        client,
        target,
        token,
        args.tx,
        normalize_address(args.to),
        parse_amount(args.min_amount, token.decimals),
        args.confirmations or target.confirmations,
    )
    print(verdict)
    return 0 if verdict.passed else 1


def _watch(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    chains_in_use = {watch.chain.key: watch.chain for watch in config.watches}
    clients = {key: RpcClient(target.rpc_urls) for key, target in chains_in_use.items()}
    store = Store(config.database)
    watcher = Watcher(config.watches, clients, store, config.notifiers)
    if args.from_block is not None:
        if len(config.watches) != 1:
            raise ConfigError("--from-block needs a config file with exactly one [[watch]].")
        watcher.set_start_block(config.watches[0], args.from_block)

    if args.once:
        found = watcher.scan_once()
        delivered = watcher.deliver()
        print(f"{len(found)} new payment(s), {delivered} alert(s) delivered", file=sys.stderr)
        return 0

    print(f"Watching {len(config.watches)} wallet(s). Ctrl+C stops.", file=sys.stderr)
    stop = threading.Event()
    try:
        watcher.run(config.poll_seconds, stop)
    except KeyboardInterrupt:
        stop.set()
    pending = store.undelivered()
    if pending:
        print(f"{pending} alert(s) still queued. They are sent on the next start.", file=sys.stderr)
    return 0


def _read(client: RpcClient, contract: str, selector: str) -> str:
    return client.call("eth_call", [{"to": contract, "data": selector}, "latest"])


def _verify_presets(args: argparse.Namespace) -> int:
    failures = 0
    for target in [chain(args.chain)] if args.chain else CHAINS.values():
        client = RpcClient(target.rpc_urls)
        for token in target.tokens.values():
            try:
                symbol = decode_string(_read(client, token.address, SYMBOL_SELECTOR))
                decimals = int(_read(client, token.address, DECIMALS_SELECTOR), 16)
            except (RpcError, ValueError) as exc:
                print(f"ERROR     {target.key:9} {token.symbol:5} {token.address}  {exc}")
                failures += 1
                continue
            matches = (symbol, decimals) == (token.symbol, token.decimals)
            failures += not matches
            print(
                f"{'ok' if matches else 'MISMATCH':9} {target.key:9} {token.symbol:5} "
                f"{token.address}  on-chain: symbol={symbol} decimals={decimals}"
            )
    return 1 if failures else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paywatch",
        description="Read-only checks and alerts for stablecoin payments on EVM chains.",
    )
    parser.add_argument("--version", action="version", version=f"paywatch {__version__}")
    commands = parser.add_subparsers(required=True, metavar="command")

    check = commands.add_parser("check-tx", help="verify a claimed payment from its hash")
    check.add_argument("--chain", required=True, choices=sorted(CHAINS))
    check.add_argument("--tx", required=True, help="transaction hash")
    check.add_argument("--to", required=True, help="the address that should have been paid")
    check.add_argument("--token", default="USDT")
    check.add_argument("--min-amount", default="0.000001", help="smallest amount to accept")
    check.add_argument("--confirmations", type=int, help="default: the chain's preset")
    check.add_argument("--rpc", action="append", help="RPC URL to use instead of the presets")
    check.set_defaults(run=_check_tx)

    watch = commands.add_parser("watch", help="alert on confirmed incoming payments")
    watch.add_argument("--config", default="paywatch.toml")
    watch.add_argument("--once", action="store_true", help="scan and deliver once, then exit")
    watch.add_argument("--from-block", type=int, help="start reading history at this block")
    watch.set_defaults(run=_watch)

    verify = commands.add_parser(
        "verify-presets", help="read symbol and decimals of every preset token from the chain"
    )
    verify.add_argument("--chain", choices=sorted(CHAINS))
    verify.set_defaults(run=_verify_presets)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # The HTTP client logs one line per request, including the full URL of the Telegram API,
    # which contains the bot token.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    try:
        return args.run(args)
    except (ConfigError, AddressError, ValueError, RpcError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
