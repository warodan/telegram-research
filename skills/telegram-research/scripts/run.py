"""The run folder: what a search leaves behind, and in what shape.

Two consumers, one output. A person reads the report; a calling agent swallows
the JSON and the notes. The JSON is primary and always written; the report is
written for the reader and does not get in the other consumer's way.

One run, one folder, under `<root>/telegram-runs/<date>-<slug>/`:

    <run>/
      brief.md                the question, the sources, the queries, the ceilings
      registry-delta.jsonl    the sources THIS run added or refreshed
      queries.md              every query by round, and what each one found
      queries.json            the same log, machine-readable and reloadable
      posts.jsonl             the posts, with the query that surfaced each one
      notes/<agent>.md        agent notes
      notes/sources/          ORIGINALS: the raw HTML of every page read
      fetchlog.jsonl          one line per network act
      run.json                ceilings, spend, stop reasons
      acceptance.json         the gate verdict -- without it the run is unaccepted
      report.md               the report

`notes/sources/` is the part that makes a quotation checkable. A claim whose
page was never saved cannot be verified by anybody -- so the originals are not
an optional nicety, they are the difference between a finding and an assertion.

`tg.py accept` is the folder gate, and what it demands -- `schema`, `depth`,
`gate`, `agents` and a run identity in `run.json`, `kind: "fetch"` on every
fetch-log line, at least one non-empty note, and an `acceptance.json` -- is
written here rather than reconstructed by hand afterwards.

Nothing is ever written to `raw/`. That directory carries an immutability
invariant and a run's output gets rewritten by every checking pass.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

import config as cfg_module

# The marker that says "this directory is a run folder of this skill's".
# `require_run_folder` reads it, so it is a contract and not decoration: widen
# or rename it and every existing run folder stops being recognised.
RUN_SCHEMA = "telegram-research.run/1"

# The three rows of the depth table. `accept` fails a run whose depth is
# anything else, so the set is not ours to widen quietly.
DEPTHS = ("quick", "normal", "deep")

# Every run folder of every run, in one place under the project root. The name
# is part of the contract `SKILL.md` states; change it and old folders are
# still run folders but nothing lists them together any more.
RUNS_DIR = "telegram-runs"

FALLBACK_ROUNDS = 3
FALLBACK_POST_FLOOR = 3
FALLBACK_REQUESTS = 400


def depth_ceilings(depth: str, budgets=None) -> dict:
    """What `--depth` actually changes, measured from the configured normal run.

    Two things were broken here and they were one thing. `--depth deep` stored
    the word `deep` and left `max_rounds: 3`, `min_new_posts: 3`,
    `max_requests: 400` -- exactly what `normal` produced -- so the flag looked
    like a decision and was not one. And `config.Budgets` declared `max_rounds`,
    `min_new_posts_per_round` and `max_requests_per_run`, none of which any line
    of the skill read. They are the NORMAL row here, so the config is what
    depth moves around rather than something to keep in sync with it by hand.
    """
    if depth not in DEPTHS:
        raise ValueError(f"depth must be one of {', '.join(DEPTHS)}, not {depth!r}")
    rounds = int(getattr(budgets, "max_rounds", FALLBACK_ROUNDS))
    floor = int(getattr(budgets, "min_new_posts_per_round", FALLBACK_POST_FLOOR))
    requests = int(getattr(budgets, "max_requests_per_run", FALLBACK_REQUESTS))
    if depth == "quick":
        return {"max_rounds": 1, "min_new_posts": floor,
                "max_requests": max(1, requests // 3)}
    if depth == "deep":
        return {"max_rounds": rounds + 2, "min_new_posts": max(1, floor - 1),
                "max_requests": requests * 2}
    return {"max_rounds": rounds, "min_new_posts": floor, "max_requests": requests}


def _configured_budgets():
    """The configured budgets, or None if the environment cannot produce them.

    Only `Brief.from_file` needs this, and only because the CLI did not pass
    budgets down that path. A broken `TELEGRAM_RESEARCH_CONFIG` has already been
    reported by the time a brief is read -- every command loads the config
    first -- so falling back to the shipped numbers here cannot hide a
    configuration error, it only avoids raising a second one from a worse place.
    """
    try:
        return cfg_module.load().budgets
    except cfg_module.ConfigError:
        return None


# A run folder is named for a LOCAL calendar day and `run.json` stamps local
# times, so both have to follow the operator rather than a compiled-in city.
# A fixed `timezone(timedelta(...))` used to be a module constant here: right on
# exactly one machine, a day boundary at the wrong hour on every other, and
# unable to follow DST. `config.local_tz()` reads the machine's own zone unless
# `TELEGRAM_RESEARCH_TZ` pins it to a fixed offset.
today_local = cfg_module.today_local
now_local = cfg_module.now_local


def slugify(text: str, limit: int = 40) -> str:
    out = re.sub(r"[^\w]+", "-", (text or "").strip().lower(), flags=re.UNICODE)
    return out.strip("-")[:limit] or "run"


# Characters a path component may not carry on Windows, plus the separators.
_UNSAFE_IN_COMPONENT = re.compile(r'[<>:"|?*\x00-\x1f]+')


def path_component(value: str, *, default: str, limit: int = 60) -> str:
    """One folder name, guaranteed to stay one folder name.

    Caller text used to be interpolated straight into the run folder's path
    while only `brief.question` went through `slugify`, so a value carrying `/`
    split the path in two and one carrying `..` left `--root` altogether --
    measured: a run folder created three levels above the root it was told to
    write inside.

    This is not a security boundary; the caller owns the flags. It is the
    invariant `references/cli.md` states, that a run lands under
    `<root>/telegram-runs/<date>-<slug>/` and nowhere else.

    Case is kept, unlike `slugify`: `T2` and `t2` are different directories on
    any filesystem that cares.
    """
    text = str(value or "").strip().replace("\\", "/")
    parts = [p for p in text.split("/") if p and p not in (".", "..")]
    text = "-".join(parts)
    text = _UNSAFE_IN_COMPONENT.sub("-", text)
    # A trailing dot or space is legal in a string and illegal in an NTFS name.
    text = text.strip(" .")[:limit].strip(" .")
    return text or default


class WriteResult(tuple):
    """`(written, suppressed)` -- how many posts were banked and how many were
    already there. A tuple so the old `int` call sites that only ever needed the
    first number keep working, with names so a new one reads."""

    __slots__ = ()

    def __new__(cls, written: int, suppressed: int = 0):
        return super().__new__(cls, (int(written), int(suppressed)))

    @property
    def written(self) -> int:
        return self[0]

    @property
    def suppressed(self) -> int:
        return self[1]

    def __int__(self) -> int:
        return self[0]

    def __index__(self) -> int:
        return self[0]

    def __repr__(self) -> str:
        return f"WriteResult(written={self[0]}, suppressed={self[1]})"


def post_key(post) -> tuple | None:
    """What makes two rows of `posts.jsonl` the same post.

    `(username, id)` is the contract; a post that has no id falls back to its
    permalink, which is the same identity written a different way. A row with
    neither is never treated as a duplicate of anything -- collapsing two
    unidentifiable rows into one would lose evidence to save a line.
    """
    data = post if isinstance(post, dict) else (
        post.as_dict() if hasattr(post, "as_dict") else dict(post))
    username = data.get("username")
    ident = data.get("id")
    if ident is not None:
        return ("id", str(username or "").lower(), ident)
    url = data.get("url")
    if url:
        return ("url", str(url))
    return None


@dataclass
class Brief:
    """What a run was asked to do. Filled before any request is made.

    When the skill is called from inside another agent's pass the brief arrives
    already written and the scoping stage collapses into a check that the fields
    are present. That matters because a subagent cannot ask a question: anything
    that needs deciding is decided here, before the run starts.
    """

    question: str
    topic: str = "general"
    lang: str | None = None
    geo: str | None = None
    since: str | None = None
    until: str | None = None
    depth: str = "normal"
    max_rounds: int = 3
    min_new_posts: int = 3
    max_requests: int = 400
    account_allowed: bool = False
    seed_sources: list = field(default_factory=list)
    seed_queries: list = field(default_factory=list)
    # `user`  -- a person asked for this run directly.
    # `agent` -- it was opened from inside another pass, which wrote the brief.
    caller: str = "user"

    def __post_init__(self) -> None:
        if self.depth not in DEPTHS:
            raise ValueError(
                f"depth must be one of {', '.join(DEPTHS)}, not {self.depth!r}. "
                "The run folder gate refuses any other value."
            )

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_file(cls, path: Path, *, budgets=None) -> "Brief":
        """A brief from JSON, with `depth` meaning the same thing it means elsewhere.

        This path used to construct the dataclass directly, so `max_rounds`,
        `min_new_posts` and `max_requests` fell back to the FIELD DEFAULTS --
        which are `normal`'s row -- and a brief saying `"depth": "deep"` ran on
        3 rounds and 400 requests while `brief.md` printed the contradiction
        side by side. That is exactly the defect `depth_ceilings` was written to
        fix, surviving intact on the one path a calling agent actually uses:
        SKILL.md names `--brief <file.json>` as THE entry point when the skill
        is called from inside another pass.

        `TELEGRAM_RESEARCH_CONFIG` never reached here either, for the same reason,
        so the promise that an override moves all three rows at once was false
        whenever `--brief` was used. When the caller does not hand us budgets we
        load them, rather than quietly using the shipped numbers.

        A ceiling the file states explicitly still wins: the depth row fills in
        what the file left out, and nothing more. `brief.json` written by
        `newrun` carries all three, so re-reading a run's own brief is unchanged.
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path} must hold a JSON object, not {type(data).__name__}")
        known = {f for f in cls.__dataclass_fields__}
        values = {k: v for k, v in data.items() if k in known}
        depth = values.get("depth", cls.__dataclass_fields__["depth"].default)
        if depth in DEPTHS:
            if budgets is None:
                budgets = _configured_budgets()
            ceilings = depth_ceilings(depth, budgets)
            for key, value in ceilings.items():
                values.setdefault(key, value)
        return cls(**values)

    @classmethod
    def for_depth(cls, depth: str = "normal", *, budgets=None, **kwargs) -> "Brief":
        """A brief whose ceilings come from its depth, then from the caller.

        Anything the caller states explicitly wins; everything else is the depth
        level's, and the depth level's come from `config.Budgets`. This is what
        makes `--depth` a decision rather than a label.
        """
        values = depth_ceilings(depth, budgets)
        values.update({k: v for k, v in kwargs.items() if v is not None})
        return cls(depth=depth, **values)

    def redacted(self) -> "Brief":
        """A copy with every credential-shaped substring blanked.

        `Run.open()` writes this one, never the original. A session string
        pasted into the question is exactly how the incident this guards against
        happened, and stdout showing `<redacted>` while the disk held the key is
        what made it invisible for a day.
        """
        return Brief(**cfg_module.redact_obj(self.as_dict()))

    def to_markdown(self) -> str:
        lines = [
            "# Brief", "",
            f"**Question.** {self.question}", "",
            "| field | value |", "| --- | --- |",
            f"| topic | {self.topic} |",
            f"| language | {self.lang or 'any'} |",
            f"| geography | {self.geo or 'any'} |",
            f"| window | {self.since or 'any'} .. {self.until or 'any'} |",
            f"| depth | {self.depth} |",
            f"| round ceiling | {self.max_rounds} |",
            f"| new-post floor | {self.min_new_posts} |",
            f"| request ceiling | {self.max_requests} |",
            f"| account allowed | {'yes' if self.account_allowed else 'NO'} |",
            f"| caller | {self.caller} |",
            "",
        ]
        if self.seed_sources:
            lines += ["**Seed sources.** " + ", ".join(self.seed_sources), ""]
        if self.seed_queries:
            lines += ["**Seed queries.** " + ", ".join(f"`{q}`" for q in self.seed_queries), ""]
        return "\n".join(lines)


class Run:
    """One search, and the folder it fills.

    A run outlives the process that started it. `newrun` writes the brief,
    `search` adds posts a minute later, `report` reads the lot an hour after
    that -- three processes, one folder, and the arithmetic has to survive the
    gaps between them. So state is loaded from disk on the way in (`attach`) and
    merged on the way out (`finish`), never reset. The version of this class
    that rebuilt itself empty in every process reported the last command's spend
    as the whole run's, and the report inherited the false number.
    """

    def __init__(self, root: Path, brief: Brief):
        self.root = Path(root)
        self.brief = brief
        self.started = now_local()
        self.finished: str | None = None
        self.stop_reasons: list[str] = []
        self.counters: dict[str, int] = {}
        self.agents: list[str] = []
        self._existing: dict = {}
        # What was already on disk when this process attached. `finish` applies
        # the DIFFERENCE between this and `counters`, never the total: two
        # processes that both attached at `requests: 15` and both spent 10 used
        # to write 25, and whichever wrote second erased the other's spend.
        self._baseline: dict[str, int] = {}
        (self.root / "notes" / "sources").mkdir(parents=True, exist_ok=True)

    # -- construction ------------------------------------------------------
    @classmethod
    def open(cls, brief: Brief, *, root: Path) -> "Run":
        """A new run folder: `<root>/telegram-runs/<date>-<slug>/`.

        One shape, always, whoever asked for the run and whatever `--topic`
        says. The topic is a field of the brief and a key the registry sorts
        by; it is not part of the path, so a run is where the path says it is
        and `ls telegram-runs` is the whole list.

        The brief is redacted BEFORE anything derived from it touches the disk,
        the folder name included. A folder name survives being deleted from the
        file it was copied out of: it is in shell history, in the fetch log's
        absolute paths, and in any listing anybody pastes anywhere.

        A second run of the same question on the same day gets its own folder.
        Sharing one meant the second `newrun` overwrote the first's brief while
        `posts.jsonl` kept appending, so the report double-counted every post
        and cited a corpus that was two runs mixed together.
        """
        brief = brief.redacted()
        slug = f"{today_local()}-{slugify(brief.question)}"
        # An absolute base is left exactly as it was handed over -- resolving it
        # would rewrite a caller's own path through symlinks and short names for
        # no reason. A relative one is anchored before anything is created, so
        # that `run.root`, the `run` field, `run.json`'s `root` and the `next:`
        # line are all a path that works from any directory. `tg.py` passes
        # `cfg.root`, which is absolute already; this is the guard for
        # every other caller.
        base = Path(root).expanduser()
        if not base.is_absolute():
            base = base.resolve()
        # `path_component` on the slug and not on the topic any more: the folder
        # name is still built from text a caller typed, and this is the one
        # place that text becomes a directory.
        root = base / RUNS_DIR / path_component(slug, default="run")
        root = _free_folder(root)
        run = cls(root, brief)
        (run.root / "brief.md").write_text(brief.to_markdown(), encoding="utf-8")
        # Both forms, deliberately. The markdown is for a person; the JSON is
        # what every later stage reads back, and without it the report stage
        # cannot even name the question it answered.
        (run.root / "brief.json").write_text(
            json.dumps(brief.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        run.record_agent("newrun")
        run.finish()
        return run

    @classmethod
    def attach(cls, root: Path, *, brief: Brief | None = None) -> "Run":
        """Re-open a run folder another process started, with its state intact.

        The brief comes from `brief.json` -- the one `newrun` wrote -- and the
        counters, the stop reasons and the start time come from `run.json`.
        Everything a later command adds is added ON TOP of that.

        A `run.json` that will not parse no longer ends the run. It used to:
        `write_text` truncates before it writes, so an interrupt inside `finish`
        left half a file, and from then on EVERY command refused with exit 7 --
        no repair, no `--force`, no rebuild -- while `posts.jsonl`,
        `fetchlog.jsonl` and `notes/sources/` sat there complete. The write is
        atomic now so it cannot happen again, and a folder already in that state
        is repaired rather than condemned: the unreadable bytes are moved aside
        under their own name, the counters are rebuilt from the folder's own
        files, and the run carries a stop reason saying so.
        """
        root = Path(root)
        rebuilt_from: str | None = None
        try:
            existing = read_run_json(root)
        except RunFolderError as exc:
            existing = {}
            rebuilt_from = str(exc)
        if brief is None:
            if (root / "brief.json").exists():
                brief = Brief.from_file(root / "brief.json")
            elif isinstance(existing.get("brief"), dict):
                known = {f for f in Brief.__dataclass_fields__}
                brief = Brief(**{k: v for k, v in existing["brief"].items() if k in known})
            else:
                brief = Brief(question="(brief written elsewhere)")
        run = cls(root, brief)
        run.counters = {k: v for k, v in (existing.get("counters") or {}).items()}
        run.stop_reasons = list(existing.get("stop_reasons") or [])
        run.agents = list(existing.get("agents") or [])
        run.started = existing.get("started") or run.started
        run.finished = existing.get("finished")
        run._existing = existing
        if rebuilt_from is not None:
            kept = run._keep_damaged(root / "run.json")
            run.counters = run._counters_from_folder()
            run.stop(
                f"run.json could not be read ({rebuilt_from}); it was kept as "
                f"{kept.name if kept else 'run.json'} and the counters below were "
                "rebuilt by counting the run folder's own files"
            )
        run._baseline = dict(run.counters)
        if rebuilt_from is None:
            # The counters were taken verbatim from a `run.json` that parsed.
            # Parsing is not the same as being current -- see below.
            run._recover_stale_counters()
        return run

    def _recover_stale_counters(self) -> None:
        """Raise a stale `run.json` to what the folder's own files prove.

        `log_fetch` appends to `fetchlog.jsonl` the moment the act happens
        and only increments an in-memory counter; `run.json` is written by
        `finish()`. `tg.persist_active_run` covers every exception exit and
        Ctrl-C -- and nothing covers `TerminateProcess`, `taskkill /F`, a closed
        console or a power loss. What those leave behind is the nastiest state
        there is: a `run.json` that **parses perfectly** and is simply out of
        date. `attach` took it verbatim, and `_counters_from_folder` -- the
        function written to rebuild the spend -- was reachable only through a
        `run.json` that could NOT be parsed, which is the one case
        `atomic_write_text` exists to make impossible.

        Measured: a `verify` walking eight names, killed after 6 seconds ->
        `fetchlog.jsonl` 2 acts, `run.json` `{"requests": 0}`, parses fine. The
        next command then re-armed the run-level brake from zero; on a `deep`
        brief that is 800 fresh requests granted after every hard kill, against
        a host whose rate limit has never been measured.

        Only ever upward, and the baseline is left where it was, so `finish()`
        writes the difference as this process's own delta and a concurrent
        process cannot have its spend erased by our correction. Two processes
        that both attach into the same gap will both correct it and the run
        will over-count: that is the side that is safe -- over-count what
        left this machine, never under-count it.
        """
        evidence = self._counters_from_folder()
        missed = {k: v for k, v in evidence.items() if v > self.counters.get(k, 0)}
        if not missed:
            return
        moved = ", ".join(
            f"{key} {self.counters.get(key, 0)} -> {missed[key]}"
            for key in sorted(missed)
        )
        self.counters.update(missed)
        self.stop(
            f"run.json's spend was behind this folder's own files ({moved}); a "
            "command killed outright logs its network acts and never gets to "
            "write run.json, so the counters were raised to what fetchlog.jsonl, "
            "posts.jsonl and registry-delta.jsonl prove. Over-counting what left "
            "the machine is the safe side; under-counting re-arms the request "
            "ceiling from zero."
        )

    # -- paths -------------------------------------------------------------
    @property
    def sources_dir(self) -> Path:
        return self.root / "notes" / "sources"

    @property
    def posts_path(self) -> Path:
        return self.root / "posts.jsonl"

    @property
    def delta_path(self) -> Path:
        return self.root / "registry-delta.jsonl"

    @property
    def fetchlog_path(self) -> Path:
        return self.root / "fetchlog.jsonl"

    @property
    def queries_path(self) -> Path:
        return self.root / "queries.json"

    # -- writing -----------------------------------------------------------
    def count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def record_agent(self, name: str) -> None:
        """Which commands wrote into this folder. `accept` asks for it."""
        if name and name not in self.agents:
            self.agents.append(name)

    def log_fetch(self, resp) -> None:
        """One line per network act, credential-scrubbed on the way out.

        Scrubbing here rather than at the call site is deliberate: this is the
        one funnel every request passes through, and a rule enforced in one place
        is a rule that holds.

        `kind: "fetch"` is the distinction a citation rests on: a URL a search
        engine merely LISTED was never read and is not quotable. Everything
        logged here was read, so everything logged here says so.
        """
        from tgweb import response_record
        record = cfg_module.redact_obj(response_record(resp))
        record.setdefault("kind", "fetch")
        record["ts"] = now_local()
        cfg_module.guarded_append(
            self.fetchlog_path, [json.dumps(record, ensure_ascii=False)],
            label="fetch log",
        )
        self.count("requests")

    def write_posts(self, messages) -> int:
        """`posts.jsonl` -- every post with a working permalink and its query.

        **Deliberately NOT scrubbed**, and the boundary matters. Redaction
        applies to what this skill AUTHORS -- the fetch log, `run.json`, notes,
        the report -- because that is where our own credential could plausibly
        appear. `posts.jsonl` holds fetched content, and running a redactor over
        it would corrupt real corpus text: the api_hash pattern is any 32 hex
        characters, which is also a commit hash, a transaction id, or the middle
        of somebody's message. There is no code path by which our credential
        reaches a parsed message, and mangling evidence to guard against an
        impossible route is a bad trade.

        The same rule now holds on stdout: `tg.py emit()` protects the fetched
        fields, because for a while the file and the terminal disagreed about
        what a post said and the terminal is what an agent reads.

        `found_by` is the field that makes the next run cheaper: it records which
        phrasing surfaced which post, so the vocabulary that worked is an asset
        rather than something rediscovered every time.

        **This is the one place a post is de-duplicated, keyed `(username, id)`,
        first write wins.** Nothing else in the skill dedupes and nothing else
        may start: `read.search_channel` deduped within a single query only, so
        `--query bitcoin --query btc` banked the same post twice, a `search` and
        a `history` over one channel banked it again, and a repeated command
        banked the lot once more. Measured on one probe run: 40 lines,
        23 distinct posts. `report.md` then said "Posts: 40", and
        `querycraft.candidates()` -- whose whole guard is that a term must
        appear in `min_documents` SEPARATE posts -- counted one person's single
        message as three, which is the exact failure that floor exists to
        prevent.

        First write wins is also what fixes `found_by`'s meaning: it records the
        FIRST route that retrieved the post, and that is the documented meaning
        from now on.

        Returns `(written, suppressed)`; `written` is field 0, so an old caller
        that unpacks or indexes still gets the count it expects.
        """
        prepared: list[tuple[object, str]] = []
        for msg in messages:
            data = msg.as_dict() if hasattr(msg, "as_dict") else dict(msg)
            prepared.append((post_key(data),
                             json.dumps(data, ensure_ascii=False, sort_keys=True)))
        if not prepared:
            return WriteResult(0, 0)
        lines: list[str] = []
        suppressed = 0
        with cfg_module.file_guard(self.posts_path, label="posts"):
            seen = self._post_keys_on_disk()
            for key, line in prepared:
                if key is not None and key in seen:
                    suppressed += 1
                    continue
                if key is not None:
                    seen.add(key)
                lines.append(line)
            cfg_module.append_lines(self.posts_path, lines)
        self.count("posts", len(lines))
        if suppressed:
            self.count("posts_duplicate", suppressed)
        return WriteResult(len(lines), suppressed)

    def _post_keys_on_disk(self) -> set:
        """Every post key `posts.jsonl` already holds. Call under the guard.

        Read from the file rather than remembered in memory, because the
        duplicates that matter come from DIFFERENT processes: `search` in one,
        `history` in the next, an hour apart. A per-instance set would not have
        seen any of them.
        """
        keys: set = set()
        rows, _ = read_jsonl(self.posts_path)
        for row in rows:
            key = post_key(row)
            if key is not None:
                keys.add(key)
        return keys

    def write_delta(self, records) -> int:
        lines = []
        for rec in records:
            data = rec.as_dict() if hasattr(rec, "as_dict") else dict(rec)
            lines.append(json.dumps(data, ensure_ascii=False, sort_keys=True))
        written = cfg_module.guarded_append(self.delta_path, lines,
                                            label="registry delta")
        self.count("sources", written)
        return written

    def write_queries(self, query_log) -> None:
        """`queries.md` for a person, `queries.json` for the next command.

        The JSON goes through `QueryLog.save` rather than a dict dump of our own,
        because the next round has to load it back and a one-way serialisation is
        how a round ceiling gets enforced on nobody.

        Both writes are atomic and guarded now. They were the last two bare
        `write_text` calls in the run folder -- `run.json` got the guard, the
        atomic replace and `_keep_damaged`, and the round ledger got none of the
        three. `queries.json` is the ONLY record of how many rounds a run has
        used, so an interrupt during any `queries` command destroyed the round
        ceiling, the yield floor and the drift ban together, and the printed
        repair advice ("move it aside and re-run `queries start`") handed the run
        an unlimited number of fresh rounds.

        `QueryLog.save` still decides the FORMAT: it writes to a private staging
        name beside the target and this method replaces the real file with those
        bytes through `config.atomic_write_text`. Nothing here duplicates the
        serialisation, and nothing here forks the atomic write.
        """
        md_path = self.root / "queries.md"
        with cfg_module.file_guard(md_path, label="queries.md"):
            cfg_module.atomic_write_text(md_path, query_log.to_markdown())
        with cfg_module.file_guard(self.queries_path, label="queries.json"):
            cfg_module.atomic_write_text(
                self.queries_path, self._query_log_text(query_log)
            )

    def _query_log_text(self, query_log) -> str:
        """Exactly the bytes `QueryLog.save` would write, without it writing them.

        The staging file carries the pid and a random token for the same reason
        `atomic_write_text`'s does -- two `queries` commands in two processes
        must not meet on one temp name -- and is removed on every exit path.
        """
        staging = self.queries_path.with_name(
            f"{self.queries_path.name}.{os.getpid()}.{os.urandom(4).hex()}.staging"
        )
        try:
            query_log.save(staging)
            return staging.read_text(encoding="utf-8")
        finally:
            try:
                staging.unlink()
            except OSError:
                pass

    def load_queries(self, factory):
        """The run's `QueryLog`, or None when stage 3 never ran here.

        None and "an empty log" are different facts and the report says different
        things about them, so they are not collapsed into one.
        """
        if not self.queries_path.exists():
            return None
        return factory.load(self.queries_path)

    def write_note(self, agent: str, text: str) -> Path:
        """Add to `notes/<agent>.md`. A second note never destroys the first.

        This was `write_text`. A branch agent that notes as it goes -- once
        after discovery, once after the query-craft loop, once after the read --
        kept only the last one, and the acceptance gate still passed because it
        only counts non-empty notes. A run that paid for its notes must not lose
        one to a repeated command.

        The separator carries a local timestamp, so the file reads as a log
        rather than as one paragraph that grew. The first note is written plain:
        a single-note run should not have to explain a separator.
        """
        path = self.root / "notes" / f"{slugify(agent)}.md"
        body = cfg_module.redact(text or "").rstrip("\n")
        with cfg_module.file_guard(path, label="note"):
            existing = ""
            if path.exists():
                existing = path.read_text(encoding="utf-8", errors="replace").rstrip("\n")
            if existing:
                block = f"{existing}\n\n---\n\n<!-- {now_local()} -->\n\n{body}\n"
            else:
                block = f"{body}\n"
            cfg_module.atomic_write_text(path, block)
        self.record_agent(slugify(agent))
        return path

    def stop(self, reason: str) -> None:
        if reason and reason not in self.stop_reasons:
            self.stop_reasons.append(reason)

    def write_acceptance(self, data: dict) -> dict:
        """`acceptance.json` -- no acceptance artefact, no closed run.

        Merged into `run.json` as the gate record at the same moment, because a
        folder that says it passed in one file and says nothing in the other is
        a folder nobody can trust.
        """
        data = cfg_module.redact_obj(data)
        (self.root / "acceptance.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.finish({"gate": {
            "tool": data.get("tool"),
            "checked_at": data.get("checked_at"),
            "exit": 0 if data.get("formal") == "PASS" else 2,
            "errors": len(data.get("errors") or []),
            "warnings": len(data.get("warnings") or []),
        }})
        return data

    def finish(self, extra: dict | None = None) -> dict:
        """`run.json` -- what was spent, what stopped it, and what it produced.

        Merged, never replaced. What is already in the file survives unless this
        process has something newer to say about it, which is what lets four
        commands in four processes add up to one run.

        Three things this had to learn, all of them the same lesson:

        * **The merge happens against the file, not against a snapshot.** It was
          a read-at-attach, modify, write-at-finish, with no guard and no
          re-read -- the textbook lost update. Two `search` processes that both
          attached at `requests: 15` and both spent 10 wrote 25, and the second
          one to finish erased the first one's spend. `_apply_request_ceiling`
          then seeded the next command's ceiling with a number 10 too low, so
          the ceiling that exists to protect an unmeasured IP rate limit
          under-counted. `agents` and `stop_reasons` were replaced wholesale, so
          an agent name the folder gate demands simply vanished.
        * **Counters accumulate by DELTA.** What this process added since it
          attached is added to what is on disk now, so nothing depends on the
          two processes having seen the same starting number.
        * **The write is atomic.** `write_text` truncates first, so an interrupt
          between the truncate and the flush left half a file -- and every
          command that touches the run then refused, with no repair, no
          `--force` and no rebuild, while `posts.jsonl`, `fetchlog.jsonl` and
          `notes/sources/` sat there intact and complete.

        The whole structure is scrubbed on the way out, not only the caller's
        `extra`. Scrubbing one field and trusting the rest is how the leak this
        guards against happened: a credential pasted into the question text rode
        the brief straight into `run.json`, past a redaction that was only ever
        applied to `extra`. Anything leaving this class leaves redacted.
        """
        self.finished = now_local()
        path = self.root / "run.json"
        with cfg_module.file_guard(path, label="run.json"):
            try:
                current = read_run_json(self.root)
            except RunFolderError:
                # A `run.json` nothing can parse is what this method is here to
                # replace. Refusing to write would leave the run bricked; the
                # unreadable bytes are kept beside it so nothing is destroyed.
                current = {}
                self._keep_damaged(path)
            data = dict(current)
            data.update({
                "schema": RUN_SCHEMA,
                "run": self.root.name,
                "root": str(self.root),
                "depth": self.brief.depth,
                "agents": _union(current.get("agents"), self.agents),
                "started": current.get("started") or self.started,
                "finished": self.finished,
                "brief": self.brief.as_dict(),
                "counters": _add_delta(current.get("counters"),
                                       self._baseline, self.counters),
                "stop_reasons": _union(current.get("stop_reasons"), self.stop_reasons),
            })
            # Present but empty until `tg.py accept` runs. The checker reads a
            # missing key as a broken record and a null one as "not accepted
            # yet", and the second is the truth here.
            data.setdefault("gate", None)
            if extra:
                data.update(extra)
            data = cfg_module.redact_obj(data)
            cfg_module.atomic_write_text(
                path, json.dumps(data, ensure_ascii=False, indent=2)
            )
        self._existing = data
        # This process's contribution is now on disk, so it is part of the
        # baseline. Without this a command that calls `finish` twice -- `stop`
        # then `close_run`, which happens on every aborted run -- would count
        # its own spend a second time.
        self.counters = dict(data.get("counters") or {})
        self._baseline = dict(self.counters)
        self.agents = list(data.get("agents") or [])
        self.stop_reasons = list(data.get("stop_reasons") or [])
        return data

    def _keep_damaged(self, path: Path) -> Path | None:
        """Move an unreadable `run.json` aside instead of overwriting it.

        Never deleted. The bytes are the only record of what the interrupted
        write had got as far as saying, and a repair that destroys evidence is
        not a repair.
        """
        path = Path(path)
        if not path.exists():
            return None
        stamp = re.sub(r"[^0-9]", "", now_local())
        for suffix in ("", *(f"-{n}" for n in range(2, 20))):
            spoiled = path.with_name(f"{path.name}.damaged-{stamp}{suffix}")
            if spoiled.exists():
                continue
            try:
                path.replace(spoiled)
                return spoiled
            except OSError:
                return None
        return None

    def _counters_from_folder(self) -> dict[str, int]:
        """Rebuild the spend by counting what the folder actually holds.

        Not a guess: `fetchlog.jsonl` has one line per network act,
        `registry-delta.jsonl` one per source touched, and `posts.jsonl` one per
        post now that it is de-duplicated. These are the same three numbers the
        report states, so a rebuilt `run.json` agrees with the report rather
        than contradicting it.
        """
        fetches, _ = read_jsonl(self.fetchlog_path)
        posts, _ = read_jsonl(self.posts_path)
        sources, _ = read_jsonl(self.delta_path)
        counters = {"requests": len(fetches), "sources": len(sources)}
        distinct = {post_key(p) for p in posts if post_key(p) is not None}
        counters["posts"] = len(distinct) + sum(
            1 for p in posts if post_key(p) is None)
        return {k: v for k, v in counters.items() if v}


class RunFolderError(RuntimeError):
    """The run folder is not one. Always says which file and what is wrong."""


class NotARunFolder(RunFolderError):
    """This path is not a run folder, and nothing was written into it.

    Separate from `RunFolderError` because the answer is different: the caller
    named the wrong directory, so the command refuses at `EXIT_USAGE` BEFORE it
    creates anything. `RunFolderError` is about a folder that is one and is
    damaged.
    """


def require_run_folder(path, *, allow_empty: bool = False,
                       flag: str = "the run folder") -> Path:
    """A directory that SAYS it is a run folder, or a refusal that wrote nothing.

    From the 2026-08-25 repairs. The old check asked three questions --
    non-empty string, exists, is a directory -- and none of them is "is this a
    run?". `brief.json`, `run.json` and `posts.jsonl` are all optional to
    `Run.attach`, so `report`, `accept` and `queries` took ANY existing
    directory, `Run.__init__` mkdir'd `notes/sources` inside it, and `report`
    answered **exit 0 with `ok: true`**. Measured on a scratch directory holding
    one unrelated file:

        BEFORE: my-important-file.txt
        AFTER : notes/sources/  report.md  run.json

    Run folders are siblings under `telegram-runs/`, so dropping the leaf or
    tab-completing to the wrong sibling is the realistic typo, and
    `report telegram-runs` wrote a report into the parent directory itself.

    The test is the marker this code already writes: a `run.json` that parses
    and carries `schema: telegram-research.run/1`. `Run.open` writes it before `newrun`
    returns, so every real run folder has had one since its first second, and
    nothing else on this machine does.

    Two callers, two shapes, one rule. The positional commands (`report`,
    `accept`, `queries`, `note`) ask for a run that already exists and pass
    nothing. `--run` passes `allow_empty=True`, because it may legitimately FILL
    a directory that has nothing in it yet: an empty directory holds no evidence
    to destroy and cannot be a wrong sibling with somebody else's run in it. An
    existing NON-empty directory with no marker is refused for both.

    What `--run` still does NOT do is create a directory that is missing
    altogether. That was repaired in an earlier pass and pinned with a measured
    story -- `--run <sibling typo>` built a second, empty run folder beside the
    real one, wrote the pages and the fetch log into it and exited 0, and the
    run then reported on was the half with nothing in it. Re-opening that
    to accept a "create it if absent" branch would undo the repair, so a missing
    path stays a refusal.

    A `run.json` that will not parse is refused here too, deliberately: this
    function's whole job is to answer "is this a run folder" from the folder's
    own words, and unreadable bytes are not words. That is the one place this
    decision costs something, and the recovery is `tg.py note <run> ...`, which
    goes through `Run.attach` and repairs a damaged `run.json` by moving it
    aside and rebuilding the counters from the folder's own files.
    """
    if not str(path or "").strip():
        # An empty positional resolved to `Path("")` -> the current directory,
        # which exists and is a directory, so `queries "" start` wrote
        # `queries.json`, `queries.md`, `run.json` and a `notes/` tree into
        # whatever folder the caller happened to be standing in.
        raise NotARunFolder(
            "the run folder is empty. Pass the path `tg.py newrun` printed; "
            "an empty one used to mean the current directory"
        )
    root = Path(path)
    if not root.exists():
        raise NotARunFolder(
            f"{flag} {root} does not exist. Nothing was created: a run folder "
            "comes from `tg.py newrun`, and inventing one here would answer a "
            "typo by scattering the run over two folders — and the half you "
            "look at would be the empty one"
            if allow_empty else
            f"{root} does not exist. Nothing was created: a run folder comes "
            "from `tg.py newrun`, and inventing one here would answer a typo "
            "with a well-formed empty report"
        )
    if not root.is_dir():
        raise NotARunFolder(f"{flag} {root} is a file, not a run folder")
    if allow_empty and not any(root.iterdir()):
        # `--run` may FILL an empty directory: there is nothing in it to
        # destroy, nothing to mistake it for, and `finish()` stamps the marker
        # on the way out, so it is a run folder by the time the command ends.
        # The positional commands do not get this branch -- `report` into an
        # empty directory is the typo case, not a run.
        return root
    marker = root / "run.json"
    if not marker.exists():
        raise NotARunFolder(
            f"{flag} {root} holds no run.json, so it is not a run folder and "
            "nothing was written into it. A run folder is made by "
            "`tg.py newrun`; the "
            "likely typo is a sibling directory or the parent of the run you "
            f"meant. Contents: {_first_names(root)}"
        )
    try:
        data = read_run_json(root)
    except RunFolderError as exc:
        raise NotARunFolder(
            f"{marker} cannot be read, so this folder cannot say it is a run "
            f"and nothing was written into it: {exc}. If this IS the run, "
            "`tg.py note <run> --agent repair --text \"...\"` moves the damaged "
            "bytes aside under their own name and rebuilds the counters from "
            "the folder's own files."
        ) from exc
    if data.get("schema") != RUN_SCHEMA:
        raise NotARunFolder(
            f"{marker} does not declare `schema: {RUN_SCHEMA}` (it says "
            f"{data.get('schema')!r}), so this folder is not a run folder of "
            "this skill's and nothing was written into it"
        )
    return root


def _first_names(root: Path, limit: int = 6) -> str:
    """A few of the names in a directory, for a refusal that names what it saw."""
    try:
        names = sorted(p.name for p in Path(root).iterdir())
    except OSError:
        return "unreadable"
    if not names:
        return "empty"
    shown = ", ".join(names[:limit])
    return shown + (f", ... ({len(names)} entries)" if len(names) > limit else "")


def _union(stored, mine) -> list:
    """Everything on disk plus everything this process added, in first-seen order.

    `agents` and `stop_reasons` were assigned wholesale, so a concurrent command
    deleted the other one's entries -- including an agent name the folder gate
    requires.
    """
    out = list(stored or [])
    for item in mine or []:
        if item not in out:
            out.append(item)
    return out


def _add_delta(stored, baseline, current) -> dict[str, int]:
    """What is on disk now, plus what THIS process added since it attached.

    Writing the absolute total is what lost the other process's spend. The
    delta is this process's own contribution and cannot erase anybody else's.
    """
    out = {k: v for k, v in (stored or {}).items()}
    for key, value in (current or {}).items():
        delta = value - (baseline or {}).get(key, 0)
        if delta:
            out[key] = out.get(key, 0) + delta
        else:
            out.setdefault(key, value)
    return out


def read_run_json(root: Path) -> dict:
    """`run.json` as a dict, or {} when there is none.

    A corrupt one is an error with a sentence in it, never a traceback: the
    caller mistyped a path or a process died mid-write, and both deserve to be
    told which.
    """
    path = Path(root) / "run.json"
    if not path.exists():
        return {}
    try:
        # `read_bytes_shared` rather than `read_text`: on NTFS an ordinary open
        # blocks another process's `os.replace` over the same name, and
        # `finish` now replaces this file. A reader must not be able to make a
        # writer fail.
        raw = cfg_module.read_bytes_shared(path)
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw.decode("utf-8-sig", errors="replace"))
    except ValueError as exc:
        raise RunFolderError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RunFolderError(f"{path} must hold a JSON object, not {type(data).__name__}")
    return data


def read_jsonl(path: Path) -> tuple[list, list[int]]:
    """Every parseable record, and the line numbers of the ones that were not.

    One corrupt line must never cost the other ten thousand -- the registry has
    behaved this way since it was written and the run folder now does too.
    """
    rows: list = []
    corrupt: list[int] = []
    path = Path(path)
    if not path.exists():
        return rows, corrupt
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace")
                                  .splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            corrupt.append(lineno)
    return rows, corrupt


def _distinct_posts(posts) -> tuple[set, int]:
    """The set of post keys, and how many rows had no identity at all."""
    keys: set = set()
    unidentified = 0
    for post in posts:
        key = post_key(post)
        if key is None:
            unidentified += 1
        else:
            keys.add(key)
    return keys, unidentified


def _free_folder(root: Path, limit: int = 99) -> Path:
    """`root`, or the first `-N` beside it that is not already a run."""
    root = Path(root)
    if not root.exists() or not any(root.iterdir()):
        return root
    for n in range(2, limit + 1):
        candidate = root.with_name(f"{root.name}-{n}")
        if not candidate.exists() or not any(candidate.iterdir()):
            return candidate
    raise RunFolderError(
        f"{limit} run folders already exist beside {root}; name the question "
        "differently rather than adding a hundredth"
    )


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------
# The marker `report_skeleton` leaves where the answer goes, and the one string
# in the report that is NOT translated. `tg.py` looks for it to tell an
# untouched skeleton from a report somebody wrote, and that test has to keep
# working on a folder whose report was generated in the other language.
ANSWER_MARKER = "<!-- ANSWER-PLACEHOLDER"

# Every sentence the skeleton writes, in both languages. A dictionary and not an
# i18n library on purpose: the skill ships as a few files and must not need a
# package installed to write its own report. English is the default; the Russian
# column is there because Russian-language sources are much of what this skill
# reads, and a report is easier to check against them in their own language.
REPORT_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "dup_lines": "lines in `posts.jsonl` — {lines}, of them {duplicates} repeated",
        "count_mismatch": "`run.json` records {recorded} — a disagreement",
        "posts": "Posts: {n}.",
        "posts_with_caveats": "Posts: {n} ({caveats}).",
        "run_line": ("**Run.** {started} — {finished}. "
                     "Network requests: {requests}. {posts} "
                     "Sources touched: {sources}."),
        "account_used": ("**The account was used**: MTProto calls — "
                         "{calls}, resolves — 0."),
        "account_unused": "**The account was not used.**",
        "h_findings": "## What was found",
        "answer_placeholder": (ANSWER_MARKER + ": the answer to the question goes "
                               "here. Every claim carries a link to the post it "
                               "came from. -->"),
        "h_sources": "## Sources",
        "sources_head": "| source | type | members | found via | posts from here |",
        "sources_rule": "| --- | --- | --- | --- | --- |",
        "h_queries": "## Queries",
        "queries_link": "The full round-by-round list — [queries.md](queries.md).",
        "log_unreadable": (
            "**The round log is on disk but could not be read** — `{name}` is "
            "damaged.{reason} That does not mean the query-craft stage never "
            "ran: it means its result is unavailable right now. Everything else "
            "in this report — the posts, the sources, the spend — was read "
            "from intact files of the run. Nobody deleted the file: repair "
            "`{path}` by hand, or move it aside and re-run `tg.py queries <run> "
            "start`. **Moving the log aside resets the round count**: how many "
            "rounds the run has already spent is recorded in that file and "
            "nowhere else, so after a fresh start neither the round ceiling nor "
            "the new-post floor nor the drift ban bounds anything — repairing "
            "it by hand is the safer route."),
        "log_unreadable_reason": " Reason: {error}.",
        "log_absent": (
            "The round log was never kept: the query-craft stage never ran "
            "through `tg.py queries`, so neither the round ceiling nor the "
            "drift ban bounded anything in this run."),
        "terms_intro": "Words mined from the corpus itself, not invented:",
        "terms_head": "| word | round | posts | what it means |",
        "terms_rule": "| --- | --- | --- | --- |",
        "gloss_todo": "<!-- fill in -->",
        "no_terms": (
            "**Not one word could be mined from the corpus** — the jargon loop "
            "returned nothing, and that is recorded as a fact of the run rather "
            "than left out."),
        "h_discovery": "## Source discovery",
        "discovery_line": "Discovery channels that ran: {n} ({names}).",
        "discovery_caveat": (
            "**No third-party service is proof of absence.** A search of "
            "somebody else's index that returns nothing means \"not in that "
            "index\", not \"nobody in Telegram writes about this\"."),
        "h_limits": "## What limited this run",
        "h_raw": "## Raw material",
        "raw_line": (
            "The originals of every page read are in `notes/sources/`. Machine "
            "output — `posts.jsonl`, the run's sources — `registry-delta.jsonl`."),
    },
    "ru": {
        "dup_lines": "строк в `posts.jsonl` — {lines}, из них {duplicates} повторных",
        "count_mismatch": "в `run.json` записано {recorded} — расхождение",
        "posts": "Постов: {n}.",
        "posts_with_caveats": "Постов: {n} ({caveats}).",
        "run_line": ("**Прогон.** {started} — {finished}. "
                     "Запросов к сети: {requests}. {posts} "
                     "Источников затронуто: {sources}."),
        "account_used": ("**Аккаунт использовался**: вызовов MTProto — "
                         "{calls}, резолвов — 0."),
        "account_unused": "**Аккаунт не использовался.**",
        "h_findings": "## Что найдено",
        "answer_placeholder": (ANSWER_MARKER + ": здесь ответ на вопрос. Каждое "
                               "утверждение — со ссылкой на пост. -->"),
        "h_sources": "## Источники",
        "sources_head": "| источник | тип | участников | найден через | постов отсюда |",
        "sources_rule": "| --- | --- | --- | --- | --- |",
        "h_queries": "## Запросы",
        "queries_link": "Полный список по ходам — [queries.md](queries.md).",
        "log_unreadable": (
            "**Журнал ходов на диске есть, но прочитать его не удалось** — "
            "`{name}` повреждён.{reason} Это не значит, что стадия query craft "
            "не проходила: значит, что её результат сейчас недоступен. Всё "
            "остальное в отчёте — посты, источники, расход — прочитано из целых "
            "файлов прогона. Файл никто не удалял: почините `{path}` вручную "
            "или отодвиньте его в сторону и перезапустите `tg.py queries <run> "
            "start`. **Отодвинутый журнал обнуляет счёт ходов**: сколько ходов "
            "прогон уже потратил, записано только в этом файле, поэтому после "
            "«начать заново» ни потолок ходов, ни порог новых постов, ни запрет "
            "на дрейф никого не ограничивают — чинить руками надёжнее."),
        "log_unreadable_reason": " Причина: {error}.",
        "log_absent": (
            "Журнал ходов не вёлся: стадия query craft через `tg.py queries` "
            "не проходила, поэтому ни потолок ходов, ни запрет на дрейф в этом "
            "прогоне никого не ограничивали."),
        "terms_intro": "Слова, добытые из самого корпуса, а не придуманные:",
        "terms_head": "| слово | ход | постов | что значит |",
        "terms_rule": "| --- | --- | --- | --- |",
        "gloss_todo": "<!-- заполнить -->",
        "no_terms": (
            "**Ни одного слова из корпуса добыть не удалось** — цикл жаргона "
            "не дал результата, и это записано как факт прогона, а не опущено."),
        "h_discovery": "## Разведка источников",
        "discovery_line": "Каналов разведки отработало: {n} ({names}).",
        "discovery_caveat": (
            "**Ни один чужой сервис не является доказательством отсутствия.** "
            "Если поиск по стороннему индексу ничего не дал, это значит «в его "
            "индексе нет», а не «в Telegram про это не пишут»."),
        "h_limits": "## Чем прогон ограничен",
        "h_raw": "## Сырьё",
        "raw_line": (
            "Оригиналы всех прочитанных страниц — в `notes/sources/`. "
            "Машинный вывод — `posts.jsonl`, источники прогона — "
            "`registry-delta.jsonl`."),
    },
}

REPORT_LANGS = tuple(REPORT_STRINGS)
DEFAULT_REPORT_LANG = "en"


def report_strings(lang: str | None) -> dict[str, str]:
    """The string table for a language, falling back to English.

    An unknown language is not an error here. `tg.py` constrains the flag, and a
    report in the wrong language is a far smaller failure than a run that spent
    its whole request budget and then refused to write a report at all.
    """
    return REPORT_STRINGS.get((lang or "").lower(),
                              REPORT_STRINGS[DEFAULT_REPORT_LANG])


def report_skeleton(run: Run, *, discovery, query_log, sources_used, posts,
                    query_log_error: str | None = None,
                    lang: str = DEFAULT_REPORT_LANG) -> str:
    """A `report.md` skeleton for the agent to finish.

    Deliberately a skeleton and not a generated report. What a run found is a
    judgement -- which posts answer the question, what the jargon means, what the
    silence in a channel implies -- and a template that fabricated those
    sentences would be writing the one part nobody should automate. What IS
    filled in here is everything mechanical: the counts, the queries, the
    sources, the ceilings, the stop reasons. Those are facts the run holds and
    that a writer would otherwise retype and get wrong.

    `lang` picks the wording, English by default. Everything a tool keys on --
    the answer marker, the file names, the links -- is the same string in both,
    so a report written in either language is still readable by the commands
    that check it.

    Two rules this template broke and now keeps:

    * **It never asserts what it does not know.** The sentence saying no corpus
      vocabulary was mined used to fire in every report, including runs whose
      `queries.md` listed four mined terms. It now fires only when the log is on
      disk AND empty; with no log at all the report says the stage did not run.
    * **It never links a file that is not there.** `queries.md` was linked
      unconditionally by a template that nothing ever made write it.

    That rule is why there are THREE states for the query log and not two. "No
    log" and "a log that cannot be read" are different facts about a run, and
    collapsing them made `report` choose between two wrong answers: say the log
    was never kept about a run that kept one, or refuse to report at all on a
    run that had already spent its whole request budget. A corrupt
    `queries.json` is now stated as what it is, with everything else -- the
    posts, the sources, the spend -- reported normally, and the file named so a
    human can repair it.

    The caller may pass `query_log_error` to quote the real message. It is not
    required: a `queries.json` on disk with `query_log=None` can only mean the
    load failed, so the state is detected either way.
    """
    S = report_strings(lang)
    counters = run.counters
    # Distinct posts, not lines. `posts.jsonl` is de-duplicated on write now,
    # but a folder written before that fix still holds the duplicates, and the
    # number the reader sees has to be the number of posts either way: the probe
    # run said "Posts: 40" about 23 posts, 74 % too high, in the document a
    # person reads and in `acceptance.json`.
    distinct, unidentified = _distinct_posts(posts)
    posts_on_disk = len(distinct) + unidentified
    duplicates = len(posts) - posts_on_disk
    recorded = counters.get("posts")
    caveats = []
    if duplicates > 0:
        caveats.append(S["dup_lines"].format(lines=len(posts),
                                             duplicates=duplicates))
    if isinstance(recorded, int) and recorded != posts_on_disk:
        caveats.append(S["count_mismatch"].format(recorded=recorded))
    posts_line = S["posts"].format(n=posts_on_disk)
    if caveats:
        posts_line = S["posts_with_caveats"].format(
            n=posts_on_disk, caveats="; ".join(caveats))
    lines = [
        f"# Telegram: {run.brief.question}",
        "",
        S["run_line"].format(
            started=run.started, finished=run.finished or now_local(),
            requests=counters.get("requests", 0), posts=posts_line,
            sources=len(sources_used)),
        "",
        # Read from the COUNTER, not from the brief's intention. The brief field
        # says what the run was allowed to do; this line says what it did, and
        # since `search` began routing a group to `messages.search` the two can
        # differ -- a run that spent the account was printing "the account was
        # not used" over the posts that call had returned.
        (S["account_used"].format(calls=counters.get("account_calls", 0))
         if counters.get("account_calls") else S["account_unused"]),
        "",
        S["h_findings"],
        "",
        S["answer_placeholder"],
        "",
        S["h_sources"],
        "",
        S["sources_head"],
        S["sources_rule"],
    ]
    # Distinct here too. The per-source column said `40` beside a channel that
    # gave up 23 posts, which is the same wrong number in a second place.
    per_source: dict[str, int] = {}
    counted: set = set()
    for post in posts:
        name = getattr(post, "username", None) or (
            post.get("username", "?") if isinstance(post, dict) else "?")
        key = post_key(post)
        if key is not None:
            if key in counted:
                continue
            counted.add(key)
        per_source[name] = per_source.get(name, 0) + 1
    for src in sources_used:
        data = src.as_dict() if hasattr(src, "as_dict") else dict(src)
        name = data.get("username", "?")
        lines.append(
            f"| [{name}](https://t.me/{name}) | {data.get('type', '?')} | "
            f"{data.get('members', '?')} | {data.get('found_via', '?')} | "
            f"{per_source.get(name, 0)} |"
        )
    lines += ["", S["h_queries"], ""]
    if (run.root / "queries.md").exists():
        lines += [S["queries_link"], ""]
    if query_log is None and (query_log_error or run.queries_path.exists()):
        # The log IS on disk and could not be read. Saying it was never kept
        # would be a false statement about the run, and refusing to write the
        # report at all would throw away everything the run DID pay for.
        reason = (S["log_unreadable_reason"].format(error=query_log_error)
                  if query_log_error else "")
        lines += [
            S["log_unreadable"].format(name=run.queries_path.name,
                                       reason=reason, path=run.queries_path),
            "",
        ]
    elif query_log is None:
        lines += [S["log_absent"], ""]
    elif query_log.terms:
        lines += [S["terms_intro"], "", S["terms_head"], S["terms_rule"]]
        for term in query_log.terms.values():
            lines.append(
                f"| `{term.term}` | {term.round_found} | {term.documents} | "
                f"{term.gloss or S['gloss_todo']} |"
            )
        lines.append("")
    else:
        lines += [S["no_terms"], ""]

    if discovery:
        used = sorted(getattr(discovery, "channels_used", []))
        lines += [
            S["h_discovery"],
            "",
            S["discovery_line"].format(n=len(used), names=", ".join(used)),
            "",
            S["discovery_caveat"],
            "",
        ]

    if run.stop_reasons:
        lines += [S["h_limits"], ""]
        for reason in run.stop_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    lines += [S["h_raw"], "", S["raw_line"], ""]
    return "\n".join(lines)
