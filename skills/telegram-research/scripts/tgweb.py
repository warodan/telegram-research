"""Accountless HTTP layer for Telegram's public web surfaces.

Everything here works with no account, no token and no api_id. It is the default
path of the skill; the MTProto layer in `account.py` is the fallback of last
resort and is never reached for a channel.

Design rules, each of them measured against real saved pages rather than
assumed. The corpus measured was 58 pages; 32 of them are kept with the pytest
suite in the project repository at `tests/fixtures/probes/`, and the 10 `selftest`
parses travel with the installed skill at the same relative path. So a count below
of the form "N of 58" is the measurement and not a claim about either directory.

* **Every refusal arrives as HTTP 200.** The status code is never the signal.
  The classifiers below are the single place that decides what a body means.
* **Redirects are not followed on `/s/`.** That surface answers 302 for a group
  AND for a name that does not exist; the 302 is information, and following it
  destroys it.
* **One request at a time, with a gap.** t.me's per-IP limits have never been
  measured. There is no parallel fetching anywhere in this module, and the gap
  is enforced across processes through a state file, not merely inside one run.
* **React to a signal, not to a counter.** A 429, a challenge page, an empty body
  or a page that suddenly shrinks stops the run and is reported. Nothing retries
  in the hope that the surface changed its mind.

Standard library only. That is deliberate: the skill has to keep working on a
machine where nothing can be installed, and any dependency here would be a
dependency on the accountless path -- the path that must never break.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import io
import itertools
import json
import os
import random
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field, asdict
from pathlib import Path

# The one finiteness check in the skill. `json.loads` accepts the bare literals
# `NaN`, `Infinity` and `-Infinity`, both pass `isinstance(x, float)`, and NaN
# makes every comparison false -- so a guard written as `if low < FLOOR` reads a
# poisoned config as "nothing to enforce". `config` imports nothing from this
# module, so this direction is the only one there is.
import config

# --------------------------------------------------------------------------
# Pacing defaults
# --------------------------------------------------------------------------
# The lower bound comes from the 2026-08-23 probe, which made ~60 requests >=2 s apart and
# saw no throttling. The upper bound and the batch rest are the pacing
# jackvale/rectg runs against the same host at a far larger crawl volume (its
# crawl.py sleeps a uniform 3-6 s and rests 60 s every 50 requests). Neither is a
# measured limit -- the limit is unmeasured, which is exactly why the defaults
# sit on the slow side.
DEFAULT_MIN_GAP = 2.0          # seconds, lower edge of the jittered gap
DEFAULT_MAX_GAP = 4.0          # seconds, upper edge
DEFAULT_BATCH_SIZE = 50        # requests before the long rest
DEFAULT_BATCH_REST = 60.0      # seconds of rest after a batch
DEFAULT_TIMEOUT = 30.0         # seconds per request
MAX_RETRIES = 3                # transport errors only; a refusal is never retried

# The two ceilings on ONE body, and why a timeout is neither of them.
# `DEFAULT_TIMEOUT` is passed to `opener.open` and bounds a single socket
# operation, so a server that keeps trickling bytes never trips it and a body
# with no declared length never ends. Measured: 203 861 bytes of gzip expanded
# to 200 MB, were decompressed whole, decoded to a 200 MB string and -- with
# `save_as` -- written to disk, out of one request that looked ordinary in the
# fetch log.
#
# 8 MiB against a corpus whose largest page is 146 974 bytes (C03, a `?q=`
# search on @durov) is ~57x the biggest thing t.me has ever served this skill,
# so nothing real can reach it; and a body that does reach it is not a page
# this module can parse anyway. `MAX_BODY_SECONDS` is the same bound in the
# other dimension: the whole body, not one socket read.
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_BODY_SECONDS = 120.0
READ_CHUNK_BYTES = 64 * 1024   # how much of the body is asked for at a time
BACKOFF_CAP = 300.0            # seconds; matches the FloodWait ceiling used elsewhere
RETRY_BACKOFF_BASE = 60.0      # seconds before the 2nd attempt; doubles after that

# Cross-process reservation lock, used by Pacer.
PACE_LOCK_TIMEOUT = 10.0       # how long to wait for another process's reservation
PACE_LOCK_STALE = 60.0         # a lock file older than this was abandoned by a dead process

# A browser UA. t.me served every saved probe under curl's default UA, so this is not
# required there; it is here because the third-party surfaces were probed with one
# and their behaviour under a bare UA is untested.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

BASE = "https://t.me"


# --------------------------------------------------------------------------
# Refusal signals -- the parsing contract
# --------------------------------------------------------------------------
# Literal markers, each one observed in a saved probe body of the corpus.
TITLE_EXISTS = "Telegram: View @"
TITLE_MISSING = "Telegram: Contact @"
MSG_WRAP = "tgme_widget_message_wrap"
POST_NOT_FOUND = "Post not found"
# The attribute every real message carries, `<username>/<id>`. Its PRESENCE is
# the one fact on these pages that no wording change can localise away, so it
# is what the "is there a message here at all" half of every classifier below
# is built on. Across the 58 saved probes it occurs 116 times and never once on
# a page that carries no message.
DATA_POST = "data-post"
# The class on the message div of a "Post not found" page. It occurs exactly 7
# times in the corpus, on exactly the 7 error pages, and nowhere else. The
# prose next to it ("Post not found") is localisable -- the request sends
# `Accept-Language: en,ru;q=0.9` -- and this class is not, which is the same
# argument SEL["not_supported"] already makes about service messages.
ERR_MESSAGE = "err_message"
# The `?q=` twin of "Post not found". Confirmed live on 2026-08-24 against
# t.me/s/durov?q=zzqwxnonexistentterm12345 (HTTP 200, 18 727 bytes); the whole
# history section came back as
#     <section class="tgme_channel_history js-message_history">
#       <div class="tgme_widget_message_wrap js-widget_message_wrap"><div
#       class="tgme_widget_message_centered"><div class="tme_no_messages_found"
#       >No posts found</div></div></div>
#     </section>
# The marker is right -- and Telegram wraps it in a `tgme_widget_message_wrap`,
# which is why nothing may pair this marker with a "and no message wrap" test.
NO_MESSAGES_FOUND = "tme_no_messages_found"
# A t.me profile card. Absent from a third-party page, an interstitial or a
# wrong URL; `tgme_page_post` narrows a t.me page down to a single post, which
# is a message page and not a peer card either.
PAGE_WRAP = "tgme_page_wrap"
PAGE_POST = "tgme_page_post"

# What an anti-bot interstitial says about itself, lowercased. Two rules govern
# this list, and the pair it replaced broke both.
#
# 1. **It has to match what is served today.** The old test was
#    `"just a moment" in low and "enable javascript" in low` -- an AND across
#    two strings from two different eras of Cloudflare's page. The current
#    managed challenge renders "Verify you are human", "needs to review the
#    security of your connection" and a `cdn-cgi/challenge-platform` script,
#    and puts "Enable JavaScript and cookies to continue" in a `<noscript>`;
#    the legacy "checking your browser" wording is gone. A markup marker beats
#    prose here, because prose is localised and the script path is not.
# 2. **A marker alone may never abort a run** -- see `challenge_page`.
#
# The list is split in two because the structural guards in `challenge_page`
# (`tgme_page_wrap`, `data-post`) are t.me's markup and cannot appear on a
# third-party surface at all -- so off t.me the test collapsed to "does this
# body contain one of fourteen strings", and a search-results page for the word
# `captcha` contains one. Markup markers are a script path and Cloudflare's own
# attribute names, which a page quotes far less readily than it quotes prose;
# they are the only ones allowed to speak for a host whose markup this module
# cannot check. See `challenge_page`.
CHALLENGE_MARKUP_MARKERS = (
    "cdn-cgi/challenge-platform",     # the script every CF interstitial loads
    "cf-browser-verification",
    "cf_chl_opt",
    "__cf_chl",
    "cf-error-details",
)
CHALLENGE_PROSE_MARKERS = (
    "just a moment",
    "checking your browser before accessing",
    "verify you are human",
    "verifying you are human",
    "enable javascript and cookies to continue",
    "needs to review the security of your connection",
    "attention required! | cloudflare",
    "ddos protection by cloudflare",
    "captcha",
)
CHALLENGE_MARKERS = CHALLENGE_MARKUP_MARKERS + CHALLENGE_PROSE_MARKERS

# A body this small on a surface that should carry messages is not a page, it is
# a shrug. The smallest real /s/ body in the probes was 62 461 bytes for a search
# matching 7 posts; the smallest refusal body was ~9 900 bytes. Both figures are
# uncompressed, which is what `Response.bytes` now measures.
SUSPICIOUS_BODY_BYTES = 500

# Statuses that carry meaning rather than failure. A 3xx is data on `/s/` -- it
# is how a group and a nonexistent name announce themselves. Everything outside
# this set and outside the stop signals is a FetchFailed.
OK_STATUSES = frozenset({200, 301, 302, 303, 304, 307, 308})


class TelegramWebError(RuntimeError):
    """Transport failed in a way that is not a documented refusal."""


class RunAborted(RuntimeError):
    """A stop signal was seen. The run does not continue and says why."""


class TruncatedBody(RuntimeError):
    """A body arrived that will not decompress. Internal to `fetch`'s retry path."""


class BodyTooLarge(TruncatedBody):
    """The body outgrew `MAX_BODY_BYTES`, or took longer than `MAX_BODY_SECONDS`.

    A subclass because the callers that catch `TruncatedBody` mean "the body did
    not arrive whole", which is true here too. It is separate because the retry
    path must tell the two apart: a half-arrived body is the flaky link retrying
    exists for, while a page that is too big will be exactly as big next time --
    three attempts at it are 24 MB on the wire to learn what the first one did.
    """


class FetchFailed(RuntimeError):
    """The surface answered with a status that is neither success nor a signal.

    A 5xx that survived every retry, or an unexpected 4xx. It is emphatically
    NOT "there is nothing here": before this existed, `fetch` handed a 502
    error page back as an ordinary `Response`, the parsers found no messages in
    it, and a walk reported the outage as the end of history -- a network fault
    rendered as evidence of absence. Callers must let this propagate or say
    plainly that the surface could not be read.

    Distinct from `RunAborted`, which means Telegram told us to stop (429, a
    challenge page) and ends the whole run.
    """


@dataclass
class Response:
    """One HTTP act, with everything a later decision could need.

    `bytes` is the size of the DECOMPRESSED body -- byte-for-byte the file that
    `save_as` writes to disk, and the same figure the measurements tabulate
    (12 850, 9 883, 142 550). It used to be `len(raw)`, the on-the-wire size
    after gzip, which made the fetch log incomparable with the measurements
    and made the small-body stop signal fire ~4x too eagerly. The transfer
    size is kept
    separately in `wire_bytes` rather than thrown away.

    `url_effective` is where the body actually came from. `follow=True` is every
    group read and every landing fetch, and urllib's answer to "which URL served
    this" was simply discarded: nothing anywhere could ask whether the page in
    hand was the page that was requested. It equals `url` when nothing
    redirected, and `None` only when the transport would not say -- which is why
    `followed_elsewhere`, not a `None` check, is the question to ask.

    `status` is `0` on a transport failure -- a dropped connection, a timeout, a
    body that would not decompress. Those are real requests that reached t.me
    and they are logged like any other, with `error` naming what went wrong;
    before this they were invisible to `request_count`, to `on_fetch` and to
    `fetchlog.jsonl` while the run's ceiling was charged once for all three.

    `attempt` is 1 for the first try at a URL and counts up through the retries,
    so a fetch log with more lines than URLs can be read rather than guessed at.
    """

    url: str
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)
    location: str | None = None
    bytes: int = 0
    elapsed_ms: int = 0
    wire_bytes: int = 0
    url_effective: str | None = None
    attempt: int = 1
    error: str | None = None

    @property
    def redirected(self) -> bool:
        return self.status in (301, 302, 303, 307, 308)

    @property
    def followed_elsewhere(self) -> bool:
        """Did a followed redirect land this body on a different URL?

        A fact, not a verdict. No probe in the corpus captures a join wall, a
        login page or an interstitial, so nothing here invents a marker for one:
        the callers that care -- `parse_embed`, which now checks that the post
        it got is the post it asked for, and `embed_unreadable` -- decide from
        the body. This says only where the body came from.
        """
        if not self.url_effective:
            return False
        return self.url_effective.rstrip("/") != self.url.rstrip("/")


# --------------------------------------------------------------------------
# Cross-process pacing
# --------------------------------------------------------------------------
_TMP_SERIAL = itertools.count()


class Pacer:
    """Enforces the gap between requests across every process on this machine.

    The gap has to survive more than one caller. Two agents fetching the same
    host from two processes halve the interval without either of them noticing --
    the exact defect measured in another tool's per-process throttle.

    Reading a shared timestamp is not enough to prevent that, and the first
    version of this class did exactly that: read `last`, sleep, write `last`.
    Two processes read the SAME `last`, computed the same due time, slept to the
    same instant and fired together -- measured at 3 of 7 requests firing under
    1.0 s apart with a 2.0 s floor, while one process died outright on a shared
    temp file. What serialises is a RESERVATION, not a reading:

        under an exclusive lock:  due = max(now, last + gap);  last := due
        outside the lock:         sleep until `due`

    Each claimant writes the instant it intends to fire, so the next claimant
    computes its slot from that instant rather than from the previous firing.
    `last` is therefore legitimately in the near future -- up to one gap.

    The class is honest about the cases where it cannot serialise:

    * a state file that exists but cannot be parsed is NOT read as "no request
      has ever been made". It paces a full gap from now and says so on stderr;
      the old code returned `{"last": 0.0}` and skipped pacing entirely.
    * a lock it cannot take within `PACE_LOCK_TIMEOUT` means another process is
      mid-reservation. It paces a full gap from now, sets
      `serialised_across_processes = False` and says so, rather than pretending.
    * the sleep is capped (`sleep_cap`). A `last` a day in the future -- a clock
      change, a state file copied between machines -- used to sleep for 24 h.
    * the gap has a floor. This class used to accept `min_gap=max_gap=0` in
      silence, and `config.Budgets.min_gap_sec` / `max_gap_sec` are reachable
      from `TELEGRAM_RESEARCH_CONFIG`, so a one-line override file removed the
      only defence the accountless path has against an unmeasured per-IP limit:
      measured, eight `wait()` calls in 0.046 s with `last_warning` still None.
      Gaps may be WIDENED and never narrowed -- the same rule `config.py`
      already applies to the account block's gaps -- and a refusal is recorded
      in `gap_floor_note` as well as printed, so the caller holding the pacer
      can see that a value was refused.
    """

    # Nothing in the skill ever lifts this; the only subclass that does lives
    # in the test suite, which does not ship. Deliberately a class attribute
    # rather than a constructor argument: an argument is one `**cfg` away from
    # being reachable from a config file, and the whole point of this floor is
    # that a config file cannot lower it.
    enforce_gap_floor = True

    def __init__(
        self,
        state_dir: Path,
        min_gap: float = DEFAULT_MIN_GAP,
        max_gap: float = DEFAULT_MAX_GAP,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_rest: float = DEFAULT_BATCH_REST,
        host: str = "t.me",
    ) -> None:
        self.gap_floor_note: str | None = None
        self.min_gap, self.max_gap = self._floored(min_gap, max_gap)
        self.batch_size = batch_size
        # The third number that reaches this class from `TELEGRAM_RESEARCH_CONFIG`
        # and the only one nothing checked. A NaN rest is `max(gap, nan)` on
        # every batch boundary and a NaN `sleep_cap`, which disarms the
        # "reservation from the future" repair the same way NaN disarmed the gap
        # floor -- every comparison against it is false.
        self.batch_rest = self._finite(batch_rest, DEFAULT_BATCH_REST, "batch rest")
        self.path = Path(state_dir) / f"pace-{host.replace(':', '_')}.json"
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Set on every wait(); a caller that wants to report honestly can read them.
        self.serialised_across_processes: bool = True
        self.last_warning: str | None = None
        if self.gap_floor_note:
            self._warn(self.gap_floor_note)
            self.last_warning = None      # a construction-time note is not a wait() one

    def _finite(self, value, fallback: float, what: str) -> float:
        """`value` as a finite number of seconds, or the shipped default, said aloud."""
        try:
            return float(config.want_finite_number({what: float(value)}, what))
        except (TypeError, ValueError):
            note = (
                f"a t.me {what} that is not a finite number ({value!r}) was "
                f"refused; using the shipped {fallback:g}s"
            )
            self.gap_floor_note = (
                f"{self.gap_floor_note}; {note}" if self.gap_floor_note else note
            )
            return fallback

    def _floored(self, min_gap, max_gap) -> tuple[float, float]:
        """Clamp a requested gap up to the shipped default, and say when it did.

        `NaN` is refused here with the same words as `"fast"`, and for a harder
        reason. `TELEGRAM_RESEARCH_CONFIG` is JSON and `json.loads` accepts the
        bare literal `NaN`, so `{"budgets": {"min_gap_sec": NaN}}` is a reachable
        input. Every comparison against NaN is false, so it walked through the
        floor untouched; `random.uniform(nan, nan)` is `nan`, `max(now, floor +
        nan)` is `now`, and the measurement was ten `wait()` calls in 0.067 s
        with `gap_floor_note` and `last_warning` both still None -- i.e. the
        accountless path firing at a host whose rate limit has never been
        measured, as fast as the socket allows, in silence. `Infinity` is the
        same value one `uniform` later.
        """
        try:
            low = config.want_finite_number({"gap": float(min_gap)}, "gap")
            high = config.want_finite_number({"gap": float(max_gap)}, "gap")
        except (TypeError, ValueError):
            low, high = DEFAULT_MIN_GAP, DEFAULT_MAX_GAP
            self.gap_floor_note = (
                f"a t.me gap that is not a finite number ({min_gap!r}, "
                f"{max_gap!r}) was refused; pacing at the shipped "
                f"{DEFAULT_MIN_GAP:g}-{DEFAULT_MAX_GAP:g}s"
            )
            return low, high
        if high < low:
            low, high = high, low
        if not self.enforce_gap_floor:
            return low, high
        if low < DEFAULT_MIN_GAP or high < DEFAULT_MAX_GAP:
            self.gap_floor_note = (
                f"a t.me gap of {low:g}-{high:g}s was refused and raised to the "
                f"shipped {DEFAULT_MIN_GAP:g}-{DEFAULT_MAX_GAP:g}s: this host's "
                "rate limit has never been measured, so the gap may be widened "
                "and never narrowed."
            )
            low = max(low, DEFAULT_MIN_GAP)
            high = max(high, DEFAULT_MAX_GAP)
        return low, high

    @property
    def sleep_cap(self) -> float:
        """How far into the future a reservation may legitimately stand.

        It has to be generous enough for a real queue -- with reservations, K
        contending processes push the last of them K gaps ahead -- and still
        bounded, so that a `last` a day in the future cannot sleep for a day.
        With the shipped defaults that is 60 s, which holds a queue of fifteen
        processes at the 4 s upper gap.
        """
        return max(self.batch_rest, self.max_gap * 10)

    # -- state file --------------------------------------------------------
    def _read(self) -> tuple[dict, bool]:
        """`(state, readable)`. `readable=False` means the file was there and bad."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"last": 0.0, "count": 0}, True     # first request ever: fine
        except OSError:
            return {"last": 0.0, "count": 0}, False
        try:
            state = json.loads(text)
        except ValueError:
            return {"last": 0.0, "count": 0}, False
        if not isinstance(state, dict):
            return {"last": 0.0, "count": 0}, False
        try:
            float(state.get("last", 0.0))
            int(state.get("count", 0))
        except (TypeError, ValueError):
            return {"last": 0.0, "count": 0}, False
        return state, True

    def _write(self, state: dict) -> None:
        # The temp file carries this process's pid and a serial. A fixed name
        # (`pace-t.me.tmp`) was shared by every process, and os.replace then
        # raised PermissionError on Windows while another process held it --
        # the exception escaped wait(), escaped fetch() and killed the run.
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{next(_TMP_SERIAL)}.tmp")
        try:
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            self._warn(f"pace state could not be written ({exc})")

    # -- cross-process lock ------------------------------------------------
    def _acquire(self) -> bool:
        deadline = time.time() + PACE_LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, str(os.getpid()).encode("ascii"))
                finally:
                    os.close(fd)
                return True
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > PACE_LOCK_STALE:
                    self._warn("a stale pace lock was removed")
                    try:
                        self.lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if time.time() >= deadline:
                    return False
                time.sleep(0.01)
            except OSError:
                return False

    def _release(self) -> None:
        try:
            self.lock_path.unlink()
        except OSError:
            pass

    def _warn(self, message: str) -> None:
        self.last_warning = message
        print(f"tgweb.Pacer: {message}", file=sys.stderr)

    # -- the reservation ---------------------------------------------------
    def _reserve(self) -> tuple[float, float]:
        """Claim the next firing instant. Returns `(due, gap)`."""
        self.last_warning = None
        locked = self._acquire()
        self.serialised_across_processes = locked
        try:
            state, readable = self._read()
            gap = random.uniform(self.min_gap, self.max_gap)
            count = (int(state.get("count", 0)) + 1) if readable else 1
            if self.batch_size and count % self.batch_size == 0:
                gap = max(gap, self.batch_rest)
            now = time.time()
            if not readable:
                self._warn(
                    "the pace state file could not be read — pacing a full gap "
                    "from now rather than assuming no request has been made"
                )
                floor = now
            elif not locked:
                self._warn(
                    "the pace lock could not be taken within "
                    f"{PACE_LOCK_TIMEOUT:g}s — this request is NOT serialised "
                    "against the other process; pacing a full gap from now"
                )
                floor = max(float(state.get("last", 0.0)), now)
            else:
                floor = float(state.get("last", 0.0))
            # A reservation standing further ahead than a queue could put it is
            # not a queue, it is a bad number -- a clock change, a state file
            # copied between machines. It used to be obeyed: a `last` one day
            # ahead made wait() sleep 86 402 s. Repaired here rather than
            # clamped at sleep time, so the bad value does not cost every
            # subsequent request as well.
            if floor > now + self.sleep_cap:
                self._warn(
                    f"the pace state names an instant {floor - now:.0f}s in the "
                    "future, which no queue can explain — ignoring it and "
                    "pacing a full gap from now"
                )
                floor = now
            due = max(now, floor + gap)
            self._write({"last": due, "count": count})
        finally:
            if locked:
                self._release()
        return due, gap

    def wait(self) -> float:
        """Sleep for as long as the gap requires. Returns the seconds slept.

        Bounded by construction: `_reserve` refuses a floor further ahead than
        `sleep_cap`, so this can never sleep longer than `sleep_cap + max_gap`.
        """
        due, _gap = self._reserve()
        now = time.time()
        if due <= now:
            return 0.0
        slept = due - now
        time.sleep(slept)
        return slept


# --------------------------------------------------------------------------
# The fetcher
# --------------------------------------------------------------------------
class TelegramWeb:
    """One-at-a-time reader of Telegram's public web surfaces.

    Every fetch optionally writes its raw body to disk before anything parses it.
    That file is what a quote is later checked against: a claim whose original
    page was never saved cannot be verified, and the spec requires the originals
    to be handed over.
    """

    def __init__(
        self,
        state_dir: Path,
        sources_dir: Path | None = None,
        pacer: Pacer | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = USER_AGENT,
        on_fetch=None,
        retry_backoff: float = RETRY_BACKOFF_BASE,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.sources_dir = Path(sources_dir) if sources_dir else None
        if self.sources_dir:
            self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.pacer = pacer or Pacer(self.state_dir)
        self.timeout = timeout
        self.user_agent = user_agent
        self.on_fetch = on_fetch          # callback(Response) for the fetch log
        # Seconds before the second attempt; doubles thereafter, capped at
        # BACKOFF_CAP. A parameter only so a test can drive the retry path
        # against a real server without waiting three minutes for it.
        self.retry_backoff = retry_backoff
        self.request_count = 0
        self.aborted_reason: str | None = None
        # WHICH surface said stop. A stop is sticky -- nothing may keep asking a
        # host that refused -- but it is sticky for that host only: `discover`
        # fetches lyzem through this same client, and a 503 or an interstitial
        # from a third-party search engine used to close Telegram down as well,
        # irreversibly and under a sentence that named Telegram as the refuser.
        # Measured: `discover --lyzem-query "captcha"` could not complete at all.
        self.aborted_host: str | None = None

    # -- low level ---------------------------------------------------------
    def fetch(
        self,
        url: str,
        *,
        follow: bool = False,
        save_as: str | None = None,
    ) -> Response:
        """Fetch one URL. Never concurrent, never silently retried on a refusal.

        Raises `RunAborted` when Telegram tells us to stop (429, a challenge),
        and `FetchFailed` when the status is neither success nor a signal we
        understand -- a 5xx that outlived its retries, or an unexpected 4xx. It
        never hands an error page back as an ordinary `Response`: that is how a
        502 used to reach the parsers, find no messages, and be reported as the
        end of a channel's history.
        """
        if self._blocked(url):
            raise RunAborted(self.aborted_reason)

        attempt = 0
        while True:
            self.pacer.wait()
            started = time.time()
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en,ru;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                },
            )
            handlers = [] if follow else [_NoRedirect()]
            opener = urllib.request.build_opener(*handlers)
            effective = None
            try:
                with opener.open(req, timeout=self.timeout) as fh:
                    raw = _read_body(fh, started + MAX_BODY_SECONDS)
                    status = fh.status
                    headers = {k.lower(): v for k, v in fh.headers.items()}
                    effective = _effective_url(fh)
                data = _decompress(raw, headers)
                body = _decode_text(data, headers)
            except urllib.error.HTTPError as exc:      # 3xx/4xx/5xx still carry a body
                try:
                    raw = _read_body(exc, started + MAX_BODY_SECONDS)
                    status = exc.code
                    headers = {k.lower(): v for k, v in exc.headers.items()}
                    effective = _effective_url(exc)
                    data = _decompress(raw, headers)
                    body = _decode_text(data, headers)
                except (http.client.HTTPException, urllib.error.URLError,
                        TimeoutError, OSError, TruncatedBody) as inner:
                    if self._failed_act(url, inner, attempt, started):
                        attempt += 1
                        continue
                    raise TelegramWebError(f"{url}: {inner}") from inner
            # `IncompleteRead` (the connection dropped mid-body) and a truncated
            # gzip stream (TruncatedBody, from `_decompress`) are exactly the
            # flaky network the retry path exists for. Neither is a URLError, and
            # before this both escaped fetch() raw, with no URL in the message.
            except (http.client.HTTPException, urllib.error.URLError,
                    TimeoutError, OSError, TruncatedBody) as exc:
                if self._failed_act(url, exc, attempt, started):
                    attempt += 1
                    continue
                raise TelegramWebError(f"{url}: {exc}") from exc

            resp = Response(
                url=url,
                status=status,
                body=body,
                headers=headers,
                location=headers.get("location"),
                bytes=len(data),
                elapsed_ms=int((time.time() - started) * 1000),
                wire_bytes=len(raw),
                url_effective=effective,
                attempt=attempt + 1,
            )
            self.request_count += 1

            # Saved BEFORE anything can raise. The page that aborts a run is the
            # single most useful page to have on disk, and it was the one page
            # the previous order guaranteed would never be written.
            #
            # The bytes written are the DECOMPRESSED transfer, not the body
            # re-encoded as UTF-8: on a page that is not UTF-8 the two differ,
            # and `notes/sources/` is the folder a `research` pass treats as the
            # original. A retried failure does not take the clean filename with
            # it either -- attempt 1's 502 used to land at `<label>.html` and the
            # real page at `<label>-2.html`, so the evidence folder held a
            # gateway error presented as the page.
            # The stop signal is decided BEFORE the save, and only so that the
            # filename can know about it -- the save still happens either way.
            # A challenge page served at 200 was taking the clean
            # `notes/sources/<label>.html` name, which is the file a `research`
            # pass reads as the original, and keeping it for ever: a later
            # successful re-run writes DIFFERENT content, so `_write_original`
            # gives the real page a numbered sibling rather than overwriting the
            # interstitial: nothing here overwrites an existing artefact.
            stop = stop_signal(resp)
            if save_as and self.sources_dir:
                label = _label_for(
                    save_as, status,
                    refused="challenge" if challenge_page(
                        body, url=effective or url) else None,
                )
                target = _write_original(self.sources_dir, label, data)
                resp.headers["x-saved-as"] = str(target)

            if stop:
                self.aborted_reason = f"{url}: {stop}"
                self.aborted_host = _host(url)
                self._log(resp)
                raise RunAborted(self.aborted_reason)

            if status not in OK_STATUSES:
                self._log(resp)
                if 500 <= status < 600 and self._retry(attempt):
                    attempt += 1
                    continue
                raise FetchFailed(
                    f"{url}: HTTP {status} — the surface could not be read. This is "
                    "a transport failure, not an empty result, and must never be "
                    "reported as 'nothing found'."
                )

            self._log(resp)
            return resp

    # -- accounting --------------------------------------------------------
    def _blocked(self, url: str) -> bool:
        """Has the surface this URL addresses already told this run to stop?

        Per host, not per client. Telegram saying stop must not be worked around
        by asking again, and a third-party surface saying stop must not close
        Telegram: those are two facts about two hosts, and one field held both.
        A reason set by hand -- with no host recorded -- blocks everything, which
        is what it used to do.
        """
        if not self.aborted_reason:
            return False
        return self.aborted_host is None or _host(url) == self.aborted_host

    def _log(self, resp: Response) -> None:
        if self.on_fetch:
            self.on_fetch(resp)

    def _failed_act(self, url: str, exc, attempt: int, started: float) -> bool:
        """Book one network act that produced no HTTP response, then say whether
        another attempt is due.

        `references/cli.md`'s run-folder table promises `fetchlog.jsonl` "one line per
        network act". A dropped connection, a timeout or a half gzip stream is a
        real request that reached t.me, and up to `MAX_RETRIES` of them happened
        per `fetch()` with `request_count` still 0, no `on_fetch` call and no log
        line -- while the 5xx path next door accounted for its retries exactly,
        which is what made the asymmetry invisible on inspection. A flaky link
        during a deep run put twice the requests on the wire that `run.json`
        admitted to, on the surface whose rate limit has never been measured.
        """
        resp = Response(
            url=url,
            status=0,
            body="",
            headers={},
            bytes=0,
            elapsed_ms=int((time.time() - started) * 1000),
            wire_bytes=0,
            attempt=attempt + 1,
            error=f"{type(exc).__name__}: {exc}",
        )
        self.request_count += 1
        self._log(resp)
        if not worth_retrying(exc):
            return False
        return self._retry(attempt)

    def _retry(self, attempt: int) -> bool:
        """Sleep and report True if another attempt is due, False if we are out."""
        if attempt + 1 >= MAX_RETRIES:
            return False
        delay = min(BACKOFF_CAP, self.retry_backoff * (2 ** attempt))
        if delay > 0:
            time.sleep(delay)
        return True

    # -- surfaces ----------------------------------------------------------
    def landing(self, username: str, *, save_as: str | None = None) -> Response:
        """`t.me/<name>` -- the metadata card. Works for channels AND groups."""
        return self.fetch(f"{BASE}/{_uname(username)}", follow=True, save_as=save_as)

    def preview(
        self,
        username: str,
        *,
        query: str | None = None,
        before: int | None = None,
        after: int | None = None,
        save_as: str | None = None,
    ) -> Response:
        """`t.me/s/<name>` -- 20 messages a page. Channels only; groups 302."""
        params: dict[str, str] = {}
        if query:
            params["q"] = query      # `q`, never `search` -- `search` is ignored
        if before is not None:
            params["before"] = str(before)
        if after is not None:
            params["after"] = str(after)
        url = f"{BASE}/s/{_uname(username)}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self.fetch(url, follow=False, save_as=save_as)

    def embed(
        self, username: str, message_id: int, *, save_as: str | None = None
    ) -> Response:
        """`t.me/<name>/<id>?embed=1` -- ONE message. The only group read path."""
        url = f"{BASE}/{_uname(username)}/{int(message_id)}?embed=1"
        return self.fetch(url, follow=True, save_as=save_as)


def worth_retrying(exc: BaseException) -> bool:
    """Could a second attempt at this transport failure succeed?

    The retry path exists for the flaky link: a timeout, a dropped connection, a
    half-arrived body. It used to take every transport error alike, including
    the two that are settled facts about this machine rather than about the
    network -- a name that does not resolve and a certificate that does not
    verify. Both cost three real attempts and two backoff sleeps (60 s + 120 s)
    before the same error came back, which on a run of any size is minutes of
    sleeping to re-learn what the first attempt established.

    Anything not recognised is retried: the safe direction here is to spend one
    more request, not to abandon a surface that would have answered.
    """
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        # urllib wraps the real error -- DNS and TLS both arrive this way.
        return worth_retrying(exc.reason)
    if isinstance(exc, socket.gaierror):
        return False                       # the name does not resolve; it will not
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False                       # the certificate will not verify either
    if isinstance(exc, BodyTooLarge):
        return False                       # it will be just as large next time
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hands the 3xx back instead of chasing it.

    The 302 on `/s/` is the measurement, not an obstacle: it is how a group and a
    nonexistent name announce themselves, and both announce themselves the same
    way -- which is why the type always comes from the landing page instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# --------------------------------------------------------------------------
# Classification of a body
# --------------------------------------------------------------------------
def stop_signal(resp: Response) -> str | None:
    """Return a sentence if this response means 'stop this surface', else None.

    Deliberately narrow. A refusal we understand -- a 302 on a group, a missing
    post -- is not a stop signal, it is data. A stop signal is the surface saying
    it no longer wants to talk, and the correct response to that is to stop and
    report, never to try again more politely.

    **Which surface said it is part of the message.** The sentences all read
    "from Telegram ... Run stopped", and `discover` fetches lyzem through the
    same `fetch`, so a 503 from a third-party search engine was reported as
    Telegram rate-limiting the run -- and, `aborted_reason` being sticky, every
    later t.me request in the process raised the same sentence without going
    near the network. `fetch` now closes only the surface that refused (see
    `TelegramWeb._blocked`), and the wording says which one that is.
    """
    telegram = from_telegram(resp.url_effective or resp.url)
    who = "Telegram" if telegram else (_host(resp.url_effective or resp.url) or "the surface")
    scope = "Run stopped" if telegram else "This surface stopped"
    if resp.status == 429:
        return f"HTTP 429 from {who} — rate limited. {scope}; nothing retried."
    if resp.status in (403, 503):
        # The status already carries the meaning here; the markers only choose
        # the wording, so prose is allowed to speak on this branch.
        if _challenge_marker(resp.body):
            return f"HTTP {resp.status} with a challenge page — blocked. {scope}."
        return f"HTTP {resp.status} from {who}. {scope}."
    if resp.status == 200 and resp.bytes < SUSPICIOUS_BODY_BYTES:
        return (
            f"HTTP 200 with only {resp.bytes} bytes — the surface answered but said "
            f"nothing. {scope} rather than treated as an empty result."
        )
    if challenge_page(resp.body, url=resp.url_effective or resp.url):
        return f"A challenge interstitial was served. {scope}."
    return None


def _host(url: str | None) -> str:
    """The hostname of a URL, lowercased. `""` when it has none to give."""
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:                          # a URL urlsplit cannot parse
        return ""


def from_telegram(url: str | None) -> bool:
    """Did this URL address t.me itself?

    A URL with no host at all counts as Telegram: it is a caller that built a
    `Response` by hand, and the old, Telegram-only behaviour is the safe answer
    for it. `endswith("t.me")` is deliberately not the test -- `nott.me` ends
    with it.
    """
    host = _host(url)
    return not host or host == "t.me" or host.endswith(".t.me")


def _marker_in(body: str, markers) -> str | None:
    low = body.lower()
    for marker in markers:
        if marker in low:
            return marker
    return None


def _challenge_marker(body: str) -> str | None:
    """The first thing in this body that reads like an interstitial, if any."""
    return _marker_in(body, CHALLENGE_MARKERS)


def challenge_page(body: str, *, url: str | None = None) -> bool:
    """Is this body an anti-bot interstitial rather than a Telegram page?

    Structural first, prose second, and in that order for a reason. At HTTP 200
    this used to be a whole-body substring test on user-controlled prose -- the
    exact defect class `search_found_nothing` and `post_missing` were both
    rewritten to eliminate. One post quoting a challenge page ("Just a moment,
    please enable JavaScript" is what a channel about scraping, bots or
    Cloudflare writes routinely, and what a `?q=cloudflare` search surfaces on
    purpose) made `fetch` raise `RunAborted` on a page carrying twenty real
    posts -- and `aborted_reason` is sticky, so every later fetch in the process
    raised too and the run reported "a challenge interstitial was served".

    An interstitial is not a t.me page: it has no `tgme_page_wrap` and no
    `data-post`. A body carrying either of those is Telegram answering, whatever
    its prose says. The `tgme_page_wrap` half matters as much as the
    `data-post` half -- a landing card carries no `data-post` either, so gating
    on messages alone would still let a channel's own description abort a run.

    **Off t.me, only the markup markers may answer.** Both structural guards are
    t.me's own markup, so on a third-party surface neither can ever fire and the
    test was back to being a substring search over prose -- on pages that are
    search results, i.e. arbitrary text chosen by the query. Measured:
    `discover --lyzem-query "captcha"` could not complete, because lyzem's
    results page for that word contains that word. `url` is how the caller says
    where the body came from; without it the body is treated as Telegram's, the
    behaviour every existing caller had.
    """
    if PAGE_WRAP in body or DATA_POST in body:
        return False
    if not from_telegram(url):          # `url=None` is Telegram, as it always was
        return _marker_in(body, CHALLENGE_MARKUP_MARKERS) is not None
    return _challenge_marker(body) is not None


def username_exists(landing_body: str) -> bool | None:
    """True / False from the landing page title; None if the title is unreadable.

    Only a peer card may answer. `t.me/tdlibchat/10000` serves
    `<title>Telegram: View @tdlibchat</title>` exactly like the landing page
    does, and this used to answer True off it -- the "not every 200 is a peer
    card" rule was applied to the `user` verdict and to nothing else. A body
    that is a t.me page but a SINGLE POST is not a card and cannot say whether
    the name is a readable channel or group, so it says None rather than
    guessing. `name_taken` still answers True for it: a post exists under the
    name, which settles the narrower question it asks.
    """
    if is_single_post_page(landing_body):
        return None
    m = re.search(r"<title>([^<]*)</title>", landing_body, re.I)
    if not m:
        return None
    title = m.group(1)
    if TITLE_EXISTS in title:
        return True
    if TITLE_MISSING in title:
        return False
    return None


def is_single_post_page(body: str) -> bool:
    """A t.me page showing one message rather than a peer's card."""
    return PAGE_WRAP in body and PAGE_POST in body


def peer_type(landing_body: str) -> str | None:
    """`channel` | `group` | `user` | None, read from the landing page.

    A channel's extra line reads `11 110 268 subscribers`; a group's reads
    `16 674 members, 362 online`. The two vocabularies do not overlap.

    `user` is the case neither the probe nor the spec recorded, and it is easy to
    get wrong: a personal account and a name nobody has taken produce the SAME
    `<title>Telegram: Contact @name</title>` and neither carries a
    `tgme_page_extra` at all. Measured live on 2026-08-24, they part on
    `og:title`: a real account puts the person's display name there, while a free
    name puts the literal string `Telegram: Contact @name`.

        @taken…      og:title = "<display name> | <their own tagline>"  -> user
        @zzqwx…      og:title = "Telegram: Contact @zzqwx…"            -> nothing

    It matters because a user account is not a readable source and must not
    reach the registry, but calling it "does not exist" is false and would send
    somebody looking for a typo that is not there.

    **A card whose extra line exists and cannot be read is None, not a person.**
    `subscriber` and `member` are English words; the `og:title` test below them
    is not a test for a personal account at all, it is "the title is not the
    literal `Telegram: Contact @name`", which every real channel's title also
    satisfies. So a channel card served in any other language fell through to
    `user` -- the one verdict this docstring says must not reach the registry --
    while `member_count` returned None at the same moment, leaving the members
    guard nothing to refuse it with. A personal account's card carries no
    `tgme_page_extra` at all (C02, and every contact card in the corpus), so
    stopping at an unreadable one costs the `user` verdict nothing.

    Two comments in this file and in `tgparse.py` insist that nothing may depend
    on the `Accept-Language: en,ru;q=0.9` header. This function did.
    """
    extra = _page_extra(landing_body)
    if extra is not None:
        low = extra.lower()
        if "subscriber" in low:
            return "channel"
        if "member" in low:
            return "group"
        return None
    if _og_title_is_a_real_name(landing_body):
        return "user"
    return None


def is_peer_card(body: str) -> bool:
    """Is this body a t.me profile card at all?

    Nothing used to ask. Any unexpected 200 body with an `og:title` that did not
    begin `Telegram: ` was classified `user` + `taken=True` -- telemetr.com,
    core.telegram.org's Bot API page and a t.me single-message URL all came back
    as "a personal account", which is precisely the verdict that makes a run
    drop a source quietly instead of reporting that something went wrong.
    """
    if PAGE_WRAP not in body:
        return False
    return PAGE_POST not in body       # a single post is a message, not a peer


def _og_title_is_a_real_name(landing_body: str) -> bool:
    if not is_peer_card(landing_body):
        return False
    m = re.search(
        r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', landing_body, re.I
    )
    if not m:
        return False
    value = m.group(1).strip()
    return bool(value) and not value.startswith("Telegram: ")


def name_taken(landing_body: str) -> bool | None:
    """Is this username claimed by anything at all -- channel, group or person?

    Broader than `username_exists`, which answers the narrower and more useful
    question of whether the name is a readable channel or group.
    """
    if username_exists(landing_body):
        return True
    if _og_title_is_a_real_name(landing_body):
        return True
    # A single-post page is not a peer card, so `username_exists` refuses it --
    # but a post served under this name is proof the name is claimed, which is
    # the only thing this function claims to know.
    if is_single_post_page(landing_body):
        m = re.search(r"<title>([^<]*)</title>", landing_body, re.I)
        if m and TITLE_EXISTS in m.group(1):
            return True
    return False


def member_count(landing_body: str) -> int | None:
    """The subscriber/member count as an integer, or None.

    Telegram writes it with non-breaking spaces as thousands separators, so the
    digits are gathered rather than parsed.
    """
    extra = _page_extra(landing_body)
    if extra is None:
        return None
    m = re.search(r"([0-9][0-9\s  ,\.]*)\s*(subscriber|member)", extra, re.I)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def online_count(landing_body: str) -> int | None:
    """The `N online` figure a group's card carries. Channels do not have one."""
    extra = _page_extra(landing_body)
    if extra is None:
        return None
    m = re.search(r"([0-9][0-9\s  ,\.]*)\s*online", extra, re.I)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


# The extra line, matched as a CLASS rather than as a whole attribute value.
# `class="tgme_page_extra"` demanded that this be the element's only class, so
# one added class -- a styling variant, an A/B test -- made every landing page
# read as carrying no extra line at all: `peer_type` fell through to `user`,
# `member_count` and `online_count` to None, and a channel of millions came back
# as `type: "user", members: null, exists: true`, which is the one verdict
# `peer_type` documents as unfit for the registry. `\b` on both sides is what
# keeps `tgme_page_extra_wide` from matching.
_PAGE_EXTRA_RE = re.compile(
    r"""class\s*=\s*["'][^"']*\btgme_page_extra\b[^"']*["'][^>]*>(.*?)</div>""",
    re.I | re.S,
)


def _page_extra(landing_body: str) -> str | None:
    m = _PAGE_EXTRA_RE.search(landing_body)
    if not m:
        return None
    return re.sub(r"<[^>]+>", " ", m.group(1))


def preview_available(resp: Response) -> bool:
    """Does `/s/<name>` actually serve this name?

    A 302 here means 'group, or no such name' -- the two are indistinguishable at
    this surface, which is why the type always comes from the landing page. T2
    recorded a `url_effective` rule for telling them apart; it did not reproduce
    on 2026-08-23 and must not be used.

    The message-wrap half is `_has_class`, not a substring of the document: this
    was the last classifier in the file still asking "does this run of
    characters occur anywhere", and it decides whether a walk stops. A post
    quoting the class name would have made an unreadable page look served -- and
    a served page look unreadable is the same defect from the other side, which
    is the `found: 0` in the shape of a real ending that this skill exists to
    never publish.
    """
    if resp.redirected:
        return False
    return resp.status == 200 and _has_class(resp.body, MSG_WRAP)


def search_found_nothing(body: str) -> bool:
    """The `?q=` surface's own 'no hits' marker, served at HTTP 200.

    This is the strongest positive claim the skill makes -- `SKILL.md` defines
    `found_nothing: true` as proven silence -- so it is the last place that may
    rest on a substring of user-controlled prose.

    Two mistakes have lived here, one in each direction:

    * `and MSG_WRAP not in body` used to be the second clause and cancelled the
      first out: Telegram serves the notice INSIDE a `tgme_widget_message_wrap`,
      so this returned False on every real zero-hit page. Confirmed live
      2026-08-24 on t.me/s/durov?q=zzqwxnonexistentterm12345.
    * With that clause removed it became a whole-body substring test, and a
      channel that DISCUSSES t.me's markup would assert its own silence:
      planting the literal `tme_no_messages_found` into one post's text on
      A01-s-durov (20 real posts) returned True. Twenty real posts, reported as
      a positive claim that nothing was said.

    So the test is structural in both halves. A page carrying even one real
    message cannot be a zero-hit page, whatever its prose says, and the marker
    has to be an element's class rather than a run of characters somewhere in
    the document. The real notice is
    `<div class="tgme_widget_message_centered"><div class="tme_no_messages_found">`;
    the centred wrapper is not required here, because one more class to match
    is one more class to rot without adding anything the `data-post` test does
    not already give.
    """
    if DATA_POST in body:
        return False
    return _has_class(body, NO_MESSAGES_FOUND)


def post_missing(embed_body: str) -> bool:
    """A missing message on `?embed=1`. HTTP is 200; the body says so in words.

    A gap is not the end of history: `birding_chats` was live at 29327 while 29320,
    10000, 50000 and 200000 were all missing on the same day.

    Both halves of the old test were wrong on the only surface that uses it.
    **No `?embed=1` page carries `tgme_widget_message_wrap` at all** -- not the
    9 with a real message, not the 7 with an error -- so `MSG_WRAP not in body`
    was always true and the test collapsed to "does the string `Post not found`
    occur anywhere, the post's own text and its reply quote included". An
    ordinary English sentence in a developer group ("the widget answers Post not
    found for deleted ids") made a live message read as an empty id: the run
    paid for the request and reported the post as absent.

    The structural test is the reverse of that. A page carrying `data-post` has
    a message on it and is never missing; a page with no `data-post` and the
    error class (or, failing that, the English words) is. A page with neither is
    NEITHER -- see `embed_unreadable`, which is the honest third answer and the
    one this function must not swallow.
    """
    if DATA_POST in embed_body:
        return False
    return _has_class(embed_body, ERR_MESSAGE) or POST_NOT_FOUND in embed_body


def embed_unreadable(embed_body: str) -> bool:
    """The third answer on `?embed=1`: no message, and no proof of absence.

    "This id is empty" and "this page is not one we understand" are different
    facts and a walk must not treat them alike. An empty id is data -- a gap,
    a deletion -- and the walk carries on counting misses. A page we cannot read
    is a front-end change, a join wall or an interstitial, and reporting a run
    of those as empty ids ends a walk with "the history stops here" about a
    group that is still talking.
    """
    return not (DATA_POST in embed_body or post_missing(embed_body))


def _has_class(body: str, name: str) -> bool:
    """Does any element in this body carry `name` as a class?

    A class attribute, not a substring of the document: the whole point is that
    a post whose TEXT contains `tme_no_messages_found` or `err_message` must not
    be able to answer a structural question about the page it sits on.
    """
    for m in re.finditer(r"""\bclass\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", body, re.I):
        value = m.group(1).strip("\"'")
        if name in value.split():
            return True
    return False


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _effective_url(handle) -> str | None:
    """Where the body in hand actually came from, if the handle will say."""
    for attr in ("url", "geturl"):
        value = getattr(handle, attr, None)
        if callable(value):
            try:
                value = value()
            except Exception:                       # noqa: BLE001 - never fatal
                value = None
        if isinstance(value, str) and value:
            return value
    return None


def _read_body(handle, deadline: float) -> bytes:
    """Read one body, bounded in bytes and in wall clock.

    Neither bound existed. `read()` with no argument buffers whatever the server
    sends, and the `timeout` on the socket bounds a single operation rather than
    the transfer, so a slow trickle is unbounded in both dimensions -- see
    MAX_BODY_BYTES for the 200 MB that measured it.

    Over-long and over-slow are both `TruncatedBody`: a body this module refused
    to finish reading is a broken transfer, which is what that exception means
    and what the retry path is for. What must never happen is the other thing --
    handing a partial body to the parsers, where a page with no markers in it
    reads as an empty surface, i.e. as absence.

    **Which is why the short read is checked explicitly.** `read()` with no
    argument raises `IncompleteRead` when the connection drops before the
    declared `Content-Length`; `read(n)` deliberately does not (http.client says
    so in a comment: "Ideally, we would raise IncompleteRead ... but it might
    break compatibility"), it simply returns less. Reading in chunks without
    this check would have converted a dropped connection into a short page --
    the exact failure the exception exists to prevent.
    """
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_BODY_BYTES:
        chunk = handle.read(min(READ_CHUNK_BYTES, MAX_BODY_BYTES + 1 - total))
        if not chunk:
            short = _unread_length(handle)
            if short > 0:
                raise TruncatedBody(
                    f"the connection ended {short} bytes short of the declared "
                    f"Content-Length ({total} bytes arrived). A part of a page "
                    "is not a page and must never reach a parser."
                )
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if time.time() > deadline:
            raise BodyTooLarge(
                f"the body was still arriving after {MAX_BODY_SECONDS:g}s "
                f"({total} bytes so far). The per-request timeout bounds one "
                "socket read, not the transfer; this is that bound."
            )
    raise BodyTooLarge(
        f"the body passed {MAX_BODY_BYTES} bytes and was not read further. The "
        "largest page in the probe corpus is 146 974 bytes, so nothing this "
        "module can parse is this size."
    )


def _unread_length(handle) -> int:
    """How much of the declared `Content-Length` never arrived.

    `http.client.HTTPResponse.length` is what is still owed on the body, and it
    is left standing when the connection drops. `urllib.error.HTTPError` proxies
    attribute lookups to the response it wraps, so both paths through `fetch`
    can be asked the same question; anything that cannot answer says 0, because
    a body with no declared length cannot be short.
    """
    length = getattr(handle, "length", None)
    if isinstance(length, int) and not isinstance(length, bool):
        return length
    return 0


def _capped(data: bytes, what: str) -> bytes:
    """`data`, or a `BodyTooLarge` saying it outgrew `MAX_BODY_BYTES`."""
    if len(data) > MAX_BODY_BYTES:
        raise BodyTooLarge(
            f"the {what} body expanded past {MAX_BODY_BYTES} bytes and was not "
            "decompressed further. A page this module can read is three orders "
            "of magnitude smaller; this one is not a page."
        )
    return data


def _inflate(raw: bytes, wbits: int) -> bytes:
    """zlib, stopped at `MAX_BODY_BYTES` of OUTPUT rather than of input.

    `zlib.decompress` raises on a stream that ends early; a `decompressobj` does
    not -- it hands back what it has and leaves `eof` False. Reading the flag is
    what keeps a half-arrived body from being decoded and parsed as a page, and
    it is raised as the `zlib.error` the caller already knows how to handle.
    """
    obj = zlib.decompressobj(wbits)
    data = _capped(obj.decompress(raw, MAX_BODY_BYTES + 1), "deflate")
    data = _capped(data + obj.flush(), "deflate")
    if not obj.eof:
        raise zlib.error("incomplete or truncated deflate stream")
    return data


def _decompress(raw: bytes, headers: dict[str, str]) -> bytes:
    """Undo Content-Encoding, or say the transfer was broken.

    A body whose declared compression will not decompress is not an empty page
    and it is not a page of garbage -- it is a truncated transfer. Passing the
    still-compressed bytes through `errors="replace"` (which is what this used
    to do for deflate) hands the parsers something with no `Post not found` and
    no message wrap in it, i.e. an "empty surface", i.e. absence.

    Only `gzip` and `deflate` are understood, and only those two are advertised
    in the request's `Accept-Encoding`, so a compliant server cannot send a
    `br` or `zstd` body. An unrecognised encoding is therefore not silently
    passed through as text -- that is the same "empty surface" failure by
    another road -- it is reported as the broken transfer it would be.

    **The output is bounded as well as the input.** `MAX_BODY_BYTES` on the wire
    says nothing about what comes out: 203 861 bytes of gzip expanded to 200 MB,
    and the expansion is where the memory went. Every branch below stops at the
    same ceiling and reports a `TruncatedBody` rather than returning the part it
    managed to inflate.
    """
    enc = (headers.get("content-encoding") or "").lower()
    if "gzip" in enc:
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as fh:
                return _capped(fh.read(MAX_BODY_BYTES + 1), "gzip")
        # EOFError is what a gzip stream cut in half raises, and it is neither
        # an OSError nor a zlib.error -- it used to escape fetch() unhandled.
        except (OSError, EOFError, zlib.error) as exc:
            raise TruncatedBody(f"gzip body could not be decompressed: {exc}") from exc
    if "deflate" in enc:
        try:
            return _inflate(raw, zlib.MAX_WBITS)
        except zlib.error:
            try:
                return _inflate(raw, -zlib.MAX_WBITS)
            except (zlib.error, EOFError) as exc:
                raise TruncatedBody(
                    f"deflate body could not be decompressed: {exc}"
                ) from exc
    known = {"", "identity", "none"}
    if enc.strip() not in known:
        raise TruncatedBody(
            f"the server answered with Content-Encoding: {enc!r}, which this "
            "module cannot decompress and never asked for. The bytes are not a "
            "page and must not be read as an empty one."
        )
    return raw


_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I
)


def _decode_text(data: bytes, headers: dict[str, str]) -> str:
    """Decode decompressed bytes to text, honouring whichever charset is declared.

    The `Content-Type` header wins, because a proxy that transcodes rewrites the
    header and not the document. Where the header is silent -- which is normal
    on the third-party surfaces, and legal -- the document's own
    `<meta charset=...>` is read. It used to be ignored outright: a
    `windows-1251` page that declared its encoding only in the document came
    back as a run of U+FFFD, with no flag anywhere on the `Response` and every
    Cyrillic word in it destroyed.

    Only the head of the document is sniffed, which is where the declaration is
    required to be and, more to the point, where it can be read without decoding
    the very bytes we are trying to decode.
    """
    charset = None
    m = re.search(r"charset=\s*[\"']?([\w\-.:]+)", headers.get("content-type", ""), re.I)
    if m:
        charset = m.group(1)
    if not charset:
        sniff = _META_CHARSET.search(data[:4096])
        if sniff:
            try:
                charset = sniff.group(1).decode("ascii")
            except UnicodeDecodeError:
                charset = None
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return data.decode(candidate, errors="replace")
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def _label_for(save_as: str, status: int, refused: str | None = None) -> str:
    """The filename an attempt gets. A failed one never takes the clean name.

    Attempt 1's 502 used to be written as `<label>.html` and the successful
    attempt 2 pushed to `<label>-2.html`, so `notes/sources/<label>.html` -- the
    folder a `research` pass reads as evidence -- held a gateway error page
    presented as the original. `Message.source_file` followed `x-saved-as` and
    still pointed at the right file, which is why nothing caught it.

    `refused` is the same rule for a body that is not a page even though its
    status says 200: an interstitial. Status alone could not see it, so
    `durov-q-bitcoin.html` in the evidence folder was a Cloudflare challenge
    with a name saying it was the search. Nothing overwrites it afterwards
    either -- a re-run's real page is different content and gets a numbered
    sibling -- so it kept the clean name permanently.

    A 200 that trips the SMALL-BODY signal is deliberately not covered: that
    body is the real answer this URL gave, there is no successful attempt to
    collide with, and `test_the_page_that_aborts_the_run_still_keeps_its_own_name`
    pins that on purpose.
    """
    if refused:
        suffix = re.sub(r"[^\w.-]+", "-", refused)
    elif status in OK_STATUSES:
        return save_as
    else:
        suffix = f"http{status}"
    stem, dot, ext = save_as.rpartition(".")
    if not dot:
        return f"{save_as}-{suffix}"
    return f"{stem}-{suffix}.{ext}"


def _uname(username: str) -> str:
    return urllib.parse.quote(username.lstrip("@").strip("/"), safe="")


# The names Windows reserves for devices. `con.html` is not a file there --
# it is the console -- and opening it for writing succeeds and writes nowhere,
# so a saved original for `t.me/con` (or `nul`, `aux`, `prn`, `com3`) vanished
# on that platform with no error anywhere. The skill is developed on Windows.
_WINDOWS_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"com{n}" for n in range(1, 10)]
    + [f"lpt{n}" for n in range(1, 10)]
)


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name)[:120] or "page"
    # The device name is claimed by the stem, extension or not: `con.html` too.
    if cleaned.partition(".")[0].lower() in _WINDOWS_DEVICE_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def _write_original(sources_dir: Path, save_as: str, payload: bytes) -> Path:
    """Write one fetched page and return the path it actually landed at.

    Two rules, both of them defects that were measured rather than imagined.

    **Binary, always.** `write_text` on Windows rewrites every LF as CRLF, so
    the saved "original" was not the page Telegram served and its size did not
    match the `bytes` in the fetch log. The originals are the evidence; they are
    written byte for byte -- and the bytes are the DECOMPRESSED transfer, not
    the decoded text re-encoded as UTF-8. Those two are the same thing on t.me,
    which serves UTF-8 on all 58 probes, and different on any third-party page
    that does not: a 631-byte `windows-1251` page landed on disk as 646 bytes of
    UTF-8 while the docstring above claimed it was byte for byte.

    **Never overwrite a different page.** The label comes from the query, and
    `read._slug` truncates it, so two related Russian queries in one run slugged
    to the same 24-character stem and the second page silently replaced the
    first -- after which every quote from the first query cited a file that did
    not contain it. A name already occupied by different content gets a numbered
    sibling, and the real path goes back to the caller (and into
    `Message.source_file`) instead of the label. Re-fetching the SAME page still
    overwrites itself, which is what a refetch should do.
    """
    sources_dir.mkdir(parents=True, exist_ok=True)
    base = _safe_name(save_as)
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    candidate = sources_dir / base
    for n in itertools.count(1):
        if not candidate.exists():
            break
        try:
            if candidate.read_bytes() == payload:
                break          # the same page again: overwriting is a no-op
        except OSError:
            pass
        suffix = f"-{n + 1}"
        candidate = sources_dir / (f"{stem}{suffix}.{ext}" if ext else f"{stem}{suffix}")
        if n > 500:            # a runaway loop is worse than a collision
            # `usedforsecurity=False` because this is a filename, not a
            # signature: on a FIPS-enforcing build `hashlib.sha1(...)` raises
            # outright, and that would turn a naming collision into a crash.
            digest = hashlib.sha1(payload, usedforsecurity=False).hexdigest()[:8]
            candidate = sources_dir / (f"{stem}-{digest}.{ext}" if ext else f"{stem}-{digest}")
            break
    with open(candidate, "wb") as fh:
        fh.write(payload)
    return candidate


def response_record(resp: Response) -> dict:
    """A fetch-log row. The body is never included -- only what it was."""
    d = asdict(resp)
    d.pop("body", None)
    d["headers"] = {
        k: v
        for k, v in resp.headers.items()
        if k in ("content-type", "location", "x-saved-as")
    }
    return d
