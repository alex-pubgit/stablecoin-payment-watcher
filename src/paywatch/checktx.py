"""Verify a claimed payment from its transaction hash. "Don't trust a screenshot."

A payment passes only if the transaction succeeded, moved the genuine token contract
(not a look-alike with the same symbol) to the expected address, for at least the
expected amount, and has enough confirmations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from paywatch.chains import Chain, Token
from paywatch.evm import decode_transfer, format_amount, short
from paywatch.rpc import RpcClient

_TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")


@dataclass(frozen=True, slots=True)
class Verdict:
    passed: bool
    reasons: tuple[str, ...]

    def __str__(self) -> str:
        head = "PASS" if self.passed else "FAIL"
        return "\n".join([head, *(f"  - {reason}" for reason in self.reasons)])


def _fail(*reasons: str) -> Verdict:
    return Verdict(False, reasons)


def check_tx(
    client: RpcClient,
    chain: Chain,
    token: Token,
    tx_hash: str,
    recipient: str,
    min_value: int,
    confirmations: int,
) -> Verdict:
    if not _TX_HASH_RE.fullmatch(tx_hash):
        return _fail(f"'{tx_hash}' is not a transaction hash: expected 0x and 64 hex characters.")
    receipt = client.call("eth_getTransactionReceipt", [tx_hash])
    if receipt is None:
        return _fail(
            f"No such transaction on {chain.name}. It may still be pending, "
            "belong to another network, or not exist."
        )
    if receipt.get("status") != "0x1":
        return _fail("The transaction was mined but failed. Nothing was transferred.")

    transfers = [t for log in receipt["logs"] if (t := decode_transfer(log)) is not None]
    genuine = [t for t in transfers if t.token == token.address]
    if not genuine:
        reason = f"The transaction moves no {token.symbol} of the genuine contract {token.address}."
        others = sorted({t.token for t in transfers if t.recipient == recipient})
        if not others:
            return _fail(reason)
        known = {known.address: known.symbol for known in chain.tokens.values()}
        if others[0] in known:
            return _fail(reason, f"It sends {known[others[0]]} to that address instead.")
        return _fail(
            reason,
            f"It sends a token from the unlisted contract {others[0]} to that address. "
            f"A token that only copies the name {token.symbol} is worth nothing.",
        )
    received = sum(t.value for t in genuine if t.recipient == recipient)
    if received == 0:
        went_to = short(genuine[0].recipient)
        return _fail(f"The {token.symbol} went to {went_to}, not to {short(recipient)}.")
    amount = format_amount(received, token.decimals)
    if received < min_value:
        expected = format_amount(min_value, token.decimals)
        return _fail(f"Only {amount} {token.symbol} arrived. At least {expected} was expected.")

    depth = client.block_number() - int(receipt["blockNumber"], 16) + 1
    if depth < confirmations:
        return _fail(
            f"{amount} {token.symbol} arrived, but with {depth} of {confirmations} "
            "confirmations so far. Check again in a moment."
        )
    return Verdict(
        True,
        (
            "transaction succeeded",
            f"{amount} {token.symbol} from the genuine contract to {short(recipient)}",
            f"{depth} confirmations ({confirmations} required)",
            chain.tx_url(tx_hash),
        ),
    )
