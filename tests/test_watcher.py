from __future__ import annotations

from conftest import BSC, LOOKALIKE, PAYER, USDT, WALLET, Collector, make_log
from paywatch.notify import NotifyError
from paywatch.watcher import RETRY_BASE_SECONDS, Watch, Watcher

ONE = 10**18
CONFIRMATIONS = 15


def watch(min_units: int = ONE) -> Watch:
    return Watch(BSC, WALLET, (USDT,), {USDT.address: min_units}, CONFIRMATIONS)


def watcher(node, store, *notifiers, clock=None, min_units: int = ONE) -> Watcher:
    extra = {"clock": clock} if clock else {}
    return Watcher([watch(min_units)], {"bsc": node}, store, notifiers or [Collector()], **extra)


def test_first_run_starts_at_the_present_and_ignores_history(node, store):
    node.logs = [make_log(500, 60 * ONE)]
    assert watcher(node, store).scan_once() == []
    assert store.cursor("bsc:" + WALLET) == node.head - CONFIRMATIONS + 1


def test_transfer_is_reported_only_when_it_is_deep_enough(node, store):
    bot = watcher(node, store)
    bot.scan_once()
    node.head += 1
    node.logs = [make_log(node.head, 60 * ONE)]  # in the newest block: 1 confirmation
    assert bot.scan_once() == []
    node.head += CONFIRMATIONS - 2
    assert bot.scan_once() == []  # 14 confirmations
    node.head += 1
    (payment,) = bot.scan_once()  # 15 confirmations
    assert (payment.amount, payment.symbol, payment.sender) == ("60.00", "USDT", PAYER)
    assert payment.explorer_url == BSC.tx_url(payment.tx_hash)


def test_history_can_be_read_and_read_again_without_duplicates(node, store):
    collector = Collector()
    node.logs = [make_log(500, 60 * ONE), make_log(600, 5 * ONE)]
    bot = watcher(node, store, collector)
    bot.set_start_block(watch(), 400)
    assert len(bot.scan_once()) == 2
    assert bot.deliver() == 2

    bot.set_start_block(watch(), 400)
    assert bot.scan_once() == []
    assert bot.deliver() == 0
    assert len(collector.sent) == 2


def test_crash_between_recording_and_alerting_loses_nothing(node, store):
    node.logs = [make_log(500, 60 * ONE)]
    first = watcher(node, store, Collector())
    first.set_start_block(watch(), 400)
    first.scan_once()  # the process dies here, before deliver()

    collector = Collector()
    second = watcher(node, store, collector)
    assert second.scan_once() == []
    assert second.deliver() == 1
    assert [payment.amount for payment in collector.sent] == ["60.00"]


def test_zero_value_and_dust_transfers_never_alert(node, store):
    node.logs = [
        make_log(500, 0, index=0),  # address poisoning: a zero-value transfer
        make_log(500, ONE // 100, index=1),  # dust below the minimum
        make_log(500, ONE, index=2),  # exactly the minimum
    ]
    bot = watcher(node, store)
    bot.set_start_block(watch(), 400)
    assert [payment.amount for payment in bot.scan_once()] == ["1.00"]


def test_zero_value_is_ignored_even_with_no_minimum_configured(node, store):
    node.logs = [make_log(500, 0)]
    bot = watcher(node, store, min_units=0)
    bot.set_start_block(watch(0), 400)
    assert bot.scan_once() == []


def test_lookalike_token_and_foreign_recipient_from_a_bad_endpoint_are_dropped(node, store):
    node.honest = False  # the endpoint ignores the filter and returns everything
    node.logs = [
        make_log(500, 60 * ONE, token=LOOKALIKE, index=0),
        make_log(500, 60 * ONE, to=PAYER, index=1),
        make_log(500, 60 * ONE, index=2),
    ]
    bot = watcher(node, store)
    bot.set_start_block(watch(), 400)
    (payment,) = bot.scan_once()
    assert payment.log_index == 2


def test_range_refused_by_the_node_is_split_until_it_is_accepted(node, store):
    node.max_range = 50
    node.logs = [make_log(410, 60 * ONE), make_log(700, 5 * ONE), make_log(985, 7 * ONE)]
    bot = watcher(node, store)
    bot.set_start_block(watch(), 400)
    assert [payment.amount for payment in bot.scan_once()] == ["60.00", "5.00", "7.00"]
    assert all(last - first + 1 <= 50 for first, last in node.ranges)
    assert node.ranges[0][0] == 400
    assert node.ranges[-1][1] == node.head - CONFIRMATIONS + 1


def test_outage_keeps_the_cursor_so_the_next_cycle_resumes(node, store):
    node.logs = [make_log(500, 60 * ONE)]
    bot = watcher(node, store)
    bot.set_start_block(watch(), 400)
    node.down = True
    assert bot.scan_once() == []
    assert store.cursor("bsc:" + WALLET) == 399
    node.down = False
    assert len(bot.scan_once()) == 1


def test_failing_notifier_is_retried_with_backoff_and_blocks_nobody(node, store, clock):
    class Flaky:
        name = "flaky"
        fail = True
        sent = 0

        def send(self, payment) -> None:
            if self.fail:
                raise NotifyError("Webhook answered HTTP 503")
            self.sent += 1

    flaky, collector = Flaky(), Collector()
    node.logs = [make_log(500, 60 * ONE)]
    bot = watcher(node, store, flaky, collector, clock=clock)
    bot.set_start_block(watch(), 400)
    bot.scan_once()

    assert bot.deliver() == 1  # the healthy notifier is served
    assert len(collector.sent) == 1
    assert bot.deliver() == 0  # not due yet
    clock.now += RETRY_BASE_SECONDS
    flaky.fail = False
    assert bot.deliver() == 1
    assert flaky.sent == 1
    assert store.undelivered() == 0
    assert len(collector.sent) == 1


def test_retry_delay_doubles_after_each_failure(node, store, clock):
    class Down:
        name = "down"

        def send(self, payment) -> None:
            raise NotifyError("unreachable")

    node.logs = [make_log(500, 60 * ONE)]
    bot = watcher(node, store, Down(), clock=clock)
    bot.set_start_block(watch(), 400)
    bot.scan_once()
    bot.deliver()
    clock.now += RETRY_BASE_SECONDS
    bot.deliver()  # second failure: next attempt in 60 seconds
    clock.now += RETRY_BASE_SECONDS
    assert store.due(clock.now) == []
    clock.now += RETRY_BASE_SECONDS
    assert len(store.due(clock.now)) == 1
