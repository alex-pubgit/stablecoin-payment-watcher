from __future__ import annotations

import pytest

from conftest import BSC, LOOKALIKE, PAYER, REAL_BLOCK, REAL_LOG, REAL_TX, USDT, WALLET, FakeNode
from paywatch import cli
from paywatch.checktx import check_tx
from paywatch.evm import parse_amount

USDC = BSC.tokens["USDC"]


@pytest.fixture
def node() -> FakeNode:
    node = FakeNode(head=REAL_BLOCK + 99)
    node.receipts[REAL_TX] = {"status": "0x1", "blockNumber": hex(REAL_BLOCK), "logs": [REAL_LOG]}
    return node


def check(node, *, tx=REAL_TX, to=WALLET, minimum="500", token=USDT, confirmations=15):
    return check_tx(node, BSC, token, tx, to, parse_amount(minimum, token.decimals), confirmations)


def test_real_payment_passes_with_its_exact_amount(node):
    verdict = check(node)
    assert verdict.passed
    assert "969.250360260820715703 USDT from the genuine contract" in verdict.reasons[1]
    assert verdict.reasons[2] == "100 confirmations (15 required)"
    assert str(verdict).startswith("PASS\n  - transaction succeeded")


def test_payment_to_someone_else_fails(node):
    verdict = check(node, to=PAYER)
    assert not verdict.passed
    assert verdict.reasons == ("The USDT went to 0x4620...9060, not to 0x5836...30ac.",)


def test_payment_smaller_than_agreed_fails(node):
    verdict = check(node, minimum="5000")
    assert not verdict.passed
    assert "At least 5,000.00 was expected" in verdict.reasons[0]


def test_another_listed_token_is_named(node):
    verdict = check(node, token=USDC, minimum="1")
    assert not verdict.passed
    assert verdict.reasons[1] == "It sends USDT to that address instead."


def test_lookalike_token_fails_with_a_warning(node):
    node.receipts[REAL_TX]["logs"] = [{**REAL_LOG, "address": LOOKALIKE}]
    verdict = check(node)
    assert not verdict.passed
    assert LOOKALIKE in verdict.reasons[1]
    assert "only copies the name USDT" in verdict.reasons[1]


def test_failed_transaction_fails(node):
    node.receipts[REAL_TX]["status"] = "0x0"
    assert check(node).reasons == (
        "The transaction was mined but failed. Nothing was transferred.",
    )


def test_unknown_hash_fails(node):
    verdict = check(node, tx="0x" + "11" * 32)
    assert not verdict.passed
    assert "No such transaction on BNB Smart Chain" in verdict.reasons[0]


def test_text_that_is_not_a_hash_fails_without_asking_the_node(node):
    node.down = True
    assert "is not a transaction hash" in check(node, tx="0xabc").reasons[0]


def test_payment_without_enough_confirmations_is_not_accepted_yet(node):
    node.head = REAL_BLOCK + 3
    verdict = check(node)
    assert not verdict.passed
    assert "4 of 15 confirmations" in verdict.reasons[0]


def test_several_transfers_to_the_recipient_are_added_up(node):
    node.receipts[REAL_TX]["logs"] = [REAL_LOG, {**REAL_LOG, "logIndex": "0x2"}]
    assert check(node, minimum="1900").passed


@pytest.fixture
def fake_rpc(node, monkeypatch) -> FakeNode:
    monkeypatch.setattr(cli, "RpcClient", lambda urls: node)
    return node


def run_check(*extra: str) -> int:
    return cli.main(["check-tx", "--chain", "bsc", "--tx", REAL_TX, "--to", WALLET, *extra])


def test_cli_exit_code_follows_the_verdict(fake_rpc, capsys):
    assert run_check("--min-amount", "500") == 0
    assert capsys.readouterr().out.startswith("PASS")
    assert run_check("--min-amount", "5000") == 1
    assert capsys.readouterr().out.startswith("FAIL")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--to", "0x55d398326f99059fF775485246999027B3197956"], "checksum"),
        (["--token", "DOGE"], "Token 'DOGE' is not configured on BNB Smart Chain"),
        (["--min-amount", "ten"], "is not an amount"),
    ],
)
def test_cli_reports_bad_input_with_exit_code_2(fake_rpc, capsys, arguments, message):
    assert run_check(*arguments) == 2
    assert message in capsys.readouterr().err


def test_cli_reports_an_unreachable_chain_with_exit_code_2(fake_rpc, capsys):
    fake_rpc.down = True
    assert run_check() == 2
    assert "error:" in capsys.readouterr().err


def test_verify_presets_fails_when_the_chain_disagrees(monkeypatch, capsys):
    class WrongDecimals:
        def call(self, method, params):
            if params[0]["data"] == cli.SYMBOL_SELECTOR:
                return "0x" + b"USDT".ljust(32, b"\x00").hex()
            return hex(6)

    monkeypatch.setattr(cli, "RpcClient", lambda urls: WrongDecimals())
    assert cli.main(["verify-presets", "--chain", "bsc"]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH  bsc       USDT" in out
    assert "on-chain: symbol=USDT decimals=6" in out
