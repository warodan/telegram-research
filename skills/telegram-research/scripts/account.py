"""The MTProto fallback: the only path in this skill that spends the account.

It is reached for exactly one job -- bulk history of a GROUP. A group has no
`/s/` preview (both a group and a nonexistent name answer 302 to `t.me/<name>`,
indistinguishably), so the free route costs one GET per message through
`t.me/<name>/<id>?embed=1`. A CHANNEL never arrives here: `/s/<name>` serves 20
messages a page for free and `?q=` searches its whole history server side.

Nothing in this file has ever reached Telegram, and that is deliberate: this
skill does not install Telethon and your account is not touched -- not a login,
not a resolve, not a "quick check". What is testable without an account
is every decision the module makes before a byte reaches Telegram: the evidence
rule, the ledger, the freeze latch, the peer cache, the lock, the paid-call
block, and the real transport as well, driven against a stubbed `telethon`
module. All of it runs against a fake, which is why the transport is a seam
rather than a detail.

**Two switches, plus a third layer that lasts only while Telethon is absent.**
Live mode needs `allow_live=True` in code AND `TELEGRAM_RESEARCH_ALLOW_LIVE` in
the environment, where only a value that actually says yes counts (`env_flag`).
Telethon is imported at the moment of use, so where it is not installed the live
path additionally dies at `TelethonMissing` -- one line past the switch check,
with nothing on the wire. That third layer is a property of the machine and not
of this module: anything that pip-installs Telethon into the same interpreter
removes it without telling anyone, so no test may assume it and no rule here may
lean on it.

The incident the safety rules are written against, measured on 2026-08-20:
sixteen `contacts.resolveUsername` calls in under seven minutes bought a wait of
36 468 seconds, and all sixteen returned success while the account was already
dead. `resolve.py` holds the accounting; this file holds the calls that spend it.

Dependency, as of 2026-08-23:

* The pin is `telethon==1.44.0`, the latest on PyPI, uploaded 2026-06-15.
* `github.com/LonamiWebs/Telethon` is ARCHIVED and points at
  `https://codeberg.org/Lonami/Telethon`, which is alive: newest commit
  2026-08-23, default branch `v1`, 16 commits in the last 90 days.
* Only the 1.x line is ever published. A v2 rewrite exists on a branch and has
  never been released, so "Telethon v2" is not something to depend on.
* The package declares `requires_python >= 3.5` and its classifiers stop at 3.8.
  Python 3.14 support is therefore NOT ESTABLISHED -- not refuted either, simply
  unclaimed by the publisher, which makes installing it on 3.14 an experiment in
  itself: the module imports there, and whether it works against Telegram is
  unanswered, because nothing in this file has ever called it.
"""

from __future__ import annotations

import inspect
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Protocol, runtime_checkable

import config as configmod
import resolve as resolvemod
from resolve import (
    AccountBusy,
    AccountLock,
    BudgetExhausted,
    ResolveFrozen,
    ResolveLedger,
    peer_is_usable,
    session_fingerprint,
)

# `AccountBusy` is imported to be re-exported: a caller of this module handles
# "another process holds the account" and should not have to know that the
# exception is defined one file over.
__all__ = [
    "ALLOW_PAID_STARS",
    "AccountBusy",
    "AccountError",
    "AccountSession",
    "EvidenceRequired",
    "FakeTransport",
    "FloodWait",
    "HistoryLog",
    "HistoryPage",
    "StateUnreadable",
    "StateWriteFailed",
    "LiveModeRefused",
    "PaidCallRefused",
    "PeerNotFound",
    "PeerUnusable",
    "PrepareReport",
    "SourceRequest",
    "TelethonMissing",
    "TelethonTransport",
    "Transport",
    "TransportError",
    "WrongSurface",
    "env_flag",
    "live_enabled_in_env",
]

TELETHON_PIN = "telethon==1.44.0"
TELETHON_SOURCE = "https://codeberg.org/Lonami/Telethon"

# The second switch. `dry_run=False` is not enough on its own: live mode also
# requires this variable in the environment, so no config file, no default
# argument and no forgotten flag in a script can reach the network by itself.
ENV_ALLOW_LIVE = "TELEGRAM_RESEARCH_ALLOW_LIVE"

# The only values that turn a switch ON. Everything else -- unset, empty, "0",
# "false", "no", "off", "disabled", a stray space, a typo -- leaves it off.
# Presence used to be enough, which meant `TELEGRAM_RESEARCH_ALLOW_LIVE=0`, the
# ordinary way to turn a switch off, turned live mode on.
ENV_TRUE_VALUES = ("1", "true", "yes", "on")


def env_flag(value, *, default: bool = False) -> bool:
    """Parse an environment switch. Anything not clearly yes is no.

    Ambiguity resolves toward refusing: this switch guards a real person's
    account, so an unrecognised value is not a reason to act.
    """
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ENV_TRUE_VALUES


def live_enabled_in_env() -> bool:
    """The second switch, read from the REAL environment at the moment of use.

    Not from an argument, and not once at construction. `AccountSession` used to
    take an `env=` mapping, so `AccountSession(..., allow_live=True,
    env={"TELEGRAM_RESEARCH_ALLOW_LIVE": "1"})` reached the transport with the
    variable absent from the environment -- which collapses two independent
    switches into one decision in one file, and the argument for having two is
    that they are independent. The tests drive this by replacing `os.environ`
    wholesale, which is the same seam a real operator uses and one a caller
    cannot reach by passing a keyword.
    """
    return env_flag(os.environ.get(ENV_ALLOW_LIVE))


def _require_live_switches(allow_live: bool, what: str) -> None:
    """Both switches, or nothing goes to Telegram. Checked at the moment of use.

    Live mode is not a state something can be left in: it is re-established at
    every call, so flipping an attribute afterwards buys nothing.
    """
    if not allow_live:
        raise LiveModeRefused(
            f"{what} It needs allow_live=True as well as dry_run=False. The argument "
            "is named after what it does so that no call site can reach the network "
            "without saying the word."
        )
    if not live_enabled_in_env():
        # The value is parsed, not merely looked for. Presence alone used to be
        # enough, so `TELEGRAM_RESEARCH_ALLOW_LIVE=0` -- the ordinary way to turn a
        # switch off -- turned live mode on.
        raise LiveModeRefused(
            f"{what} It also needs {ENV_ALLOW_LIVE} set to one of "
            f"{', '.join(ENV_TRUE_VALUES)} in the environment, and it is "
            f"{os.environ.get(ENV_ALLOW_LIVE)!r}. Two switches, because one of them "
            "is always the one somebody left on."
        )


# Every Telegram error that carries a wait, by name. They are SIBLINGS under
# `FloodError`, not subclasses of `FloodWaitError`: catching only
# `FloodWaitError` misses a `FLOOD_PREMIUM_WAIT_36468` entirely, which is the
# same wait wearing another name. Looked up defensively, because a future pin
# may rename or drop any one of them and this file must still import.
FLOOD_ERROR_NAMES = (
    "FloodWaitError",
    "FloodPremiumWaitError",
    "SlowModeWaitError",
    "FloodTestPhoneWaitError",
    "TakeoutInitDelayError",
)

# A wait we could not read a number out of. Telegram said stop, so we stop; the
# figure is a policy floor chosen to be long enough to break a burst, and it is
# NOT a measurement. The run-local latch is what actually ends the run.
UNKNOWN_FLOOD_WAIT_SEC = 300

# Durable accounting for messages.getHistory: its own file next to the ledger,
# because its budget is not the resolve budget and must not borrow its numbers.
HISTORY_STATE_FILE = "account-history.json"

# Usernames to (id, access_hash), next to the ledger. See `PeerCache`.
PEER_CACHE_FILE = "peers.json"

# Telegram's answers to "that access hash is not yours / not any more". A stale
# hash is the one failure mode a permanent peer cache introduces, so it is named
# rather than left to the generic transport-failure branch: the fix is to look
# the peer up again with one `contacts.search`, which the generic branch cannot
# know. Matched by CLASS NAME because the pin renames these more often than it
# renames anything else, and a rename must not turn a stale hash into an
# unexplained error that stops the run.
PEER_STALE_ERROR_NAMES = (
    "ChannelInvalidError", "ChannelPrivateError", "PeerIdInvalidError",
    "ChatIdInvalidError", "ChannelPublicGroupNaError",
)

# `last_resolve_ts + min_gap`, read back as `slot - last_resolve_ts`, comes out
# a few hundred nanoseconds SHORT of min_gap for every gap that is not a dyadic
# rational: 30.1 gives 30.09999990463257, 0.1 gives 0.09999990463256836. The
# ledger then refuses the very call the pause exists to make legal (30.0, the
# shipped value, happens to be exact, which is why nothing caught it). A
# millisecond of slack costs nothing and removes the whole class of it.
GAP_EPSILON = 0.001

# Never spend Stars. The parameter is not merely defaulted off, it is forced to
# this value after every options merge, so neither a config file nor a
# caller-supplied dict can turn it back on. Passing None is what the free search
# in the official client does; anything else is money leaving the account.
ALLOW_PAID_STARS = None

# What the free surface is allowed to say a name is. Anything else means the
# landing card was not parsed, and an unparsed card is not evidence.
KNOWN_TYPES = ("channel", "group")

# How many `messages.getHistory` calls THIS PROCESS has made. A run is a process
# invocation, not an `AccountSession` object: the counter used to be an instance
# attribute, so opening a session per source multiplied `max_requests_per_run` by
# the number of sources and nothing ever refused.
_PROCESS_HISTORY_REQUESTS = 0

# When the last getHistory left this PROCESS, for the same reason the count is
# per process. The ceiling was lifted to module scope so that a script opening
# one `AccountSession` per source could not multiply it; the pacing timestamp
# was left an instance attribute, so that same script sent the first page of
# every session with no gap at all after the last page of the previous one --
# eight sources, seven of the fifteen inter-call gaps zero. Measured 2026-08-25.
_PROCESS_LAST_HISTORY_TS = 0.0


def history_requests_this_process() -> int:
    """getHistory calls made since this process started."""
    return _PROCESS_HISTORY_REQUESTS


def last_history_ts_this_process() -> float:
    """When the last getHistory of this process left, 0.0 if none has."""
    return _PROCESS_LAST_HISTORY_TS


def reset_history_requests_this_process() -> None:
    """Start the run's history count over. A fresh process is the only other way.

    For tests, and for a caller that genuinely is starting a new run inside one
    long-lived process; both are deliberate acts, which is why it is a function
    with a name rather than an attribute anybody can assign. It resets the
    pacing timestamp with the count: they describe the same run.
    """
    global _PROCESS_HISTORY_REQUESTS, _PROCESS_LAST_HISTORY_TS
    _PROCESS_HISTORY_REQUESTS = 0
    _PROCESS_LAST_HISTORY_TS = 0.0


# --------------------------------------------------------------------------
# Errors. Every message is redacted on the way in, and every `except Exception`
# in this file wraps what it caught in one of these types with `from None`, so
# no branch forwards a message -- or a traceback, or a chained cause -- that it
# did not write. The one thing deliberately passed through untouched is
# `BaseException`: KeyboardInterrupt and CancelledError are control flow, and
# Ctrl-C has to keep working.
# --------------------------------------------------------------------------
class AccountError(RuntimeError):
    """Base for everything this module raises. Redacts its own message.

    Redaction happens in the constructor rather than at each raise site because
    the rule has to hold at the raise site nobody remembers to check.
    """

    def __init__(self, *args):
        super().__init__(*[configmod.redact(a) if isinstance(a, str) else a for a in args])


class TelethonMissing(AccountError):
    """Telethon is not importable. This skill does not install it."""


class EvidenceRequired(AccountError):
    """A resolve was asked for without free-surface evidence that the name exists."""


class PaidCallRefused(AccountError):
    """A call arrived carrying a paid parameter. It is not sent."""


class LiveModeRefused(AccountError):
    """Live mode was asked for without both switches."""


class PeerNotFound(AccountError):
    """Telegram says the name resolves to nothing."""


class PeerUnusable(AccountError):
    """The cached peer belongs to another login session, or is incomplete."""


class WrongSurface(AccountError):
    """This is the account path. The free web surface answers this one."""


class TransportError(AccountError):
    """The transport failed for a reason that is not one of the above."""


class StateUnreadable(AccountError):
    """A durable state file exists but cannot be understood.

    Fail closed: a file we cannot read is not permission to act. The alternative
    -- treating an unreadable file as "nothing spent, not frozen" -- turns every
    corrupt byte into a licence to call Telegram again.
    """


class StateWriteFailed(AccountError):
    """A durable state file could not be written. Nothing may proceed as if it was.

    The twin of `resolve.LedgerWriteFailed`, for the history state. It exists
    because the alternative was a raw `PermissionError [WinError 5]` escaping a
    module whose contract is that every exception it raises is one of its own
    redacted types -- and because a freeze the caller believes was recorded is
    worse than one that failed loudly.
    """


class FloodWait(AccountError):
    """Telegram asked for a wait. Transport-neutral twin of `FloodWaitError`.

    Carried as our own type so that nothing above the seam has to import
    Telethon to catch it, which is what makes the flood rules testable here.
    """

    def __init__(self, seconds: float, where: str = "", *, error_name: str = ""):
        self.seconds = int(seconds)
        self.error_name = error_name
        named = f" ({error_name})" if error_name else ""
        super().__init__(
            f"Telegram asked for a wait of {self.seconds} s on {where or 'an account call'}"
            f"{named}. The first one stops the run; retrying is what extends the ban."
        )


def wait_seconds_of(exc) -> int:
    """Read the wait out of any Telegram error that carries one.

    All five siblings expose `.seconds`; a 420 that does not is still Telegram
    saying stop, and an unreadable number is not a reason to keep calling.
    """
    seconds = getattr(exc, "seconds", None)
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 0
    return seconds if seconds > 0 else UNKNOWN_FLOOD_WAIT_SEC


# --------------------------------------------------------------------------
# The paid-call block
# --------------------------------------------------------------------------
def free_call_options(*layers: dict | None) -> dict:
    """Merge whatever the caller and the config say, then force the free value.

    The order is the whole point: `allow_paid_stars` is written LAST, after every
    layer has had its say, so a config file that sets it true changes nothing.
    Spending has to become a code edit, and a code edit is a diff somebody reads.
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer:
            merged.update(layer)
    merged["allow_paid_stars"] = ALLOW_PAID_STARS
    return merged


def _assert_free(options: dict | None) -> dict:
    """Second gate, at the transport boundary, for callers that skipped the first.

    `free_call_options` cannot be defeated by editing a config file; this catches
    the other route, a hand-built options dict handed straight to a transport.
    """
    opts = dict(options or {})
    if opts.get("allow_paid_stars") is not None:
        raise PaidCallRefused(
            "a call arrived with allow_paid_stars set. This skill never spends "
            "Stars, and the parameter is forced to None in free_call_options(). "
            "The call was not sent."
        )
    opts["allow_paid_stars"] = ALLOW_PAID_STARS
    return opts


# --------------------------------------------------------------------------
# The seam
# --------------------------------------------------------------------------
@runtime_checkable
class Transport(Protocol):
    """The operations the reading path is allowed to perform.

    Four, and no more. Joining a group is NOT part of this protocol: a reading
    path cannot call an operation the protocol does not carry, so "reading never
    joins" is enforced by the shape of the seam rather than by remembering.
    `AccountSession.join_group` looks the capability up on the transport by name,
    and that lookup exists in exactly one method.

    The two search operations are why `resolve_username` is no longer on the
    ordinary path. `contacts.search` answers with the peer AND its access_hash in
    one response, so the key that used to cost a resolve now arrives as a
    by-product of the search that had to happen anyway. Measured: 8 calls, 0
    resolves, no wait. `resolve_username` stays as the fallback for a name the
    search box will not return.
    """

    def resolve_username(self, username: str, *, options: dict | None = None) -> dict:
        """Return `{"id": int, "access_hash": int}` or raise PeerNotFound/FloodWait."""
        ...

    def fetch_history(self, peer: dict, *, limit: int = 100, offset_id: int = 0,
                      options: dict | None = None) -> list[dict]:
        """Return up to `limit` message records older than `offset_id`."""
        ...

    def search_contacts(self, query: str, *, limit: int = 50,
                        options: dict | None = None) -> list[dict]:
        """Peer records from the app's own search box: TITLES and usernames only.

        It never sees inside a message, which is why stage 2 runs it beside web
        search and lyzem rather than instead of them. `references/account.md`.
        """
        ...

    def search_messages(self, peer: dict, query: str, *, limit: int = 50,
                        add_offset: int = 0, options: dict | None = None) -> dict:
        """Return `{"messages": [...], "total": int}` for one query inside one peer."""
        ...


# Said in one place because two of them refuse the same call: the transport, on
# its way out, and `join_group`, BEFORE it charges the daily ceiling of 3 for a
# call that would never leave this machine.
PEER_NOT_NUMERIC = (
    "a peer reached the transport without a numeric id and access_hash. "
    "Telethon resolves a string peer by calling contacts.resolveUsername, "
    "which would spend the account outside the ledger. The call was not sent."
)


def peer_is_numeric(peer) -> bool:
    """Are the id and the access_hash numbers Telethon will not try to resolve?

    `resolve.peer_is_usable` tests both fields for TRUTH only, never for type,
    so a registry record holding `"id": "111"` / `"access_hash": "99"` -- a
    hand-edited file, or a JSON round trip through a stringifying writer --
    passes that gate, reaches the transport and is refused there. That refusal
    happens on this machine with nothing on the wire, so anything that charged
    a ceiling before the call charged it for a call that never happened.
    """
    values = ((peer or {}).get("id"), (peer or {}).get("access_hash"))
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
    return bool(values[1])


def _flood_error_types(errors) -> tuple:
    """The wait-carrying error classes this pin actually has, as a catch tuple.

    Looked up by name and skipped when absent: `except ()` catches nothing,
    which is why the generic branch of every transport method also asks
    `_is_wait_error`. A renamed class must not turn a flood into a silent retry.
    """
    found = []
    for name in FLOOD_ERROR_NAMES:
        cls = getattr(errors, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            found.append(cls)
    return tuple(found)


def _is_wait_error(tl: SimpleNamespace, exc: BaseException) -> bool:
    """Last net: any 420-family error carrying a wait, whatever it is called.

    `FloodError` is the base of all of them (`rpcbaseerrors.py`), so a sibling
    this pin adds -- `TwoFaConfirmWaitError` is already one such -- is still
    read as "Telegram said wait" rather than as an unknown transport failure.
    """
    if isinstance(exc, tuple(t for t in getattr(tl, "flood_errors", ()) if isinstance(t, type))):
        return True
    base = getattr(tl, "flood_base", None)
    if isinstance(base, type) and isinstance(exc, base):
        return True
    return False


def telethon_installed() -> bool:
    """Is Telethon importable, asked WITHOUT importing it.

    While it is absent nothing here can reach Telegram, whatever the two switches
    say; installing it -- for this skill, or for anything else sharing the
    interpreter -- removes that barrier without a word. It is a property of the
    machine rather than a switch this skill owns, which is why the answer belongs
    in the status output. `find_spec` looks the module up on the path and executes
    none of it.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("telethon") is not None
    except (ImportError, ValueError):
        return False


def _import_telethon() -> SimpleNamespace:
    """Import Telethon at the moment of use, never at module import.

    The rest of the skill is stdlib-only and must keep working on a machine that
    has no Telethon at all. Importing at module scope would make `import
    account` fail there and take the free 95% of the skill down with the 5% that
    needs an account.
    """
    try:
        import telethon
        from telethon import TelegramClient, errors
        from telethon.sessions import StringSession
        from telethon.tl import functions, types
    except ImportError:
        raise TelethonMissing(
            f"{TELETHON_PIN} is not installed, and this skill does not install it. "
            f"Install it yourself if you want the account path: pip install {TELETHON_PIN}. "
            f"Upstream moved: github.com/LonamiWebs/Telethon is archived and the live "
            f"repository is {TELETHON_SOURCE}. The package claims no support for Python "
            f"3.14 (its classifiers stop at 3.8), so that install is itself the experiment. "
            f"Everything except bulk group history works without it."
        ) from None
    return SimpleNamespace(
        telethon=telethon,
        TelegramClient=TelegramClient,
        StringSession=StringSession,
        errors=errors,
        functions=functions,
        types=types,
        flood_errors=_flood_error_types(errors),
        flood_base=getattr(errors, "FloodError", None),
    )


def _current_event_loop():
    """This thread's current event loop, or None. Never creates one.

    Python 3.14 raises rather than conjuring a loop, which is the answer we
    want: "there was none, put none back".
    """
    import asyncio
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return asyncio.get_event_loop()
    except Exception:
        return None


# Telethon is constructed with this skill's policy, not its own. Measured
# against the pin: `flood_sleep_threshold=60`, `request_retries=5`,
# `connection_retries=5`, `auto_reconnect=True`, `receive_updates=True`. Under
# those defaults
# `telethon/client/users.py:69-124` answers a `FLOOD_WAIT_17` on a resolve by
# sleeping 17 s and sending `contacts.resolveUsername` again, up to five times,
# entirely inside `self._client(...)`. The ledger would charge one resolve for
# five wire calls and never see a wait -- the 2026-08-20 signature exactly, when
# all sixteen calls "succeeded" on an account that was already dead. Every wait
# and every retry decision belongs to this file and to the ledger.
TELETHON_POLICY = {
    "flood_sleep_threshold": 0,      # never sleep a wait off inside Telethon
    "request_retries": 1,            # one wire call per call the ledger charges
    "connection_retries": 1,         # reconnecting is a decision we take, not a default
    "auto_reconnect": False,         # ... and it is never taken behind our back
    "receive_updates": False,        # nothing here reads updates; keep that socket shut
}


class TelethonTransport(Transport):
    """The real transport, constructed with this skill's policy, not Telethon's.

    Synchronous on the outside because everything else in the skill is (tgweb is
    urllib), and Telethon is async on the inside, so this owns a private event
    loop rather than infecting the rest of the skill with one.

    The tests drive this class against a stubbed `telethon` module, because a
    class nothing constructs is a class where a mutation putting the live session
    string into `__repr__` leaves the suite green.
    """

    def __init__(self, api_id, api_hash, session_string: str, *, timeout: float = 30.0,
                 allow_live: bool = False):
        # The session string is held only long enough to hand to StringSession.
        # It is never logged, never returned and never put in an exception: see
        # `__repr__`, and see `AccountError`, which redacts every message.
        try:
            self._api_id = int(api_id)
        except (TypeError, ValueError):
            # The offending value is NOT repeated. `int()`'s own message quotes
            # what it was handed, and the thing most likely to be in this
            # position by mistake is the api_hash: the two are adjacent strings
            # out of the same dict, and a bare `ValueError` is not an
            # `AccountError`, so nothing redacts it on the way to the traceback.
            raise TransportError(
                "the api_id is not a number. Check that TELEGRAM_API_ID and "
                "TELEGRAM_API_HASH have not been swapped — the value is not "
                "repeated here, because one of the two is a credential."
            ) from None
        self._api_hash = str(api_hash)
        self._session_string = str(session_string)
        self._timeout = timeout
        self._allow_live = bool(allow_live)
        self._tl: SimpleNamespace | None = None
        self._client = None
        self._loop = None
        self._previous_loop = None
        # Whether `connect()` actually made our private loop this thread's
        # current one. `close()` restores the previous loop only if it does,
        # because restoring `None` on an instance that never connected clears
        # the CALLER's loop -- and `AccountSession.__exit__` closes the transport
        # unconditionally, failed connection included.
        self._loop_swapped = False

    def __repr__(self) -> str:
        # No credential, ever: not the session string, not the api_hash, not the
        # api_id. A repr lands in tracebacks, in logs and in pytest output, and
        # this one is the reason none of them can carry the login.
        return f"<TelethonTransport connected={self._client is not None}>"

    @property
    def connected(self) -> bool:
        """Can this transport make a call at all? Asked BEFORE the budget is spent.

        `_require()` answers the same question from inside the call, which is
        after the resolve has been counted on disk -- eight sources against a
        transport nobody connected used to exhaust the burst ceiling and arm the
        30 s gap latch with zero packets sent.
        """
        return self._client is not None and self._tl is not None

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> "TelethonTransport":
        """Open the MTProto connection. Both switches, because this IS the wire.

        `connect()` performs the handshake and asks Telegram whether the session
        is authorised: two real calls on the account. It used to be reachable
        with no switch at all, so four lines of the module's own public API --
        read the credential, construct, connect, resolve -- spent the account
        outside the lock and outside the ledger.
        """
        import asyncio

        _require_live_switches(
            self._allow_live,
            "connecting a TelethonTransport is a wire call: it performs the MTProto "
            "handshake and asks Telegram whether the session is authorised.",
        )
        tl = _import_telethon()
        self._tl = tl
        self._loop = asyncio.new_event_loop()
        # Telethon reaches for `asyncio.get_event_loop()` whenever it is called
        # from outside a running loop -- `helpers.get_running_loop()` -- so the
        # private loop is made this thread's current one for as long as we hold
        # it. Without that, `disconnect()` runs on a different loop from the one
        # the connection lives on.
        self._previous_loop = _current_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_swapped = True
        try:
            self._client = tl.TelegramClient(
                tl.StringSession(self._session_string), self._api_id, self._api_hash,
                timeout=self._timeout, **TELETHON_POLICY,
            )
            self._loop.run_until_complete(self._client.connect())
            authorized = self._loop.run_until_complete(self._client.is_user_authorized())
        except AccountError:
            self._close_quietly()
            raise
        except Exception as exc:
            # `from None`, like everywhere else here: a chained cause can carry
            # the session string and a traceback prints causes.
            self._close_quietly()
            raise TransportError(f"connecting to Telegram failed: {exc}") from None
        if not authorized:
            self._close_quietly()
            raise TransportError(
                "the session string does not authorise a user. Log in with the tool that "
                "owns the credential and re-read it; nothing is logged in from here."
            )
        return self

    def close(self) -> None:
        """Disconnect and drop the loop. Safe to call twice, safe after a failure.

        `disconnect()` returns a coroutine only when the loop is already
        running, which it never is here -- it returns None and does the work
        itself. `run_until_complete(None)` is a TypeError, so the result is run
        only when there is one to run.
        """
        import asyncio

        client, loop = self._client, self._loop
        previous = getattr(self, "_previous_loop", None)
        swapped = getattr(self, "_loop_swapped", False)
        self._client = None
        self._loop = None
        self._tl = None
        self._previous_loop = None
        self._loop_swapped = False
        try:
            try:
                result = client.disconnect() if client is not None else None
            except Exception:
                # It failed to say goodbye. The loop still closes, and the
                # message is not forwarded: it is not one we wrote.
                result = None
            if inspect.isawaitable(result):
                if loop is not None and not loop.is_closed():
                    loop.run_until_complete(result)
                else:                           # nothing can run it: do not leak a warning
                    result.close()
        finally:
            if loop is not None and not loop.is_closed():
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                loop.close()
            if swapped:
                # Only put back what we took. On an instance that never
                # connected -- or on the second `close()`, which is documented as
                # safe -- `previous` is None, and setting that is not "restore",
                # it is "take the caller's loop away".
                asyncio.set_event_loop(previous)

    def _close_quietly(self) -> None:
        """Close on a failure path, where a cleanup error must not mask the cause."""
        try:
            self.close()
        except Exception:
            pass

    def _require(self) -> SimpleNamespace:
        if self._tl is None or self._client is None:
            raise TransportError("transport is not connected; call connect() first")
        return self._tl

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    # -- the two operations ------------------------------------------------
    def resolve_username(self, username: str, *, options: dict | None = None) -> dict:
        options = _assert_free(options)
        tl = self._require()
        where = f"contacts.resolveUsername @{username}"
        try:
            res = self._run(self._client(
                tl.functions.contacts.ResolveUsernameRequest(username=username)
            ))
        except tl.flood_errors as exc:
            # All five wait-carrying errors, not just FloodWaitError: they are
            # siblings under FloodError, and FLOOD_PREMIUM_WAIT_36468 is the
            # same ten-hour wait wearing a different class name.
            raise FloodWait(wait_seconds_of(exc), where,
                            error_name=type(exc).__name__) from None
        except (tl.errors.UsernameNotOccupiedError, tl.errors.UsernameInvalidError, ValueError):
            raise PeerNotFound(f"@{username} does not resolve") from None
        except Exception as exc:
            if _is_wait_error(tl, exc):
                raise FloodWait(wait_seconds_of(exc), where,
                                error_name=type(exc).__name__) from None
            # `from None` on purpose: a chained cause can carry the session
            # string in its own message, and a traceback prints causes.
            raise TransportError(f"{where} failed: {exc}") from None
        for chat in list(getattr(res, "chats", []) or []):
            access_hash = getattr(chat, "access_hash", None)
            if access_hash is not None:
                return {"id": int(chat.id), "access_hash": int(access_hash)}
        raise PeerNotFound(
            f"@{username} resolved to no chat with an access_hash. "
            "A user account is not a source this skill reads."
        )

    def _input_peer(self, tl: SimpleNamespace, peer: dict):
        """Build an InputPeerChannel, and never hand Telethon a string.

        `telethon/client/users.py:44` resolves every request before sending it,
        and `get_input_entity` issues `contacts.ResolveUsernameRequest` for
        anything handed to it as a `str`. An id and an access_hash short-circuit
        that path on its first line. This is the single place the rule can be
        broken, so it is the single place it is checked.
        """
        if not peer_is_numeric(peer):
            raise TransportError(PEER_NOT_NUMERIC)
        peer_id, access_hash = peer.get("id"), peer.get("access_hash")
        # A public @username group is a supergroup, so InputPeerChannel is the
        # right wrapper for both of the peer kinds this skill ever holds.
        return tl.types.InputPeerChannel(
            channel_id=int(peer_id), access_hash=int(access_hash),
        )

    def fetch_history(self, peer: dict, *, limit: int = 100, offset_id: int = 0,
                      options: dict | None = None) -> list[dict]:
        options = _assert_free(options)
        tl = self._require()
        input_peer = self._input_peer(tl, peer)
        try:
            res = self._run(self._client(tl.functions.messages.GetHistoryRequest(
                peer=input_peer, offset_id=int(offset_id), offset_date=None,
                add_offset=0, limit=int(limit), max_id=0, min_id=0, hash=0,
            )))
        except tl.flood_errors as exc:
            raise FloodWait(wait_seconds_of(exc), "messages.getHistory",
                            error_name=type(exc).__name__) from None
        except Exception as exc:
            if _is_wait_error(tl, exc):
                raise FloodWait(wait_seconds_of(exc), "messages.getHistory",
                                error_name=type(exc).__name__) from None
            raise TransportError(f"messages.getHistory failed: {exc}") from None
        # `res.users` and `res.chats` carry the senders of these very messages.
        # A raw GetHistoryRequest never runs Telethon's `_finish_init`, so
        # `msg.sender` is always None on this path and the who was being thrown
        # away with the response.
        senders = _entity_index(getattr(res, "users", None), getattr(res, "chats", None))
        return [_message_record(m, senders)
                for m in list(getattr(res, "messages", []) or [])]

    def search_contacts(self, query: str, *, limit: int = 50,
                        options: dict | None = None) -> list[dict]:
        """`contacts.search` -- the search box, and NOT a resolve.

        The response carries `access_hash` for every chat in it, so one call both
        finds the peer and hands over the key to read it. Where its own rate
        limit is has never been measured, so it is paced and counted like
        getHistory. `references/account.md`.
        """
        options = _assert_free(options)
        tl = self._require()
        where = "contacts.search"
        try:
            res = self._run(self._client(tl.functions.contacts.SearchRequest(
                q=str(query), limit=int(limit),
            )))
        except tl.flood_errors as exc:
            raise FloodWait(wait_seconds_of(exc), where,
                            error_name=type(exc).__name__) from None
        except Exception as exc:
            if _is_wait_error(tl, exc):
                raise FloodWait(wait_seconds_of(exc), where,
                                error_name=type(exc).__name__) from None
            raise TransportError(f"{where} failed: {exc}") from None
        rows = []
        for chat in list(getattr(res, "chats", []) or []):
            row = _peer_record(chat)
            if row is not None:
                rows.append(row)
        return rows

    def search_messages(self, peer: dict, query: str, *, limit: int = 50,
                        add_offset: int = 0, options: dict | None = None) -> dict:
        """`messages.search` -- server-side full-text search inside ONE peer.

        `total` is the server's own count of matches, so the cost of the rest is
        arithmetic rather than a guess: `ceil(total / limit)` calls, `limit`
        capped at 100 by Telegram. Live numbers: `references/surfaces.md`.
        """
        options = _assert_free(options)
        tl = self._require()
        input_peer = self._input_peer(tl, peer)
        where = "messages.search"
        try:
            res = self._run(self._client(tl.functions.messages.SearchRequest(
                peer=input_peer, q=str(query),
                filter=tl.types.InputMessagesFilterEmpty(),
                min_date=0, max_date=0, offset_id=0, add_offset=int(add_offset),
                limit=int(limit), max_id=0, min_id=0, hash=0,
            )))
        except tl.flood_errors as exc:
            raise FloodWait(wait_seconds_of(exc), where,
                            error_name=type(exc).__name__) from None
        except Exception as exc:
            if _is_wait_error(tl, exc):
                raise FloodWait(wait_seconds_of(exc), where,
                                error_name=type(exc).__name__) from None
            if type(exc).__name__ in PEER_STALE_ERROR_NAMES:
                # The one failure a permanent peer cache can cause. Named, so the
                # caller can drop the record and look the peer up again for one
                # `contacts.search` instead of reporting an unexplained failure.
                raise PeerUnusable(
                    f"{where}: Telegram refused this peer ({type(exc).__name__}). "
                    "The cached access_hash is stale or the peer is not reachable "
                    "from this account; look the name up again with contacts.search."
                ) from None
            raise TransportError(f"{where} failed: {exc}") from None
        senders = _entity_index(getattr(res, "users", None), getattr(res, "chats", None))
        rows = [_message_record(m, senders)
                for m in list(getattr(res, "messages", []) or [])]
        # `count` is on the paged result and absent from the small one Telegram
        # answers with when everything fits: then the page IS the total.
        total = getattr(res, "count", None)
        return {"messages": rows,
                "total": int(total) if isinstance(total, int) else len(rows)}

    # -- beyond the protocol: see Transport's docstring --------------------
    def join_group(self, peer: dict, *, options: dict | None = None) -> dict:
        options = _assert_free(options)
        tl = self._require()
        input_peer = self._input_peer(tl, peer)
        try:
            self._run(self._client(
                tl.functions.channels.JoinChannelRequest(channel=input_peer)
            ))
        except tl.flood_errors as exc:
            raise FloodWait(wait_seconds_of(exc), "channels.joinChannel",
                            error_name=type(exc).__name__) from None
        except Exception as exc:
            if _is_wait_error(tl, exc):
                raise FloodWait(wait_seconds_of(exc), "channels.joinChannel",
                                error_name=type(exc).__name__) from None
            raise TransportError(f"channels.joinChannel failed: {exc}") from None
        return {"joined": True, "peer_id": int(peer["id"])}


def _entity_index(users, chats) -> dict:
    """Index the entities that came back with a history page, by every id form.

    Telethon marks ids when it exposes them on a message: a user keeps its id, a
    chat becomes `-id`, a channel becomes `-(1000000000000 + id)`
    (`utils.get_peer_id`). The index carries all three forms so a lookup by
    `msg.sender_id` succeeds whatever the sender was.
    """
    index: dict[int, Any] = {}
    for entity in list(users or []) + list(chats or []):
        raw = getattr(entity, "id", None)
        try:
            raw = int(raw)
        except (TypeError, ValueError):
            continue
        for key in (raw, -raw, -(1000000000000 + raw)):
            index.setdefault(key, entity)
    return index


def _entity_name(entity) -> str | None:
    """A displayable name for a user or a chat, or None if there is neither."""
    if entity is None:
        return None
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [str(p) for p in (getattr(entity, "first_name", None),
                              getattr(entity, "last_name", None)) if p]
    return " ".join(parts) if parts else None


def _peer_record(entity) -> dict | None:
    """A chat from a search response, in the shape the registry already speaks.

    None for anything that cannot be read as a public source: no username, or no
    access_hash. Users are never here -- `search_contacts` reads `res.chats`
    only, because a person is not a source this skill reads.
    """
    access_hash = getattr(entity, "access_hash", None)
    username = getattr(entity, "username", None)
    if not username:
        for alias in (getattr(entity, "usernames", None) or ()):
            name = getattr(alias, "username", None)
            if name:
                username = name
                break
    if not username or access_hash is None:
        return None
    return {
        "username": str(username),
        "id": int(getattr(entity, "id", 0) or 0),
        "access_hash": int(access_hash),
        # `megagroup` is the only field that separates a public group from a
        # channel here, and it lands in the SAME vocabulary the free landing card
        # settles: `verify` reads "N members, M online" for a group and
        # "N subscribers" for a channel. Two surfaces, one word.
        "type": "group" if getattr(entity, "megagroup", False) else "channel",
        "title": getattr(entity, "title", None),
        "participants": getattr(entity, "participants_count", None),
        "verified": bool(getattr(entity, "verified", False)),
        "scam": bool(getattr(entity, "scam", False)),
    }


def _message_record(msg, senders: dict | None = None) -> dict:
    """Telethon message to the flat shape the rest of the skill already speaks.

    Kept deliberately close to `tgparse.Message`: the account path is a fallback
    for one surface, not a second data model.

    `msg.sender` is populated only by Telethon's `get_messages` helpers, never by
    a raw `GetHistoryRequest`, so on this path it is always None and the author
    used to be lost -- for a skill whose output is "what people said". The sender
    is looked up in the entities the same response carried instead.
    """
    sender = getattr(msg, "sender", None)
    sender_id = getattr(msg, "sender_id", None)
    if sender is None and senders and sender_id is not None:
        sender = senders.get(sender_id)
    date = getattr(msg, "date", None)
    return {
        "id": int(getattr(msg, "id", 0) or 0),
        "date": date.isoformat() if date is not None else None,
        "text": getattr(msg, "message", "") or "",
        "author_id": sender_id,
        "author_name": _entity_name(sender),
        "author_username": getattr(sender, "username", None) if sender is not None else None,
        "reply_to_id": getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
        "via": "mtproto",
    }


class FakeTransport(Transport):
    """In-memory transport. Every safety rule in this module is proved through it.

    Scriptable three ways, which is exactly what the rules need: answer with a
    peer, raise a FloodWait of N seconds, raise not-found. It records every call
    it receives, so a test can assert on what was NOT sent, which is the more
    important half here.
    """

    def __init__(self, peers: dict | None = None):
        self.peers: dict[str, dict] = dict(peers or {})
        self.floods: dict[str, int] = {}          # username, "*" for any, "history",
        #                                           "contacts.search", "messages.search"
        self.missing: set[str] = set()
        self.pages: dict[int, list] = {}          # peer id -> message records
        self.contacts: dict[str, list] = {}       # query -> peer records
        self.hits: dict[tuple, dict] = {}         # (peer id, query) -> {messages, total}
        self.stale: set = set()                   # access hashes Telegram refuses
        self.resolve_calls: list[dict] = []
        self.history_calls: list[dict] = []
        self.contacts_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.join_calls: list[dict] = []
        self.closed = False

    # -- scripting ---------------------------------------------------------
    def answer_with(self, username: str, peer_id: int, access_hash: int = 1234567890):
        self.peers[username] = {"id": int(peer_id), "access_hash": int(access_hash)}
        return self

    def flood_on(self, username: str, seconds: int = 36468):
        self.floods[username] = int(seconds)
        return self

    def not_found(self, username: str):
        self.missing.add(username)
        return self

    def with_history(self, peer_id: int, messages: list):
        self.pages[int(peer_id)] = list(messages)
        return self

    def with_contacts(self, query: str, rows: list):
        self.contacts[query] = list(rows)
        return self

    def with_hits(self, peer_id: int, query: str, messages: list, total: int | None = None):
        self.hits[(int(peer_id), query)] = {
            "messages": list(messages),
            "total": len(messages) if total is None else int(total),
        }
        return self

    def stale_peer(self, access_hash: int):
        """Script the one failure a permanent peer cache can cause.

        Keyed on the HASH, not the peer: a fake that refuses the whole peer
        cannot show the repair working.
        """
        self.stale.add(int(access_hash))
        return self

    # -- the two operations ------------------------------------------------
    def resolve_username(self, username: str, *, options: dict | None = None) -> dict:
        options = _assert_free(options)
        self.resolve_calls.append({"username": username, "options": options})
        seconds = self.floods.get(username, self.floods.get("*"))
        if seconds:
            raise FloodWait(seconds, f"contacts.resolveUsername @{username}")
        if username in self.missing or username not in self.peers:
            raise PeerNotFound(f"@{username} does not resolve")
        return dict(self.peers[username])

    def fetch_history(self, peer: dict, *, limit: int = 100, offset_id: int = 0,
                      options: dict | None = None) -> list[dict]:
        options = _assert_free(options)
        self.history_calls.append(
            {"peer": dict(peer), "limit": limit, "offset_id": offset_id, "options": options}
        )
        seconds = self.floods.get("history")
        if seconds:
            raise FloodWait(seconds, "messages.getHistory")
        rows = list(self.pages.get(int(peer.get("id", 0)), []))
        if offset_id:
            rows = [r for r in rows if int(r.get("id", 0)) < int(offset_id)]
        return rows[:limit]

    def search_contacts(self, query: str, *, limit: int = 50,
                        options: dict | None = None) -> list[dict]:
        options = _assert_free(options)
        self.contacts_calls.append({"query": query, "limit": limit, "options": options})
        seconds = self.floods.get("contacts.search")
        if seconds:
            raise FloodWait(seconds, "contacts.search")
        return [dict(row) for row in self.contacts.get(query, [])][:limit]

    def search_messages(self, peer: dict, query: str, *, limit: int = 50,
                        add_offset: int = 0, options: dict | None = None) -> dict:
        options = _assert_free(options)
        self.search_calls.append({"peer": dict(peer), "query": query, "limit": limit,
                                  "add_offset": add_offset, "options": options})
        seconds = self.floods.get("messages.search")
        if seconds:
            raise FloodWait(seconds, "messages.search")
        peer_id = int(peer.get("id", 0))
        if int(peer.get("access_hash", 0)) in self.stale:
            raise PeerUnusable(
                "messages.search: Telegram refused this peer (ChannelInvalidError). "
                "The cached access_hash is stale or the peer is not reachable "
                "from this account; look the name up again with contacts.search."
            )
        found = self.hits.get((peer_id, query), {"messages": [], "total": 0})
        rows = list(found["messages"])[add_offset:add_offset + limit]
        return {"messages": rows, "total": int(found["total"])}

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """The real transport has one, so the stand-in has one: a caller that
        connects and then fails before the session opens must still close it."""
        self.closed = True

    # -- beyond the protocol -----------------------------------------------
    def join_group(self, peer: dict, *, options: dict | None = None) -> dict:
        options = _assert_free(options)
        self.join_calls.append({"peer": dict(peer), "options": options})
        return {"joined": True, "peer_id": int(peer.get("id", 0))}


# --------------------------------------------------------------------------
# History accounting
# --------------------------------------------------------------------------
class HistoryLog:
    """Durable accounting for `messages.getHistory`, in its own file.

    Why not another column in the resolve ledger: that ledger counts the one call
    that has ever cost this account downtime, and every ceiling in it is argued
    from that one incident. History is a different call whose budget nobody has
    measured, and inventing a daily number for it would be inventing a
    measurement. What it does borrow is the lesson: **a wait must outlive the
    process that earned it.** A history FloodWait used to be a run-local
    attribute and nothing else, so the next `AccountSession` -- a retry, the next
    source, the same process one second later -- called getHistory again
    immediately. It also borrows the audit trail: how many pages were pulled
    today is a number the next caller can see.

    Fail closed. A file that exists and does not parse means refuse, never
    "nothing spent, not frozen".

    It borrows the ledger's WRITE discipline too, and for the same measured
    reasons: the cross-process guard around the read-modify-write, the floor that
    a write may never shorten a freeze, the retrying `os.replace`, and the
    shared-mode read. Without them a second writer's stale copy took a
    36 468 s history freeze back to zero, and any other handle open on the file
    -- a second shell asking for status -- turned a freeze into a raw
    `PermissionError [WinError 5]` out of a module that promises its own types.
    """

    def __init__(self, path, guard_timeout: float = 10.0):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # A bare `mkdir` in a constructor was the first thing in the module
            # to touch the disk, so `TELEGRAM_RESEARCH_STATE=Q:\nowhere\state`
            # answered `python scripts/account.py` with a raw
            # `FileNotFoundError [WinError 3]` traceback. Every exception this
            # module raises is one of its own; `status()` calls
            # `Config.ensure_dirs()` first, which turns the same failure into a
            # ConfigError carrying the advice that fits it, and this is the
            # backstop for every other caller.
            raise StateWriteFailed(
                f"the state directory {self.path.parent} could not be created: "
                f"{exc}. Nothing was read and nothing was recorded."
            ) from None
        self.guard_timeout = guard_timeout

    # -- state -------------------------------------------------------------
    @staticmethod
    def _fresh(now: float | None = None) -> dict:
        return {"date": _day_of(now), "requests": 0, "frozen_until": 0.0,
                "frozen_reason": "", "frozen_until_mono": 0.0, "mono_at_freeze": 0.0}

    def _guard(self) -> configmod.FileGuard:
        return configmod.FileGuard(
            self.path.with_name(self.path.name + ".rmw"),
            timeout=self.guard_timeout, stale_after=60.0, label="history state",
        )

    def _guard_or_refuse(self) -> configmod.FileGuard:
        guard = self._guard()
        try:
            guard.acquire()
        except configmod.GuardBusy as exc:
            raise StateWriteFailed(
                f"{exc} Nothing was recorded."
            ) from None
        return guard

    def read(self, now: float | None = None) -> dict:
        try:
            # Shared-mode, like the ledger: an ordinary `read_text` on Windows
            # blocks another process's `os.replace` over the same file, and this
            # is exactly the file another process reads.
            raw = configmod.read_bytes_shared(self.path).decode("utf-8")
        except FileNotFoundError:
            return self._fresh(now)               # no file yet is not a broken file
        except UnicodeDecodeError as exc:
            raise StateUnreadable(
                f"the history state file {self.path} is not UTF-8 ({exc}). Refusing."
            ) from None
        except OSError as exc:
            raise StateUnreadable(
                f"the history state file {self.path} cannot be read ({exc}). "
                "Refusing: an unreadable record of what this account has already "
                "spent is not permission to spend more."
            ) from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise StateUnreadable(
                f"the history state file {self.path} does not parse as JSON. "
                "Refusing rather than assuming an empty budget. Delete it deliberately "
                "if you know the account is not waiting on anything."
            ) from None
        if not isinstance(data, dict):
            raise StateUnreadable(
                f"the history state file {self.path} holds {type(data).__name__}, "
                "not an object. Refusing."
            )
        try:
            # Every number read off disk goes through the one shared check.
            # `json.loads` accepts the literals `Infinity` and `NaN`, both pass
            # `isinstance(x, float)`, and `int(float("inf"))` is an OverflowError
            # while `int(float("nan"))` is a ValueError -- neither an
            # `AccountError`, so `frozen_for()` broke this class's "our own
            # redacted types" contract on a hand-edited file. Worse if it had
            # not raised: NaN makes every comparison false, so a poisoned
            # `frozen_until` reads as "not frozen" while Telegram is still
            # counting.
            state = {
                "date": str(data.get("date", "")),
                "requests": int(configmod.want_finite_number(data, "requests", 0)),
                "frozen_until": float(
                    configmod.want_finite_number(data, "frozen_until", 0.0)),
                "frozen_reason": str(data.get("frozen_reason", "")),
                "frozen_until_mono": float(
                    configmod.want_finite_number(data, "frozen_until_mono", 0.0)),
                "mono_at_freeze": float(
                    configmod.want_finite_number(data, "mono_at_freeze", 0.0)),
            }
        except (TypeError, ValueError) as exc:
            raise StateUnreadable(
                f"the history state file {self.path} has fields this version cannot "
                f"read ({exc}). Refusing rather than guessing what they meant."
            ) from None
        today = _day_of(now)
        if not state["date"]:
            state["date"] = today
        elif state["date"] < today:
            # A new local day resets the daily count and ONLY that. A wait
            # crosses midnight untouched: Telegram's clock is not our calendar.
            #
            # `<`, not `!=`. An inequality is not a rollover test: a file stamped
            # with TOMORROW -- a clock that ran ahead, an NTP correction, a
            # restored snapshot -- had its count zeroed the moment the clock came
            # back. `resolve._roll_day` was repaired for exactly this and this
            # copy of the rule was left behind.
            state["date"] = today
            state["requests"] = 0
        return state

    def write(self, state: dict) -> None:
        """Replace the file, under the guard, without ever shortening a freeze."""
        guard = self._guard_or_refuse()
        try:
            self._write_locked(state)
        finally:
            guard.release()

    def _write_locked(self, state: dict) -> None:
        """Serialise the state. Called with the guard held.

        The freeze floor is enforced here rather than trusted to callers: both
        `freeze` and `record_request` are read-modify-write over the whole state,
        and a `record_request` that had read before a `freeze` landed used to
        write `frozen_until = 0.0` back over it -- measured, 36 467 s -> 0 s.
        """
        try:
            disk = self.read()
        except StateUnreadable:
            disk = None
        if disk is not None:
            if disk["frozen_until"] > state.get("frozen_until", 0.0):
                state["frozen_until"] = disk["frozen_until"]
                state["frozen_reason"] = (
                    state.get("frozen_reason") or disk["frozen_reason"]
                )
            if disk["frozen_until_mono"] > state.get("frozen_until_mono", 0.0):
                state["frozen_until_mono"] = disk["frozen_until_mono"]
                state["mono_at_freeze"] = disk["mono_at_freeze"]
        try:
            configmod.atomic_write_text(
                self.path, json.dumps(state, indent=2, ensure_ascii=False)
            )
        except configmod.AtomicWriteFailed as exc:
            raise StateWriteFailed(
                f"{exc}\nThe history state was NOT written. Treat the wait as "
                "unrecorded and stop rather than retry."
            ) from None
        # A live run refreshes its own lock. History writes this file and never
        # the ledger, and the ledger write was the only heartbeat there was.
        resolvemod.touch_held_locks()

    def _mutate(self, change, now: float | None = None) -> dict:
        """read -> change -> write, all inside one cross-process critical section."""
        guard = self._guard_or_refuse()
        try:
            state = self.read(now)
            change(state)
            self._write_locked(state)
            return state
        finally:
            guard.release()

    # -- decisions ---------------------------------------------------------
    def frozen_for(self, now: float | None = None) -> int:
        """Seconds left on a history wait, 0 when there is none.

        Two deadlines, later one wins, exactly as `ResolveLedger.frozen_for`
        does it: a wall-clock deadline on its own ends the moment the clock jumps
        forward, and an eleven-hour NTP correction ended a ten-hour wait that
        Telegram was still enforcing.
        """
        now = time.time() if now is None else now
        state = self.read(now)
        left = state["frozen_until"] - now
        if state["frozen_until_mono"]:
            mono_now = time.monotonic()
            if mono_now >= state["mono_at_freeze"]:
                # Same boot: the monotonic deadline is authoritative. After a
                # reboot the counter restarts, which is the case this excludes.
                left = max(left, state["frozen_until_mono"] - mono_now)
        return max(0, int(left))

    def freeze(self, seconds: float, reason: str, now: float | None = None) -> dict:
        injected = now is not None
        when = time.time() if now is None else now
        try:
            seconds = float(configmod.want_finite_number({"seconds": seconds},
                                                         "seconds"))
        except ValueError as exc:
            # A wait we cannot turn into a number is still Telegram saying stop.
            # `float("nan")` used to land on disk, and NaN makes every
            # comparison false: `frozen_for` would then read the freeze as over.
            raise StateWriteFailed(
                f"a freeze of {seconds!r} cannot be recorded ({exc}). Nothing was "
                "written; treat the wait as unrecorded and stop rather than retry."
            ) from None

        def change(state: dict) -> None:
            state["frozen_until"] = max(state["frozen_until"], when + seconds)
            state["frozen_reason"] = (
                f"{reason} (recorded "
                f"{datetime.now(configmod.local_tz()).isoformat(timespec='seconds')})"
            )
            if not injected:
                # Only a real clock gets a monotonic twin. A test driving `now`
                # by hand describes a hypothetical moment, and anchoring that to
                # this machine's uptime would make its assertions untestable.
                mono = time.monotonic()
                if mono + seconds > state["frozen_until_mono"]:
                    state["frozen_until_mono"] = mono + seconds
                    state["mono_at_freeze"] = mono

        return self._mutate(change, now)

    def record_request(self, now: float | None = None) -> dict:
        """Count a getHistory that actually left the machine.

        `now` is honoured rather than accepted and ignored: it decides which
        local day the request lands on, so a caller simulating another day gets
        that day instead of silently getting today.
        """
        def change(state: dict) -> None:
            state["requests"] += 1

        return self._mutate(change, now)

    def summary(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        state = self.read(now)
        left = self.frozen_for(now)
        return {
            "date": state["date"],
            "history_requests_today": state["requests"],
            "history_frozen": left > 0,
            "history_frozen_for_sec": left,
            "history_frozen_reason": state["frozen_reason"],
        }


class PeerCache:
    """Usernames to (id, access_hash), on disk, stamped with the login session.

    **This file is what makes `contacts.resolveUsername` unnecessary**, and
    `references/account.md` argues it in full. Two rules live here:

    * a record carries `auth_session_fingerprint` and `resolve.peer_is_usable` is
      its only reader, because Telegram documents access hashes as not reusable
      across login sessions;
    * **unreadable means empty, not refuse.** The ledger fails closed because
      losing what it holds spends the account; losing what this holds costs one
      `contacts.search`, while handing out a peer from a file we could not parse
      is the dangerous direction.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.unreadable = ""

    def read(self) -> dict:
        self.unreadable = ""
        try:
            raw = configmod.read_bytes_shared(self.path)
            if not raw:
                return {}
            peers = json.loads(raw.decode("utf-8"))["peers"]
            if not isinstance(peers, dict):
                raise ValueError("`peers` is not an object")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            # Named, never swallowed: `summary()` prints it and the CLI shows it,
            # so a cache that went unreadable is visible instead of just empty.
            self.unreadable = f"{self.path} could not be read as a peer cache: {exc}"
            return {}
        return {str(k).lower(): v for k, v in peers.items() if isinstance(v, dict)}

    def get(self, username: str, fingerprint: str) -> dict | None:
        """The cached peer for this name under THIS login, or None."""
        entry = self.read().get(str(username).lstrip("@").lower())
        if not resolvemod.peer_is_usable(entry, fingerprint):
            return None
        return dict(entry)

    def put(self, rows, fingerprint: str) -> int:
        """Store peer records under the fingerprint that produced them.

        A row with no username, id or access_hash is not stored: it could never
        be handed out, so counting it is a lie about what the cache holds.
        """
        keep = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if not row.get("username") or not row.get("id") or not row.get("access_hash"):
                continue
            entry = dict(row)
            entry["auth_session_fingerprint"] = fingerprint
            entry["seen_at"] = datetime.now(configmod.local_tz()).isoformat(
                timespec="seconds")
            keep.append(entry)
        if not keep:
            return 0
        peers = self.read()
        for entry in keep:
            peers[str(entry["username"]).lower()] = entry
        self._write(peers)
        return len(keep)

    def drop(self, username: str) -> bool:
        """Forget one name -- what a stale access_hash is answered with."""
        peers = self.read()
        if peers.pop(str(username).lstrip("@").lower(), None) is None:
            return False
        self._write(peers)
        return True

    def _write(self, peers: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            configmod.atomic_write_text(
                self.path,
                json.dumps({"peers": peers}, ensure_ascii=False, indent=2),
            )
        except (OSError, configmod.AtomicWriteFailed) as exc:
            raise StateWriteFailed(
                f"the peer cache {self.path} could not be written: {exc}. The peers "
                "this run paid for are not on disk; the next run will look them up again."
            ) from None

    def summary(self) -> dict:
        peers = self.read()
        return {"peers_cached": len(peers),
                "peer_cache": str(self.path),
                "peer_cache_unreadable": self.unreadable or None}


def _day_of(now: float | None = None) -> str:
    """The local day of `now`, or of the wall clock when nobody said.

    A moment nobody can turn into a date -- `NaN`, an infinity, a number of
    seconds no calendar has -- is refused as one of this module's own errors.
    `datetime.fromtimestamp` answers those with a bare `ValueError`,
    `OverflowError` or `OSError`, and every caller of this module catches
    `AccountError` only.
    """
    if now is None:
        return datetime.now(configmod.local_tz()).date().isoformat()
    try:
        when = datetime.fromtimestamp(
            configmod.want_finite_number({"now": now}, "now"),
            configmod.local_tz())
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise AccountError(
            f"{now!r} is not a moment in time this skill can use ({exc})"
        ) from None
    return when.date().isoformat()


def _today() -> str:
    return _day_of(None)


# --------------------------------------------------------------------------
# Requests and results
# --------------------------------------------------------------------------
@dataclass
class SourceRequest:
    """One source, plus the free-surface evidence that permits touching it.

    `evidence` is a `tgparse.PeerCard` or any mapping carrying `exists` and
    `type`. It is duck-typed on purpose: this module must not import the parsers
    to do accounting, and a test must be able to hand it two fields.
    """

    username: str
    evidence: Any = None
    cached_peer: dict | None = None

    @classmethod
    def from_card(cls, card, cached_peer: dict | None = None) -> "SourceRequest":
        return cls(username=getattr(card, "username", ""), evidence=card,
                   cached_peer=cached_peer)


@dataclass
class PrepareReport:
    """What a run would do, or did, to get a usable peer for each source."""

    dry_run: bool = True
    peers: dict = field(default_factory=dict)
    from_cache: list = field(default_factory=list)
    from_session: list = field(default_factory=list)   # resolved earlier in this run
    resolved: list = field(default_factory=list)
    would_resolve: list = field(default_factory=list)
    cache_discarded: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)      # username -> why
    frozen: bool = False
    # A dry run cannot check the fingerprint against the live credential -- it
    # deliberately never reads it -- so it says which login session it judged the
    # cache against. False means "this preview assumes nobody has logged in again
    # since the last live run", which is exactly what it cannot know.
    fingerprint_verified: bool = True

    def as_dict(self) -> dict:
        return configmod.redact_obj({
            "dry_run": self.dry_run,
            "peers": {k: {"id": v.get("id")} for k, v in self.peers.items()},
            "from_cache": list(self.from_cache),
            "from_session": list(self.from_session),
            "resolved": list(self.resolved),
            "would_resolve": list(self.would_resolve),
            "cache_discarded": list(self.cache_discarded),
            "skipped": dict(self.skipped),
            "frozen": self.frozen,
            "fingerprint_verified": self.fingerprint_verified,
        })


def _attach_report(exc: BaseException, report: "PrepareReport") -> None:
    """Let a run's paid-for work ride out on the exception that ended the run.

    Defensive on purpose: some exception types refuse an attribute, and losing
    the report is exactly what this exists to prevent, so it must not be able to
    replace the original failure with an `AttributeError`.
    """
    try:
        exc.report = report
    except Exception:
        pass


@dataclass
class HistoryPage:
    """One page of group history, or the description of the page not fetched."""

    username: str
    messages: list = field(default_factory=list)
    requests: int = 0
    dry_run: bool = True
    would: dict | None = None
    stopped: str | None = None

    @property
    def truncated(self) -> bool:
        """Short because something stopped it, not because history ran out.

        A pagination loop written as `while page.messages:` reads a stopped page
        as the end of the group. It is not: it is a wait, a ceiling, or a refusal,
        and the difference is the difference between "we have it all" and "we
        have a fifth of it and said so".
        """
        return bool(self.stopped)

    def as_dict(self) -> dict:
        return configmod.redact_obj({
            "username": self.username,
            "messages": len(self.messages),
            "requests": self.requests,
            "dry_run": self.dry_run,
            "would": self.would,
            "stopped": self.stopped,
            "truncated": self.truncated,
        })


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------
class AccountSession:
    """Everything that spends the account, behind one lock and one ledger.

    Dry run is the default, and live mode needs two independent switches:
    `allow_live=True` in code AND `TELEGRAM_RESEARCH_ALLOW_LIVE` in the
    environment. One of them alone is a mistake somebody made; both of them is a
    decision somebody took.

    The whole session runs inside `AccountLock`. Two writers on one account
    double its request rate against a single identity, and any other client
    signed into the same account -- a script of your own, somebody else's tool,
    each throttling per process and none of them able to see this one -- is
    exactly that second writer. A second caller gets `AccountBusy`, not a slower
    run.
    """

    def __init__(self, transport, *, cfg=None, ledger=None, lock=None,
                 history_log=None, peer_cache=None, fingerprint: str | None = None,
                 dry_run: bool = True, allow_live: bool = False,
                 options: dict | None = None,
                 sleep=time.sleep):
        self.cfg = cfg if cfg is not None else configmod.load()
        self.transport = transport
        # Read-only from here on: `dry_run` was a plain attribute, and one
        # assignment on a session constructed with no switches at all put a real
        # `contacts.resolveUsername` on the wire. There is no `env=` keyword
        # either -- the second switch is a fact about the environment, and a
        # keyword collapsed two independent switches into one line of code.
        self._dry_run = bool(dry_run)
        self._allow_live = bool(allow_live)
        self._sleep = sleep

        if not self._dry_run:
            _require_live_switches(self._allow_live, "this session is live.")

        budgets = self.cfg.budgets
        self.ledger = ledger if ledger is not None else ResolveLedger(
            self.cfg.ledger_path,
            daily_ceiling=budgets.daily_resolve_ceiling,
            burst_ceiling=budgets.burst_ceiling,
            burst_window=budgets.burst_window_sec,
            min_gap=budgets.min_resolve_gap_sec,
            join_ceiling=budgets.daily_join_ceiling,
        )
        self.lock = lock if lock is not None else AccountLock(self.cfg.lock_path)
        self.history_log = history_log if history_log is not None else HistoryLog(
            Path(self.cfg.state_dir) / HISTORY_STATE_FILE
        )
        self.peer_cache = peer_cache if peer_cache is not None else PeerCache(
            Path(self.cfg.state_dir) / PEER_CACHE_FILE
        )
        self._options = dict(options or {})
        self._fingerprint_source = "supplied"
        self.fingerprint = (
            fingerprint if fingerprint is not None else self._discover_fingerprint()
        )
        self.ledger.fingerprint = self.fingerprint

        # A dry run's fingerprint comes from the last live run's ledger, which may
        # be two logins old; only a live run hashes the credential itself.
        self.fingerprint_verified = self._fingerprint_source != "ledger"

        self._open = False
        self._frozen = False          # run-local latch: no resolve after the first flood
        self._budget_stop = False     # run-local latch: our own ceiling said stop
        self._flood_stop = False      # run-local latch: history flooded
        self._error_stop = False      # run-local latch: a resolve failed unexpectedly
        self._stop_reason = ""
        self._history_reason = ""     # run-local latch: why history stopped, if it did
        self._peers_seen: dict[str, dict] = {}     # username -> peer paid for in this run
        self._history_requests = 0                 # getHistory calls made by THIS session
        self._last_history_ts = 0.0
        # A dry run's private copy of the ledger, so that its plan meets the same
        # ceilings in the same places a live run would. Built on first use.
        self._sim: Any = None
        self._planned: set[str] = set()            # names this dry run already costed

    # -- the switches ------------------------------------------------------
    @property
    def account_calls(self) -> int:
        """Calls this session put on the wire -- resolves excluded, failures IN.

        Adding up the `requests` field of each answer cannot see a call that
        raised instead of answering, and reported a refused one as zero.
        """
        return self._history_requests

    @property
    def dry_run(self) -> bool:
        """Read-only. Live mode is established at construction and re-checked at use."""
        return self._dry_run

    @property
    def allow_live(self) -> bool:
        return self._allow_live

    def _require_live(self) -> None:
        """Both switches again, at the moment of the call.

        The constructor checked them once, and every spending method then
        consulted `dry_run` alone -- so the guarantee held only for as long as
        nobody assigned to an attribute. Asked again here, against the real
        environment, it holds for the call itself.
        """
        _require_live_switches(self._allow_live, "this call would reach Telegram.")

    def _require_transport_ready(self) -> None:
        """Refuse before the budget is spent, not from inside the call.

        A transport that was never connected raises from its own `_require()`,
        which is after the resolve has been counted: eight sources against an
        unconnected transport exhausted the burst ceiling and armed the 30 s gap
        latch with nothing sent. A transport that does not answer the question is
        taken at its word.
        """
        if getattr(self.transport, "connected", None) is False:
            raise TransportError(
                "the transport is not connected; call connect() first. Nothing was "
                "sent, and nothing was charged to the account."
            )

    def _touch_lock(self) -> None:
        """Tell the lock the run is alive. Never raises: it is a heartbeat.

        `stale_after` has to mean "stopped working", not "started long ago". The
        only thing that refreshed the lock was a ledger write, and bulk group
        history -- the one job this file exists for -- writes no ledger entry: a
        `deep` run pages for longer than the 1800 s staleness, and a second
        process took the lock mid-run. Measured 2026-08-25.
        """
        toucher = getattr(self.lock, "touch", None)
        if not callable(toucher):
            return
        try:
            toucher()
        except Exception:
            pass

    # -- fingerprint -------------------------------------------------------
    def _discover_fingerprint(self) -> str:
        """The id of the login session whose access hashes we may reuse.

        Dry run does not read the credential file at all: it takes the
        fingerprint the last live run wrote into the ledger, which is a truncated
        hash and carries no secret. Live mode hashes the current session string
        and lets it go immediately, because a fingerprint from a previous login
        would silently bless access hashes that are already dead.
        """
        if not self.dry_run:
            values = configmod.read_credentials(self.cfg)
            fingerprint = session_fingerprint(values.get("TELEGRAM_SESSION", ""))
            values.clear()
            self._fingerprint_source = "credential"
            return fingerprint
        self._fingerprint_source = "ledger"
        return self.ledger.read().fingerprint or ""

    # -- lock --------------------------------------------------------------
    def __enter__(self) -> "AccountSession":
        self.lock.acquire()           # wait=0: a busy account fails now, not later
        self._open = True
        return self

    def __exit__(self, *exc):
        # The connection is closed before the lock is dropped. The lock exists to
        # keep two authorised connections off one identity, and a session that
        # released the lock while still holding an open MTProto connection let
        # the next process connect alongside it.
        try:
            self._close_transport()
        finally:
            self.lock.release()
            self._open = False
        return False

    def _close_transport(self) -> None:
        closer = getattr(self.transport, "close", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception:
            # Cleanup never masks the exception that is already on its way out,
            # and never forwards a message this module did not write.
            pass

    def _require_open(self) -> None:
        if not self._open:
            raise AccountError(
                "the account session is not open. Use `with AccountSession(...) as s:` "
                "so that the account lock is held for the whole run."
            )

    # -- options -----------------------------------------------------------
    def call_options(self) -> dict:
        """Options attached to every outgoing call, with the paid flag forced off."""
        return free_call_options(getattr(self.cfg, "call_options", None), self._options)

    # -- evidence ----------------------------------------------------------
    def _require_evidence(self, request: SourceRequest) -> str:
        """Refuse to spend a resolve on a name the free surface has not confirmed.

        `t.me/<name>` answers exists and type in one accountless GET. Resolving a
        name to find out whether it exists spends the one call that has ever cost
        this account downtime, to learn something that was free. The refusal is an
        exception rather than a log line because a log line gets read afterwards.
        """
        evidence = request.evidence
        if evidence is None:
            raise EvidenceRequired(
                f"@{request.username}: no evidence. A resolve is only allowed to fetch an "
                "access_hash for a name that t.me/<name> already confirmed for free. "
                "Fetch the landing card first (tgweb + tgparse.parse_landing)."
            )
        if isinstance(evidence, dict):
            exists = evidence.get("exists")
            kind = evidence.get("type")
            named = evidence.get("username")
        else:
            exists = getattr(evidence, "exists", None)
            kind = getattr(evidence, "type", None)
            named = getattr(evidence, "username", None)
        if named and str(named).lstrip("@").lower() != request.username.lstrip("@").lower():
            raise EvidenceRequired(
                f"@{request.username}: the evidence is about @{named}. Evidence for one "
                "name never licenses a resolve of another."
            )
        if exists is not True:
            raise EvidenceRequired(
                f"@{request.username}: evidence says exists={exists!r}. Only a landing card "
                "that positively said the name exists licenses an account call."
            )
        if kind not in KNOWN_TYPES:
            raise EvidenceRequired(
                f"@{request.username}: evidence says type={kind!r}, which is not one of "
                f"{KNOWN_TYPES}. An unparsed card is not evidence."
            )
        return kind

    def _require_group(self, request: SourceRequest) -> str:
        kind = self._require_evidence(request)
        if kind == "channel":
            raise WrongSurface(
                f"@{request.username} is a channel, and the account path exists for groups. "
                "t.me/s/<name> serves a channel 20 messages a page for free and ?q= searches "
                "its whole history; use read.search_channel / read.walk_channel."
            )
        return kind

    # -- resolve -----------------------------------------------------------
    def _sim_state(self):
        """The ledger as a dry run imagines it, one planned resolve at a time.

        A dry run that consults only the disk reports the same clean slate for
        source 30 as for source 1: thirty sources came back as thirty resolves,
        when the live run of the same list resolves eight and refuses
        twenty-two on the burst ceiling. The preview exists to let a human
        decide whether a run is safe, so it has to describe a run that can
        happen.
        """
        if self._sim is None:
            self._sim = self.ledger.read()         # LedgerUnreadable propagates
        return self._sim

    def _next_slot(self) -> float:
        """The earliest moment the minimum gap allows another resolve.

        A gap is a wait, not a refusal: the ledger's ceilings refuse, the gap
        merely slows down. The ceilings are therefore checked as of this moment
        rather than as of now, or the ledger would refuse the very call the pause
        exists to make legal. Sleeping is injected so that the tests do not spend
        30 s proving that we would have.

        A dry run reads its own simulated state, so its slots march forward one
        minimum gap at a time, exactly as a live run's would.
        """
        state = self._sim_state() if self._dry_run else self.ledger.read()
        now = time.time()
        if not state.last_resolve_ts:
            return now
        return max(now, state.last_resolve_ts + self.ledger.min_gap)

    def _budget_would_clear(self, slot: float) -> bool:
        """Is this refusal a pause or a wall?

        The minimum gap is a pause by design -- `_next_slot` exists to wait it
        out -- while the ceilings are what refuse. Latching `_budget_stop` on a
        gap refusal ends a whole run over a wait of seconds, and reports
        "budget exhausted" with the budget untouched. Asked structurally rather
        than by reading the message: re-check one whole gap later. A daily
        ceiling still says no, a ten-minute burst window still says no, only the
        gap clears.
        """
        when = slot + max(self.ledger.min_gap, 1.0) + GAP_EPSILON
        try:
            if self._dry_run:
                self.ledger.check_state(self._sim_state(), when)
            else:
                self.ledger.check_resolve(now=when)
        except Exception:
            return False
        return True

    def _refusal_is_a_wall(self) -> bool:
        """A budget refusal we could not re-examine at all is a wall.

        `_budget_would_clear(self._next_slot())` used to be evaluated inside
        `prepare`'s own `except BudgetExhausted` handler, and `_next_slot` reads
        the ledger: a ledger damaged mid-run raised `LedgerUnreadable` a second
        time, out of the handler, out of `prepare`, and every peer the run had
        already paid for died with it.
        """
        try:
            slot = self._next_slot()
        except Exception:
            return True                    # we cannot read the accounting: stop
        return not self._budget_would_clear(slot)

    def _plan_resolve(self, request: SourceRequest, slot: float) -> None:
        """Charge one resolve to the dry run's own copy of the ledger.

        Raises exactly what a live run would raise, from the same arithmetic --
        `ResolveLedger.plan_resolve` is `check` plus the recording step that
        `reserve_resolve` uses, with the disk left out.
        """
        if request.username in self._planned:
            return                         # already costed this run; it costs once
        self.ledger.plan_resolve(self._sim_state(), slot + GAP_EPSILON)
        self._planned.add(request.username)

    def _settle_resolve(self, token, username: str, ok: bool) -> None:
        """Close the reservation taken before the call.

        Best effort ONLY when there is a reservation to close: the charge is
        already on disk -- that is what reserving means -- so a failure here
        costs an audit line and nothing else, and it must not replace the
        exception the caller is already handling with a write error. An
        unsettled reservation shows up in `summary()` as a pending resolve and
        is pruned by `PENDING_TTL_SEC`.

        Without a token nothing has been counted yet, and then a failed write is
        the old rule again and the loud one: the caller must not proceed as if
        the account had been charged when it has not.
        """
        if token is None:
            self.ledger.record_resolve(username, ok=ok)
            return
        try:
            # The token settles THAT reservation and no other.
            # `record_resolve` used to match on the username, find nothing --
            # every reservation the working path took was anonymous -- and fall
            # back to settling the OLDEST nameless one on the books, so a
            # healthy call cleared the pending record left by a run that died
            # mid-call. `resolve.settle_resolve` now raises `ReservationUnknown`
            # rather than closing somebody else's, and that raise is swallowed
            # here on purpose: the charge is already durable, and the safe side
            # when the two sides disagree is to leave the reservation standing
            # rather than to destroy the evidence.
            self.ledger.record_resolve(username, ok=ok, token=token)
        except Exception:
            pass

    def resolve(self, request: SourceRequest) -> dict | None:
        """One `contacts.resolveUsername`, fully accounted.

        Returns the stamped peer, or None in dry run, where every check still
        runs, the transport is never touched, and the ceilings are charged to a
        copy of the ledger so that the plan meets them where the run would.

        Accounted means, in order: the ceilings, the pause, the DURABLE charge,
        the call, the settlement. Anything else leaves a window in which the
        account is spent and the accounting says it is not.
        """
        self._require_open()
        self._require_group(request)
        if self._frozen:
            raise ResolveFrozen(
                f"@{request.username}: resolving stopped for this run. {self._stop_reason}"
            )
        slot = self._next_slot()
        if self.dry_run:
            # Dry run pauses for nothing and calls nothing -- but it charges its
            # own copy of the ledger, so the plan meets every ceiling where the
            # live run would meet it.
            self._plan_resolve(request, slot)
            return None

        self._require_live()
        self._require_transport_ready()
        # The ceilings before the pause: nobody sleeps 30 s to be refused after.
        # GAP_EPSILON, not bare `slot`: see the constant.
        self.ledger.check_resolve(now=slot + GAP_EPSILON)
        delay = slot - time.time()
        if delay > 0:
            self._sleep(delay)
        options = self.call_options()
        self._touch_lock()
        # The accounting is DURABLE BEFORE THE CALL. `check_resolve(reserve=True)`
        # writes the increment, the burst timestamp and the minimum-gap latch to
        # disk and hands back a token; `record_resolve` afterwards settles that
        # reservation rather than counting a second time. Recording after the
        # call cannot survive a kill mid-call -- `except Exception` does not catch
        # KeyboardInterrupt and nothing catches SIGKILL -- and that is the
        # 2026-08-20 signature: real calls on the wire, ledger total zero,
        # minimum-gap latch never armed, the next process resolving at once.
        #
        # The reservation carries the NAME as well as the count. It was taken
        # anonymously (`reserve_resolve("")`), and after a crash the pending
        # record is the only evidence of which name was mid-resolve: without it
        # the recovery path cannot tell one abandoned reservation from another,
        # and `summary()` can say a run died mid-call but not on what.
        when = max(time.time(), slot + GAP_EPSILON)
        token = self.ledger.check_resolve(now=when, reserve=True,
                                          username=request.username)
        try:
            raw = self.transport.resolve_username(request.username, options=options)
        except FloodWait as exc:
            # The one rule that is not negotiable, in the one order that survives
            # a failing disk: latch the run FIRST, write the freeze SECOND, settle
            # the accounting LAST. The freeze used to be queued behind the
            # accounting write, so a `LedgerWriteFailed` on the accounting left
            # ten hours of downtime recorded nowhere -- not on disk, not even in
            # the run -- and the next process resolved immediately, which is what
            # extends the ban.
            self._frozen = True
            self._stop_reason = (
                f"the first FloodWait ({exc.seconds} s) stopped resolving for every "
                "remaining source in this run."
            )
            try:
                self.ledger.freeze(
                    exc.seconds, f"FloodWait on resolve of @{request.username}"
                )
            except Exception as write_error:
                self._settle_resolve(token, request.username, ok=False)
                raise ResolveFrozen(
                    f"@{request.username}: {self._stop_reason} The freeze is NOT on "
                    f"disk: writing it failed ({type(write_error).__name__}: "
                    f"{configmod.redact(str(write_error))}). No other process can see "
                    f"this wait, so nothing else may start until {exc.seconds} s have "
                    "passed."
                ) from None
            self._settle_resolve(token, request.username, ok=False)
            raise ResolveFrozen(f"@{request.username}: {self._stop_reason}") from None
        except PeerNotFound:
            self._settle_resolve(token, request.username, ok=False)
            raise
        except AccountError:
            self._settle_resolve(token, request.username, ok=False)
            raise
        except Exception as exc:
            self._settle_resolve(token, request.username, ok=False)
            raise TransportError(f"resolve of @{request.username} failed: {exc}") from None

        self._settle_resolve(token, request.username, ok=True)
        peer = self._stamp(raw)
        # Paid for once, this run. `prepare()` consults this before spending the
        # budget on a name it already holds -- a candidate list with a duplicate
        # in it used to spend a quarter of the burst window re-buying one name.
        self._peers_seen[request.username] = dict(peer)
        return peer

    def _stamp(self, raw: dict) -> dict:
        """Attach the login-session fingerprint. The transport never gets to.

        Telegram documents that access hashes are not reusable across login
        sessions (core.telegram.org/api/peers), so the hash is worth nothing
        without the id of the session that produced it. That id comes from the
        credential we hashed, not from whatever the transport says.
        """
        peer = {
            "id": raw.get("id"),
            "access_hash": raw.get("access_hash"),
            "auth_session_fingerprint": self.fingerprint,
        }
        if not peer["id"] or not peer["access_hash"]:
            raise TransportError("the transport returned a peer with no id or no access_hash")
        return peer

    def prepare(self, requests: Iterable[SourceRequest]) -> PrepareReport:
        """Get a usable peer for each source, spending as little as possible.

        Cache first, resolve second, and after the first FloodWait neither: the
        remaining sources are skipped rather than retried, while the ones already
        holding a valid peer keep working.

        **It always returns the report it has.** Sources 1..7 resolving and
        source 8 hitting a reset connection used to raise out of here, and the
        report -- a local variable -- died with the exception: seven paid-for
        peers lost, and a retry that re-resolves the same seven names. An
        unexpected failure is a skipped source, and the first one latches the
        run: something we do not understand happened on the wire, and the answer
        to that is to stop spending, not to try the next name.
        """
        self._require_open()
        report = PrepareReport(dry_run=self.dry_run,
                               fingerprint_verified=self.fingerprint_verified)
        try:
            self._prepare_each(requests, report)
        except BaseException as exc:
            # Loudly, but not destructively: the peers already paid for ride out
            # on the exception rather than disappearing with it. Around the whole
            # loop rather than around one call in it, because the path that
            # actually lost seven paid-for peers was not the one that had a
            # handler: a ledger damaged mid-run raised out of the BudgetExhausted
            # handler itself.
            _attach_report(exc, report)
            raise
        report.frozen = report.frozen or self._frozen
        return report

    def _prepare_each(self, requests: Iterable[SourceRequest],
                      report: PrepareReport) -> None:
        for request in requests:
            self._require_group(request)          # a bad caller fails loudly, always

            if request.cached_peer is not None:
                if peer_is_usable(request.cached_peer, self.fingerprint):
                    report.peers[request.username] = dict(request.cached_peer)
                    report.from_cache.append(request.username)
                    continue
                # Silently: a hash from another login session is not an error, it
                # is an ordinary consequence of logging in again. It is dropped
                # and re-earned through the budget, never tried and never warned
                # about, because a warning here would train people to ignore it.
                report.cache_discarded.append(request.username)

            already = self._peers_seen.get(request.username)
            if already is not None and peer_is_usable(already, self.fingerprint):
                report.peers[request.username] = dict(already)
                report.from_session.append(request.username)
                continue

            if self._frozen or self._budget_stop or self._error_stop:
                report.skipped[request.username] = self._stop_reason
                report.frozen = report.frozen or self._frozen
                continue

            try:
                peer = self.resolve(request)
            except ResolveFrozen as exc:
                report.frozen = True
                report.skipped[request.username] = str(exc)
                continue
            except BudgetExhausted as exc:
                # A pause is not a wall: only a refusal that survives a whole
                # minimum gap ends the run. Asking the question must not be able
                # to raise -- `LedgerUnreadable` IS a `BudgetExhausted`, and
                # re-reading a damaged ledger inside this handler took the whole
                # report down with it.
                if self._refusal_is_a_wall():
                    self._budget_stop = True
                    self._stop_reason = str(exc)
                report.skipped[request.username] = str(exc)
                continue
            except PeerNotFound as exc:
                report.skipped[request.username] = str(exc)
                continue
            except Exception as exc:
                self._error_stop = True
                self._stop_reason = (
                    f"@{request.username} failed in a way this run does not "
                    f"understand ({type(exc).__name__}); no further name is resolved. "
                    f"{configmod.redact(str(exc))}"
                )
                report.skipped[request.username] = self._stop_reason
                continue

            if peer is None:
                report.would_resolve.append(request.username)
            else:
                report.peers[request.username] = peer
                report.resolved.append(request.username)

    # -- history -----------------------------------------------------------
    def _history_ceiling(self) -> int | None:
        """How many getHistory calls one run may make.

        `None` is the only way to say "no ceiling", and this method never
        says it: the account path does not have an unlimited mode. The caller
        still tests `is not None` rather than truth, because that is the shape
        that keeps a ceiling of 0 meaning zero.

        The LOWER of the account's own `max_history_requests_per_run` and the
        general `max_requests_per_run`, and never above the shipped account
        number. Both halves of that were defects once:

        * `0` used to mean two things at once -- "the operator asked for zero"
          and "there is no ceiling" -- and `_history_stop_reason` tested the
          value for truth, so a configured budget of zero switched the ceiling
          OFF on the one path in the skill that spends the account. Zero now
          means zero, here as everywhere: absent is the only way to say
          "unlimited", and this path never says it.
        * the ceiling was BORROWED from `max_requests_per_run`, a free-surface
          knob an override file may raise to anything, so `{"budgets":
          {"max_requests_per_run": 100000}}` raised the account's getHistory
          ceiling from 400 to 100000 in silence. An override may still lower
          either knob; neither can raise this one past what shipped.
        """
        budgets = getattr(self.cfg, "budgets", None)
        shipped = configmod.Budgets().max_history_requests_per_run
        found = []
        for name in ("max_history_requests_per_run", "max_requests_per_run"):
            value = getattr(budgets, name, None)
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            found.append(max(0, value))
        if not found:
            # A configuration carrying neither number is not permission to page
            # without a ceiling: the shipped account number applies.
            return shipped
        return min(min(found), shipped)

    def _history_stop_reason(self) -> str:
        """Why this run must not make an account call, or "" if it may.

        Read before every page: the durable wait first, because the whole point
        of writing it to disk is that the next process sees it too.

        One counter for `getHistory`, `contacts.search` and `messages.search`,
        deliberately. They are three names for the same thing -- a call this
        account makes that is not a resolve -- and giving each its own ceiling
        would be inventing three measurements where nobody has taken one. What is
        measured is the incident this whole module is argued from, and that was a
        resolve; the resolve ledger still counts resolves separately.
        """
        if self._history_reason:
            return self._history_reason
        left = self.history_log.frozen_for()      # StateUnreadable propagates: fail closed
        if left:
            reason = self.history_log.read().get("frozen_reason", "")
            self._history_reason = (
                f"history is frozen for another {left} s "
                f"({left // 3600} h {left % 3600 // 60} m): {reason}"
            )
            return self._history_reason
        ceiling = self._history_ceiling()
        # Counted per PROCESS, not per `AccountSession`. The counter was an
        # instance attribute, so a script that opens a session per source
        # multiplied the ceiling by the number of sources -- a second session
        # constructed one line later fetched another page with the first one's
        # ceiling already reached, and the durable count went 3 -> 4 without
        # anything refusing.
        spent = history_requests_this_process()
        # `is not None`, never truthiness: a ceiling of 0 refuses everything, and
        # `if ceiling and ...` read it as "no ceiling" -- the one place in the
        # skill where that mistake spends the account.
        if ceiling is not None and spent >= ceiling:
            if ceiling == 0:
                self._history_reason = (
                    "the configured account-call ceiling is 0, so this run makes "
                    "no call at all. Refused locally — Telegram has not been asked."
                )
            else:
                self._history_reason = (
                    f"{spent} account calls already made by this run, "
                    f"ceiling is {ceiling}. Refused locally — Telegram has not "
                    "been asked."
                )
            return self._history_reason
        return ""

    def _pace_history(self) -> None:
        """A gap between two getHistory calls. Policy, not a measurement.

        Nothing has ever measured a rate limit for this call on this account, so
        the gap the free surfaces use is borrowed rather than a number being
        invented for it. Sleeping is injected, like everywhere else here.
        """
        gap = getattr(getattr(self.cfg, "budgets", None), "min_gap_sec", 0.0) or 0.0
        last = last_history_ts_this_process()
        if not gap or not last:
            return
        delay = (last + float(gap)) - time.time()
        if delay > 0:
            self._sleep(delay)

    def _record_history_request(self) -> None:
        """One getHistory left the machine. Counted whether or not it answered."""
        global _PROCESS_HISTORY_REQUESTS, _PROCESS_LAST_HISTORY_TS

        self._history_requests += 1
        _PROCESS_HISTORY_REQUESTS += 1
        _PROCESS_LAST_HISTORY_TS = time.time()
        self._last_history_ts = _PROCESS_LAST_HISTORY_TS      # this session's view
        # The run is alive: refresh the lock. A bulk history read writes no
        # ledger entry, and a ledger write was the only heartbeat there was.
        self._touch_lock()
        try:
            self.history_log.record_request()
        except AccountError:
            # The durable count is an audit trail, not a gate: failing to write
            # it must not swallow the exception the caller is already handling.
            pass

    def history(self, request: SourceRequest, peer: dict, *, limit: int = 100,
                offset_id: int = 0) -> HistoryPage:
        """One page of group history, through the same funnel as the searches.

        It used to carry its own copy of the pacing, the durable freeze, the
        per-run ceiling and the count-before-you-know-the-answer discipline --
        seventy lines that had to stay in step with `_spend_one_call` by hand.
        One funnel, and `send` is the only part that differs.
        """
        self._require_open()
        self._require_group(request)
        if not peer_is_usable(peer, self.fingerprint):
            raise PeerUnusable(
                f"@{request.username}: the peer is incomplete or belongs to another login "
                "session. It is not tried; resolve it again under the budget."
            )
        options = self.call_options()
        if self.dry_run:
            stopped = self._history_stop_reason()
            return HistoryPage(
                username=request.username, dry_run=True, stopped=stopped,
                would=None if stopped else
                {"call": "messages.getHistory", "peer_id": peer.get("id"),
                 "limit": limit, "offset_id": offset_id, "options": options},
            )
        rows, stopped = self._spend_one_call(
            "messages.getHistory",
            lambda: self.transport.fetch_history(
                peer, limit=limit, offset_id=offset_id, options=options),
        )
        if stopped:
            return HistoryPage(username=request.username, dry_run=False,
                               requests=1 if self._flood_stop else 0, stopped=stopped)
        return HistoryPage(username=request.username, messages=list(rows), requests=1,
                           dry_run=False)

    # -- search --------------------------------------------------------------
    def _searchable(self, username: str, peer: dict) -> dict:
        """The peer a group search may be sent to, or a refusal saying why not.

        Two checks and no landing card: `history()` demands free-surface evidence
        because it licenses a RESOLVE, while a search peer comes from
        `contacts.search` -- Telegram itself saying what the peer is. The channel
        refusal stays: `?q=` searches a channel for free, so the account near one
        buys risk and nothing else.
        """
        if not peer_is_usable(peer, self.fingerprint):
            raise PeerUnusable(
                f"@{username}: the peer is incomplete or belongs to another login "
                "session. It is not tried; look the name up again with contacts.search."
            )
        if peer.get("type") == "channel":
            raise WrongSurface(
                f"@{username} is a channel, and the account path exists for groups. "
                "t.me/s/<name>?q= searches a channel's whole history for free; use "
                "read.search_channel."
            )
        return peer

    def _spend_one_call(self, what: str, send):
        """One account call: the pacing, the ceiling, the freeze and the count.

        The one funnel for `getHistory`, `contacts.search` and `messages.search`.
        It grew one incident at a time and a second copy of it would drift.
        Returns `(payload, stopped)`; `stopped` is a sentence and the payload is
        then None.
        """
        stopped = self._history_stop_reason()
        if stopped:
            return None, stopped
        self._require_live()
        self._require_transport_ready()
        self._pace_history()
        # Before the call as well as after it: a call can sit on a 30 s transport
        # timeout and the lock has to read as alive throughout.
        self._touch_lock()
        try:
            payload = send()
        except FloodWait as exc:
            # Same order as the history freeze, for the same reason: the run-local
            # latch first, the durable wait second, the audit count last, so a
            # failing disk cannot lose the downtime.
            self._flood_stop = True
            self._history_reason = (
                f"{what} flooded for {exc.seconds} s; this run stops calling, and "
                "the next one waits it out."
            )
            try:
                self.history_log.freeze(exc.seconds, f"FloodWait on {what}")
            except AccountError as write_error:
                self._history_reason = (
                    f"{self._history_reason} The wait is NOT on disk: writing it "
                    f"failed ({write_error}). No other process can see it."
                )
            self._record_history_request()
            return None, self._history_reason
        except PeerUnusable:
            # Telegram answered, and about this PEER rather than about the wire.
            # Counted (the call left the machine) and deliberately NOT latched:
            # the latch is for a failure nobody understands, and this one has a
            # known repair. Latching it made the repair unreachable -- measured
            # live against a corrupted hash, `found: 0` over 44 real matches.
            self._record_history_request()
            raise
        except AccountError:
            # The call left the machine, so it is counted. Ours already, so the
            # message is already redacted.
            self._record_history_request()
            self._history_reason = f"{what} failed; this run stops calling."
            raise
        except Exception as exc:
            self._record_history_request()
            self._history_reason = (
                f"{what} failed with {type(exc).__name__}; this run stops calling."
            )
            raise TransportError(f"{what} failed: {exc}") from None
        except BaseException:
            # The call is on the wire and the process is going away. The durable
            # daily count is the only record that the account made it.
            self._record_history_request()
            raise
        self._record_history_request()
        return payload, ""

    def search_contacts(self, query: str, *, limit: int = 50) -> dict:
        """The app's own search box: one call, no resolve, and it fills the cache.

        It answers with chats AND their access hashes, so every name it returns is
        readable afterwards for nothing -- which is why the result is cached here
        rather than by the caller. It sees titles and usernames and **nothing
        inside a message**.
        """
        self._require_open()
        text = (query or "").strip()
        if not text:
            raise AccountError(
                "contacts.search was asked for an empty query. Nothing was sent: "
                "`found: 0` from a search nobody ran is the silence this skill "
                "exists not to produce."
            )
        options = self.call_options()
        if self.dry_run:
            return {"query": text, "dry_run": True, "requests": 0, "peers": [],
                    "would": {"call": "contacts.search", "q": text,
                              "limit": limit, "options": options}}
        rows, stopped = self._spend_one_call(
            "contacts.search",
            lambda: self.transport.search_contacts(text, limit=limit, options=options),
        )
        if stopped:
            return {"query": text, "dry_run": False, "requests": 1 if self._flood_stop else 0,
                    "peers": [], "stopped": stopped}
        cached = self.peer_cache.put(rows, self.fingerprint)
        return {"query": text, "dry_run": False, "requests": 1,
                "peers": list(rows), "peers_cached": cached}

    def search_messages(self, username: str, peer: dict, query: str, *,
                        limit: int = 50, add_offset: int = 0) -> dict:
        """One page of `messages.search` inside one group. The reason for all this.

        `total` is the server's count of matches, so a caller that stops early
        knows exactly what it left behind -- the thing `?q=` can never say.
        """
        self._require_open()
        text = (query or "").strip()
        if not text:
            raise AccountError(
                f"@{username}: messages.search was asked for an empty query. Nothing "
                "was sent: `found: 0` from a search nobody ran reads as silence."
            )
        peer = self._searchable(username, peer)
        options = self.call_options()
        if self.dry_run:
            return {"username": username, "query": text, "dry_run": True,
                    "requests": 0, "messages": [], "total": None,
                    "would": {"call": "messages.search", "peer_id": peer.get("id"),
                              "q": text, "limit": limit, "add_offset": add_offset,
                              "options": options}}
        payload, stopped = self._spend_one_call(
            "messages.search",
            lambda: self.transport.search_messages(
                peer, text, limit=limit, add_offset=add_offset, options=options),
        )
        if stopped:
            return {"username": username, "query": text, "dry_run": False,
                    "requests": 1 if self._flood_stop else 0,
                    "messages": [], "total": None, "stopped": stopped}
        return {"username": username, "query": text, "dry_run": False, "requests": 1,
                "messages": list(payload.get("messages", [])),
                "total": payload.get("total")}

    # -- join --------------------------------------------------------------
    def join_group(self, request: SourceRequest, peer: dict) -> dict:
        """Join a group. Explicit, budgeted, and never a side effect of reading.

        This is the only method in the class that touches the join budget or
        names the transport's join capability, and `Transport` does not carry
        that capability at all, so no reading path can reach it. It also does not
        resolve: it takes a peer the caller already holds, which keeps joining
        off the resolve budget entirely.
        """
        self._require_open()
        self._require_group(request)
        if not peer_is_usable(peer, self.fingerprint):
            raise PeerUnusable(
                f"@{request.username}: joining needs a peer from the current login session."
            )
        self.ledger.check_join()
        if self.dry_run:
            return {"would_join": request.username, "dry_run": True}
        self._require_live()
        self._require_transport_ready()
        joiner = getattr(self.transport, "join_group", None)
        if joiner is None:
            raise TransportError("this transport cannot join; it carries only the read seam")
        options = self.call_options()
        # Everything that can refuse this call ON THIS MACHINE happens here,
        # before a single unit of the daily ceiling of 3 is charged. Both of
        # these used to be raised from inside the transport and land in the
        # handlers below, which charge: three local refusals exhausted the day's
        # join budget with nothing on the wire. `_assert_free` is the paid-call
        # gate, and `peer_is_numeric` is the one reachable from real registry
        # data -- see its docstring.
        _assert_free(options)
        if not peer_is_numeric(peer):
            raise PeerUnusable(
                f"@{request.username}: {PEER_NOT_NUMERIC} Nothing was charged."
            )
        self._touch_lock()

        def charge() -> None:
            """The request left the machine, so the identity wore it.

            Quietly on the failure paths: the ceiling is an audit trail as well
            as a gate, and a ledger that cannot write must not replace the
            failure the caller is already handling with a write error.
            """
            try:
                self.ledger.record_join()
            except Exception:
                pass

        try:
            result = joiner(peer, options=options)
        except AccountError:
            # Counted even when it failed: the request left the machine and the
            # identity wore it, which is what the ceiling is counting.
            charge()
            raise
        except Exception as exc:
            # The same rule, for every other way a transport can fail. Counting
            # only `AccountError` meant a transport that failed in the ordinary
            # way -- a reset connection, a bug -- bypassed the ceiling of 3
            # indefinitely, and forwarded its own message and traceback while
            # doing it.
            charge()
            raise TransportError(
                f"joining @{request.username} failed: {exc}"
            ) from None
        except BaseException:
            # Ctrl-C, a supervisor timeout, VS Code stopping the task: none of
            # them is an `Exception`, so the join stayed on the wire and the
            # ceiling was never charged. Three interrupted joins and the account
            # has joined three groups with `joins_today = 0`. Over-counting what
            # left the machine is the safe direction; under-counting is the one
            # that bought the 36 468 s wait.
            charge()
            raise
        self.ledger.record_join()
        return dict(result)

    # -- reporting ---------------------------------------------------------
    def summary(self) -> dict:
        """Safe to print, safe to file: redacted, and the fingerprint is a hash."""
        try:
            history = self.history_log.summary()
        except AccountError as exc:
            history = {"unreadable": str(exc)}
        return configmod.redact_obj({
            "mode": "dry-run" if self.dry_run else "live",
            "session_fingerprint": self.fingerprint,
            "fingerprint_source": self._fingerprint_source,
            "fingerprint_verified": self.fingerprint_verified,
            "resolving_stopped": self._frozen or self._budget_stop or self._error_stop,
            "stop_reason": self._stop_reason,
            "reading_stopped": bool(self._history_reason),
            "history_stop_reason": self._history_reason,
            "history_requests_this_run": self._history_requests,
            "ledger": self.ledger.summary(),
            "history": history,
            "telethon_pin": TELETHON_PIN,
        })


# --------------------------------------------------------------------------
# Status only. The skill's CLI defaults to dry run; this entry point has no
# live mode to default away from, because it never calls a transport at all.
# --------------------------------------------------------------------------
# What this command is allowed to print. Nothing else reaches stdout: the keys
# are listed here rather than assembled inline because this output goes into run
# logs, and a mutation that added a `"credentials"` key to it printed the api_id,
# the api_hash and the session string verbatim with the whole suite green.
STATUS_KEYS = (
    "ledger",
    "history",
    "peers",
    "telethon_pin",
    "telethon_source",
    "telethon_installed",
    "live_enabled_in_env",
    "notice",
)


def status(cfg=None) -> dict:
    """The status payload, redacted, with exactly the keys STATUS_KEYS names."""
    cfg = configmod.load() if cfg is None else cfg
    # `Config.ensure_dirs` and `_mkdir_advice` exist to turn every OSError from
    # creating the state directory into a ConfigError carrying the sentence that
    # fits THAT failure. Status skipped them, so the first thing to touch the
    # disk was a bare `mkdir` in `HistoryLog.__init__`: an unmapped drive in
    # `TELEGRAM_RESEARCH_STATE` answered `python scripts/account.py` with a
    # `FileNotFoundError [WinError 3]` traceback at exit 9, against a docstring
    # promising "a configuration error with a sentence, not a traceback".
    ensure = getattr(cfg, "ensure_dirs", None)
    if callable(ensure):
        ensure()
    ledger = ResolveLedger(
        cfg.ledger_path,
        daily_ceiling=cfg.budgets.daily_resolve_ceiling,
        burst_ceiling=cfg.budgets.burst_ceiling,
        burst_window=cfg.budgets.burst_window_sec,
        min_gap=cfg.budgets.min_resolve_gap_sec,
        join_ceiling=cfg.budgets.daily_join_ceiling,
    )
    try:
        history = HistoryLog(Path(cfg.state_dir) / HISTORY_STATE_FILE).summary()
    except AccountError as exc:
        history = {"unreadable": str(exc)}
    # Reported because whether the library is there is the difference between "the
    # live path cannot run at all" and "two switches away from the wire", and only
    # the operator can see which. Asked without importing it -- a status command
    # has no business loading Telethon.
    installed = telethon_installed()
    try:
        peers = PeerCache(Path(cfg.state_dir) / PEER_CACHE_FILE).summary()
    except AccountError as exc:
        peers = {"unreadable": str(exc)}
    payload = {
        "ledger": ledger.summary(),
        "history": history,
        "peers": peers,
        "telethon_pin": TELETHON_PIN,
        "telethon_source": TELETHON_SOURCE,
        "telethon_installed": installed,
        # The value is parsed, not merely found: `TELEGRAM_RESEARCH_ALLOW_LIVE=0`
        # used to be reported here as live mode enabled, which it also was. Same
        # reader the live check itself uses, so status cannot disagree with it.
        "live_enabled_in_env": live_enabled_in_env(),
        "notice": configmod.CREDENTIAL_NOTICE,
    }
    return configmod.redact_obj({k: payload[k] for k in STATUS_KEYS})


def main(argv=None) -> int:
    """Print the status, or a sentence. Never a traceback on stdout, never exit 1.

    `TELEGRAM_RESEARCH_STATE` pointing at a file is the documented configuration
    error, and `references/cli.md` promises it is "a configuration error with a sentence,
    not a traceback". It was a traceback, at exit 1, with the sentence inside it.
    Exit codes follow the skill's table: 7 for something the operator has to fix,
    9 for a failure this module did not foresee.
    """
    import argparse
    import sys as sysmod

    parser = argparse.ArgumentParser(
        description="Account budget status. Reads the ledger, spends nothing.")
    parser.parse_args(argv)
    try:
        payload = status()
    except (configmod.ConfigError, AccountError) as exc:
        print(json.dumps({"ok": False, "error": configmod.redact(str(exc)),
                          "error_type": type(exc).__name__},
                         indent=2, ensure_ascii=False))
        return 7
    except Exception as exc:
        # The traceback goes to stderr, where it helps; stdout stays JSON,
        # because that is what the run log keeps and what an agent parses.
        import traceback

        traceback.print_exc(file=sysmod.stderr)
        print(json.dumps({"ok": False, "error": configmod.redact(str(exc)),
                          "error_type": type(exc).__name__},
                         indent=2, ensure_ascii=False))
        return 9
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
