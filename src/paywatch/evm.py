"""The small part of the EVM this tool needs: addresses, Transfer logs and token amounts.

Everything here is read-side. There is no key handling, signing or transaction building
anywhere in this package.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from Crypto.Hash import keccak

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_AMOUNT_RE = re.compile(r"(\d+)(?:\.(\d+))?")


class AddressError(ValueError):
    """An address is malformed or fails its checksum."""


def keccak256(data: bytes) -> bytes:
    # Ethereum uses the original Keccak padding. hashlib.sha3_256 is the later NIST variant
    # and gives different digests, so it cannot be used here.
    return keccak.new(digest_bits=256, data=data).digest()


def to_checksum(address: str) -> str:
    """EIP-55 mixed-case form of an address."""
    body = address[2:].lower()
    digest = keccak256(body.encode("ascii")).hex()
    return "0x" + "".join(
        char.upper() if int(digest[index], 16) >= 8 else char for index, char in enumerate(body)
    )


def normalize_address(address: str) -> str:
    """Lowercase form of ``address``. A mixed-case address must pass its EIP-55 checksum.

    All-lowercase and all-uppercase addresses carry no checksum and are accepted as they
    are. A mixed-case one that fails means a character was mistyped, which in a payments
    tool is worth stopping for.
    """
    address = address.strip()
    if not _ADDRESS_RE.fullmatch(address):
        raise AddressError(f"'{address}' is not an address: expected 0x and 40 hex characters.")
    body = address[2:]
    if body not in (body.lower(), body.upper()) and to_checksum(address) != address:
        raise AddressError(
            f"'{address}' fails its checksum, so at least one character is wrong. "
            "Copy it again from the wallet."
        )
    return address.lower()


def address_topic(address: str) -> str:
    """An address as a 32-byte log topic."""
    return "0x" + address[2:].lower().rjust(64, "0")


def short(address: str) -> str:
    return f"{address[:6]}...{address[-4:]}"


@dataclass(frozen=True, slots=True)
class Transfer:
    token: str  # contract that emitted the log
    sender: str
    recipient: str
    value: int  # smallest units, exactly as on-chain
    tx_hash: str
    log_index: int
    block_number: int


def decode_transfer(log: Mapping[str, Any]) -> Transfer | None:
    """Decode an ERC-20 ``Transfer`` log, or return None for anything else.

    An ERC-721 transfer has the same first topic but indexes the token id as a fourth
    topic, so the topic count tells them apart. A log flagged ``removed`` belongs to a
    block that was reorganized away.
    """
    topics = log.get("topics") or []
    if len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC or log.get("removed"):
        return None
    data = log.get("data") or "0x"
    return Transfer(
        token=log["address"].lower(),
        sender="0x" + topics[1][-40:].lower(),
        recipient="0x" + topics[2][-40:].lower(),
        value=int(data, 16) if len(data) > 2 else 0,
        tx_hash=log["transactionHash"].lower(),
        log_index=int(log["logIndex"], 16),
        block_number=int(log["blockNumber"], 16),
    )


def decode_string(result: str) -> str:
    """Decode the return value of ``symbol()``: an ABI string, or bytes32 on a few old tokens."""
    raw = bytes.fromhex(result[2:])
    if len(raw) == 32:
        return raw.rstrip(b"\x00").decode("utf-8", "replace")
    length = int.from_bytes(raw[32:64], "big")
    return raw[64 : 64 + length].decode("utf-8", "replace")


def parse_amount(text: str, decimals: int) -> int:
    """'60.5' -> smallest units. Amounts are integers end to end: no floats, no rounding."""
    match = _AMOUNT_RE.fullmatch(text.strip())
    if not match:
        raise ValueError(f"'{text}' is not an amount. Use digits with an optional decimal point.")
    whole, fraction = match[1], match[2] or ""
    if len(fraction) > decimals:
        raise ValueError(f"'{text}' has more than the token's {decimals} decimal places.")
    return int(whole) * 10**decimals + int(fraction.ljust(decimals, "0") or "0")


def format_amount(value: int, decimals: int) -> str:
    """Smallest units -> exact decimal string with at least two decimals, e.g. '1,250.00'."""
    whole, fraction = divmod(value, 10**decimals)
    digits = str(fraction).rjust(decimals, "0").rstrip("0").ljust(min(2, decimals), "0")
    return f"{whole:,}.{digits}" if digits else f"{whole:,}"
