"""The source registry: which channels and groups exist, and what they are.

Storage is JSONL, one line per source, append-only. The reasons, from the spec:
a line is greppable, a new field needs no migration, the file reads back without
being loaded whole, and there is nothing in it that can come apart. A single YAML
document at this scale cannot say any of that -- a registry of a few hundred
sources runs to thousands of lines, and one username starting with `@` is
enough to make the entire file unreadable.

Append-only means a source is *updated* by writing a newer line for the same
username, and the last line wins. Nothing is ever rewritten in place except by
`compact()`, which is the only operation that rewrites the file.

Appending is NOT atomic on Windows -- the docstring here used to claim it was.
`open(..., "a")` is seek-then-write in the CRT and the pair can interleave: the
review measured two processes writing 600 records and 22 of them vanishing, every
survivor well-formed JSON so nothing downstream noticed. Every write below
therefore runs inside `config.FileGuard`, a cross-process mutex, and that is what
makes the claim true rather than the file mode.

The registry never holds a secret. `peer.access_hash` is the one field that
comes close, and it is stored with the fingerprint of the login session that
produced it -- see `resolve.py` for why a hash without that fingerprint is
worthless and must not be trusted.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path

import config as configmod

VALID_TYPES = ("channel", "group")
READABLE_TYPES = VALID_TYPES        # a "user" is a real peer and not a source
VALID_STATUS = ("alive", "gone", "private", "unknown")
VALID_FOUND_VIA = ("lyzem", "web", "link", "catalog", "manual", "registry",
                   # the account's own search box: `contacts.search`, which
                   # sees titles and usernames and nothing inside a message
                   "account")

# Telegram's own username rule, and the ONLY copy of it in the skill.
#
# There used to be two. `tg.py` refused anything that did not start with a
# letter and accepted three characters; this file accepted a leading underscore
# and required four. So `verify abc --write` spent a real GET, verified the
# name, and was then refused by the registry inside the same command -- and
# `_abcd` passed admission but could never be typed at the CLI. The rule below
# is the intersection of the two, so nothing both of them accepted is refused
# now: a letter first, four to thirty-two characters, letters, digits and
# underscores.
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
USERNAME_RULE = ("a Telegram username is 4-32 characters, starts with a letter, "
                 "and holds only letters, digits and underscores")


def valid_username(username: str) -> bool:
    return bool(USERNAME_RE.fullmatch((username or "").lstrip("@")))


class RegistryDamaged(RuntimeError):
    """The log holds lines nothing can read, so it must not be rewritten."""


class WouldDestroy(RuntimeError):
    """The operation would overwrite the only copy of something.

    Raised by `compact()` when `<name>.bak` already holds an earlier
    compaction's bytes: writing a new backup over it deletes the corrupt line
    the first `--force` compaction salvaged, and that line is by then the only
    place a truncated high-water mark still exists. `tg.py` maps this to
    `EXIT_WOULD_DESTROY = 10`; the message names the file and what is in it.
    """


class SourceRefused(ValueError):
    """The record offered to the registry is not one, and is not written.

    A hostile or mistyped argument leaves this module as a named refusal,
    never as a bare `AttributeError` from `"".lstrip` or a `TypeError` out of
    `json.dumps` three frames down. `append` writes nothing when it raises.
    """


class VocabularyUnreadable(RegistryDamaged):
    """`topics.json` cannot be read, so no source can be classified from it.

    A `RegistryDamaged` by inheritance rather than by category: both mean "a file
    this module reads is not something it can be read from", and `tg.py` already
    maps that family to a JSON refusal instead of a traceback. Without the base
    class a trailing comma in the vocabulary would still leave a traceback and
    exit 9, where a public entry point of this module owes a named refusal.
    """


# `first_seen` and `last_checked` are LOCAL calendar dates: stamping them in UTC
# silently backdates every check made between midnight and the local offset by a
# day, which is exactly when long runs tend to happen. The offset used to be
# a fixed `timezone(timedelta(...))` compiled into this file; it now comes from
# `config.local_tz()`, which reads the machine's own zone unless
# `TELEGRAM_RESEARCH_TZ` pins it.
today_local = configmod.today_local
now_local = configmod.now_local


def _no_duplicate_keys(pairs):
    """`json.loads` hook: a repeated key is a damaged line, not a last-wins vote.

    `{"members": 5000, "members": 1}` read back as 1 with nothing said. Our own
    writer cannot produce it, so any line that has it was hand-edited or spliced,
    and a silent winner is the wrong way to resolve that.
    """
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in one record")
        seen[key] = value
    return seen


@dataclass
class Source:
    """One Telegram source. `type` decides the entire read route, so it is
    mandatory before the record is allowed into the registry."""

    username: str                                   # without the @
    type: str | None = None                         # channel | group
    title: str | None = None
    description: str | None = None
    members: int | None = None
    topics: list[str] = field(default_factory=list)  # classification by FIELD, never by folder
    lang: str | None = None
    geo: str | None = None
    found_via: str | None = None
    first_seen: str | None = None
    last_checked: str | None = None
    preview: bool | None = None                     # does /s/ serve it -- measured once
    max_id_seen: int | None = None                  # the group cursor
    peer: dict | None = None                        # {id, access_hash, auth_session_fingerprint}
    # `None`, not `"unknown"`. `as_dict()` drops `None`, and that is what makes a
    # PARTIAL append safe: `Source(username="chan", max_id_seen=120)` says
    # nothing about the status and must not overwrite one. A default of
    # `"unknown"` was written on every append and won every merge, so a source
    # recorded `alive`, `gone` or `private` silently became `unknown` -- and
    # `gone` / `private` are exactly what `judge` recently learned to produce.
    # A caller that really means "unknown" still says so explicitly.
    status: str | None = None
    notes: str | None = None
    # NOT a fact about the source: a directive to `_merge`, and the only way a
    # wrong `type` can ever be corrected. `verify --write` sets it when the
    # type was read from a page fetched in that same call, never from cache.
    # It has to survive onto the line, because `_merge` runs at READ time over
    # the lines in the file -- a flag stripped at write time would never reach it.
    type_confirmed: bool | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and v != []}


class Registry:
    """A JSONL log of sources, read by streaming and written by appending."""

    def __init__(self, path: Path, *, guard_timeout: float = 20.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.guard_timeout = guard_timeout

    # -- the write mutex ---------------------------------------------------
    def _guard(self) -> configmod.FileGuard:
        return configmod.FileGuard(
            self.path.with_name(self.path.name + ".write"),
            timeout=self.guard_timeout, stale_after=120.0, label="registry",
        )

    # -- reading -----------------------------------------------------------
    def iter_raw(self):
        """Every line, in file order, including superseded ones.

        Read as BYTES and decoded per line, strictly. Three things are earned:

        * a BOM (which is what "UTF-8" means in Notepad) silently cost the FIRST
          record, so it is stripped;
        * one cp1251 byte anywhere used to raise `UnicodeDecodeError` out of this
          generator -- outside the per-line `try` below -- so `load()` AND
          `corrupt_lines()` both died and the other ten thousand lines became
          unreachable. The bad line lands in the corrupt bucket instead;
        * `errors="replace"` made that true only for bytes OUTSIDE a JSON string.
          A cp1251 byte inside one -- which is exactly where a Cyrillic channel
          title sits -- still parsed, and the record was admitted with a mojibake
          title, `ok: true`, and no damage flag anywhere; `compact()` then wrote
          the U+FFFD replacement characters back as the stored bytes and the
          original was gone. Decoding strictly is what makes the docstring's
          promise ("like every other bad line") true for that case too.

        A line that really does hold U+FFFD, encoded properly, still reads back
        as itself: the difference is now the bytes on disk, not the characters.
        """
        if not self.path.exists():
            return
        with self.path.open("rb") as fh:
            for lineno, raw in enumerate(fh, 1):
                if lineno == 1:
                    raw = raw.removeprefix(b"\xef\xbb\xbf")
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    # Salvage from a lossy decode: the username and the cursor
                    # are ASCII in every record our own writer produces, so they
                    # survive bytes the rest of the line did not.
                    lossy = raw.decode("utf-8", "replace").strip()
                    yield lineno, {"_corrupt": True,
                                   "_why": f"not UTF-8: {exc}",
                                   "_raw": lossy[:200], "_salvaged": _salvage(lossy)}
                    continue
                if not line or line.startswith("#"):
                    continue
                try:
                    rec = json.loads(line, object_pairs_hook=_no_duplicate_keys)
                except ValueError as exc:
                    # One corrupt line must never cost the other ten thousand.
                    # It is skipped and reported, not raised. `_salvaged` reads
                    # the WHOLE line, not the 200-character preview: our own
                    # writer sorts keys, so `username` is the last field in a
                    # record and a truncated line loses it first.
                    yield lineno, {"_corrupt": True, "_why": str(exc),
                                   "_raw": line[:200], "_salvaged": _salvage(line)}
                    continue
                if not isinstance(rec, dict):
                    yield lineno, {"_corrupt": True, "_why": "not a JSON object",
                                   "_raw": line[:200], "_salvaged": _salvage(line)}
                    continue
                yield lineno, rec

    def load(self) -> dict[str, dict]:
        """Collapse the log: username -> the newest record for it.

        A corrupt line is not simply skipped any more. When the NEWEST line for
        a username is the unreadable one, skipping it hands the caller the
        PREVIOUS record with nothing said -- and the previous record's
        `max_id_seen` is by definition older. Measured consequence: a truncated
        line holding `max_id_seen: 91234`, the line before it holding `120`, and
        `history <chan> --since-last` silently re-fetching 91 114 messages on
        the most expensive surface there is. `_MERGE_MAX` cannot help, because
        the newer value was not in the collapsed view at all.

        So a corrupt line is mined for the two things that can be recovered from
        its raw text -- the username it belonged to and any high-water mark in
        it -- and both are folded in: the cursor cannot rewind, and the record
        carries `damaged_lines` so every reader can see that it is incomplete.
        """
        out: dict[str, dict] = {}
        damage: dict[str, list[int]] = {}
        cursors: dict[str, int] = {}
        orphan: list[int] = []
        orphan_cursor = False
        for lineno, rec in self.iter_raw():
            if rec.get("_corrupt"):
                salvaged = rec.get("_salvaged") or {}
                key = _key(salvaged.get("username", ""))
                mark = salvaged.get("max_id_seen")
                if not key:
                    orphan.append(lineno)
                    orphan_cursor = orphan_cursor or mark is not None
                    continue
                damage.setdefault(key, []).append(lineno)
                if mark is not None:
                    cursors[key] = max(cursors.get(key, mark), mark)
                continue
            key = _key(rec.get("username", ""))
            if not key:
                continue
            existing = out.get(key)
            out[key] = _merge(existing, rec) if existing else rec
        # A damaged line never becomes a source: a record nothing could read is
        # not evidence that the source exists. It attaches to the record it
        # belongs to, and when there is no such record its line number joins the
        # unattributed bucket like any other unreadable line.
        for key, lines in damage.items():
            if key in out:
                out[key] = dict(out[key], damaged_lines=lines)
            else:
                orphan.extend(lines)
        for key, mark in cursors.items():
            if key in out:
                out[key] = _merge(out[key], {"max_id_seen": mark})
        if orphan:
            orphan = sorted(set(orphan))
            for key in out:
                extra = {"damaged_lines_unattributed": orphan}
                # Our own writer sorts keys, so `username` is the LAST field of
                # a record and a truncated line loses it first while keeping
                # `max_id_seen`. When that happens the high-water mark cannot be
                # attributed to anybody, and applying it to the wrong source
                # would skip unread messages for ever -- so it is not applied,
                # and every cursor in the file is declared suspect instead.
                # `_MERGE_MAX` cannot save this one: the newer value is not in
                # the collapsed view at all.
                if orphan_cursor:
                    extra["cursor_may_be_stale"] = True
                out[key] = dict(out[key], **extra)
        for key in out:
            out[key] = _sanitise(out[key])
        return out

    def damage_report(self) -> dict:
        """Everything unreadable in the file, and what could be read out of it.

        `registry stats` reported a bare `corrupt_lines`; `registry get`,
        `registry list`, `history` and `group` reported nothing, and those are
        the commands that act on the record. This is the detail behind the flags
        `load()` puts on the records themselves.

        It had no caller outside the tests, which is how a computed answer stays
        wrong without anybody noticing. `problems()` and the `RegistryDamaged`
        message `compact()` raises are both built from it now, so the operator
        who is refused a compaction is told WHICH source lost WHICH cursor
        rather than only which line numbers did not parse.
        """
        lines: list[dict] = []
        for lineno, rec in self.iter_raw():
            if not rec.get("_corrupt"):
                continue
            salvaged = rec.get("_salvaged") or {}
            lines.append({
                "line": lineno,
                "why": rec.get("_why", "unparsable"),
                "username": salvaged.get("username"),
                "max_id_seen": salvaged.get("max_id_seen"),
            })
        return {
            "corrupt_lines": [item["line"] for item in lines],
            "details": lines,
            "cursor_may_be_stale": any(
                item["max_id_seen"] is not None and not item["username"]
                for item in lines
            ),
        }

    def get(self, username: str) -> dict | None:
        return self.load().get(_key(username))

    def corrupt_lines(self) -> list[int]:
        return [ln for ln, rec in self.iter_raw() if rec.get("_corrupt")]

    def problems(self) -> list[tuple[int, str]]:
        """`corrupt_lines()` with the reason attached, for a human to act on.

        One scan definition, in `damage_report()`, so the three answers cannot
        drift apart.
        """
        return [(item["line"], item["why"])
                for item in self.damage_report()["details"]]

    # -- writing -----------------------------------------------------------
    @staticmethod
    def _stamp(source: "Source | dict") -> dict:
        """The record as it will be written, or a refusal naming what is wrong.

        Nothing hostile gets past here. `{"username": 12345}` used to raise
        `AttributeError` from `"".lstrip` and a value `json` cannot serialise
        raised `TypeError` from inside `append`, both of them bare and both of
        them from a public entry point.
        """
        if isinstance(source, Source):
            rec = source.as_dict()
        elif isinstance(source, dict):
            rec = dict(source)
        else:
            raise SourceRefused(
                f"a source must be a Source or a dict, not {type(source).__name__}"
            )
        # A record read out of `load()` carries fields computed on read. Writing
        # them back would turn a derived observation into a stored one that
        # nothing ever refreshes.
        for derived in DERIVED_FIELDS:
            rec.pop(derived, None)
        username = rec.get("username", "")
        if not isinstance(username, str):
            raise SourceRefused(
                f"username is {username!r}, which is not text — nothing is written"
            )
        rec["username"] = username.lstrip("@")
        if not rec["username"]:
            raise SourceRefused("a source with no username cannot be written")
        for key in _WHOLE_NUMBER_FIELDS:
            if key in rec and rec[key] is not None:
                mark = _as_mark(rec[key])
                if mark is None:
                    raise SourceRefused(
                        f"{key} is {rec[key]!r}, which is not a whole number of "
                        "messages — nothing is written"
                    )
                rec[key] = mark
        if rec.get("type") is not None and rec["type"] not in VALID_TYPES:
            raise SourceRefused(
                f"type is {rec['type']!r}; it decides the whole read route and "
                f"must be one of {VALID_TYPES}"
            )
        if rec.get("status") is not None and rec["status"] not in VALID_STATUS:
            raise SourceRefused(
                f"status is {rec['status']!r}, which is not one of {VALID_STATUS}"
            )
        if not rec.get("type_confirmed"):
            # A merge directive, and only ever a true one. `false` on a line is
            # noise that says nothing `_merge` does not already assume.
            rec.pop("type_confirmed", None)
        rec.setdefault("first_seen", today_local())
        rec["last_checked"] = today_local()
        return rec

    @staticmethod
    def _line(rec: dict) -> str:
        try:
            return json.dumps(rec, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise SourceRefused(
                f"the record cannot be written as JSON ({exc}) — nothing is written"
            ) from None

    def _heal_last_line(self, fh) -> None:
        """Start on a fresh line, whatever the previous writer managed.

        A crash mid-append leaves a line with no terminator, and the next append
        concatenates onto it -- so ONE interrupted write destroyed TWO records,
        the half-written one and the healthy one that followed. The docstring
        promised "at most the line being written"; this is what makes that true.
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if not size:
            return
        with self.path.open("rb") as probe:
            probe.seek(-1, 2)
            if probe.read(1) != b"\n":
                fh.write("\n")

    def append(self, source: Source | dict) -> dict:
        """Add or update one source. Returns the record actually written."""
        rec = self._stamp(source)
        line = self._line(rec)
        with self._guard():
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                self._heal_last_line(fh)
                fh.write(line + "\n")
        return rec

    def append_many(self, sources) -> int:
        # Every record is stamped and serialised BEFORE the guard is taken, so a
        # batch with one bad record in it writes none of it rather than half.
        lines = [self._line(self._stamp(source)) for source in sources]
        if not lines:
            return 0
        with self._guard():
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                self._heal_last_line(fh)
                for line in lines:
                    fh.write(line + "\n")
        return len(lines)

    def backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def compact(self, *, force: bool = False) -> int:
        """Rewrite the file keeping one line per username. Returns lines kept.

        Three refusals the earlier versions did not have, all of them permanent
        data loss otherwise:

        * **It will not run over corrupt lines.** `compact()` rebuilt the file
          from `load()`, which skips them, so the bytes of a half-written record
          -- and of the healthy record welded onto it -- were deleted for good,
          and the return value said `kept = 1` as if that were a success.
        * **It keeps the previous file.** `<name>.bak` is written before the
          replace, always, so the log before the compaction is one `mv` away.
        * **It will not overwrite a backup that is already there.** The
          operator is told `--force` "keeps the original in sources.jsonl.bak",
          runs it, and the corrupt bytes -- including a truncated newest line
          holding `max_id_seen: 91234` -- then exist ONLY in `.bak`. A second
          compaction days later found a clean file, asked for nothing, warned
          about nothing, and replaced `.bak` with the already-compacted content:
          the bytes were gone from both files, permanently, and this skill's
          own rule that it never deletes anything by itself with them.

        `force=True` compacts anyway, and says in the refusal message that it
        replaces the existing backup. The whole operation holds the write guard,
        so an append can no longer land between the read and the replace and be
        discarded.
        """
        with self._guard():
            report = self.damage_report()
            damaged = [(item["line"], item["why"]) for item in report["details"]]
            if damaged and not force:
                shown = report["details"][:5]
                where = ", ".join(
                    f"line {item['line']} ({item['why']}"
                    + (f", @{item['username']}" if item["username"] else "")
                    + (f", max_id_seen {item['max_id_seen']}"
                       if item["max_id_seen"] is not None else "")
                    + ")"
                    for item in shown
                )
                more = "" if len(damaged) <= 5 else f" and {len(damaged) - 5} more"
                raise RegistryDamaged(
                    f"{self.path} has {len(damaged)} unreadable line(s): {where}{more}. "
                    "Compaction rebuilds the file from the lines that DO parse, so "
                    "running it now would delete those bytes permanently. Repair "
                    "them, or run `tg.py registry compact --force` (force=True from "
                    "the Python API) to compact anyway and keep the original in "
                    f"{self.path.name}.bak."
                )
            backup = self.backup_path()
            if not force:
                self._refuse_to_lose_the_backup(backup)
            collapsed = {
                key: {k: v for k, v in rec.items() if k not in DERIVED_FIELDS}
                for key, rec in self.load().items()
            }
            if self.path.exists():
                # Byte-exact, not text: the whole point of the backup is the
                # bytes `load()` could not read. `atomic_write_text` is the
                # guarded write everything here goes through, and it writes
                # TEXT, so the bytes ride through it as latin-1 -- the one
                # codec that maps every byte 0x00-0xFF to a character and back
                # unchanged. Reading with `read_bytes_shared` keeps another
                # process's `os.replace` from failing against our own handle.
                data = configmod.read_bytes_shared(self.path)
                configmod.atomic_write_text(
                    backup, data.decode("latin-1"), encoding="latin-1")
            body = "".join(
                json.dumps(collapsed[key], ensure_ascii=False, sort_keys=True) + "\n"
                for key in sorted(collapsed)
            )
            # A pid-stamped temp name, and a retried replace: the shared
            # `.compacting` name crashed one of two concurrent compactions with
            # an unhandled PermissionError in 3 of 5 trials.
            configmod.atomic_write_text(self.path, body)
            return len(collapsed)

    def _refuse_to_lose_the_backup(self, backup: Path) -> None:
        """What is already in `<name>.bak` is not ours to overwrite."""
        try:
            if not backup.stat().st_size:
                return
        except OSError:
            return
        held = Registry(backup)
        names = sorted(held.load())
        sample = ", ".join(f"@{n}" for n in names[:3])
        if len(names) > 3:
            sample += f" and {len(names) - 3} more"
        corrupt = held.corrupt_lines()
        raise WouldDestroy(
            f"{backup} already exists and holds {len(names)} source(s)"
            + (f" ({sample})" if sample else "")
            + (f" plus {len(corrupt)} unreadable line(s) at {corrupt[:5]}"
               if corrupt else "")
            + " from an earlier compaction. Compacting again writes a new backup "
            "over it, and those bytes — the ones an earlier `--force` compaction "
            "salvaged — exist nowhere else. Move the backup aside and keep it, or "
            "run `tg.py registry compact --force` (force=True from the Python API) "
            "to replace it deliberately."
        )


def _key(username: str) -> str:
    return (username or "").lstrip("@").strip().lower()


# Fields computed on READ and never written back. `compact()` rebuilds the file
# from the collapsed view, so anything derived has to be stripped there or it
# becomes a stored field that nothing maintains.
DERIVED_FIELDS = ("damaged_lines", "damaged_lines_unattributed",
                  "cursor_may_be_stale", "type_conflict", "unreadable_fields")

# Fields that are a count of messages and nothing else. A hand repair -- the one
# `RegistryDamaged` explicitly asks for -- or any other writer can put a quoted
# number in one of them, and `"120"` beat a stored `91234` outright: `max()`
# raised `TypeError`, the `except` swallowed it, and the fall-through stored the
# newer value anyway. That is a cursor rewound by 91 114 messages, permanently,
# because every later integer loses the same race against the stored string.
_WHOLE_NUMBER_FIELDS = ("max_id_seen", "members")


def _as_mark(value):
    """`value` as a whole non-negative number, or `None` if it cannot be one.

    A quoted number is read rather than rejected -- `"120"` from a hand repair
    means 120 and nothing else, and dropping it would rewind the cursor just as
    surely as trusting it. Anything ambiguous (a float with a fraction, a
    negative, a bool, a dict) is refused instead of guessed.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d{1,18}", value.strip()):
        return int(value.strip())
    return None


def _sanitise(rec: dict) -> dict:
    """The collapsed record with every count readable, or said to be missing.

    `load()` used to hand back whatever was on the line. `tg.py` then computed
    `max(cursor, known.get("max_id_seen") or 0)` on it and died with a bare
    `TypeError: '>' not supported between instances of 'str' and 'int'` -- so a
    single hand-edited line stopped `history --write` outright. A value nothing
    can read is not a cursor: it is dropped, and the record says so where every
    reader can see it rather than in a traceback three commands later.
    """
    bad: dict = {}
    for key in _WHOLE_NUMBER_FIELDS:
        if key not in rec or rec[key] is None:
            continue
        mark = _as_mark(rec[key])
        if mark is None:
            bad[key] = repr(rec[key])
        elif mark != rec[key]:
            rec = dict(rec, **{key: mark})
    if bad:
        rec = {k: v for k, v in rec.items() if k not in bad}
        rec["unreadable_fields"] = bad
    return rec

_SALVAGE_USERNAME = re.compile(r'"username"\s*:\s*"([A-Za-z0-9_@.\-]{1,64})"')
_SALVAGE_MAX_ID = re.compile(r'"max_id_seen"\s*:\s*(\d{1,18})')


def _salvage(line: str) -> dict:
    """What can still be read out of a line JSON cannot parse.

    Only two fields matter and only one of them can do harm by being missing.
    The username says WHOSE record was damaged, so the damage can be reported on
    the record rather than on the file as a whole; `max_id_seen` is a high-water
    mark, and recovering it is what stops a truncated newest line from handing
    the caller an older cursor and buying 91 000 re-fetched messages.

    Deliberately regular expressions over the raw text, not a JSON repair. A
    line that does not parse has no structure left to trust, and the two values
    below are the only ones this file is willing to act on without it.
    """
    out: dict = {}
    if not line:
        return out
    match = _SALVAGE_USERNAME.search(line)
    if match:
        name = match.group(1).lstrip("@")
        if name:
            out["username"] = name
    match = _SALVAGE_MAX_ID.search(line)
    if match:
        try:
            out["max_id_seen"] = int(match.group(1))
        except ValueError:
            pass
    return out


# Fields where "newest wins" is the wrong rule, and why.
#
# `max_id_seen` is a high-water mark, not an observation: a partial read, a
# resumed walk or a page of recent history writes what IT saw, and last-write-wins
# rewound the cursor -- measured going from 91234 back to 120, which costs 91114
# re-fetched messages on the most expensive path there is (one HTTP GET per
# message). `first_seen` is the opposite: `append` stamps today's date on any
# record that arrives without one, so a later cheap check used to overwrite the
# real first sighting with today.
_MERGE_MAX = ("max_id_seen",)
_MERGE_MIN = ("first_seen",)


def _keep_higher(previous, new):
    """The high-water mark of two values. A value that is not one never wins.

    `try: max(...) except TypeError: pass` fell THROUGH to `out[k] = v`, so the
    unreadable value won every time it was newer -- the exact opposite of what
    the exception handler looks like it is doing. The rule now has no
    fall-through: if only one of the two can be read as a mark, that one stands;
    if neither can, the stored value stays and `_sanitise` reports it.
    """
    old_mark, new_mark = _as_mark(previous), _as_mark(new)
    if old_mark is None and new_mark is None:
        return previous
    if old_mark is None:
        return new_mark
    if new_mark is None:
        return old_mark
    return max(old_mark, new_mark)


def _keep_earlier(previous, new):
    """The earlier of two ISO dates; a non-date never overwrites a date."""
    old_ok, new_ok = isinstance(previous, str), isinstance(new, str)
    if old_ok and new_ok:
        return min(previous, new)
    if old_ok:
        return previous
    if new_ok:
        return new
    return previous


def _merge(old: dict, new: dict) -> dict:
    """Newer wins per field, but a newer `null` never erases an older value.

    A cheap check that only looked at the landing page must not wipe the
    `max_id_seen` a full read paid for -- and must not walk it backwards either.

    `type` is the third exception and the loudest. It is the field that decides
    the entire read route, and it was plain newest-wins: a `verify chan --write`
    that read a rate-limit interstitial, a supergroup migration or the same
    misread `SKILL.md` already documents in the `group --write` direction flipped
    a verified channel to `group`, and from that moment `search` and `history`
    refused it with exit 6 -- from one command that printed `ok: true` and
    `updated: 1`. The stored value now stands and the contradiction is recorded
    where every reader of the record can see it. `type_confirmed: true` on the
    incoming record is the deliberate correction: a caller that really did
    establish the new type says so.
    """
    out = dict(old)
    if (new.get("type") and out.get("type") and new["type"] != out["type"]
            and not new.get("type_confirmed")):
        out = dict(out, type_conflict={
            "stored": out["type"], "seen": new["type"], "at": now_local(),
            "note": "the stored type stands; the read route is not changed by a "
                    "contradicting check. Re-verify with type_confirmed to correct it.",
        })
        new = {k: v for k, v in new.items() if k != "type"}
    elif new.get("type_confirmed") and new.get("type"):
        out.pop("type_conflict", None)
    for k, v in new.items():
        if k == "type_confirmed":
            continue
        if v is None or v == []:
            continue
        previous = out.get(k)
        if k in _MERGE_MAX and previous is not None:
            out[k] = _keep_higher(previous, v)
            continue
        if k in _MERGE_MIN and previous is not None:
            out[k] = _keep_earlier(previous, v)
            continue
        out[k] = v
    return out


# --------------------------------------------------------------------------
# Admission -- what may enter the registry
# --------------------------------------------------------------------------
# The gate SHAPE is adapted from jackvale/rectg's `filter_rules.py` (Apache-2.0):
# validity first, then a content gate, then size thresholds, with the reason
# always returned rather than the candidate silently vanishing. No line of that
# file is copied -- its thresholds are tuned to a 534-record Chinese directory
# and its content gate is a Chinese harm-keyword list, neither of which
# transfers. What transfers is the discipline of a named reason per rejection.


@dataclass
class AdmissionRules:
    min_channel_members: int = 100
    min_group_members: int = 50
    require_type: bool = True
    require_members: bool = True     # a card with no member count fails the floor
    allow_status: tuple[str, ...] = ("alive",)
    banned_usernames: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Verdict:
    admit: bool
    reason: str
    action: str = "insert"        # insert | update | reject
    # Things the caller should PRINT even when the candidate is admitted. A
    # verdict that admits and says nothing is how a channel's read route got
    # flipped by a command that reported `updated: 1` and no more.
    warnings: tuple[str, ...] = ()
    # The status the registry should record for this source, when the card says
    # something about it that the admission rules would otherwise throw away.
    record_status: str | None = None


def judge(card: dict, rules: AdmissionRules, existing: dict | None = None) -> Verdict:
    """Decide whether a candidate belongs in the registry, and say why.

    A rejection is a sentence, never a silent drop: the run report has to be able
    to state how many candidates were refused and on what grounds, otherwise a
    discovery stage that quietly threw away the good half looks identical to one
    that found nothing.
    """
    username = (card.get("username") or "").lstrip("@")
    if not username:
        return Verdict(False, "no username", "reject")
    if not valid_username(username):
        return Verdict(
            False,
            f"{username!r} is not a valid Telegram username — {USERNAME_RULE}",
            "reject",
        )
    if _key(username) in {_key(b) for b in rules.banned_usernames}:
        return Verdict(False, "on the ban list", "reject")

    if card.get("type") == "user":
        return Verdict(
            False,
            "a personal account, not a channel or a group — nothing to read here",
            "reject",
        )
    if card.get("exists") is False:
        # A source that has DIED is news about a source the registry already
        # holds, not a candidate for admission. Refusing the card outright left
        # `status` at `alive` for ever -- `VALID_STATUS` listed `gone` and
        # `private` and nothing in the skill could produce either -- so every
        # later run kept spending requests on a name that is not there any more.
        # A name nobody knew is still refused: a dead name is not a source.
        if existing:
            dead = "private" if card.get("taken") else "gone"
            return Verdict(
                True,
                f"known source is no longer readable — status recorded as {dead}",
                "update",
                warnings=(
                    f"{username}: was in the registry and is now {dead}; the record "
                    "keeps its cursor and its history, and the status says so.",
                ),
                record_status=dead,
            )
        if card.get("taken"):
            return Verdict(
                False,
                "the name is taken but serves no readable channel or group",
                "reject",
            )
        return Verdict(False, "no such name on Telegram", "reject")

    status = card.get("status", "unknown")
    if status not in rules.allow_status and status != "unknown":
        return Verdict(False, f"status is {status}", "reject")

    ptype = card.get("type")
    if rules.require_type and ptype not in VALID_TYPES:
        return Verdict(
            False,
            "type is unknown — it decides the whole read route and is never guessed",
            "reject",
        )

    # A card is data from outside: `members: "many"` used to reach
    # `members < floor` and leave a bare `TypeError` out of a public entry point.
    # A size nothing can read is not a size, so it becomes the missing one it is
    # -- and a floor that cannot be applied is not waived.
    members = _as_mark(card.get("members"))
    floor = rules.min_channel_members if ptype == "channel" else rules.min_group_members
    if members is None:
        # `if members is not None` waived the floor entirely for a card whose
        # member count did not parse, which is the one case where the floor is
        # most likely to be the thing that matters: a landing page that did not
        # give up its size is not evidence of size. SKILL.md states the floor as
        # an unconditional refusal, and it now is one.
        if rules.require_members:
            return Verdict(
                False,
                f"no member count on the card, so the floor of {floor} cannot be "
                "applied — and a floor that cannot be applied is not waived",
                "reject",
            )
    elif members < floor:
        return Verdict(False, f"{members} members is below the floor of {floor}", "reject")

    warnings: tuple[str, ...] = ()
    if existing and existing.get("type") and ptype and ptype != existing["type"]:
        warnings = (
            f"{username}: this check says {ptype}, the registry says "
            f"{existing['type']}. The stored type stands — it decides the read "
            "route and a contradicting check does not get to change it silently.",
        )

    if existing:
        return Verdict(True, "already known — fields refreshed", "update",
                       warnings=warnings)
    return Verdict(True, "admitted", "insert", warnings=warnings)


# --------------------------------------------------------------------------
# Topics -- classification by field, and more than one label per source
# --------------------------------------------------------------------------
# rectg's `categorize.py` is single-label and first-match-wins over 20 hardcoded
# Chinese buckets. Ours has to be multi-label because `topics` is a list, and it
# has to be re-pointable at whatever topics a project collects, so the vocabulary is
# data loaded from a file rather than a constant in the code.

PROMO_PATTERNS = [
    r"https?://\S+",
    r"@[A-Za-z0-9_]{4,32}",
    r"по вопросам рекламы[^.\n]*",
    r"for ads?[^.\n]*",
    r"\bad(vertis(ing|ement)s?)?\b",
]


def normalise(text: str | None) -> str:
    """Lowercase, strip promo boilerplate and URLs, fold width and accents.

    Channel cards are half advertisement. Leaving the ad copy in makes every
    channel look like it is about advertising, which is the failure mode the
    same step exists to prevent in rectg.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    for pat in PROMO_PATTERNS:
        out = re.sub(pat, " ", out, flags=re.I)
    out = re.sub(r"[^\w\s\-]+", " ", out, flags=re.UNICODE)
    return re.sub(r"\s+", " ", out).strip().lower()


# How a keyword is allowed to meet a word.
#
# Plain substring matching produced twelve mis-labels on realistic channel cards,
# every one of them executed: «Такси Пхукет» became finance_payments on `tax`,
# "Business chat" became transport on `bus`, «Барахолка» became food_restaurants
# on `бар`, «промокод» became technology_software on `код`. A label nobody can
# see is wrong is worse than no label, because labels select sources.
#
# Whole-word matching alone is not the answer either: `topics.json` documents
# stems ("'аренд' catches аренда/аренду/аренды/арендовать") and a Russian
# vocabulary is unusable without them. So: a keyword must start a word, and the
# tail it is allowed to carry depends on the script -- up to three letters of
# inflection in Cyrillic, a plain plural in Latin and only for a keyword long
# enough for that to mean something.
#
# The Cyrillic tail is a closed set rather than a length cap. A cap wide enough
# for the README's own example ('аренд' -> 'арендовать', five letters of tail)
# is also wide enough for «банк» -> «банкомат» and «спорт» -> «спортсмен»,
# neither of which is an inflection of the keyword at all. Russian inflection is
# a small finite list, so the list is what gets used.
_RU_TAILS = frozenset("""
а у ы и е о ю я ь
ой ей ою ею ом ем ов ев ам ям ах ях ей ий ый ая яя ое ее ые ие ую юю ии ья ье
ами ями ого его ому ему ым им ых их ов ев ин ина ины
ать ять ить еть уть ся тся ться ал ял ил ел ла ло ли ет ут ют ит ят аю яю ем им
овать евать ирова ируется
ьный ьная ьное ьные ьного ьному ьным ьных
""".split())
_LATIN_PLURALS = ("s", "es")
_MIN_LATIN_PLURAL_STEM = 4
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text, flags=re.UNICODE)


def _token_matches(token: str, keyword: str) -> bool:
    if token == keyword:
        return True
    if not token.startswith(keyword):
        return False
    tail = token[len(keyword):]
    if _CYRILLIC.search(keyword):
        return tail in _RU_TAILS
    return len(keyword) >= _MIN_LATIN_PLURAL_STEM and tail in _LATIN_PLURALS


def _phrase_at(tokens: list[str], start: int, words: list[str]) -> bool:
    if start + len(words) > len(tokens):
        return False
    for offset, word in enumerate(words):
        token = tokens[start + offset]
        if offset < len(words) - 1:
            if token != word:               # only the last word may be inflected
                return False
        elif not _token_matches(token, word):
            return False
    return True


def _keyword_hits(tokens: list[str], keyword: str) -> bool:
    words = _tokens(keyword)
    if not words:
        return False
    if len(words) == 1:
        return any(_token_matches(t, words[0]) for t in tokens)
    return any(_phrase_at(tokens, i, words) for i in range(len(tokens)))


class TopicClassifier:
    """Keyword matching, multi-label, with the matched keyword kept as evidence.

    Not machine learning and not an LLM call: at discovery time this runs over
    every candidate, and a classification a human cannot audit is one nobody
    will ever correct. Every label a source carries can be traced to the word
    that put it there.
    """

    def __init__(self, vocabulary: dict[str, list[str]]):
        # Two guards, both earned. A key starting with `_` is documentation, not
        # a topic -- the shipped vocabulary carries a `_README`. And a value that
        # is a bare string must be rejected rather than iterated: iterating a
        # string yields its CHARACTERS, every one of which matches almost every
        # source, so one prose line in the file silently labels the whole
        # registry with it. Measured on the first live run, where `_README`
        # attached itself to three sources out of four.
        self.vocabulary = {}
        self.skipped: list[str] = []
        if not isinstance(vocabulary, dict):
            raise VocabularyUnreadable(
                f"the topic vocabulary is {type(vocabulary).__name__}, not an "
                "object of topic -> keywords. No source can be classified from it."
            )
        for topic, words in vocabulary.items():
            if topic.startswith("_"):
                continue
            if not isinstance(words, (list, tuple, set)):
                self.skipped.append(topic)
                continue
            self.vocabulary[topic] = [normalise(w) for w in words if str(w).strip()]

    @classmethod
    def from_file(cls, path: Path) -> "TopicClassifier":
        """The vocabulary from disk, or a named refusal.

        A `topics.json` with a trailing comma in it used to leave
        `json.JSONDecodeError` -- a bare `ValueError` -- out of `get_classifier`
        and out of `tg.py verify` as a traceback.
        """
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise VocabularyUnreadable(
                f"the topic vocabulary at {path} could not be read: {exc}"
            ) from None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise VocabularyUnreadable(
                f"the topic vocabulary at {path} does not parse: {exc}. Every "
                "source verified from now on would carry no topics, and nothing "
                "would say why."
            ) from None
        return cls(data)

    def classify(self, *texts: str | None) -> tuple[list[str], dict[str, list[str]]]:
        """Return (topics, evidence). Empty list means 'undecided', not 'none'."""
        haystack = " ".join(normalise(t) for t in texts if t)
        tokens = _tokens(haystack)
        topics: list[str] = []
        evidence: dict[str, list[str]] = {}
        for topic, words in self.vocabulary.items():
            hits = [w for w in words if w and _keyword_hits(tokens, w)]
            if hits:
                topics.append(topic)
                evidence[topic] = hits
        return topics, evidence
