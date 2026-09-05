"""`tg.py` itself: the exit codes, the route guards, the cursors, the ceilings.

This file exists because a mutation pass over the 479-test suite on 2026-08-25
found twelve surviving mutants and **every one of them was in `tg.py` or in a
default `tg.py` exposes**. Both exit-6 route guards could be deleted whole and
the suite stayed green. `--since-last` could be nulled on both commands and the
suite stayed green. `verify --write` could stop writing and the suite stayed
green. Half the documented exit codes appeared in no test at all.

So the tests here are deliberately about the command line and nothing else: what
code came back, what JSON came out, what the registry holds afterwards. Every
one of them runs offline against the `site` fixture, which replaces
`urllib.request.build_opener` -- `tgweb`, `read`, `registry` and `run` are all
the real code and only the socket is fake.

Where a test pins a repair, its docstring carries the measured before-state.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = (Path(__file__).resolve().parent.parent
           / "skills" / "telegram-research" / "scripts")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config as config_module  # noqa: E402
import querycraft  # noqa: E402
import account as account_module  # noqa: E402
from fakes import FakeTransport  # noqa: E402
import registry as registry_module  # noqa: E402
import resolve as resolve_module  # noqa: E402
import tg  # noqa: E402
import tgweb  # noqa: E402

LANDING_CHANNEL = "https://t.me/durov"
LANDING_GROUP = "https://t.me/tdlibchat"
SEARCH_1 = "https://t.me/s/durov?q=bitcoin"
SEARCH_2 = "https://t.me/s/durov?q=bitcoin&before=62"
SEARCH_DEAD = "https://t.me/s/durov?q=deadquery"
WALK_1 = "https://t.me/s/durov"
WALK_2 = "https://t.me/s/durov?before=523"
GROUP_SEARCH = "https://t.me/s/tdlibchat?q=x"
EMBED = "https://t.me/tdlibchat/10000?embed=1"

# Every probe `cmd_selftest` opens. The 2026-08-25 list, plus the four
# the command already read: a copy of the corpus that is missing one of these
# is a `selftest` that cannot run, which is a fact a test should state.
# The sentence `report_skeleton` writes when stage 3 never ran at all. It
# must NOT appear for a log that is on disk and merely unreadable.
STAGE_DID_NOT_RUN = "The round log was never kept"

SELFTEST_PROBES = (
    "C01-landing-durov.html", "A18-landing-tdlibchat.html",
    "C02-landing-nonexistent.html", "A01-s-durov.html",
    "A09-s-Astana_motoriders.html", "C15-s-durov-q-rare.html",
    "C26-embed-birding-29320.html", "C08-embed-tdlibchat-50000.html",
    "C10-embed-tdlibchat-10000.html", "C16-embed-birding-1000.html",
)


def build_site(site, probe) -> None:
    """The pages every test here reads, and nothing else.

    An unmapped URL raises inside `FakeSite`, so a command that fetches
    something a test did not expect fails loudly instead of quietly.
    """
    site.add(LANDING_CHANNEL, probe("C01-landing-durov.html"))
    site.add(LANDING_GROUP, probe("A18-landing-tdlibchat.html"))
    site.add(SEARCH_1, probe("C15-s-durov-q-rare.html"))
    site.add(SEARCH_2, probe("C15-s-durov-q-rare.html"))
    site.add(GROUP_SEARCH, probe("C15-s-durov-q-rare.html"))
    site.add(WALK_1, probe("A01-s-durov.html"))
    site.add(WALK_2, probe("A01-s-durov.html"))
    site.add(EMBED, probe("C10-embed-tdlibchat-10000.html"))


def state_dir(tmp_path) -> Path:
    """Where the `cli` fixture points `TELEGRAM_RESEARCH_STATE`."""
    return tmp_path / "state"


def seed_registry(tmp_path, username: str, **fields) -> None:
    """Put a record in the scratch registry without a network call.

    Used where the test is about what the CLI *reads* from the registry
    (`--since-last`, the type guard), so the write half is not what is under
    test and should not shape the fixture.
    """
    root = state_dir(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    registry_module.Registry(root / "sources.jsonl").append(
        registry_module.Source(username=username, **fields)
    )


def get_source(cli, username: str):
    return cli("registry", "get", "--username", username).json["source"]


@pytest.fixture
def no_retries(monkeypatch):
    """A 5xx fails at once instead of sleeping 60s and then 120s.

    `TelegramWeb._retry` sleeps `RETRY_BACKOFF_BASE * 2**attempt`. That backoff
    is correct against a real host and unusable in a test, and the thing under
    test here is what `tg.py` does with the `FetchFailed` that comes out the far
    end, not the retry policy that produced it.
    """
    monkeypatch.setattr(tgweb.TelegramWeb, "_retry", lambda self, attempt: False)


# ==========================================================================
# The exit-code contract
# ==========================================================================
# `SKILL.md`: "Every subcommand prints JSON, on the way out and on the way
# down", over an exit table that has no 1 in it. Before this repair, 1 was the
# code for a damaged registry, an unreadable queries.json, a contended write
# guard and every AttributeError in the codebase -- each with an empty stdout,
# which a subagent cannot tell from "there was nothing to say".

EXIT_MAP = [
    (KeyboardInterrupt(), tg.EXIT_STOPPED, "KeyboardInterrupt"),
    (config_module.ConfigError("bad config"), tg.EXIT_OPERATOR, "ConfigError"),
    (config_module.GuardBusy("guard held"), tg.EXIT_ACCOUNT_BUSY, "GuardBusy"),
    (resolve_module.AccountBusy("account held"), tg.EXIT_ACCOUNT_BUSY, "AccountBusy"),
    (tgweb.RunAborted("429"), tg.EXIT_STOPPED, "RunAborted"),
    (tgweb.FetchFailed("HTTP 502"), tg.EXIT_FETCH_FAILED, "FetchFailed"),
    (tgweb.TelegramWebError("transport"), tg.EXIT_FETCH_FAILED, "TelegramWebError"),
    (tg.read_module.WrongRoute("group read as a channel"), tg.EXIT_WRONG_ROUTE, "WrongRoute"),
    (tg.UsageError("you typed it wrong"), tg.EXIT_OPERATOR, "UsageError"),
    # Both of these used to be swallowed by the operator clause and
    # reported as a mistyped path -- `NotARunFolder` as a `RunFolderError`,
    # `NothingAsked` as a `ValueError`. The order of the `except` clauses in
    # `dispatch` IS the mapping, and these two rows are what keeps them ahead of
    # the clause that would take them back.
    (tg.NotARunFolder("that is not a run folder"), tg.EXIT_USAGE, "NotARunFolder"),
    (tg.read_module.NothingAsked("nothing was asked"), tg.EXIT_USAGE, "NothingAsked"),
    (registry_module.RegistryDamaged("unreadable line"), tg.EXIT_INTERNAL, "RegistryDamaged"),
    (resolve_module.LedgerUnreadable("ledger"), tg.EXIT_INTERNAL, "LedgerUnreadable"),
    (config_module.AtomicWriteFailed("replace"), tg.EXIT_INTERNAL, "AtomicWriteFailed"),
    # The account family. Until 2026-08-26 this table had no `account_module`
    # row at all, so every one of these fell through to `except Exception` and
    # was answered with exit 9 and `internal: true` -- "this is a bug in tg.py,
    # not something you typed" -- about a stale cached access_hash. Measured live
    # on 2026-08-25. The exit code is how a caller tells a freeze from a wrong
    # surface, so one row each.
    (account_module.FloodWait(36468, "messages.search"), tg.EXIT_STOPPED, "FloodWait"),
    (account_module.WrongSurface("a channel is free"), tg.EXIT_WRONG_ROUTE, "WrongSurface"),
    (account_module.PeerUnusable("stale access_hash"), tg.EXIT_FETCH_FAILED, "PeerUnusable"),
    (account_module.PeerNotFound("@nobody"), tg.EXIT_FETCH_FAILED, "PeerNotFound"),
    (account_module.TransportError("connection reset"), tg.EXIT_FETCH_FAILED, "TransportError"),
    (account_module.TelethonMissing("not installed"), tg.EXIT_OPERATOR, "TelethonMissing"),
    (account_module.LiveModeRefused("switch is off"), tg.EXIT_OPERATOR, "LiveModeRefused"),
    (account_module.EvidenceRequired("no landing card"), tg.EXIT_OPERATOR, "EvidenceRequired"),
    (account_module.PaidCallRefused("stars"), tg.EXIT_OPERATOR, "PaidCallRefused"),
    (AttributeError("'NoneType' object has no attribute 'id'"), tg.EXIT_INTERNAL, "AttributeError"),
    (KeyError("missing"), tg.EXIT_INTERNAL, "KeyError"),
    (TypeError("bad operand"), tg.EXIT_INTERNAL, "TypeError"),
]


@pytest.mark.parametrize("exc,code,name", EXIT_MAP, ids=[e[2] for e in EXIT_MAP])
def test_every_exception_that_reaches_main_is_json_and_a_documented_code(
    cli, monkeypatch, exc, code, name
):
    """One row per exception the CLI can raise. None of them may be exit 1.

    Before: `GuardBusy`, `RegistryDamaged`, `AtomicWriteFailed`,
    `QueryLogError` and the whole `AttributeError`/`KeyError`/`TypeError`
    family reached the interpreter, which printed a traceback and exited 1 with
    zero bytes on stdout. `KeyboardInterrupt` exited 130, also silent.
    """
    def boom(args, cfg):
        raise exc

    monkeypatch.setattr(tg, "cmd_budget", boom)
    result = cli("budget")
    assert result.exit_code == code, result.stdout
    assert result.exit_code != 1
    assert result.json is not None, "stdout carried no JSON at all"
    assert result.json["ok"] is False
    assert result.json["error_type"] == name
    assert "Traceback" not in result.stdout


def test_a_damaged_registry_answers_compact_with_json_and_never_exit_1(cli, tmp_path):
    """The one input the command exists for was the one input that crashed.

    `registry stats` reports `corrupt_lines: [2]`; the obvious next act is
    `registry compact`; before this repair that act was `EXIT=1`, stdout empty,
    `registry.RegistryDamaged` on stderr -- and the error text told the operator
    to "pass `force=True`", a Python keyword argument, from a command line that
    had no such flag.
    """
    root = state_dir(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "sources.jsonl").write_text(
        '{"username":"durov","type":"channel","status":"alive"}\n'
        '{"username": "halfwrit\n'
        '{"username":"tdlibchat","type":"group","status":"alive"}\n',
        encoding="utf-8",
    )
    stats = cli("registry", "stats")
    assert stats.json["corrupt_lines"] == [2]

    refused = cli("registry", "compact")
    assert refused.exit_code == tg.EXIT_INTERNAL
    assert refused.json["ok"] is False
    assert refused.json["error_type"] == "RegistryDamaged"
    assert (root / "sources.jsonl").read_text(encoding="utf-8").count("\n") == 3

    forced = cli("registry", "compact", "--force")
    assert forced.exit_code == tg.EXIT_OK, forced.stdout
    assert forced.json["forced"] is True
    assert (root / "sources.jsonl.bak").exists(), "the corrupt bytes were not kept"
    assert "halfwrit" in (root / "sources.jsonl.bak").read_text(encoding="utf-8")


def test_the_traceback_goes_to_stderr_and_stdout_stays_parseable(tmp_path):
    """A real process, because the promise is about the two streams.

    Run out of process so stdout and stderr are genuinely separate: a caller
    parsing stdout must never meet a traceback there, and must never be handed
    an empty string instead of an answer.
    """
    root = tmp_path / "state"
    root.mkdir()
    (root / "sources.jsonl").write_text(
        '{"username":"durov","type":"channel"}\n{"username": "halfwrit\n',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "tg.py"), "registry", "compact"],
        capture_output=True, text=True, encoding="utf-8",
        env={"TELEGRAM_RESEARCH_STATE": str(root), "SYSTEMROOT": "C:\\Windows",
             "PATH": "", "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == tg.EXIT_INTERNAL, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["error_type"] == "RegistryDamaged"
    assert "Traceback" not in proc.stdout


def test_an_unreadable_queries_json_is_a_sentence_and_the_run_keeps_its_files(
    cli, tmp_path
):
    """A `queries` command killed mid-write made the whole run unreportable.

    `QueryLog.save` is a plain `write_text`, so a half-written `queries.json` is
    a reachable state, and `QueryLogError` was in no `except` clause: `report`,
    `queries record`, `queries accept` and `queries show` all answered it with
    exit 1 and an empty stdout. There was no repair path on the CLI and a
    subagent was told nothing.
    """
    root = Path(cli("--root", tmp_path, "newrun", "--question", "вопрос",
                    "--topic", "t", "--depth", "quick").json["run"])
    assert cli("queries", root, "start", "--query", "аренда").exit_code == 0
    broken = root / "queries.json"
    text = broken.read_text(encoding="utf-8")
    broken.write_text(text[: len(text) // 2], encoding="utf-8")

    # The three that WRITE into the log refuse: proceeding on a file they
    # cannot read is how a round ceiling gets enforced on nobody.
    for argv in (("queries", root, "show"), ("queries", root, "record"),
                 ("queries", root, "accept", "--term", "x")):
        result = cli(*argv)
        assert result.exit_code == tg.EXIT_OPERATOR, (argv, result.stdout)
        assert result.json is not None and result.json["ok"] is False
        assert "queries.json" in result.json["error"]
    assert broken.exists(), "the CLI deleted the operator's file to unblock itself"


def test_report_survives_a_queries_json_it_cannot_read(cli, site, probe, tmp_path):
    """A run that spent its whole request budget was unreportable over one
    corrupt sidecar: exit 1, no `report.md`, and no flag to skip the log.

    `report` only describes the query log, so it must survive one it cannot
    read -- but it may not answer with the "stage did not run" sentence either,
    which is a false statement about a run that DID keep a log. The third
    state in `report_skeleton` says the true thing; this asserts the CLI
    reaches it.
    """
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "vopros",
                    "--topic", "t", "--depth", "quick").json["run"])
    cli("--run", root, "search", "durov", "--query", "bitcoin")
    assert cli("queries", root, "start", "--query", "bitcoin").exit_code == 0
    broken = root / "queries.json"
    text = broken.read_text(encoding="utf-8")
    broken.write_text(text[: len(text) // 2], encoding="utf-8")

    result = cli("report", root)
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["query_log_error"], "the report said nothing about the damage"
    written = (root / "report.md").read_text(encoding="utf-8")
    assert STAGE_DID_NOT_RUN not in written, "a lie about a run that kept a log"
    assert "queries.json" in written
    assert result.json["posts"] == 7, "the intact half of the run was not reported"
    assert broken.exists()


def test_selftest_does_not_answer_a_parser_mismatch_with_the_crash_code(
    cli, tmp_path, probe
):
    """`1` was both "the parsers no longer match" and "the program blew up".

    `selftest` is the command `SKILL.md` says to run first and the one that
    separates "Telegram changed" from "we broke it"; a caller could not tell
    its verdict from a crash. Now 0 or 9, and 9 says the second thing on
    purpose.
    """
    probes = tmp_path / "probes"
    probes.mkdir()
    for name in SELFTEST_PROBES:
        (probes / name).write_text(probe(name), encoding="utf-8")
    assert cli("selftest", "--probes", probes).exit_code == tg.EXIT_OK

    landing = probes / "C01-landing-durov.html"
    body = landing.read_text(encoding="utf-8")
    assert "11 110 268 subscribers" in body       # the string the parser reads
    landing.write_text(body.replace("11 110 268 subscribers",
                                    "11 110 269 subscribers"), encoding="utf-8")
    broken = cli("selftest", "--probes", probes)
    assert broken.exit_code == tg.EXIT_INTERNAL
    assert broken.exit_code != 1
    assert broken.json["failed"] == ["durov.members"]


def test_selftest_covers_the_markup_a_front_end_change_would_move(cli, probe, tmp_path):
    """Seven files and twelve assertions, none of them about service messages,
    media, replies, peers, dates, text or cursors.

    So a rename of `service_message`, `message_media_not_supported_wrap`,
    `js-message_text`, `tgme_widget_message_reply`, `data-peer` or the cursor
    markup passed `selftest` green while the parse degraded -- and `selftest` is
    the command that is supposed to separate "Telegram changed" from "we broke
    it".
    """
    result = cli("selftest")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    names = {c["check"] for c in result.json["checks"]}
    for wanted in ("durov.blocks_seen", "durov.blocks_unparsed",
                   "durov.texts_non_empty", "durov.every_message_dated",
                   "durov.page_before", "q_bitcoin.no_cursor",
                   "service_messages_counted", "unsupported_video_recorded",
                   "reply_quote_kept", "reply_quote_out_of_text",
                   "group_chat_peer", "ghost_post.detected",
                   "ghost_post.not_a_message"):
        assert wanted in names, wanted
    assert len(result.json["checks"]) >= 24


def test_selftest_finds_a_relocated_probe_corpus(cli, probe, tmp_path, monkeypatch):
    """A copy of the skill whose probe corpus sits elsewhere could not self-test.

    `TELEGRAM_RESEARCH_PROBES` is the override the test suite already reads;
    `selftest` did not, so the first command `SKILL.md` tells you to run failed
    on exactly the layout that override exists to support -- a probe corpus
    kept outside the skill folder.
    """
    probes = tmp_path / "elsewhere"
    probes.mkdir()
    for name in SELFTEST_PROBES:
        (probes / name).write_text(probe(name), encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_RESEARCH_PROBES", str(probes))
    monkeypatch.setattr(tg, "default_probes", tg.default_probes)   # no caching
    result = cli("selftest")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert Path(result.json["probes"]) == probes


def test_the_installed_skill_carries_exactly_the_probes_selftest_reads():
    """The suite reads the corpus in the REPOSITORY; `selftest` reads the one
    inside the skill folder. Only the second is installed, and nothing else in
    this file would notice the two parting company.

    Both directions are asserted because both are real. An eleventh probe added
    to `cmd_selftest` leaves this suite green -- it has the full corpus at hand --
    while the installed skill's `selftest` dies on a missing file, which is the
    first command `SKILL.md` tells a new user to run. And a probe put back into
    the skill folder "just in case" is 2 MB of test material copied to every
    person who installs the skill, which is the thing this layout exists to stop.
    """
    shipped = SCRIPTS.parent / "tests" / "fixtures" / "probes"
    assert shipped.is_dir(), shipped
    on_disk = {p.name for p in shipped.iterdir() if p.is_file()}
    assert on_disk == set(SELFTEST_PROBES), {
        "missing from the skill folder": sorted(set(SELFTEST_PROBES) - on_disk),
        "in the skill folder and read by nothing": sorted(on_disk - set(SELFTEST_PROBES)),
    }
    # And they are the same bytes as the repository's copy, not a stale fork of
    # it: a probe re-saved on one side only makes `selftest` and the suite
    # disagree about what Telegram serves.
    corpus = Path(__file__).resolve().parent / "fixtures" / "probes"
    for name in SELFTEST_PROBES:
        assert (shipped / name).read_bytes() == (corpus / name).read_bytes(), name


# ==========================================================================
# A ceiling that is a ceiling
# ==========================================================================
@pytest.mark.parametrize("value", ["0", "-5", "-1"])
def test_a_max_requests_that_is_not_a_ceiling_is_a_usage_error(cli, site, value):
    """`--max-requests -5` removed the run-level brake ALTOGETHER.

    Measured: `-5` is truthy, so `build_web` applied the wrapper, and the
    wrapper returned immediately on `ceiling <= 0` -- `web.fetch` was never
    wrapped and ten fetches ran with nothing able to stop them. `0` was dropped
    as falsy, so an operator who typed 0 meaning "spend nothing" was given the
    brief's 133/400/800. Neither said a word in the JSON.

    And the refusal itself is JSON now. The check was an argparse `type=`,
    which exits 2 with **zero bytes on stdout** -- against the one promise this
    module makes about every subcommand. A subagent reading an empty line
    cannot tell a refused flag from a command that found nothing, and this is
    the flag most likely to be typed wrong. `row_limit` says so in its own
    docstring and refuses through `UsageError` for exactly this reason.
    """
    result = cli("--max-requests", value, "search", "durov", "--query", "bitcoin")
    assert result.exit_code == tg.EXIT_OPERATOR, result.stdout
    assert result.json is not None, repr(result.stdout)
    assert result.json["ok"] is False
    assert result.json["error_type"] == "UsageError"
    assert "--max-requests" in result.json["error"]
    assert not site.requested, "a request was spent before the flag was checked"


def flaky(site, url: str, body: str, *, fails: int) -> None:
    """Serve `fails` 502s for `url`, then the real page -- one `fetch()`, several
    network acts. `FakeSite` answers from a fixed dict, so the sequence is
    installed by shadowing `_entry`."""
    site.add(url, body)
    original = site._entry
    left = {"n": fails}

    def entry(u: str):
        if u == url and left["n"] > 0:
            left["n"] -= 1
            return (502, b"bad gateway", {"content-type": "text/html"})
        return original(u)

    site._entry = entry


def test_the_ceiling_counts_network_acts_and_not_fetch_calls(
    cli, site, probe, monkeypatch
):
    """A declared ceiling of 800 permitted up to 2400 requests to t.me.

    One `fetch()` is one to three acts: a 5xx or a dropped connection is retried
    up to `MAX_RETRIES` times and every attempt leaves the machine. The wrapper
    kept its own tally and added 1 per CALL, so the number in the flag was not
    the number of requests. `web.request_count` counts acts now, so the
    wrapper charges against it -- and the ceiling is the only thing standing
    between a deep run and an IP block on a host whose rate limit has never been
    measured.

    Here page 1 costs two acts (one 502, one retry that succeeds) and page 2
    costs one. Under the old wrapper page 1 charged 1, page 2 was allowed, and
    three acts ran against a ceiling of 2.
    """
    monkeypatch.setattr(tgweb.TelegramWeb, "_retry",
                        lambda self, attempt: attempt + 1 < 3)   # retry, do not sleep
    build_site(site, probe)
    flaky(site, WALK_1, probe("A01-s-durov.html"), fails=1)

    result = cli("--max-requests", 2, "history", "durov", "--max-pages", 5)
    assert result.exit_code == tg.EXIT_STOPPED, result.stdout
    assert "request ceiling of 2" in result.json["stopped"]
    assert len(site.requested) == 2, (
        f"the ceiling of 2 let {len(site.requested)} requests reach the wire"
    )


def test_a_positive_max_requests_still_bounds_the_run(cli, site, probe):
    build_site(site, probe)
    for mid in range(9990, 10001):
        site.add(f"https://t.me/tdlibchat/{mid}?embed=1",
                 probe("C10-embed-tdlibchat-10000.html"))
    result = cli("--max-requests", 3, "group", "tdlibchat",
                 *[a for mid in range(9990, 10001) for a in ("--id", mid)])
    assert result.exit_code in (tg.EXIT_OK, tg.EXIT_STOPPED)
    assert len(site.requested) <= 3


# ==========================================================================
# `group --write` never asserts a type it did not establish
# ==========================================================================
def test_group_writes_nothing_to_the_registry_and_locks_no_name_out(
    cli, site, probe
):
    """Reproduced 2026-08-25 against the real @telegram channel.

    `group telegram --start-id 100 --count 1 --write` inserted
    `type: "group", members: null, found_via: null`, and from that moment
    `search telegram` and `history telegram` answered exit 6 -- in
    the shared source registry, which every future run reads. One mistyped
    command poisoned the channel's whole free surface permanently, because
    `registry._merge` is newest-wins and nothing ever re-typed it.

    The repair then was to write `None` for a type this command never
    established. The repair now is stronger and smaller: `group` writes to the
    registry at all, so there is nothing for it to poison. `verify` is the one
    command that types a name, and it reads the landing card to do it.
    """
    build_site(site, probe)
    read = cli("group", "tdlibchat", "--id", 10000)
    assert read.exit_code == tg.EXIT_OK, read.stdout
    assert read.json["found"] == 1

    assert get_source(cli, "tdlibchat") is None, \
        "group wrote a registry line, and a type it never established with it"
    # And the two cheap surfaces are still reachable for that name.
    assert cli("search", "tdlibchat", "--query", "x").exit_code == tg.EXIT_OK
    assert cli("history", "tdlibchat", "--max-pages", 1).exit_code != tg.EXIT_WRONG_ROUTE


# ==========================================================================
# A cursor may only advance when the walk reached the end it claims
# ==========================================================================
def test_history_write_withholds_the_cursor_after_a_truncated_walk(cli, site, probe):
    """900 posts made unreachable by one `--write` on a bounded walk.

    Measured: a 1000-post channel read with the default `--max-pages 25` stored
    the newest id it saw; the next `--since-last` run answered
    `found: 0, reached_until_id: true`, which `SKILL.md` glosses as "caught up
    with stored work" -- about 900 posts no run had ever fetched.
    `registry._MERGE_MAX` guarantees a cursor can only go up, so there is no way
    back.
    """
    build_site(site, probe)
    truncated = cli("history", "durov", "--max-pages", 1, "--write")
    assert truncated.exit_code == tg.EXIT_OK, truncated.stdout
    assert truncated.json["max_id_seen"] == 543          # it SAW the id
    assert truncated.json["cursor_written"] is False     # and did not store it
    assert truncated.json["cursor_withheld_reason"] == "page_ceiling"
    assert "--max-pages" in truncated.json["cursor_withheld"]
    assert get_source(cli, "durov") is None, "a truncated walk wrote a cursor"


def test_history_write_stores_the_cursor_when_the_walk_reaches_an_end(cli, site, probe):
    """The other half: a walk that really ended still produces the cursor
    `--since-last` needs. Without this the registry knew nothing after a full
    channel walk and the second run re-read the whole history."""
    build_site(site, probe)
    walked = cli("history", "durov", "--max-pages", 5, "--write")
    assert walked.exit_code == tg.EXIT_OK, walked.stdout
    assert walked.json["exhausted"] is True
    assert walked.json["cursor_written"] is True
    assert walked.json["cursor_withheld"] is None
    assert get_source(cli, "durov")["max_id_seen"] == walked.json["max_id_seen"]


# ==========================================================================
# The run survives its own failures
# ==========================================================================
def test_a_failed_fetch_keeps_the_posts_it_already_paid_for(
    cli, site, probe, tmp_path, no_retries
):
    """`read.py` attaches its harvest to EVERY exception; the CLI banked one.

    Under a 429 four pages of posts reached `posts.jsonl` and the command exited
    3 saying `posts_banked: N`. Under a 502 one page later the identical posts
    were dropped, `posts.jsonl` stayed empty, and the run held originals in
    `notes/sources/` with no posts parsed out of them. The requests were paid
    either way.
    """
    build_site(site, probe)
    site.add(WALK_2, "bad gateway", status=502)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    result = cli("--run", root, "history", "durov", "--max-pages", 5)
    assert result.exit_code == tg.EXIT_FETCH_FAILED, result.stdout
    assert result.json["error_type"] == "FetchFailed"
    assert result.json["posts_banked"] == 20
    lines = (root / "posts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 20, "the posts read before the 502 were thrown away"


def test_a_failed_command_still_writes_the_run_s_spend(
    cli, site, probe, tmp_path, no_retries
):
    """The next command re-armed the whole ceiling from zero.

    `log_fetch` appends to `fetchlog.jsonl` at once but only increments an
    in-memory counter; `run.json` is written by `finish()`, which ran on the
    success path and on `RunAborted` and nowhere else. Measured: exit 5, three
    lines in `fetchlog.jsonl`, `run.json` counters `{}`, and the Russian report
    stating the run's network spend as 0.
    """
    build_site(site, probe)
    site.add(WALK_2, "bad gateway", status=502)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    assert cli("--run", root, "history", "durov", "--max-pages", 5).exit_code == 5

    logged = len((root / "fetchlog.jsonl").read_text(encoding="utf-8").strip().splitlines())
    counters = json.loads((root / "run.json").read_text(encoding="utf-8"))["counters"]
    assert logged >= 1
    assert counters.get("requests") == logged, (counters, logged)


def test_search_banks_the_posts_of_earlier_queries_when_a_later_one_fails(
    cli, site, probe, tmp_path, no_retries
):
    """`--query a --query b --query c` printed a's and b's posts and wrote none.

    `cmd_search` accumulated into `out` and wrote `posts.jsonl` only after the
    whole loop, so every early return jumped over the single write: the run
    folder held saved pages with no posts parsed out of them, and the fetch
    log's lines matched nothing.
    """
    build_site(site, probe)
    site.add(SEARCH_DEAD, "bad gateway", status=502)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    result = cli("--run", root, "search", "durov",
                 "--query", "bitcoin", "--query", "deadquery")
    assert result.exit_code == tg.EXIT_FETCH_FAILED, result.stdout
    lines = (root / "posts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 7, "the first query's posts were lost with the second's failure"
    assert result.json["results"][0]["query"] == "bitcoin"


def test_a_contended_registry_guard_costs_json_and_a_code_but_not_the_spend(
    cli, site, probe, tmp_path, monkeypatch
):
    """This one costs money, and it was the only lock the CLI can contend for.

    In `cmd_verify` the landing GETs happen first and the registry write happens
    after, so a `GuardBusy` -- raised when `sources.jsonl.write` is not free
    within 20 s, which is exactly what "four commands in four processes" produce
    -- spent one request per name and then died with a traceback and exit 1. The
    `verified` block was never emitted, so the results of the paid fetches were
    lost rather than merely unrecorded, and the spend never reached `run.json`
    either. Exit 4 is the documented "another process holds the lock", and until
    now nothing could reach it.
    """
    build_site(site, probe)

    def busy(*a, **kw):
        raise config_module.GuardBusy("the registry guard did not free in 20 s")

    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    monkeypatch.setattr(tg.discover_module, "admit", busy)
    result = cli("--run", root, "verify", "durov", "--write")
    assert result.exit_code == tg.EXIT_ACCOUNT_BUSY, result.stdout
    assert result.json["error_type"] == "GuardBusy"

    logged = len((root / "fetchlog.jsonl").read_text(encoding="utf-8").strip().splitlines())
    counters = json.loads((root / "run.json").read_text(encoding="utf-8"))["counters"]
    assert logged == 1, "the landing GET was not logged"
    assert counters.get("requests") == 1, (
        "the run lost the request it paid for; the next command re-arms the "
        "ceiling from zero"
    )


def test_a_run_folder_that_does_not_exist_is_refused_instead_of_invented(
    cli, site, probe, tmp_path
):
    """`--run <sibling typo>` built a second, empty run folder and exited 0.

    Only a missing PARENT was refused; the leaf was taken on trust. The run
    then read was whichever half was opened, and one of them had nothing
    in it.
    """
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    typo = root.parent / (root.name + "-TYPO")
    result = cli("--run", typo, "verify", "durov")
    # D5-on-`--run`: one code for every "that is not a run folder", and a
    # missing path is still refused rather than created.
    assert result.exit_code == tg.EXIT_USAGE
    assert result.json["error_type"] == "NotARunFolder"
    # And it says which of the refusals this is. "is a file, not a run folder"
    # about a path that is not there sends the operator looking for a file.
    assert "does not exist" in result.json["error"], result.json["error"]
    assert not typo.exists(), "the typo was answered by creating the folder"
    assert not site.requested, "a request was spent on a run folder that is not one"


# ==========================================================================
# The route guard, end to end: verify --write -> registry -> exit 6
# ==========================================================================
def test_verify_write_types_a_group_and_the_read_routes_then_refuse_it(cli, site, probe):
    """The documented chain had zero coverage at either end.

    `SKILL.md` §2: "`--write` is what puts the line in the registry; without it
    nothing is recorded and the type guard in stage 4 has nothing to read." Both
    the write and both guards could be deleted with the suite staying green,
    and the chain is the only thing standing between an agent and reading a
    group through the channel surface -- which returns a landing card the
    parsers read as "no messages", i.e. as evidence of absence.
    """
    build_site(site, probe)
    verified = cli("verify", "tdlibchat", "--write")
    assert verified.exit_code == tg.EXIT_OK, verified.stdout
    assert verified.json["verified"][0]["type"] == "group"
    assert verified.json["admission"]["inserted"] == 1

    stored = get_source(cli, "tdlibchat")
    assert stored["type"] == "group" and stored["members"] == 16674

    spent = len(site.requested)
    # `search` no longer refuses a group -- it ROUTES it, to messages.search,
    # which is the whole repair. With the live switch off (the `cli` fixture
    # clears it) the account path refuses before it reads the credential, and
    # the sentence names the variable rather than the surface.
    searched = cli("search", "tdlibchat", "--query", "x")
    assert searched.exit_code == tg.EXIT_OPERATOR, searched.stdout
    assert searched.json["ok"] is False
    assert "TELEGRAM_RESEARCH_ALLOW_LIVE" in searched.json["error"]
    # `history` routes a group too, since 2026-08-26 -- to `messages.getHistory`,
    # which had been fully accounted and called by nothing. With the live switch
    # off it refuses in the same words `search` does.
    walked = cli("history", "tdlibchat", "--max-pages", 1)
    assert walked.exit_code == tg.EXIT_OPERATOR, walked.stdout
    assert "TELEGRAM_RESEARCH_ALLOW_LIVE" in walked.json["error"]
    assert len(site.requested) == spent, "a refused route still spent a request"


def test_group_refuses_a_name_the_registry_types_as_a_channel(cli, site, probe):
    build_site(site, probe)
    assert cli("verify", "durov", "--write").exit_code == tg.EXIT_OK
    spent = len(site.requested)
    result = cli("group", "durov", "--id", 10000)
    assert result.exit_code == tg.EXIT_WRONG_ROUTE
    assert len(site.requested) == spent


# ==========================================================================
# `--since-last` on both commands
# ==========================================================================
def test_history_since_last_reads_the_stored_cursor(cli, site, probe, tmp_path):
    """Two lines with no test: nulling them made the flag a silent no-op and
    `history --since-last` re-walked the full channel."""
    build_site(site, probe)
    seed_registry(tmp_path, "durov", type="channel", max_id_seen=530, status="alive")
    result = cli("history", "durov", "--since-last", "--max-pages", 1)
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["until_id"] == 530
    ids = [m["id"] for m in result.json["messages"]]
    assert ids and min(ids) == 531, "the stored cursor did not bound the walk"
    assert max(ids) == 543


# ==========================================================================
# The documented numbers
# ==========================================================================
def test_the_page_ceilings_are_the_documented_ones(cli, site, probe):
    """`--max-pages` 5 on search, 25 on history, capped at 25 by config.

    Four numbers in `SKILL.md`'s cost table; two of them had no test, and a
    default that drifts is a cost table that lies.
    """
    build_site(site, probe)
    # The declared defaults, read off the parser: `history`'s default and the
    # config cap are both 25, so a defaults-only assertion through the emitted
    # `page_ceiling` cannot tell them apart.
    parser = tg.build_parser()
    assert parser.parse_args(["search", "durov", "--query", "x"]).max_pages == 5
    assert parser.parse_args(["history", "durov"]).max_pages == 25
    # And the cap that overrides them.
    assert cli("search", "durov", "--query", "bitcoin").json["page_ceiling"] == 5
    assert cli("search", "durov", "--query", "bitcoin",
               "--max-pages", 100).json["page_ceiling"] == 25
    assert cli("history", "durov").json["page_ceiling"] == 25
    assert cli("history", "durov", "--max-pages", 100).json["page_ceiling"] == 25


def test_found_by_records_the_query_on_search_and_nothing_on_a_walk(cli, site, probe):
    """Provenance: the `?q=` route knows which phrasing surfaced a post and the
    walk does not. A route that faked it would make the vocabulary worthless."""
    build_site(site, probe)
    searched = cli("search", "durov", "--query", "bitcoin")
    assert {m["found_by"] for m in searched.json["results"][0]["messages"]} == {"bitcoin"}
    walked = cli("history", "durov", "--max-pages", 1)
    assert {m["found_by"] for m in walked.json["messages"]} == {None}


# ==========================================================================
# discover, note, budget
# ==========================================================================
def test_discover_says_what_it_threw_away(cli, tmp_path):
    """`NOT_A_SOURCE` was the one filter that ran silently.

    The module states its contract twice -- "nothing is discarded silently" --
    and `cmd_discover` was the only caller that never passed the `dropped` list.
    `telegram` is on that list and is a real channel with millions of
    subscribers, so for any question about Telegram itself the obvious source
    was removed without a word.
    """
    result = cli("discover", "--text", "смотри @telegram и @birding_chats")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    names = [c["username"].lower() for c in result.json["candidates"]]
    assert "birding_chats" in names
    assert "telegram" not in names
    assert result.json["dropped"], "a candidate was dropped with no record of it"
    assert any("telegram" in reason for reason in result.json["dropped"])


def test_discover_checks_its_outputs_before_it_spends_the_request(cli, site, tmp_path):
    """`check_username` refuses "before spending a request on it"; these did
    not. A `--snippets-to` whose folder does not exist, or a mistyped
    `--from-file`, raised AFTER the lyzem GET and before `emit`, so the request
    was spent and its candidates thrown away."""
    # The URL comes from the code that builds it, never from a copy of it here.
    # This line used to spell `per_page=50` by hand; the real parameter is
    # `per-page`, and lyzem ignored the hand-spelled one and served its default
    # page -- 4 peers instead of 33 on one measured query, 10 instead of 50 on
    # another. The test stayed green through two rounds of changes because it
    # asserts the GET never happens, so the stub was never fetched and never
    # contradicted: a fixture agreeing with the bug.
    site.add(tg.discover_module.lyzem_url("x"), "<html></html>")
    bad_dir = cli("discover", "--lyzem-query", "x",
                  "--snippets-to", tmp_path / "nope" / "s.txt")
    assert bad_dir.exit_code == tg.EXIT_OPERATOR
    bad_file = cli("discover", "--lyzem-query", "x",
                   "--from-file", tmp_path / "missing.txt")
    assert bad_file.exit_code == tg.EXIT_OPERATOR
    assert not site.requested, "the paid GET happened before the flags were checked"


def test_budget_does_not_report_an_unreadable_ledger_as_healthy(cli, tmp_path):
    """`ok` was hard-coded True on the command `SKILL.md` calls "always safe to
    ask" and positions as the safety check before touching the account. The
    fail-closed verdict was two levels down, where an agent branching on the
    top-level flag never looked."""
    healthy = cli("budget")
    assert healthy.exit_code == tg.EXIT_OK and healthy.json["ok"] is True

    (state_dir(tmp_path) / "resolve-ledger.json").write_text("{broken", encoding="utf-8")
    broken = cli("budget")
    assert broken.json["ledger"]["readable"] is False
    assert broken.json["ok"] is False
    assert broken.exit_code == tg.EXIT_OPERATOR


def test_the_cli_and_the_registry_agree_on_what_a_username_is(cli, site, probe):
    """Two rules, `{2,31}` here and `{4,32}` in the registry, in both directions.

    So `verify abc --write` passed the CLI gate, spent a real GET on a name the
    registry was always going to refuse, and reported the refusal afterwards.
    One imported rule now, and the refusal happens before the request.
    """
    build_site(site, probe)
    result = cli("verify", "abc", "--write")
    assert result.exit_code == tg.EXIT_OPERATOR, result.stdout
    assert not site.requested, "a GET was spent on a name the registry refuses"
    assert tg.USERNAME_RE is registry_module.USERNAME_RE


def test_a_registry_that_cannot_vouch_for_its_cursor_says_so(cli, site, probe, tmp_path):
    """A truncated registry line loses `username` before it loses `max_id_seen`.

    The writer sorts keys, so the mark survives with nothing to attribute it to:
    the registry refuses to apply it and flags every cursor as suspect. Without
    that flag on the CLI, `--since-last` silently used the previous record --
    measured at a rewind from 91234 to 120, which is 91114 messages re-fetched on
    the surface that costs one GET each.
    """
    build_site(site, probe)
    seed_registry(tmp_path, "durov", type="channel", max_id_seen=530, status="alive")
    with (state_dir(tmp_path) / "sources.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"first_seen": "2026-08-25", "max_id_seen": 91234, "userna')

    result = cli("history", "durov", "--since-last", "--max-pages", 1)
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["cursor_may_be_stale"], (
        "the registry flagged the cursor as suspect and the CLI said nothing"
    )
    assert "registry stats" in result.json["cursor_may_be_stale"]


def test_budget_prints_the_config_overrides_that_were_refused(cli, tmp_path, monkeypatch):
    """An override clamped in the safe direction was recorded in
    `Config.override_notes`, "so it appears in `tg.py config`" -- a subcommand
    that does not exist. Nothing on the CLI printed them, so a caller running
    with a silently clamped ceiling had no way to find out."""
    override = tmp_path / "cfg.json"
    override.write_text(
        json.dumps({"budgets": {"daily_resolve_ceiling": 99999}}), encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(override))
    result = cli("budget")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["config_notes"], "a clamped override reached nobody"
    assert any("daily_resolve_ceiling" in n for n in result.json["config_notes"])


def test_a_note_piped_in_is_decoded_as_utf8_not_as_the_console_codepage(
    cli, tmp_path, monkeypatch
):
    """Silent corruption at exit 0, in the run's only prose artefact.

    `--from-file` is read with an explicit `encoding="utf-8"`; stdin was decoded
    with the console locale, which is cp1251 on a Russian Windows console -- and
    `PYTHONIOENCODING` is NOT a persisted variable here, so PowerShell, `cmd`
    and any agent environment hit it. Measured 2026-08-25 through a plain pipe:
    `аренда квартиры` in, `Р°СЂРµРЅРґР° РєРІР°СЂС‚РёСЂС‹` in
    `notes/moji.md`, `{"ok": true, "bytes": 61}` on stdout.
    """
    class Cp1251Stdin:
        """stdin as Python builds it on a cp1251 console: real UTF-8 bytes
        underneath, a text layer that would mis-decode them."""

        encoding = "cp1251"

        def __init__(self, data: bytes):
            self.buffer = _Bytes(data)

        def read(self) -> str:                 # what the old code called
            return self.buffer.data.decode("cp1251")

    class _Bytes:
        def __init__(self, data: bytes):
            self.data = data

        def read(self) -> bytes:
            return self.data

    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    monkeypatch.setattr(tg, "_CONSOLE_ENCODING", None)
    monkeypatch.setattr(sys, "stdin", Cp1251Stdin("аренда квартиры\n".encode("utf-8")))
    result = cli("note", root, "--agent", "moji")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    written = (root / "notes" / "moji.md").read_text(encoding="utf-8")
    assert "аренда квартиры" in written
    assert "Р°СЂ" not in written

    # And a note that really IS cp1251 -- typed into a cp1251 console and
    # redirected -- still arrives intact. Refusing it would be its own bug.
    monkeypatch.setattr(tg, "_CONSOLE_ENCODING", None)
    monkeypatch.setattr(sys, "stdin", Cp1251Stdin("аренда офиса\n".encode("cp1251")))
    assert cli("note", root, "--agent", "ansi").exit_code == tg.EXIT_OK
    assert "аренда офиса" in (root / "notes" / "ansi.md").read_text(encoding="utf-8")


def test_a_second_note_from_the_same_agent_does_not_destroy_the_first(cli, tmp_path):
    """A branch agent notes as it goes -- one after discovery, one after the
    query-craft loop, one after the read -- and only the last survived, while
    the acceptance gate still passed because it only counted non-empty notes.

    `Run.write_note` appends. This test pins the CLI behaviour the caller
    sees, not how the separator is spelled.
    """
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    cli("note", root, "--agent", "telegram", "--text", "ПЕРВОЕ наблюдение")
    cli("note", root, "--agent", "telegram", "--text", "ВТОРОЕ наблюдение")
    written = (root / "notes" / "telegram.md").read_text(encoding="utf-8")
    assert "ПЕРВОЕ наблюдение" in written
    assert "ВТОРОЕ наблюдение" in written


def test_accept_fails_the_folder_on_one_empty_note(
    cli, site, probe, tmp_path
):
    """One good note and one 0-byte stub passed `accept` and exited 0, which is
    the verdict `SKILL.md` promises is worth something. EACH empty note is an
    error now -- "a researcher that
    died early leaves a stub, and its sub-question is not closed" -- while
    `SKILL.md` claims `accept` applies the same demands."""
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    cli("--run", root, "search", "durov", "--query", "bitcoin")
    cli("note", root, "--agent", "telegram", "--text", "Настоящая заметка. " * 10)
    (root / "notes" / "dead-researcher.md").write_text("", encoding="utf-8")
    cli("report", root)
    result = cli("accept", root)
    assert result.exit_code == tg.EXIT_NOT_ACCEPTED, result.stdout
    assert any("dead-researcher.md" in err for err in result.json["errors"])


def test_accepting_a_term_twice_does_not_call_it_drift(cli, tmp_path):
    """`candidates()` skips anything already in the vocabulary, so the second
    `accept` of the same word was answered with the drift ban -- "appears in no
    post this run retrieved" -- about a word that is in the corpus AND in the
    vocabulary. It also meant a gloss could not be corrected without editing
    `queries.json` by hand."""
    root = Path(cli("--root", tmp_path, "newrun", "--question", "аренда",
                    "--topic", "t").json["run"])
    rows = [{"username": "x", "url": f"https://t.me/x/{i}",
             "text": "снимаю студию за рахмету, контракт на год"} for i in range(4)]
    (root / "posts.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    cli("queries", root, "start", "--query", "аренда")
    cli("queries", root, "record")
    first = cli("queries", root, "accept", "--term", "рахмету", "--gloss", "валюта")
    assert first.exit_code == tg.EXIT_OK, first.stdout

    again = cli("queries", root, "accept", "--term", "рахмету", "--gloss", "местное слово для взятки")
    assert again.exit_code == tg.EXIT_OK, again.stdout
    assert again.json["already_accepted"] is True
    log = querycraft.QueryLog.load(root / "queries.json")
    assert log.terms["рахмету"].gloss == "местное слово для взятки"


def test_a_run_folder_argument_that_is_empty_does_not_mean_the_current_directory(
    cli, tmp_path, monkeypatch
):
    """`queries "" start` wrote `queries.json`, `queries.md`, `run.json` and a
    `notes/` tree into whatever folder the caller was standing in: `Path("")` is
    the current directory, which exists and is a directory."""
    monkeypatch.chdir(tmp_path)
    result = cli("queries", "", "start", "--query", "аренда")
    # A directory that does not say it is a run folder is a usage refusal.
    assert result.exit_code == tg.EXIT_USAGE
    assert result.json["error_type"] == "NotARunFolder"
    assert not (tmp_path / "queries.json").exists()
    assert not (tmp_path / "run.json").exists()


# ==========================================================================
# Catch-up is preparation, and preparation is not the read
# ==========================================================================


def test_posts_already_banked_are_reported_as_suppressed_not_as_new(
    cli, site, probe, tmp_path
):
    """A `search` and a `history` over the same channel retrieve the same
    message, and `posts.jsonl` was append-only: 40 lines over 23 distinct
    `(username, id)` pairs, and `report.md` said 40 -- 74 % high in the document
    a person reads. Dedup happens once, in `write_posts`, which hands back the count so
    the caller can say so instead of the two files disagreeing in silence."""
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    first = cli("--run", root, "search", "durov", "--query", "bitcoin")
    assert first.json["posts_suppressed_as_duplicates"] == 0
    again = cli("--run", root, "search", "durov", "--query", "bitcoin")
    assert again.json["posts_suppressed_as_duplicates"] == 7, again.stdout
    lines = (root / "posts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 7, "the same posts were banked twice"


# ==========================================================================
# What `verify --write` vouches for, and what it merely met
# ==========================================================================
def test_the_run_delta_lists_only_the_sources_this_run_actually_admitted(
    cli, site, probe, tmp_path
):
    """`registry-delta.jsonl` is described to the reader as "sources this run
    added or refreshed". It was built from every record whose name merely
    APPEARED in the batch, so a candidate the admission rules REFUSED -- but
    that some earlier run had already admitted -- was written into the delta as
    though this run had vouched for it. The delta is what `report.md` builds its
    source table from, so the run reported sources it had rejected.

    `admit` names what it really wrote (`AdmissionReport.admitted`), so the
    CLI no longer has to infer it.
    """
    build_site(site, probe)
    # durov is already known from some earlier run, and is refused here by an
    # absurd member floor. tdlibchat is admitted normally.
    seed_registry(tmp_path, "durov", type="channel", members=11110268,
                  status="alive", found_via="manual")
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    result = cli("--run", root, "verify", "durov", "tdlibchat", "--write",
                 "--min-channel-members", 99999999)
    assert result.exit_code == tg.EXIT_OK, result.stdout
    admission = result.json["admission"]
    assert admission["rejected"] == 1, admission
    assert [n.lower() for n in admission["admitted"]] == ["tdlibchat"], admission

    delta = [json.loads(line) for line in
             (root / "registry-delta.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    names = sorted(rec["username"].lower() for rec in delta)
    assert names == ["tdlibchat"], (
        f"the delta claims this run vouched for {names}, and durov was refused"
    )


def test_verify_prints_the_warning_when_a_fresh_card_contradicts_the_registry(
    cli, site, probe, tmp_path
):
    """An admission used to carry a count and nothing else.

    A source whose fresh landing card disagrees with the stored type is exactly
    the case where `updated: 1` and no more is the wrong answer: the stored type
    decides which read route every future run takes. `judge` returns the
    sentence and `AdmissionReport` aggregates it; this asserts the CLI
    puts it on stdout, which is the only place a subagent can see it.

    The second half of this has changed since. `_merge` still refuses to move a stored
    type on the say-so of a record that did not establish it -- `history
    --write` and `group --write` assert a type from the command's own name and
    are still refused -- but `verify` is the one command whose entire job is to
    read the landing card, and the merge guard's own message says the way back
    is to "re-verify with type_confirmed". Nothing had ever passed that flag, so
    the way back did not exist and a record typed wrong once was wrong for ever,
    in a registry every run sharing the state directory reads. A card read off a page
    fetched in THIS call now corrects it, and says so in `type_corrections`.
    """
    build_site(site, probe)
    # The registry says group; the live landing card for tdlibchat says group
    # too, so contradict it deliberately by storing the opposite.
    seed_registry(tmp_path, "tdlibchat", type="channel", members=16674,
                  status="alive", found_via="manual", max_id_seen=91234)
    result = cli("verify", "tdlibchat", "--write")
    assert result.exit_code == tg.EXIT_OK, result.stdout

    warnings = result.json["admission"]["warnings"]
    assert warnings, "the contradiction was admitted with a count and no sentence"
    assert any("tdlibchat" in w for w in warnings), warnings

    # The fresh landing read corrects the stored type, and the correction is
    # evidenced -- named, with the value it replaced, in the command's own JSON.
    corrections = result.json["type_corrections"]
    assert [c["username"] for c in corrections] == ["tdlibchat"], corrections
    assert corrections[0]["was"] == "channel" and corrections[0]["now"] == "group"
    stored = get_source(cli, "tdlibchat")
    assert stored["type"] == "group", stored
    assert "type_conflict" not in stored, "a corrected record still reads as disputed"
    assert stored["max_id_seen"] == 91234, "the conflict cost the run its cursor"


# ==========================================================================
# Regression guards, one test per finding
# ==========================================================================
# Every measurement quoted below was taken before the fix, from the command
# line, with `TELEGRAM_RESEARCH_STATE` pointed at a scratch directory. Where the
# test harness could have flattered the result, the probe was run with
# `PYTHONIOENCODING` unset, which is how a plain Windows console really is.


def a_run(cli, tmp_path, question="жаргон аренды", **extra):
    """A real run folder, made the way `newrun` makes one."""
    argv = ["--root", tmp_path, "newrun", "--question", question,
            "--topic", "qprobe"]
    for key, value in extra.items():
        argv += [f"--{key.replace('_', '-')}", value]
    return Path(cli(*argv).json["run"])


def test_report_into_a_directory_that_is_not_a_run_writes_nothing(cli, tmp_path):
    """Measured before the repair, on a directory holding one file:

        BEFORE: my-important-file.txt
        AFTER : my-important-file.txt  notes/sources/  report.md  run.json
        exit=0, ok=true

    Run folders are siblings under `telegram-runs/`, so dropping the leaf or
    tab-completing to the wrong one is the realistic typo -- `report
    telegram-runs` wrote a report into the parent directory itself and answered
    `ok: true`.
    """
    stranger = tmp_path / "notarun"
    stranger.mkdir()
    (stranger / "my-important-file.txt").write_text("hello", encoding="utf-8")

    result = cli("report", stranger, "--question", "what do people say")
    assert result.exit_code == tg.EXIT_USAGE, result.stdout
    assert result.json["ok"] is False
    assert result.json["error_type"] == "NotARunFolder"
    assert result.json["wrote_anything"] is False
    assert [p.name for p in stranger.iterdir()] == ["my-important-file.txt"]

    # `accept`, `queries` and `note` too: all of them refuse, and none creates a
    # `notes/` tree on the way to the refusal.
    assert cli("accept", stranger).exit_code == tg.EXIT_USAGE
    assert cli("queries", stranger, "start", "--query", "x").exit_code == tg.EXIT_USAGE
    assert cli("note", stranger, "--text", "hi").exit_code == tg.EXIT_USAGE
    assert [p.name for p in stranger.iterdir()] == ["my-important-file.txt"]

    # And a real run folder is still accepted, by the marker it has carried
    # since its first second.
    root = a_run(cli, tmp_path)
    assert cli("report", root).exit_code == tg.EXIT_OK


def test_report_does_not_replace_a_finished_report_without_force(cli, tmp_path):
    """Measured: `report` twice ->

        answer still there: False
        placeholder back  : True
        exit=0

    `report.md` is the one file in the run folder a human writes by hand, and
    it was the one file written with an unconditional `write_text`.
    """
    root = a_run(cli, tmp_path)
    assert cli("report", root).exit_code == tg.EXIT_OK
    written = (root / "report.md").read_text(encoding="utf-8")
    answer = "A LIVE AGENT ANSWER: people here call it «рахмету»."
    (root / "report.md").write_text(
        written.replace(tg.REPORT_PLACEHOLDER, answer), encoding="utf-8")

    refused = cli("report", root)
    assert refused.exit_code == tg.EXIT_WOULD_DESTROY, refused.stdout
    assert refused.exit_code != 1, "1 is the interpreter's code for a crash"
    assert refused.json["ok"] is False
    assert refused.json["error_type"] == "WouldDestroy"
    assert "report.md" in refused.json["error"]
    assert "--force" in refused.json["error"]
    assert answer in (root / "report.md").read_text(encoding="utf-8")

    forced = cli("report", root, "--force")
    assert forced.exit_code == tg.EXIT_OK, forced.stdout
    assert forced.json["overwrote_existing"] is True
    assert answer not in (root / "report.md").read_text(encoding="utf-8")


def test_the_report_is_english_by_default_and_russian_on_request(cli, tmp_path):
    """The skill ships in English; the Russian wording is a flag, not a fork.

    Both languages come out of one dictionary in `run.py`, so the two reports
    are the same document in two wordings -- and the ONE string that is not
    translated is the answer marker, because `would_destroy_report` looks for
    it to tell an untouched skeleton from a report somebody wrote. A Russian
    report that this program could no longer recognise would be overwritten
    without a warning, which is the defect this refusal exists to prevent.
    """
    english = a_run(cli, tmp_path)
    result = cli("report", english)
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["report_lang"] == "en"
    text = (english / "report.md").read_text(encoding="utf-8")
    assert "## What was found" in text
    assert "## Sources" in text
    assert "The account was not used" in text
    assert "Что найдено" not in text

    russian = a_run(cli, tmp_path)
    ru_result = cli("report", russian, "--report-lang", "ru")
    assert ru_result.exit_code == tg.EXIT_OK, ru_result.stdout
    assert ru_result.json["report_lang"] == "ru"
    ru_text = (russian / "report.md").read_text(encoding="utf-8")
    assert "## Что найдено" in ru_text
    assert "## Источники" in ru_text
    assert "Аккаунт не использовался" in ru_text
    assert "What was found" not in ru_text

    # The marker is the same string in both, so the guard sees both.
    assert tg.REPORT_PLACEHOLDER in text
    assert tg.REPORT_PLACEHOLDER in ru_text
    assert tg.would_destroy_report(russian, force=False) is not None

    # An unknown language is refused by the parser, before anything is written.
    with pytest.raises(SystemExit):
        cli("report", russian, "--report-lang", "de")


def test_the_exit_table_has_a_row_for_ten_and_none_for_one(cli):
    """A new code is only a code if the table it lives in says so: the
    module docstring is what a subagent is pointed at, and `--help` is where an
    operator looks for the way past a refusal."""
    assert tg.EXIT_WOULD_DESTROY == 10
    assert "\n   10  refused" in tg.__doc__
    assert "1 is not in that table" in tg.__doc__
    codes = {name: value for name, value in vars(tg).items()
             if name.startswith("EXIT_")}
    assert 1 not in codes.values(), codes


def test_queries_record_refuses_a_posts_path_that_is_not_there(cli, tmp_path):
    """Measured on a run holding 8 posts, with a mistyped `--posts`:

        ok: true, new_posts: 0, may_continue: false,
        why_not: "round 1 brought 0 new posts, below the floor of 3"
        run.json stop_reasons: ["round 1 brought 0 new posts, below the floor of 3"]

    about a round that brought 8 -- and `_union` only ever adds, so the false
    sentence could not be retracted by re-running `record` correctly.
    """
    root = a_run(cli, tmp_path)
    posts = [{"username": "durov", "id": i, "url": f"https://t.me/durov/{i}",
              "text": f"аренда квартиры рахмету {i}", "found_by": "аренда"}
             for i in range(1, 9)]
    (root / "posts.jsonl").write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in posts) + "\n",
        encoding="utf-8")
    assert cli("queries", root, "start", "--query", "аренда").exit_code == 0

    typo = cli("queries", root, "record", "--posts", tmp_path / "nope-typo.jsonl")
    assert typo.exit_code == tg.EXIT_OPERATOR, typo.stdout
    assert typo.json["ok"] is False
    assert "does not exist" in typo.json["error"]

    stop_reasons = json.loads(
        (root / "run.json").read_text(encoding="utf-8"))["stop_reasons"]
    assert not any("0 new posts" in reason for reason in stop_reasons), stop_reasons
    log = querycraft.QueryLog.load(root / "queries.json")
    assert log.rounds[-1].new_posts == 0, "the round was scored against a typo"
    assert log.rounds[-1].stopped_because is None

    # And the correct call still works, on the same run, afterwards.
    good = cli("queries", root, "record")
    assert good.exit_code == tg.EXIT_OK, good.stdout
    assert good.json["new_posts"] == 8
    assert good.json["may_continue"] is True


def test_a_query_that_asks_nothing_is_a_usage_refusal_that_keeps_the_rest(
    cli, site, probe, monkeypatch
):
    """`tgweb.preview` builds `?q=` only `if query:`, so `search durov
    --query ""` fetched the channel's front page and reported its twenty newest
    posts as twenty hits for that query, `found_nothing: false`, exit 0 -- and
    inside a run banked all twenty with `found_by: ""` and cleared the yield
    floor with them.

    The refusal itself lives in `read.py` as `NothingAsked`; this is the CLI
    half, the mapping. The queries already answered are kept: an empty string in the
    middle of a list must not cost the ones before it.
    """
    build_site(site, probe)
    real = tg.read_module.search_channel

    def refuse_empty(web, username, query, **kwargs):
        if not (query or "").strip():
            raise tg.read_module.NothingAsked(
                f"an empty query against {username} asks nothing: /s/ without "
                "?q= is the channel's front page, not a search result")
        return real(web, username, query, **kwargs)

    monkeypatch.setattr(tg.read_module, "search_channel", refuse_empty)
    result = cli("search", "durov", "--query", "bitcoin", "--query", "  ")
    assert result.exit_code == tg.EXIT_USAGE, result.stdout
    assert result.json["ok"] is False
    assert result.json["error_type"] == "NothingAsked"
    assert result.json["query"] == "  "
    assert [r["query"] for r in result.json["results"]] == ["bitcoin"]
    assert result.json["results"][0]["found"] == 7


def test_registry_list_refuses_a_limit_that_would_hide_rows(cli, tmp_path):
    """Measured with four sources in the registry:

        --limit 50: count= 4 returned= ['aaaa1','bbbb2','cccc3','dddd4']
        --limit -1: count= 4 returned= ['aaaa1','bbbb2','cccc3']
        --limit -3: count= 4 returned= ['aaaa1']
        --limit  0: count= 4 returned= []

    `count` kept reporting the true total, so the output looked complete.
    """
    for name in ("aaaa1", "bbbb2", "cccc3", "dddd4"):
        seed_registry(tmp_path, name, type="group", status="alive")
    whole = cli("registry", "list", "--limit", 50)
    assert whole.json["count"] == 4 and whole.json["shown"] == 4
    assert whole.json["truncated"] is False

    for bad in (-1, -3, 0):
        result = cli("registry", "list", "--limit", bad)
        assert result.exit_code == tg.EXIT_OPERATOR, (bad, result.stdout)
        assert "--limit" in result.json["error"]

    # A real truncation says so instead of looking complete.
    cut = cli("registry", "list", "--limit", 2)
    assert cut.json["count"] == 4 and cut.json["shown"] == 2
    assert cut.json["truncated"] is True
    # `--top` is the same slice in `queries record`, and gets the same refusal.
    root = a_run(cli, tmp_path)
    assert cli("queries", root, "start", "--query", "аренда").exit_code == 0
    assert cli("queries", root, "record", "--top", -2).exit_code == tg.EXIT_OPERATOR


def test_budget_unfreeze_lifts_the_freeze_and_records_the_reason(cli, tmp_path,
                                                                 monkeypatch):
    """A freeze could only be lifted by editing the ledger JSON by hand --
    on the one file whose corruption stops the account half of the skill dead,
    while another process may be holding its guard. Before this flag,
    `tg.py budget --unfreeze` was `unrecognized arguments`, exit 2, and zero
    bytes on stdout.

    `resolve.clear_freeze` is tested where it lives, so what is pinned here
    is the CLI half: the flag exists, it calls `clear_freeze` with a reason,
    and it prints what came back.
    """
    seen = {}

    class Ledger:
        def __init__(self, path, **kwargs):
            self.path = path
            self.frozen = True

        def summary(self):
            return {"readable": True, "frozen": self.frozen,
                    "frozen_for_sec": 3600 if self.frozen else 0,
                    "frozen_reason": "FloodWait 3600 (recorded 2026-08-25)"}

        def clear_freeze(self, reason):
            seen["reason"] = reason
            self.frozen = False
            return {"cleared_frozen_until": 1.0, "reason": reason}

    monkeypatch.setattr(tg.resolve_module, "ResolveLedger", Ledger)
    result = cli("budget", "--unfreeze", "--reason", "FloodWait long expired")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["ok"] is True
    assert result.json["was_frozen"] is True
    assert result.json["frozen"] is False
    assert result.json["cleared"]["reason"] == "FloodWait long expired"
    assert seen["reason"] == "FloodWait long expired"

    # And with no --reason there is still a reason, because "the cleared value
    # and the reason are recorded" is the whole of the rule.
    result = cli("budget", "--unfreeze")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert "budget --unfreeze" in seen["reason"]


def test_verify_confirms_a_type_only_from_a_page_it_fetched_itself(
    cli, site, probe, tmp_path, monkeypatch
):
    """`type_confirmed` is a claim about evidence, so it is measured: the
    landing act must have happened in THIS call. With the fetch removed -- a
    card handed over by anything that did not go to the network -- the stored
    type stands and the conflict is recorded, exactly as before."""
    build_site(site, probe)
    seed_registry(tmp_path, "tdlibchat", type="channel", status="alive",
                  members=16674, found_via="manual")

    import discover as discover_module
    import tgparse
    cached = tgparse.PeerCard(username="tdlibchat", exists=True, type="group",
                              title="TDLib", members=16674)
    monkeypatch.setattr(discover_module, "verify",
                        lambda web, username, **kwargs: cached)

    result = cli("verify", "tdlibchat", "--write")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["type_corrections"] == []
    assert not any(v.get("type_confirmed") for v in result.json["verified"])
    stored = get_source(cli, "tdlibchat")
    assert stored["type"] == "channel", "a card nobody fetched moved the route"
    assert stored["type_conflict"]["seen"] == "group"


def test_ids_covered_counts_the_ids_an_album_hides():
    """An album is one `data-post` block, one permalink and several ids:
    verified live on `t.me/s/nexta_tv`, where a page carrying 27033-27052
    published 18 blocks and 27043/27044 lived only in `?single` links. `found`
    answers how many posts came back; this answers how many ids were seen."""
    class M:
        def __init__(self, ident, ids=None):
            self.id = ident
            if ids is not None:
                self.ids = ids

    assert tg.ids_covered([M(1), M(2)]) == 2
    assert tg.ids_covered([M(27042, [27042, 27043, 27044]), M(27052)]) == 4
    assert tg.ids_covered([{"id": 1, "ids": [1, 2]}, {"id": 3}]) == 3
    assert tg.ids_covered([]) == 0

    # And the number the CLI prints comes from the READER's own count when it
    # kept one: `read.py` counts distinct ids across the whole walk, so an id
    # two pages both carried is one id there and two here. Counting off the
    # messages is the fallback for a result that has no such field -- a partial
    # harvest hung on an exception, or a test double.
    class R:
        def __init__(self, messages, **kw):
            self.messages = messages
            self.__dict__.update(kw)

    assert tg.ids_seen_of(R([M(1), M(2)], ids_seen=5)) == 5
    assert tg.ids_seen_of(R([M(1), M(2)])) == 2
    assert tg.ids_seen_of(R([M(27042, [27042, 27043])], ids_seen=0)) == 2
    assert tg.ids_seen_of(R([])) == 0


def test_an_empty_query_is_refused_before_the_wire_end_to_end(cli, site, probe):
    """Both halves in place: `read.py` raises, `tg.py` maps it to 2.

    The sibling test above stubs the refusal so that the CLI half is pinned
    on its own. This one spends no request and asks for none: `search durov --query ""`
    used to fetch `t.me/s/durov` -- the front page -- and report its twenty
    newest posts as twenty hits for that query.
    """
    build_site(site, probe)
    for empty in ("", "   "):
        result = cli("search", "durov", "--query", empty)
        assert result.exit_code == tg.EXIT_USAGE, result.stdout
        assert result.json["error_type"] == "NothingAsked"
        assert not site.requested, "a request was spent on a query nobody asked"


def test_budget_unfreeze_clears_a_real_freeze_end_to_end(cli, tmp_path):
    """Both halves in place: `resolve.clear_freeze` and the flag.

    The sibling test above stubs the ledger so that the CLI half is pinned on
    its own; this one freezes a real ledger and lifts it through the CLI.
    """
    path = Path(cli("budget").json["path"])
    ledger = resolve_module.ResolveLedger(path, daily_ceiling=10, burst_ceiling=3,
                                          burst_window=60, min_gap=1,
                                          join_ceiling=1)
    ledger.freeze(3600, "FloodWait 3600")
    assert cli("budget").json["frozen"] is True

    result = cli("budget", "--unfreeze", "--reason", "the clock was wrong")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["was_frozen"] is True
    assert result.json["frozen"] is False
    assert cli("budget").json["frozen"] is False


def test_a_non_numeric_request_ceiling_is_refused_in_json(cli):
    """`--max-requests abc` was argparse's refusal: exit 2, empty stdout.

    The flag's own check catches 0 and negatives, but `type=int` ran first and
    argparse never prints JSON -- so the caller parsing stdout got an empty
    string for a mistyped ceiling and an error object for a zero one.
    """
    result = cli("--max-requests", "abc", "verify", "durov")
    assert result.exit_code == tg.EXIT_OPERATOR, result.stdout
    assert result.json["error_type"] == "UsageError"
    assert "whole number" in result.json["error"]


def test_budget_answers_even_when_the_history_state_is_damaged(cli, tmp_path):
    """`budget` is the command SKILL.md calls always safe to ask.

    A damaged `account-history.json` raised out of the new history block and
    came back as exit 5, which the table reserves for "the surface answered
    badly" -- about a file that never left this disk. The ledger's own
    unreadable verdict has always been a field, and so is this one now.
    """
    state = Path(cli("budget").json["path"]).parent
    (state / account_module.HISTORY_STATE_FILE).write_text("{ broken",
                                                           encoding="utf-8")

    result = cli("budget")
    assert result.exit_code == tg.EXIT_OPERATOR, result.stdout
    assert result.json["ok"] is False
    assert result.json["readable"] is False
    assert result.json["history"]["readable"] is False


def test_selftest_names_the_probe_that_is_missing(cli, tmp_path):
    """A corpus short one page is an operator error, not a crash.

    `selftest` is the command whose whole job is a clear verdict about the
    parsers, and it read its ten pages straight off disk: point `--probes` at a
    directory holding nine of them and it answered with a bare
    FileNotFoundError and a traceback.
    """
    short = tmp_path / "nine-pages"
    short.mkdir()
    (short / "A01-s-durov.html").write_text("<html></html>", encoding="utf-8")

    result = cli("selftest", "--probes", str(short))
    assert result.exit_code == tg.EXIT_OPERATOR, result.stdout
    assert result.json["error_type"] == "UsageError"
    assert "C01-landing-durov.html" in result.json["error"]


def test_budget_reports_and_lifts_the_history_freeze_too(cli, tmp_path):
    """The freeze a CLI run can actually earn is the history one.

    `resolveUsername` is off every ordinary path, so the resolve ledger is the
    freeze that almost never fires; `getHistory`, `contacts.search` and
    `messages.search` all gate on the history log instead. Reading only the
    ledger, `budget` answered `frozen: false` while all three account commands
    refused -- from the command SKILL.md calls the safety check -- and
    `--unfreeze` had nothing to lift.
    """
    state = Path(cli("budget").json["path"]).parent
    log = account_module.HistoryLog(state / account_module.HISTORY_STATE_FILE)
    log.freeze(3600, "FloodWait 3600 on getHistory")

    frozen = cli("budget")
    assert frozen.json["frozen"] is True, frozen.stdout
    assert frozen.json["history"]["history_frozen"] is True
    assert frozen.json["ledger"]["frozen"] is False, "the resolve ledger is clean"

    lifted = cli("budget", "--unfreeze", "--reason", "the clock was wrong")
    assert lifted.exit_code == tg.EXIT_OK, lifted.stdout
    assert lifted.json["was_frozen"] is True
    assert lifted.json["frozen"] is False
    assert cli("budget").json["frozen"] is False


# ==========================================================================
# Found by re-testing the repaired tree
# ==========================================================================
def test_run_refuses_a_directory_that_is_not_a_run_and_fills_one_that_is_empty(
    cli, site, probe, tmp_path
):
    """The run-folder check, extended to `open_run`. Measured on the tree
    after the first repair:

        tg.py --run <dir holding one file> discover --text "@durov"
        -> exit 0, and run.json + notes/sources/ created inside it

    `--run` checked the leaf for existence and never asked whether it was a run,
    so the hole `report <stranger>` had was still open one flag over.

    Three branches, and this test walks all three:

    * an existing NON-EMPTY directory with no marker -> refused, exit 2, nothing
      written, no request spent;
    * an EMPTY directory -> filled, because there is nothing in it to destroy
      and `finish()` stamps the marker on the way out;
    * a real run folder -> accepted, as before.

    The fourth branch, a path that does not exist, stays a refusal and is pinned
    by `test_a_run_folder_that_does_not_exist_is_refused_instead_of_invented`.
    """
    build_site(site, probe)

    stranger = tmp_path / "notarun"
    stranger.mkdir()
    (stranger / "my-important-file.txt").write_text("hello", encoding="utf-8")
    refused = cli("--run", stranger, "discover", "--text", "look at @durov")
    assert refused.exit_code == tg.EXIT_USAGE, refused.stdout
    assert refused.json["error_type"] == "NotARunFolder"
    assert refused.json["wrote_anything"] is False
    assert [p.name for p in stranger.iterdir()] == ["my-important-file.txt"]
    assert not site.requested

    # A fetching command is refused the same way, and before the GET.
    assert cli("--run", stranger, "verify", "durov").exit_code == tg.EXIT_USAGE
    assert not site.requested, "a request was spent on a folder that is not a run"
    assert [p.name for p in stranger.iterdir()] == ["my-important-file.txt"]

    empty = tmp_path / "empty"
    empty.mkdir()
    filled = cli("--run", empty, "discover", "--text", "look at @durov")
    assert filled.exit_code == tg.EXIT_OK, filled.stdout
    marker = json.loads((empty / "run.json").read_text(encoding="utf-8"))
    assert marker["schema"] == "telegram-research.run/1"
    # And it is a run folder from now on, by the same test everything else uses.
    assert cli("--run", empty, "discover", "--text", "@tdlibchat").exit_code == 0

    root = a_run(cli, tmp_path)
    assert cli("--run", root, "verify", "durov").exit_code == tg.EXIT_OK


def test_selftest_asks_for_the_id_count_not_the_block_count(cli, tmp_path, probe):
    """`check("durov.messages",
    len(page.messages), 20)` is the exact pattern removed everywhere else: an
    album is ONE `data-post` block carrying several ids, so the block count is
    short by the size of every album on the page.

    Both numbers are 20 on this frozen probe, which has no album on it -- the
    change is which question the check asks, and the sibling test below is where
    the two answers part company.
    """
    result = cli("selftest")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    names = {c["check"]: c for c in result.json["checks"]}
    assert "durov.ids_seen" in names, sorted(names)
    assert names["durov.ids_seen"]["want"] == 20
    assert names["durov.ids_seen"]["got"] == 20
    assert names["durov.ids_seen"]["ok"] is True
    assert "durov.messages" not in names, "the block count is still the assertion"


def album_corpus(tmp_path: Path, probe_dir: Path) -> Path:
    """The eight probes `selftest` reads, with the durov page turned into one
    that carries an album -- 19 blocks over 20 ids.

    Built here rather than saved into the corpus: `tests/fixtures/probes`
    holds pages as they came off the wire and nothing may edit them. The surgery is the one Telegram performs
    itself -- the last block disappears and its id survives only as the
    `?single` permalink inside the previous block's `js-message_grouped_wrap`,
    exactly as measured live on `t.me/s/nexta_tv`.
    """
    import re
    out = tmp_path / "album-probes"
    out.mkdir(parents=True, exist_ok=True)
    for name in SELFTEST_PROBES:
        (out / name).write_text(
            (probe_dir / name).read_text(encoding="utf-8", errors="replace"),
            encoding="utf-8")
    body = (out / "A01-s-durov.html").read_text(encoding="utf-8", errors="replace")
    starts = [m.start() for m in re.finditer(r'<div class="tgme_widget_message_wrap', body)]
    last, prev = starts[-1], starts[-2]
    end = body.index("</section>", last)
    album = ('<div class="js-message_grouped_wrap">'
             '<a href="https://t.me/durov/543?single"></a></div>')
    merged = body[prev:last].replace('<div class="tgme_widget_message_text',
                                     album + '<div class="tgme_widget_message_text', 1)
    (out / "A01-s-durov.html").write_text(body[:prev] + merged + body[end:],
                                          encoding="utf-8")
    return out


def test_selftest_keeps_its_id_check_on_a_page_that_carries_an_album(
    cli, tmp_path, fixtures
):
    """The same page with one album on it: 19 blocks, 19 messages, **20 ids**.

    The id check still passes, which is the whole of the counting rule. The two
    block-shaped checks report 19 and go red -- correctly, because they are
    asking about blocks and this page really has 19 of them. If a later pass
    moves `durov.texts_non_empty` onto an id-shaped question too, this test is
    where that shows up.
    """
    corpus = album_corpus(tmp_path, fixtures)
    result = cli("selftest", "--probes", corpus)
    names = {c["check"]: c for c in result.json["checks"]}

    assert names["durov.ids_seen"]["got"] == 20
    assert names["durov.ids_seen"]["ok"] is True
    assert names["durov.blocks_unparsed"]["got"] == 0, "the album block parsed fine"
    assert names["durov.chat_id"]["ok"] is True
    assert names["durov.page_before"]["ok"] is True

    assert names["durov.blocks_seen"]["got"] == 19
    assert result.exit_code == tg.EXIT_INTERNAL
    assert set(result.json["failed"]) == {"durov.blocks_seen", "durov.texts_non_empty"}


def test_a_second_compact_is_a_refusal_with_its_own_code_not_a_crash(cli, tmp_path):
    """The registry half. Measured on the tree after the first repair:

        registry compact   -> exit 0, "backup": null   (and sources.jsonl.bak on disk)
        registry compact   -> exit 9, error_type WouldDestroy, internal: true,
                              traceback on stderr

    `internal: true` says "this is a bug in tg.py, not something you typed"
    about a deliberate refusal whose way past is `--force`. And the first
    command reported no backup while the file it refuses to overwrite was
    already sitting beside the registry.
    """
    root = state_dir(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    seed = [{"username": "aaaa1", "type": "channel", "status": "alive"},
            {"username": "bbbb2", "type": "group", "status": "alive"},
            {"username": "aaaa1", "type": "channel", "status": "alive",
             "max_id_seen": 91234}]
    (root / "sources.jsonl").write_text(
        "\n".join(json.dumps(rec) for rec in seed) + "\n", encoding="utf-8")

    first = cli("registry", "compact")
    assert first.exit_code == tg.EXIT_OK, first.stdout
    assert first.json["kept"] == 2
    backup = Path(first.json["backup"])
    assert backup.exists(), "the backup was written but not reported"
    assert first.json["backup_bytes"] == backup.stat().st_size > 0
    kept_bytes = backup.read_bytes()

    second = cli("registry", "compact")
    assert second.exit_code == tg.EXIT_WOULD_DESTROY, second.stdout
    assert second.exit_code != 1
    assert second.json["ok"] is False
    assert second.json["error_type"] == "WouldDestroy"
    assert not second.json.get("internal"), "a documented refusal called itself a bug"
    assert "Traceback" not in second.stdout
    assert "--force" in second.json["next"]
    assert backup.read_bytes() == kept_bytes, "the refusal still replaced the backup"

    forced = cli("registry", "compact", "--force")
    assert forced.exit_code == tg.EXIT_OK, forced.stdout
    assert forced.json["forced"] is True
    assert Path(forced.json["backup"]).exists()


# ==========================================================================
# What the CLI must SAY
# ==========================================================================
LYZEM_SELECT = (
    '<select name="per-page">'
    '<option value="10">10</option><option value="25">25</option>'
    '<option value="50">50</option><option value="100">100</option>'
    "</select>"
)


def lyzem_page(select: str = LYZEM_SELECT, *, results: int = 50,
               claims: int = 120) -> str:
    """A lyzem search page: its page-size control and some result blocks.

    `results` defaults to the page size the skill asks for, because a page
    carrying FEWER than it asked for over an index claiming more is itself one
    of the silent cuts `parse_lyzem` counts -- so a fixture with two blocks on
    it would trip that counter for a reason that has nothing to do with the
    test using it.
    """
    blocks = "".join(
        f'<div class="search-result"><a href="https://t.me/chan{i}/{i}">'
        f"чат про аренду {i}</a><span>аренда квартиры в центре {i}</span></div>"
        for i in range(1, results + 1)
    )
    return f"<html><body>{select}<div>{claims} results</div>{blocks}</body></html>"


def test_search_says_when_the_surface_capped_instead_of_ending(cli, site, probe,
                                                               tmp_path):
    """The cap blocker, on the half that reaches the reader.

    The `?q=` surface fills its first page and then stops serving. Measured live
    on a news channel of 98 658 posts: 21 hits for a word that appears in 32
    of its last 60 posts, and `&before=` past the last hit returns nothing, so it is
    not a paging bug. `read.py` refuses to call that `exhausted` and sets
    `surface_truncated` with `stop_reason: "surface_cap"` -- and none of it
    reached stdout, which is the only place a subagent can see anything. A
    truncated answer that prints exactly like a complete one is the defect.
    """
    full = probe("A01-s-durov.html")            # 20 hits and a cursor at 523
    tail = probe("C15-s-durov-q-rare.html")     # 7 more, and no cursor at all
    site.add("https://t.me/s/durov?q=bitcoin", full)
    site.add("https://t.me/s/durov?q=bitcoin&before=523", tail)

    result = cli("search", "durov", "--query", "bitcoin", "--max-pages", 5)
    assert result.exit_code == tg.EXIT_OK, result.stdout
    row = result.json["results"][0]
    assert row["surface_truncated"] is True
    assert row["stop_reason"] == "surface_cap"
    assert row["exhausted"] is False, "a cap reported as the end of the matches"
    assert row["ids_seen"] == row["found"] == 27
    assert "cap" in (row["stopped_early"] or "")

    # And at the top of the object, where a caller that reads one level deep
    # will see it.
    assert result.json["partial"] is True
    assert result.json["surface_truncated"] == ["bitcoin"]
    assert "not all of them" in result.json["warning"]


def test_search_does_not_cry_truncation_on_a_short_first_page(cli, site, probe):
    """The other half of the same rule: a first page that came back SHORT is
    all there was, and saying "partial" about it would be its own false note."""
    build_site(site, probe)                     # SEARCH_1 serves 7 hits
    result = cli("search", "durov", "--query", "bitcoin")
    row = result.json["results"][0]
    assert row["surface_truncated"] is False
    assert row["exhausted"] is True
    assert result.json["partial"] is False
    assert result.json["warning"] is None
    assert row["ids_seen"] == 7


def test_discover_asks_lyzem_with_the_parameter_lyzem_listens_to(cli, site):
    """Item 7, and the reason it survived two passes.

    The stub in this file spelled `per_page=50` by hand -- the parameter lyzem
    ignores. The real one is `per-page`, and the difference was measured live:
    4 unique peers against 33 on one query, 10 against 50 on another, with
    `dropped: {}` and no note. The test stayed green because it asserts the GET
    never happens, so the wrong URL was never requested and never contradicted:
    a fixture agreeing with the bug.

    The URL is spelled out HERE, in the assertion, on purpose. `FakeSite` raises
    for anything it was not given, so a build that goes back to the ignored
    parameter fails on this line instead of quietly fetching a page it did not
    ask for.
    """
    wanted = "https://lyzem.com/search?q=vpn&f=messages&per-page=50"
    site.add(wanted, lyzem_page())
    result = cli("discover", "--lyzem-query", "vpn", "--lyzem-kind", "messages")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert site.requested == [wanted], site.requested
    names = [c["username"] for c in result.json["candidates"]]
    assert len(names) == 50 and names[0] == "chan1", names[:3]
    # The page's own control agrees with what we sent, so there is nothing to
    # report -- and `silent_cuts` exists to say so either way.
    assert result.json["silent_cuts"] == []


def test_discover_prints_the_cut_when_lyzem_renames_its_page_size(cli, site):
    """Item 6. The counters `parse_lyzem` keeps went to stderr only, and stderr
    is not what a subagent reads. A rename of the page-size parameter costs 88 %
    of the candidates and looks exactly like a thin index; this is the line that
    makes it look like what it is."""
    renamed = LYZEM_SELECT.replace('name="per-page"', 'name="page-size"')
    site.add(tg.discover_module.lyzem_url("vpn"), lyzem_page(renamed))
    result = cli("discover", "--lyzem-query", "vpn", "--lyzem-kind", "messages")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    cuts = result.json["silent_cuts"]
    assert cuts and any("page-size" in note for note in cuts), cuts
    assert any("LYZEM_PER_PAGE_PARAM" in note for note in cuts)
    # And in the prose the report is written from, not only in the machine field.
    assert any("page-size" in note for note in result.json["notes"])


def test_queries_record_says_what_the_mining_cut(cli, tmp_path):
    """Item 6, the stage-3 half. A shortlist that is a `top` cut, a floor no
    batch of that size could clear, a footer removed -- each makes
    `candidates: []` mean something different, and none of them was said out
    loud. `queries record` now prints `log.last_mining`."""
    root = a_run(cli, tmp_path)
    posts = [{"username": "durov", "id": i, "url": f"https://t.me/durov/{i}",
              "text": "аренда квартиры рахмету депозит", "found_by": "аренда"}
             for i in range(1, 4)]
    (root / "posts.jsonl").write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in posts) + "\n",
        encoding="utf-8")
    assert cli("queries", root, "start", "--query", "аренда").exit_code == 0

    result = cli("queries", root, "record", "--top", 1)
    assert result.exit_code == tg.EXIT_OK, result.stdout
    mining = result.json["mining"]
    assert mining["documents"] == 3
    assert mining["top"] == 1
    assert mining["min_documents"] >= 1
    assert mining["returned"] == len(result.json["candidates"]) == 1
    assert mining["qualified"] >= 2, mining
    assert mining["cut_by_top"] >= 1, "a shortlist that is a cut said nothing"


# ==========================================================================
# Found by a live run of the assembled skill
# ==========================================================================
def test_newrun_lands_under_the_root_and_not_in_whatever_shell_ran_it(
    cli, tmp_path, monkeypatch
):
    """The same command in two shells created two run folders in two places.

    Measured 2026-08-25 with no `--root`:

        from a scratch directory  -> run: telegram-runs\\<slug>, created there
        from the project root     -> the same relative string, in the project
        from the skill root       -> a whole run inside
                                     .claude\\skills\\telegram-research\\...

    So `SKILL.md`'s promise about where a run lives was true only by accident of
    `cwd`, and the printed `run` -- a bare relative string -- was valid only
    from the shell that produced it, which is exactly what the `next:` line
    hands to the next command. Same class as the state directory following the
    shell.

    The anchor is `config`'s own `root`. It is passed explicitly here: this
    suite writes nothing outside `tmp_path`, and the earlier version of this
    test built a real run folder inside the checkout and swept it up with
    `shutil.rmtree` afterwards -- a `pytest` that litters somebody else's
    project is not a `pytest` anybody should run.
    """
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)            # a shell somewhere else entirely

    result = cli("--root", project, "newrun", "--question", "проба места",
                 "--topic", "qprobe")
    assert result.exit_code == tg.EXIT_OK, result.stdout

    run_root = Path(result.json["run"])
    assert run_root.is_absolute(), result.json["run"]
    assert run_root.parts[:1] != (".",)
    assert list(elsewhere.iterdir()) == [], "the run landed in the shell's cwd"
    assert run_root.is_relative_to(project.resolve()), run_root
    assert run_root.parent == project.resolve() / "telegram-runs"
    # The topic is a brief field and never a directory any more.
    assert "qprobe" not in run_root.parts

    # Everything the caller is handed next is absolute too, or the `next:` line
    # only works from the directory that happened to produce it.
    assert Path(result.json["sources_dir"]).is_absolute()
    assert str(run_root) in result.json["next"]
    assert run_root.is_dir()


def test_an_explicit_root_still_decides_and_is_made_absolute(cli, tmp_path,
                                                                    monkeypatch):
    """The flag is the operator speaking, so it keeps ordinary command-line
    semantics -- relative to the shell -- and is made absolute once, here, so
    that everything printed downstream works from anywhere."""
    monkeypatch.chdir(tmp_path)
    result = cli("--root", ".", "newrun", "--question", "здесь",
                 "--topic", "qprobe")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    run_root = Path(result.json["run"])
    assert run_root.is_absolute()
    assert run_root.is_relative_to(tmp_path.resolve()), run_root
    assert (tmp_path / "telegram-runs").is_dir()
    assert not (tmp_path / "store").exists(), "the old topic layout is back"

    # And a `~` in the flag is a home directory, not a folder named `~`.
    assert tg.root_arg(argparse.Namespace(root="~")) == \
        Path("~").expanduser().resolve()
    assert tg.root_arg(argparse.Namespace(root=None)) is None
    assert tg.root_arg(argparse.Namespace(root="  ")) is None


def test_the_probe_override_does_not_follow_the_shell_either(monkeypatch, tmp_path):
    """The one environment path `tg.py` reads for itself.

    `TELEGRAM_RESEARCH_PROBES=tests/fixtures/probes` -- the shape the corpus now
    has -- used to name a different directory in every shell, and
    `~/probes` a directory literally called `~`.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_RESEARCH_PROBES", "tests/fixtures/probes")
    assert tg.default_probes() == config_module.repo_root() / "tests" / "fixtures" / "probes"

    monkeypatch.setenv("TELEGRAM_RESEARCH_PROBES", "~/probes")
    assert tg.default_probes() == Path("~/probes").expanduser().resolve()

    # With nothing set it is found from the skill's own file, which is what
    # makes `selftest` the first command anybody can run.
    monkeypatch.delenv("TELEGRAM_RESEARCH_PROBES")
    assert tg.default_probes().is_dir()


def test_group_names_the_ids_that_answered_nothing_instead_of_dropping_them(
    cli, site, probe
):
    """An id that answered nothing is a fact, and it costs the same GET as a hit.

    `SKILL.md` says twice that a group read costs one GET **per id ASKED**, not
    per message found -- measured on `birding_chats`, 175 ids for 3 messages, a
    1.7 % hit rate. So a command that returns one message out of three ids has
    to say which two were empty: `found: 1` alone reads as a cheap read, and
    dropping the empty ids hides both the cost and the fact that those ids were
    asked about at all.
    """
    ghost = probe("C08-embed-tdlibchat-50000.html")     # an id that answers empty
    site.add("https://t.me/tdlibchat/10002?embed=1", ghost)
    site.add("https://t.me/tdlibchat/10001?embed=1", ghost)
    site.add(EMBED, probe("C10-embed-tdlibchat-10000.html"))

    result = cli("group", "tdlibchat", "--id", 10000, "--id", 10001, "--id", 10002)
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["found"] == 1
    assert result.json["ids_asked"] == [10000, 10001, 10002]
    assert result.json["missing_ids"] == [10001, 10002], result.json
    assert result.json["mismatched_ids"] == []
    assert result.json["requests"] == 3, "an empty id costs the same GET as a hit"


def test_a_page_that_could_not_be_read_is_not_reported_as_an_empty_id(
    cli, site, probe
):
    """A page nobody could read is not evidence that the id holds nothing.

    `?embed=1` has three answers, not two: the message, Telegram's own "post
    not found", and a body this reader cannot make a message out of -- a login
    wall, or a front end that moved. Folding the third into `missing_ids` is
    how a talking group gets reported as a finished history, because the walk
    stops on empty ids. So the third answer gets its own list.
    """
    # Long enough not to trip the short-body stop signal: this is a full page
    # that simply carries no message the reader knows how to parse.
    wall = ("<!DOCTYPE html><html><head><title>Telegram</title></head><body>"
            + "<div class='tgme_page'>Please log in to view this.</div>" * 40
            + "</body></html>")
    site.add("https://t.me/tdlibchat/10001?embed=1", wall)
    site.add(EMBED, probe("C10-embed-tdlibchat-10000.html"))

    result = cli("group", "tdlibchat", "--id", 10000, "--id", 10001)
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["found"] == 1
    assert result.json["unreadable_ids"] == [10001], result.json
    assert result.json["missing_ids"] == [], "an unread page is not a proven empty id"


def test_a_stopped_group_read_banks_what_answered_before_the_stop(cli, site, probe,
                                                             tmp_path):
    """A 429 mid-walk banked what it had and said nothing about what it spent
    getting there -- and on this surface the spend IS the id count.

    **This assertion is the second measurement of the charging rule.** The first
    version of this test recorded `ids_tried: 2` for a walk that had put three
    GETs on the wire: `read.py` incremented the counter only after a fetch
    RETURNED, so the id whose GET took the 429 vanished from the count. That is
    the wrong side of "a call that reached the network and was then interrupted
    is charged; the safe side is to over-count what left the machine, never to
    under-count it" -- and on this surface it is not cosmetic. A group read
    costs one GET per id TRIED, so a counter that drops the failures makes an
    expensive read look cheap, and the failures are exactly what made it
    expensive. The increment moved into `read.py`'s `_reached_the_wire`; this
    test pins the charged side.

    The assertion is written against `site.requested` rather than against the
    literal 3, because the rule is "every act that left the machine is counted"
    and that is the thing worth pinning.
    """
    site.add(EMBED, probe("C10-embed-tdlibchat-10000.html"))
    site.add("https://t.me/tdlibchat/10001?embed=1", "rate limited", status=429)

    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    # Ids are asked in order, so 10000 answers and 10001 takes the 429.
    result = cli("--run", root, "group", "tdlibchat", "--id", 10001, "--id", 10000)
    assert result.exit_code == tg.EXIT_STOPPED, result.stdout
    assert result.json["ok"] is False and result.json["stopped"]
    # The id that answered before the 429 is banked, not lost with the stop.
    assert result.json["found"] == 1
    assert [m["id"] for m in result.json["messages"]] == [10000]
    banked = (root / "posts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(banked) == 1, banked


def test_search_and_history_say_when_a_page_parsed_nothing(cli, site, probe):
    """`understood_nothing` and `blocks_unparsed` are computed by every walk and
    were printed by none. A page that carried message blocks and yielded no
    message is a front-end change, and on stdout it was byte-identical to a
    channel with nothing to say -- which is the one thing this skill exists not
    to report."""
    build_site(site, probe)
    search = cli("search", "durov", "--query", "bitcoin")
    assert search.json["results"][0]["understood_nothing"] is False
    assert search.json["results"][0]["blocks_unparsed"] == 0

    history = cli("history", "durov", "--max-pages", 1)
    assert history.json["understood_nothing"] is False
    assert history.json["blocks_unparsed"] == 0

    # A page whose message blocks no longer carry `data-post` parses to nothing
    # while the page itself is plainly full of messages.
    broken = probe("A01-s-durov.html").replace("data-post=", "data-nothing=")
    site.pages.clear()
    site.add(WALK_1, broken)
    blind = cli("history", "durov", "--max-pages", 1)
    assert blind.json["understood_nothing"] is True, blind.stdout
    assert blind.json["blocks_unparsed"] == 20
    assert blind.json["found"] == 0

    # And the `?q=` surface, which is the one an agent reaches for first: a
    # search whose hits all failed to parse used to print `found: 0` and
    # `found_nothing: true` -- "this channel never said that word".
    site.pages.clear()
    site.add(SEARCH_1, probe("C15-s-durov-q-rare.html").replace("data-post=",
                                                               "data-nothing="))
    deaf = cli("search", "durov", "--query", "bitcoin")
    row = deaf.json["results"][0]
    assert row["understood_nothing"] is True, deaf.stdout
    assert row["blocks_unparsed"] == 7
    assert row["found"] == 0


# ==========================================================================
# `search` picks the surface, and a group's surface is the account
# ==========================================================================
# One command instead of two. `search` used to refuse a group outright (exit 6,
# "read them with `group`") and send the caller to a walk that could not search
# at all: measured on `birding_chats`, 200 GETs, 2 messages, 0 of them carrying the
# word the run was about. The same question through `messages.search` is 1 call
# and 44 hits, and the peer it needs arrives from `contacts.search` rather than
# from the resolve that once froze this account for ten hours.

ACCOUNT_PEER = {"username": "birding_chats", "id": 1000000001, "access_hash": 77,
                "type": "group", "title": "Большой чат | Общение",
                "participants": 2835, "verified": False, "scam": False}

ACCOUNT_HITS = [
    {"id": 28569, "date": "2026-03-12T00:49:08+00:00", "text": "первое найденное сообщение",
     "author_name": "кто-то", "author_username": None, "author_id": 7,
     "reply_to_id": None, "via": "mtproto"},
    {"id": 15597, "date": "2024-09-29T18:42:51+00:00", "text": "второе найденное сообщение",
     "author_name": None, "author_username": None, "author_id": 8,
     "reply_to_id": None, "via": "mtproto"},
]


@pytest.fixture
def account_wire(monkeypatch, tmp_path):
    """A live-looking account path with a fake wire under it.

    The env switch is set because the CLI refuses before reading a credential
    without it; `_open_account` is replaced so nothing constructs Telethon and
    nothing reaches Telegram. What the fake records is the assertion surface:
    the tests below are mostly about calls that were NOT made.
    """
    import account as account_module

    monkeypatch.setenv("TELEGRAM_RESEARCH_ALLOW_LIVE", "1")
    # A live session hashes the current session string to stamp the peers it
    # caches: a fingerprint from a previous login would bless access hashes that
    # are already dead. Nothing here logs in -- the string is a stand-in of the
    # real length, and `_open_account` never runs.
    env = tmp_path / "telegram.env"
    env.write_text(
        "TELEGRAM_API_ID=1\n"
        "TELEGRAM_API_HASH=" + "a" * 32 + "\n"
        "TELEGRAM_SESSION=1" + "A9zK" * 88 + "\n",
        encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_RESEARCH_ENV", str(env))
    fake = FakeTransport()
    fake.with_contacts("birding_chats", [ACCOUNT_PEER])
    fake.with_contacts("аренда квартиры недорого", [ACCOUNT_PEER])
    fake.with_hits(1000000001, "слово", ACCOUNT_HITS, total=2)
    monkeypatch.setattr(tg, "_open_account", lambda cfg: fake)
    return fake


def group_in_registry(cli, tmp_path, name="birding_chats"):
    seed_registry(tmp_path, name, type="group", status="alive", found_via="manual")


def test_search_of_a_group_goes_to_mtproto_and_spends_no_resolve(
    cli, tmp_path, account_wire
):
    group_in_registry(cli, tmp_path)
    result = cli("search", "birding_chats", "--query", "слово")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["surface"] == "mtproto"
    assert account_wire.resolve_calls == [], "the group search resolved a name"
    assert result.json["resolves"] == 0
    # One call to find the peer, one to run the query.
    assert result.json["account_calls"] == 2
    row = result.json["results"][0]
    assert row["found"] == 2 and row["server_total"] == 2 and row["complete"] is True
    assert [m["id"] for m in row["messages"]] == [28569, 15597]
    assert row["messages"][0]["url"] == "https://t.me/birding_chats/28569"
    assert row["messages"][0]["found_by"] == "слово"


def test_the_second_question_about_a_group_costs_one_call_not_two(
    cli, tmp_path, account_wire
):
    """The peer cache is the replacement for the resolve, and this is what it
    buys: the access hash does not expire while the login lives, so looking the
    group up is a cost paid once for the life of the session."""
    group_in_registry(cli, tmp_path)
    assert cli("search", "birding_chats", "--query", "слово").json["account_calls"] == 2
    second = cli("search", "birding_chats", "--query", "слово")
    assert second.json["account_calls"] == 1, second.stdout
    assert len(account_wire.contacts_calls) == 1, "the peer was looked up twice"


def test_a_group_search_says_when_it_returned_less_than_the_server_holds(
    cli, tmp_path, account_wire, monkeypatch
):
    """A count that is not the count has to say so. The free `?q=` surface cannot
    know -- it caps silently and 21 hits got reported as what a channel said
    about a subject, out of a channel that used the word in 32 of its last 60
    posts. Here the server states the total, so silence about it is a choice."""
    account_wire.with_hits(1000000001, "слово", ACCOUNT_HITS, total=44)
    monkeypatch.setattr(tg, "MTPROTO_PAGE", 2)
    group_in_registry(cli, tmp_path)
    result = cli("search", "birding_chats", "--query", "слово", "--max-pages", 1)
    row = result.json["results"][0]
    assert row["found"] == 2 and row["server_total"] == 44
    assert row["complete"] is False
    assert result.json["partial"] is True
    assert result.json["incomplete_queries"] == ["слово"]
    assert "44" not in str(row["found"]) and result.json["warning"]


def test_a_group_search_with_the_live_switch_off_reads_no_credential(
    cli, tmp_path, monkeypatch
):
    """One switch stands between this command and the wire, so it has to refuse
    before it touches the credential file -- not after."""
    monkeypatch.delenv("TELEGRAM_RESEARCH_ALLOW_LIVE", raising=False)
    monkeypatch.setenv("TELEGRAM_RESEARCH_ENV", str(tmp_path / "nowhere.env"))
    group_in_registry(cli, tmp_path)
    result = cli("search", "birding_chats", "--query", "слово")
    assert result.exit_code == tg.EXIT_OPERATOR, result.stdout
    assert "TELEGRAM_RESEARCH_ALLOW_LIVE" in result.json["error"]
    assert "nowhere.env" not in result.json["error"], \
        "the refusal read the credential path before refusing"


def test_a_name_the_search_box_will_not_return_is_not_resolved_instead(
    cli, tmp_path, account_wire
):
    """`contacts.search` matches titles and usernames, so it can miss a name --
    and the answer to that is NOT the call that froze this account for ten hours.
    A resolve stays reachable from Python under `references/account.md`, and from
    no flag."""
    seed_registry(tmp_path, "quiet_group", type="group", status="alive",
                  found_via="manual")
    result = cli("search", "quiet_group", "--query", "слово")
    assert result.exit_code == tg.EXIT_OPERATOR, result.stdout
    assert account_wire.resolve_calls == []
    assert "resolve" in result.json["error"]
    assert result.json["account_calls"] == 1


def test_the_posts_a_group_search_found_land_in_the_run(cli, tmp_path, account_wire):
    group_in_registry(cli, tmp_path)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "слово",
                    "--topic", "t").json["run"])
    result = cli("--run", root, "search", "birding_chats", "--query", "слово")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    rows = [json.loads(line) for line
            in (root / "posts.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assert [r["id"] for r in rows] == [28569, 15597]
    assert rows[0]["username"] == "birding_chats" and rows[0]["found_by"] == "слово"
    # And the same de-duplication every other route gets, keyed (username, id).
    again = cli("--run", root, "search", "birding_chats", "--query", "слово")
    assert again.json["posts_suppressed_as_duplicates"] == 2
    assert len((root / "posts.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 2


def test_a_stale_cached_hash_is_repaired_inside_the_command(
    cli, tmp_path, account_wire
):
    """A permanent peer cache has exactly one failure mode, and it is recoverable.

    Verified live 2026-08-25 by corrupting the stored access_hash: Telegram
    answers `ChannelInvalidError`, and the repair costs one `contacts.search` --
    the same as never having cached at all. Two earlier versions of it failed
    here and both were found live, not by this suite: the first reported the
    refusal as exit 9, "a bug in tg.py"; the second refreshed the peer and then
    let `--max-pages 1` count the REFUSED call as the only page it was allowed,
    answering `found: 0` about a group holding 44 matches.
    """
    import account as account_module

    group_in_registry(cli, tmp_path)
    stale = dict(ACCOUNT_PEER, access_hash=999)
    cache = account_module.PeerCache(
        state_dir(tmp_path) / account_module.PEER_CACHE_FILE)
    cache.put([stale], _live_fingerprint())
    account_wire.stale_peer(999)

    result = cli("search", "birding_chats", "--query", "слово", "--max-pages", 1)
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["peer_refreshed"] is True
    assert result.json["results"][0]["found"] == 2, result.stdout
    # The refused call, the fresh look-up and the retry. All three left the
    # machine, and a count that dropped the refused one made the repair free.
    assert result.json["account_calls"] == 3
    assert account_wire.resolve_calls == []


def _live_fingerprint() -> str:
    """The fingerprint a live session in this fixture will stamp its peers with."""
    import config as config_module
    from resolve import session_fingerprint

    cfg = config_module.load()
    return session_fingerprint(
        config_module.read_credentials(cfg).get("TELEGRAM_SESSION", ""))


# ==========================================================================
# Stage 2: three channels, and none of them alone
# ==========================================================================
def test_discover_asks_lyzem_for_groups_and_channels_and_not_only_messages(cli, site):
    """`lyzem_url` has had a `kind` parameter since the first day and no caller
    ever set it, so the one discovery channel that can answer "which GROUPS talk
    about this" was permanently asking "which posts contain these words" -- by
    OR, over an index a third of whose names are dead. Measured live: a
    three-word query in message mode returned nothing on the subject; in group
    mode the first line was the group that is actually about it."""
    for kind in ("groups", "channels", "messages"):
        site.add(tg.discover_module.lyzem_url("vpn", kind=kind), lyzem_page())
    result = cli("discover", "--lyzem-query", "vpn")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert sorted(result.json["lyzem_kinds"]) == ["channels", "groups", "messages"]
    assert len(site.requested) == 3, site.requested
    assert all("f=" + k in " ".join(site.requested) for k in
               ("groups", "channels", "messages"))


def test_a_name_two_channels_found_says_which_two(cli, site, tmp_path):
    """Each channel is blind in its own way, so a name two of them produced
    independently is a better bet than a name one of them mentioned twice -- and
    the merge used to keep only whichever spoke first."""
    for kind in ("groups", "channels", "messages"):
        site.add(tg.discover_module.lyzem_url("vpn", kind=kind), lyzem_page())
    page = tmp_path / "page.txt"
    page.write_text("см. https://t.me/chan1 и https://t.me/onlyweb", encoding="utf-8")
    result = cli("discover", "--lyzem-query", "vpn", "--from-file", page,
                 "--found-via", "web")
    found = {c["username"]: c for c in result.json["candidates"]}
    assert sorted(found["chan1"]["channels"]) == ["lyzem", "web"]
    assert found["onlyweb"]["channels"] == ["web"]
    assert found["chan2"]["channels"] == ["lyzem"]


def test_discover_through_the_account_costs_one_call_and_no_resolve(
    cli, tmp_path, account_wire
):
    """Channel three, and the only one that asks Telegram itself. It also fills
    the peer cache, which is what makes a group it finds searchable afterwards
    without a resolve -- the double payoff the resolve removal depends on."""
    result = cli("discover", "--account-query", "аренда квартиры недорого")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert account_wire.resolve_calls == []
    assert result.json["account"]["calls"] == 1
    assert result.json["account"]["resolves"] == 0
    assert result.json["account"]["peers_cached"] == 1
    names = [c["username"] for c in result.json["candidates"]]
    assert names == ["birding_chats"]
    assert result.json["candidates"][0]["channels"] == ["account"]
    # The blind spot is stated wherever the channel is used: it never sees
    # inside a message, which is why it runs beside the other two.
    assert "cannot see inside a message" in result.json["account"]["blind_spot"]


# --------------------------------------------------------------------------
# Promises nothing was pinning: found by mutating the code and re-running
# --------------------------------------------------------------------------
def test_the_search_box_answering_about_another_peer_is_not_searched_instead(
    cli, tmp_path, account_wire
):
    """`contacts.search` matches TITLES, so asking it for one name can come back
    with a different group whose title happens to carry the word.

    Only the empty-result refusal was covered, so dropping the exact-username
    guard left the suite green -- and it would have meant searching somebody
    else's chat and reporting the hits under the name that was asked for.
    """
    account_wire.with_contacts("quiet_group", [ACCOUNT_PEER])   # a different name
    seed_registry(tmp_path, "quiet_group", type="group", status="alive",
                  found_via="manual")
    result = cli("search", "quiet_group", "--query", "слово")
    assert result.exit_code == tg.EXIT_OPERATOR, result.stdout
    assert account_wire.search_calls == [], "it searched a peer nobody asked for"
    assert "does not return @quiet_group" in result.json["error"]


def test_a_peer_that_stays_stale_is_refused_rather_than_looked_up_for_ever(
    cli, tmp_path, account_wire
):
    """`if refreshed: raise` bounds the stale-hash repair at ONE re-look-up.

    The repair test hands back a good hash, so a second refusal never happened in
    the suite and the cap was unproven. Without it the command loops --
    `contacts.search`, retry, refused, `contacts.search` -- and because the
    `PeerUnusable` path deliberately does not count a page, nothing ends it. That
    is an unbounded account spend, which is the one failure this whole repair
    exists to prevent.
    """
    import account as account_module

    group_in_registry(cli, tmp_path)
    stale = dict(ACCOUNT_PEER, access_hash=999)
    account_module.PeerCache(
        state_dir(tmp_path) / account_module.PEER_CACHE_FILE
    ).put([stale], _live_fingerprint())
    # Both the cached hash AND the one the search box hands back are refused.
    account_wire.stale_peer(999).stale_peer(ACCOUNT_PEER["access_hash"])

    result = cli("search", "birding_chats", "--query", "слово")
    assert result.exit_code == tg.EXIT_FETCH_FAILED, result.stdout
    assert result.json["error_type"] == "PeerUnusable"
    # One refused search, one look-up, one refused retry. Never a second loop.
    assert len(account_wire.contacts_calls) == 1, account_wire.contacts_calls
    assert len(account_wire.search_calls) == 2, account_wire.search_calls


def test_history_of_a_group_reads_it_through_the_account_in_one_call(
    cli, tmp_path, account_wire
):
    """`AccountSession.history` was fully accounted and **called by nothing**.

    "What are they talking about in there" is a question no query answers, and it
    is one `messages.getHistory` with `limit=100`. The accountless surface would
    have to try ~10 000 ids to find 100 live ones.
    """
    account_wire.with_history(ACCOUNT_PEER["id"], [
        {"id": 29327, "date": "2026-08-22T05:00:00+00:00", "text": "Hi",
         "author_id": 1, "author_name": None, "author_username": None,
         "reply_to_id": None, "via": "mtproto"},
        {"id": 29201, "date": "2026-08-08T05:00:00+00:00", "text": "погода на выходные",
         "author_id": 2, "author_name": None, "author_username": None,
         "reply_to_id": None, "via": "mtproto"},
    ])
    group_in_registry(cli, tmp_path)
    result = cli("history", "birding_chats", "--max-pages", 1)
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["surface"] == "mtproto"
    assert result.json["found"] == 2
    assert result.json["resolves"] == 0 and account_wire.resolve_calls == []
    # One call for the peer, one for the page.
    assert result.json["account_calls"] == 2
    assert result.json["messages"][0]["url"] == "https://t.me/birding_chats/29327"
    # A `history` walk has no query behind it, so `found_by` must stay null --
    # the same promise the channel walk has always kept.
    assert result.json["messages"][0]["found_by"] is None


def test_a_bounded_group_history_does_not_move_the_cursor(cli, tmp_path, account_wire):
    """The oldest cursor rule, applied to the new route: a walk stopped by its
    page ceiling writes no high-water mark, because the unread middle would be
    hidden for ever and every later run would report `reached_until_id` about it.
    """
    rows = [{"id": i, "date": None, "text": str(i), "author_id": None,
             "author_name": None, "author_username": None, "reply_to_id": None,
             "via": "mtproto"} for i in range(29327, 29327 - 100, -1)]
    account_wire.with_history(ACCOUNT_PEER["id"], rows)
    group_in_registry(cli, tmp_path)
    result = cli("history", "birding_chats", "--max-pages", 1, "--write")
    assert result.exit_code == tg.EXIT_OK, result.stdout
    assert result.json["cursor_written"] is False
    assert result.json["cursor_withheld_reason"]
    assert get_source(cli, "birding_chats").get("max_id_seen") is None


@pytest.mark.parametrize("command", ["search", "history"])
def test_a_page_ceiling_of_zero_is_refused_on_every_surface(
    cli, tmp_path, account_wire, command
):
    """One flag, one meaning. `--max-pages 0` is exit 2 on a channel, and on a
    group it used to be `max(1, ...)` -- silently one page, reported as a result.

    A caller who types 0 meaning "no limit" then gets a number that is not a
    count, out of a command that spent an account call to produce it.
    """
    group_in_registry(cli, tmp_path)
    args = ["--query", "слово"] if command == "search" else []
    result = cli(command, "birding_chats", *args, "--max-pages", 0)
    assert result.exit_code == tg.EXIT_USAGE, result.stdout
    assert result.json["error_type"] == "NothingAsked"
    assert account_wire.contacts_calls == [] and account_wire.search_calls == []
    assert account_wire.history_calls == [], "a refusal spent an account call"


# ==========================================================================
# The parser as an operator meets it
# ==========================================================================
def test_every_subcommand_is_listed_in_the_top_level_help():
    """`registry` was added with no `help=`, and argparse lists only the
    subcommands that have one: the command that repairs and inspects the source
    log was the single one missing from `tg.py --help`."""
    parser = tg.build_parser()
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(actions) == 1
    sub = actions[0]
    # The name is in the usage line either way. What decides whether it gets a
    # LINE of its own, with a sentence saying what it does, is `help=`:
    # argparse only records a choice in `_choices_actions` when it has one.
    described = {action.dest for action in sub._choices_actions}
    assert set(sub.choices) - described == set(), (
        "these subcommands have no help= and so appear nowhere in the list "
        f"`tg.py --help` prints: {sorted(set(sub.choices) - described)}")
    for action in sub._choices_actions:
        assert action.help, action.dest


def test_the_root_help_describes_the_flag_and_not_its_own_history():
    """"...which used to decide it" is a note about a repair, not a description
    of what the flag does. Help text is read by somebody deciding what to type."""
    # Collapsed, because argparse rewraps the text it prints.
    help_text = " ".join(tg.build_parser().format_help().split())
    assert "used to decide it" not in help_text
    assert "the project this skill is installed in" in help_text


def test_the_parser_adds_one_argument_per_statement():
    """`p.add_argument(...), p.add_argument(...)` builds a tuple and throws it
    away. It works, it reads as a typo, and it hides the second flag from
    anybody scanning the parser for one."""
    source = (SCRIPTS / "tg.py").read_text(encoding="utf-8")
    offenders = [n for n, line in enumerate(source.splitlines(), 1)
                 if "), p.add_argument(" in line]
    assert offenders == [], offenders


def test_history_refuses_a_before_that_is_not_a_message_id(cli, site):
    """`--before` and `--until-id` went into the URL exactly as typed, while
    `--id` and `--max-pages` next door refuse before the wire. Measured:
    `--before -5` reached `t.me`, came back with nothing, and cost a paid GET
    to say so."""
    refused = cli("history", "durov", "--before", -5)
    assert refused.exit_code == tg.EXIT_OPERATOR, refused.stdout
    assert refused.json["error_type"] == "UsageError"
    assert "--before" in refused.json["error"]

    negative_floor = cli("history", "durov", "--until-id", -1)
    assert negative_floor.exit_code == tg.EXIT_OPERATOR, negative_floor.stdout
    assert "--until-id" in negative_floor.json["error"]

    assert not site.requested, "a request was spent before the flags were checked"


def test_snippets_to_does_not_overwrite_a_file_that_is_already_there(
    cli, site, tmp_path
):
    """`report` refuses to replace a `report.md` it did not write; this wrote
    over whatever was at the path with no question and no backup. The obvious
    way to use the flag -- the same file for a second discovery pass -- lost
    the first pass's snippets."""
    site.add(tg.discover_module.lyzem_url("x"), "<html></html>")
    kept = tmp_path / "snippets.md"
    kept.write_text("what the first pass found", encoding="utf-8")

    refused = cli("discover", "--lyzem-query", "x", "--snippets-to", kept)
    assert refused.exit_code == tg.EXIT_OPERATOR, refused.stdout
    assert str(kept) in refused.json["error"]
    assert kept.read_text(encoding="utf-8") == "what the first pass found"
    assert not site.requested, "the paid GET happened before the flag was checked"


def test_report_md_is_written_the_way_the_other_artefacts_are(
    cli, tmp_path, monkeypatch
):
    """`report.md` went out through a bare `write_text` while `run.json` and
    `queries.json` beside it get the write guard and the atomic replace.
    `write_text` truncates first, so an interrupt here leaves a half-written
    report on the one file in the folder a human writes by hand."""
    root = a_run(cli, tmp_path)
    written: list[str] = []
    real = config_module.atomic_write_text

    def watched(path, text, **kwargs):
        written.append(Path(path).name)
        return real(path, text, **kwargs)

    monkeypatch.setattr(config_module, "atomic_write_text", watched)
    assert cli("report", root).exit_code == tg.EXIT_OK
    assert "report.md" in written, written
    assert tg.REPORT_PLACEHOLDER in (root / "report.md").read_text(encoding="utf-8")
