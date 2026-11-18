from __future__ import annotations

import pytest

from conftest import PAYER, REAL_BLOCK, REAL_LOG, REAL_TX, REAL_VALUE, WALLET
from paywatch.chains import CHAINS
from paywatch.evm import (
    TRANSFER_TOPIC,
    AddressError,
    address_topic,
    decode_string,
    decode_transfer,
    format_amount,
    keccak256,
    normalize_address,
    parse_amount,
    to_checksum,
)


def test_transfer_topic_is_the_hash_of_the_event_signature():
    assert "0x" + keccak256(b"Transfer(address,address,uint256)").hex() == TRANSFER_TOPIC


@pytest.mark.parametrize(
    "address",
    [  # test vectors from EIP-55
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
        "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
    ],
)
def test_checksum_matches_the_eip55_vectors(address):
    assert to_checksum(address.lower()) == address
    assert normalize_address(address) == address.lower()


def test_one_mistyped_character_fails_the_checksum():
    with pytest.raises(AddressError, match="checksum"):
        normalize_address("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAee")


@pytest.mark.parametrize(
    "address", ["0x1234", "55d398326f99059ff775485246999027b3197955", "0xYour"]
)
def test_malformed_address_is_rejected(address):
    with pytest.raises(AddressError, match="40 hex"):
        normalize_address(address)


def test_lowercase_address_is_accepted_without_a_checksum():
    assert normalize_address(f"  {WALLET}  ") == WALLET


def test_every_preset_token_address_is_stored_normalized():
    for chain in CHAINS.values():
        for token in chain.tokens.values():
            assert token.address == normalize_address(to_checksum(token.address))


def test_real_transfer_log_is_decoded_exactly():
    transfer = decode_transfer(REAL_LOG)
    assert (transfer.sender, transfer.recipient) == (PAYER, WALLET)
    assert transfer.value == REAL_VALUE
    assert (transfer.tx_hash, transfer.log_index, transfer.block_number) == (REAL_TX, 1, REAL_BLOCK)
    assert transfer.token == CHAINS["bsc"].tokens["USDT"].address


def test_nft_transfer_with_a_fourth_topic_is_not_a_payment():
    nft = {**REAL_LOG, "topics": [*REAL_LOG["topics"], address_topic(WALLET)], "data": "0x"}
    assert decode_transfer(nft) is None


def test_log_removed_by_a_reorganization_is_ignored():
    assert decode_transfer({**REAL_LOG, "removed": True}) is None


def test_other_events_are_ignored():
    approval = {**REAL_LOG, "topics": ["0x" + "8c" * 32, *REAL_LOG["topics"][1:]]}
    assert decode_transfer(approval) is None


@pytest.mark.parametrize(
    ("text", "decimals", "value"),
    [
        ("60", 18, 60 * 10**18),
        ("60.5", 6, 60_500_000),
        ("0.000001", 6, 1),
        ("0.000000000000000001", 18, 1),
        ("0", 6, 0),
    ],
)
def test_amounts_are_parsed_to_exact_integer_units(text, decimals, value):
    assert parse_amount(text, decimals) == value


@pytest.mark.parametrize("text", ["0.0000001", "-5", "1e6", "ten", "1,000", ""])
def test_amount_that_cannot_be_exact_is_rejected(text):
    with pytest.raises(ValueError, match=r"amount|decimal"):
        parse_amount(text, 6)


@pytest.mark.parametrize(
    ("value", "decimals", "text"),
    [
        (60 * 10**18, 18, "60.00"),
        (60_500_000, 6, "60.50"),
        (1, 6, "0.000001"),
        (1_250_000_000, 6, "1,250.00"),
        (REAL_VALUE, 18, "969.250360260820715703"),
    ],
)
def test_amounts_are_shown_without_rounding(value, decimals, text):
    assert format_amount(value, decimals) == text


def test_same_number_of_units_is_a_different_amount_on_another_chain():
    # USDT has 18 decimals on BNB Smart Chain and 6 on Ethereum.
    assert format_amount(60_000_000, 6) == "60.00"
    assert format_amount(60_000_000, 18) == "0.00000000006"


def test_symbol_is_decoded_from_string_and_from_bytes32():
    as_string = "0x" + (32).to_bytes(32, "big").hex() + (4).to_bytes(32, "big").hex()
    as_string += b"USDT".ljust(32, b"\x00").hex()
    assert decode_string(as_string) == "USDT"
    assert decode_string("0x" + b"MKR".ljust(32, b"\x00").hex()) == "MKR"
