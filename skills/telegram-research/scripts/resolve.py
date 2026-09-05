"""Account safety: the resolve ledger, the cross-process lock, the peer cache.

Nothing here talks to Telegram. It is the accounting that decides whether a call
is allowed to happen at all, kept separate from the transport so that it can be
tested exhaustively without an account -- which is the only way it will ever be
tested, because testing it against the real account is the thing it exists to
prevent.

The incident this module is written against, measured on a real account on
2026-08-20: sixteen `contacts.resolveUsername` calls in under seven minutes
bought `A wait of 36468 seconds` -- ten hours and seven minutes. It was not a
ban; reading and searching kept working, only resolution froze. Worse than the
freeze: all sixteen calls returned success and wrote empty records, so the tool
reported a good run while the account was already dead.

Three rules follow, and they are structural rather than advisory:

1. **The count lives on disk.** A per-process counter cannot see the second
   caller, and there is always eventually a second caller.
2. **The first FloodWait stops resolving for everything.** Each further attempt
   extends the ban, so retrying is not merely useless, it is the harm.
3. **A cached `access_hash` is worthless without the fingerprint of the login
   session that produced it.** Telegram documents this in `core.telegram.org/api/peers`:
   "Access hashes may not be reused across different accounts or different
   login/auth sessions of the same account". A hash whose fingerprint does not
   match the current session is discarded, never tried.

A fourth rule was added after the 2026-08-24 review, and it governs every branch
below:

4. **Fail closed.** A ledger or a lock that cannot be read and understood means
   *stop*, never *proceed*. Before this, a truncated, empty or hand-edited
   ledger read back as "0 resolves today, not frozen" -- the safest-looking
   answer there is and the most dangerous one, because it was believed.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path

import config as configmod

# The ceiling. 180, not 200, and the difference is deliberate.
#
# Telegram's own page for `contacts.resolveUsername` documents no quota at all --
# a grep of it for limit|quota|flood|daily|rate returns nothing. The number 200
# has exactly one published source: a TDLib maintainer on Telegram's issue
# tracker, 2021-03-15, "The limit for username resolving is 200 usernames daily".
# That is five years old and nobody has restated it since. The one measurement in
# our own records that reached a wall did so at 16 calls in 7 minutes -- which
# says the burst matters at least as much as the daily total.
#
# So: 180 as the daily ceiling, because 200 is an unrepeated five-year-old claim
# and there is no reason to stand on its edge; plus a burst rule, because the
# only failure ever observed here was a burst.
DAILY_RESOLVE_CEILING = 180
BURST_WINDOW_SEC = 600          # ten minutes
BURST_CEILING = 8               # half of the sixteen that froze the account
MIN_RESOLVE_GAP_SEC = 30.0      # never a pack of resolves, whatever the totals say
DAILY_JOIN_CEILING = 3          # joining is its own operation with its own budget

# How long an unsettled reservation stays on the books. It has already been
# counted against the day, so keeping it costs nothing but a line in `summary()`;
# dropping it too early would hide the fact that a run died mid-call.
PENDING_TTL_SEC = 3600.0

# The longest freeze this module will ever impose, and what it does with a
# number it cannot use.
#
# `freeze(seconds, ...)` took any float at all, `frozen_until` is `max()`-monotone
# and `_write_locked` refuses every write that would shorten it -- so ONE bad
# value stopped all resolving, in every process, for as long as it said. The
# module's own docstring names the ways a clock goes wrong (an NTP correction, a
# restored VM snapshot, a second writer with a wrong clock); a FloodWait landing
# inside that window wrote a deadline years in the future that the correction
# could not undo, and the only repair was deleting the ledger by hand, which
# also threw away the day's counts and the pending list.
#
# The largest wait ever measured on this account is 36468 s. Two days is far
# above anything Telegram has been seen to ask for and far below "for ever", so
# a longer one is a clock artefact rather than a ban and is clamped to it. A
# value that is not a number at all does NOT cancel the freeze -- refusing to
# record a real ban is the fail-open direction -- it freezes for an hour and
# says so in the reason, where `tg.py budget` prints it.
MAX_FREEZE_SEC = 2 * 86400.0
UNREADABLE_FREEZE_SEC = 3600.0


def freeze_seconds(seconds) -> tuple[float, str]:
    """How long to freeze for, and a note when that is not what was asked.

    Never raises and never returns zero for a value it could not read: a
    `freeze` call happens because Telegram has already said no, so refusing to
    record it is the fail-open direction. See `MAX_FREEZE_SEC`.

    Module level rather than a method because there are two freezes in this
    skill and they had two different rules: `account.HistoryLog.freeze` applied
    no ceiling at all (a billion seconds went to disk verbatim -- 31 years) and
    raised on a value it could not read, writing nothing, which is the fail-open
    direction on the one call the CLI can really earn a freeze with. Both call
    this now, so neither can drift from the other again.
    """
    try:
        value = float(configmod.want_finite_number({"seconds": seconds}, "seconds"))
    except ValueError:
        return UNREADABLE_FREEZE_SEC, (
            f"the wait was given as {seconds!r}, which is not a number of "
            f"seconds; frozen for {UNREADABLE_FREEZE_SEC:.0f} s instead"
        )
    if value < 0:
        return UNREADABLE_FREEZE_SEC, (
            f"the wait was given as {value:.0f} s, which is not a wait; frozen "
            f"for {UNREADABLE_FREEZE_SEC:.0f} s instead"
        )
    if value > MAX_FREEZE_SEC:
        return MAX_FREEZE_SEC, (
            f"the wait was given as {value:.0f} s, longer than this machine "
            f"will freeze for; clamped to {MAX_FREEZE_SEC:.0f} s. A wait that "
            "long is a clock, not a ban — `tg.py budget --unfreeze` lifts it"
        )
    return value, ""


class BudgetExhausted(RuntimeError):
    """The call is refused by our own accounting, before Telegram sees it."""


class LedgerUnreadable(BudgetExhausted):
    """The ledger exists but cannot be understood, so nothing may be spent.

    A subclass of `BudgetExhausted` on purpose: every caller that already treats
    a budget refusal as "do not make the call" treats a damaged ledger the same
    way, without knowing this class exists. There is no path on which a
    corrupted safety file reads as permission.
    """


class LedgerWriteFailed(RuntimeError):
    """The accounting could not be recorded. The caller must not proceed as if it was."""


class ResolveFrozen(RuntimeError):
    """Telegram has already said no. Nothing resolves until the wait expires."""


class AccountBusy(RuntimeError):
    """Another process holds the account. There is exactly one writer."""


class ReservationUnknown(RuntimeError):
    """A settlement named a reservation that is not on the books.

    A token settles THAT reservation or nothing. The old code fell back to
    "the oldest reservation with no name on it", so a healthy run settled the
    reservation left by a run that had DIED mid-call -- deleting the only
    durable trace of the failure this whole module exists to detect, while
    leaving its own on the books for `summary()` to report instead.
    """


# The ledger's day boundary is the operator's own, not a compiled-in one:
# `config.local_tz()` reads the machine's zone and `TELEGRAM_RESEARCH_TZ` pins it.
# It decides when the daily resolve ceiling resets and when a freeze recorded
# "yesterday" stops counting, so a zone belonging to somebody else moves both.
def _today() -> str:
    return datetime.now(configmod.local_tz()).date().isoformat()


def _now_iso() -> str:
    return datetime.now(configmod.local_tz()).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Session fingerprint
# --------------------------------------------------------------------------
def session_fingerprint(session_string: str) -> str:
    """A stable, non-reversible id for a login session.

    The session string is a credential and must never reach a log, a report or a
    registry line. What the registry needs is only the ability to answer 'is this
    the same login session that produced the hash', and a truncated SHA-256
    answers that without carrying the secret anywhere.
    """
    if not isinstance(session_string, str) or not session_string:
        # A named refusal, and fail closed with it: an empty fingerprint
        # means every cached peer is discarded, which costs requests.
        # `AttributeError` from `.encode` costs the run.
        return ""
    return hashlib.sha256(session_string.encode("utf-8")).hexdigest()[:16]


def peer_is_usable(peer: dict | None, fingerprint: str) -> bool:
    """A cached peer may be used only if all three parts are present and current.

    Missing fingerprint means the record predates this rule and cannot be
    trusted: per Telegram's documentation an access hash minted by one login
    dies at the next re-login, and a record that cannot say which login minted
    it will fail without anything noticing.
    """
    if not isinstance(peer, dict) or not peer or not fingerprint:
        return False
    if not peer.get("id") or not peer.get("access_hash"):
        return False
    return peer.get("auth_session_fingerprint") == fingerprint


# --------------------------------------------------------------------------
# Process identity -- used only to shorten a wait, never to lengthen one
# --------------------------------------------------------------------------
def _win_has_terminated(k32, handle) -> bool:
    """Has this process object terminated? Asked the one way that is defined.

    A process object is signaled **when and only when the process terminates**
    (`WaitForSingleObject`, MSDN), so `WAIT_OBJECT_0` at a zero timeout is an
    exact answer and every other result is "no opinion". Anything else this API
    offers is worse:

    * `GetProcessTimes`' `lpExitTime` is documented as *undefined* while the
      process is running. It is zero in practice, and betting a live account
      lock on "in practice" is how two writers end up on one identity -- a
      non-zero value read out of a live process would make us break its lock.
    * `GetExitCodeProcess` answers `STILL_ACTIVE` (259), which a process is also
      free to exit WITH, so it cannot distinguish the two cases either.

    This one has no undefined case. It costs the SYNCHRONIZE access right, and
    when we cannot get that we simply do not shorten the wait.
    """
    import ctypes
    import ctypes.wintypes as wt

    WAIT_OBJECT_0 = 0x0
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.WaitForSingleObject.restype = wt.DWORD
    return k32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0


def _process_identity(pid: int) -> str | None | bool:
    """`""` if the process is definitely gone, a start-time token if it is
    running, `None` if this machine will not say.

    A pid on its own is not evidence: pids are recycled, and breaking a live
    lock because a recycled pid looked familiar is the two-writers failure this
    whole module exists to prevent. The start time makes the answer exact.

    A dead process is not always an absent one, which is the case this used to
    miss. On Windows a terminated process whose handle is still held open -- by
    its parent: a shell, a task runner, `subprocess.Popen` that has not been
    waited on -- keeps its pid reserved and answers `GetProcessTimes` with the
    creation time it always had. The identity therefore MATCHED what the lock
    recorded, `_is_stale` concluded "alive", and the crashed run's lock was
    respected for the full 1800 s even though the pid was right there in the
    file. Linux has the same shape: an unreaped child is a zombie whose
    `/proc/<pid>/stat` still exists, `starttime` and all.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            ERROR_INVALID_PARAMETER = 87
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
            k32.OpenProcess.restype = wt.HANDLE
            # SYNCHRONIZE is what makes the termination question askable. It is
            # requested first and dropped if it is refused, so a process we can
            # only query still gets exactly the old answer rather than none.
            can_wait = True
            handle = k32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
            if not handle:
                if k32.GetLastError() == ERROR_INVALID_PARAMETER:
                    return ""            # no such process, and Windows is sure
                can_wait = False
                handle = k32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                if k32.GetLastError() == ERROR_INVALID_PARAMETER:
                    return ""
                return None              # access denied: no opinion
            try:
                if can_wait and _win_has_terminated(k32, handle):
                    # It has exited; something is merely holding its handle open.
                    return ""
                creation = wt.FILETIME()
                exit_t, kernel_t, user_t = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
                ok = k32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exit_t),
                    ctypes.byref(kernel_t), ctypes.byref(user_t),
                )
                if not ok:
                    return None
                return f"win:{creation.dwHighDateTime}:{creation.dwLowDateTime}"
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return None
    # POSIX: /proc gives the start time in clock ticks since boot.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        if fields and fields[0] == b"Z":
            return ""                    # a zombie has exited; nobody reaped it
        return f"posix:{fields[19].decode()}"
    except FileNotFoundError:
        # A missing `/proc/<pid>/stat` means the process is gone -- but only on
        # a machine that HAS `/proc`. macOS has none, so every pid on it read as
        # "definitely dead", `_is_stale` broke the lock of a run that was still
        # working, and two writers went at one account. The opposite reading is
        # no better: with no opinion at all a crashed run holds the account for
        # the full 1800 s. `None` is what this function has for "this machine
        # will not say", and that is the honest answer here.
        if not os.path.isdir("/proc"):
            return None
        return ""
    except Exception:
        return None


# --------------------------------------------------------------------------
# Strict field readers. A missing key is a default; a key that is there with the
# wrong value is a damaged file. Nothing below turns a `null` into a zero.
# --------------------------------------------------------------------------
def _want_number(data: dict, key: str):
    """A thin wrapper over the one shared check: nobody writes a second copy.

    What this file's own version was missing: `json.loads` accepts the bare
    literals `NaN`, `Infinity` and `-Infinity`, and both pass
    `isinstance(x, float)`. NaN then makes EVERY comparison false, so
    `"frozen_until": NaN` passed the strict reader, `frozen_for()` returned NaN,
    `left > 0` was False and `check_resolve()` PERMITTED resolving while a real
    ten-hour FloodWait was on the books -- the fail-open direction that rule 4
    exists to close. `summary()`, the command a human runs to find out whether
    anything is safe, died on the same value with a bare
    `ValueError: cannot convert float NaN to integer`; `Infinity` gave a bare
    `OverflowError` that `except LedgerUnreadable` could not see either; and
    `"last_resolve_ts": NaN` made the minimum-gap latch pass unconditionally.
    One finite check closes all four, and it lives in `config`.
    """
    return configmod.want_finite_number(data, key, 0)


def _want_int(data: dict, key: str) -> int:
    value = _want_number(data, key)
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{key!r} is {value!r}, which is not a whole number")
    return int(value)


def _want_float(data: dict, key: str) -> float:
    return float(_want_number(data, key))


# Two readings of `time.time() - time.monotonic()` taken microseconds apart
# still differ by the time between the two calls, so the estimate needs a little
# slack before two of them may be called the same boot.
BOOT_SLACK_SEC = 5.0


def same_boot(boot_at_freeze: float, mono_at_freeze: float, mono_now: float) -> bool:
    """Is `time.monotonic()` still counting from the boot that took the freeze?

    A monotonic deadline is only comparable within one boot, and the test used
    to be `mono_now >= mono_at_freeze` -- which is true again after ANY reboot,
    as soon as the new uptime passes the old one. Measured: a wall deadline
    three days expired, a freeze still held, and (before `clear_freeze`) no way
    out of it at all.

    So the freeze records `time.time() - time.monotonic()`, an estimate of the
    moment the machine booted, and this compares it with the same estimate
    taken now. Equal, near enough, means the same boot.

    **Both readings come from THIS machine's clock, never from a caller's
    `now`.** The estimate was written from the real clock, so only the real
    clock may be compared with it -- and the case the monotonic twin exists for
    is exactly a caller reasoning about a wall-clock moment the machine itself
    does not agree with.

    Unequal is where the care is, because the monotonic twin exists for the case
    that ALSO moves the estimate: a wall clock jumping forward moves it by the
    size of the jump, and an eleven-hour NTP correction ending a ten-hour wait
    Telegram was still enforcing is the incident that put the twin here. The two
    are told apart by how far the estimate moved:

    * backwards -- the clock was set back. The wall deadline is then the longer
      of the two anyway, so the monotonic one is not needed and not trusted.
    * forward by LESS than the uptime the freeze was taken at -- a reboot cannot
      do that: it moves the estimate by the old uptime plus the downtime, both
      of which it must pay in full. So this is a clock jump on the same boot,
      and the monotonic deadline stands.
    * forward by at least that much -- a reboot is possible, and a monotonic
      deadline that may belong to a previous boot is not evidence of anything.
    """
    if not boot_at_freeze:
        # Written by a version that did not record it, or hand-edited away.
        # There is no evidence of the boot, so there is nothing to trust.
        return False
    shift = (time.time() - mono_now) - boot_at_freeze
    if abs(shift) <= BOOT_SLACK_SEC:
        return True
    if shift < 0:
        return False
    return shift < max(float(mono_at_freeze), 0.0)


def _want_now(now):
    """The caller's `now`, checked: no bare `TypeError` out of a check.

    `check_resolve(now="soon")` reached `state.frozen_until - now` and left
    `TypeError` out of the one function whose whole job is to answer "is this
    safe". A moment nobody can read is not a moment, and the safe answer to an
    unreadable question is no.
    """
    if now is None:
        return time.time()
    try:
        return float(configmod.want_finite_number({"now": now}, "now"))
    except ValueError as exc:
        raise BudgetExhausted(
            f"the moment given to the accounting is not one: {exc}. Refused "
            "locally — Telegram has not been asked."
        ) from None


def _want_str(data: dict, key: str) -> str:
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key!r} is {value!r}, which is not text")
    return value


def _want_floats(data: dict, key: str) -> list:
    value = data.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key!r} is {value!r}, which is not a list")
    out = []
    for item in value:
        # Same shared check, one item at a time: a NaN in `recent_resolve_ts`
        # satisfies `now - t < burst_window` never, so a poisoned burst list
        # reads as an empty one.
        try:
            number = configmod.want_finite_number({key: item}, key)
        except ValueError:
            raise ValueError(f"{key!r} holds {item!r}, which is not a timestamp") from None
        out.append(float(number))
    return out


def _want_dicts(data: dict, key: str) -> list:
    value = data.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{key!r} is {value!r}, which is not a list of records")
    return [dict(item) for item in value]


def _want_reservations(data: dict, key: str) -> list:
    """The pending list, with every timestamp readable.

    `_prune_pending` computes `now - float(p["ts"])` on these, so a hand edit or
    a NaN in one of them left a bare `ValueError`/`TypeError` out of every
    mutation -- a public entry point raising something nothing declares.
    """
    out = _want_dicts(data, key)
    for item in out:
        item["ts"] = float(configmod.want_finite_number(item, "ts", 0.0))
        if not isinstance(item.get("id", ""), str):
            raise ValueError(f"a reservation id is {item.get('id')!r}, which is not text")
        if not isinstance(item.get("username", ""), str):
            raise ValueError(
                f"a reservation username is {item.get('username')!r}, which is not text")
    return out


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------
@dataclass
class LedgerState:
    date: str = ""
    resolves: int = 0
    joins: int = 0
    last_resolve_ts: float = 0.0
    recent_resolve_ts: list = None            # timestamps inside the burst window
    frozen_until: float = 0.0                 # unix time; 0 means not frozen
    frozen_reason: str = ""
    fingerprint: str = ""
    # A freeze deadline expressed in wall-clock time alone ends early the moment
    # the clock jumps forward -- an NTP correction, a restored VM snapshot, a
    # second writer with a wrong clock. `time.monotonic()` is anchored to the
    # boot on both platforms we run on and is comparable between processes on the
    # same machine, so the freeze carries a second deadline that no clock change
    # can move. Whichever deadline is later wins.
    frozen_until_mono: float = 0.0
    mono_at_freeze: float = 0.0
    # `time.time() - time.monotonic()` at the moment of the freeze: an estimate
    # of when this machine booted. `same_boot` compares it with the same
    # estimate taken later, because a monotonic deadline from another boot is
    # not a deadline at all.
    boot_at_freeze: float = 0.0
    # Resolves that have been counted but whose outcome is not yet settled. The
    # rule in `references/account.md` is that a resolve is counted BEFORE it
    # happens; this list is what makes that true across a kill mid-call.
    pending: list = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["recent_resolve_ts"] = list(self.recent_resolve_ts or [])
        d["pending"] = list(self.pending or [])
        return d


class ResolveLedger:
    """Durable accounting for the one call that has ever cost real downtime.

    Every mutation is a read-modify-write of a small JSON file, and the whole
    read-modify-write runs inside a cross-process guard (`config.FileGuard`).
    That guard is not the same thing as `AccountLock`: the lock is the policy
    ("one writer to the account"), this is the mutex ("one writer to this file").
    They are separate because the second writer this module exists for -- any
    other tool signed into the same account -- does not take our account lock,
    and because the review measured what happens
    without the mutex: two processes, 300 real resolves, a ledger that counted
    124 of them, and a `freeze` erased by a `record_resolve` that had started
    before it.

    Reading is unguarded and stays that way, so `tg.py budget` can look at the
    accounting without taking anything.
    """

    def __init__(self, path: Path, fingerprint: str = "",
                 daily_ceiling: int = DAILY_RESOLVE_CEILING,
                 burst_ceiling: int = BURST_CEILING,
                 burst_window: int = BURST_WINDOW_SEC,
                 min_gap: float = MIN_RESOLVE_GAP_SEC,
                 join_ceiling: int = DAILY_JOIN_CEILING,
                 guard_timeout: float | None = None):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # `HistoryLog` already wraps its copy of this line and this one was
            # left bare, so a `TELEGRAM_RESEARCH_STATE` pointing at a drive that
            # is not there answered with a raw `FileNotFoundError` out of a
            # module that promises only its own types -- from a CONSTRUCTOR,
            # before any caller had a chance to ask it anything.
            raise LedgerWriteFailed(
                f"the state directory {self.path.parent} could not be created: "
                f"{exc}. Nothing was read and nothing was recorded."
            ) from None
        self.daily_ceiling = daily_ceiling
        self.burst_ceiling = burst_ceiling
        self.burst_window = burst_window
        self.min_gap = min_gap
        self.join_ceiling = join_ceiling
        self.guard_timeout = guard_timeout
        self._fingerprint = ""
        self.fingerprint = fingerprint

    def _timeout(self) -> float:
        """The guard timeout, read at call time.

        A default argument is bound at import while its partner
        `GUARD_STALE_AFTER` is read at call time, so anything that moved both
        would have moved only one and silently inverted `timeout > stale_after`
        -- the invariant that lets a waiter outlive a dead writer's guard.
        """
        return (configmod.GUARD_TIMEOUT if self.guard_timeout is None
                else self.guard_timeout)

    # -- the login session this ledger belongs to --------------------------
    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @fingerprint.setter
    def fingerprint(self, value: str) -> None:
        """Setting it persists it, which is the whole point of recording it.

        Before this, `account.AccountSession` assigned the fingerprint here and
        it only ever reached disk if the run happened to mutate the ledger
        afterwards. A dry run takes its fingerprint FROM the ledger, so an empty
        field means every cached peer is discarded and every source is planned as
        a resolve -- the plan overstates the budget a human uses to decide
        whether the run is safe.
        """
        value = value or ""
        if not value:
            self._fingerprint = value
            return
        try:
            # Read with the field blanked, or `read()` adopts the new value into
            # the state it hands back and the comparison can never differ.
            self._fingerprint = ""
            on_disk = self.read().fingerprint
            self._fingerprint = value
            if on_disk != value:
                self._mutate(lambda state: setattr(state, "fingerprint", value))
        except (LedgerUnreadable, LedgerWriteFailed, OSError):
            # A damaged or busy ledger is not repaired from a property setter.
            # The next real mutation will refuse loudly, which is the right place.
            self._fingerprint = value

    # -- guard -------------------------------------------------------------
    def _guard(self) -> configmod.FileGuard:
        return configmod.FileGuard(
            self.path.with_name(self.path.name + ".rmw"),
            timeout=self._timeout(), stale_after=configmod.GUARD_STALE_AFTER,
            label="ledger",
        )

    # -- state -------------------------------------------------------------
    def read(self) -> LedgerState:
        """The ledger as it stands, or a refusal. Never a silent zero.

        The distinction that did not exist before: *no ledger yet* is a clean
        slate, *a ledger that will not parse* is a stop sign. A truncated file, a
        zero-byte file, a hand edit with `"resolves": "many"` in it -- every one
        of those used to reset the day's count, the burst list, the minimum-gap
        latch and the freeze all at once, and report a clean slate.
        """
        raw = self._read_raw()
        if raw is None:
            state = LedgerState(recent_resolve_ts=[], pending=[])
        else:
            state = self._parse(raw)
        self._roll_day(state)
        if self._fingerprint and state.fingerprint != self._fingerprint:
            # The login session changed. Cached hashes elsewhere are now void;
            # this is recorded so a later run can see when it happened.
            state.fingerprint = self._fingerprint
        return state

    def _read_raw(self) -> str | None:
        """The file's text, `None` if it has never been written, or a refusal."""
        try:
            data = configmod.read_bytes_shared(self.path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LedgerUnreadable(self._damaged(f"it could not be read: {exc}")) from None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LedgerUnreadable(self._damaged(f"it is not UTF-8: {exc}")) from None
        if not text.strip():
            raise LedgerUnreadable(self._damaged(
                "it is empty. An empty safety file is not an empty account; a "
                "write was interrupted, or something truncated it."
            ))
        return text

    def _parse(self, text: str) -> LedgerState:
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError(f"the ledger holds {type(data).__name__}, not an object")
            state = LedgerState(
                date=_want_str(data, "date"),
                resolves=_want_int(data, "resolves"),
                joins=_want_int(data, "joins"),
                last_resolve_ts=_want_float(data, "last_resolve_ts"),
                recent_resolve_ts=_want_floats(data, "recent_resolve_ts"),
                frozen_until=_want_float(data, "frozen_until"),
                frozen_reason=_want_str(data, "frozen_reason"),
                fingerprint=_want_str(data, "fingerprint"),
                frozen_until_mono=_want_float(data, "frozen_until_mono"),
                mono_at_freeze=_want_float(data, "mono_at_freeze"),
                boot_at_freeze=_want_float(data, "boot_at_freeze"),
                pending=_want_reservations(data, "pending"),
            )
        except (ValueError, TypeError) as exc:
            # The coercions used to sit OUTSIDE the guarded parse, so a hand
            # edit escaped as a bare `ValueError: invalid literal for int()`
            # with no instruction attached to it. And nothing here coerces a
            # `null` into a zero: `"frozen_until": null` reading back as "not
            # frozen" is the fail-OPEN direction, which is the whole defect.
            raise LedgerUnreadable(self._damaged(f"it does not parse: {exc}")) from None
        if state.resolves < 0 or state.joins < 0:
            raise LedgerUnreadable(self._damaged("it holds a negative count"))
        return state

    def _damaged(self, why: str) -> str:
        return (
            f"the resolve ledger at {self.path} cannot be read: {why}\n"
            "Nothing is resolved while the accounting is unreadable — a damaged "
            "ledger reads as 'no resolves spent today, not frozen', which is the "
            "one answer that must never be guessed. Repair the file or move it "
            "aside deliberately, then run `tg.py budget` to confirm what it says."
        )

    def _roll_day(self, state: LedgerState) -> None:
        """Reset the daily counters on a rollover -- and only on a rollover.

        `state.date != _today()` was an inequality, not a rollover test. A ledger
        stamped with TOMORROW's date -- a clock minutes ahead across local
        midnight, an NTP correction, a second writer whose clock is off -- had
        its counters zeroed the moment the clock came back, handing back a
        spent day's worth of budget. Dates are ISO, so `<` is the rollover.
        """
        today = _today()
        if not state.date:
            state.date = today
        elif state.date < today:
            state.date = today
            state.resolves = 0
            state.joins = 0
        # state.date > today: a clock somewhere is ahead of ours. Keep the count
        # and keep the future stamp; the counters reset when the real day passes
        # it. Fail closed.

    def write(self, state: LedgerState) -> None:
        if not isinstance(state, LedgerState):
            raise LedgerWriteFailed(
                f"a ledger state must be a LedgerState, not {type(state).__name__}. "
                "Nothing was recorded, so nothing may be spent."
            )
        guard = self._guard_or_refuse()
        try:
            self._write_locked(state)
        finally:
            guard.release()

    def _guard_or_refuse(self):
        guard = self._guard()
        try:
            guard.acquire()
        except configmod.GuardBusy as exc:
            raise LedgerWriteFailed(
                f"{exc} Nothing was recorded, so nothing may be spent."
            ) from None
        return guard

    def _write_locked(self, state: LedgerState, *, may_shorten: bool = False) -> None:
        """Serialise the state. Called with the guard held.

        One invariant is enforced here rather than trusted to callers: a write
        may never shorten a freeze. `record_resolve` and `freeze` are both
        read-modify-write over the whole state, and a `record_resolve` that had
        read before a `freeze` landed used to write `frozen_until = 0.0` back
        over it -- measured, with Telegram's 36468 s wait still running.

        `may_shorten=True` is the ONE deliberate exception and belongs to
        `clear_freeze` alone: a freeze written from a wrong clock could
        otherwise be lifted only by deleting the ledger, which throws away the
        day's counts and the pending list with it.
        """
        try:
            disk = self.read()
        except LedgerUnreadable:
            disk = None
        if may_shorten:
            disk = None
        if disk is not None:
            if disk.frozen_until > state.frozen_until:
                state.frozen_until = disk.frozen_until
                state.frozen_reason = state.frozen_reason or disk.frozen_reason
            if disk.frozen_until_mono > state.frozen_until_mono:
                state.frozen_until_mono = disk.frozen_until_mono
                state.mono_at_freeze = disk.mono_at_freeze
                state.boot_at_freeze = disk.boot_at_freeze
        try:
            configmod.atomic_write_text(
                self.path, json.dumps(state.as_dict(), indent=2, ensure_ascii=False)
            )
        except configmod.AtomicWriteFailed as exc:
            raise LedgerWriteFailed(
                f"{exc}\nThe account was NOT charged in the ledger. Treat the call "
                "as unrecorded and stop rather than retry."
            ) from None
        _touch_held_locks()

    def _mutate(self, change, now: float | None = None, *,
                may_shorten: bool = False) -> LedgerState:
        """read -> change -> write, all inside one cross-process critical section."""
        guard = self._guard_or_refuse()
        try:
            state = self.read()
            self._prune_pending(state, now)
            change(state)
            self._write_locked(state, may_shorten=may_shorten)
            return state
        finally:
            guard.release()

    @staticmethod
    def _prune_pending(state: LedgerState, now: float | None = None) -> None:
        """Drop reservations older than the TTL, on the caller's clock.

        It used to read `time.time()` while every other operation on the pending
        list takes an injected `now`. A test driving the clock by hand -- which
        is every timing test in this suite, because none of them sleeps -- had
        its reservations pruned instantly and the resolve then counted a second
        time, so a test could assert the wrong arithmetic and still pass.
        """
        when = time.time() if now is None else now
        state.pending = [
            p for p in (state.pending or [])
            if when - float(p.get("ts", 0.0) or 0.0) < PENDING_TTL_SEC
        ]

    # -- decisions ---------------------------------------------------------
    def frozen_for(self, state: LedgerState, now: float) -> float:
        """Seconds of freeze left, taking the later of the two deadlines."""
        left = state.frozen_until - now
        if state.frozen_until_mono:
            mono_now = time.monotonic()
            if same_boot(state.boot_at_freeze, state.mono_at_freeze, mono_now):
                # Same boot: the monotonic deadline is authoritative and no clock
                # change can move it. After a reboot the counter restarts and its
                # readings mean something else, which is what `same_boot` excludes
                # -- `mono_now >= state.mono_at_freeze` did not: it comes true
                # again the moment a rebooted machine outlives its previous
                # uptime, and held a three-day-expired freeze open.
                left = max(left, state.frozen_until_mono - mono_now)
        return left

    def _check(self, state: LedgerState, now: float) -> None:
        left = self.frozen_for(state, now)
        if left > 0:
            left = int(left)
            raise ResolveFrozen(
                f"resolve is frozen for another {left} s "
                f"({left // 3600} h {left % 3600 // 60} m): {state.frozen_reason}. "
                "Sources that already hold a valid peer keep working; "
                "nothing re-attempts a resolve until this expires."
            )

        if state.resolves >= self.daily_ceiling:
            raise BudgetExhausted(
                f"{state.resolves} resolves already spent today, ceiling is "
                f"{self.daily_ceiling}. Refused locally — Telegram has not been asked."
            )

        recent = self._recent(state, now)
        if len(recent) >= self.burst_ceiling:
            raise BudgetExhausted(
                f"{len(recent)} resolves in the last {self.burst_window // 60} minutes, "
                f"burst ceiling is {self.burst_ceiling}. The only failure ever measured "
                "on this account was a burst, not a daily total."
            )

        gap = now - state.last_resolve_ts
        # `-self.min_gap <`, not a bare `<`: a stamp AHEAD of `now` -- a clock
        # that ran fast when the resolve was recorded, a restored snapshot,
        # another writer -- made `gap` negative and refused EVERY resolve until
        # the clock caught up, while `summary()` reported no freeze at all, so
        # nothing said why. `_recent` was given exactly this bound and the rule
        # was not copied here. Inside one gap ahead still counts, which is the
        # closed direction.
        if state.last_resolve_ts and -self.min_gap < gap < self.min_gap:
            raise BudgetExhausted(
                f"only {max(0.0, gap):.0f} s since the last resolve; the minimum "
                f"gap is {self.min_gap:.0f} s."
            )

    def _recent(self, state: LedgerState, now: float) -> list:
        """Timestamps inside the burst window, future ones included but bounded.

        A timestamp AHEAD of `now` satisfied `now - t < burst_window` forever, so
        eight resolves recorded while the clock was a day fast refused every
        resolve for a day after the correction. Anything more than one window
        ahead is a clock artefact rather than a resolve, and is dropped; a
        timestamp inside one window ahead still counts, which is the closed
        direction.
        """
        return [
            t for t in (state.recent_resolve_ts or [])
            if -self.burst_window < now - t < self.burst_window
        ]

    def check_resolve(self, now: float | None = None, *, reserve: bool = False,
                      username: str = ""):
        """Raise if a resolve must not happen. Returns None if it may.

        With `reserve=True` the permission is *taken*: the resolve is counted on
        disk before the call leaves, and a token comes back for `settle_resolve`.
        That is what `references/account.md` asks for in so many words -- "every
        resolve is counted in a durable, cross-process ledger before it happens"
        -- and what the check-call-record order could not give, because a Ctrl+C
        during the call skipped the recording entirely: five real calls, ledger
        total zero, minimum-gap latch never armed.

        `username` is passed through to the reservation. It used to be dropped
        here (`reserve_resolve("")`), so every reservation the working path took
        was anonymous and `summary()` could say a run had died mid-call without
        being able to say on which name.
        """
        when = _want_now(now)
        if not reserve:
            self._check(self.read(), when)
            return None
        return self.reserve_resolve(username, now=when)

    def check_state(self, state: LedgerState, now: float | None = None) -> None:
        """Would THIS state allow a resolve at `now`? Raises exactly as `check_resolve`.

        The public door to the same arithmetic, for a caller reasoning about a
        state it holds rather than about the file. `account.AccountSession` uses
        it for the dry run: a preview that consults only the disk reports the
        same clean slate for source 30 as for source 1, and promises resolves the
        live run refuses.
        """
        if not isinstance(state, LedgerState):
            raise BudgetExhausted(
                f"a ledger state must be a LedgerState, not {type(state).__name__}. "
                "Refused locally — Telegram has not been asked."
            )
        self._check(state, _want_now(now))

    def _apply_resolve(self, state: LedgerState, when: float) -> None:
        """Count one resolve into `state`. No disk and no guard: the caller owns both.

        Shared by the durable reservation and by the dry run's simulation, so the
        two cannot drift: what a preview says a run would spend is computed by the
        code that spends it.
        """
        state.resolves += 1
        state.last_resolve_ts = when
        recent = self._recent(state, when)
        recent.append(when)
        state.recent_resolve_ts = recent

    def plan_resolve(self, state: LedgerState, now: float | None = None) -> LedgerState:
        """One resolve on paper: check the budgets, then spend from `state` alone.

        Nothing is written. This is what a dry run charges its own copy of the
        ledger, so that source 9 of 30 meets the burst ceiling in the preview
        exactly where it would meet it on the wire.
        """
        when = _want_now(now)
        if not isinstance(state, LedgerState):
            raise BudgetExhausted(
                f"a ledger state must be a LedgerState, not {type(state).__name__}. "
                "Refused locally — Telegram has not been asked."
            )
        self._check(state, when)
        self._apply_resolve(state, when)
        return state

    def reserve_resolve(self, username: str = "", now: float | None = None) -> str:
        """Check the budgets and spend one, durably, before the call is made."""
        when = _want_now(now)
        token = os.urandom(8).hex()

        def change(state: LedgerState) -> None:
            self._check(state, when)
            self._apply_resolve(state, when)
            state.pending = list(state.pending or []) + [
                {"id": token, "username": str(username or ""), "ts": when}
            ]

        self._mutate(change, when)
        return token

    def settle_resolve(self, token: str, ok: bool = True,
                       now: float | None = None) -> LedgerState:
        """Close THAT reservation. The count was taken at reserve time and stands.

        A token settles the reservation it names or nothing at all. Settling
        a reservation nobody can find is not a no-op -- it means the caller and
        the ledger disagree about what has been spent, and the safe side of that
        disagreement is to leave the reservation standing (an unsettled one costs
        a re-read; a wrongly settled one destroys the evidence that a run died
        mid-call) and to say so.
        """
        when = _want_now(now)
        settled: list = []

        def change(state: LedgerState) -> None:
            pending = list(state.pending or [])
            settled.extend(p for p in pending if p.get("id") == token)
            if not settled:
                return
            state.pending = [p for p in pending if p.get("id") != token]

        state = self._mutate(change, when)
        if not settled:
            raise ReservationUnknown(
                f"there is no reservation {token!r} on the books at {self.path}: it "
                "was settled already, it expired after "
                f"{PENDING_TTL_SEC:.0f} s, or it belongs to another ledger. Nothing "
                "was settled and nothing was counted — the reservations that ARE "
                "open stand, because one of them may be the trace of a run that "
                "died mid-call."
            )
        return state

    def pending_resolves(self) -> list:
        """Reservations nothing ever settled -- a run killed mid-call leaves one."""
        return list(self.read().pending or [])

    def record_resolve(self, username: str, ok: bool = True, *,
                       token: str | None = None,
                       now: float | None = None) -> LedgerState:
        """Count a resolve that actually happened, or settle the one reserved for it.

        Counted whether it succeeded or not. Whether Telegram penalises a failed
        resolve more heavily than a successful one is not established in either
        direction, and the safe reading of an unknown is that it costs the same.

        **The load-bearing half.** `token` is the value
        `check_resolve(reserve=True)` handed back, and it settles THAT
        reservation. It used to be absent from this signature entirely: the
        function matched on `username`, found nothing (every reservation the
        working path took was anonymous), and fell back to settling the OLDEST
        nameless reservation on the books. Measured: run A reserves and is killed
        mid-call; ten minutes later run B reserves and completes; B's settlement
        removes A's reservation and leaves its own, so `summary()` reports a
        pending resolve against a call that succeeded while the only durable
        trace of a run dying mid-call has been quietly cleared by an unrelated
        healthy call. The daily count stays right, so nothing else notices.

        Without a token nothing is settled: the resolve is counted as a fresh
        one. That over-counts when a caller reserved and then forgot its token,
        and over-counting what left the machine is the safe direction -- the one
        thing that must never happen is a reservation somebody else owns being
        closed.
        """
        when = _want_now(now)
        if token is not None:
            return self.settle_resolve(token, ok=ok, now=when)

        def change(state: LedgerState) -> None:
            state.resolves += 1
            state.last_resolve_ts = when
            recent = self._recent(state, when)
            recent.append(when)
            state.recent_resolve_ts = recent

        return self._mutate(change, when)

    @staticmethod
    def _freeze_seconds(seconds) -> tuple[float, str]:
        """The module-level rule, under the name this class has always used."""
        return freeze_seconds(seconds)

    def freeze(self, seconds: float, reason: str, now: float | None = None) -> LedgerState:
        """Telegram said wait. Record it; stop resolving for everything.

        Called on the first FloodWait and never argued with. Each further attempt
        extends the ban, so the correct behaviour is to stop resolving entirely
        and let sources that already hold a valid peer carry on.

        The wait itself IS argued with, which is a different thing:
        `frozen_until` is `max()`-monotone and `_write_locked` refuses to shorten
        it, so an unchecked `seconds` -- or a clock briefly years out -- stopped
        every resolve in every process for as long as it said, with nothing in
        the skill able to lift it. The value is now bounded and the bound is
        recorded in the reason; `clear_freeze` is the deliberate way out.
        """
        injected = now is not None
        when = _want_now(now)
        seconds, note = self._freeze_seconds(seconds)

        def change(state: LedgerState) -> None:
            state.frozen_until = max(state.frozen_until, when + seconds)
            state.frozen_reason = (f"{reason} (recorded {_now_iso()})"
                                   + (f" — {note}" if note else ""))
            if not injected:
                # Only a real clock gets a monotonic twin. A test driving `now`
                # by hand is describing a hypothetical moment, and anchoring that
                # to this machine's uptime would make its assertions untestable.
                mono = time.monotonic()
                if mono + seconds > state.frozen_until_mono:
                    state.frozen_until_mono = mono + seconds
                    state.mono_at_freeze = mono
                    # Which boot the two numbers above belong to. Without it a
                    # monotonic deadline survives every reboot.
                    state.boot_at_freeze = time.time() - mono

        return self._mutate(change, when)

    def clear_freeze(self, reason: str, now: float | None = None) -> dict:
        """Lift a freeze deliberately, recording what was lifted and why.

        There was no way to do this at all. `frozen_until` only ever grows,
        `_write_locked` refuses every write that would shorten it, and `tg.py`
        had no `--unfreeze`: a freeze written from a wrong clock could be
        removed only by deleting the ledger by hand, which throws away the day's
        counts, the burst list and the pending reservations with it.

        This is not a way to argue with Telegram. It is the repair for a freeze
        that was never Telegram's, and because it cannot tell the two apart it
        appends what it lifted to `<ledger>.freezes.jsonl`. The lift happens
        FIRST and the journal line follows on a best-effort basis: a journal
        that cannot be written does not un-lift a freeze the caller asked for.
        The price is `recorded: False` in the answer, which says the freeze is
        gone and the decision left no trace -- the one field worth reading here.
        """
        when = _want_now(now)
        before = {}

        def change(state: LedgerState) -> None:
            before.update({
                "frozen_until": state.frozen_until,
                "frozen_until_mono": state.frozen_until_mono,
                "frozen_reason": state.frozen_reason,
                "frozen_for_sec": max(0, int(self.frozen_for(state, when))),
            })
            state.frozen_until = 0.0
            state.frozen_until_mono = 0.0
            state.mono_at_freeze = 0.0
            state.boot_at_freeze = 0.0
            state.frozen_reason = ""

        self._mutate(change, when, may_shorten=True)
        record = {
            "at": _now_iso(),
            "reason": str(reason or "no reason given"),
            "was_frozen": before.get("frozen_for_sec", 0) > 0,
            "cleared": before,
        }
        try:
            configmod.guarded_append(
                self.path.with_name(self.path.name + ".freezes.jsonl"),
                [json.dumps(record, ensure_ascii=False)],
                label="freeze log",
            )
        except (configmod.GuardBusy, OSError):
            # The freeze is already lifted and that is what the caller asked
            # for; a missing audit line does not un-lift it. It is reported.
            record["recorded"] = False
        else:
            record["recorded"] = True
        return record

    def _check_join(self, state: LedgerState) -> None:
        if state.joins >= self.join_ceiling:
            raise BudgetExhausted(
                f"{state.joins} joins already made today, ceiling is {self.join_ceiling}. "
                "Joining is an explicit operation with its own budget, never a "
                "side effect of a search."
            )

    def check_join(self) -> None:
        self._check_join(self.read())

    def reserve_join(self, now: float | None = None) -> LedgerState:
        """Check the join ceiling and spend one, durably, BEFORE the call is made.

        The resolve half of this ledger has counted before the call since the
        review; joining still counted after it, from handlers that catch
        `Exception` and `BaseException`. Those cover Ctrl+C and a supervisor's
        timeout and cover nothing else: `SIGKILL`, a lost power supply and a
        machine that panics leave no handler running at all, so three killed
        processes joined three real groups with `joins_today: 0` on disk and the
        ceiling of 3 untouched.

        It also closes the gap between the check and the count. `check_join()`
        read the file and `record_join()` wrote it one call later, so two
        processes at the ceiling both read 2 and both joined. One guarded
        read-modify-write, exactly as `reserve_resolve` does it.
        """
        def change(state: LedgerState) -> None:
            self._check_join(state)
            state.joins += 1

        return self._mutate(change, _want_now(now))

    def record_join(self) -> LedgerState:
        """Count a join that has already happened. `reserve_join` is the door.

        Kept for a caller that joins outside this ledger's knowledge and wants
        the count to be right afterwards; nothing in the skill uses it, because
        counting after the wire is the direction a kill can lose.
        """
        def change(state: LedgerState) -> None:
            state.joins += 1

        return self._mutate(change)

    def summary(self) -> dict:
        """Safe to print at any time -- including when the ledger is damaged.

        It reports the damage instead of raising, because `tg.py budget` is the
        command a human runs to find out whether anything is safe, and a
        traceback answers that question with nothing. What it never does is
        report a damaged ledger as an unfrozen one.
        """
        try:
            state = self.read()
        except LedgerUnreadable as exc:
            return {
                "date": "",
                "readable": False,
                "resolves_today": None,
                "resolve_ceiling": self.daily_ceiling,
                "joins_today": None,
                "join_ceiling": self.join_ceiling,
                "frozen": True,
                "frozen_for_sec": 0,
                "frozen_reason": str(exc),
                "session_fingerprint": "",
                "pending_resolves": None,
            }
        now = time.time()
        left = self.frozen_for(state, now)
        return {
            "date": state.date,
            "readable": True,
            "resolves_today": state.resolves,
            "resolve_ceiling": self.daily_ceiling,
            "joins_today": state.joins,
            "join_ceiling": self.join_ceiling,
            "frozen": left > 0,
            "frozen_for_sec": max(0, int(left)),
            "frozen_reason": state.frozen_reason,
            "session_fingerprint": state.fingerprint,
            "pending_resolves": len(state.pending or []),
        }


# --------------------------------------------------------------------------
# The cross-process lock
# --------------------------------------------------------------------------
# Every AccountLock this process holds. A ledger write refreshes all of them,
# which is what turns `stale_after` from "started long ago" into "stopped
# working": a run that spends 60 resolves at the 30 s minimum gap takes 1800 s,
# exactly the default staleness, so a live holder used to be robbed of its lock
# a third of the way through its own budget.
#
# A ledger write is NOT the only thing a live run does, which is why this is
# public. Bulk group history -- the one job the account path exists for -- writes
# the history log and never the ledger, so at the shipped pace a `deep` run paged
# for ~2000 s against a 1800 s staleness and a second process took the lock
# mid-run: measured 2026-08-25 with six real processes. Activity refreshes the
# lock; a ledger write is merely one kind of activity.
_HELD_LOCKS: list["AccountLock"] = []


def touch_held_locks() -> None:
    """Refresh every lock this process holds. Never raises: it is a heartbeat."""
    for lock in list(_HELD_LOCKS):
        try:
            lock.touch()
        except Exception:
            pass


# The private name this module has always used internally. Kept as an alias so
# that a caller written against either name gets the same heartbeat.
_touch_held_locks = touch_held_locks


class AccountLock:
    """One writer to the account at a time, across processes and across projects.

    A tool that throttles inside one process cannot see a second process. Two
    callers on one account therefore double the request rate against a single
    identity without either of them being able to see it, and any other Telegram
    tool signed into the same account is exactly that second caller. This lock is
    the thing standing between the two of them.

    Held as a file created with O_EXCL. Three rules the review had to put in:

    * a lock file that will not parse is **held**, not stale. It used to be
      broken in one millisecond whatever `stale_after` said, because `{}` gives
      `ts = 0.0` and an age of fifty-six years -- and `acquire` itself creates
      the file empty for a moment, so a second process arriving in that window
      stole a live lock.
    * breaking is serialised and recorded. Two processes breaking the same stale
      lock both ended up holding it, because the unlink was unconditional.
    * `release()` removes only a lock this instance still owns. A holder whose
      lock had been stolen used to delete the thief's lock on its way out.
    """

    def __init__(self, path: Path, stale_after: float = 1800.0,
                 owner: str = "telegram-research"):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # The same rule as the ledger above and as `HistoryLog`: a state
            # path that cannot exist is this module's own refusal, not a raw
            # `OSError` out of a constructor. `AccountBusy` because that is what
            # every caller already handles as "this run does not start", and
            # because `acquire` answers a file-system refusal with it too.
            raise AccountBusy(
                f"the state directory {self.path.parent} could not be created: "
                f"{exc}. Nothing was assumed about who owns the account; this "
                "run does not start."
            ) from None
        self.stale_after = stale_after
        self.owner = owner
        self._held = False
        self._token: dict = {}

    # -- content -----------------------------------------------------------
    def _content(self, broke: dict | None = None) -> dict:
        info = {
            "owner": self.owner,
            "pid": os.getpid(),
            "since": _now_iso(),
            "ts": time.time(),
            "host": socket.gethostname(),
            "pid_created": _process_identity(os.getpid()) or "",
        }
        if broke:
            info["broke"] = {
                "owner": broke.get("owner", "?"),
                "pid": broke.get("pid", "?"),
                "since": broke.get("since", "?"),
                "at": _now_iso(),
            }
        return info

    def _create(self, broke: dict | None = None) -> None:
        """O_EXCL create, content written and flushed through the same fd.

        The file must never be visible empty: an empty lock reads as unparsable,
        and an unparsable lock is one a second process has to reason about.
        """
        info = self._content(broke)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(info, fh)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            # A half-written lock is worse than none: remove it and let the
            # caller see the failure.
            try:
                self.path.unlink()
            except OSError:
                pass
            raise
        self._held = True
        self._token = {"pid": info["pid"], "since": info["since"]}
        if self not in _HELD_LOCKS:
            _HELD_LOCKS.append(self)

    def acquire(self, wait: float = 0.0, poll: float = 2.0) -> None:
        deadline = time.time() + wait
        # NTFS keeps a just-unlinked name unusable for a few milliseconds and
        # answers ACCESS_DENIED rather than "exists". That is not a busy account
        # and must not be reported as one, so it gets its own small budget.
        transient_deadline = time.time() + 1.0
        while True:
            try:
                self._create()
                return
            except PermissionError:
                if time.time() >= transient_deadline:
                    raise AccountBusy(
                        f"the lock file {self.path} could not be created: the name "
                        "is held by the file system. Nothing was assumed about who "
                        "owns the account; this run does not start."
                    ) from None
                time.sleep(0.02)
            except FileExistsError:
                info, parsed = self._read()
                if self._is_stale(info, parsed) and self._break_and_take(info):
                    return
                if time.time() >= deadline:
                    raise AccountBusy(
                        f"the account is held by {info.get('owner', '?')} "
                        f"(pid {info.get('pid', '?')}) since {info.get('since', '?')}. "
                        "Two writers to one account double its request rate; "
                        "this run does not start."
                    )
                time.sleep(poll)

    # -- staleness ---------------------------------------------------------
    def _mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def _is_stale(self, info: dict, parsed: bool) -> bool:
        """Is the holder gone? Unreadable means HELD, not stale.

        The age comes from the file's own mtime as well as from the `ts` inside
        it, so a lock written by something else -- another tool, an older format,
        a hand edit -- with no `ts` at all is still respected for the full
        `stale_after`, instead of being broken on sight because `float(None or 0)`
        put its birthday in 1970.
        """
        mtime = self._mtime()
        if mtime is None:
            return False              # it vanished; the O_EXCL retry will settle it
        now = time.time()
        if not parsed:
            return (now - mtime) > self.stale_after

        recorded = info.get("pid_created")
        same_host = info.get("host") in (None, socket.gethostname())
        if recorded and same_host:
            identity = _process_identity(info.get("pid"))
            if identity is not None and identity != recorded:
                return True           # the owner is definitively gone, pid reuse included

        try:
            ts = float(info.get("ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        last_alive = max(ts, mtime)
        return (now - last_alive) > self.stale_after

    def _breaker(self, timeout=None) -> configmod.FileGuard:
        """The guard that serialises everything which may REPLACE this lock file.

        Named once here because `touch()` has to take the same one: any two
        operations that both end in a write to the lock path must exclude each
        other, or the older one lands after the newer.

        **The wait outlives the staleness threshold**, which it did not: two
        seconds of waiting against thirty seconds of "this guard is dead" meant
        a process killed while breaking a lock made every later break give up
        one turn short of being allowed to clear it -- for half a minute, and
        with the account lock itself unbreakable for the whole of it. Both
        numbers are `config`'s, so this file cannot drift from the rule again.
        `touch` and `release` pass a deliberately short wait instead: they are
        a heartbeat and a cleanup, and the right thing for them to do about a
        guard somebody else holds is nothing.

        Both numbers are read when the guard is BUILT rather than bound as
        defaults when this function is defined, so a caller that lowers them --
        the suite does, to keep three lock-contention tests from waiting out a
        real timeout -- lowers the pair together and cannot leave the wait
        shorter than the threshold by accident.
        """
        return configmod.FileGuard(
            self.path.with_name(self.path.name + ".break"),
            timeout=configmod.GUARD_TIMEOUT if timeout is None else timeout,
            stale_after=configmod.GUARD_STALE_AFTER,
            label="account-lock break",
        )

    def _break_and_take(self, info: dict) -> bool:
        """Break a stale lock and take it, with nobody else able to do both.

        Two processes breaking the same lock used to end with both of them
        holding it: A unlinked and created, then B unlinked A's fresh lock and
        created its own. The break runs inside its own short-lived guard and
        ends with the O_EXCL create, so the window does not exist any more.
        """
        breaker = self._breaker()
        try:
            breaker.acquire()
        except configmod.GuardBusy:
            return False
        try:
            current, parsed = self._read()
            if self.path.exists() and not self._is_stale(current, parsed):
                return False          # somebody already replaced it with a live one
            previous = current if parsed else info
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return False
            self._record_break(previous)
            self._create(broke=previous)
            return True
        except (FileExistsError, PermissionError):
            return False
        finally:
            breaker.release()

    def _record_break(self, previous: dict) -> None:
        """`references/account.md`: breaking a lock is recorded, not silent."""
        line = {
            "at": _now_iso(),
            "broken_by": {"owner": self.owner, "pid": os.getpid()},
            "previous": {
                "owner": previous.get("owner", "?"),
                "pid": previous.get("pid", "?"),
                "since": previous.get("since", "?"),
                "parsed": bool(previous),
            },
            "stale_after": self.stale_after,
        }
        try:
            with self.path.with_name(self.path.name + ".broken.jsonl").open(
                    "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # -- holding -----------------------------------------------------------
    def owns_the_file(self) -> bool:
        """Is the lock on disk still the one this instance created?"""
        if not self._held:
            return False
        info, parsed = self._read()
        if not parsed:
            return False
        return (info.get("pid") == self._token.get("pid")
                and info.get("since") == self._token.get("since"))

    def touch(self) -> None:
        """Refresh `ts` so the lock reads as alive for another `stale_after`.

        `ts` used to be written once at acquire and never again, so "stale" meant
        "started long ago" rather than "stopped working".

        It runs inside the same `.break` guard `_break_and_take` holds, and that
        is not decoration. `owns_the_file()` READS and `atomic_write_text` is an
        `os.replace`: a lock legitimately broken between those two statements
        was replaced by the old holder's content, and then A and B both had
        `_held = True` and both believed they owned the account -- the
        two-writers failure this class exists to prevent. B's own
        `owns_the_file` then failed, so it stopped refreshing and its `release()`
        correctly declined to unlink, and A's resurrected file survived a further
        `stale_after`. The guard makes breaking and refreshing exclude each
        other, and the ownership check is re-read INSIDE it.

        Never raises: it is a heartbeat, called from every ledger write. A guard
        held by somebody else means somebody is already replacing this lock, and
        the right thing to do about that is nothing.
        """
        if not self._held:
            return
        breaker = self._breaker(timeout=0.5)
        try:
            breaker.acquire()
        except configmod.GuardBusy:
            return
        try:
            if not self.owns_the_file():
                return
            info, parsed = self._read()
            if not parsed:
                return
            info["ts"] = time.time()
            try:
                configmod.atomic_write_text(self.path, json.dumps(info))
            except (configmod.AtomicWriteFailed, OSError):
                pass
        finally:
            breaker.release()

    def release(self) -> None:
        """Remove the lock file, but only while it is still ours to remove.

        Inside the same `.break` guard `touch` and `_break_and_take` hold, and
        for the same measured reason: `owns_the_file()` READS and the unlink
        follows it. A lock legitimately broken between those two statements --
        the holder had gone quiet long enough to look stale -- was deleted by its
        PREVIOUS owner on the way out, and the new holder then ran with no lock
        file at all, so a third process took the account alongside it. The
        ownership check is re-read inside the guard, exactly as `touch` does it.

        A guard held by somebody else means somebody is already replacing this
        lock. The file on disk is then about to stop being ours whatever we do,
        and deleting it would delete theirs, so the unlink is skipped -- but this
        instance still stops claiming to hold anything.
        """
        if not self._held:
            return
        breaker = self._breaker(timeout=0.5)
        try:
            breaker.acquire()
        except configmod.GuardBusy:
            breaker = None
        try:
            if breaker is not None and self.owns_the_file():
                self.path.unlink(missing_ok=True)
        finally:
            if breaker is not None:
                breaker.release()
            self._held = False
            self._token = {}
            if self in _HELD_LOCKS:
                _HELD_LOCKS.remove(self)

    def _read(self) -> tuple[dict, bool]:
        """`(info, parsed)`. `parsed=False` means: treat it as held, age unknown."""
        try:
            data = configmod.read_bytes_shared(self.path)
        except OSError:
            return {}, False
        try:
            info = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}, False
        if not isinstance(info, dict):
            return {}, False
        return info, True

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False
