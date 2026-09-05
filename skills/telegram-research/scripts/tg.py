"""`tg.py` -- the command line the skill actually drives.

The division of labour, and it is the whole design: **this program performs acts,
the agent makes judgements.** Fetching a page, deciding whether a name is a group,
counting a resolve against a budget, appending a registry line -- mechanical, and
they belong in code where they are testable and cannot drift. Deciding which
posts answer the question, which word is really the local term for a bribe, when
a channel is off-topic -- judgements, and they stay with the agent.

Every subcommand prints JSON to stdout, on the way out AND on the way down. That
is what makes the skill usable from inside a subagent, which is the case it has
to survive: a subagent cannot ask a person anything, so a command that needed a
prompt would be a command that deadlocks -- and a command that answered a
mistyped path with a traceback would be one that reports nothing at all.

Exit codes, and each one means exactly one thing:

    0  the command did what it says
    2  usage: an unknown flag or subcommand (argparse's own)
    3  a stop signal: Telegram said stop, a declared ceiling fired, or Ctrl-C
    4  a lock is held by somebody else: the account, or the registry write guard
    5  a fetch failed in a way that is not a documented refusal
    6  wrong route: a group read as a channel, or the reverse
    7  operator error: a path, a missing file, a configuration
    8  the run folder did not pass its own acceptance gate
    9  internal error: a bug here, damaged state, or `selftest` disagreeing with
       the saved probes. A traceback goes to stderr; stdout still gets JSON
   10  refused: doing what you asked would destroy existing output. The message
       names the file and what is in it, and `--force` is the way to say it
       anyway

**1 is not in that table and must never be produced.** It used to be the code
for a damaged registry, an unreadable `queries.json`, a contended write guard
and every ordinary `AttributeError` -- all of them with a traceback on stdout's
channel and not one byte of JSON, which is the one thing a subagent cannot
recover from: it sees an empty string and cannot tell "crashed" from "found
nothing". Every exception now leaves through `main()` as JSON with a documented
code, and the traceback goes to stderr where it belongs.

Windows note: the program reconfigures stdout, stderr AND stdin to UTF-8
itself. It used to require `PYTHONIOENCODING=utf-8` and exit 1 with zero bytes
of output when the first emoji in a Telegram post met a cp1251 terminal -- after
the run folder was fully written, so the caller's obvious response was to
re-spend the whole network budget on a run that had already worked. stdin was
left out of that fix for a while, which silently turned every Russian note
piped in on a cp1251 console into mojibake, at exit 0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as config_module          # noqa: E402
import discover as discover_module      # noqa: E402
import querycraft as querycraft_module  # noqa: E402
import read as read_module              # noqa: E402
import account as account_module        # noqa: E402
import registry as registry_module      # noqa: E402
import resolve as resolve_module        # noqa: E402
import tgparse                          # noqa: E402
import tgweb                            # noqa: E402
import run as run_module                # noqa: E402
from run import (                       # noqa: E402
    Brief, NotARunFolder, Run, RunFolderError, now_local, read_jsonl,
    read_run_json, report_skeleton,
)

TOOL_VERSION = "1.1.0"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_STOPPED = 3
EXIT_ACCOUNT_BUSY = 4
EXIT_FETCH_FAILED = 5
EXIT_WRONG_ROUTE = 6
EXIT_OPERATOR = 7
EXIT_NOT_ACCEPTED = 8
# Everything that is nobody's fault at the keyboard: a bug here, a state file
# that cannot be read, a guard that cannot be taken. Kept separate from 7 so a
# caller can tell "you typed the wrong path" from "this program is broken".
EXIT_INTERNAL = 9
# The command was well-formed and was refused anyway, because carrying it
# out would have overwritten something somebody else wrote. Its own code and not
# 7, because nothing was typed wrong and there is a documented way to proceed:
# `--force`. **Never 1**: the interpreter returns 1 for an uncaught exception,
# so a deliberate 1 is indistinguishable from a crash, which is precisely the
# confusion this table exists to end.
EXIT_WOULD_DESTROY = 10

# What `emit()` must not touch. `redact_obj` blanks anything shaped like an
# api_hash, and an api_hash is shaped like any 32 hex characters -- a commit
# hash, a transaction id, or the middle of somebody's message. `posts.jsonl` has
# always been exempt for that reason; stdout was not, so the file and the
# terminal disagreed about what a post said and the terminal is what an agent
# reads and quotes. Our own fields stay redacted; fetched content passes.
STDOUT_PROTECTED = frozenset({
    "text", "messages", "description", "title", "reply_to_text",
    "author_name", "channel_title", "results", "posts",
    # `discover` returns the surrounding text a candidate name was found in, and
    # `queries` returns the corpus lines a mined term appeared in. Both are
    # fetched content like any other, and redacting them printed `<redacted>`
    # over ordinary hex in somebody else's page.
    "context", "snippets", "examples",
})

# Telegram's own rule, applied before a request is spent rather than after. A
# name with a slash or a Cyrillic letter cannot exist, and asking costs a GET
# out of a budget the whole skill exists to protect.
#
# ONE rule, imported rather than copied. There used to be two: `{2,31}` here and
# `{4,32}` in the registry, so `verify abc --write` passed this gate, spent a
# real GET, and was then refused by the registry inside the same command.
USERNAME_RE = registry_module.USERNAME_RE


class UsageError(RuntimeError):
    """The operator has to fix something. Always names what and where."""


# The run the current command is writing into, if any. `main()` needs it to
# record WHY a run stopped: `stop_reasons` was empty in every `run.json` ever
# written, including runs killed by a 429, so the report's "what limited this
# run" section could never appear.
_ACTIVE_RUN: Run | None = None
# Whether `_ACTIVE_RUN.finish()` has already run in this process. `main()`'s
# `finally` uses it so a command that succeeded is not written twice and a
# command that failed is written at all.
_RUN_PERSISTED: bool = False

# The console's own encoding, captured before we force UTF-8 on the streams. It
# is the only remaining evidence of what a pipe's bytes were meant to be, and
# `read_stdin_text` falls back to it.
_CONSOLE_ENCODING: str | None = None


def _use_utf8_stdout() -> None:
    """A Windows console can be cp1251 and Telegram posts are full of emoji.

    stdin is in the list because it was the half nobody fixed. `--from-file` is
    read with an explicit `encoding="utf-8"`; stdin was decoded with the console
    locale, so a UTF-8 Russian note piped in on a cp1251 console was decoded as
    cp1251, re-encoded as UTF-8 and written to `notes/<agent>.md` as mojibake --
    at exit 0, with a plausible byte count in the JSON. Measured 2026-08-25:
    `аренда квартиры` in, `Р°СЂРµРЅРґР° РєРІР°СЂС‚РёСЂС‹` on disk. Text with a
    byte cp1251 cannot map took the other road: surrogates, then a `write_text`
    that failed with exit 7 and left a 0-byte note behind.
    """
    global _CONSOLE_ENCODING
    if _CONSOLE_ENCODING is None:
        # Read BEFORE the reconfigure, or the fallback in `read_stdin_text`
        # would look at the encoding we just imposed and learn nothing.
        _CONSOLE_ENCODING = getattr(sys.stdin, "encoding", None)
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def read_stdin_text() -> str:
    """Whatever was piped in, decoded as UTF-8 and never as the console's guess.

    UTF-8 first because that is what every producer in this project emits; the
    console encoding second, because a note typed into a cp1251 `cmd` and
    redirected really is cp1251 and refusing it would be its own bug. `replace`
    last so a note is never lost to a byte, and never carries a surrogate into
    `write_text`.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:                       # a stream with no bytes underneath
        return sys.stdin.read()
    raw = buffer.read()
    if isinstance(raw, str):                 # a test double handing back text
        return raw
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    fallback = _CONSOLE_ENCODING or getattr(sys.stdin, "encoding", None) or "utf-8"
    try:
        return raw.decode(fallback)
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", "replace")


def _print_traceback() -> None:
    """The stack goes to stderr. stdout is the JSON channel and only that."""
    try:
        traceback.print_exc(file=sys.stderr)
    except Exception:                        # noqa: BLE001 -- reporting a crash
        pass


def emit(payload) -> None:
    """Everything leaves through here, redacted but not corrupted.

    Two rules meet at this line. Our own credential must never reach stdout, and
    fetched content must reach it exactly as fetched; `STDOUT_PROTECTED` is where
    the two are separated. The encoding fallback below is the difference between
    a garbled line and a successful run reporting total failure.
    """
    # `default=str` is the last line of the "JSON on the way down" promise: a
    # payload holding something json does not know how to render used to raise
    # inside the error handler itself, which turned a documented failure back
    # into a traceback with exit 1.
    text = json.dumps(
        config_module.redact_obj(payload, protect=STDOUT_PROTECTED),
        ensure_ascii=False, indent=2, default=str,
    )
    try:
        print(text)
        return
    except UnicodeEncodeError:
        pass
    _use_utf8_stdout()
    try:
        print(text)
        return
    except UnicodeEncodeError:
        pass
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8", "replace") + b"\n")
        buffer.flush()
    else:                                  # a stream with no bytes underneath
        print(text.encode("ascii", "backslashreplace").decode("ascii"))


# --------------------------------------------------------------------------
# The run folder
# --------------------------------------------------------------------------
def open_run(args) -> Run | None:
    """Wire a stage command into a run folder, if one was named.

    Without this every stage writes its pages somewhere and its network log
    nowhere, and `fetchlog.jsonl` -- the file that makes a citation checkable --
    stays empty. One flag, `--run`, connects the originals, the log and the
    counters to the same folder.

    `attach` rather than a fresh `Run`: the brief `newrun` wrote and the spend of
    every earlier command are loaded from disk first, so four commands in four
    processes add up to one run instead of the last one overwriting the other
    three.
    """
    run_dir = getattr(args, "run_dir", None)
    if not run_dir:
        return None
    # The run-folder check, extended to `--run` after the first repair pass
    # measured what was left open: the leaf was checked for existence and never
    # asked whether it was a run.
    # `tg.py --run <any existing directory> discover --text "@durov"`
    # answered exit 0 and created `run.json` and `notes/sources/` inside a
    # directory holding somebody else's file -- the same defect as `report
    # <stranger>`, one flag over.
    #
    # `allow_empty=True` is the difference from the positional commands: `--run`
    # may fill a directory that is empty, because there is nothing in it to
    # destroy and `finish()` stamps the marker on the way out. A missing path is
    # still refused, and an existing non-empty directory with no marker is now
    # refused too -- exit 2, nothing written.
    root = run_module.require_run_folder(run_dir, allow_empty=True, flag="--run")
    run = Run.attach(root)
    run.record_agent(getattr(args, "cmd", None) or "?")
    return track_run(run)


def track_run(run: Run | None) -> Run | None:
    """Register the run `main()` must persist even if the command dies.

    Commands that take their run folder as a positional (`queries`, `note`,
    `report`, `accept`) register here too, so the agent name they recorded
    survives a failure the same way a fetching command's counters do.
    """
    global _ACTIVE_RUN, _RUN_PERSISTED
    _ACTIVE_RUN = run
    _RUN_PERSISTED = False
    return run


def close_run(run: Run | None, extra: dict | None = None) -> None:
    """Persist the run's spend. Every command that opened one calls this.

    `verify` and `discover` used to call it only under `--write`, so a run made
    of those two had no `run.json` at all and the report that followed had no
    numbers to state.
    """
    global _RUN_PERSISTED
    if run is not None:
        run.finish(extra)
        if run is _ACTIVE_RUN:
            _RUN_PERSISTED = True


def _stop_run(reason: str) -> None:
    global _RUN_PERSISTED
    if _ACTIVE_RUN is not None:
        _ACTIVE_RUN.stop(reason)
        _ACTIVE_RUN.finish()
        _RUN_PERSISTED = True


def persist_active_run() -> None:
    """`run.json` survives the failure that ended the command.

    `Run.log_fetch` appends to `fetchlog.jsonl` at once but only increments an
    in-memory counter; `run.json` is written by `finish()`, and `finish()` was
    reached from the success path and from `RunAborted` and from nowhere else.
    So every exit 4/5/6/7 threw the spend away: the next command in the same run
    read `counters.requests = 0` and re-armed the ceiling from zero. Three
    `search` commands that each fetched 120 pages and then met a 502 spent 360
    requests against a ceiling of 133 that never once fired, and the
    report stated the run's network spend as 0 while `fetchlog.jsonl` held the
    real number.

    Called from a `finally`, so it must not be able to raise: a bookkeeping
    failure here would replace the real error with itself.
    """
    global _RUN_PERSISTED
    run = _ACTIVE_RUN
    if run is None or _RUN_PERSISTED:
        return
    _RUN_PERSISTED = True
    try:
        run.finish()
    except Exception:                        # noqa: BLE001 -- never mask the real error
        _print_traceback()


def harvest_partial(run: Run | None, exc) -> int:
    """Write the posts a stopped walk had already paid for.

    `read.py` hangs its harvest on the exception (`exc.partial`) precisely so
    the CLI can bank it. A stop signal is correct; throwing away thirty fetched
    posts on top of it is not, and the network cost has been spent either way.

    `read.py` catches `Exception`, so `FetchFailed` and `TelegramWebError`
    carry the same harvest a `RunAborted` does -- and for a while only the
    `RunAborted` branch called this. Under a 429 four pages of posts were banked
    and the command exited 3 saying `posts_banked: 4`; under a 502 on the fifth
    page the identical four pages were dropped, `posts.jsonl` stayed empty, and
    the run held originals in `notes/sources/` with no posts parsed out of them.
    The requests were paid either way.
    """
    partial = getattr(exc, "partial", None)
    messages = getattr(partial, "messages", None) or []
    if run is not None and messages:
        run.write_posts(messages)
    return len(messages)


def positive_int_flag(flag: str, why: str):
    """An argparse `type=` that refuses 0 and negatives, with a reason.

    A budget flag that accepts 0 or -5 fails OPEN, which is the wrong direction
    on the only brakes this skill has. `--max-requests 0` was dropped as falsy
    and the brief's 133/400/800 applied instead, to an operator who had typed 0
    meaning "spend nothing"; `--max-requests -5` was truthy, so the wrapper was
    reached and returned immediately on `ceiling <= 0` -- **the run-level brake
    was removed altogether**, silently. `--count` right next door has always
    refused 0 with a sentence; so do these now, at exit 2, before any request.
    """
    def parse(text: str) -> int:
        try:
            value = int(text)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"{flag} {text!r} is not a whole number")
        if value <= 0:
            raise argparse.ArgumentTypeError(f"{flag} {value} is not a budget. {why}")
        return value

    parse.__name__ = f"positive_int{flag.replace('-', '_')}"
    return parse


def row_limit(value, flag: str) -> int:
    """How many rows to show, refused when it would silently hide some.

    `--limit` and `--top` are plain `type=int` and both end up as
    `rows[:value]`, and Python reads a negative bound as "all but the last N":
    with four sources in the registry, `--limit -1` returned three of them
    beside `count: 4`, `--limit -3` returned one, `--limit 0` returned none.
    The `count` field kept reporting the true total, so the output looked
    complete -- a listing that quietly drops rows is worse than one that
    refuses, because nothing downstream can tell.

    Not argparse's `type=`, deliberately: argparse exits 2 with **zero bytes on
    stdout**, and every other refusal in this program is JSON a subagent can
    read. Same reason `--count 0` is refused inside `cmd_group` rather than by
    the parser.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise UsageError(f"{flag} {value!r} is not a whole number")
    if number <= 0:
        raise UsageError(
            f"{flag} {number} is not a number of rows. Python slices a negative "
            "bound as 'all but the last N', so this used to drop rows while "
            "`count` still reported the true total — the output looked "
            "complete and was not. Pass a positive number."
        )
    return number


positive_int = positive_int_flag(
    "--max-requests",
    "0 used to be dropped as falsy so the brief's ceiling applied anyway, and a "
    "negative value removed the run-level brake entirely instead of lowering it. "
    "Pass a positive number, or leave the flag off to use the brief's.",
)

def request_ceiling(cfg, run: Run | None, args) -> int:
    """How many requests this command may spend, and where the number came from.

    `--max-requests` beats the brief, the brief beats the config default. The
    config's own comment says "a run that wants more says so explicitly", which
    is exactly what the flag is.
    """
    explicit = getattr(args, "max_requests", None)
    if explicit is not None:
        return int(explicit)
    if run is not None:
        return int(run.brief.max_requests)
    return int(cfg.budgets.max_requests_per_run)


def _apply_request_ceiling(web, ceiling: int, spent: int = 0) -> None:
    """Make the declared ceiling real.

    `max_requests_per_run` was defined in `config`, printed into `brief.md` and
    read by nothing: one `group` command spent 674 requests past both it and the
    embed ceiling without a word in its output. Roughly 45 minutes of continuous
    requests, at the module's own pacing, against a host whose rate limit has
    never been measured.

    The override sits on the instance rather than inside `tgweb` because every
    surface method dispatches through `self.fetch`, so this one line covers
    `landing`, `preview` and `embed` alike, and because the ceiling belongs to
    a run rather than to the transport module, which knows nothing about runs.

    **It counts network acts, not `fetch()` calls.** One `fetch()` is one to
    three requests on the wire: a 5xx or a dropped connection is retried up to
    `MAX_RETRIES` times and every attempt reaches t.me. The wrapper used to keep
    its own tally and add 1 per call, so a declared ceiling of 800 permitted up
    to 2400 requests against a host whose rate limit has never been measured.
    `web.request_count` now counts acts and `run.log_fetch`
    increments the run's counter once per act too, so charging against
    `base + web.request_count` makes the flag mean what it says and makes the
    two counters agree in both directions.

    A single `fetch()` can still overshoot by its own retries -- the check is
    before the call and nothing below it can be interrupted -- but the overshoot
    is bounded by `MAX_RETRIES` and the next check sees it.
    """
    if not ceiling or ceiling <= 0:
        return
    inner = web.fetch
    base = int(spent)

    def fetch(url, *, follow=False, save_as=None):
        acts = base + int(getattr(web, "request_count", 0) or 0)
        if acts >= ceiling:
            raise tgweb.RunAborted(
                f"request ceiling of {ceiling} reached ({acts} network acts "
                f"already) before {url}; nothing was fetched. Raise it "
                "deliberately with --max-requests, or with a deeper --depth on "
                "newrun — never by accident."
            )
        return inner(url, follow=follow, save_as=save_as)

    web.fetch = fetch


def _fetch_hook(run: Run | None, extra_dir: Path | None):
    """The `on_fetch` callback: log the act, and mirror the page if asked.

    `--save-to` ADDS a destination, it never replaces the run's. It used to
    replace it, which meant the only way to get a searched page into
    `<run>/notes/sources/` was to spell that exact path into a flag `SKILL.md`
    never mentioned.
    """
    if run is None and extra_dir is None:
        return None

    def hook(resp) -> None:
        if extra_dir is not None:
            saved = (resp.headers or {}).get("x-saved-as")
            if saved and Path(saved).exists():
                extra_dir.mkdir(parents=True, exist_ok=True)
                target = extra_dir / Path(saved).name
                if Path(saved).resolve() != target.resolve():
                    shutil.copyfile(saved, target)      # bytes, not text
        if run is not None:
            run.log_fetch(resp)

    return hook


def ids_seen_of(res) -> int:
    """How many ids a read accounted for, from the reader's own count.

    `read.py` keeps `ReadResult.ids_seen` across the whole walk, which is the
    number to print: it is counted where the pages are, so it sees an id that
    two pages both carried exactly once. `ids_covered` below is the fallback for
    a result that carries no such field -- a partial harvest hung on an
    exception, or a test double.
    """
    seen = getattr(res, "ids_seen", None)
    if isinstance(seen, int) and seen > 0:
        return seen
    return ids_covered(getattr(res, "messages", None) or [])


def ids_covered(messages) -> int:
    """How many message ids a read accounted for.

    Not the same number as `found`, and the difference is the whole point of
    `Message.ids`. An album is ONE post with one permalink and one block on the
    page, and several ids: verified live on `t.me/s/nexta_tv`, where a page
    carrying ids 27033-27052 published only 18 `data-post` attributes, with
    27043 and 27044 present as `?single` links inside the grouped wrapper. So
    `found` answers "how many posts came back" and this answers "how many ids
    were seen", and a walk that returns 18 of the 20 ids it asked about is no
    longer indistinguishable from one that lost two.

    Falls back to one id per message wherever the parse does not carry `ids`,
    so this is exactly `found` on a page with no albums on it.
    """
    total = 0
    for msg in messages or []:
        ids = getattr(msg, "ids", None)
        if not ids and isinstance(msg, dict):
            ids = msg.get("ids")
        total += len(ids) if ids else 1
    return total


def root_arg(args) -> Path | None:
    r"""`--root` as an absolute path, or None to let `config` decide.

    The flag used to default to `"."`, so **where a run folder landed depended
    on which directory the command was started in**. Measured 2026-08-25, the
    same command in two shells:

        from a scratch directory  -> telegram-runs\<slug> created there
        from the project root     -> the same relative string, inside the project
        from the skill root       -> a whole run inside
                                     .claude\skills\telegram-research\telegram-runs\...

    `references/cli.md`'s promise that a run lives under `<root>/telegram-runs/` was
    therefore true only by accident of `cwd`, and the `run` path it printed
    -- a bare relative string -- was valid only from that same shell, which is
    what the `next:` line hands to the next command. This is the same class as
    the state directory following the shell: a path nobody typed, silently
    taken from the environment the process happened to start in.

    None means "not given", and `config.load(None)` anchors on `repo_root()` --
    found from the skill's own file, never from the shell. A value that IS given
    is the operator speaking, so it keeps ordinary command-line semantics
    (relative to the shell) and is made absolute here, once, so that everything
    printed downstream works from anywhere.
    """
    value = getattr(args, "root", None)
    if value is None or not str(value).strip():
        return None
    return config_module.env_path(str(value))


def check_output_dir(path: Path, flag: str) -> Path:
    """A destination we can actually write to, named by the flag that set it."""
    if path.exists() and not path.is_dir():
        raise UsageError(f"{flag} {path} is a file, not a directory")
    if not path.exists() and not path.parent.exists():
        raise UsageError(f"{flag} {path}: its parent {path.parent} does not exist")
    return path


def build_web(cfg, run=None, *, args=None, ceiling: int | None = None):
    save_to = Path(args.save_to) if (args and getattr(args, "save_to", None)) else None
    if save_to is not None:
        check_output_dir(save_to, "--save-to")
    primary = run.sources_dir if run is not None else save_to
    extra = save_to if (run is not None and save_to) else None
    pacer = tgweb.Pacer(
        cfg.pace_dir,
        min_gap=cfg.budgets.min_gap_sec,
        max_gap=cfg.budgets.max_gap_sec,
        batch_size=cfg.budgets.batch_size,
        batch_rest=cfg.budgets.batch_rest_sec,
    )
    web = tgweb.TelegramWeb(
        state_dir=cfg.pace_dir,
        sources_dir=primary,
        pacer=pacer,
        on_fetch=_fetch_hook(run, extra),
    )
    if ceiling is None and args is not None:
        ceiling = request_ceiling(cfg, run, args)
    if ceiling is not None and int(ceiling) <= 0:
        # `--max-requests` cannot get here any more (argparse refuses it), but a
        # brief file or a config override can still carry a nonsense ceiling,
        # and the old code answered that by running with NO ceiling at all.
        raise UsageError(
            f"a request ceiling of {ceiling} would let this command spend "
            "nothing, and the way that used to be handled was to remove the "
            "brake instead. Fix max_requests in the brief or the config."
        )
    if ceiling:
        _apply_request_ceiling(
            web, ceiling, spent=(run.counters.get("requests", 0) if run else 0)
        )
    return web


# What the four truncating limits are called in `ReadResult.stop_reason`, and
# what an operator has to change to get past each one.
_TRUNCATION = {
    "count": "--count was satisfied",
    "page_ceiling": "--max-pages was reached",
    "miss_tolerance": "--max-misses was reached",
    "request_ceiling": "the request ceiling fired",
    "no_messages": "a page came back with no messages on it",
    "aborted": "the walk was interrupted",
}


def stale_cursor_note(known: dict | None) -> str | None:
    """Say so when the registry cannot vouch for the cursor it just handed over.

    `Registry.load` skips a corrupt line, and the writer sorts keys, so a line
    truncated mid-write loses `username` first and keeps `max_id_seen`: the mark
    exists, cannot be attributed, and is therefore not applied. The registry
    flags every cursor as suspect instead (`cursor_may_be_stale`) and that flag
    has to reach the operator, because the failure it describes is silent and
    expensive -- a cursor rewound from 91234 to 120 re-fetches 91114 messages on
    the surface that costs one GET each.
    """
    if not (known or {}).get("cursor_may_be_stale"):
        return None
    return (
        "the registry holds a line it could not read, and a truncated line loses "
        "its username before it loses its max_id_seen — so the stored cursor "
        "may be older than what some run actually reached. Check "
        "`tg.py registry stats` before trusting --since-last here."
    )


def cursor_verdict(res) -> tuple[bool, str | None]:
    """May this walk's highest id become the registry's cursor?

    Only if the walk reached an end it can prove: it caught up with the stored
    `until_id`, it saw the first post, or it ran out of material by itself.
    Anything else is a WINDOW, and a high-water mark written after a window
    makes everything below that window unreachable forever -- `registry._MERGE_MAX`
    guarantees a cursor can only go up, so there is no way back.

    Measured 2026-08-25: a group at cursor 1000 with ids 1001-1500 new, read
    with `--count 50 --since-last --write`, stored 1500 and hid 1001-1450 from
    every future run, which then answered `found: 0, reached_until_id: true` --
    glossed in `references/cli.md` as "caught up with stored work". A channel over 500
    posts did the same on the default `--max-pages 25`. The walk KNEW: it
    returned `stop_reason: "count"`, `exhausted: False`. The cursor was written
    anyway.

    Returns `(settled, why_not)`; `why_not` names the limit, because a run that
    silently declines to write a cursor is only marginally better than one that
    writes a wrong one.
    """
    settled = bool(
        getattr(res, "reached_until_id", False)
        or getattr(res, "reached_first_post", False)
        or getattr(res, "exhausted", False)
    )
    if settled:
        return True, None
    reason = getattr(res, "stop_reason", None) or "an undeclared limit"
    named = _TRUNCATION.get(reason, reason)
    detail = getattr(res, "stopped_early", None)
    return False, (
        f"stop_reason={reason}: {named}"
        + (f" ({detail})" if detail else "")
        + ". The walk stopped on a limit, not at an end it can prove, so the "
        "highest id it saw is the top of a window and not a cursor. Writing it "
        "would put everything between the old cursor and that window out of "
        "reach of --since-last permanently."
    )


def saving_prefix(args, run) -> str | None:
    """The label that makes a page get written to disk.

    `search`, `history` and `group` are the three commands that produce quotable
    posts, and they were the three that saved nothing: the label was gated on
    `--save-to`, so `--run` alone archived the fetch log and not one page behind
    it. `notes/sources/` is what makes a quotation checkable and a citing
    pass will not accept a claim without it.
    """
    return args.username if (run is not None or getattr(args, "save_to", None)) else None


def get_registry(cfg) -> registry_module.Registry:
    cfg.ensure_dirs()
    return registry_module.Registry(cfg.registry_path)


def get_classifier(cfg):
    """The topic vocabulary, or None -- and never silently None.

    `config` applies the shipped default only when nothing is configured, so a
    typo in an override's `topics_vocabulary` both loses the override AND
    suppresses the default: every source admitted from then on carried no
    topics, and nothing anywhere said the vocabulary had not been loaded. The
    caller emits `topics_vocabulary_missing` so the silence has a name.
    """
    if not cfg.topics_vocabulary:
        return None, None
    path = Path(cfg.topics_vocabulary)
    if not path.exists():
        return None, (
            f"topics_vocabulary points at {path}, which does not exist: no "
            "source verified by this command carries topics, and the shipped "
            "default was suppressed by the override rather than replaced by it"
        )
    return registry_module.TopicClassifier.from_file(path), None


def check_username(name: str) -> str:
    """Telegram's own syntax, checked before a GET is spent on it."""
    cleaned = (name or "").strip().lstrip("@").strip("/")
    if not registry_module.valid_username(cleaned):
        raise UsageError(
            f"{name!r} cannot be a Telegram username: "
            f"{registry_module.USERNAME_RULE}. Refused before spending a "
            "request on it."
        )
    return cleaned


def require_run_folder(path) -> Path:
    """A run folder that says it is one, or a refusal that wrote nothing.

    One implementation, in `run.py`, for all four commands that take a run
    folder as a positional (`report`, `accept`, `queries`, `note`). The version
    that lived here asked only "non-empty string, exists, is a directory", so
    `report <any existing directory>` created `report.md`, `run.json` and
    `notes/sources/` inside it and answered `ok: true` with exit 0 -- the exact
    behaviour its own docstring claimed to have fixed, surviving on the more
    likely typo. The refusal is `run.NotARunFolder` and `dispatch` maps it to
    EXIT_USAGE.
    """
    return run_module.require_run_folder(path)


# --------------------------------------------------------------------------
# verify -- the gate every candidate passes
# --------------------------------------------------------------------------
def cmd_verify(args, cfg) -> int:
    """One free GET per name: does it exist, and is it a channel or a group?

    Nothing may reach a resolve without passing here first. A resolve spent on a
    name that does not exist costs exactly as much as one that works, out of the
    budget whose exhaustion once cost ten hours.
    """
    names = [check_username(n) for n in args.usernames]
    active_run = open_run(args)
    web = build_web(cfg, active_run, args=args)
    reg = get_registry(cfg)
    classifier, vocabulary_warning = get_classifier(cfg)
    rules = registry_module.AdmissionRules(
        min_channel_members=args.min_channel_members,
        min_group_members=args.min_group_members,
    )
    cards, results = [], []
    for name in names:
        acts_before = int(getattr(web, "request_count", 0) or 0)
        try:
            card = discover_module.verify(web, name)
        except tgweb.RunAborted as exc:
            _stop_run(str(exc))
            emit({"ok": False, "stopped": str(exc), "verified": results})
            return EXIT_STOPPED
        entry = card.as_dict()
        # `type_confirmed` is a claim about EVIDENCE: this type was read off
        # a page fetched by this command, just now. Measured rather than
        # assumed, so a cache anywhere below this line can never be mistaken for
        # a read: if the network act did not happen in this call, the flag is
        # not set, whatever the card says.
        if card.type and int(getattr(web, "request_count", 0) or 0) > acts_before:
            entry["type_confirmed"] = True
        if args.probe_preview and card.type == "channel":
            entry["preview"] = discover_module.probe_preview(web, name)
        cards.append(entry)
        results.append(entry)
    report = None
    corrections: list[dict] = []
    if args.write:
        stored_before = reg.load()
        report = discover_module.admit(
            reg, cards, rules=rules, classifier=classifier,
            found_via=args.found_via, lang=args.lang, geo=args.geo,
        )
        corrections = confirm_types(reg, cards, stored_before)
        if active_run:
            # `report.admitted` names what `admit` really wrote, in write order
            # and after its duplicate merge. `registry-delta.jsonl` is described
            # to the reader as "sources this run added or refreshed", and it was
            # built from every record whose name merely APPEARED in this batch --
            # so a candidate the admission rules REFUSED, but that some earlier
            # run had already admitted, was listed as though this run had
            # vouched for it. The first repair established the same fact by
            # loading the registry either side of `admit` and diffing: exact,
            # but it re-read a growing file twice and could still miss a record
            # rewritten identically inside the same second. `admit` knew all
            # along; now it says.
            loaded = reg.load()
            keys = [(name or "").lstrip("@").lower() for name in report.admitted]
            active_run.write_delta([loaded[k] for k in keys if k in loaded])
    close_run(active_run)
    emit({
        "ok": True,
        "verified": results,
        "requests": web.request_count,
        "admission": (report.__dict__ if report else None),
        "type_corrections": corrections,
        "topics_vocabulary_missing": vocabulary_warning,
        "run": str(active_run.root) if active_run else None,
    })
    return EXIT_OK


def confirm_types(reg, cards, stored_before: dict) -> list[dict]:
    """Let a page read in THIS call correct a type the registry got wrong.

    `registry._merge` refuses to change `type` on a contradiction -- it records
    a `type_conflict` and says "the stored type stands; re-verify with
    type_confirmed to correct it". That refusal is right: `type` decides the
    entire read route, and a rate-limit interstitial or a misread must not be
    able to flip a verified channel to `group` from a command that prints
    `ok: true`. But **nothing in this skill had ever passed `type_confirmed`**,
    so the escape hatch the message names did not exist: a record typed wrong
    once -- and `group --write` used to stamp `type: group` from the command's
    own NAME, on the official @telegram channel -- was uncorrectable for ever,
    with `search` and `history` refusing it at exit 6 in every future run, in a
    registry every project shares.

    Only a contradiction is written, and only from a card whose type came off a
    page fetched in this call. A name whose stored type already agrees needs no
    correction line, and a name the registry does not know yet is `admit`'s job.
    """
    out: list[dict] = []
    for entry in cards:
        if not entry.get("type_confirmed") or not entry.get("type"):
            continue
        username = (entry.get("username") or "").lstrip("@")
        stored = stored_before.get(username.lower()) or {}
        if not stored.get("type") or stored.get("type") == entry["type"]:
            continue
        record = reg.append({
            "username": username,
            "type": entry["type"],
            "type_confirmed": True,
            "status": entry.get("status") or stored.get("status") or "alive",
        })
        out.append({
            "username": username, "was": stored.get("type"),
            "now": entry["type"], "at": record.get("last_checked"),
            "note": "the stored type was corrected because this command read "
                    "the landing page itself; the previous value is still in "
                    "the registry's own log",
        })
    return out


# --------------------------------------------------------------------------
# discover -- candidates out of text, and out of lyzem
# --------------------------------------------------------------------------
def ate_claim(kinds: dict) -> str:
    """lyzem's own result counts, one per mode it was asked.

    Printed as the several numbers they are rather than summed. lyzem matches its
    words by OR, so each of these counts a union -- every block carrying ANY word
    of the query rather than the query -- and adding three unions together would be
    arithmetic on top of a number that already means nothing about the query.
    """
    return ", ".join(f"{kind}={row.get('claimed')}" for kind, row in kinds.items()) or "no"


def _discover_by_account(cfg, query: str, result, active_run=None) -> dict:
    """`contacts.search` as a discovery channel. One call, zero resolves.

    Its blind spot is the sharpest of the three and has to be stated wherever it
    is used: **it never sees inside a message.** The literal text of a real post
    in a real group returned zero entities, and the best group for a housing
    question in one city was missed entirely because its title carries only that
    city, its country and the word for "chat" -- and nothing of the subject. So
    this runs beside web search and lyzem, never instead of them.

    The peers land in the cache with their access hashes, which is what makes the
    group searchable afterwards without a resolve.
    """
    transport = _open_account(cfg)
    try:
        with account_module.AccountSession(
            transport, cfg=cfg, dry_run=False, allow_live=True
        ) as session:
            found = session.search_contacts(query)
            calls = session.account_calls
    finally:
        transport.close()
    if active_run and calls:
        active_run.count("account_calls", calls)
    if found.get("stopped"):
        return {"query": query, "calls": calls,
                "stopped": found["stopped"], "peers": []}
    peers = found.get("peers", [])
    for row in peers:
        result.add(discover_module.Candidate(
            row["username"], "account",
            context=" — ".join(str(x) for x in (row.get("title"),
                                                 row.get("type")) if x),
        ))
    return {
        "query": query,
        "calls": calls,
        "resolves": 0,
        "peers_cached": int(found.get("peers_cached", 0)),
        "peers": [{k: row.get(k) for k in
                   ("username", "type", "title", "participants", "verified", "scam")}
                  for row in peers],
        "blind_spot": ("contacts.search matches titles and usernames only; it "
                       "cannot see inside a message, so a group whose title does "
                       "not name the subject is invisible to it"),
    }


def cmd_discover(args, cfg) -> int:
    """Turn text into candidate usernames. No verification happens here.

    The searching itself is the agent's job: it has web search and this program
    does not. What the program owns is extraction, deduplication and the record
    of which discovery channel produced what -- because the stage is only
    finished when two channels of a DIFFERENT nature have both spoken, and that
    is a bookkeeping question, not a judgement.
    """
    result = discover_module.DiscoveryResult()
    # Everything checkable is checked before a request is spent. The paid GET
    # used to come first: a `--snippets-to` whose parent did not exist, or a
    # mistyped `--from-file`, raised after the lyzem page had been fetched and
    # before `emit`, so the request was spent AND its candidates thrown away.
    # `check_username` refuses "before spending a request on it"; so does this.
    sources = [Path(p) for p in (args.from_file or [])]
    for source in sources:
        if not source.exists():
            raise UsageError(f"--from-file {source} does not exist")
    snippets_to = Path(args.snippets_to) if args.snippets_to else None
    if snippets_to is not None:
        if snippets_to.exists() and snippets_to.is_dir():
            raise UsageError(f"--snippets-to {snippets_to} is a directory, not a file")
        if not snippets_to.parent.is_dir():
            raise UsageError(
                f"--snippets-to {snippets_to}: its folder {snippets_to.parent} "
                "does not exist"
            )

    active_run = open_run(args)
    web = None
    lyzem_notes: list[str] = []
    lyzem_kinds: dict[str, dict] = {}
    account: dict = {}

    if args.lyzem_query:
        web = build_web(cfg, active_run, args=args)
        snippets: list[str] = []
        # Every mode, not just `f=messages`. The `kind` parameter existed in
        # `lyzem_url` from the first day and no caller ever set it, so the one
        # discovery channel that can answer "which GROUPS talk about this" was
        # permanently asking "which posts contain these words", by OR, over an
        # index a third of whose names are dead. See `discover.LYZEM_KINDS`.
        for kind in args.lyzem_kind or list(discover_module.LYZEM_KINDS):
            url = discover_module.lyzem_url(args.lyzem_query, kind=kind)
            try:
                resp = web.fetch(url, follow=True,
                                 save_as=f"lyzem-{kind}-{args.lyzem_query}.html")
            except tgweb.RunAborted as exc:
                # The 429 that used to leave this command as a traceback with
                # exit 1, while the identical signal one phase later produced
                # JSON and exit 3. The candidates from the modes that DID answer
                # are kept: they were paid for.
                _stop_run(str(exc))
                emit({"ok": False, "stopped": str(exc),
                      "candidates": [c.__dict__ for c in result.ranked()],
                      "lyzem_kinds": lyzem_kinds})
                return EXIT_STOPPED
            # `parse_lyzem` counts every way its answer can come back short --
            # the page-size parameter renamed under us (measured: 4 peers
            # instead of 33 on one query), a short page over an index that
            # claims more, blocks that carry no `t.me` link at all -- and writes
            # them to stderr. stdout is what a subagent reads, so they come out
            # here too: a cut nobody is told about is the defect these counters
            # were added to end.
            cands, page_snippets, claimed = discover_module.parse_lyzem(
                resp.body, args.lyzem_query, notes=lyzem_notes)
            snippets.extend(page_snippets)
            for cand in cands:
                result.add(cand)
            lyzem_kinds[kind] = {"candidates": len(cands), "claimed": claimed}
        for note in lyzem_notes:
            result.note(note)
        result.notes.append(
            f"lyzem claims {ate_claim(lyzem_kinds)} results for "
            f"{args.lyzem_query!r} across {len(lyzem_kinds)} modes; that is its "
            "index, not Telegram. It is never evidence of absence."
        )
        if snippets_to is not None:
            snippets_to.write_text("\n\n---\n\n".join(snippets), encoding="utf-8")

    if args.account_query:
        # Channel three, and the only one that talks to Telegram itself. One
        # call, zero resolves -- and the peers it returns are cached with their
        # access hashes, so a group it finds here is searchable afterwards for
        # nothing. That second effect is the whole reason the resolve is gone
        # from the ordinary path; see `_group_peer`.
        account = _discover_by_account(cfg, args.account_query, result, active_run)
        if account.get("stopped"):
            close_run(active_run)
            emit({"ok": False, "stopped": account["stopped"],
                  "candidates": [c.__dict__ for c in result.ranked()],
                  "account": account})
            return EXIT_STOPPED

    # `dropped` is what makes the NOT_A_SOURCE filter audible. The module states
    # its own contract twice -- "nothing is discarded silently" -- and this was
    # the one caller that never passed the list, so a text mentioning
    # `@telegram` (a real channel with millions of subscribers, and the obvious
    # source for any question about Telegram itself) lost it without a word.
    dropped: list[str] = []
    for source in sources:
        text = source.read_text(encoding="utf-8", errors="replace")
        for cand in discover_module.candidates_from_text(
            text, args.found_via, dropped=dropped
        ):
            result.add(cand)

    if args.text:
        for cand in discover_module.candidates_from_text(
            args.text, args.found_via, dropped=dropped
        ):
            result.add(cand)

    for reason in dropped:
        result.note(reason)

    close_run(active_run)
    emit({
        "ok": True,
        "candidates": [c.__dict__ for c in result.ranked()],
        "channels_used": sorted(result.channels_used),
        "corroborated": result.corroborated,
        "notes": result.notes,
        # Separate from `notes` on purpose: `notes` is prose for a reader and
        # this is the machine-readable answer to "did this stage come back thin
        # because the index is thin, or because something cut it".
        "silent_cuts": lyzem_notes,
        "dropped": result.dropped,
        # Which lyzem mode produced what. A mode that came back empty is a fact
        # about that mode, and it used to be invisible because only one mode was
        # ever asked.
        "lyzem_kinds": lyzem_kinds,
        "account": account or None,
        "requests": web.request_count if web else 0,
        "run": str(active_run.root) if active_run else None,
        "next": "verify every candidate with `tg.py verify` before anything else "
                "touches it",
    })
    return EXIT_OK


# --------------------------------------------------------------------------
# search -- one command, and the source's type picks the surface
# --------------------------------------------------------------------------
# Telegram caps `messages.search` at 100 per call, so a whole page is the
# cheapest unit there is: `server_total` divided by this is what a complete
# answer costs, and no smaller number buys anything.
MTPROTO_PAGE = 100


def _mtproto_post(username: str, row: dict, query: str):
    """One `messages.search` row as the same `Message` the free surface returns.

    A second post shape would have to be de-duplicated, counted, mined for jargon
    and reported separately everywhere the first one is. There is one shape.
    """
    ident = int(row.get("id") or 0)
    return tgparse.Message(
        username=username,
        id=ident,
        url=f"https://t.me/{username}/{ident}",
        ids=[ident],
        date=row.get("date"),
        text=row.get("text") or "",
        author_name=row.get("author_name"),
        author_username=row.get("author_username"),
        reply_to_id=row.get("reply_to_id"),
        found_by=query,
    )


def _open_account(cfg):
    """Connect a live transport, or raise the sentence that says what is missing.

    The environment switch is the gate. `TELEGRAM_RESEARCH_ALLOW_LIVE` unset means
    every account path in this program refuses before it reads the credential,
    and the refusal names the variable.
    """
    if not account_module.live_enabled_in_env():
        raise UsageError(
            "searching a group goes through the account (messages.search), and "
            "live mode is off. Set TELEGRAM_RESEARCH_ALLOW_LIVE=1 to allow it. "
            "Nothing was sent and no credential was read."
        )
    cred = config_module.read_credentials(cfg)
    transport = account_module.TelethonTransport(
        cred["TELEGRAM_API_ID"], cred["TELEGRAM_API_HASH"], cred["TELEGRAM_SESSION"],
        allow_live=True,
    )
    return transport.connect()


def _group_peer(session, username: str) -> tuple[dict | None, str | None]:
    """The peer to search, from the cache or from ONE `contacts.search`.

    Returns `(peer, refusal)`; what it cost is `session.account_calls`, which
    counts a call that raised as well as one that answered. **No resolve happens
    here and none is reachable from this command.** `contacts.resolveUsername` is
    the call that bought this account a 36 468-second freeze; `contacts.search`
    answers with the peer and its access_hash together, so the resolve is not
    needed for a name the search box returns. It stays in
    `AccountSession.resolve` as the fallback for a name the box will not return
    -- a deliberate act of writing a script under `references/account.md`, not
    something a CLI flag can trigger.
    """
    cached = session.peer_cache.get(username, session.fingerprint)
    if cached:
        return cached, None
    found = session.search_contacts(username)
    if found.get("stopped"):
        return None, found["stopped"]
    want = username.lstrip("@").lower()
    for row in found.get("peers", []):
        if str(row.get("username", "")).lower() == want:
            return session.peer_cache.get(username, session.fingerprint), None
    return None, (
        f"contacts.search does not return @{username}: it matches titles and "
        "usernames, and it returned "
        f"{len(found.get('peers', []))} other peers for this one. Nothing else here "
        "will resolve the name — a resolve is the fallback, and it is a scripted "
        "act under references/account.md, not a flag."
    )


def _search_group(args, cfg, active_run) -> int:
    """`messages.search` inside one public group. The reason the account exists.

    Measured 2026-08-25 on `hanoi_chats`: 1 call for the peer, 1 call for the
    query, 44 hits spanning 2023-04 to 2026-03, 0 resolves, no wait. What this
    replaced was 200 accountless GETs that returned 2 messages, neither of them
    carrying the word the run was about.
    """
    if args.max_pages < 1:
        # The same refusal the channel route and `history` already make. It used
        # to be `max(1, ...)` here, so one flag on one command meant three
        # different things depending on the surface -- and a caller who typed 0
        # meaning "no limit" silently got one page and a number that was not a
        # count.
        raise read_module.NothingAsked(
            f"--max-pages {args.max_pages} buys no page at all, so this search "
            "cannot report on the group. A `found: 0` from a search nobody ran "
            "is a silence an agent would write into a report as a fact."
        )
    transport = _open_account(cfg)
    calls = 0
    out = []
    suppressed = 0
    try:
        with account_module.AccountSession(
            transport, cfg=cfg, dry_run=False, allow_live=True
        ) as session:
            peer, refusal = _group_peer(session, args.username)
            if peer is None:
                emit({"ok": False, "username": args.username, "surface": "mtproto",
                      "error": refusal, "account_calls": session.account_calls,
                      "resolves": 0, "results": []})
                return EXIT_STOPPED if "flooded" in (refusal or "") else EXIT_OPERATOR
            refreshed = False
            for query in args.query:
                messages = []
                total = None
                stopped = None
                pages = 0
                while pages < args.max_pages:
                    try:
                        page = session.search_messages(
                            args.username, peer, query,
                            limit=MTPROTO_PAGE, add_offset=len(messages),
                        )
                    except account_module.PeerUnusable:
                        # The one failure a permanent peer cache can cause, and
                        # it is recoverable for exactly the cost of not having
                        # cached: ONE `contacts.search`. Verified live
                        # 2026-08-25 by corrupting the stored access_hash --
                        # Telegram answers `ChannelInvalidError`, and before this
                        # the command reported it as exit 9, "a bug in tg.py".
                        #
                        # Once per command. A second refusal after a fresh
                        # look-up is Telegram saying the peer is not readable
                        # from this account, and asking a third time would be
                        # spending the account on a question already answered.
                        if refreshed:
                            raise
                        refreshed = True
                        session.peer_cache.drop(args.username)
                        peer, refusal = _group_peer(session, args.username)
                        if peer is None:
                            stopped = refusal
                            break
                        # `continue` WITHOUT counting a page. The refused call
                        # asked nothing about the query, and charging it to
                        # `--max-pages 1` ended the search having returned
                        # nothing about a group holding 44 matches -- measured
                        # live 2026-08-25, on the first version of this repair.
                        continue
                    pages += 1
                    if page.get("stopped"):
                        stopped = page["stopped"]
                        break
                    total = page.get("total")
                    rows = page.get("messages", [])
                    messages.extend(rows)
                    # Two endings, and they are different claims. An empty page
                    # is the surface saying there is no more; `len >= total` is
                    # the surface's own count saying everything is in.
                    if not rows or (isinstance(total, int) and len(messages) >= total):
                        break
                posts = [_mtproto_post(args.username, row, query) for row in messages]
                if active_run:
                    suppressed += active_run.write_posts(posts).suppressed
                out.append({
                    "query": query,
                    "found": len(posts),
                    # The server's own count of matches. `?q=` can never say this
                    # about itself, which is why a capped web search has to warn
                    # and this one can simply state what it left behind.
                    "server_total": total,
                    "complete": bool(isinstance(total, int) and len(posts) >= total),
                    "stopped": stopped,
                    "messages": [m.as_dict() for m in posts],
                })
                if stopped:
                    break
            calls = session.account_calls
    finally:
        transport.close()
    if active_run and calls:
        # Into the run's own counters, so `report` can say what the run SPENT
        # rather than what its brief allowed.
        active_run.count("account_calls", calls)
    partial = [row["query"] for row in out if not row["complete"]]
    close_run(active_run)
    emit({
        "ok": True, "username": args.username, "surface": "mtproto",
        "results": out,
        "account_calls": calls,
        # Printed because it is the number this repair exists to hold at zero.
        "resolves": 0,
        # A stale access_hash was met and repaired inside this command, for one
        # extra `contacts.search`. Said out loud because it is a cost, and
        # because a cache that silently re-looks-up every time is not a cache.
        "peer_refreshed": refreshed,
        "posts_suppressed_as_duplicates": suppressed,
        "partial": bool(partial),
        "incomplete_queries": partial,
        "warning": (
            "these queries returned fewer hits than the server says exist: "
            + ", ".join(f"{q!r}" for q in partial)
            + ". Raise --max-pages; each page is one account call and up to "
              f"{MTPROTO_PAGE} messages."
        ) if partial else None,
        "run": str(active_run.root) if active_run else None,
    })
    return EXIT_OK


def _history_group(args, cfg, active_run, known: dict, until_id: int | None) -> int:
    """A group's recent messages, newest first, through `messages.getHistory`.

    The other half of what an account is for. `search` answers "what does this
    group say about X"; this answers "what is being said in there right now",
    which no query can, because the answer is whatever the last hour happened to
    be about.

    One page is one call and up to 100 messages -- so the whole question
    "what are they talking about" is normally **one** account call, against the
    ~10 000 GETs the accountless surface would have needed to find 100 live ids.
    """
    if args.max_pages < 1:
        raise read_module.NothingAsked(
            f"--max-pages {args.max_pages} buys no page at all, so this read "
            "cannot report on the group. A `found: 0` from a read nobody ran is "
            "the silence this skill exists not to produce."
        )
    transport = _open_account(cfg)
    out, stopped, reached_until = [], None, False
    try:
        with account_module.AccountSession(
            transport, cfg=cfg, dry_run=False, allow_live=True
        ) as session:
            peer, refusal = _group_peer(session, args.username)
            if peer is None:
                emit({"ok": False, "username": args.username, "surface": "mtproto",
                      "error": refusal, "account_calls": session.account_calls,
                      "resolves": 0, "messages": []})
                return EXIT_STOPPED if "flooded" in (refusal or "") else EXIT_OPERATOR
            # The evidence `history()` demands is the landing card `verify` read
            # when it typed this name -- it is in the registry, and re-fetching
            # it would spend a GET to be told what we already recorded.
            request = account_module.SourceRequest(
                username=args.username,
                evidence={"exists": True, "type": known.get("type"),
                          "username": args.username},
            )
            offset_id = args.before or 0
            for _page in range(args.max_pages):
                page = session.history(request, peer, limit=MTPROTO_PAGE,
                                       offset_id=offset_id)
                if page.stopped:
                    stopped = page.stopped
                    break
                rows = list(page.messages)
                if until_id is not None:
                    kept = [r for r in rows if int(r.get("id") or 0) > int(until_id)]
                    if len(kept) < len(rows):
                        reached_until = True
                    rows = kept
                out.extend(rows)
                if reached_until or len(page.messages) < MTPROTO_PAGE:
                    break
                offset_id = min(int(r.get("id") or 0) for r in page.messages)
            calls = session.account_calls
    finally:
        transport.close()
    posts = [_mtproto_post(args.username, row, None) for row in out]
    suppressed = active_run.write_posts(posts).suppressed if active_run else 0
    if active_run and calls:
        active_run.count("account_calls", calls)
    # `exhausted` only when the surface itself ran out, never when a ceiling bit.
    exhausted = not stopped and not reached_until and len(out) < args.max_pages * MTPROTO_PAGE
    settled = bool(reached_until or exhausted)
    cursor = max((m.id for m in posts), default=None)
    if args.write and cursor is not None and settled:
        get_registry(cfg).append(registry_module.Source(
            username=args.username, type=known.get("type"),
            max_id_seen=max(cursor, known.get("max_id_seen") or 0), status="alive",
        ))
    close_run(active_run)
    emit({
        "ok": True, "username": args.username, "surface": "mtproto",
        "found": len(posts), "account_calls": calls, "resolves": 0,
        "posts_suppressed_as_duplicates": suppressed,
        "reached_until_id": reached_until, "exhausted": exhausted,
        "stopped": stopped,
        "until_id": until_id,
        "cursor_written": bool(args.write and cursor is not None and settled),
        # Same rule the channel walk has kept since the cursor was invented: a
        # bounded walk does not move the high-water mark, because the cost of not
        # advancing is re-reading and the cost of advancing wrongly is silent loss.
        "cursor_withheld_reason": (
            None if settled else "the page ceiling stopped this read, so the "
                                 "unread middle would be hidden for ever"),
        "messages": [m.as_dict() for m in posts],
        "run": str(active_run.root) if active_run else None,
    })
    return EXIT_OK


# --------------------------------------------------------------------------
# search / history -- channels, no account, ever
# --------------------------------------------------------------------------
def cmd_search(args, cfg) -> int:
    args.username = check_username(args.username)
    active_run = open_run(args)
    reg = get_registry(cfg)
    known = reg.get(args.username)
    if known and known.get("type") == "group":
        # The whole point of one command: the type in the registry picks the
        # surface, and the caller does not have to know that a group has no free
        # one. This used to be a refusal (exit 6, "read them with `group`") that
        # sent the caller to a walk which could not search at all.
        return _search_group(args, cfg, active_run)
    web = build_web(cfg, active_run, args=args)
    max_pages = min(args.max_pages, cfg.budgets.max_pages_per_channel)
    out = []
    suppressed = 0
    for query in args.query:
        try:
            res = read_module.search_channel(
                web, args.username, query,
                max_pages=max_pages,
                save_prefix=saving_prefix(args, active_run),
            )
        except read_module.NothingAsked as exc:
            # An empty or whitespace-only `--query` is refused in `read.py`
            # before the wire, because `tgweb.preview` builds `?q=` only `if
            # query:` -- so an empty one fetched the channel's front page and
            # `cmd_search` reported the twenty newest posts as twenty results
            # for that query, `found_nothing: false`, exit 0. Inside a run that
            # is worse than a wasted request: `write_posts` banked all twenty
            # with `found_by: ""`, and `queries record` counted them as the
            # round's new posts, clearing the yield floor without having
            # searched for anything.
            #
            # The queries ALREADY answered are kept in the output. They were
            # paid for, and an empty string in the middle of a list must not
            # cost the ones before it.
            banked = harvest_partial(active_run, exc)
            close_run(active_run)
            emit({"ok": False, "error": str(exc), "error_type": "NothingAsked",
                  "query": query, "results": out, "posts_banked": banked,
                  "next": "nothing was asked, so nothing here says the channel "
                          "is silent. Give --query a term with characters in it."})
            return EXIT_USAGE
        except read_module.WrongRoute as exc:
            banked = harvest_partial(active_run, exc)
            close_run(active_run)
            emit({"ok": False, "error": str(exc), "error_type": "WrongRoute",
                  "results": out, "posts_banked": banked})
            return EXIT_WRONG_ROUTE
        except tgweb.RunAborted as exc:
            banked = harvest_partial(active_run, exc)
            _stop_run(str(exc))
            emit({"ok": False, "stopped": str(exc), "results": out,
                  "posts_banked": banked})
            return EXIT_STOPPED
        except (tgweb.FetchFailed, tgweb.TelegramWebError) as exc:
            banked = harvest_partial(active_run, exc)
            close_run(active_run)
            emit({"ok": False, "error": str(exc),
                  "error_type": type(exc).__name__,
                  "results": out, "posts_banked": banked})
            return EXIT_FETCH_FAILED
        # Banked per query, not after the loop. `--query a --query b --query c`
        # with a 502 on `c` used to print the posts of `a` and `b` to stdout and
        # write none of them: the early return jumped over the single
        # `write_posts` that came after the loop, so the run folder held saved
        # pages with no posts parsed out of them and the fetch log's lines
        # matched nothing.
        if active_run:
            # Dedup happens once, inside `write_posts`, keyed on
            # `(username, id)`. It hands back how many it suppressed so the
            # caller can say so -- a `search` and a `history` over the same
            # channel really do retrieve the same message, and a run that
            # printed `found: 40` over 23 distinct posts was 74 % high in the
            # document a person reads.
            suppressed += active_run.write_posts(res.messages).suppressed
        out.append({
            "query": query,
            "found": len(res.messages),
            "ids_seen": ids_seen_of(res),
            # The blocker under this line: the `?q=` surface fills its
            # first page and then simply stops serving, and `read.py` used to
            # end that walk as `exhausted` -- which every caller reads as "all
            # the matches are in". Measured live on a news channel of 98 658
            # posts: 21 hits for a word that appears in 32 of its last 60. The
            # refusal to call a cap an ending lives in `read.py`; if the CLI
            # does not PRINT it, the repair is invisible and the agent still
            # writes a truncated count into a report as a total.
            "surface_truncated": bool(res.surface_truncated),
            "stop_reason": res.stop_reason,
            "exhausted": bool(res.exhausted),
            # Computed by every walk and printed by none: a page that carried
            # message blocks and parsed none of them is a front-end change, and
            # it is byte-identical on stdout to a channel with nothing to say.
            "understood_nothing": bool(res.understood_nothing),
            "blocks_unparsed": res.blocks_unparsed,
            "found_nothing": res.found_nothing,
            "pages": res.pages,
            "requests": res.requests,
            "stopped_early": res.stopped_early,
            "messages": [m.as_dict() for m in res.messages],
        })
    close_run(active_run)
    capped = [row["query"] for row in out if row["surface_truncated"]]
    emit({"ok": True, "username": args.username, "results": out,
          "requests": web.request_count,
          "posts_suppressed_as_duplicates": suppressed,
          # The headline, not only the per-query row: a caller that reads the
          # top of this object and stops must not be able to miss that what it
          # holds is a partial answer.
          "surface_truncated": capped,
          "partial": bool(capped),
          "warning": (
              "the ?q= surface capped " + ", ".join(f"{q!r}" for q in capped)
              + ": these are SOME of the matches, not all of them, and the "
                "counts here must not be reported as what the channel said. "
                "Walk the history with `history` if the number matters."
          ) if capped else None,
          "page_ceiling": max_pages,
          "run": str(active_run.root) if active_run else None})
    return EXIT_OK


def cmd_history(args, cfg) -> int:
    args.username = check_username(args.username)
    active_run = open_run(args)
    reg = get_registry(cfg)
    known = reg.get(args.username) or {}
    until_id = args.until_id
    if until_id is None and args.since_last:
        until_id = known.get("max_id_seen")
    if known.get("type") == "group":
        # Same shape as `search`: the registry's type picks the surface. A group
        # has no preview page, and until 2026-08-26 this was a refusal pointing
        # at nothing -- `AccountSession.history` existed, was fully accounted,
        # and **had no caller anywhere in the skill**. Reading a group's recent
        # messages is one `messages.getHistory` with `limit=100`; the module
        # around it is the accounting every account call needs anyway.
        return _history_group(args, cfg, active_run, known, until_id)
    web = build_web(cfg, active_run, args=args)
    max_pages = min(args.max_pages, cfg.budgets.max_pages_per_channel)
    try:
        res = read_module.walk_channel(
            web, args.username, before=args.before, max_pages=max_pages,
            until_id=until_id,
            save_prefix=saving_prefix(args, active_run),
        )
    except read_module.WrongRoute as exc:
        banked = harvest_partial(active_run, exc)
        close_run(active_run)
        emit({"ok": False, "error": str(exc), "error_type": "WrongRoute",
              "posts_banked": banked})
        return EXIT_WRONG_ROUTE
    except tgweb.RunAborted as exc:
        banked = harvest_partial(active_run, exc)
        _stop_run(str(exc))
        emit({"ok": False, "stopped": str(exc), "posts_banked": banked})
        return EXIT_STOPPED
    except (tgweb.FetchFailed, tgweb.TelegramWebError) as exc:
        banked = harvest_partial(active_run, exc)
        close_run(active_run)
        emit({"ok": False, "error": str(exc), "error_type": type(exc).__name__,
              "posts_banked": banked})
        return EXIT_FETCH_FAILED
    # Deliberately `m.id` and NOT `max(m.ids)`. A stored cursor is a
    # high-water mark and `registry._MERGE_MAX` guarantees it can only go up, so
    # the two readings of an album's ids differ in exactly one direction: the
    # album member id re-reads two posts if it is too low and hides them for
    # ever if it is too high. The contract's tie-breaker is the reading that
    # re-reads. `ids_covered` in the output is where the album ids are stated.
    cursor = max((m.id for m in res.messages), default=None)
    settled, withheld = cursor_verdict(res)
    if args.write and cursor is not None and settled:
        # The producer `--until-id` never had. Without it the registry knew
        # nothing after a full channel walk, so the second run re-read the whole
        # history -- the one defence there is against an unmeasured IP limit.
        reg.append(registry_module.Source(
            username=args.username, type="channel",
            max_id_seen=max(cursor, known.get("max_id_seen") or 0),
            status="alive",
        ))
    suppressed = active_run.write_posts(res.messages).suppressed if active_run else 0
    close_run(active_run)
    emit({
        "ok": True, "username": args.username,
        "found": len(res.messages), "ids_seen": ids_seen_of(res),
        "pages": res.pages, "requests": res.requests,
        "posts_suppressed_as_duplicates": suppressed,
        # Four different endings, four different fields. `exhausted` used to be
        # printed as `reached_first_post`, which turned "the cursor stopped
        # moving" into "this channel is fully read" -- a claim only an id at or
        # below 1 can support.
        "reached_first_post": res.reached_first_post,
        "reached_until_id": res.reached_until_id,
        "no_more_pages": res.no_more_pages,
        "exhausted": res.exhausted,
        "stop_reason": res.stop_reason,
        "stopped_early": res.stopped_early,
        "understood_nothing": bool(res.understood_nothing),
        "blocks_unparsed": res.blocks_unparsed,
        "until_id": until_id,
        "cursor_may_be_stale": stale_cursor_note(known),
        "max_id_seen": cursor,
        "cursor_written": bool(args.write and cursor is not None and settled),
        "cursor_withheld": withheld if (args.write and cursor is not None) else None,
        "cursor_withheld_reason": (res.stop_reason
                                   if (args.write and cursor is not None and not settled)
                                   else None),
        "page_ceiling": max_pages,
        "messages": [m.as_dict() for m in res.messages],
        "run": str(active_run.root) if active_run else None,
    })
    return EXIT_OK


# --------------------------------------------------------------------------
# group -- ONE GET for ONE id you already know
# --------------------------------------------------------------------------
def cmd_group(args, cfg) -> int:
    """Fetch named message ids of a group through `?embed=1`. One GET each.

    This is the whole accountless group surface, and it is deliberately not a
    search. Until 2026-08-25 it also carried the machinery that GUESSED which
    ids to ask for -- a head estimator, a catch-up creep, a blind scan, a miss
    tolerance derived from observed density. Measured on `hanoi_chats` that
    machinery spent 200 requests, returned 2 messages, and matched the word the
    run was about **zero** times; about 1 % of a group's id range answers at
    all, so finding ten messages carrying one word would have cost ~199 000
    GETs against the 29 327 ids that exist. It was not expensive, it was unable.
    Searching a group is `tg.py search`, which routes it to `messages.search`.

    What is left is what always worked: an id somebody already has -- out of a
    permalink, out of a search hit, out of a citation -- read for one request.
    """
    args.username = check_username(args.username)
    ids = sorted({int(i) for i in args.ids})
    if any(i < 1 for i in ids):
        raise UsageError(
            "--id takes a message id, which starts at 1. A group read cannot "
            "report on an id that cannot exist."
        )
    active_run = open_run(args)
    web = build_web(cfg, active_run, args=args)
    reg = get_registry(cfg)
    known = reg.get(args.username) or {}
    if known.get("type") == "channel":
        emit({
            "ok": False,
            "error": f"{args.username} is a channel in the registry. Reading it one "
                     "id at a time costs a GET per id; `/s/` serves it 20 messages "
                     "a page. Use `search` or `history`.",
        })
        return EXIT_WRONG_ROUTE
    found, missing, mismatched = [], [], []
    try:
        for message_id in ids:
            msg, verdict = read_module._fetch_group_message(
                web, args.username, message_id,
                save_prefix=saving_prefix(args, active_run),
            )
            if verdict == "hit":
                found.append(msg)
            elif verdict == "wrong_post":
                mismatched.append(message_id)
            else:
                missing.append(message_id)
    except tgweb.RunAborted as exc:
        # Whatever was already paid for is banked before the stop is reported,
        # exactly as the channel commands do it.
        if active_run:
            active_run.write_posts(found)
        _stop_run(str(exc))
        emit({"ok": False, "stopped": str(exc), "username": args.username,
              "ids_asked": ids, "found": len(found),
              "messages": [m.as_dict() for m in found]})
        return EXIT_STOPPED
    suppressed = active_run.write_posts(found).suppressed if active_run else 0
    close_run(active_run)
    emit({
        "ok": True, "username": args.username,
        "ids_asked": ids,
        "found": len(found),
        # An id that answered nothing is not a group that said nothing: a gap in
        # a group's ids is ordinary (124 empty ids in a row sat between two live
        # messages on `hanoi_chats`), and `?embed=1` renders some message types
        # not at all. Named separately from `mismatched_ids`, which is a page
        # that came back for another id or another peer.
        "missing_ids": missing,
        "mismatched_ids": mismatched,
        "requests": web.request_count,
        "posts_suppressed_as_duplicates": suppressed,
        "messages": [m.as_dict() for m in found],
        "run": str(active_run.root) if active_run else None,
    })
    return EXIT_OK


# --------------------------------------------------------------------------
# queries -- stage 3, and the three stoppers it exists to enforce
# --------------------------------------------------------------------------
def _load_query_log(run: Run) -> querycraft_module.QueryLog | None:
    """The run's query log, or a sentence saying it cannot be read.

    `QueryLog.save` is a plain `write_text`, so a `queries` command killed
    mid-write leaves a half file, and `QueryLog.load` deliberately raises rather
    than pretending the log is empty. That refusal is right; what was wrong is
    where it landed -- `QueryLogError` was in no `except` clause, so `report`,
    `queries record`, `queries accept` and `queries show` all answered a
    truncated `queries.json` with a traceback and exit 1. A run that had spent
    its whole request budget was then unreportable, and a subagent reading
    stdout was told nothing at all.
    """
    log, why_not = load_query_log_or_reason(run)
    if why_not is not None:
        raise UsageError(why_not)
    return log


def load_query_log_or_reason(run: Run) -> tuple[object | None, str | None]:
    """`(log, None)` or `(None, a sentence)`. Never a traceback either way.

    Two callers with opposite needs, which is why this returns rather than
    raises. `queries start/record/accept` WRITE into the log and must not
    proceed on a file they cannot read -- for them the sentence is the answer,
    and `_load_query_log` turns it into an exit 7. `report` only DESCRIBES the
    log, so a corrupt sidecar must not make a run that spent its whole request
    budget unreportable: it takes the sentence and hands it to
    `report_skeleton` as its own state.
    """
    try:
        return run.load_queries(querycraft_module.QueryLog), None
    except querycraft_module.QueryLogError as exc:
        return None, (
            f"{exc}. The run's other files are intact: repair "
            f"{run.queries_path} by hand, or move it aside and re-run "
            "`tg.py queries <run> start`. Nothing here will delete it for you."
        )


def _fold_term(text: str) -> str:
    """Fold a term the way `querycraft` folds the corpus, whatever it calls it.

    `QueryLog.terms` is keyed by the folded form. `casefold()` alone missed NFKC
    (and, once `querycraft` folds them, ё/е), so an accepted term did not match
    its own key.
    """
    folder = (getattr(querycraft_module, "fold", None)
              or getattr(querycraft_module, "_fold", None))
    cleaned = (text or "").strip()
    return folder(cleaned) if folder else cleaned.casefold()


def _run_posts(run: Run, path: Path | None = None) -> tuple[list, list[int]]:
    return read_jsonl(path or run.posts_path)


def _posts_source(value) -> Path | None:
    """The `--posts` file, checked before a round can be scored against it."""
    if not value:
        return None
    path = Path(value)
    if not path.exists():
        raise UsageError(
            f"--posts {path} does not exist. Nothing was recorded: a round "
            "scored against a file that is not there brings 0 new posts, trips "
            "the yield floor, and writes «round N brought 0 new posts» into "
            "queries.json, run.json and the report about a round that "
            "may have brought plenty. That sentence cannot be retracted."
        )
    if path.is_dir():
        raise UsageError(f"--posts {path} is a directory, not a posts.jsonl")
    return path


def cmd_queries(args, cfg) -> int:
    """The query-craft ledger, on the command line at last.

    `querycraft.QueryLog` enforces a round ceiling, a yield floor and a drift ban,
    and until now nothing could call it: the module was imported by its own test
    and by nothing else, so all three stoppers were enforced on nobody and
    `queries.md` had no writer at all. The judgement stays with the agent -- which
    candidate is really the local word for the thing -- and the bookkeeping,
    which is what drifts, is here.
    """
    root = require_run_folder(args.run)
    run = track_run(Run.attach(root))
    run.record_agent("queries")
    log = _load_query_log(run)

    if args.action == "start":
        if not args.query:
            raise UsageError("`queries start` needs at least one --query")
        if log is None:
            log = querycraft_module.QueryLog(
                max_rounds=run.brief.max_rounds,
                min_new_posts=run.brief.min_new_posts,
            )
        may, why = log.may_continue()
        if not may:
            run.stop(why)
            run.write_queries(log)
            run.finish()
            emit({"ok": False, "stopped": why, "rounds": len(log.rounds)})
            return EXIT_STOPPED
        verdicts, allowed = [], []
        for query in args.query:
            ok, why_not = log.allows(query)
            verdicts.append({"query": query, "allowed": ok, "why": why_not})
            if ok:
                allowed.append(query)
        if not allowed:
            run.write_queries(log)
            run.finish()
            emit({"ok": False, "error": "every query was refused as drift",
                  "queries": verdicts})
            return EXIT_STOPPED
        rnd = log.start_round(allowed)
        run.write_queries(log)
        run.finish()
        emit({"ok": True, "round": rnd.number, "queries": verdicts,
              "run": str(run.root),
              "next": f"tg.py search <channel> " +
                      " ".join(f'--query "{q}"' for q in allowed) +
                      f' --run "{run.root}", then `tg.py queries <run> record`'})
        return EXIT_OK

    if log is None:
        raise UsageError(
            f"{root} holds no queries.json: start a round first with "
            "`tg.py queries <run> start --query \"...\"`"
        )

    if args.action == "record":
        # A path that is not there is refused BEFORE the round is scored.
        # `read_jsonl` answers a missing file with `([], [])`, so a mistyped
        # `--posts` scored the round as having produced zero new posts, the
        # yield floor fired, and «round N brought 0 new posts, below the floor
        # of M» went into `queries.json`, into `run.json`'s `stop_reasons` and
        # from there into the report -- permanently, because `_union`
        # only ever adds. Measured on a run holding 8 posts: `ok: true`,
        # `new_posts: 0`, `may_continue: false`, about a round that brought 8.
        # Every other file-path flag in this CLI is existence-checked; this was
        # the one that was not.
        source = _posts_source(args.posts)
        posts, corrupt = _run_posts(run, source)
        fresh = log.record_posts(posts)
        # The question's own words are excluded: stage 3 exists to find what the
        # question could NOT have said.
        exclude = list(run.brief.seed_queries)
        exclude += querycraft_module.TOKEN_RE.findall(run.brief.question or "")
        for rnd in log.rounds:
            exclude += list(rnd.queries)
        candidates = log.candidates(posts, exclude=exclude,
                                    top=row_limit(args.top, "--top"))
        may, why = log.may_continue()
        if not may and log.rounds:
            log.rounds[-1].stopped_because = why
            run.stop(why)
        run.write_queries(log)
        run.finish()
        emit({
            "ok": True, "round": len(log.rounds), "new_posts": fresh,
            "posts_seen": len(log.seen_post_urls),
            "corrupt_post_lines": corrupt,
            "may_continue": may, "why_not": why,
            # What shortened the shortlist, in numbers: the `top` cut, the
            # `min_documents` floor, the words excluded as the question's own,
            # the boilerplate lines removed. `candidates: []` means something
            # quite different in each case, and a floor no batch could clear
            # used to read as "this corpus has no jargon".
            "mining": log.last_mining,
            "candidates": [c.as_dict() for c in candidates],
            "next": "read the examples, then `tg.py queries <run> accept --term "
                    "<word> --gloss \"<what it means>\"` for the real ones. The "
                    "shortlist is evidence, not a decision.",
        })
        return EXIT_OK

    if args.action == "accept":
        if not args.term:
            raise UsageError("`queries accept` needs --term")
        posts, _ = _run_posts(run)
        wanted = _fold_term(args.term)
        already = log.terms.get(wanted)
        if already is not None:
            # `candidates()` skips anything already in the vocabulary, so a
            # second `accept` of the same word found nothing and was answered
            # with the drift ban -- "appears in no post this run retrieved",
            # about a word that is in the corpus AND in the vocabulary. It also
            # meant a gloss could not be corrected without editing
            # `queries.json` by hand.
            if args.gloss:
                already.gloss = args.gloss
            run.write_queries(log)
            run.finish()
            emit({"ok": True, "term": already.as_dict(), "already_accepted": True,
                  "gloss_updated": bool(args.gloss), "run": str(run.root)})
            return EXIT_OK
        found = None
        for cand in log.candidates(posts, top=10_000, min_documents=1):
            if cand.term == wanted:
                found = cand
                break
        if found is None:
            raise UsageError(
                f"{args.term!r} appears in no post this run retrieved, so it "
                "cannot be taken into the vocabulary. That is the drift ban: a "
                "word must come from the corpus, not from plausibility."
            )
        log.accept(found, gloss=args.gloss)
        run.write_queries(log)
        run.finish()
        emit({"ok": True, "term": found.as_dict(), "run": str(run.root)})
        return EXIT_OK

    # show
    run.write_queries(log)
    run.finish()
    may, why = log.may_continue()
    emit({"ok": True, "log": log.as_dict(), "may_continue": may, "why_not": why,
          "queries_md": str(run.root / "queries.md")})
    return EXIT_OK


# --------------------------------------------------------------------------
# notes / acceptance
# --------------------------------------------------------------------------
def cmd_note(args, cfg) -> int:
    """Write `notes/<agent>.md`.

    A calling agent reads a run through its notes, and `accept` refuses a run
    that has none: "a run with no notes has no material the report could come
    from". `Run.write_note` existed and no command called it.
    """
    root = require_run_folder(args.run)
    run = track_run(Run.attach(root))
    if args.from_file:
        source = Path(args.from_file)
        if not source.exists():
            raise UsageError(f"--from-file {source} does not exist")
        text = source.read_text(encoding="utf-8", errors="replace")
    elif args.text:
        text = args.text
    else:
        text = read_stdin_text()
    if not text.strip():
        raise UsageError("the note is empty; an empty note fails the folder gate")
    path = run.write_note(args.agent, text)
    run.finish()
    emit({"ok": True, "note": str(path), "bytes": path.stat().st_size,
          "run": str(run.root)})
    return EXIT_OK


def _acceptance_findings(root: Path) -> tuple[list[str], list[str], dict]:
    """Whether this folder is a finished run, and what is wrong with it if not.

    The whole gate lives here. It reads nothing but the run folder itself and
    calls nothing outside this skill, so `accept` means the same thing on a
    machine that has only this skill installed as it does anywhere else.
    """
    errors: list[str] = []
    warnings: list[str] = []

    def size(name: str) -> int:
        path = root / name
        return path.stat().st_size if path.is_file() else -1

    for name in ("brief.md", "report.md", "run.json", "fetchlog.jsonl"):
        n = size(name)
        if n < 0:
            errors.append(f"{name} is missing")
        elif n == 0:
            errors.append(f"{name} is empty")

    notes = [p for p in sorted((root / "notes").glob("*.md")) if p.is_file()] \
        if (root / "notes").is_dir() else []
    if not notes:
        errors.append(
            "notes/ holds no *.md note: a run with no notes has no material the "
            "report could come from. Write one with `tg.py note <run> --agent ...`"
        )
    for note in notes:
        # EACH empty note is an error, not "every one of them": a researcher
        # that died early leaves a stub, and its sub-question is not closed. A
        # folder with one good note and one 0-byte stub used to pass here with
        # `formal: PASS` and exit 0, which is the verdict `SKILL.md` promises
        # is worth something.
        if note.stat().st_size == 0:
            errors.append(
                f"notes/{note.name} is empty. A researcher that died early "
                "leaves a stub, and its sub-question is not closed"
            )

    records, corrupt = read_jsonl(root / "fetchlog.jsonl")
    fetches = sum(1 for r in records if isinstance(r, dict) and r.get("kind") == "fetch")
    if records and not fetches:
        errors.append(
            f"{len(records)} fetch-log record(s) and not one is kind=fetch; "
            "nothing in the report would be citable"
        )
    if corrupt:
        errors.append(f"fetchlog.jsonl has unparseable line(s): {corrupt[:5]}")

    if (root / "raw").exists():
        errors.append("raw/ exists inside the run folder; a run writes notes/, never raw/")

    posts, post_corrupt = read_jsonl(root / "posts.jsonl")
    if not posts:
        warnings.append("posts.jsonl is empty: this run quoted nothing")
    if post_corrupt:
        warnings.append(f"posts.jsonl has unparseable line(s): {post_corrupt[:5]}")
    sources = list((root / "notes" / "sources").glob("*")) \
        if (root / "notes" / "sources").is_dir() else []
    if posts and not sources:
        warnings.append(
            "posts.jsonl holds posts but notes/sources/ is empty: the pages "
            "behind the quotes were not archived and a claim from them cannot "
            "be checked by anybody"
        )
    if not (root / "queries.json").exists():
        warnings.append(
            "no queries.json: stage 3 never ran through the CLI, so the round "
            "ceiling, the yield floor and the drift ban bound nothing"
        )
    return errors, warnings, {
        "notes": len(notes), "fetch_records": len(records), "fetches": fetches,
        "posts": len(posts), "originals": len(sources),
    }


def cmd_accept(args, cfg) -> int:
    """Write `acceptance.json`, and record the verdict in `run.json`.

    No acceptance artefact, no closed run. The gate is written by the same
    process that decides it, so the two files can never disagree, and it is
    decided from the run folder alone -- no other skill, no other script.
    """
    root = require_run_folder(args.run)
    run = track_run(Run.attach(root))
    errors, warnings, counts = _acceptance_findings(root)
    data = {
        "tool": "tg.py accept",
        "version": TOOL_VERSION,
        "checked_at": now_local(),
        "formal": "FAIL" if errors else "PASS",
        "errors": errors,
        "warnings": warnings,
        "report": str((root / "report.md").resolve()),
        "brief": str((root / "brief.md").resolve()),
        "fetch_log": str((root / "fetchlog.jsonl").resolve()),
        "vocabulary": str((root / "queries.md").resolve())
        if (root / "queries.md").exists() else None,
        "not_mechanised": "what was found, what the jargon means and what a "
                          "silence implies — those sentences are the agent's "
                          "and this file does not claim them",
        "counts": counts,
    }
    run.write_acceptance(data)
    emit({"ok": not errors, "acceptance": str(root / "acceptance.json"),
          "formal": data["formal"], "errors": errors, "warnings": warnings,
          "counts": counts,
          "next": (f'the run is closed; the report is at "{root / "report.md"}"'
                   if not errors else
                   "fix the errors above, then run `tg.py accept` again")})
    return EXIT_OK if not errors else EXIT_NOT_ACCEPTED


# --------------------------------------------------------------------------
# registry / budget
# --------------------------------------------------------------------------
def cmd_registry(args, cfg) -> int:
    reg = get_registry(cfg)
    if args.action == "stats":
        loaded = reg.load()
        by_type: dict[str, int] = {}
        by_topic: dict[str, int] = {}
        for rec in loaded.values():
            by_type[rec.get("type", "unknown")] = by_type.get(rec.get("type", "unknown"), 0) + 1
            for topic in rec.get("topics", []) or []:
                by_topic[topic] = by_topic.get(topic, 0) + 1
        emit({
            "ok": True, "path": str(reg.path), "sources": len(loaded),
            "by_type": by_type, "by_topic": by_topic,
            "corrupt_lines": reg.corrupt_lines(),
        })
    elif args.action == "list":
        limit = row_limit(args.limit, "--limit")
        loaded = reg.load()
        rows = [
            rec for rec in loaded.values()
            if (not args.topic or args.topic in (rec.get("topics") or []))
            and (not args.type or rec.get("type") == args.type)
        ]
        shown = rows[:limit]
        emit({"ok": True, "count": len(rows), "shown": len(shown),
              "limit": limit,
              # `count` is the whole matching set and `shown` is what is printed.
              # One number for both is how `--limit -1` looked complete while
              # dropping the last row.
              "truncated": len(shown) < len(rows),
              "sources": shown})
    elif args.action == "get":
        if not args.username:
            raise UsageError(
                "`registry get` needs --username. Without it the answer was "
                "`source: null`, which reads exactly like 'that name is not in "
                "the registry' and is a different fact."
            )
        record = reg.get(args.username)
        emit({"ok": True, "username": args.username.lstrip("@"), "source": record,
              "known": record is not None})
    elif args.action == "compact":
        # `--force` exists because `registry.compact` told the operator to "pass
        # force=True" -- a Python keyword argument, from a command line that had
        # no such flag. The whole experience of a damaged registry was: a
        # traceback, exit 1, and an instruction that could not be followed. And
        # `registry stats` printing `corrupt_lines` makes `compact` the obvious
        # next command, which is the one input that crashed.
        kept = reg.compact(force=args.force)
        # `compact()` writes `<name>.bak` on EVERY run now, not only under
        # `--force`, so reporting the path only under `--force` told the
        # operator there was no backup while one was sitting on disk -- and it
        # is the file the next compaction will refuse to overwrite. Reported
        # from the registry's own `backup_path()` rather than rebuilt here, and
        # null only when there was nothing to back up.
        backup = reg.backup_path()
        emit({"ok": True, "kept": kept, "forced": bool(args.force),
              "path": str(reg.path),
              "backup": str(backup) if backup.exists() else None,
              "backup_bytes": backup.stat().st_size if backup.exists() else 0})
    return EXIT_OK


def _unfreeze(ledger, args, cfg) -> int:
    """`budget --unfreeze`: lift a resolve freeze without hand-editing JSON.

    `freeze()` is called on every FloodWait and there was no way back other
    than opening the ledger in an editor and changing a float -- on the one file
    whose corruption is a hard stop for the whole account half of the skill, and
    which is read under a guard by processes that may be running at the time.
    A freeze that can only be repaired by hand is a freeze that gets repaired by
    `del` sooner or later.

    The clearing itself belongs to `resolve.clear_freeze(reason)`, which records
    the value it cleared and why. Nothing is decided here: this command supplies
    the reason and prints what came back.
    """
    before = ledger.summary()
    reason = (args.reason or "").strip() or (
        f"cleared with `tg.py budget --unfreeze` at {now_local()}"
    )
    record = ledger.clear_freeze(reason)
    after = ledger.summary()
    emit({
        "ok": True,
        "was_frozen": bool(before.get("frozen")),
        "frozen_for_sec_before": before.get("frozen_for_sec"),
        "frozen_reason_before": before.get("frozen_reason"),
        "cleared": record,
        "reason": reason,
        "frozen": bool(after.get("frozen")),
        "ledger": after,
        "path": str(cfg.ledger_path),
        "next": "the freeze is Telegram's own signal: clearing one it still "
                "means earns the next one longer. Resolve slowly.",
    })
    return EXIT_OK


def cmd_budget(args, cfg) -> int:
    """What the account has spent today, and whether it is frozen.

    Reads the ledger only. It makes no network call and touches no credential,
    so it is safe to run at any time, including while wondering whether it is
    safe to run anything else.
    """
    ledger = resolve_module.ResolveLedger(
        cfg.ledger_path,
        daily_ceiling=cfg.budgets.daily_resolve_ceiling,
        burst_ceiling=cfg.budgets.burst_ceiling,
        burst_window=cfg.budgets.burst_window_sec,
        min_gap=cfg.budgets.min_resolve_gap_sec,
        join_ceiling=cfg.budgets.daily_join_ceiling,
    )
    if getattr(args, "unfreeze", False):
        return _unfreeze(ledger, args, cfg)
    summary = ledger.summary()
    # `ok` used to be hard-coded True, so the command `SKILL.md` calls "always
    # safe to ask" and positions as the safety check before touching the account
    # answered a corrupt ledger with `ok: true` and exit 0. The fail-closed
    # verdict was there all along, two levels down in `ledger.summary()`, where
    # an agent branching on the top-level flag never looked.
    readable = bool(summary.get("readable", True))
    emit({"ok": readable, "readable": readable,
          "frozen": bool(summary.get("frozen", False)),
          "ledger": summary, "path": str(cfg.ledger_path),
          "error": None if readable else
          f"{cfg.ledger_path} cannot be read, so nothing may be resolved. "
          "Repair it or move it aside; a ledger that cannot be read is a "
          "freeze, not a permission.",
          # `config.py` records every override it clamped -- the account
          # ceilings may only fall, the pacing gaps may only rise -- in
          # `override_notes`, whose only documented reader was a `tg.py config`
          # subcommand that does not exist. `budget` is the command that already
          # answers "what am I allowed to spend", so it answers this too.
          "config_notes": list(getattr(cfg, "override_notes", []) or []),
          "free_surface": {
              "max_requests_per_run": cfg.budgets.max_requests_per_run,
              "max_pages_per_channel": cfg.budgets.max_pages_per_channel,
          }})
    return EXIT_OK if readable else EXIT_OPERATOR


def cmd_newrun(args, cfg) -> int:
    """Create the run folder and its brief. Everything else appends into it."""
    if args.brief:
        source = Path(args.brief)
        if not source.exists():
            raise UsageError(f"--brief {source} does not exist")
        # `budgets=` explicitly: a `--brief` file names a `depth` and the
        # ceilings that depth implies have to come from THIS process's config,
        # not from a second `config.load()` inside the loader. Without it a
        # "deep" brief ran on `normal`'s ceilings.
        brief = Brief.from_file(source, budgets=cfg.budgets)
    else:
        if not (args.question or "").strip():
            raise UsageError(
                "newrun needs --question (or --brief). A question-less run wrote "
                "`**Question.** None` into the brief and landed every one of them "
                "in the same folder."
            )
        brief = Brief.for_depth(
            args.depth, budgets=cfg.budgets,
            question=args.question, topic=args.topic, caller=args.caller,
            lang=args.lang, geo=args.geo, since=args.since, until=args.until,
            seed_sources=args.seed_source or None,
            seed_queries=args.seed_query or None,
            max_rounds=args.max_rounds,
            min_new_posts=args.min_new_posts,
            max_requests=args.max_requests,
        )
    # `cfg.root`, not the raw flag: `config.load` has already turned
    # "not given" into the repository the skill is installed in, so there is one
    # answer to "where do runs live" and it is not the shell's idea of it.
    run = Run.open(brief, root=cfg.root)
    emit({
        "ok": True, "run": str(run.root),
        "sources_dir": str(run.sources_dir),
        "brief": run.brief.as_dict(),
        "next": f'tg.py --run "{run.root}" <fetching command>',
    })
    return EXIT_OK


# The line `report_skeleton` leaves for the agent to replace. Its presence is
# how this program tells an untouched skeleton from a report somebody wrote.
# It is deliberately the one string in the report that is not translated: a
# report generated with `--report-lang ru` has to be recognisable here too.
REPORT_PLACEHOLDER = run_module.ANSWER_MARKER


def would_destroy_report(root: Path, *, force: bool) -> str | None:
    """Why a second `report` must not run, or None if it may.

    `cmd_report` wrote `report.md` with an unconditional `write_text`, so a
    second `tg.py report <run>` replaced a finished report with the
    empty skeleton and exited 0. The documented flow is `report` -> the agent
    fills in the answer section and the jargon glosses -> `accept`; a second round of
    searching and then `report <run>` again to refresh the counts -- the obvious
    thing to do, since `report` is advertised as stating what the folder holds
    -- deleted every sentence the agent had written. Nothing warned, nothing was
    backed up, and the command answered `"ok": true`.

    The rule everywhere else in this skill is that nothing deletes anything:
    `run.json` gets `_keep_damaged`, `notes/<agent>.md` appends with a
    separator, `posts.jsonl` is first-write-wins. `report.md`, the one file a
    human writes by hand, got truncate-and-replace.

    The message says what is in the file, because "it exists" is not enough to
    decide with: an untouched skeleton and a finished report both exist.
    """
    path = Path(root) / "report.md"
    if force:
        return None
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    written = "still the untouched skeleton" if REPORT_PLACEHOLDER in text else (
        "somebody has written the answer into it — the answer placeholder "
        "is gone")
    return (
        f"{path} already exists ({path.stat().st_size} bytes, "
        f"{len(text.splitlines())} lines, {written}) and rewriting it would "
        "replace it with a fresh empty skeleton. Nothing was written. Pass "
        "--force to overwrite it deliberately, or read it first: this is the "
        "one file in the run folder a human writes by hand and the only copy "
        "of it."
    )


def cmd_report(args, cfg) -> int:
    """Write the report skeleton from what the run folder already holds.

    Everything it states is something the folder holds. It used to state the last
    command's counters as the run's, assert in every report that no corpus
    vocabulary had been mined, and link a `queries.md` that nothing wrote.
    """
    root = require_run_folder(args.run)
    refusal = would_destroy_report(root, force=args.force)
    if refusal is not None:
        # Before `Run.attach`, so a refused `report` writes NOTHING at all --
        # not the skeleton, not `run.json`, not a `notes/` tree.
        emit({"ok": False, "error": refusal, "error_type": "WouldDestroy",
              "report": str(root / "report.md"),
              "next": "read it first; `tg.py report <run> --force` overwrites it "
                      "with a fresh skeleton and loses whatever it says"})
        return EXIT_WOULD_DESTROY
    fallback = Brief(question=args.question or "(question not recorded)").redacted()
    run = track_run(
        Run.attach(root, brief=None if (root / "brief.json").exists() else fallback)
    )
    run.brief = run.brief.redacted()
    run.record_agent("report")
    posts, post_corrupt = read_jsonl(root / "posts.jsonl")
    sources, source_corrupt = read_jsonl(root / "registry-delta.jsonl")
    # `report` survives a `queries.json` it cannot read. The alternative was
    # exit 7 on a run whose whole network spend was already paid for, over one
    # corrupt sidecar -- and passing `query_log=None` alone would have said the
    # round log was never kept, which is a false statement about a run that DID
    # keep a log. A third state says the true thing instead.
    query_log, query_log_error = load_query_log_or_reason(run)
    lang = getattr(args, "report_lang", None) or run_module.DEFAULT_REPORT_LANG
    text = report_skeleton(
        run, discovery=None, query_log=query_log, sources_used=sources, posts=posts,
        query_log_error=query_log_error, lang=lang,
    )
    (root / "report.md").write_text(config_module.redact(text), encoding="utf-8")
    run.finish()
    emit({"ok": True, "report": str(root / "report.md"),
          "overwrote_existing": bool(args.force),
          "report_lang": lang,
          "posts": len(posts), "sources": len(sources),
          "requests": run.counters.get("requests", 0),
          "corrupt_lines": {"posts.jsonl": post_corrupt,
                            "registry-delta.jsonl": source_corrupt},
          "query_rounds": len(query_log.rounds) if query_log else None,
          "query_log_error": query_log_error,
          "next": "fill in the answer section and the jargon glosses — that is "
                  "the agent's work, not this command's; then "
                  "`tg.py accept <run>`"})
    return EXIT_OK


def default_probes() -> Path:
    """The saved probe corpus, found from this file rather than from the cwd.

    `selftest` is the first command `SKILL.md` tells you to run and it used to
    work from the repository root and nowhere else. The 10 probes it opens now
    travel INSIDE the skill folder, so an installed copy self-tests with no file
    from the project repository -- where the full 32-page corpus stays, beside
    the pytest suite, so that it is not copied to every user of the skill.
    `TELEGRAM_RESEARCH_PROBES` points this at that fuller corpus, or at any
    other -- the same override the test suite reads.
    """
    override = os.environ.get("TELEGRAM_RESEARCH_PROBES")
    if override:
        # `Path(override)` follows the shell for a relative value and makes
        # `~/probes` a directory literally named `~`; `anchored_env_path`
        # expands the home, and anchors a relative value on the repository the
        # way `TELEGRAM_RESEARCH_STATE` is anchored. An override that names a
        # different corpus in every shell is the defect this whole class of
        # variable has.
        return config_module.anchored_env_path(override)
    skill_root = Path(__file__).resolve().parent.parent
    return skill_root / "tests" / "fixtures" / "probes"


def cmd_selftest(args, cfg) -> int:
    """Parse the saved probe pages and confirm the parsers still agree with them.

    Offline and free. It is the check to run after any change to the selector
    table, and the one to run first when a live read starts returning nothing:
    it separates "Telegram changed" from "we broke it".

    The 10 probes and the assertions below are the 2026-08-25 list. The
    seven-file version covered one channel page, one search page, one landing
    card, one missing post and one group embed, and asserted nothing about
    service messages, media, replies, peers, dates, text or cursors -- so a
    rename of `service_message`, `message_media_not_supported_wrap`,
    `js-message_text`, `tgme_widget_message_reply`, `data-peer` or the cursor
    markup passed green while the parse degraded. `blocks_seen` /
    `blocks_unparsed` catch a block-level rename directly; the twenty non-empty
    texts catch the one degradation the parsing layer cannot detect on its own,
    because twenty media-only messages are a thing a channel may legitimately
    post.
    """
    import tgparse
    probes = Path(args.probes) if args.probes else default_probes()
    if not probes.is_dir():
        raise UsageError(
            f"the probe corpus is not at {probes}. The 10 pages this command "
            "reads ship with the skill at tests/fixtures/probes; point --probes "
            "at a copy of them, or at the full corpus in the project repository"
        )
    checks, failures = [], []

    def check(name, got, want):
        ok = got == want
        checks.append({"check": name, "got": got, "want": want, "ok": ok})
        if not ok:
            failures.append(name)

    body = (probes / "C01-landing-durov.html").read_text(encoding="utf-8", errors="replace")
    card = tgparse.parse_landing(body, "durov")
    check("durov.type", card.type, "channel")
    check("durov.members", card.members, 11110268)

    body = (probes / "A18-landing-tdlibchat.html").read_text(encoding="utf-8", errors="replace")
    card = tgparse.parse_landing(body, "tdlibchat")
    check("tdlibchat.type", card.type, "group")
    check("tdlibchat.online", card.online, 362)

    body = (probes / "C02-landing-nonexistent.html").read_text(encoding="utf-8", errors="replace")
    check("nonexistent.exists", tgparse.parse_landing(body, "zzqwx").exists, False)

    body = (probes / "A01-s-durov.html").read_text(encoding="utf-8", errors="replace")
    page = tgparse.parse_preview(body, "durov")
    # The id count, not the block count. An album is ONE `data-post` block
    # carrying several ids -- measured live on `t.me/s/nexta_tv`, 18 blocks over
    # ids 27033-27052 -- so `len(page.messages)` asks "how many blocks parsed"
    # and answers it with a number that is short by the size of every album on
    # the page. `ids_seen` asks what this check was always for: did the page
    # come back whole. Both are 20 on this frozen probe, which has no album on
    # it; they part company the moment one appears.
    check("durov.ids_seen", page.ids_seen, 20)
    check("durov.chat_id", page.chat_id, -1006503122)
    check("durov.first_views_raw", page.messages[0].views_raw, "12.5M")
    # A block-level markup rename -- `data-post`, `tgme_widget_message_wrap` --
    # turns a full page into "nothing was said", which is byte-identical to a
    # channel with nothing to say. These two say which it was.
    check("durov.blocks_seen", page.blocks_seen, 20)
    check("durov.blocks_unparsed", page.blocks_unparsed, 0)
    check("durov.every_message_dated", all(bool(m.date) for m in page.messages), True)
    # The one degradation the parsing layer cannot flag for itself: with
    # `js-message_text` renamed it still returns 20 messages with correct ids
    # and permalinks, and every text empty.
    check("durov.texts_non_empty",
          sum(1 for m in page.messages if (m.text or "").strip()), 20)
    check("durov.page_before", page.before, 523)

    body = (probes / "A09-s-Astana_motoriders.html").read_text(encoding="utf-8", errors="replace")
    page = tgparse.parse_preview(body, "Astana_motoriders")
    check("service_messages_counted",
          sum(1 for m in page.messages if m.is_service), 1)
    check("unsupported_video_recorded",
          "unsupported:video" in {kind for m in page.messages for kind in (m.media or [])},
          True)

    body = (probes / "C15-s-durov-q-rare.html").read_text(encoding="utf-8", errors="replace")
    page = tgparse.parse_preview(body, "durov", found_by="bitcoin")
    check("q_bitcoin.ids", [m.id for m in page.messages], [62, 67, 77, 116, 215, 232, 440])
    # A terminal search page publishes no cursor. If this ever becomes an id,
    # every search starts paying one request past its last page of hits again.
    check("q_bitcoin.no_cursor", page.before, None)

    body = (probes / "C26-embed-hanoi-29320.html").read_text(encoding="utf-8", errors="replace")
    check("missing_post", tgparse.parse_embed(body, "hanoi_chats", 29320), None)

    body = (probes / "C08-embed-tdlibchat-50000.html").read_text(encoding="utf-8", errors="replace")
    check("ghost_post.detected", tgweb.post_missing(body), True)
    check("ghost_post.not_a_message", tgparse.parse_embed(body, "tdlibchat", 50000), None)

    body = (probes / "C10-embed-tdlibchat-10000.html").read_text(encoding="utf-8", errors="replace")
    msg = tgparse.parse_embed(body, "tdlibchat", 10000)
    check("group_author_username", msg.author_username, "redacted_user_01")
    check("reply_not_leaking", msg.text.startswith("If you set permissions"), True)
    check("reply_quote_kept", bool((msg.reply_to_text or "").strip()), True)
    check("reply_quote_out_of_text", (msg.reply_to_text or "!") in (msg.text or ""), False)

    body = (probes / "C16-embed-hanoi-1000.html").read_text(encoding="utf-8", errors="replace")
    msg = tgparse.parse_embed(body, "hanoi_chats", 1000)
    check("group_chat_peer", msg.chat_peer, "c1931920118_4774030320557415984")

    emit({"ok": not failures, "checks": checks, "failed": failures,
          "probes": str(probes),
          "note": "mostly real saved pages, a few authored stand-ins, with "
                  "personal data replaced"})
    # 9, not 1. 1 is what an uncaught crash returned, so the caller could not
    # tell "the parsers no longer match the saved pages" -- the whole purpose of
    # this command, and the one `SKILL.md` says to run first -- from "the
    # program blew up". 9 says the second thing on purpose: a parser that no
    # longer agrees with its own fixtures IS this program being broken.
    return EXIT_OK if not failures else EXIT_INTERNAL


# --------------------------------------------------------------------------
def _add_run_flag(parser) -> None:
    """`--run` after the subcommand as well as before it.

    The documented position is before, and the natural transposition used to be
    `unrecognized arguments: --run <path>` with exit 2.
    """
    parser.add_argument("--run", dest="run_dir_local", default=None,
                        help="the run folder to write into (same as the global --run)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tg.py",
        description="Accountless Telegram research. JSON on stdout.",
    )
    parser.add_argument("--root", default=None,
                        help="the project directory run folders are created "
                             "under, in <root>/telegram-runs/. The default is "
                             "the project this skill is installed in — NOT the "
                             "current directory, which used to decide it and "
                             "made where a run landed depend on which shell "
                             "started it")
    parser.add_argument("--run", dest="run_dir",
                        help="a run folder: originals go to <run>/notes/sources and "
                             "every request is logged to <run>/fetchlog.jsonl")
    parser.add_argument("--max-requests", type=positive_int, default=None,
                        help="raise or lower this command's request ceiling; the "
                             "default is the run brief's, then config's "
                             "max_requests_per_run")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("verify", help="one free GET per name: exists? channel or group?")
    p.add_argument("usernames", nargs="+")
    p.add_argument("--write", action="store_true", help="admit into the registry")
    p.add_argument("--probe-preview", action="store_true")
    p.add_argument("--found-via", default="manual",
                   choices=list(registry_module.VALID_FOUND_VIA))
    p.add_argument("--lang"), p.add_argument("--geo")
    p.add_argument("--min-channel-members", type=int, default=100)
    p.add_argument("--min-group-members", type=int, default=50)
    p.add_argument("--save-to", help="a SECOND place to copy the originals; the "
                                     "run's notes/sources always gets them too")
    _add_run_flag(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("discover", help="pull candidate usernames out of text, lyzem or the account")
    p.add_argument("--lyzem-query")
    p.add_argument("--lyzem-kind", action="append",
                   choices=["all", "channels", "groups", "messages", "bots"],
                   help="lyzem search mode, repeatable. The default asks "
                        "groups, channels and messages — one GET each — "
                        "because `messages` alone answers the wrong question: "
                        "it matches post text by OR and carries no title, no "
                        "description and no type")
    p.add_argument("--account-query",
                   help="ask the account's own search box (contacts.search): "
                        "one call, no resolve, and every peer it returns is "
                        "cached with its access_hash so a group it finds can "
                        "then be searched for free. Needs "
                        "TELEGRAM_RESEARCH_ALLOW_LIVE")
    p.add_argument("--from-file", action="append")
    p.add_argument("--text")
    p.add_argument("--found-via", default="web",
                   choices=list(registry_module.VALID_FOUND_VIA))
    p.add_argument("--snippets-to")
    p.add_argument("--save-to")
    _add_run_flag(p)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("search", help="server-side ?q= search of a channel's history")
    p.add_argument("username")
    p.add_argument("--query", action="append", required=True)
    p.add_argument("--max-pages", type=int, default=5)
    p.add_argument("--save-to")
    _add_run_flag(p)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("history", help="recent messages: a channel through "
                                       "?before=, a group through the account")
    p.add_argument("username")
    p.add_argument("--before", type=int)
    p.add_argument("--until-id", type=int)
    p.add_argument("--since-last", action="store_true",
                   help="stop at the registry's max_id_seen for this channel")
    p.add_argument("--max-pages", type=int, default=25,
                   help="pages of 20 messages on a channel; on a group a page is "
                        "one account call and up to 100 messages")
    p.add_argument("--write", action="store_true",
                   help="record the newest id read, so --since-last has a value "
                        "next time")
    p.add_argument("--save-to")
    _add_run_flag(p)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("group", help="one GET per KNOWN id of a group (?embed=1)")
    p.add_argument("username")
    p.add_argument("--id", dest="ids", action="append", type=int, required=True,
                   help="a message id you already have — out of a permalink, a "
                        "search hit or a citation. Repeatable. There is no flag "
                        "for guessing which ids exist: about one id in a hundred "
                        "answers, and searching a group is `search`")
    p.add_argument("--save-to")
    _add_run_flag(p)
    p.set_defaults(func=cmd_group)

    p = sub.add_parser("queries", help="stage 3: rounds, the drift ban, queries.md")
    p.add_argument("run")
    p.add_argument("action", choices=["start", "record", "accept", "show"])
    p.add_argument("--query", action="append", default=[])
    p.add_argument("--posts", help="a posts.jsonl to mine instead of the run's own")
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--term"), p.add_argument("--gloss")
    p.set_defaults(func=cmd_queries)

    p = sub.add_parser("note", help="write notes/<agent>.md into a run")
    p.add_argument("run")
    p.add_argument("--agent", default="telegram")
    p.add_argument("--text"), p.add_argument("--from-file")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("accept", help="write acceptance.json and the gate verdict")
    p.add_argument("run")
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("registry")
    p.add_argument("action", choices=["stats", "list", "get", "compact"])
    p.add_argument("--username"), p.add_argument("--topic"), p.add_argument("--type")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--force", action="store_true",
                   help="compact a registry that holds unreadable lines anyway; "
                        "the original bytes are kept in <registry>.bak. Without "
                        "it a damaged registry is refused, because compaction "
                        "rebuilds the file from the lines that DO parse")
    p.set_defaults(func=cmd_registry)

    p = sub.add_parser("budget", help="resolve ledger: spent, ceiling, frozen?")
    p.add_argument("--unfreeze", action="store_true",
                   help="lift the resolve freeze. The cleared value and the "
                        "reason are recorded in the ledger; without this the "
                        "only way back was editing the JSON by hand")
    p.add_argument("--reason", help="why the freeze is being lifted; recorded "
                                    "in the ledger beside the cleared value")
    p.set_defaults(func=cmd_budget)

    p = sub.add_parser("newrun", help="create the run folder and its brief")
    p.add_argument("--brief"), p.add_argument("--question")
    p.add_argument("--topic", default="general",
                   help="a subject label for the brief and the registry. It is "
                        "not part of the run folder's path")
    p.add_argument("--depth", default="normal", choices=["quick", "normal", "deep"])
    p.add_argument("--lang"), p.add_argument("--geo")
    p.add_argument("--since"), p.add_argument("--until")
    p.add_argument("--seed-source", action="append")
    p.add_argument("--seed-query", action="append")
    p.add_argument("--max-rounds", type=int, default=None)
    p.add_argument("--min-new-posts", type=int, default=None)
    p.add_argument("--caller", default="user", choices=["user", "agent"],
                   help="`user` — a person asked for this run directly; "
                        "`agent` — it was opened from inside another pass")
    p.set_defaults(func=cmd_newrun)

    p = sub.add_parser("report", help="write the report skeleton")
    p.add_argument("run"), p.add_argument("--question")
    p.add_argument("--report-lang", default=run_module.DEFAULT_REPORT_LANG,
                   choices=list(run_module.REPORT_LANGS),
                   help="the language the report skeleton is written in "
                        "(default: en). The answer marker and the file names "
                        "are the same in both")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing report.md. Without it a second "
                        "`report` over a finished report is refused at exit "
                        "10: the skeleton would replace every sentence the "
                        "agent wrote, and there is no backup of it anywhere")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("selftest", help="parse the saved probes; no network")
    p.add_argument("--probes", default=None)
    p.set_defaults(func=cmd_selftest)
    return parser


def dispatch(args) -> int:
    """Run the subcommand, and turn every way it can fail into JSON and a code.

    The list below is the exit-code table, in code. Two rules govern it and
    neither is negotiable: **stdout carries JSON and only JSON** -- a traceback
    goes to stderr, where a caller parsing stdout will not choke on it -- and
    **1 is not a code this program produces**. It used to be the code for a
    damaged registry, an unreadable `queries.json`, a contended write guard, a
    Ctrl-C (130, actually) and every `AttributeError` in the codebase, all of
    them with an empty stdout. A subagent cannot tell that apart from "there was
    nothing to say".
    """
    try:
        # `config.load()` used to sit outside this block, so the one error class
        # the program formats as JSON was the one error that always tracebacked.
        cfg = config_module.load(root_arg(args))
        cfg.ensure_dirs()
        return args.func(args, cfg)
    except KeyboardInterrupt:
        # The same family as a 429 or a declared ceiling: somebody said stop.
        # It used to be exit 130 with a traceback, mid-walk, with the run's
        # whole spend still unwritten.
        reason = "interrupted from the keyboard (Ctrl-C)"
        _stop_run(reason)
        emit({"ok": False, "stopped": reason, "error_type": "KeyboardInterrupt"})
        return EXIT_STOPPED
    except config_module.ConfigError as exc:
        emit({"ok": False, "error": str(exc), "error_type": "ConfigError"})
        return EXIT_OPERATOR
    except config_module.GuardBusy as exc:
        # 4 is "another process holds the lock", and the registry write guard is
        # the only lock the CLI can actually contend for -- no subcommand
        # touches the account, so `AccountBusy` was 4's only route and 4 was
        # unreachable. A contended guard cost a paid GET per name in `verify`
        # and answered with a traceback.
        emit({"ok": False, "error": str(exc), "error_type": "GuardBusy",
              "next": "another process holds the registry write guard. Wait for "
                      "it, or find the stale <registry>.write file it left"})
        return EXIT_ACCOUNT_BUSY
    except resolve_module.AccountBusy as exc:
        emit({"ok": False, "error": str(exc), "error_type": "AccountBusy"})
        return EXIT_ACCOUNT_BUSY
    except tgweb.RunAborted as exc:
        _stop_run(str(exc))
        emit({"ok": False, "stopped": str(exc), "error_type": "RunAborted"})
        return EXIT_STOPPED
    except tgweb.FetchFailed as exc:
        emit({"ok": False, "error": str(exc), "error_type": "FetchFailed"})
        return EXIT_FETCH_FAILED
    except tgweb.TelegramWebError as exc:
        emit({"ok": False, "error": str(exc), "error_type": "TelegramWebError"})
        return EXIT_FETCH_FAILED
    except read_module.WrongRoute as exc:
        emit({"ok": False, "error": str(exc), "error_type": "WrongRoute"})
        return EXIT_WRONG_ROUTE
    except account_module.AccountError as exc:
        # Every one of these is a condition the module raises deliberately, and
        # without this clause they all landed on exit 9 -- "this is a bug in
        # tg.py, not something you typed" -- with a traceback. Measured
        # 2026-08-25 against the live account: a stale cached access_hash
        # answered `PeerUnusable` and the CLI called it an internal error.
        code = (
            EXIT_STOPPED if isinstance(exc, account_module.FloodWait) else
            EXIT_WRONG_ROUTE if isinstance(exc, account_module.WrongSurface) else
            EXIT_OPERATOR if isinstance(exc, (
                account_module.TelethonMissing, account_module.LiveModeRefused,
                account_module.EvidenceRequired, account_module.PaidCallRefused)) else
            EXIT_FETCH_FAILED
        )
        if code == EXIT_STOPPED:
            _stop_run(str(exc))
            emit({"ok": False, "stopped": str(exc), "error_type": type(exc).__name__})
        else:
            emit({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return code
    except NotARunFolder as exc:
        # The path named is not a run folder and NOTHING was written into
        # it. Above the `RunFolderError` clause on purpose: this is not a
        # damaged run, it is a directory that is not one, and the two deserve
        # different codes. Usage, because the fix is to type the other path.
        emit({"ok": False, "error": str(exc), "error_type": "NotARunFolder",
              "wrote_anything": False,
              "next": "a run folder is created by `tg.py newrun`, which prints "
                      "its path; `--run` and the run positional of `report`, "
                      "`accept`, `queries` and `note` take that path and "
                      "nothing else. `--run` will also fill a directory that is "
                      "empty; it will not fill one that holds something else"})
        return EXIT_USAGE
    except read_module.NothingAsked as exc:
        # `NothingAsked` subclasses `ValueError`, so it landed in the
        # operator clause below and answered a question that could not be asked
        # with the code for a mistyped path. It is a usage refusal: an empty
        # query, a page ceiling of zero, an id range with no ids in it. Above
        # the `ValueError` clause for that reason -- Python takes the first
        # matching `except`, and the order here IS the mapping.
        emit({"ok": False, "error": str(exc), "error_type": "NothingAsked",
              "next": "nothing was asked, so nothing in this answer is evidence "
                      "of absence"})
        return EXIT_USAGE
    except registry_module.WouldDestroy as exc:
        # The registry half. `compact()` refuses to write a new backup over
        # an existing one, and that refusal had no clause here: it fell to the
        # generic `except Exception`, which answered a deliberate, documented
        # refusal with **exit 9, a traceback on stderr and `internal: true`** --
        # "this is a bug in tg.py, not something you typed" -- about the one
        # thing the operator can fix by typing `--force`. Measured on the
        # repaired tree: `registry compact` twice -> exit 9, `error_type:
        # WouldDestroy`, `internal: true`.
        #
        # Above the `RegistryDamaged` clause deliberately. They are siblings
        # today (both plain `RuntimeError`), so the order is not load-bearing
        # yet -- and it is one edit in `registry.py` away from being so.
        emit({"ok": False, "error": str(exc), "error_type": "WouldDestroy",
              "next": "read what the existing backup holds before you replace "
                      "it; `--force` compacts anyway and says so"})
        return EXIT_WOULD_DESTROY
    except (registry_module.RegistryDamaged, resolve_module.LedgerUnreadable,
            config_module.AtomicWriteFailed) as exc:
        # State on disk that cannot be read or cannot be replaced. Not the
        # operator's typo and not a bug in the command they ran, so not 7.
        emit({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return EXIT_INTERNAL
    except (UsageError, RunFolderError, ValueError, OSError) as exc:
        # Every operator error used to be a bare traceback with exit 1 and no
        # JSON at all -- a mistyped path, an unwritable --save-to, a state
        # directory pointing at a file. "Every subcommand prints JSON" is the
        # promise; this is where it is kept.
        emit({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return EXIT_OPERATOR
    except Exception as exc:                 # noqa: BLE001 -- the point of the clause
        # `ValueError` and `OSError` were caught; the rest of the ordinary bug
        # family -- `AttributeError`, `KeyError`, `TypeError` -- was not, and
        # each one meant exit 1 with an empty stdout.
        _print_traceback()
        emit({"ok": False, "error": str(exc), "error_type": type(exc).__name__,
              "internal": True,
              "next": "this is a bug in tg.py, not something you typed. The "
                      "traceback is on stderr"})
        return EXIT_INTERNAL


def main(argv=None) -> int:
    global _ACTIVE_RUN, _RUN_PERSISTED
    _ACTIVE_RUN = None
    _RUN_PERSISTED = False
    _use_utf8_stdout()
    args = build_parser().parse_args(argv)
    if getattr(args, "run_dir_local", None):
        args.run_dir = args.run_dir_local
    try:
        try:
            return dispatch(args)
        finally:
            # Whatever happened, the run keeps its spend. This is the only
            # reason `run.json` survives an exit 4/5/6/7.
            persist_active_run()
    except KeyboardInterrupt:
        emit({"ok": False, "stopped": "interrupted from the keyboard (Ctrl-C)",
              "error_type": "KeyboardInterrupt"})
        return EXIT_STOPPED
    except BaseException as exc:             # noqa: BLE001 -- exit 1 must be impossible
        # Reached only if a handler above, or `emit` itself, failed. There is
        # nothing left to try except saying so in the one format the caller can
        # read.
        _print_traceback()
        try:
            emit({"ok": False, "error": str(exc), "error_type": type(exc).__name__,
                  "internal": True})
        except BaseException:                # noqa: BLE001 -- stdout itself is gone
            pass
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
