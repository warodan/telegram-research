"""Tests for scripts/resolve.py: the resolve ledger, freezes and the account lock.

This is the account-safety accounting. No MTProto call is made anywhere in this
file -- the suite is offline by construction -- and no test sleeps;
every timing rule is driven with explicit `now` values.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = (Path(__file__).resolve().parent.parent
           / "skills" / "telegram-research" / "scripts")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pytest

from config import local_tz
from resolve import (
    AccountBusy,
    AccountLock,
    BudgetExhausted,
    LedgerState,
    ResolveFrozen,
    ResolveLedger,
    _today,
    peer_is_usable,
    session_fingerprint,
)


# --------------------------------------------------------------------------
# Daily ceiling
# --------------------------------------------------------------------------
def test_daily_ceiling_refuses_181st_resolve_and_names_the_number(tmp_path):
    ledger = ResolveLedger(tmp_path / "ledger.json", daily_ceiling=180)
    ledger.write(LedgerState(date=_today(), resolves=180))
    with pytest.raises(BudgetExhausted) as exc:
        ledger.check_resolve(now=1_000_000.0)
    msg = str(exc.value)
    assert "180" in msg


# --------------------------------------------------------------------------
# Burst rule -- driven by explicit `now`, never by sleeping
# --------------------------------------------------------------------------
def test_burst_rule_refuses_even_though_daily_total_is_far_from_spent(tmp_path):
    ledger = ResolveLedger(
        tmp_path / "ledger.json",
        daily_ceiling=180, burst_ceiling=3, burst_window=600, min_gap=1.0,
    )
    now = 1_000_000.0
    for i in range(3):
        ledger.record_resolve(f"user{i}", True, now=now)
        now += 2.0  # respects min_gap, stays well inside the burst window

    assert ledger.read().resolves == 3  # daily total is nowhere near the ceiling

    with pytest.raises(BudgetExhausted) as exc:
        ledger.check_resolve(now=now)
    msg = str(exc.value)
    assert "burst ceiling is 3" in msg


# --------------------------------------------------------------------------
# Minimum gap
# --------------------------------------------------------------------------
def test_minimum_gap_refuses_two_resolves_back_to_back(tmp_path):
    ledger = ResolveLedger(tmp_path / "ledger.json", min_gap=30.0)
    now = 2_000_000.0
    ledger.record_resolve("user1", True, now=now)
    with pytest.raises(BudgetExhausted) as exc:
        ledger.check_resolve(now=now + 1.0)
    assert "minimum gap" in str(exc.value)


# --------------------------------------------------------------------------
# freeze()
# --------------------------------------------------------------------------
def test_freeze_then_check_resolve_raises_with_remaining_time(tmp_path):
    ledger = ResolveLedger(tmp_path / "ledger.json")
    now = 3_000_000.0
    ledger.freeze(3600, "FloodWait", now=now)
    with pytest.raises(ResolveFrozen) as exc:
        ledger.check_resolve(now=now + 10)
    msg = str(exc.value)
    # left = int(3600 - 10) = 3590 s = 0 h 59 m
    assert "3590 s" in msg
    assert "0 h 59 m" in msg


def test_freeze_survives_midnight_rollover_while_daily_counters_reset(tmp_path):
    """Telegram's clock is not our calendar: a freeze must not be undone by the
    local-day rollover that resets the daily resolve/join counters."""
    path = tmp_path / "ledger.json"
    yesterday = (datetime.now(local_tz()) - timedelta(days=1)).date().isoformat()
    now_ts = time.time()
    raw_state = {
        "date": yesterday,
        "resolves": 150,
        "joins": 2,
        "last_resolve_ts": now_ts - 100,
        "recent_resolve_ts": [now_ts - 100],
        "frozen_until": now_ts + 3600,   # extends well into "today"
        "frozen_reason": "FloodWait recorded yesterday",
        "fingerprint": "",
    }
    path.write_text(json.dumps(raw_state), encoding="utf-8")

    ledger = ResolveLedger(path)
    with pytest.raises(ResolveFrozen):
        ledger.check_resolve(now=now_ts)

    state = ledger.read()
    assert state.date != yesterday
    assert state.resolves == 0
    assert state.joins == 0
    assert state.frozen_until == now_ts + 3600  # freeze itself is untouched


# --------------------------------------------------------------------------
# Durability
# --------------------------------------------------------------------------
def test_ledger_survives_process_restart(tmp_path):
    path = tmp_path / "ledger.json"
    ledger1 = ResolveLedger(path)
    ledger1.record_resolve("user1", True, now=time.time())

    ledger2 = ResolveLedger(path)  # fresh instance, same path
    state = ledger2.read()
    assert state.resolves == 1


# --------------------------------------------------------------------------
# session_fingerprint
# --------------------------------------------------------------------------
def test_session_fingerprint_stable_nonempty_and_does_not_leak_session():
    session = "1A" + "x" * 200  # StringSession-shaped token
    fp1 = session_fingerprint(session)
    fp2 = session_fingerprint(session)
    assert fp1 == fp2
    assert fp1 != ""
    assert session not in fp1


# --------------------------------------------------------------------------
# peer_is_usable
# --------------------------------------------------------------------------
def test_peer_is_usable_false_for_missing_peer():
    assert peer_is_usable(None, "somefingerprint") is False


def test_peer_is_usable_false_for_no_access_hash():
    peer = {"id": 123, "auth_session_fingerprint": "fp"}
    assert peer_is_usable(peer, "fp") is False


def test_peer_is_usable_false_for_fingerprint_mismatch():
    peer = {"id": 123, "access_hash": 456, "auth_session_fingerprint": "fp-old"}
    assert peer_is_usable(peer, "fp-new") is False


def test_peer_is_usable_false_for_empty_fingerprint():
    peer = {"id": 123, "access_hash": 456, "auth_session_fingerprint": "fp"}
    assert peer_is_usable(peer, "") is False


def test_peer_is_usable_true_only_when_all_three_agree():
    peer = {"id": 123, "access_hash": 456, "auth_session_fingerprint": "fp"}
    assert peer_is_usable(peer, "fp") is True


# --------------------------------------------------------------------------
# Join ceiling
# --------------------------------------------------------------------------
def test_join_ceiling_refuses_fourth_join(tmp_path):
    ledger = ResolveLedger(tmp_path / "ledger.json", join_ceiling=3)
    for _ in range(3):
        ledger.check_join()  # must not raise
        ledger.record_join()
    with pytest.raises(BudgetExhausted) as exc:
        ledger.check_join()
    assert "3" in str(exc.value)


# --------------------------------------------------------------------------
# AccountLock
# --------------------------------------------------------------------------
def test_account_lock_second_acquire_raises_with_first_holders_pid(tmp_path):
    lock_path = tmp_path / "account.lock"
    lock1 = AccountLock(lock_path)
    lock1.acquire()
    try:
        lock2 = AccountLock(lock_path)
        with pytest.raises(AccountBusy) as exc:
            lock2.acquire()
        assert str(os.getpid()) in str(exc.value)
    finally:
        lock1.release()


def test_account_lock_release_frees_it(tmp_path):
    lock_path = tmp_path / "account.lock"
    lock1 = AccountLock(lock_path)
    lock1.acquire()
    lock1.release()
    assert not lock_path.exists()

    lock2 = AccountLock(lock_path)
    lock2.acquire()  # must not raise now that the lock is free
    lock2.release()


def test_account_lock_stale_lock_is_broken_then_acquire_succeeds(tmp_path):
    lock_path = tmp_path / "account.lock"
    lock_path.write_text(
        json.dumps({"owner": "dead-proc", "pid": 999999, "since": "then",
                    "ts": time.time() - 10_000}),
        encoding="utf-8",
    )
    # The mtime is part of the evidence now: a lock nobody has written to for
    # longer than `stale_after` is the definition of abandoned, and a fixture
    # that claims an old `ts` on a file touched one millisecond ago is claiming
    # something that cannot happen. See `test_a_fresh_mtime_keeps_the_lock_held`.
    os.utime(lock_path, (time.time() - 10_000, time.time() - 10_000))
    lock = AccountLock(lock_path, stale_after=100.0)
    lock.acquire()  # the existing lock is far older than stale_after
    try:
        assert lock_path.exists()
        info = json.loads(lock_path.read_text(encoding="utf-8"))
        assert info["pid"] == os.getpid()
    finally:
        lock.release()


def test_account_lock_context_manager_releases_on_exception(tmp_path):
    lock_path = tmp_path / "account.lock"
    with pytest.raises(ValueError):
        with AccountLock(lock_path) as lock:
            assert lock_path.exists()
            raise ValueError("boom")
    assert not lock_path.exists()


# ==========================================================================
# Regression guards, adversarial by construction: these tests corrupt
# a ledger, race two real processes, kill a writer mid-call and move the clock.
# Every one of them fails against the code as it stood before the repair.
# ==========================================================================
import subprocess
import textwrap

import socket

import config as configmod
from resolve import LedgerUnreadable, LedgerWriteFailed

SCRIPTS_DIR = str(SCRIPTS)


def _spawn(tmp_path, name, body, *args):
    """Write a worker script and start it. Real processes, no threads."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(path), SCRIPTS_DIR, *[str(a) for a in args]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir()
                  if p.suffix in (".tmp", ".rmw") or p.name.endswith(".rmw"))


# --------------------------------------------------------------------------
# A ledger that cannot be read must refuse, never report zero
# --------------------------------------------------------------------------
DAMAGED = {
    "truncated": '{"date": "2026-08-24", "resolves": 17, "frozen_unt',
    "empty": "",
    "whitespace": "   \n\n",
    "a list": "[1, 2, 3]",
    "resolves is a word": '{"resolves": "many"}',
    "frozen_until is null": '{"frozen_until": null, "resolves": 3, "date": "x"}',
    "recent is an int": '{"recent_resolve_ts": 5}',
    "a negative count": '{"resolves": -4}',
}


@pytest.mark.parametrize("label", sorted(DAMAGED))
def test_a_damaged_ledger_refuses_instead_of_reporting_a_clean_slate(tmp_path, label):
    """`except (OSError, ValueError): data = {}` turned every one of these into
    "0 resolves today, not frozen" -- the day's count, the burst list, the
    minimum-gap latch and the freeze all reset at once, and the tool reported a
    clean slate. The numeric coercions then sat OUTSIDE that guard, so a hand
    edit escaped as a bare `ValueError` with no instruction attached.
    """
    path = tmp_path / "ledger.json"
    path.write_text(DAMAGED[label], encoding="utf-8")
    ledger = ResolveLedger(path)

    with pytest.raises(LedgerUnreadable):
        ledger.read()
    with pytest.raises(LedgerUnreadable):
        ledger.check_resolve()
    with pytest.raises(LedgerUnreadable):
        ledger.record_resolve("someone", True)
    with pytest.raises(LedgerUnreadable):
        ledger.check_join()

    # LedgerUnreadable is a BudgetExhausted, so every caller that already
    # refuses on a budget refusal refuses here too, without knowing it exists.
    with pytest.raises(BudgetExhausted):
        ledger.check_resolve()

    message = str(pytest.raises(LedgerUnreadable, ledger.read).value)
    assert str(path) in message
    assert "Nothing is resolved" in message


def test_a_ledger_with_a_non_utf8_byte_refuses(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_bytes(b'{"resolves": 3, "note": "\xc0\xe0"}')
    with pytest.raises(LedgerUnreadable):
        ResolveLedger(path).read()


def test_a_ledger_truncated_mid_freeze_refuses_the_resolve(tmp_path):
    """The incident's exact shape: Telegram's 36468 s wait still running, the
    file damaged, and `check_resolve` answering ALLOWED."""
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path)
    ledger.freeze(36468, "FloodWait on resolve of @tdlibchat")
    good = path.read_text(encoding="utf-8")
    path.write_text(good[: len(good) // 2], encoding="utf-8")

    with pytest.raises(LedgerUnreadable):
        ResolveLedger(path).check_resolve()


def test_summary_reports_a_damaged_ledger_as_frozen_rather_than_crashing(tmp_path):
    """`tg.py budget` is what a human runs to find out whether anything is safe.
    A traceback answers that with nothing; "not frozen" answers it with a lie."""
    path = tmp_path / "ledger.json"
    path.write_text("{oh dear", encoding="utf-8")
    summary = ResolveLedger(path).summary()
    assert summary["readable"] is False
    assert summary["frozen"] is True
    assert summary["resolves_today"] is None
    assert "cannot be read" in summary["frozen_reason"]


def test_a_missing_ledger_is_still_a_clean_slate(tmp_path):
    """The distinction that did not exist: no ledger yet is not a damaged one."""
    ledger = ResolveLedger(tmp_path / "never-written.json")
    state = ledger.read()
    assert state.resolves == 0
    assert state.date == _today()
    ledger.check_resolve()                    # must not raise
    assert ledger.summary()["readable"] is True


# --------------------------------------------------------------------------
# The read-modify-write no longer erases a freeze
# --------------------------------------------------------------------------
def test_a_freeze_is_not_erased_by_a_write_that_started_before_it(tmp_path):
    """Reproduced exactly:

        after B.freeze  frozen_until = 1787595056
        after A.write   frozen_until = 0.0
        >>> resolving ALLOWED while Telegram's 36468 s wait is still running
    """
    path = tmp_path / "ledger.json"
    a = ResolveLedger(path)
    b = ResolveLedger(path)

    stale = a.read()                     # A reads first...
    b.freeze(36468, "FloodWait on resolve of @tdlibchat")   # ...B freezes...
    stale.resolves += 1
    a.write(stale)                       # ...and A writes the whole state back

    assert a.read().frozen_until > 0
    with pytest.raises(ResolveFrozen):
        a.check_resolve()


def test_two_processes_recording_resolves_lose_no_count(tmp_path):
    """With two real processes.

        resolves that actually happened: 300
        resolves the ledger counted   : 124
        >>> LOST: 176

    59 % of a day's spend vanished, and 44 of the 300 writes failed outright
    with PermissionError [WinError 5] leaving `<name>.<pid>.tmp` orphans in the
    state directory.
    """
    path = tmp_path / "ledger.json"
    each = 40
    body = """
        import sys
        sys.path.insert(0, sys.argv[1])
        import resolve
        ledger = resolve.ResolveLedger(
            sys.argv[2], daily_ceiling=10 ** 9, burst_ceiling=10 ** 9,
            burst_window=600, min_gap=0.0, join_ceiling=10 ** 9)
        for i in range(int(sys.argv[3])):
            ledger.record_resolve("user%d" % i, True)
        print("done")
    """
    workers = [_spawn(tmp_path, f"w{i}.py", body, path, each) for i in range(2)]
    for w in workers:
        out, err = w.communicate(timeout=180)
        assert w.returncode == 0, err

    assert ResolveLedger(path).read().resolves == 2 * each
    assert _leftovers(tmp_path) == []


def test_a_reader_process_cannot_block_the_freeze(tmp_path):
    """The ledger is exactly the file other processes read.

    A second process reading it in a loop used to make `os.replace` fail, so a
    FloodWait freeze arriving at that moment never reached disk and only the
    run-local latch survived -- and that dies with the process.
    """
    path = tmp_path / "ledger.json"
    ResolveLedger(path).record_resolve("warmup", True)
    body = """
        import sys, time
        sys.path.insert(0, sys.argv[1])
        import resolve
        ledger = resolve.ResolveLedger(sys.argv[2])
        end = time.time() + 3.0
        while time.time() < end:
            try:
                ledger.read()
            except Exception:
                pass
    """
    reader = _spawn(tmp_path, "reader.py", body, path)
    try:
        time.sleep(0.4)
        ledger = ResolveLedger(path)
        for _ in range(15):
            ledger.freeze(36468, "FloodWait on resolve of @tdlibchat")
        assert ledger.read().frozen_until > time.time() + 36000
    finally:
        reader.kill()
        reader.wait()
    assert _leftovers(tmp_path) == []


def test_a_write_that_cannot_land_raises_rather_than_returning(tmp_path, monkeypatch):
    """A freeze the caller believes was recorded is worse than one that failed
    loudly: the run carries on resolving on the strength of it."""
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path)

    monkeypatch.setattr(configmod, "atomic_write_text",
                        lambda *a, **k: (_ for _ in ()).throw(
                            configmod.AtomicWriteFailed("no")))
    with pytest.raises(LedgerWriteFailed):
        ledger.freeze(36468, "FloodWait")


# --------------------------------------------------------------------------
# A resolve is counted BEFORE it happens
# --------------------------------------------------------------------------
def test_a_reserved_resolve_is_on_disk_before_the_call(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, min_gap=0.0)
    token = ledger.reserve_resolve("tdlibchat")

    fresh = ResolveLedger(path)            # a different process's view
    state = fresh.read()
    assert state.resolves == 1
    assert state.last_resolve_ts > 0       # the minimum-gap latch is armed too
    assert [p["username"] for p in state.pending] == ["tdlibchat"]

    ledger.settle_resolve(token, ok=True)
    assert ResolveLedger(path).read().pending == []
    assert ResolveLedger(path).read().resolves == 1     # settling counts nothing


def test_reserve_then_record_resolve_counts_exactly_once(tmp_path):
    """`account.py` calls `record_resolve` after the call. With the token it
    reserved with, the ledger ends with one entry and no open reservation.

    CHANGED from an earlier version of this test, which used to reserve, then call
    `record_resolve("tdlibchat", ok=True)` with no token, and assert one entry --
    which is the defect stated as a guarantee: matching a settlement by name (and
    then, when the name did not match, by "the oldest reservation with no name on
    it") is what let a healthy call close a dead run's reservation. The token is
    the identity now, and `account.py` passes it.
    """
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, min_gap=0.0)
    token = ledger.reserve_resolve("tdlibchat")
    ledger.record_resolve("tdlibchat", ok=True, token=token)
    assert ledger.read().resolves == 1
    assert ledger.read().pending == []


def test_the_old_check_then_record_order_still_counts_once(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, min_gap=0.0)
    ledger.check_resolve()
    ledger.record_resolve("tdlibchat", ok=True)
    assert ledger.read().resolves == 1


def test_a_process_killed_between_reserve_and_settle_still_paid(tmp_path):
    """With a real kill.

        attempt 1..5: check_resolve ALLOWED; call made; process dies before
        record_resolve -> ledger says resolves=0
        >>> 5 real resolveUsername calls, ledger total 0, min-gap latch never armed

    `except Exception` does not catch KeyboardInterrupt, so Ctrl+C during the
    call skipped the accounting entirely. This is the incident's shape: the
    account spent while the accounting says it is not.
    """
    path = tmp_path / "ledger.json"
    body = """
        import os, sys
        sys.path.insert(0, sys.argv[1])
        import resolve
        ledger = resolve.ResolveLedger(sys.argv[2], min_gap=0.0)
        ledger.reserve_resolve("tdlibchat")
        os._exit(1)                      # the call is in flight and we are gone
    """
    worker = _spawn(tmp_path, "killed.py", body, path)
    worker.communicate(timeout=120)

    state = ResolveLedger(path).read()
    assert state.resolves == 1
    assert state.last_resolve_ts > 0
    assert len(ResolveLedger(path).pending_resolves()) == 1
    assert ResolveLedger(path).summary()["pending_resolves"] == 1


# --------------------------------------------------------------------------
# The daily reset is a rollover, not an inequality
# --------------------------------------------------------------------------
def _write_raw(path: Path, **fields) -> None:
    payload = {"date": _today(), "resolves": 0, "joins": 0, "last_resolve_ts": 0.0,
               "recent_resolve_ts": [], "frozen_until": 0.0, "frozen_reason": "",
               "fingerprint": ""}
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_ledger_stamped_with_tomorrow_does_not_hand_back_a_spent_day(tmp_path):
    """Measured before the repair:

        on disk: date=2026-08-25 resolves=180 joins=3
        read():  date=2026-08-24 resolves=0   joins=0
        >>> 181st resolve of the same real day ALLOWED; join budget also back to 0

    A clock minutes ahead across local midnight, an NTP correction, a VM clock
    from a snapshot, a second writer whose clock is off: any of them.
    """
    path = tmp_path / "ledger.json"
    tomorrow = (datetime.now(local_tz()) + timedelta(days=1)).date().isoformat()
    _write_raw(path, date=tomorrow, resolves=180, joins=3)

    ledger = ResolveLedger(path, daily_ceiling=180, join_ceiling=3)
    state = ledger.read()
    assert state.resolves == 180
    assert state.joins == 3
    assert state.date == tomorrow          # the future stamp is kept, not overwritten
    with pytest.raises(BudgetExhausted):
        ledger.check_resolve()
    with pytest.raises(BudgetExhausted):
        ledger.check_join()


def test_a_ledger_stamped_with_yesterday_still_rolls_over(tmp_path):
    path = tmp_path / "ledger.json"
    yesterday = (datetime.now(local_tz()) - timedelta(days=1)).date().isoformat()
    _write_raw(path, date=yesterday, resolves=180, joins=3)
    state = ResolveLedger(path).read()
    assert state.date == _today()
    assert state.resolves == 0
    assert state.joins == 0


def test_a_freeze_survives_a_clock_jumped_a_day_forward(tmp_path):
    """The second half of the same defect, measured:

        check_resolve(now=now+86400) on a 36468 s freeze returns ALLOWED

    The deadline was a bare `time.time()` value, so any forward clock movement
    ended the freeze early. It now carries a monotonic twin, anchored to this
    machine's uptime, that no clock change can move.
    """
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path)
    ledger.freeze(36468, "FloodWait on resolve of @tdlibchat")

    with pytest.raises(ResolveFrozen):
        ledger.check_resolve(now=time.time() + 86_400)
    with pytest.raises(ResolveFrozen):
        ResolveLedger(path).check_resolve(now=time.time() + 10 * 86_400)


def test_a_clock_moved_backwards_keeps_the_freeze(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path)
    ledger.freeze(3600, "FloodWait")
    with pytest.raises(ResolveFrozen):
        ledger.check_resolve(now=time.time() - 86_400)


# --------------------------------------------------------------------------
# A fast clock must not lock the burst window for a day
# --------------------------------------------------------------------------
def test_timestamps_from_a_wildly_fast_clock_do_not_lock_the_burst_window(tmp_path):
    """`now - t < burst_window` is true for EVERY future timestamp, so eight
    resolves recorded while the clock was a day ahead refused every resolve for
    a day after the correction."""
    path = tmp_path / "ledger.json"
    ahead = time.time() + 86_400
    _write_raw(path, recent_resolve_ts=[ahead + i for i in range(8)],
               last_resolve_ts=0.0)
    ledger = ResolveLedger(path, burst_ceiling=3, burst_window=600)
    ledger.check_resolve(now=time.time())      # must not raise


def test_timestamps_just_ahead_of_now_still_count_against_the_burst(tmp_path):
    """A second writer whose clock is ten seconds fast is still a burst. Only
    something more than a whole window ahead is a clock artefact."""
    path = tmp_path / "ledger.json"
    now = time.time()
    _write_raw(path, recent_resolve_ts=[now + 10 + i for i in range(8)],
               last_resolve_ts=0.0)
    ledger = ResolveLedger(path, burst_ceiling=3, burst_window=600)
    with pytest.raises(BudgetExhausted):
        ledger.check_resolve(now=now)


# --------------------------------------------------------------------------
# The session fingerprint reaches disk
# --------------------------------------------------------------------------
def test_assigning_the_fingerprint_persists_it(tmp_path):
    """A dry run takes its fingerprint FROM the ledger. An empty field there
    means every cached peer is discarded and every source is planned as a
    resolve, so the plan overstates the budget a human uses to decide whether
    the run is safe."""
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path)
    ledger.fingerprint = "0123456789abcdef"

    assert json.loads(path.read_text(encoding="utf-8"))["fingerprint"] == "0123456789abcdef"
    assert ResolveLedger(path).read().fingerprint == "0123456789abcdef"
    assert ResolveLedger(path).summary()["session_fingerprint"] == "0123456789abcdef"


def test_an_empty_fingerprint_never_overwrites_a_real_one(tmp_path):
    path = tmp_path / "ledger.json"
    ResolveLedger(path).fingerprint = "0123456789abcdef"
    ResolveLedger(path).fingerprint = ""
    assert ResolveLedger(path).read().fingerprint == "0123456789abcdef"


# --------------------------------------------------------------------------
# An unparsable lock is HELD, not stale
# --------------------------------------------------------------------------
UNPARSABLE = {
    "zero bytes": b"",
    "truncated json": b'{"owner": "other-tool", "pid": 4',
    "not an object": b'"other-tool"',
    "not utf-8": b'{"owner": "\xc0\xe0"}',
}


@pytest.mark.parametrize("label", sorted(UNPARSABLE))
def test_an_unparsable_lock_file_is_treated_as_held(tmp_path, label):
    """Measured before the repair:

        >>> acquired instantly over an EXISTING lock file, stale_after=86400 s

    `_read()` returned `{}`, `float(info.get("ts", 0.0))` gave 1970, and the age
    came out at fifty-six years -- always older than `stale_after`. `acquire()`
    itself creates the file empty for an instant, and any lock written by
    another tool (the second writer this exists for) has a schema of its own.
    """
    lock_path = tmp_path / "account.lock"
    lock_path.write_bytes(UNPARSABLE[label])
    with pytest.raises(AccountBusy):
        AccountLock(lock_path, stale_after=86_400.0).acquire()
    assert lock_path.read_bytes() == UNPARSABLE[label]     # and it was not touched


def test_a_lock_without_a_ts_field_is_still_respected(tmp_path):
    """A live foreign lock that merely lacked a `ts` field was broken on sight."""
    lock_path = tmp_path / "account.lock"
    lock_path.write_text(json.dumps({"owner": "other-tool", "pid": 4321}), encoding="utf-8")
    with pytest.raises(AccountBusy) as exc:
        AccountLock(lock_path, stale_after=86_400.0).acquire()
    assert "other-tool" in str(exc.value)


def test_an_unparsable_lock_is_broken_once_it_is_genuinely_old(tmp_path):
    """Held, not immortal: the age comes from the file's own mtime."""
    lock_path = tmp_path / "account.lock"
    lock_path.write_bytes(b"")
    old = time.time() - 5_000
    os.utime(lock_path, (old, old))
    lock = AccountLock(lock_path, stale_after=100.0)
    lock.acquire()
    lock.release()


def test_the_lock_file_is_never_visible_empty(tmp_path):
    """`os.open(O_CREAT|O_EXCL)` made a zero-byte file and `json.dump` filled it
    afterwards; a process arriving inside that window stole a live lock."""
    lock_path = tmp_path / "account.lock"
    lock = AccountLock(lock_path)
    lock.acquire()
    try:
        info = json.loads(lock_path.read_text(encoding="utf-8"))
        assert info["pid"] == os.getpid()
        assert info["ts"] > 0
        assert info["host"]
    finally:
        lock.release()


# --------------------------------------------------------------------------
# A live holder is not robbed, and release() frees only its own lock
# --------------------------------------------------------------------------
def test_a_ledger_write_refreshes_the_lock_so_a_long_run_is_not_robbed(tmp_path):
    """`stale_after` defaults to 1800 s and `MIN_RESOLVE_GAP_SEC`
    is 30 s, so 60 resolves under one lock hold -- a third of the daily ceiling
    -- took exactly long enough for the lock to be declared stale mid-run."""
    lock_path = tmp_path / "account.lock"
    ledger_path = tmp_path / "ledger.json"
    holder = AccountLock(lock_path, stale_after=100.0)
    holder.acquire()
    try:
        # Age the lock past `stale_after`, the way real elapsed time would.
        info = json.loads(lock_path.read_text(encoding="utf-8"))
        info["ts"] = time.time() - 10_000
        lock_path.write_text(json.dumps(info), encoding="utf-8")
        old = time.time() - 10_000
        os.utime(lock_path, (old, old))

        ResolveLedger(ledger_path, min_gap=0.0).record_resolve("tdlibchat", True)

        thief = AccountLock(lock_path, stale_after=100.0, owner="other-tool")
        with pytest.raises(AccountBusy):
            thief.acquire()
        assert holder.owns_the_file() is True
    finally:
        holder.release()


def test_release_does_not_delete_a_lock_someone_else_now_holds(tmp_path):
    """Measured before the repair:

        after the ORIGINAL holder's release(), thief's lock file exists: False
        >>> a THIRD writer acquired with no wait at all

    `release()` unlinked whatever was at the path whenever `_held` was true.
    """
    lock_path = tmp_path / "account.lock"
    original = AccountLock(lock_path, owner="telegram-research")
    original.acquire()

    lock_path.write_text(json.dumps({
        "owner": "other-tool", "pid": os.getpid() + 1, "since": "later",
        "ts": time.time(), "host": socket.gethostname(),
    }), encoding="utf-8")

    original.release()
    assert lock_path.exists()
    assert json.loads(lock_path.read_text(encoding="utf-8"))["owner"] == "other-tool"
    with pytest.raises(AccountBusy):
        AccountLock(lock_path, stale_after=86_400.0).acquire()


def test_breaking_a_stale_lock_is_serialised(tmp_path):
    """Two processes breaking the same stale lock both ended up holding it: the
    unlink was unconditional, so B deleted the lock A had just created."""
    lock_path = tmp_path / "account.lock"
    lock_path.write_text(json.dumps({"owner": "dead", "pid": 999_999,
                                     "since": "then", "ts": 1.0}), encoding="utf-8")
    old = time.time() - 10_000
    os.utime(lock_path, (old, old))

    # Somebody else is already inside the break. Nobody else may be.
    breaker = configmod.FileGuard(lock_path.with_name(lock_path.name + ".break"),
                                  timeout=0.2)
    breaker.acquire()
    try:
        with pytest.raises(AccountBusy):
            AccountLock(lock_path, stale_after=100.0).acquire()
    finally:
        breaker.release()

    winner = AccountLock(lock_path, stale_after=100.0)
    winner.acquire()          # the break is free again
    winner.release()


def test_four_processes_racing_one_stale_lock_produce_one_winner(tmp_path):
    lock_path = tmp_path / "account.lock"
    lock_path.write_text(json.dumps({"owner": "dead", "pid": 999_999,
                                     "since": "then", "ts": 1.0}), encoding="utf-8")
    old = time.time() - 10_000
    os.utime(lock_path, (old, old))

    body = """
        import os, sys, time
        sys.path.insert(0, sys.argv[1])
        import resolve
        lock_path, go = sys.argv[2], sys.argv[3]
        while not os.path.exists(go):
            time.sleep(0.005)
        lock = resolve.AccountLock(lock_path, stale_after=100.0, owner="w%s" % os.getpid())
        try:
            lock.acquire()
        except resolve.AccountBusy:
            print("BUSY")
        else:
            print("GOT")
            time.sleep(1.0)
            lock.release()
    """
    go = tmp_path / "go"
    workers = [_spawn(tmp_path, f"race{i}.py", body, lock_path, go) for i in range(4)]
    time.sleep(0.6)
    go.write_text("go", encoding="utf-8")
    verdicts = []
    for w in workers:
        out, err = w.communicate(timeout=180)
        assert w.returncode == 0, err
        verdicts.append(out.strip())
    assert verdicts.count("GOT") == 1, verdicts


def test_breaking_a_lock_is_recorded(tmp_path):
    """`references/account.md`: "breaking it is recorded rather than silently
    overwritten". It was not recorded anywhere, and the replacement did not
    mention the previous owner."""
    lock_path = tmp_path / "account.lock"
    lock_path.write_text(json.dumps({"owner": "other-tool", "pid": 4321,
                                     "since": "2026-08-24T01:00:00+05:00",
                                     "ts": 1.0}), encoding="utf-8")
    old = time.time() - 10_000
    os.utime(lock_path, (old, old))

    lock = AccountLock(lock_path, stale_after=100.0)
    lock.acquire()
    try:
        record = lock_path.with_name(lock_path.name + ".broken.jsonl")
        assert record.exists()
        line = json.loads(record.read_text(encoding="utf-8").strip())
        assert line["previous"]["owner"] == "other-tool"
        assert line["previous"]["pid"] == 4321
        assert json.loads(lock_path.read_text(encoding="utf-8"))["broke"]["owner"] == "other-tool"
    finally:
        lock.release()


# --------------------------------------------------------------------------
# The recorded pid is checked, but only ever to shorten a wait
# --------------------------------------------------------------------------
def test_a_lock_whose_owner_is_definitively_gone_is_broken_at_once(tmp_path):
    """A lock left by a killed process used to block for the full 30 minutes
    even though the pid was right there in the file."""
    lock_path = tmp_path / "account.lock"
    lock_path.write_text(json.dumps({
        "owner": "telegram-research", "pid": 999_999, "since": "just now",
        "ts": time.time(), "host": socket.gethostname(),
        "pid_created": "win:1:2",
    }), encoding="utf-8")
    lock = AccountLock(lock_path, stale_after=86_400.0)
    lock.acquire()
    lock.release()


# ==========================================================================
# The primitives `account.py` needs, tested here where they live.
# ==========================================================================
def test_plan_resolve_charges_a_state_and_never_the_file(tmp_path):
    """A dry run must meet the ceilings a live run would meet, and spend nothing.

    `account.md` 9: in dry run every source was checked against the same on-disk
    ledger, so thirty sources came back as thirty resolves while the live run of
    that list resolves eight. The simulation reuses the arithmetic that does the
    spending -- `_check` plus the recording step `reserve_resolve` uses -- so the
    preview cannot drift away from the run it describes.
    """
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, daily_ceiling=180, burst_ceiling=8,
                           burst_window=600, min_gap=30.0)
    state = ledger.read()
    now = 5_000_000.0
    for i in range(8):
        ledger.plan_resolve(state, now + i * 30.0)
    assert state.resolves == 8
    with pytest.raises(BudgetExhausted) as exc:
        ledger.plan_resolve(state, now + 8 * 30.0)
    assert "burst ceiling is 8" in str(exc.value)
    assert not path.exists(), "a plan must not write to the ledger"
    assert ResolveLedger(path).read().resolves == 0


def test_check_state_refuses_exactly_what_check_resolve_refuses(tmp_path):
    """The public door onto the same arithmetic, for a state held in memory."""
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, daily_ceiling=2, burst_ceiling=8, min_gap=0.0)
    state = ledger.read()
    ledger.check_state(state, now=1_000.0)          # a clean slate allows it
    state.resolves = 2
    with pytest.raises(BudgetExhausted):
        ledger.check_state(state, now=1_000.0)
    state.resolves = 0
    state.frozen_until = 1_000.0 + 36468
    with pytest.raises(ResolveFrozen):
        ledger.check_state(state, now=1_000.0)


def test_touching_the_held_locks_is_public_and_refreshes_them(tmp_path):
    """account.md 3. `AccountLock.touch()` was reachable only from a ledger
    write, and bulk group history -- the one job the account path exists for --
    writes no ledger entry. The heartbeat is a named, public thing now, so a
    caller that spends the account without touching the ledger can say so."""
    import resolve as resolvemod

    lock_path = tmp_path / "account.lock"
    lock = AccountLock(lock_path, stale_after=100.0)
    lock.acquire()
    try:
        info = json.loads(lock_path.read_text(encoding="utf-8"))
        info["ts"] = time.time() - 10_000
        lock_path.write_text(json.dumps(info), encoding="utf-8")
        old = time.time() - 10_000
        os.utime(lock_path, (old, old))

        resolvemod.touch_held_locks()

        refreshed = json.loads(lock_path.read_text(encoding="utf-8"))
        assert refreshed["ts"] > time.time() - 60
        thief = AccountLock(lock_path, stale_after=100.0, owner="other-tool")
        with pytest.raises(AccountBusy):
            thief.acquire()
        # The private name this module used internally still works.
        assert resolvemod._touch_held_locks is resolvemod.touch_held_locks
    finally:
        lock.release()


def _wait_for(predicate, timeout: float = 10.0, poll: float = 0.02):
    """Poll a condition to a deadline. Returns whether it came true."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


CRASHER = """
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    import resolve
    lock = resolve.AccountLock(sys.argv[2], stale_after=86400.0, owner="crasher")
    lock.acquire()
    print("HELD", flush=True)
    time.sleep(600)                      # ... and then it is killed
"""


def test_a_killed_process_whose_handle_is_still_open_reads_as_gone(tmp_path):
    """account.md 16, with a real kill and a deliberately unreaped child.

    The parent holds the child's handle open -- which is what a shell, a task
    runner or an un-waited `subprocess.Popen` does -- so on Windows the pid stays
    reserved and `GetProcessTimes` keeps answering with the creation time the
    process always had. `_process_identity` therefore returned the SAME token the
    lock recorded and `_is_stale` read a dead owner as alive. Linux is the same
    shape: an unreaped child is a zombie whose `/proc/<pid>/stat` still exists.

    `Popen.wait()` is not called anywhere before the assertion, on purpose: it is
    what would close the handle and make the failure disappear.
    """
    from resolve import _process_identity

    lock_path = tmp_path / "account.lock"
    worker = _spawn(tmp_path, "crasher.py", CRASHER, lock_path)
    try:
        assert _wait_for(lock_path.exists), "the child never took the lock"
        alive = _process_identity(worker.pid)
        assert alive not in ("", None), "a live child must answer with its identity"
        assert alive == json.loads(lock_path.read_text(encoding="utf-8"))["pid_created"]

        worker.kill()                    # no wait(), no communicate(): the handle stays
        assert _wait_for(lambda: _process_identity(worker.pid) == ""), (
            "a killed process still reads as alive"
        )
    finally:
        worker.kill()
        worker.communicate(timeout=60)


def test_a_lock_left_by_a_crashed_process_is_broken_at_once(tmp_path):
    """The fast path the code comment always promised: "the owner is definitively
    gone, pid reuse included". It never fired for the commonest crash there is,
    so the next run waited out the whole 1800 s staleness with the dead owner's
    pid sitting in the file."""
    lock_path = tmp_path / "account.lock"
    worker = _spawn(tmp_path, "crasher2.py", CRASHER, lock_path)
    try:
        assert _wait_for(lock_path.exists), "the child never took the lock"
        with pytest.raises(AccountBusy):
            AccountLock(lock_path, stale_after=86_400.0).acquire()   # it IS alive

        worker.kill()
        from resolve import _process_identity

        assert _wait_for(lambda: _process_identity(worker.pid) == "")

        taken = AccountLock(lock_path, stale_after=86_400.0)
        taken.acquire()                  # no wait: the owner is provably gone
        try:
            assert taken.owns_the_file() is True
            record = lock_path.with_name(lock_path.name + ".broken.jsonl")
            assert json.loads(record.read_text(encoding="utf-8").strip())[
                "previous"]["owner"] == "crasher"
        finally:
            taken.release()
    finally:
        worker.kill()
        worker.communicate(timeout=60)


def test_a_lock_owned_by_a_live_process_is_never_broken_early(tmp_path):
    """The other direction, which matters more: pid reuse must not be able to
    talk us into breaking a live lock."""
    lock_path = tmp_path / "account.lock"
    from resolve import _process_identity
    lock_path.write_text(json.dumps({
        "owner": "other-tool", "pid": os.getpid(), "since": "just now",
        "ts": time.time(), "host": socket.gethostname(),
        "pid_created": _process_identity(os.getpid()),
    }), encoding="utf-8")
    with pytest.raises(AccountBusy):
        AccountLock(lock_path, stale_after=86_400.0).acquire()


# ==========================================================================
# Regression guards.
# The new exception types are imported INSIDE the tests that need them: a
# module-level import of a name the old code does not have turns the whole file
# into a collection error, and then "how many fail against the code as it
# stood" cannot be measured at all.
# ==========================================================================
POISONED = {
    "frozen_until is NaN": '{"date": "%s", "frozen_until": NaN, '
                           '"frozen_reason": "FloodWait 36468 s"}',
    "frozen_until is Infinity": '{"date": "%s", "frozen_until": Infinity}',
    "frozen_until is -Infinity": '{"date": "%s", "frozen_until": -Infinity}',
    "the gap latch is NaN": '{"date": "%s", "last_resolve_ts": NaN, "resolves": 1}',
    "a burst timestamp is NaN": '{"date": "%s", "recent_resolve_ts": [NaN, NaN]}',
    "the monotonic twin is NaN": '{"date": "%s", "frozen_until_mono": NaN}',
    "a reservation timestamp is NaN":
        '{"date": "%s", "pending": [{"id": "a", "username": "b", "ts": NaN}]}',
}


@pytest.mark.parametrize("label", sorted(POISONED))
def test_a_non_finite_number_in_the_ledger_is_damage_not_permission(tmp_path, label):
    """Measured, verbatim, before the repair:

        check_resolve with frozen_until=NaN : ALLOWED (returned None)
        summary                             : ValueError: cannot convert float NaN to integer
        Infinity summary                    : OverflowError: cannot convert float infinity
        min-gap latch with last_resolve_ts=NaN : ALLOWED

    `json.loads` accepts the bare literals, `isinstance(NaN, float)` is True so
    the strict reader passed them, and every comparison against NaN is False --
    so a ledger holding a real ten-hour FloodWait read as "not frozen" while
    `tg.py budget`, the command a human runs to find out whether anything is
    safe, died with a bare `ValueError` that `except LedgerUnreadable` could not
    see.
    """
    path = tmp_path / "ledger.json"
    path.write_text(POISONED[label] % _today(), encoding="utf-8")
    ledger = ResolveLedger(path, min_gap=30.0)

    with pytest.raises(LedgerUnreadable):
        ledger.read()
    with pytest.raises(LedgerUnreadable):
        ledger.check_resolve(now=time.time())
    with pytest.raises(BudgetExhausted):          # ... and it IS a budget refusal
        ledger.check_resolve(now=time.time())

    summary = ledger.summary()                    # never a traceback
    assert summary["readable"] is False
    assert summary["frozen"] is True              # damaged reads as frozen
    assert "cannot be read" in summary["frozen_reason"]


def test_the_finite_check_is_the_shared_one_and_there_is_no_second_copy():
    """Nobody writes a second copy of this check: `_want_number` is a thin
    wrapper over `config.want_finite_number` -- so a repair to the shared reader
    reaches this module, and a divergence between two copies cannot happen."""
    import resolve as resolvemod

    calls: list = []
    real = configmod.want_finite_number

    def watched(data, key, default=0):
        calls.append(key)
        return real(data, key, default)

    configmod.want_finite_number = watched
    try:
        resolvemod._want_number({"resolves": 3}, "resolves")
    finally:
        configmod.want_finite_number = real
    assert calls == ["resolves"]

    source = inspect.getsource(resolvemod)
    assert "isfinite" not in source, "a second copy of the finite check"


# --------------------------------------------------------------------------
# The reservation token is honoured or it does not exist
# --------------------------------------------------------------------------
def test_the_settlement_signature_is_the_frozen_contract():
    """`account.py` threads the token through against this exact shape, so the
    signature is pinned here and neither half can drift."""
    check = inspect.signature(ResolveLedger.check_resolve)
    assert check.parameters["reserve"].kind is inspect.Parameter.KEYWORD_ONLY
    assert check.parameters["username"].kind is inspect.Parameter.KEYWORD_ONLY

    record = inspect.signature(ResolveLedger.record_resolve)
    assert record.parameters["token"].kind is inspect.Parameter.KEYWORD_ONLY
    assert record.parameters["token"].default is None
    assert record.parameters["now"].kind is inspect.Parameter.KEYWORD_ONLY

    settle = inspect.signature(ResolveLedger.settle_resolve)
    assert list(settle.parameters)[1] == "token"


def test_a_healthy_call_cannot_settle_a_dead_runs_reservation(tmp_path):
    """Measured before the repair:

        resolves: 2 | pending ids: ['<t_live>'] | settled the DEAD reservation: True

    Run A reserves and is killed mid-call -- the 2026-08-20 signature this whole
    mechanism exists to detect. Ten minutes later run B reserves and completes
    normally, and B's settlement removed A's reservation and left its own: the
    only durable trace that a run died mid-call, cleared by an unrelated healthy
    call, while `summary()` reported a pending resolve against a call that
    worked. The daily count stayed right, so nothing else could notice.
    """
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, min_gap=0.0, burst_ceiling=10 ** 9)
    now = 4_000_000.0
    dead = ledger.check_resolve(now=now - 600, reserve=True, username="victim")
    live = ledger.check_resolve(now=now, reserve=True, username="realchannel")

    ledger.record_resolve("realchannel", ok=True, token=live, now=now)

    ids = [p["id"] for p in ledger.read().pending]
    assert ids == [dead], "the dead run's reservation is the one that must remain"
    assert ledger.read().resolves == 2
    assert ledger.summary()["pending_resolves"] == 1


def test_settling_a_reservation_nobody_has_is_refused(tmp_path):
    """Settling without a matching token raises. Silence here means the
    caller and the ledger disagree about what has been spent, and a settlement
    that quietly matched nothing is how the wrong one got closed instead."""
    from resolve import ReservationUnknown

    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, min_gap=0.0)
    token = ledger.reserve_resolve("tdlibchat", now=1_000.0)

    with pytest.raises(ReservationUnknown) as exc:
        ledger.settle_resolve("not-a-token", now=1_001.0)
    assert "not-a-token" in str(exc.value)
    # ... and the real reservation is untouched by the failed attempt.
    assert [p["id"] for p in ledger.read().pending] == [token]

    ledger.settle_resolve(token, now=1_002.0)
    assert ledger.read().pending == []
    # Settling twice is the same disagreement, and is refused the same way.
    with pytest.raises(ReservationUnknown):
        ledger.settle_resolve(token, now=1_003.0)

    # `record_resolve` is the same door: a token that matches nothing raises.
    with pytest.raises(ReservationUnknown):
        ledger.record_resolve("tdlibchat", ok=True, token=token, now=1_004.0)


def test_a_record_without_a_token_counts_and_never_settles(tmp_path):
    """The safe side, stated: an unsettled reservation costs a re-read, a
    wrongly settled one loses the evidence silently. So a caller that reserved
    and then forgot its token OVER-counts rather than closing somebody's
    reservation by guessing at the username."""
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, min_gap=0.0, burst_ceiling=10 ** 9)
    token = ledger.reserve_resolve("tdlibchat", now=2_000.0)
    ledger.record_resolve("tdlibchat", ok=True, now=2_001.0)

    state = ledger.read()
    assert state.resolves == 2                       # over-counted, deliberately
    assert [p["id"] for p in state.pending] == [token]


def test_a_reservation_carries_the_name_it_was_taken_for(tmp_path):
    """`check_resolve(reserve=True)` called `reserve_resolve("")`, so every
    reservation the working path took was anonymous -- `summary()` could say a
    run had died mid-call but never on which name."""
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, min_gap=0.0)
    ledger.check_resolve(now=3_000.0, reserve=True, username="tdlibchat")
    assert [p["username"] for p in ledger.read().pending] == ["tdlibchat"]


def test_a_reservation_is_pruned_on_the_clock_the_caller_gave(tmp_path):
    """`_prune_pending` read `time.time()` while
    every other operation on the pending list takes an injected `now`. A test
    driving the clock by hand had its reservations pruned instantly and the
    resolve counted again, so it could assert the wrong arithmetic and pass."""
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, min_gap=0.0, burst_ceiling=10 ** 9)
    token = ledger.reserve_resolve("tdlibchat", now=5_000.0)

    # One second later on the caller's clock, the reservation is still open --
    # even though `time.time()` is decades past it.
    ledger.reserve_resolve("other", now=5_001.0)
    assert token in [p["id"] for p in ledger.read().pending]

    # Past the TTL on that same clock, it goes.
    ledger.reserve_resolve("third", now=5_001.0 + 2 * 3600)
    assert token not in [p["id"] for p in ledger.read().pending]


# --------------------------------------------------------------------------
# A freeze that can be lifted, and one that cannot run away
# --------------------------------------------------------------------------
def test_a_freeze_longer_than_this_machine_imposes_is_clamped(tmp_path):
    """Measured before the repair:

        led.freeze(10**9, "clock was wrong") -> {'frozen': True, 'frozen_for_sec': 999999999}
        ... and a clean write afterwards     -> 999999999

    `frozen_until` is `max()`-monotone and `_write_locked` refuses every write
    that would shorten it, so one bad value stopped all resolving in every
    process for thirty-one years. The defence against a freeze ending EARLY was
    thorough; there was none at all against one that never ends.
    """
    from resolve import MAX_FREEZE_SEC

    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path)
    ledger.freeze(10 ** 9, "the clock was wrong", now=6_000_000.0)
    state = ledger.read()
    assert state.frozen_until == 6_000_000.0 + MAX_FREEZE_SEC
    assert "clamped" in state.frozen_reason
    assert "unfreeze" in state.frozen_reason          # and it names the way out

    # A real ten-hour FloodWait is nowhere near the bound and is recorded exactly.
    other = ResolveLedger(tmp_path / "b.json")
    other.freeze(36468, "FloodWait on resolve of @tdlibchat", now=1_000.0)
    assert other.read().frozen_until == 1_000.0 + 36468


HOSTILE_WAITS = {
    "a word": "later",
    "nothing at all": None,
    "not a number": float("nan"),
    "no end": float("inf"),
    "a negative wait": -5,
    "a boolean": True,
}


@pytest.mark.parametrize("label", sorted(HOSTILE_WAITS))
def test_an_unreadable_wait_still_freezes_and_never_raises_bare(tmp_path, label):
    """A named refusal and the fail-closed direction together. `freeze("later")` left a bare
    `ValueError`, `freeze(None)` a bare `TypeError`, `freeze(inf)` a bare
    `OverflowError` -- and `freeze(nan)` and `freeze(-5)` were ACCEPTED and
    froze for nothing at all, which is the dangerous one: a FloodWait had
    arrived and the ledger said the account was free.
    """
    from resolve import UNREADABLE_FREEZE_SEC

    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path)
    ledger.freeze(HOSTILE_WAITS[label], "FloodWait on resolve of @tdlibchat",
                  now=7_000.0)
    state = ledger.read()
    assert state.frozen_until == 7_000.0 + UNREADABLE_FREEZE_SEC
    assert "@tdlibchat" in state.frozen_reason        # the reason still says why
    assert str(HOSTILE_WAITS[label]) in state.frozen_reason   # ... and what it was
    with pytest.raises(ResolveFrozen):
        ledger.check_resolve(now=7_001.0)


def test_a_freeze_can_be_lifted_deliberately_and_the_lift_is_recorded(tmp_path):
    """The second half of it. There was no way to do this at all: the only
    repair for a freeze written from a wrong clock was deleting the ledger by
    hand, which throws away the day's counts, the burst list and the pending
    reservations with it."""
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, min_gap=0.0, burst_ceiling=10 ** 9)
    ledger.record_resolve("earlier", ok=True, now=8_000.0)
    ledger.freeze(36468, "FloodWait on resolve of @tdlibchat", now=8_001.0)
    with pytest.raises(ResolveFrozen):
        ledger.check_resolve(now=8_002.0)

    lifted = ledger.clear_freeze("the VM clock was a year fast", now=8_003.0)
    assert lifted["was_frozen"] is True
    assert lifted["cleared"]["frozen_for_sec"] > 36000
    assert "@tdlibchat" in lifted["cleared"]["frozen_reason"]
    assert lifted["reason"] == "the VM clock was a year fast"

    ledger.check_resolve(now=8_004.0)                 # must not raise
    assert ledger.summary()["frozen"] is False
    assert ledger.read().resolves == 1                # the day's count survives

    # ... and it is written down, because nothing else can tell afterwards
    # whether the freeze was Telegram's.
    record = path.with_name(path.name + ".freezes.jsonl")
    line = json.loads(record.read_text(encoding="utf-8").strip())
    assert line["reason"] == "the VM clock was a year fast"
    assert line["cleared"]["frozen_until"] == 8_001.0 + 36468

    # Lifting when there is nothing to lift is not an error, and says so.
    again = ledger.clear_freeze("nothing to do", now=8_005.0)
    assert again["was_frozen"] is False


def test_clearing_a_freeze_is_the_only_thing_that_may_shorten_one(tmp_path):
    """The invariant `_write_locked` exists for is unchanged for everybody else:
    a `record_resolve` that read before a `freeze` landed still cannot write
    `frozen_until = 0.0` back over it."""
    path = tmp_path / "ledger.json"
    a = ResolveLedger(path)
    b = ResolveLedger(path)
    stale = a.read()
    b.freeze(36468, "FloodWait on resolve of @tdlibchat")
    stale.resolves += 1
    a.write(stale)
    assert a.read().frozen_until > 0
    a.clear_freeze("measured: the freeze was ours, not Telegram's")
    assert a.read().frozen_until == 0.0


# --------------------------------------------------------------------------
# Hostile arguments leave by a declared door
# --------------------------------------------------------------------------
HOSTILE_MOMENTS = {"a word": "soon", "nothing": float("nan"), "no end": float("inf"),
                   "a list": [1, 2]}


@pytest.mark.parametrize("label", sorted(HOSTILE_MOMENTS))
def test_a_moment_nobody_can_read_is_a_refusal_not_a_traceback(tmp_path, label):
    """`check_resolve(now="soon")` reached `state.frozen_until - now` and left a
    bare `TypeError` out of the one function whose whole job is to answer "is
    this safe". The safe answer to an unreadable question is no."""
    path = tmp_path / "ledger.json"
    ledger = ResolveLedger(path, min_gap=0.0)
    when = HOSTILE_MOMENTS[label]
    with pytest.raises(BudgetExhausted):
        ledger.check_resolve(now=when)
    with pytest.raises(BudgetExhausted):
        ledger.check_resolve(now=when, reserve=True)
    with pytest.raises(BudgetExhausted):
        ledger.record_resolve("tdlibchat", ok=True, now=when)
    with pytest.raises(BudgetExhausted):
        ledger.check_state(LedgerState(date=_today()), now=when)
    assert not path.exists(), "a refused check must not write the ledger"


def test_a_state_that_is_not_one_is_refused_by_name(tmp_path):
    """The same, on the other public doors that take an object."""
    ledger = ResolveLedger(tmp_path / "ledger.json")
    with pytest.raises(BudgetExhausted):
        ledger.check_state({"resolves": 0}, now=1_000.0)
    with pytest.raises(BudgetExhausted):
        ledger.plan_resolve({"resolves": 0}, now=1_000.0)
    with pytest.raises(LedgerWriteFailed):
        ledger.write({"resolves": 0})
    # And the two readers that take data from a registry record.
    assert peer_is_usable([1, 2], "fp") is False
    assert peer_is_usable("not a peer", "fp") is False
    assert session_fingerprint(1234) == ""


DAMAGED_RESERVATIONS = {
    "an id that is not text": '{"date": "%s", "pending": [{"id": 5, "ts": 1.0}]}',
    "a name that is not text":
        '{"date": "%s", "pending": [{"id": "a", "username": 5, "ts": 1.0}]}',
    "a timestamp that is a word":
        '{"date": "%s", "pending": [{"id": "a", "ts": "then"}]}',
}


@pytest.mark.parametrize("label", sorted(DAMAGED_RESERVATIONS))
def test_a_damaged_reservation_list_refuses(tmp_path, label):
    """`_prune_pending` computes `now - float(p["ts"])` on every mutation, so a
    hand edit in there left a bare `ValueError` out of `record_resolve`,
    `freeze` and `record_join` alike."""
    path = tmp_path / "ledger.json"
    path.write_text(DAMAGED_RESERVATIONS[label] % _today(), encoding="utf-8")
    ledger = ResolveLedger(path, min_gap=0.0)
    with pytest.raises(LedgerUnreadable):
        ledger.read()
    with pytest.raises(LedgerUnreadable):
        ledger.record_resolve("tdlibchat", ok=True)
    assert ledger.summary()["readable"] is False


# --------------------------------------------------------------------------
# touch() must not resurrect a lock that was legitimately broken
# --------------------------------------------------------------------------
def test_touch_cannot_resurrect_a_lock_that_was_legitimately_broken(tmp_path):
    """Filed as PLAUSIBLE ("the window is the sub-millisecond gap between
    `self._read()` and the replace"), and CONFIRMED: forcing the interleaving
    at exactly that statement boundary, against the file restored from git,
    produced

        B broke the stale lock and took it : True
        A._held / B._held                  : True True
        the file on disk says owner        : A
        >>> TWO WRITERS both hold the account: True

    Process A is suspended past `stale_after` (a laptop sleeping mid-run), B
    takes the `.break` guard and legitimately takes the lock, A wakes and its
    `os.replace` puts its own content back on top. Both then believe they own
    the account, which is the failure the class exists to prevent.

    The interleaving is FORCED rather than waited for: B's break runs from inside
    A's own `_read`, so the race is exercised on every machine and every run.
    """
    import resolve as resolvemod

    path = tmp_path / "account.lock"
    a = AccountLock(path, stale_after=100.0, owner="A")
    a.acquire()
    b = AccountLock(path, stale_after=100.0, owner="B")
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        info["ts"] = time.time() - 10_000
        path.write_text(json.dumps(info), encoding="utf-8")
        old = time.time() - 10_000
        os.utime(path, (old, old))

        seen = {"reads": 0, "b_took_it": False}
        real_read = resolvemod.AccountLock._read

        def racing_read(self):
            out = real_read(self)
            if self is a:
                seen["reads"] += 1
                # After the ownership check has read the file, before the replace.
                if seen["reads"] == 2 and not seen["b_took_it"]:
                    try:
                        b.acquire()
                        seen["b_took_it"] = True
                    except AccountBusy:
                        pass
            return out

        resolvemod.AccountLock._read = racing_read
        try:
            a.touch()
        finally:
            resolvemod.AccountLock._read = real_read

        # Whoever ended up with the file, there is exactly one of them, and the
        # file on disk agrees with that one.
        owner = json.loads(path.read_text(encoding="utf-8")).get("owner")
        assert not (a.owns_the_file() and b.owns_the_file())
        assert not (seen["b_took_it"] and owner == "A"), (
            "A's refresh landed on top of a lock B had legitimately taken")
    finally:
        b.release()
        a.release()
