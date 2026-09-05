"""Paths, budgets and the credential the skill deliberately does not own.

The credential is READ, never kept: from three environment variables, or
failing that from a file whose path an environment variable names. This skill
stores no copy of its own and creates no credential file anywhere -- two copies
of one secret drift apart and survive every tidy-up, so there is exactly one
copy and it is not this skill's.

Everything else in this file is ordinary: where the registry lives, where run
state lives, how fast the free surfaces are read, what the account budgets are.

Two low-level primitives also live here because both `resolve.py` and
`registry.py` need them and neither should import the other: `FileGuard`, a
cross-process mutex built on `O_EXCL`, and `atomic_write_text` /
`read_bytes_shared`, the pair that makes replace-into-place survive Windows.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Printed verbatim in every error about a missing or unreadable credential. It
# says where the skill looks and why it looks nowhere else: a tool that goes
# hunting for credentials on disk eventually finds the wrong ones.
CREDENTIAL_NOTICE = (
    "The Telegram credential comes from the three TELEGRAM_API_ID / "
    "TELEGRAM_API_HASH / TELEGRAM_SESSION variables, or failing that from the "
    "file named by TELEGRAM_RESEARCH_ENV. Nothing else is read: this skill never "
    "searches the disk for a credential and never creates one. Set the three "
    "variables, or point TELEGRAM_RESEARCH_ENV at the file that already holds "
    "them — somewhere outside any folder that is committed or cloud-synced."
)

# Environment variables -- every path is overridable, none is compiled in.
ENV_CREDENTIAL = "TELEGRAM_RESEARCH_ENV"     # FALLBACK file holding TELEGRAM_API_ID/HASH/SESSION
ENV_STATE = "TELEGRAM_RESEARCH_STATE"        # DIRECTORY for registry, ledger, locks
ENV_CONFIG = "TELEGRAM_RESEARCH_CONFIG"      # optional JSON overriding the defaults
ENV_TZ = "TELEGRAM_RESEARCH_TZ"              # fixed offset for run dates, else the machine's

# The one line that decides where the state lives: `~/.telegram-research`.
#
# Data, not code, and deliberately OUTSIDE the skill's own folder. The registry,
# the resolve ledger, the account lock and the peer cache have to outlive every
# reinstall: `npx skills update` replaces the skill's folder wholesale, and a
# ledger kept inside it would be replaced with it -- taking the record of an
# account freeze, the one thing that stops the next run from repeating it.
#
# One named constant, because the skill's own name may change and this is the
# only line that would have to change with it.
STATE_DIR_NAME = ".telegram-research"

# Dict KEYS whose value is replaced wholesale by `redact_obj`.
#
# This is a key-form list and nothing more. It does NOT mean the values behind
# those names can be recognised anywhere else: an `api_id` is a bare integer and
# no regular expression can tell it from a member count or a message id, so
# `_SECRET_PATTERNS` deliberately carries no value-level pattern for it. The only
# real protection for `api_id` is never letting it into a string; see
# `read_credentials`, which hands back a dict the caller is expected to clear.
SECRET_KEYS = (
    "TELEGRAM_SESSION", "TELEGRAM_API_HASH", "TELEGRAM_API_ID",
    "SESSION", "STRING_SESSION", "API_HASH", "API_ID", "BOT_TOKEN",
)


class ConfigError(RuntimeError):
    """Something the operator has to fix. Always says what and where."""


# --------------------------------------------------------------------------
# Local time -- the operator's calendar day, not a compiled-in city
# --------------------------------------------------------------------------
# `run.py` and `registry.py` both stamped a fixed `timezone(timedelta(...))` into
# a module constant. Run-folder names, `first_seen` and `last_checked` are LOCAL
# calendar dates, so a compiled-in offset silently shifts every one of them for
# every operator it was not compiled for: the day boundary falls at the wrong
# hour, and a source first seen late in the evening is filed under the wrong
# date. A fixed offset also cannot follow DST, so wherever DST is observed the
# same stamps slide by an hour twice a year and a long run keeps writing the
# offset it started with.
#
# The machine's own zone is the right default: it is what "today" means to the
# person reading the report. `TELEGRAM_RESEARCH_TZ` pins it to a fixed offset when
# that matters -- a test that must not depend on this machine, or an operator who
# wants a run's dates to stay in one zone while travelling.
_OFFSET_RE = re.compile(r"^(?P<sign>[+-])(?P<h>\d{1,2})(?::?(?P<m>\d{2}))?$")
_TZ_CACHE: dict[str, timezone] = {}


def local_tz() -> timezone:
    """The timezone run dates are stamped in. Machine-local unless pinned."""
    raw = (os.environ.get(ENV_TZ) or "").strip()
    if not raw or raw.lower() == "local":
        # `astimezone()` on a naive now() attaches the platform zone, DST and
        # all, and re-reads it every call so a long run crossing a DST boundary
        # does not keep stamping the old offset.
        return datetime.now().astimezone().tzinfo
    if raw in _TZ_CACHE:
        return _TZ_CACHE[raw]
    if raw.upper() in ("UTC", "Z", "GMT"):
        _TZ_CACHE[raw] = timezone.utc
        return _TZ_CACHE[raw]
    match = _OFFSET_RE.match(raw)
    if not match:
        raise ConfigError(
            f"{ENV_TZ}={raw!r} is not a timezone this skill can read. Use a fixed "
            "offset (`+05:00`, `-03:30`, `+07`), `UTC`, or `local` for the "
            "machine's own zone."
        )
    hours = int(match.group("h"))
    minutes = int(match.group("m") or 0)
    if hours > 14 or minutes > 59:
        raise ConfigError(f"{ENV_TZ}={raw!r} is not a real UTC offset")
    delta = timedelta(hours=hours, minutes=minutes)
    if match.group("sign") == "-":
        delta = -delta
    _TZ_CACHE[raw] = timezone(delta)
    return _TZ_CACHE[raw]


def today_local() -> str:
    return datetime.now(local_tz()).date().isoformat()


def now_local() -> str:
    return datetime.now(local_tz()).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Shared readers, so that no module invents its own: every number that comes
# off disk goes through the one finite check, and every path out of the
# environment through the one path reader. Do not fork these.
# --------------------------------------------------------------------------
def want_finite_number(data: dict, key: str, default=0):
    """The number at `key`, or a ValueError naming what was there instead.

    `json.loads` accepts `NaN`, `Infinity` and `-Infinity` by default, and both
    pass `isinstance(x, float)`. NaN then makes EVERY comparison false, so a
    guard written as `if now < frozen_until` reads a damaged file as "not
    frozen" and a paced fetcher as "no floor". A finite check is the only place
    that catches it; `math.isfinite` is false for both NaN and the infinities.
    """
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key!r} is {value!r}, which is not a number")
    if not math.isfinite(value):
        raise ValueError(f"{key!r} is {value!r}, which is not a finite number")
    return value


def env_path(value: str) -> Path:
    """A path taken from the environment, anchored so it cannot drift.

    `Path("state")` follows the current directory, so the same run reads one
    ledger and writes another; `Path("~/tg")` is a directory literally named
    `~`. Both are silent: the second ledger is created on demand and looks
    healthy. expanduser() first -- resolve() would anchor the `~` instead of
    expanding it.
    """
    return Path(value).expanduser().resolve()


class GuardBusy(RuntimeError):
    """A cross-process guard could not be taken. The operation must not proceed."""


class AtomicWriteFailed(RuntimeError):
    """`os.replace` never won. Nothing was written; the destination is untouched."""


# --------------------------------------------------------------------------
# Windows-survivable file primitives
# --------------------------------------------------------------------------
# Two facts about NTFS drive everything below.
#
# 1. `open()` in CPython does not pass FILE_SHARE_DELETE, so while any process
#    holds a file open, `os.replace` over it fails with PermissionError
#    [WinError 5]. The ledger is exactly the file other processes read, and the
#    review measured 44 failed writes out of 300 under two writers -- each one a
#    FloodWait freeze that never reached disk.
# 2. `open(..., "a")` is seek-then-write in the CRT, and the pair is not atomic,
#    so concurrent appends overwrite each other and every survivor is still
#    well-formed JSON. The review measured 22 whole records lost out of 600.
#
# `read_bytes_shared` fixes (1) from the reader's side; `atomic_write_text`
# retries from the writer's side; `FileGuard` fixes (2) by serialising the
# writers outright.

_IS_WINDOWS = sys.platform == "win32"


def read_bytes_shared(path: Path) -> bytes:
    """Read a file without blocking another process's `os.replace` over it.

    On Windows the handle is opened with FILE_SHARE_DELETE, which CPython's own
    `open()` does not request. Everywhere else this is an ordinary read. Any
    failure inside the ctypes path falls back to a plain read so that the
    behaviour is never worse than the standard library's.
    """
    path = Path(path)
    if not _IS_WINDOWS:
        return path.read_bytes()
    try:
        import ctypes
        import ctypes.wintypes as wt
        import msvcrt

        GENERIC_READ = 0x80000000
        FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_SHARE_DELETE = 0x1, 0x2, 0x4
        OPEN_EXISTING = 3
        INVALID_HANDLE = ctypes.c_void_p(-1).value

        create = ctypes.windll.kernel32.CreateFileW
        create.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                           wt.DWORD, wt.DWORD, wt.HANDLE]
        create.restype = ctypes.c_void_p
        handle = create(
            str(path), GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, OPEN_EXISTING, 0, None,
        )
        if handle in (INVALID_HANDLE, None, 0):
            code = ctypes.get_last_error() or ctypes.windll.kernel32.GetLastError()
            if code in (2, 3):                       # FILE_NOT_FOUND / PATH_NOT_FOUND
                raise FileNotFoundError(errno.ENOENT, "No such file", str(path))
            return path.read_bytes()                 # sharing violation and friends
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        raise
    except Exception:
        return path.read_bytes()


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8",
                      attempts: int = 14, delay: float = 0.02) -> None:
    """Write via a private temp file and `os.replace`, retrying the replace.

    The temp name carries the pid AND a random token: one fixed `.tmp` turns two
    concurrent saves into a FileNotFoundError on rename, and one pid-only name
    does the same for two threads. The temp file is removed on every exit path,
    so a failed write leaves no orphan in the state directory.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    last: Exception | None = None
    try:
        with tmp.open("w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(attempts):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:           # WinError 5 / 32: someone has it open
                last = exc
                time.sleep(delay * (attempt + 1))
        raise AtomicWriteFailed(
            f"could not replace {path} after {attempts} attempts: {last}. "
            "Another process is holding it open. Nothing was written, and the "
            "file on disk is unchanged."
        )
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


class FileGuard:
    """A cross-process mutex: one holder of `path` at a time, or nobody acts.

    `O_EXCL` on NTFS is sound -- the review raced eight processes at a shared
    start instant and got exactly one winner -- so the primitive is the file
    itself. What the guard adds is a bounded wait, a staleness rule keyed to the
    lock's own mtime rather than to anything written inside it, and a refusal
    when the wait runs out. It never returns having failed to take the guard.

    This is NOT `resolve.AccountLock`. That one is a policy object with an owner,
    a half-hour staleness and a message for a human. This is a mutex held for
    milliseconds around a read-modify-write.
    """

    def __init__(self, path: Path, *, timeout: float = 10.0,
                 stale_after: float = 60.0, poll: float = 0.005,
                 label: str = "state"):
        self.path = Path(path)
        self.timeout = timeout
        self.stale_after = stale_after
        self.poll = poll
        self.label = label
        self._fd: int | None = None
        # What this object wrote into the guard file, so that `release` can tell
        # "still mine" from "broken as stale and re-taken by somebody else".
        # The random half is there because pid plus a millisecond is not unique
        # enough for one process re-taking a guard it was just broken out of.
        self._token: bytes | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                # O_BINARY on Windows: without it the CRT translates the "\n"
                # into "\r\n" and the bytes on disk are not the bytes `release`
                # compares against. Absent everywhere else, hence the getattr.
                self._fd = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                )
                token = (f"{os.getpid()} {time.time():.3f} "
                         f"{os.urandom(4).hex()}\n").encode("utf-8")
                os.write(self._fd, token)
                self._token = token
                return
            except FileExistsError:
                if self._break_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise GuardBusy(
                        f"the {self.label} guard at {self.path} is held by another "
                        f"process and did not free in {self.timeout:.0f} s. "
                        "The operation is refused rather than run unguarded."
                    )
                time.sleep(self.poll)
            except PermissionError:
                # NTFS delete-pending. Between the previous holder's `close` and
                # the name actually going away, `CreateFile` on it returns
                # ACCESS_DENIED rather than "exists" or "created". Measured under
                # two processes appending: it clears in milliseconds, but it is
                # not rare, and letting it escape means an unguarded caller.
                if time.monotonic() >= deadline:
                    raise GuardBusy(
                        f"the {self.label} guard at {self.path} could not be taken "
                        f"in {self.timeout:.0f} s (the name is still being released). "
                        "The operation is refused rather than run unguarded."
                    )
                time.sleep(max(self.poll, 0.01))

    def _break_if_stale(self) -> bool:
        """A guard older than `stale_after` belonged to a process that died."""
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True          # it vanished between the create and the stat
        if age <= self.stale_after:
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return True

    def release(self) -> None:
        """Drop the guard, and ONLY if this object is still the one holding it.

        The `unlink` used to sit outside the "did we ever acquire" test, so a
        guard that never acquired -- or one whose file had been broken as stale
        by another process while this one was suspended -- deleted the CURRENT
        holder's guard on its way out, after which a third process could
        `O_EXCL`-create it and two writers ran the read-modify-write at once.
        On NTFS the damage is partly hidden by `unlink` refusing to remove a
        file another process holds open; on POSIX it succeeds, which is why the
        test for this drives the token rather than an open handle.

        Ownership is the token written at acquire time. A guard file whose
        contents are not ours belongs to somebody else and is left alone.
        """
        if self._fd is None:
            self._token = None
            return                    # never acquired: nothing here is ours
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        token, self._token = self._token, None
        try:
            current = self.path.read_bytes()
        except OSError:
            return                    # already gone, or unreadable: not ours to remove
        if token is not None and current.strip() != token.strip():
            return                    # broken as stale and re-taken: leave the holder alone
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "FileGuard":
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def file_guard(path: Path, *, timeout: float = 20.0, stale_after: float = 120.0,
               label: str = "run") -> FileGuard:
    """The write guard for a data file, by the convention `Registry` set.

    The guard file sits beside the target and is named `<name>.write`, so two
    writers of the same file serialise as long as they both ask for it by the
    file's own path -- which is the whole reason the name is derived here rather
    than chosen at each call site.
    """
    path = Path(path)
    return FileGuard(path.with_name(path.name + ".write"),
                     timeout=timeout, stale_after=stale_after, label=label)


def append_lines(path: Path, lines) -> int:
    """Append whole lines, starting on a fresh one. CALL THIS UNDER A GUARD.

    Split out from `guarded_append` so a caller that has to READ the file before
    deciding what to append -- the post de-duplicator does -- can do both inside
    one hold of the guard rather than taking it twice and racing itself.

    Starting on a fresh line is the rule `Registry._heal_last_line` earned: a
    crash mid-append leaves a line with no terminator, and appending onto it
    destroys TWO records, the half-written one and the healthy one welded to it.
    """
    path = Path(path)
    payload = [line for line in lines if line]
    if not payload:
        return 0
    needs_newline = False
    try:
        if path.stat().st_size:
            with path.open("rb") as probe:
                probe.seek(-1, 2)
                needs_newline = probe.read(1) != b"\n"
    except OSError:
        needs_newline = False
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        if needs_newline:
            fh.write("\n")
        for line in payload:
            fh.write(line + "\n")
    return len(payload)


def guarded_append(path: Path, lines, *, timeout: float = 20.0,
                   stale_after: float = 120.0, label: str = "run") -> int:
    """Append whole lines to a JSONL file under a cross-process guard.

    The registry has been written this way since the review measured 22 records
    out of 600 vanishing under two unguarded appenders; the run folder had not.
    `posts.jsonl`, `fetchlog.jsonl` and `registry-delta.jsonl` were appended with
    a bare `open("a")`, and two `tg.py --run <same run>` processes -- which is
    exactly how a `research` branch fans out over three channels -- lost records
    with every survivor still well-formed JSON, so nothing downstream could see
    that anything was missing. Measured here on 2026-08-25: 48 of 600 lost.
    """
    path = Path(path)
    payload = [line for line in lines if line]
    if not payload:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_guard(path, timeout=timeout, stale_after=stale_after, label=label):
        return append_lines(path, payload)


@dataclass
class Budgets:
    """What the skill is allowed to spend, per run and per day."""

    # free surfaces
    min_gap_sec: float = 2.0
    max_gap_sec: float = 4.0
    batch_size: int = 50
    batch_rest_sec: float = 60.0
    max_requests_per_run: int = 400        # a run that wants more says so explicitly
    max_pages_per_channel: int = 25        # 20 messages a page

    # account
    daily_resolve_ceiling: int = 180
    burst_ceiling: int = 8
    burst_window_sec: int = 600
    min_resolve_gap_sec: float = 30.0
    daily_join_ceiling: int = 3
    # How many `messages.getHistory` calls one run may make. It used to be
    # BORROWED from `max_requests_per_run` above -- a free-surface knob an
    # override file may legitimately raise for a big crawl -- so
    # `{"budgets": {"max_requests_per_run": 100000}}` raised the account's own
    # ceiling from 400 to 100000 with `override_notes` empty and nothing on
    # stderr. `account._history_ceiling` already looked for this name first;
    # until now `Budgets` had no such field, so the knob it looked for could not
    # be set at all. It is an account ceiling, so it may only fall.
    max_history_requests_per_run: int = 400

    # the jargon loop
    max_rounds: int = 3
    min_new_posts_per_round: int = 3

    def as_dict(self) -> dict:
        return asdict(self)


# The account block is not a tuning knob. `references/account.md` calls these
# non-negotiable, and an override file that raised the daily ceiling to 100000
# and dropped the minimum gap to 0 was accepted silently before this existed.
# An override may still move each of them in the SAFE direction, and the change
# is recorded in `Config.override_notes` so it appears in `tg.py budget`'s
# `config_notes`.
_CEILINGS_MAY_ONLY_FALL = {
    "daily_resolve_ceiling", "burst_ceiling", "daily_join_ceiling",
    # The getHistory ceiling is an account ceiling like the three above it. It
    # was not in this set and could not be, because the field did not exist:
    # the account borrowed the free surface's `max_requests_per_run`, which an
    # override may raise to anything. See `Budgets.max_history_requests_per_run`.
    "max_history_requests_per_run",
}
_GAPS_MAY_ONLY_RISE = {
    "min_resolve_gap_sec", "burst_window_sec",
    # The free surface's pacing belongs here for the same reason the account's
    # does. `Pacer` accepted `min_gap=0, max_gap=0` with no floor at all -- 8
    # waits in 0.046 s, measured -- so one override file sent every request to
    # `t.me` out back to back. `Pacer` now enforces the floor itself, which is
    # the guarantee; this is the announcement, so a refused value appears in
    # `override_notes` and on stderr instead of being clamped in silence.
    # Widening a gap is still allowed: slowing down is never the dangerous
    # direction.
    "min_gap_sec", "max_gap_sec",
}


@dataclass
class Config:
    # `default_state_dir` is defined further down; the lambda defers the call to
    # construction time, which is also what lets a test move HOME.
    state_dir: Path = field(default_factory=lambda: default_state_dir())
    budgets: Budgets = field(default_factory=Budgets)
    credential_path: Path | None = None
    topics_vocabulary: Path | None = None
    root: Path | None = None
    override_notes: list[str] = field(default_factory=list)

    # -- derived paths -----------------------------------------------------
    @property
    def registry_path(self) -> Path:
        return self.state_dir / "sources.jsonl"

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "resolve-ledger.json"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "account.lock"

    @property
    def pace_dir(self) -> Path:
        return self.state_dir / "pace"

    def ensure_dirs(self) -> None:
        """Create the state directory, or say which setting is wrong.

        `TELEGRAM_RESEARCH_STATE` names a DIRECTORY. Pointing it at one of the
        files inside it (`.../sources.jsonl`) used to produce a raw
        `FileExistsError` traceback from `mkdir`; it is a configuration error
        and it says so.

        The advice has to match the failure. Every `OSError` used to get the
        file-vs-folder sentence, so a UNC host that does not resolve
        (`WinError 53`) and a location this account cannot write to
        (`WinError 5`) were both diagnosed as "you pointed it at a file" --
        one sentence, exit 7, and the wrong instruction. Each kind now names
        what actually went wrong.
        """
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.pace_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                f"the state directory {self.state_dir} could not be created: {exc}. "
                + _mkdir_advice(exc, self.state_dir)
            ) from None

    def as_dict(self) -> dict:
        return {
            "state_dir": str(self.state_dir),
            "registry_path": str(self.registry_path),
            "ledger_path": str(self.ledger_path),
            "credential_path": str(self.credential_path) if self.credential_path else None,
            "topics_vocabulary": str(self.topics_vocabulary) if self.topics_vocabulary else None,
            "budgets": self.budgets.as_dict(),
            "override_notes": list(self.override_notes),
            "notice": CREDENTIAL_NOTICE,
        }


# Windows error numbers that mean something other than "that is a file".
#
# 3 (ERROR_PATH_NOT_FOUND) is in the set because `mkdir(parents=True)` creates
# every missing component it is allowed to create: what is left when it still
# says "path not found" is a root nobody can create -- an unmapped drive letter,
# a dropped share. Measured on 2026-08-25 with `TELEGRAM_RESEARCH_STATE=Q:\nowhere\state`,
# which fell through to the generic advice before this.
_WIN_UNREACHABLE = {3, 53, 67, 1231, 1232}  # path/net path / bad net name / unreachable
_WIN_DENIED = {5, 1314}                     # access denied / no privilege held


def _mkdir_advice(exc: OSError, target: Path) -> str:
    """The sentence that fits THIS failure, not the one that fits most of them."""
    winerror = getattr(exc, "winerror", None)
    if isinstance(exc, PermissionError) or winerror in _WIN_DENIED:
        return (
            f"This account may not create {target}. Point {ENV_STATE} at a "
            "directory you can write to — a folder under your home directory, "
            "say — rather than at a protected location."
        )
    if winerror in _WIN_UNREACHABLE:
        return (
            f"The location {target} could not be reached: the network path or "
            "drive it names is not available. Check the host or the mapping, or "
            f"point {ENV_STATE} at a local directory."
        )
    if isinstance(exc, (FileExistsError, NotADirectoryError)):
        return (
            f"{ENV_STATE} names a DIRECTORY — the registry, the ledger and the "
            "lock are files inside it. Point it at a folder, not at a file."
        )
    return (
        f"{ENV_STATE} names a DIRECTORY that this skill creates if it is "
        "missing; the path above is one it cannot create. Check every component "
        "of it, then point the variable at a directory."
    )


# --------------------------------------------------------------------------
# Where the state lives -- and why it is not the working directory
# --------------------------------------------------------------------------
def skill_root() -> Path:
    """The skill's own folder -- the one holding `SKILL.md`, `scripts/` and
    `references/`. Taken from this file, so it is right wherever the skill was
    installed."""
    return Path(__file__).resolve().parent.parent


# The two directories a GLOBAL install lands in. A skill under either of them
# belongs to the machine, not to a project, so there is no project above it to
# find -- and walking up anyway lands in the home directory, which is the one
# place a run folder must never be created.
_GLOBAL_INSTALL_DIRS = (".claude", ".agents")


def is_global_install(skill_dir: Path | None = None) -> bool:
    """True when the skill's folder sits inside `~/.claude` or `~/.agents`.

    That is what `npx skills add -g` produces, and it is the whole reason
    `repo_root()` may not walk up from there: `~/.claude/CLAUDE.md` is the
    STANDARD place Claude Code keeps user-level memory, so the walk finds it on
    a large share of machines and declares the home directory to be "the
    project". The operator is then standing in their own repository while
    `telegram-runs/` is created under `~/.claude/`. A versioned `~/.claude`
    (a `.git` in it) does the same thing one level higher.

    A machine that cannot say where home is answers False rather than raising:
    `repo_root()` is not the place to refuse a run, and `home_dir()` already
    refuses loudly wherever home actually matters.
    """
    here = skill_root() if skill_dir is None else Path(skill_dir).resolve()
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        return False
    for name in _GLOBAL_INSTALL_DIRS:
        root = home / name
        if here == root or root in here.parents:
            return True
    return False


def repo_root() -> Path:
    """The project directory RUN FOLDERS are created under. Never the state.

    Found by walking up from the skill's own folder for a `.git` or a
    `CLAUDE.md`, which is what an installed skill sits inside when it is
    installed into a project; failing that, the directory the command was run
    from, because a report belongs in the project the operator is working in.

    **A global install does not walk at all.** A skill under `~/.claude` or
    `~/.agents` has no project above it by construction, and the walk would find
    `~/.claude/CLAUDE.md` -- user-level agent memory, present on most machines --
    and answer with the home directory. Run folders then pile up in
    `~/.claude/telegram-runs/` while the operator is sitting in their project.
    The working directory is the honest answer there, and the only one.

    The working-directory fallback is exactly why the state no longer comes from
    here: a state directory that follows the shell means one `cd` creates a
    second, empty ledger with no freeze and no count in it -- and a second lock
    file, so the cross-process lock cannot see the first process at all. State is
    anchored on the home directory instead; see `default_state_dir`.
    """
    if is_global_install():
        return Path.cwd()
    here = skill_root()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists() or (candidate / "CLAUDE.md").exists():
            return candidate
    return Path.cwd()


def home_dir() -> Path:
    """The home directory, or a sentence naming the one setting that fixes it.

    `Path.home()` raises when the platform cannot say where home is (neither
    `HOME` nor `USERPROFILE` set). Guessing at that point -- a temp folder, the
    skill's own folder, the working directory -- is how a second empty ledger
    gets created without anybody seeing it, so the skill stops and asks for an
    explicit directory instead.
    """
    try:
        return Path.home()
    except RuntimeError:
        raise ConfigError(
            "this machine does not say where the home directory is (neither "
            "HOME nor USERPROFILE is set), so the default state directory "
            f"~/{STATE_DIR_NAME} has nowhere to be. Set {ENV_STATE} to an "
            "absolute directory that survives reinstalls of this skill: the "
            "registry, the resolve ledger and the account lock live in it."
        ) from None


def default_state_dir() -> Path:
    """`~/.telegram-research` -- outside every project and outside this skill.

    Not the skill's own folder: `npx skills update` replaces that folder
    wholesale, and the resolve ledger inside it -- the record of an account
    freeze -- would go with it. Not the working directory either, for the reason
    `repo_root` gives. `TELEGRAM_RESEARCH_STATE` overrides it.
    """
    return home_dir() / STATE_DIR_NAME


# Names that mean "you pointed the variable at one of the files, not at the
# folder holding them", plus the suffixes a data file carries. The existing-file
# check only fires on a machine where the file is already there; on a fresh one
# it is not, and `mkdir` cheerfully made a DIRECTORY called `sources.jsonl` with
# the real registry then living at `sources.jsonl/sources.jsonl`.
_STATE_FILE_NAMES = frozenset({
    "sources.jsonl", "resolve-ledger.json", "account.lock",
})
_STATE_FILE_SUFFIXES = frozenset({
    ".jsonl", ".json", ".lock", ".log", ".txt", ".csv", ".db", ".sqlite",
    ".bak", ".tmp", ".yaml", ".yml", ".ini", ".cfg", ".md", ".env",
})


def _anchored(value: str, anchor: Path) -> Path:
    """A path from a file or a variable, resolved against `anchor` and nothing else.

    One rule, used with three anchors -- the home directory for the state, the
    project for the other variables, an override file's own folder for the paths
    written inside it -- so that the expanduser step does not exist three times. `env_path` still does the expanding and the resolving; this decides
    what a RELATIVE value is relative TO, which is the one thing `resolve()`
    gets wrong on its own -- it uses the process's current directory, which is
    the shell.

    An absolute value needs no special case: joining an absolute path onto
    anything returns the absolute path unchanged, drive and all. The one value
    the join does touch is a drive-relative `/vocab.json` on Windows, which
    takes the ANCHOR's drive rather than the shell's -- which is the answer this
    function exists to give. A branch for `is_absolute()` was here and was
    removed: mutation showed it changed no behaviour any test could see, and
    untested code that looks load-bearing is worse than no code.
    """
    return env_path(str(Path(anchor) / Path(value).expanduser()))


def anchored_state_path(value: str) -> Path:
    """`env_path`, plus the anchor `TELEGRAM_RESEARCH_STATE` needs: HOME.

    The state directory is the one path that may never move between two runs on
    the same machine, whatever shell started them. Letting it move was exactly
    that: a run in one folder wrote a 36 468 s freeze, a run in another read
    `frozen_for() == 0`, kept its own ledger and took a DIFFERENT `account.lock`,
    so both processes held "the" account lock at once.

    A relative value is therefore anchored where the default is anchored -- on
    the home directory, not on `repo_root()`. `repo_root()` falls back to the
    working directory (a run folder belongs in the project you are in), so
    anchoring state there would hand the decision back to the shell the moment
    the skill is installed outside a project. An absolute value is left where it
    points.

    Home is consulted only where it is actually needed: for a relative value and
    for a `~` to expand. An absolute value is a complete answer on its own, and
    on a machine that cannot say where home is it is the ONLY answer available --
    asking for home first would refuse the very setting that fixes that machine.
    """
    if value.startswith("~") or not Path(value).is_absolute():
        return _anchored(value, home_dir())
    return env_path(value)


def anchored_env_path(value: str) -> Path:
    """`env_path`, plus the anchor a RELATIVE value needs: the project, not the shell.

    Every path out of the environment goes through `config.env_path`, and this
    calls it -- there is no second copy of the
    expanduser/resolve rule here. What it adds is the half `resolve()` alone
    cannot give: `resolve()` turns a relative value absolute *against the
    current working directory*, so a relative `TELEGRAM_RESEARCH_ENV=telegram.env`
    names a different file in every shell -- and finds a different file in any
    folder that happens to hold one of that name.

    This is the anchor for the credential and for the override file -- paths
    whose natural neighbourhood is the project. `TELEGRAM_RESEARCH_STATE` does NOT
    come through here: it has a stricter anchor of its own,
    `anchored_state_path`. The difference is loudness. A credential path
    resolved against the wrong folder is loud -- the file is not there and the
    error names the path it looked at; a state directory in the wrong folder is
    silent, and silence is what the 36 468 s freeze was made of.

    An environment variable has no file of its own to be relative to, which is
    why the anchor here is the installation. A path written INSIDE a file is a
    different question and gets a different anchor -- see `_vocabulary_path`.
    """
    return _anchored(value, repo_root())


def _state_dir_from_env(state_path: Path) -> Path:
    """The state directory named by the environment, or a sentence saying why not."""
    if state_path.exists():
        if not state_path.is_dir():
            raise ConfigError(
                f"{ENV_STATE} points at {state_path}, which is a file. It names a "
                "DIRECTORY: the registry (sources.jsonl), the ledger "
                "(resolve-ledger.json) and the account lock are files inside it. "
                "Point it at the folder that holds them."
            )
        return state_path
    name = state_path.name
    if name.lower() in _STATE_FILE_NAMES or state_path.suffix.lower() in _STATE_FILE_SUFFIXES:
        raise ConfigError(
            f"{ENV_STATE} points at {state_path}, which names a FILE. It names a "
            "DIRECTORY: the registry (sources.jsonl), the ledger "
            "(resolve-ledger.json) and the account lock are files inside it. "
            f"The file is not there yet, so nothing refused it and a directory "
            f"called {name!r} would have been created with the registry inside it "
            f"at {state_path / 'sources.jsonl'}. You almost certainly mean "
            f"{state_path.parent}."
        )
    return state_path


def load(root: Path | None = None) -> Config:
    """Build the configuration from environment and an optional JSON override.

    `root` names the project root for run folders. It deliberately does NOT
    decide where the state lives: `--root` used to default to `"."`, and the
    state directory following that default is the defect this signature used to
    carry. It defaults to nothing now (`tg.root_arg` returns None or an absolute
    path), so `root=None` anchors run folders on `repo_root()` too. State comes
    from `TELEGRAM_RESEARCH_STATE`, or from `~/.telegram-research`; from nowhere
    else.
    """
    # Every path out of the environment goes through `env_path`, which anchors
    # it: a relative `TELEGRAM_RESEARCH_STATE=state/_telegram` used to be taken
    # verbatim, so a run in one directory wrote the freeze and a run in another
    # read `frozen_for() == 0`, kept its own ledger and took a DIFFERENT
    # `account.lock`, which is both safety rules failing at once. `~/tg-state`
    # was worse: a literal directory named `~`. State takes the stricter of the
    # two anchors, the home directory, so that it cannot follow the shell even
    # where the project root IS the shell -- see `anchored_state_path`.
    #
    # It is resolved BEFORE the `Config` is built, and not through the field
    # default, so that a machine which cannot say where home is still gets the
    # directory the variable names instead of the error about home.
    state = os.environ.get(ENV_STATE)
    cfg = Config(state_dir=(_state_dir_from_env(anchored_state_path(state))
                            if state else default_state_dir()))
    cfg.root = Path(root) if root else repo_root()

    cred = os.environ.get(ENV_CREDENTIAL)
    cfg.credential_path = anchored_env_path(cred) if cred else None

    override = os.environ.get(ENV_CONFIG)
    if override:
        _apply_override(cfg, anchored_env_path(override))
    if cfg.topics_vocabulary is None:
        default_vocab = skill_root() / "references" / "topics.json"
        if default_vocab.exists():
            cfg.topics_vocabulary = default_vocab
    _announce_override_notes(cfg)
    return cfg


# Notes already shown in this process. A clamp is announced once, not once per
# `load()` -- several commands in one process would otherwise repeat it.
_ANNOUNCED: set[str] = set()


def _announce_override_notes(cfg: Config) -> None:
    """Say on stderr that a configured value was refused and clamped.

    The clamp itself is right and is not the finding. The finding is that the
    explanation went into `Config.override_notes`, whose only reader is
    `Config.as_dict()`, which `tg.py` calls from nowhere -- so an operator who
    set `daily_resolve_ceiling: 1000` and planned a session around it was
    clamped to 180 with no word anywhere, and the run failed at 18 % with a
    "ceiling reached" that contradicted the file on disk.

    stderr, not stdout: every subcommand's stdout is JSON an agent parses, and
    a warning belongs where it cannot corrupt that.
    """
    for note in cfg.override_notes:
        if note in _ANNOUNCED:
            continue
        _ANNOUNCED.add(note)
        print(f"{ENV_CONFIG}: {note}", file=sys.stderr)


# Every top-level key an override file may carry. Anything else is a typo.
_OVERRIDE_KEYS = frozenset({"budgets", "topics_vocabulary"})


def _apply_override(cfg: Config, path: Path) -> None:
    """Read the override file, validating every value it tries to set.

    Before this, `setattr` accepted anything of any type: a `null` reached
    `check_resolve` and raised `TypeError` inside the very comparison that is
    supposed to refuse the call, and a malformed file raised a raw
    `JSONDecodeError` rather than the `ConfigError` this module promises.
    """
    if not path.exists():
        raise ConfigError(f"{ENV_CONFIG} points at {path}, which does not exist")
    try:
        # `utf-8-sig` eats a BOM if there is one and behaves as plain utf-8 if
        # there is not. Notepad and `Set-Content -Encoding UTF8` under Windows
        # PowerShell 5.1 both write one by default on this machine, and a BOM
        # made the whole file "not valid JSON" -- every budget silently back at
        # its shipped default.
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        # `exists()` is true for a DIRECTORY, and `read_text` on one raises
        # PermissionError / IsADirectoryError -- neither a ValueError nor a
        # ConfigError, so pointing the variable at the folder that holds the
        # JSON instead of at the JSON produced a traceback.
        raise ConfigError(
            f"{ENV_CONFIG} points at {path}, which could not be read ({exc}). "
            "It names the override FILE itself, not the folder holding it."
        ) from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{ENV_CONFIG} points at {path}, which is not valid JSON: {exc}"
        ) from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a JSON object, not {type(data).__name__}")

    # A misspelled BUDGET has always been a loud error naming every real budget.
    # A misspelled CONTAINER was not: `{"budget": {...}}`, `{"Budgets": {...}}`
    # or any other spelling of the top level was read, accepted and did nothing,
    # and the operator believed all three depth rows had moved when none had.
    unknown = sorted(k for k in data if k not in _OVERRIDE_KEYS)
    if unknown:
        raise ConfigError(
            f"{path}: {', '.join(repr(k) for k in unknown)} "
            f"{'is not a key' if len(unknown) == 1 else 'are not keys'} this skill "
            f"reads. Known top-level keys: {', '.join(sorted(_OVERRIDE_KEYS))}. "
            "A misspelled container was accepted in silence and changed nothing."
        )

    budgets = data.get("budgets") or {}
    if not isinstance(budgets, dict):
        raise ConfigError(f"{path}: 'budgets' must be a JSON object")

    known = {f.name: f for f in fields(Budgets)}
    shipped = Budgets()
    for key, value in budgets.items():
        if key not in known:
            raise ConfigError(
                f"{path}: 'budgets.{key}' is not a budget this skill has. "
                f"Known budgets: {', '.join(sorted(known))}."
            )
        default = getattr(shipped, key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(
                f"{path}: 'budgets.{key}' must be a number, not "
                f"{type(value).__name__} ({value!r}). A value of the wrong type "
                "used to reach the ceiling check itself and fail there."
            )
        try:
            # Through the one shared reader: `json.loads` accepts `NaN`,
            # `Infinity` and `-Infinity`, all three pass `isinstance(x, float)`,
            # and `NaN < 0` is False -- so a NaN budget walked straight through
            # the negativity check onto the dataclass, where it makes every
            # comparison in every ceiling false.
            value = want_finite_number(budgets, key, default)
        except ValueError:
            raise ConfigError(
                f"{path}: 'budgets.{key}' is {value!r}, which is not a finite "
                "number. NaN makes every comparison false, so a ceiling holding "
                "one refuses nothing at all."
            ) from None
        if isinstance(default, int) and not isinstance(value, int):
            if float(value).is_integer():
                value = int(value)
            else:
                raise ConfigError(f"{path}: 'budgets.{key}' must be a whole number")
        if value < 0:
            raise ConfigError(f"{path}: 'budgets.{key}' must not be negative")

        if key in _CEILINGS_MAY_ONLY_FALL and value > default:
            cfg.override_notes.append(
                f"budgets.{key}={value} was refused and clamped to the shipped "
                f"ceiling {default}: account ceilings may be lowered, never raised."
            )
            value = default
        elif key in _GAPS_MAY_ONLY_RISE and value < default:
            cfg.override_notes.append(
                f"budgets.{key}={value} was refused and clamped to the shipped "
                f"minimum {default}: account gaps may be widened, never narrowed."
            )
            value = default
        setattr(cfg.budgets, key, value)

    vocab = data.get("topics_vocabulary")
    if vocab:
        if not isinstance(vocab, str):
            raise ConfigError(f"{path}: 'topics_vocabulary' must be a path string")
        vocab_path = _vocabulary_path(vocab, path)
        # A typo here used to cost BOTH the override and the shipped default:
        # `load()` only falls back to `references/topics.json` when
        # `topics_vocabulary` is still None, and `tg.py` returns None for a path
        # that does not exist. Every source admitted after that carried no
        # topics at all, with nothing anywhere saying the vocabulary was missing.
        if not vocab_path.exists():
            raise ConfigError(
                f"{path}: 'topics_vocabulary' names {vocab!r}, and this skill "
                f"looked for it at {vocab_path}, which does not exist. A relative "
                f"path in an override file is read relative to the file itself, "
                f"so that is {path.parent}; write an absolute path to name a file "
                "anywhere else. A vocabulary that cannot be read is not a smaller "
                "vocabulary — it silently switches classification off, and the "
                "shipped default is not used as a fallback for a path you named."
            )
        cfg.topics_vocabulary = vocab_path


def _vocabulary_path(value: str, config_path: Path) -> Path:
    """`topics_vocabulary` from an override file, anchored on THAT FILE.

    The anchor is the override file's own directory. A relative path written
    inside a configuration file is that file's statement about its own
    neighbourhood -- which is how configuration formats resolve one, and what
    an operator writing
    `topics/ru.json` means -- and it keeps a config portable together with the
    vocabulary it names. `repo_root()` was the other candidate, for consistency
    with `anchored_env_path`, and it loses on the case that will happen: an
    override kept outside the repository naming `topics/ru.json` would then
    point INSIDE the repository, where a file of that name may exist and be a
    different vocabulary. The file anchor's worst case is a loud "not found";
    the repo anchor's worst case is a silent wrong classification of everything
    the run admits.

    Measured before the repair, with `.exists()` against the shell: one override
    file resolved to the operator's vocabulary from one directory, to a
    ConfigError from another, and to a DECOY `topics/ru.json` sitting in a third
    -- loaded in silence, keys and all.
    """
    return _anchored(value, config_path.parent)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------
def read_credentials(cfg: Config) -> dict:
    """Read the Telegram credential, or fail with an instruction.

    Two behaviours are deliberate and neither is negotiable:

    * **It never searches the disk.** A tool that goes looking for credentials
      will eventually find the wrong ones, and a tool that finds credentials the
      operator did not point it at is a tool nobody can reason about.
    * **It fails loudly and specifically.** "No credential" is not an error the
      caller should be able to shrug off into a half-run.

    There are two sources, tried in this order:

    1. the three variables in the process environment -- **all three or none**;
    2. the file named by `TELEGRAM_RESEARCH_ENV`.

    The environment comes first because it leaves no file to be committed,
    synced or copied by accident. A *partial* environment is deliberately not
    used and never merged with the file: half a credential from one place and
    half from another is precisely the configuration nobody can reason about.
    Reading named variables is not searching the disk, so the rule above stands
    unchanged.
    """
    keys = ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION")
    from_env = {k: (os.environ.get(k) or "").strip() for k in keys}
    if all(from_env.values()):
        return from_env
    partial = [k for k in keys if from_env[k]]
    partial_note = (
        "\nSet in the environment, but not all three, so the environment was "
        f"ignored: {', '.join(partial)}."
        if partial else ""
    )

    if not cfg.credential_path:
        raise ConfigError(
            f"{ENV_CREDENTIAL} is not set and the environment does not hold all "
            "three of TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION, "
            "so there is no Telegram credential.\n"
            "Either set those three variables, or point "
            f"{ENV_CREDENTIAL} at the file holding them."
            f"{partial_note}\n"
            f"{CREDENTIAL_NOTICE}\n"
            "Nothing is searched for on disk, by design."
        )
    path = Path(cfg.credential_path)
    if not path.exists():
        raise ConfigError(
            f"{ENV_CREDENTIAL} points at {path}, which does not exist. "
            "The path is not guessed and no fallback is attempted."
        )
    try:
        # `utf-8-sig`, for the reason the override file has it -- and here the
        # consequence was worse than a loud failure: the BOM welded onto the
        # first key, so `values` held a key whose name starts with U+FEFF and the operator
        # was told the file "is missing TELEGRAM_API_ID" about a file that has
        # it on line 1. Live mode unreachable, and the message pointing at the
        # wrong thing.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ConfigError(
            f"{ENV_CREDENTIAL} points at {path}, which could not be read ({exc}). "
            "It names the credential FILE itself, not the folder holding it."
        ) from None
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")

    missing = [k for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION")
               if not values.get(k)]
    if missing:
        raise ConfigError(
            f"{path} is missing {', '.join(missing)}. "
            "The file was read but is not usable; nothing else was consulted."
            f"{partial_note}"
        )
    return values


# --------------------------------------------------------------------------
# Secret hygiene
# --------------------------------------------------------------------------
# A StringSession is a long opaque token. The patterns below are deliberately
# broad: it is better to redact a harmless long string in a log than to let one
# session string reach a report, a fetch log or a knowledge base. The rule the
# spec sets is that the key appears in none of them, and it is checked by a test
# rather than by care.
#
# What these patterns do NOT do, and will not pretend to do: recognise a bare
# `api_id`. It is a six-to-eight digit integer, indistinguishable from a member
# count, a message id or a year. A pattern broad enough to catch it would redact
# most of the corpus. `api_id` is covered by its KEY forms only -- `api_id=...`,
# `api_id: ...`, and the dict key in `SECRET_KEYS` -- and the only real defence
# is never letting it into a string.
_KEYED_SECRET = (
    r"(?i)\b((?:telegram_)?(?:api_hash|api_id|session|string_session|bot_token))"
    r"([\"']?\s*[:=]\s*)[\"']?[^\s\"',;}\]]+"
)
_SECRET_PATTERNS = [
    re.compile(_KEYED_SECRET),
    # Telethon StringSession shape: version byte '1' plus base64 of the packed
    # dc id, ip, port and 256-byte auth key -- around 350 characters in practice,
    # never fewer than 200. The first version of this pattern asked for 40, which
    # is the length of an ordinary URL path segment: it redacted the middle of
    # `bloomberg.com/news/articles/2026-05-14/...` and of `vc.ru/media/1234567-...`
    # inside fetched corpus text, which is the corruption `run.py` argues against
    # at length. A session string is very long; asking for it is free.
    re.compile(r"\b1[A-Za-z0-9_\-+/=]{200,}\b"),
    re.compile(r"(?i)\b[0-9a-f]{32}\b"),                 # api_hash shape, either case
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),     # bot token shape
]


def _blank(match) -> str:
    """Keep the key and its separator, drop the value. `api_id": 1` stays legible."""
    groups = match.groups()
    if len(groups) >= 2:
        return groups[0] + groups[1] + "<redacted>"
    if groups:
        return groups[0] + "=<redacted>"
    return "<redacted>"


def redact(text: str) -> str:
    """Blank anything that looks like a credential. Used on every outbound path."""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(_blank, out)
    return out


def redact_obj(obj, *, protect=()):
    """`redact` over a nested structure, keys included.

    `protect` is a set of key names whose values pass through untouched, all the
    way down: fetched Telegram content is not ours to rewrite, and a 32-hex
    string inside a post is a post, not our api_hash. It propagates recursively,
    so `{"results": [{"text": ...}]}` protects the text inside the list too.

    A key that is in BOTH `protect` and `SECRET_KEYS` is redacted. Protection
    never wins over a credential name; that is the fail-closed direction.
    """
    protect = _protect_set(protect)
    return _redact_obj(obj, protect)


def _protect_set(protect) -> frozenset:
    if not protect:
        return frozenset()
    if isinstance(protect, str):
        protect = (protect,)
    return frozenset(str(k) for k in protect)


def _redact_obj(obj, protect: frozenset):
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            name = k if isinstance(k, str) else str(k)
            if name.upper() in SECRET_KEYS:
                out[k] = "<redacted>"
            elif name in protect:
                out[k] = v                       # untouched, and not walked into
            else:
                out[k] = _redact_obj(v, protect)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(v, protect) for v in obj]
    return obj
