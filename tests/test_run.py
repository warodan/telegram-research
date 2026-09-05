"""The run folder and the command line, and the one test the spec asks for by name.

Section 7 of the spec: "the key must not reach a log, a report or
`fetchlog.jsonl`. Check that with a test at acceptance, not by eye." This file
is that check, and it earned its place twice. The first time, a credential
pasted into the question text rode the brief into `run.json`, past a redaction
that only ever covered the caller's `extra` dictionary. The second time, the
test written to close that hole exercised `Run.finish()` alone -- and the leak
had moved to `Run.open()`, which is the path `newrun` actually takes, so the
same session string sat in `brief.md`, `brief.json`, `report.md` and the folder
NAME while stdout showed `<redacted>` and the suite stayed green.

So the credential tests here do not assert about a function. They grep the whole
run folder and the run folder's own path.

Everything runs offline. The CLI tests drive `tg.main()` in process against the
`site` fixture, which replaces `urllib.request.build_opener` -- so `tgweb`,
`read` and `run` are all the real code and only the socket is fake.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = (Path(__file__).resolve().parent.parent
           / "skills" / "telegram-research" / "scripts")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import querycraft  # noqa: E402
import run as run_module  # noqa: E402
import tg  # noqa: E402
import tgparse  # noqa: E402
import tgweb  # noqa: E402
from run import Brief, Run, report_skeleton, slugify  # noqa: E402

# Shaped like the real thing and belonging to nobody. Length matters and was
# measured: a real Telethon v1 StringSession is 353 characters -- the version
# byte plus base64 of the packed dc id, ip, port and 256-byte auth key. The
# first version of this fixture was 83, which let the bare-session redaction
# pattern be loose enough to eat ordinary URL path segments out of fetched
# corpus text with no test noticing.
FAKE_SESSION = (
    "1BQANOTEuMTA4LjU2LjEyOAG7Xk9abcdefGHIJklmnopQRstuvWXyz0123456789ABCDEFGHIJKLMNOP"
    + "QRstuvWXyz0123456789ABCDEFGHIJKLMNOPabcdefGHIJklmnop" * 5
    + "GHIJklmnopq=="
)
assert len(FAKE_SESSION) == 353, len(FAKE_SESSION)
FAKE_API_HASH = "0123456789abcdef0123456789abcdef"

LANDING = "https://t.me/durov"
SEARCH_1 = "https://t.me/s/durov?q=bitcoin"
SEARCH_2 = "https://t.me/s/durov?q=bitcoin&before=62"
WALK_1 = "https://t.me/s/durov"
WALK_2 = "https://t.me/s/durov?before=523"
EMBED = "https://t.me/tdlibchat/10000?embed=1"


def _run(tmp_path, question="что пишут про аренду"):
    return Run(tmp_path / "run", Brief(question=question, topic="relocation"))


def credential_leaks(root: Path) -> list[str]:
    """Every place a fake credential survived -- files AND the folder's own path.

    The path is checked because a folder name outlives the file it was copied
    out of: it is in shell history, in the fetch log's absolute paths, and in any
    listing anybody pastes anywhere.
    """
    markers = {
        "session": FAKE_SESSION,
        "session-prefix": FAKE_SESSION[:16],
        "api_hash": FAKE_API_HASH,
    }
    found: list[str] = []
    whole_path = str(Path(root).resolve()).lower()
    for label, needle in markers.items():
        if needle.lower() in whole_path:
            found.append(f"<the run folder's path>: {label}")
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        blob = path.read_bytes().decode("utf-8", "replace").lower()
        for label, needle in markers.items():
            if needle.lower() in blob:
                found.append(f"{path.relative_to(root).as_posix()}: {label}")
    return found


def build_site(site, probe, *, with_hash: bool = False) -> None:
    """The two channel pages and the one group message every CLI test uses."""
    search = probe("C15-s-durov-q-rare.html")
    walk = probe("A01-s-durov.html")
    embed = probe("C10-embed-tdlibchat-10000.html")
    if with_hash:
        # Real markup, plus the one thing the corpus is full of and the redactor
        # cannot tell from an api_hash: 32 hex characters in somebody's message.
        embed = embed.replace("If you set permissions",
                              f"commit {FAKE_API_HASH} broke it. If you set permissions", 1)
    site.add(LANDING, probe("C01-landing-durov.html"))
    site.add(SEARCH_1, search)
    site.add(SEARCH_2, search)          # same ids -> the search stops by itself
    site.add(WALK_1, walk)
    site.add(WALK_2, walk)
    site.add(EMBED, embed)


# ==========================================================================
# The run folder itself
# ==========================================================================
def test_run_writes_every_file_the_contract_names(tmp_path):
    run = _run(tmp_path)
    run.log_fetch(
        tgweb.Response(url="https://t.me/durov", status=200, body="<html/>", bytes=7,
                       headers={"content-type": "text/html"})
    )
    run.write_posts([
        tgparse.Message(username="durov", id=523, url="https://t.me/durov/523",
                        date="2026-06-09T19:33:51+00:00", text="привет ⌚️",
                        found_by="аренда")
    ])
    run.write_delta([{"username": "durov", "type": "channel", "members": 11110268}])
    run.write_note("scout", "заметка агента")
    info = run.finish()

    for name in ("fetchlog.jsonl", "posts.jsonl", "registry-delta.jsonl", "run.json"):
        assert (run.root / name).exists(), name
    assert (run.root / "notes" / "scout.md").exists()
    assert (run.root / "notes" / "sources").is_dir()
    assert info["counters"] == {"requests": 1, "posts": 1, "sources": 1}


def test_run_json_carries_what_the_folder_gate_demands(tmp_path):
    """`schema`, `depth`, `gate`, `agents` and a run identity.

    Not decoration: `tg.py accept` and `require_run_folder` both read this
    record, and a folder missing any of it is not a run folder of this skill's.
    """
    run = Run(tmp_path / "run", Brief(question="q", depth="deep"))
    run.record_agent("newrun")
    data = run.finish()
    assert data["schema"] == "telegram-research.run/1"
    assert data["depth"] == "deep"
    assert data["agents"] == ["newrun"]
    assert data["run"] and data["started"] and data["finished"]
    assert "gate" in data
    assert "track" not in data, "the removed track field is back"


def test_fetchlog_records_the_act_and_never_the_page(tmp_path):
    run = _run(tmp_path)
    run.log_fetch(
        tgweb.Response(url="https://t.me/s/durov", status=200,
                       body="SECRET PAGE CONTENT", bytes=19,
                       headers={"content-type": "text/html"})
    )
    record = json.loads((run.fetchlog_path).read_text(encoding="utf-8").strip())
    assert record["url"] == "https://t.me/s/durov"
    assert record["status"] == 200
    assert "body" not in record
    assert "SECRET PAGE CONTENT" not in json.dumps(record)
    # A URL a search engine merely listed was never read. Everything logged here
    # was read, and says so, or nothing resting on it is citable.
    assert record["kind"] == "fetch"


def test_posts_keep_emoji_and_the_query_that_found_them(tmp_path):
    run = _run(tmp_path)
    run.write_posts([
        tgparse.Message(username="hanoi_chats", id=29327,
                        url="https://t.me/hanoi_chats/29327",
                        date="2026-08-22T17:58:18+00:00", text="Привет 🏠",
                        found_by="жетонов")
    ])
    row = json.loads(run.posts_path.read_text(encoding="utf-8").strip())
    assert row["url"] == "https://t.me/hanoi_chats/29327"
    assert row["date"] == "2026-08-22T17:58:18+00:00"
    assert "🏠" in row["text"]
    assert row["found_by"] == "жетонов"


def test_no_credential_reaches_any_file_in_the_run_folder(tmp_path):
    """The acceptance test section 7 asks for by name.

    Every route out is exercised at once: the credential is in the question (so
    it rides the brief), in a fetched URL, in an agent note, and in the extra
    dictionary handed to finish().
    """
    run = _run(tmp_path, question=f"вопрос с ключом {FAKE_SESSION} внутри")
    run.log_fetch(
        tgweb.Response(url=f"https://example.test/x?session={FAKE_SESSION}",
                       status=200, body="x", bytes=1,
                       headers={"content-type": "text/html"})
    )
    run.write_note("agent", f"случайно записал TELEGRAM_SESSION={FAKE_SESSION} "
                            f"и hash {FAKE_API_HASH}")
    run.finish({"creds": {"TELEGRAM_SESSION": FAKE_SESSION,
                          "TELEGRAM_API_HASH": FAKE_API_HASH}})

    assert credential_leaks(run.root) == []


def test_fetched_content_is_deliberately_not_scrubbed(tmp_path):
    """The other half of the rule, stated as a test so nobody "fixes" it later.

    Redaction covers what the skill authors. It must NOT cover fetched content:
    the api_hash pattern is any 32 hex characters, which is equally a commit
    hash or the middle of somebody's message, and running a redactor over
    `posts.jsonl` would quietly corrupt the corpus the run exists to collect.
    No code path carries our credential into a parsed message, so there is
    nothing to defend against here and real evidence to lose.
    """
    run = _run(tmp_path)
    run.write_posts([tgparse.Message(
        username="durov", id=1, url="https://t.me/durov/1",
        text=f"коммит {FAKE_API_HASH} сломал сборку")])

    assert FAKE_API_HASH in run.posts_path.read_text(encoding="utf-8")


def test_run_state_survives_the_process_that_wrote_it(tmp_path):
    """`attach` loads; `finish` merges. Four commands, one run, one arithmetic."""
    first = Run(tmp_path / "run", Brief(question="настоящий вопрос", topic="x"))
    (first.root / "brief.json").write_text(
        json.dumps(first.brief.as_dict(), ensure_ascii=False), encoding="utf-8")
    first.count("requests", 4)
    first.count("posts", 3)
    first.stop("429 from Telegram")
    first.record_agent("search")
    first.finish()

    second = Run.attach(first.root)
    assert second.brief.question == "настоящий вопрос"
    assert second.counters == {"requests": 4, "posts": 3}
    second.count("requests", 5)
    second.record_agent("history")
    data = second.finish()

    assert data["counters"] == {"requests": 9, "posts": 3}
    assert data["stop_reasons"] == ["429 from Telegram"]
    assert data["agents"] == ["search", "history"]
    assert data["brief"]["question"] == "настоящий вопрос"


def test_slugify_survives_cyrillic_and_punctuation():
    assert slugify("Что пишут про аренду?!") == "что-пишут-про-аренду"
    assert slugify("") == "run"
    assert len(slugify("а" * 200)) <= 40


def test_depth_is_a_decision_not_a_label():
    """`--depth deep` used to store the word and change nothing at all."""
    quick = Brief.for_depth("quick", question="q")
    deep = Brief.for_depth("deep", question="q")
    assert (quick.max_rounds, quick.max_requests) != (deep.max_rounds, deep.max_requests)
    assert deep.max_rounds > quick.max_rounds
    with pytest.raises(ValueError):
        Brief.for_depth("thorough", question="q")


def test_the_configured_budgets_are_what_depth_moves_around():
    """`max_rounds`, `min_new_posts_per_round` and `max_requests_per_run` were
    declared in `config` and read by nothing at all."""
    import config as config_module
    budgets = config_module.Budgets(max_rounds=7, min_new_posts_per_round=5,
                                    max_requests_per_run=90)
    normal = Brief.for_depth("normal", budgets=budgets, question="q")
    assert (normal.max_rounds, normal.min_new_posts, normal.max_requests) == (7, 5, 90)
    deep = Brief.for_depth("deep", budgets=budgets, question="q")
    assert deep.max_rounds > normal.max_rounds
    assert deep.max_requests > normal.max_requests


# ==========================================================================
# The report
# ==========================================================================
def test_the_report_states_what_the_run_spent_on_the_account(tmp_path):
    """The POSITIVE case, which nobody checked: the account line appeared in
    exactly one assertion in this file, and it was the negative one.

    The line used to be read off `brief.account_allowed` -- the run's INTENTION.
    Since `search` and `history` began routing a group to the account the two can
    differ, and a run that spent the account printed "the account was not used"
    over the very posts that call had returned. It is read off the counter now,
    and both directions are pinned here.
    """
    run = _run(tmp_path)
    run.count("requests", 4)
    run.count("account_calls", 3)
    text = report_skeleton(run, discovery=None, query_log=querycraft.QueryLog(),
                           sources_used=[], posts=[])
    head = text.split("## What was found")[0]
    assert "The account was used" in head, head
    assert "3" in head, "the report does not say how many calls it spent"
    assert "resolves — 0" in head
    assert "was not used" not in head, head


def test_report_skeleton_states_the_account_was_not_used(tmp_path):
    run = _run(tmp_path)
    run.count("requests", 22)
    run.count("posts", 95)
    posts = [{"username": "Hanoirentapartment", "url": "https://t.me/Hanoirentapartment/1"}]
    sources = [{"username": "Hanoirentapartment", "type": "channel",
                "members": 329, "found_via": "web"}]
    text = report_skeleton(run, discovery=None,
                           query_log=querycraft.QueryLog(),
                           sources_used=sources, posts=posts)

    assert "The account was not used" in text
    assert "Hanoirentapartment" in text
    assert "https://t.me/Hanoirentapartment" in text
    # An empty vocabulary is reported as a fact of the run, never quietly omitted
    # -- but only when the log is really there and really empty.
    assert "Not one word could be mined" in text


def test_report_does_not_assert_that_nothing_was_mined_when_there_is_no_log(tmp_path):
    """The sentence fired in every report ever generated, including runs whose
    `queries.md` listed four mined terms in the same folder."""
    run = _run(tmp_path)
    text = report_skeleton(run, discovery=None, query_log=None,
                           sources_used=[], posts=[])
    assert "Not one word could be mined" not in text
    assert "The round log was never kept" in text


def test_report_links_queries_md_only_when_it_exists(tmp_path):
    run = _run(tmp_path)
    text = report_skeleton(run, discovery=None, query_log=None,
                           sources_used=[], posts=[])
    assert "[queries.md](queries.md)" not in text

    (run.root / "queries.md").write_text("# Queries", encoding="utf-8")
    text = report_skeleton(run, discovery=None, query_log=None,
                           sources_used=[], posts=[])
    assert "[queries.md](queries.md)" in text


def test_report_counts_the_posts_the_folder_holds(tmp_path):
    """30 posts on disk and "Posts: 3" in the document a person reads."""
    run = _run(tmp_path)
    run.count("posts", 3)
    posts = [{"username": "durov", "url": f"https://t.me/durov/{i}"} for i in range(30)]
    text = report_skeleton(run, discovery=None, query_log=None,
                           sources_used=[], posts=posts)
    assert "Posts: 30" in text
    assert "a disagreement" in text       # and the disagreement is named, not hidden


# ==========================================================================
# The command line: the credential
# ==========================================================================
def test_newrun_keeps_the_credential_out_of_the_folder_and_out_of_its_path(cli, tmp_path):
    result = cli("--root", tmp_path, "newrun", "--topic", "leaktest",
                 "--question",
                 f"аренда TELEGRAM_SESSION={FAKE_SESSION} hash {FAKE_API_HASH}")
    assert result.exit_code == 0
    root = Path(result.json["run"])
    assert "<redacted>" in result.json["brief"]["question"]
    assert credential_leaks(root) == []
    assert FAKE_SESSION not in (root / "brief.md").read_text(encoding="utf-8")


def test_report_does_not_open_a_second_door_for_the_credential(cli, tmp_path):
    """The `--question` fallback, on a run folder whose `brief.json` is gone.

    The folder check moved onto the marker `run.json` carries, so this can no
    longer be a bare `mkdir` -- `report` refuses a directory that never was a
    run. The door being tested is unchanged: with no `brief.json` to read,
    `--question` is what the report is titled from.
    """
    root = Path(cli("--root", tmp_path, "newrun", "--question", "аренда",
                    "--topic", "leaktest").json["run"])
    (root / "brief.json").unlink()
    result = cli("report", root, "--question", f"секрет TELEGRAM_SESSION={FAKE_SESSION}")
    assert result.exit_code == 0, result.stdout
    assert credential_leaks(root) == []


def test_two_runs_of_one_question_on_one_day_do_not_share_a_folder(cli, tmp_path):
    """14 post lines, 7 distinct URLs, one brief describing only the second run."""
    first = cli("--root", tmp_path, "newrun", "--question", "аренда в Ханое",
                "--topic", "relocation")
    second = cli("--root", tmp_path, "newrun", "--question", "аренда в Ханое",
                 "--topic", "relocation", "--depth", "deep")
    assert first.json["run"] != second.json["run"]
    assert Path(first.json["run"]).exists() and Path(second.json["run"]).exists()


def test_newrun_without_a_question_is_refused(cli, tmp_path):
    result = cli("--root", tmp_path, "newrun", "--topic", "relocation")
    assert result.exit_code == tg.EXIT_OPERATOR
    assert result.json["ok"] is False
    assert not (tmp_path / "store" / "relocation" / "reports").exists()


# ==========================================================================
# The command line: originals, the fetch log and the counters
# ==========================================================================
def test_a_run_saves_the_pages_behind_the_posts_it_quotes(cli, site, probe, tmp_path):
    """The three commands that produce quotable posts saved not one page.

    `--save-to` was the only route to `notes/sources/`, and `SKILL.md` never
    mentions the flag.
    """
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "биткойн",
                    "--topic", "t").json["run"])
    result = cli("--run", root, "search", "durov", "--query", "bitcoin")
    assert result.exit_code == 0, result.stdout
    originals = sorted(p.name for p in (root / "notes" / "sources").glob("*.html"))
    assert originals, "search under --run saved no page at all"
    assert (root / "posts.jsonl").exists()
    assert (root / "fetchlog.jsonl").exists()

    history = cli("--run", root, "history", "durov", "--max-pages", 1)
    assert history.exit_code == 0
    group = cli("--run", root, "group", "tdlibchat", "--id", 10000)
    assert group.exit_code == 0, group.stdout
    saved = sorted(p.name for p in (root / "notes" / "sources").glob("*"))
    assert len(saved) >= 3, saved


def test_save_to_adds_a_destination_and_never_replaces_the_run(cli, site, probe, tmp_path):
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "биткойн",
                    "--topic", "t").json["run"])
    extra = tmp_path / "elsewhere"
    result = cli("--run", root, "search", "durov", "--query", "bitcoin",
                 "--save-to", extra)
    assert result.exit_code == 0
    in_run = sorted(p.name for p in (root / "notes" / "sources").glob("*.html"))
    in_extra = sorted(p.name for p in extra.glob("*.html"))
    assert in_run, "the run lost its originals to --save-to"
    assert in_extra == in_run


def test_run_json_accumulates_across_commands(cli, site, probe, tmp_path):
    """Each `--run` command used to overwrite the previous one's spend, and the
    brief `newrun` wrote never reached `run.json` at all."""
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "вопрос прогона",
                    "--topic", "t").json["run"])
    cli("--run", root, "search", "durov", "--query", "bitcoin")
    cli("--run", root, "history", "durov", "--max-pages", 1)

    data = json.loads((root / "run.json").read_text(encoding="utf-8"))
    posts_on_disk = len([1 for line in (root / "posts.jsonl")
                        .read_text(encoding="utf-8").splitlines() if line.strip()])
    fetches = len([1 for line in (root / "fetchlog.jsonl")
                  .read_text(encoding="utf-8").splitlines() if line.strip()])
    assert data["brief"]["question"] == "вопрос прогона"
    assert data["counters"]["posts"] == posts_on_disk
    assert data["counters"]["requests"] == fetches
    assert set(data["agents"]) >= {"newrun", "search", "history"}


def test_verify_and_discover_write_run_json_without_write(cli, site, probe, tmp_path):
    """A run made only of those two had no `run.json` at all.

    D5-on-`--run` changed how this is set up, not what it asks. Deleting
    `run.json` outright is now a directory that cannot say it is a run folder,
    and `--run` refuses it; the marker is stripped back to nothing but the
    schema instead, which is the same starting state for the thing under test:
    the run holds no record of these two commands until they write one.
    """
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    (root / "run.json").write_text(
        json.dumps({"schema": "telegram-research.run/1"}), encoding="utf-8")
    assert cli("--run", root, "verify", "durov").exit_code == 0
    after = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert after["counters"]["requests"] == 1
    assert "verify" in after["agents"]
    assert cli("--run", root, "discover", "--text", "@tdlibchat").exit_code == 0
    assert "discover" in json.loads(
        (root / "run.json").read_text(encoding="utf-8"))["agents"]


# ==========================================================================
# The command line: stdout
# ==========================================================================
def test_stdout_keeps_fetched_text_and_still_hides_our_own_credential(capsys):
    """Same post, two texts: stdout said `commit <redacted>` and the file did not.

    The agent quotes the one on stdout.
    """
    capsys.readouterr()
    tg.emit({
        "results": [{"messages": [{"text": f"commit {FAKE_API_HASH} broke it"}]}],
        "error": f"TELEGRAM_SESSION={FAKE_SESSION}",
        "note": f"our own note mentioning {FAKE_API_HASH}",
    })
    out = json.loads(capsys.readouterr().out)
    assert FAKE_API_HASH in out["results"][0]["messages"][0]["text"]
    assert FAKE_SESSION not in json.dumps(out)
    assert FAKE_API_HASH not in out["note"]


def test_stdout_and_posts_jsonl_agree_about_what_a_post_says(cli, site, probe, tmp_path):
    build_site(site, probe, with_hash=True)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    result = cli("--run", root, "group", "tdlibchat", "--id", 10000)
    assert result.exit_code == 0, result.stdout
    on_stdout = result.json["messages"][0]["text"]
    on_disk = json.loads((root / "posts.jsonl").read_text(encoding="utf-8").strip())["text"]
    assert on_stdout == on_disk
    assert FAKE_API_HASH in on_stdout


def test_emit_survives_an_emoji_on_a_cp1251_stdout(monkeypatch):
    """A successful run used to exit 1 with zero bytes of JSON after the folder
    was fully written, and the obvious response -- retry -- re-spends the whole
    network budget."""
    raw = io.BytesIO()
    monkeypatch.setattr(
        sys, "stdout", io.TextIOWrapper(raw, encoding="cp1251", errors="strict")
    )
    tg.emit({"text": "Привет 👍", "ok": True})
    sys.stdout.flush()
    payload = json.loads(raw.getvalue().decode("utf-8"))
    assert payload["text"] == "Привет 👍"


# ==========================================================================
# The command line: failures are JSON
# ==========================================================================
def test_a_broken_config_path_is_json_not_a_traceback(cli, tmp_path, monkeypatch):
    """`config.load()` sat outside the try, so the one error the code formats as
    JSON was the one error that always tracebacked."""
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(tmp_path / "nope.json"))
    result = cli("budget")
    assert result.exit_code == tg.EXIT_OPERATOR
    assert result.json["ok"] is False
    assert result.json["error_type"] == "ConfigError"


def test_a_run_path_that_is_a_file_is_json_not_a_traceback(cli, site, probe, tmp_path):
    """D5-on-`--run`: the code moved from 7 to 2 with every other "that is not a
    run folder" refusal, and it is still JSON rather than a traceback."""
    build_site(site, probe)
    plain = tmp_path / "plain.txt"
    plain.write_text("not a run", encoding="utf-8")
    result = cli("--run", plain, "verify", "durov")
    assert result.exit_code == tg.EXIT_USAGE
    assert result.json["ok"] is False
    assert result.json["error_type"] == "NotARunFolder"
    assert plain.read_text(encoding="utf-8") == "not a run"


def test_a_missing_from_file_is_json_not_a_traceback(cli, tmp_path):
    result = cli("discover", "--from-file", tmp_path / "nope.txt")
    assert result.exit_code == tg.EXIT_OPERATOR
    assert result.json["ok"] is False


def test_report_on_a_mistyped_path_creates_nothing_and_fails(cli, tmp_path):
    """A well-formed, confident, empty Russian report and `ok: true` was the
    answer to a typo.

    The exit code moved from 7 to 2: `NotARunFolder` is a usage refusal --
    the path names something that is not a run -- and is separate from
    `RunFolderError`, which is a run folder that is damaged.
    """
    missing = tmp_path / "no-such-run"
    result = cli("report", missing)
    assert result.exit_code == tg.EXIT_USAGE
    assert result.json["ok"] is False
    assert result.json["error_type"] == "NotARunFolder"
    assert not missing.exists()


def test_report_survives_one_corrupt_posts_line(cli, tmp_path):
    """The folder has to be a real run now -- a bare `mkdir` is no longer accepted
    by `report` at all -- and one unparseable line still costs only itself."""
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    (root / "posts.jsonl").write_text(
        '{"username":"durov","url":"https://t.me/durov/1"}\nBROKEN\n', encoding="utf-8")
    result = cli("report", root)
    assert result.exit_code == 0, result.stdout
    assert result.json["posts"] == 1
    assert result.json["corrupt_lines"]["posts.jsonl"] == [2]


def test_run_after_the_subcommand_is_accepted(cli, site, probe, tmp_path):
    """The documented position is before the subcommand; the natural
    transposition was `unrecognized arguments: --run <path>`."""
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    result = cli("verify", "durov", "--run", root)
    assert result.exit_code == 0
    assert result.json["run"] == str(root)


# ==========================================================================
# The command line: stop signals and ceilings
# ==========================================================================
def test_a_stop_signal_on_a_group_read_is_json_and_exit_three(cli, site, tmp_path):
    """A 429 mid-read is a stop signal, not a crash.

    It used to depend on WHICH PHASE was hit: the head-finding phase answered
    `tgweb.RunAborted:` on stderr with exit 1, and the identical 429 twenty
    requests later produced JSON and exit 3, so an agent branching on the exit
    code got the wrong answer half the time. There is no head-finding phase any
    more -- it was the machinery that guessed which ids to try -- and what is
    left has to keep answering the way the repaired half did.
    """
    site.add("https://t.me/floodgroup/1024?embed=1", "rate limited", status=429)
    stopped = cli("group", "floodgroup", "--id", 1024)
    assert stopped.exit_code == tg.EXIT_STOPPED, stopped.stdout
    assert stopped.json["ok"] is False and stopped.json["stopped"]


def test_discover_turns_a_stop_signal_into_json(cli, site, tmp_path):
    site.add(tg.discover_module.lyzem_url("flood test", kind="groups"),
             "rate limited", status=429)
    result = cli("discover", "--lyzem-query", "flood test")
    assert result.exit_code == tg.EXIT_STOPPED
    assert result.json["ok"] is False and result.json["stopped"]


def test_the_declared_request_ceiling_is_enforced(cli, site, probe, tmp_path):
    """674 requests from one command, past both the 400-per-run and the
    300-embed ceilings, with no warning in the output."""
    build_site(site, probe)
    for mid in range(9990, 10001):
        site.add(f"https://t.me/tdlibchat/{mid}?embed=1", probe("C08-embed-tdlibchat-50000.html"))
    result = cli("--max-requests", 3, "group", "tdlibchat",
                 *[a for mid in range(9990, 10001) for a in ("--id", mid)])
    assert result.exit_code in (tg.EXIT_OK, tg.EXIT_STOPPED)
    spent = len(site.requested)
    assert spent <= 3, f"the ceiling of 3 did not hold: {spent} requests"


def test_a_stop_reason_reaches_run_json_and_the_report(cli, site, tmp_path):
    """`stop_reasons` was `[]` in every `run.json` ever written, including a run
    killed by a 429, so the report's "what limited this run" section could never
    appear."""
    site.add("https://t.me/floodgroup/1024?embed=1", "rate limited", status=429)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    cli("--run", root, "group", "floodgroup", "--id", 1024)
    data = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert data["stop_reasons"], "the run does not record why it stopped"
    cli("report", root)
    assert "What limited this run" in (root / "report.md").read_text(encoding="utf-8")


# ==========================================================================
# The command line: stage 3, the cursor, and the small refusals
# ==========================================================================
def test_queries_enforces_the_round_ceiling_and_the_drift_ban(cli, tmp_path):
    """`querycraft` was imported by its own test and by nothing else: all three
    stoppers were enforced on nobody."""
    root = Path(cli("--root", tmp_path, "newrun", "--question", "аренда",
                    "--topic", "t", "--depth", "quick").json["run"])
    first = cli("queries", root, "start", "--query", "аренда")
    assert first.exit_code == 0 and first.json["round"] == 1
    assert (root / "queries.md").exists() and (root / "queries.json").exists()

    # quick == one round, and the ceiling now stops the second.
    second = cli("queries", root, "start", "--query", "аренда")
    assert second.exit_code == tg.EXIT_STOPPED
    assert "round ceiling" in second.json["stopped"]


def test_a_query_that_appears_in_no_retrieved_post_is_refused_as_drift(cli, tmp_path):
    root = Path(cli("--root", tmp_path, "newrun", "--question", "аренда",
                    "--topic", "t").json["run"])
    rows = [{"username": "x", "url": f"https://t.me/x/{i}",
             "text": "снимаю студию за жетони, контракт на год"} for i in range(4)]
    (root / "posts.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    cli("queries", root, "start", "--query", "аренда")
    cli("queries", root, "record")
    refused = cli("queries", root, "start", "--query", "ипотека")
    assert refused.exit_code == tg.EXIT_STOPPED
    assert refused.json["queries"][0]["allowed"] is False
    assert "drift" in refused.json["queries"][0]["why"]


def test_queries_record_mines_the_corpus_and_accept_takes_a_word(cli, tmp_path):
    root = Path(cli("--root", tmp_path, "newrun", "--question", "аренда",
                    "--topic", "t").json["run"])
    rows = [
        {"username": "x", "url": f"https://t.me/x/{i}",
         "text": "плачу жетони за студию, контракт на год"}
        for i in range(4)
    ]
    (root / "posts.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    cli("queries", root, "start", "--query", "аренда")
    mined = cli("queries", root, "record")
    assert mined.exit_code == 0
    terms = {c["term"] for c in mined.json["candidates"]}
    assert "жетони" in terms
    taken = cli("queries", root, "accept", "--term", "жетони", "--gloss", "валюта")
    assert taken.exit_code == 0
    assert "жетони" in (root / "queries.md").read_text(encoding="utf-8")
    invented = cli("queries", root, "accept", "--term", "рахмет")
    assert invented.exit_code == tg.EXIT_OPERATOR


def test_history_write_records_the_cursor_until_id_needs(cli, site, probe, tmp_path):
    """`--until-id` had no producer anywhere: after a full channel walk the
    registry knew nothing and the second run re-read the whole history.

    The walk has to reach a real end for the cursor to be written, and that is
    the half of the rule this test pins. It used to run with
    `--max-pages 1`, which stops on the page ceiling: a high-water mark written
    after a truncated walk makes the unread middle unreachable for ever, so that
    case now writes nothing. The withheld half is pinned in `test_tg_cli.py`,
    which owns the flag.

    `build_site` serves the same preview page for `/s/durov` and for
    `?before=523`, so the second page publishes the cursor it was given and the
    walk ends on `no_more_pages` -- a real end, reached by itself.
    """
    build_site(site, probe)
    walked = cli("history", "durov", "--write")
    assert walked.exit_code == 0
    assert walked.json["no_more_pages"] is True
    assert walked.json["stopped_early"] is None
    assert walked.json["cursor_written"] is True
    got = cli("registry", "get", "--username", "durov")
    assert got.json["source"]["max_id_seen"] == walked.json["max_id_seen"]


def test_registry_get_needs_a_username(cli):
    """The documented form answered `source: null`, which reads exactly like
    "that name is not in the registry" and is a different fact."""
    result = cli("registry", "get")
    assert result.exit_code == tg.EXIT_OPERATOR
    assert result.json["ok"] is False


def test_group_refuses_an_id_that_cannot_exist(cli, site, probe):
    """`found: 0` from an id below 1 is a silence about a question nobody asked.

    Message ids start at 1. A GET for id 0 answers `Post not found` like any
    empty id, so without this the command reports a well-formed nothing about a
    group it never asked anything about.
    """
    build_site(site, probe)
    result = cli("group", "tdlibchat", "--id", 0)
    assert result.exit_code == tg.EXIT_OPERATOR
    assert not site.requested


def test_a_name_that_cannot_be_a_username_is_refused_before_a_request(cli, site):
    for bad in ("durov/../../etc", "обычный_запрос", "a"):
        result = cli("verify", bad)
        assert result.exit_code == tg.EXIT_OPERATOR, bad
    assert not site.requested


def test_group_cannot_be_asked_to_guess_which_ids_exist(cli, site, probe):
    """There is no way to say "read this group" without saying which ids.

    There used to be: a head estimator, a catch-up creep and a blind scan, and
    they are gone because they could not do the job. Measured on `hanoi_chats`,
    200 requests bought 2 messages and 0 hits on the word the run was about --
    about one id in a hundred answers, so ten messages containing one word would
    have cost ~199 000 GETs against 29 327 ids that exist. `--id` is required,
    and a group is SEARCHED through `search`, never walked.
    """
    build_site(site, probe)
    with pytest.raises(SystemExit) as exit_code:      # argparse: --id is required
        cli("group", "tdlibchat")
    assert exit_code.value.code == tg.EXIT_USAGE
    assert not site.requested
    # And nothing anywhere still offers to work the ids out.
    for gone in ("--start-id", "--rss-hint", "--allow-blind-estimate",
                 "--catch-up-budget", "--max-misses"):
        assert gone not in tg.build_parser().format_help()


def test_selftest_runs_from_any_directory(cli, tmp_path, monkeypatch):
    """The first command SKILL.md tells you to run worked from the repo root and
    nowhere else."""
    monkeypatch.chdir(tmp_path)
    result = cli("selftest")
    assert result.exit_code == 0
    assert result.json["failed"] == []


# ==========================================================================
# The whole folder, judged by the gate that ships with the skill
# ==========================================================================
def test_note_and_accept_write_what_the_gate_asks_for(cli, site, probe, tmp_path):
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "биткойн",
                    "--topic", "t").json["run"])
    cli("--run", root, "search", "durov", "--query", "bitcoin")
    note = cli("note", root, "--agent", "telegram", "--text", "что нашлось: посты")
    assert note.exit_code == 0 and (root / "notes" / "telegram.md").exists()
    cli("report", root)
    accepted = cli("accept", root)
    assert accepted.exit_code == 0, accepted.stdout
    acceptance = json.loads((root / "acceptance.json").read_text(encoding="utf-8"))
    assert acceptance["formal"] == "PASS"
    assert json.loads((root / "run.json").read_text(encoding="utf-8"))["gate"]["exit"] == 0


def test_accept_refuses_a_run_with_no_notes(cli, site, probe, tmp_path):
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "q",
                    "--topic", "t").json["run"])
    cli("--run", root, "search", "durov", "--query", "bitcoin")
    cli("report", root)
    result = cli("accept", root)
    assert result.exit_code == tg.EXIT_NOT_ACCEPTED
    assert any("notes/" in e for e in result.json["errors"])


def test_a_finished_run_folder_passes_the_acceptance_gate(cli, site, probe, tmp_path):
    """The whole cycle, judged by the only gate that ships with this skill.

    A real run folder used to fail acceptance with six errors while `SKILL.md`
    claimed the notes went in the form a caller already swallows.

    This test used to shell out to an external folder checker this skill does
    not ship, and `pytest.fail()` when that script was not on disk -- so a copy
    of this skill installed on its own had a red suite out of the box, over a
    file no installer ever delivers. The demands did not move: `accept` has
    always applied them itself, in `tg._acceptance_findings`. What moved is who
    states them, and it is now this folder and this skill and nothing else.

    So every demand is spelled out below rather than delegated. Deleting one
    from `_acceptance_findings` has to fail here, which is the whole reason the
    external checker was worth reproducing in the first place.
    """
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "биткойн у Дурова",
                    "--topic", "t").json["run"])
    cli("--run", root, "search", "durov", "--query", "bitcoin")
    cli("queries", root, "start", "--query", "bitcoin")
    cli("queries", root, "record")
    cli("note", root, "--agent", "telegram", "--text", "An agent note. " * 80)
    cli("report", root)

    accepted = cli("accept", root)
    assert accepted.exit_code == 0, accepted.stdout
    assert accepted.json["formal"] == "PASS"
    assert accepted.json["errors"] == []

    for name in ("brief.md", "report.md", "run.json", "fetchlog.jsonl",
                 "acceptance.json"):
        path = root / name
        assert path.is_file() and path.stat().st_size > 0, name
    notes = sorted((root / "notes").glob("*.md"))
    assert notes, "a run with no notes has no material the report came from"
    assert all(note.stat().st_size for note in notes), notes
    records = [json.loads(line) for line
               in (root / "fetchlog.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    assert records and all(r.get("kind") == "fetch" for r in records)
    assert not (root / "raw").exists(), "a run writes notes/, never raw/"
    gate = json.loads((root / "run.json").read_text(encoding="utf-8"))["gate"]
    assert gate["exit"] == 0

    # And the verdict points at nothing this skill does not ship, which is the
    # regression this rewrite exists to prevent.
    assert "check_run" not in accepted.stdout, accepted.stdout
    assert "skills/research" not in accepted.stdout, accepted.stdout


# ==========================================================================
# Regression guards. One test per finding, each red before the fix it pins.
# ==========================================================================
import textwrap
import time

import config as config_module
from run import RunFolderError  # noqa: E402


def _spawn_run(tmp_path, name, body, *args):
    """A second real process writing into the same run folder."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(path), str(SCRIPTS), *[str(a) for a in args]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )


# --------------------------------------------------------------------------
# The run folder's appenders are cross-process safe
# --------------------------------------------------------------------------
def test_two_processes_writing_one_run_folder_lose_no_record(tmp_path):
    """With two real processes.

        lines: 553 parsed: 552 corrupt: 1 distinct: 552 expected 600
        lost records: 48

    measured on 2026-08-25 against the code as it stood. `log_fetch`,
    `write_posts` and `write_delta` all appended with a bare `open("a")`, which
    is seek-then-write in the Windows CRT and not atomic, so a branch
    fanning out over three channels lost posts and fetch-log lines with every
    survivor still well-formed JSON. `run.py` used none of the three primitives
    `config.py` exists to provide, while `registry.py` had been fixed with all
    of them.
    """
    root = tmp_path / "run"
    each = 120
    body = """
        import sys
        sys.path.insert(0, sys.argv[1])
        import tgweb
        from run import Brief, Run
        run = Run(sys.argv[2], Brief(question="q"))
        tag, n = sys.argv[3], int(sys.argv[4])
        for i in range(n):
            run.write_posts([{"username": tag, "id": i,
                              "url": "https://t.me/%s/%d" % (tag, i),
                              "text": "x" * 200}])
            run.write_delta([{"username": "%s%04d" % (tag, i), "type": "channel"}])
            run.log_fetch(tgweb.Response(
                url="https://t.me/%s/%d" % (tag, i), status=200,
                body="<html/>", bytes=200 + i,
                headers={"content-type": "text/html"}))
        print("done")
    """
    workers = [_spawn_run(tmp_path, f"w{i}.py", body, root, f"w{i}", each)
               for i in range(2)]
    for worker in workers:
        out, err = worker.communicate(timeout=300)
        assert worker.returncode == 0, err

    posts, corrupt = tg.read_jsonl(root / "posts.jsonl")
    assert corrupt == []
    assert len(posts) == 2 * each
    assert {(p["username"], p["id"]) for p in posts} == {
        (tag, i) for tag in ("w0", "w1") for i in range(each)}

    delta, delta_corrupt = tg.read_jsonl(root / "registry-delta.jsonl")
    assert delta_corrupt == []
    assert len(delta) == 2 * each

    # The fetch log is the third appender, and the one a citation rests on:
    # a page that WAS read with no `kind: "fetch"` record is a
    # page no quotation can be accepted from.
    fetches, fetch_corrupt = tg.read_jsonl(root / "fetchlog.jsonl")
    assert fetch_corrupt == []
    assert len(fetches) == 2 * each
    assert all(record["kind"] == "fetch" for record in fetches)
    assert len({record["url"] for record in fetches}) == 2 * each


def test_two_real_processes_finishing_one_run_add_up(tmp_path):
    """The same defect again, through the process boundary it lives at.

    Both processes attach at the same number, both spend, and both write. Before
    the repair the second write erased the first one's spend outright, and
    `_apply_request_ceiling` then seeded the next command with a ceiling that
    believed 10 more requests were available than the run had spent.
    """
    root = tmp_path / "run"
    seed = Run(root, Brief(question="q"))
    seed.count("requests", 15)
    seed.record_agent("newrun")
    seed.finish()

    body = """
        import sys, time
        sys.path.insert(0, sys.argv[1])
        from run import Run
        run = Run.attach(sys.argv[2])
        start = float(sys.argv[4])
        while time.time() < start:
            time.sleep(0.001)
        for _ in range(10):
            run.count("requests")
        run.record_agent(sys.argv[3])
        run.finish()
        print("done")
    """
    start = time.time() + 2.0
    workers = [_spawn_run(tmp_path, f"f{i}.py", body, root, name, f"{start:.3f}")
               for i, name in enumerate(("search", "group"))]
    for worker in workers:
        out, err = worker.communicate(timeout=300)
        assert worker.returncode == 0, err

    data = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert data["counters"]["requests"] == 35
    assert set(data["agents"]) == {"newrun", "search", "group"}


def test_two_processes_finishing_one_run_do_not_erase_each_others_spend(tmp_path):
    """`attach` read, the command mutated in memory, `finish` rewrote --
    unguarded, with no re-read. Two `search` processes that both attached at
    `requests: 15` and both spent 10 wrote 25, and the agent name the second one
    recorded replaced the first one's rather than joining it."""
    root = tmp_path / "run"
    first = Run(root, Brief(question="q"))
    first.count("requests", 15)
    first.record_agent("newrun")
    first.finish()

    a = Run.attach(root)
    b = Run.attach(root)
    a.count("requests", 10)
    a.record_agent("search")
    a.stop("429 from Telegram")
    b.count("requests", 10)
    b.record_agent("group")
    a.finish()
    b.finish()

    data = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert data["counters"]["requests"] == 35
    assert set(data["agents"]) == {"newrun", "search", "group"}
    assert data["stop_reasons"] == ["429 from Telegram"]

    # And calling finish twice from one process does not double-count it.
    a.finish()
    again = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert again["counters"]["requests"] == 35


def test_run_json_is_written_atomically_and_a_broken_one_is_repaired(tmp_path):
    """`write_text` truncates first, so an interrupt inside `finish` left
    half a file -- and every command that touched the run then refused with no
    repair, no `--force` and no rebuild, while `posts.jsonl`, `fetchlog.jsonl`
    and `notes/sources/` sat there complete."""
    root = tmp_path / "run"
    run = Run(root, Brief(question="q"))
    run.log_fetch(tgweb.Response(url="https://t.me/durov", status=200, body="<html/>",
                                 bytes=7, headers={"content-type": "text/html"}))
    run.write_posts([{"username": "durov", "id": 1, "url": "https://t.me/durov/1"}])
    run.write_delta([{"username": "durov", "type": "channel"}])
    run.finish()

    whole = (root / "run.json").read_text(encoding="utf-8")
    (root / "run.json").write_text(whole[:len(whole) // 2], encoding="utf-8")

    repaired = Run.attach(root)
    assert repaired.counters["requests"] == 1
    assert repaired.counters["posts"] == 1
    assert repaired.counters["sources"] == 1
    assert any("run.json" in reason for reason in repaired.stop_reasons)
    # The unreadable bytes are kept, never deleted: they are the only record of
    # what the interrupted write had got as far as saying.
    kept = list(root.glob("run.json.damaged-*"))
    assert len(kept) == 1
    assert kept[0].read_text(encoding="utf-8") == whole[:len(whole) // 2]

    data = repaired.finish()
    assert data["counters"]["requests"] == 1
    assert json.loads((root / "run.json").read_text(encoding="utf-8"))["schema"]


def test_run_json_is_read_the_way_that_does_not_block_a_replace(tmp_path,
                                                                monkeypatch):
    """`finish` replaces `run.json` now. On NTFS CPython's own `open()` does not
    pass FILE_SHARE_DELETE, so an ordinary reader blocks another process's
    `os.replace` over the same name -- which is exactly what `read_bytes_shared`
    exists to avoid, and what `read_run_json` used to ignore."""
    root = tmp_path / "run"
    run = Run(root, Brief(question="q"))
    run.count("requests", 3)
    run.finish()

    calls: list = []
    real = config_module.read_bytes_shared
    monkeypatch.setattr(config_module, "read_bytes_shared",
                        lambda path: (calls.append(path), real(path))[1])
    data = tg.read_run_json(root)
    assert data["counters"]["requests"] == 3
    assert calls and calls[0].name == "run.json"


# --------------------------------------------------------------------------
# Posts are de-duplicated once, here, and nowhere else
# --------------------------------------------------------------------------
def test_write_posts_dedupes_by_username_and_id_first_write_wins(tmp_path):
    """`search_channel` deduped within one
    query only, so `--query bitcoin --query btc` banked the same post twice, a
    `search` and a `history` over one channel banked it again, and a repeated
    command banked the lot once more. Measured: 40 lines, 23 distinct posts."""
    run = Run(tmp_path / "run", Brief(question="q"))
    first = run.write_posts([
        {"username": "durov", "id": 7, "url": "https://t.me/durov/7", "found_by": "bitcoin"},
        {"username": "durov", "id": 7, "url": "https://t.me/durov/7", "found_by": "btc"},
        {"username": "durov", "id": 8, "url": "https://t.me/durov/8", "found_by": "bitcoin"},
    ])
    assert (first.written, first.suppressed) == (2, 1)
    assert int(first) == 2                      # and the old int contract holds

    # A second command, a second call: the file does not grow.
    second = run.write_posts([
        {"username": "durov", "id": 7, "url": "https://t.me/durov/7", "found_by": "btc"},
        {"username": "durov", "id": 9, "url": "https://t.me/durov/9"},
    ])
    assert (second.written, second.suppressed) == (1, 1)

    rows, corrupt = tg.read_jsonl(run.posts_path)
    assert corrupt == []
    assert [r["id"] for r in rows] == [7, 8, 9]
    # `found_by` records the FIRST route that retrieved the post. That is the
    # documented meaning from now on.
    assert rows[0]["found_by"] == "bitcoin"
    assert run.counters["posts"] == 3
    assert run.counters["posts_duplicate"] == 2

    # Different channels, same id, are different posts.
    run.write_posts([{"username": "tdlibchat", "id": 7, "url": "https://t.me/tdlibchat/7"}])
    rows, _ = tg.read_jsonl(run.posts_path)
    assert len(rows) == 4

    # A row with no id is keyed on its permalink; a row with neither is never
    # collapsed into anything, because losing evidence to save a line is worse.
    from run import post_key

    assert post_key({"username": "d", "url": "u"}) == ("url", "u")
    assert post_key({"username": "d"}) is None
    run.write_posts([{"username": "d"}, {"username": "d"}])
    rows, _ = tg.read_jsonl(run.posts_path)
    assert len(rows) == 6


def test_the_dedup_holds_across_processes(tmp_path):
    """The duplicates that matter come from DIFFERENT processes: `search` in
    one, `history` in the next, an hour apart. A per-instance set would not have
    seen any of them."""
    root = tmp_path / "run"
    Run(root, Brief(question="q")).write_posts(
        [{"username": "durov", "id": i, "url": f"https://t.me/durov/{i}"}
         for i in range(5)])
    body = """
        import sys
        sys.path.insert(0, sys.argv[1])
        from run import Brief, Run
        run = Run(sys.argv[2], Brief(question="q"))
        res = run.write_posts([{"username": "durov", "id": i,
                                "url": "https://t.me/durov/%d" % i}
                               for i in range(3, 8)])
        print(res.written, res.suppressed)
    """
    worker = _spawn_run(tmp_path, "second.py", body, root)
    out, err = worker.communicate(timeout=120)
    assert worker.returncode == 0, err
    assert out.split() == ["3", "2"]

    rows, _ = tg.read_jsonl(root / "posts.jsonl")
    assert sorted(r["id"] for r in rows) == list(range(8))


def test_the_report_and_run_json_agree_about_how_many_posts_there_were(tmp_path):
    """`report.md` said `Posts: 40` about 23 posts -- 74 % too high,
    in the document a person reads and in `acceptance.json` -- and the
    per-source column repeated the same wrong number."""
    run = _run(tmp_path)
    posts = [{"username": "durov", "id": 1, "url": "https://t.me/durov/1"},
             {"username": "durov", "id": 1, "url": "https://t.me/durov/1"},
             {"username": "durov", "id": 2, "url": "https://t.me/durov/2"},
             {"username": "tdlibchat", "id": 5, "url": "https://t.me/tdlibchat/5"}]
    sources = [{"username": "durov", "type": "channel", "members": 1, "found_via": "web"},
               {"username": "tdlibchat", "type": "group", "members": 2, "found_via": "web"}]
    run.count("posts", 3)
    text = report_skeleton(run, discovery=None, query_log=None,
                           sources_used=sources, posts=posts)

    assert "Posts: 3 " in text or "Posts: 3." in text
    assert "repeated" in text                   # the duplicate lines are named
    assert "a disagreement" not in text         # run.json and the report agree
    for line in text.splitlines():
        if line.startswith("| [durov]"):
            assert line.rstrip().endswith("| 2 |"), line
        if line.startswith("| [tdlibchat]"):
            assert line.rstrip().endswith("| 1 |"), line


def test_a_search_run_twice_does_not_double_its_posts(cli, site, probe, tmp_path):
    """The same thing through the whole CLI, which is where it was measured."""
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "дубли",
                    "--topic", "t").json["run"])
    cli("--run", root, "search", "durov", "--query", "bitcoin")
    after_one = (root / "posts.jsonl").read_text(encoding="utf-8")
    cli("--run", root, "search", "durov", "--query", "bitcoin")
    assert (root / "posts.jsonl").read_text(encoding="utf-8") == after_one

    cli("report", root)
    report = (root / "report.md").read_text(encoding="utf-8")
    lines = [ln for ln in after_one.splitlines() if ln.strip()]
    assert f"Posts: {len(lines)}." in report


# --------------------------------------------------------------------------
# `--brief` is a decision too
# --------------------------------------------------------------------------
def test_a_brief_file_gets_the_ceilings_its_depth_names(tmp_path, monkeypatch):
    """`Brief.from_file` constructed the dataclass directly, so a brief
    saying `"depth": "deep"` fell back to the FIELD defaults -- which are
    `normal`'s row -- and ran on 3 rounds and 400 requests while `brief.md`
    printed the contradiction side by side. SKILL.md names `--brief` as THE
    entry point when the skill is called from inside another agent's pass."""
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)
    path = tmp_path / "brief.json"
    path.write_text(json.dumps({"question": "q", "topic": "probe", "depth": "deep",
                                "caller": "agent", "lang": "ru"}), encoding="utf-8")
    brief = Brief.from_file(path)
    assert (brief.max_rounds, brief.min_new_posts, brief.max_requests) == (5, 2, 800)

    path.write_text(json.dumps({"question": "q", "depth": "quick"}), encoding="utf-8")
    quick = Brief.from_file(path)
    assert (quick.max_rounds, quick.min_new_posts, quick.max_requests) == (1, 3, 133)

    # A ceiling the file states explicitly still wins -- which is why re-reading
    # a run's own `brief.json`, where `newrun` wrote all three, is unchanged.
    path.write_text(json.dumps({"question": "q", "depth": "deep", "max_rounds": 9}),
                    encoding="utf-8")
    explicit = Brief.from_file(path)
    assert (explicit.max_rounds, explicit.max_requests) == (9, 800)

    # And TELEGRAM_RESEARCH_CONFIG finally reaches this path.
    override = tmp_path / "cfg.json"
    override.write_text(json.dumps({"budgets": {"max_rounds": 6,
                                                "min_new_posts_per_round": 10,
                                                "max_requests_per_run": 90}}),
                        encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(override))
    path.write_text(json.dumps({"question": "q", "depth": "deep"}), encoding="utf-8")
    configured = Brief.from_file(path)
    assert (configured.max_rounds, configured.min_new_posts,
            configured.max_requests) == (8, 9, 180)


def test_newrun_with_a_brief_file_writes_the_ceilings_its_depth_names(
        cli, tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)
    path = tmp_path / "brief.json"
    path.write_text(json.dumps({"question": "глубокий прогон", "topic": "probe",
                                "depth": "deep", "caller": "agent"}),
                    encoding="utf-8")
    result = cli("--root", tmp_path, "newrun", "--brief", path)
    assert result.exit_code == 0
    assert result.json["brief"]["max_rounds"] == 5
    assert result.json["brief"]["max_requests"] == 800
    text = (Path(result.json["run"]) / "brief.md").read_text(encoding="utf-8")
    assert "| round ceiling | 5 |" in text
    assert "| request ceiling | 800 |" in text
    assert "| new-post floor | 2 |" in text


def test_every_number_in_the_depth_table_is_the_one_skill_md_prints():
    """A4. `deep` could be rewritten to 4 rounds, floor 3 and 401 requests --
    three of the six numbers wrong at once -- and the suite stayed green. Only
    `quick`'s round ceiling was pinned, and only indirectly."""
    rows = {depth: Brief.for_depth(depth, question="q") for depth in
            ("quick", "normal", "deep")}
    table = {d: (b.max_rounds, b.min_new_posts, b.max_requests)
             for d, b in rows.items()}
    assert table == {"quick": (1, 3, 133), "normal": (3, 3, 400), "deep": (5, 2, 800)}


# --------------------------------------------------------------------------
# A note never destroys the note before it
# --------------------------------------------------------------------------
def test_a_second_note_from_one_agent_keeps_the_first(tmp_path):
    """`write_text`, not append. An agent that notes as it goes kept
    only the last one, and the acceptance gate still passed because it counts
    non-empty notes."""
    run = _run(tmp_path)
    run.write_note("telegram", "FIRST observation")
    path = run.write_note("telegram", "SECOND observation")
    text = path.read_text(encoding="utf-8")
    assert "FIRST observation" in text
    assert "SECOND observation" in text
    assert text.index("FIRST") < text.index("SECOND")
    assert text.endswith("\n")                  # `cat` no longer runs into the next line

    # A different agent still gets its own file.
    other = run.write_note("querycraft", "THIRD")
    assert other != path
    assert "FIRST" not in other.read_text(encoding="utf-8")

    # And the credential redaction still applies to every note, not only the first.
    run.write_note("telegram", f"key TELEGRAM_SESSION={FAKE_SESSION}")
    assert FAKE_SESSION not in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Where a run folder lands
# --------------------------------------------------------------------------
def test_a_run_lands_in_telegram_runs_under_the_root(tmp_path):
    """A5. The documented location was asserted nowhere: it could be redirected
    and the suite stayed green.

    There is one shape now, `<root>/telegram-runs/<date>-<slug>/`. The two it
    replaced were a topic-shaped path and a track-shaped one, both belonging to
    the layout of the repository this skill was written inside -- neither of
    which exists on the machine of anybody who installs it.
    """
    first = Run.open(Brief(question="аренда", topic="relocation"), root=tmp_path)
    assert first.root.parent == tmp_path / "telegram-runs"
    assert first.root.name.endswith("-аренда")

    # The topic stays a field of the brief -- the registry sorts by it -- and
    # is no longer part of the path, so two topics land side by side.
    second = Run.open(Brief(question="аренда", topic="marketing"), root=tmp_path)
    assert second.root.parent == tmp_path / "telegram-runs"
    assert second.root != first.root
    written = json.loads((second.root / "brief.json").read_text(encoding="utf-8"))
    assert written["topic"] == "marketing"
    for gone in ("store", "docs"):
        assert not (tmp_path / gone).exists(), f"{gone}/ is back"


def test_the_topic_cannot_move_the_run_folder_any_more(tmp_path):
    """Only `brief.question` went through `slugify` and the topic was
    interpolated into the path raw, so `--topic "a/b"` split the path in two
    and `--topic "../.."` left `--root` altogether -- measured, a run folder
    created three levels above the root it was given.

    The topic is out of the path entirely now, so the only caller text that
    still becomes a directory is the question, and it goes through both guards.
    """
    from run import path_component

    escaped = Run.open(Brief(question="../../escape", topic="../../escaped"),
                       root=tmp_path)
    assert tmp_path.resolve() in escaped.root.resolve().parents
    assert ".." not in escaped.root.parts
    assert escaped.root.parent == tmp_path / "telegram-runs"

    nested = Run.open(Brief(question="nested", topic="marketing/telegram"),
                      root=tmp_path)
    assert nested.root.parent == tmp_path / "telegram-runs"

    # `--track` went with the folder shape it addressed; nothing accepts it.
    with pytest.raises(TypeError):
        Run.open(Brief(question="x"), root=tmp_path, track="S1")

    # Case is kept, unlike slugify: `T2` and `t2` are different directories.
    assert path_component("T2", default="general") == "T2"
    assert path_component("  ", default="general") == "general"
    assert path_component("a b.", default="general") == "a b"


def test_the_run_folder_date_follows_the_configured_timezone(tmp_path, monkeypatch):
    """`LOCAL_TZ` was a fixed `timezone(timedelta(...))` in `run.py` and in
    `registry.py`: right on one machine and an hour out on every other, with
    run-folder names, `first_seen` and `last_checked` all riding on it."""
    from datetime import datetime, timedelta, timezone

    for offset, label in (("+14:00", 14), ("-11:00", -11)):
        monkeypatch.setenv(config_module.ENV_TZ, offset)
        run = Run.open(Brief(question=f"tz{label}", topic="t"), root=tmp_path)
        expected = datetime.now(timezone(timedelta(hours=label))).date().isoformat()
        assert run.root.name.startswith(expected), run.root.name
        assert json.loads((run.root / "run.json").read_text(encoding="utf-8"))[
            "started"].endswith(offset)


# --------------------------------------------------------------------------
# Tests that were true of themselves rather than of the code
# --------------------------------------------------------------------------
def test_run_json_counters_are_the_numbers_the_fixture_really_serves(
        cli, site, probe, tmp_path):
    """A15. The neighbouring test compares `run.json` against the run's own
    files, so both being wrong passes. The site fixture serves a fixed set of
    pages, so the absolute numbers are knowable and are asserted here."""
    build_site(site, probe)
    root = Path(cli("--root", tmp_path, "newrun", "--question", "абсолютные",
                    "--topic", "t").json["run"])
    search = cli("--run", root, "search", "durov", "--query", "bitcoin")
    history = cli("--run", root, "history", "durov", "--max-pages", 1)

    assert search.json["results"][0]["found"] == 7
    assert len(history.json["messages"]) == 20

    data = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert data["counters"] == {"requests": 2, "posts": 27}
    assert data["agents"] == ["newrun", "search", "history"]


def test_a_query_log_that_cannot_be_read_is_its_own_state(tmp_path):
    """There were two states where there are three, and `report` had
    to choose between two wrong answers on a run with a corrupt `queries.json`:
    say the round log was never kept about a run that DID keep one, or refuse
    to report at all on a run that had already spent its whole request
    budget."""
    run = _run(tmp_path)
    run.queries_path.write_text('{"rounds": [{"n', encoding="utf-8")

    text = report_skeleton(run, discovery=None, query_log=None,
                           sources_used=[], posts=[])
    assert "could not be read" in text
    assert "queries.json" in text
    assert "never kept" not in text                    # the false sentence is gone
    assert "Not one word could be mined" not in text   # and so is the other one

    # The caller may quote the real reason; it is not required, because a
    # `queries.json` on disk with no log loaded can only mean the load failed.
    detailed = report_skeleton(run, discovery=None, query_log=None,
                               sources_used=[], posts=[],
                               query_log_error="Expecting ',' delimiter: line 1")
    assert "Expecting ',' delimiter: line 1" in detailed

    # The two states that already existed still say what they said.
    run.queries_path.unlink()
    absent = report_skeleton(run, discovery=None, query_log=None,
                             sources_used=[], posts=[])
    assert "The round log was never kept" in absent
    assert "could not be read" not in absent

    empty = report_skeleton(run, discovery=None, query_log=querycraft.QueryLog(),
                            sources_used=[], posts=[])
    assert "Not one word could be mined" in empty
    assert "could not be read" not in empty


# --------------------------------------------------------------------------
# The run folder half of the same regression set
# --------------------------------------------------------------------------
def test_a_run_folder_is_one_only_if_it_says_so(tmp_path):
    """The old check asked "non-empty string, exists, is a directory" and
    none of those is "is this a run?" -- so `report <any directory>` mkdir'd
    `notes/sources` inside it, wrote `report.md` and `run.json`, and returned
    `ok: true` with exit 0.

    The marker is the one this code already writes: a `run.json` that parses and
    declares `schema: telegram-research.run/1`. `Run.open` writes it before `newrun`
    returns.
    """
    stranger = tmp_path / "notarun"
    stranger.mkdir()
    (stranger / "my-important-file.txt").write_text("hello", encoding="utf-8")

    for bad in ("", "   ", tmp_path / "no-such-thing", stranger):
        with pytest.raises(run_module.NotARunFolder):
            run_module.require_run_folder(bad)
    # And nothing was created on the way to any of those refusals.
    assert [p.name for p in stranger.iterdir()] == ["my-important-file.txt"]
    assert not (tmp_path / "no-such-thing").exists()

    a_file = tmp_path / "afile.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(run_module.NotARunFolder):
        run_module.require_run_folder(a_file)

    real = Run.open(Brief(question="настоящий прогон", topic="t"),
                    root=tmp_path)
    assert run_module.require_run_folder(real.root) == real.root
    assert run_module.require_run_folder(str(real.root)) == real.root

    # A folder carrying somebody else's run.json is not this skill's run folder.
    other = tmp_path / "other"
    other.mkdir()
    (other / "run.json").write_text('{"schema": "someone.else/9"}', encoding="utf-8")
    with pytest.raises(run_module.NotARunFolder):
        run_module.require_run_folder(other)

    # A run.json nobody can parse is refused here too, and the refusal says how
    # to get the folder repaired rather than leaving it to be guessed.
    damaged = real.root / "run.json"
    damaged.write_text(damaged.read_text(encoding="utf-8")[:40], encoding="utf-8")
    with pytest.raises(run_module.NotARunFolder) as caught:
        run_module.require_run_folder(real.root)
    assert "tg.py note" in str(caught.value)
    # ...and that repair path really works: `attach` moves the bytes aside and
    # rebuilds, after which the folder answers for itself again.
    Run.attach(real.root).finish()
    assert run_module.require_run_folder(real.root) == real.root


def test_the_round_ledger_is_written_atomically_and_under_a_guard(tmp_path, monkeypatch):
    """`queries.md` was a bare `write_text` and `queries.json` went
    through `QueryLog.save`, also a bare `write_text`: no guard, no atomic
    replace, no `_keep_damaged` -- while `run.json` had all three. Measured
    before the repair, with another process holding the write guard on
    `queries.json`:

        write_queries wrote THROUGH a held guard: True True

    `queries.json` is the only record of how many rounds a run has used, so an
    interrupt in the middle of any `queries` command destroyed the round
    ceiling, the yield floor and the drift ban together.
    """
    run = _run(tmp_path)
    log = querycraft.QueryLog(max_rounds=3, min_new_posts=3)
    log.start_round(["аренда"])
    run.write_queries(log)
    before = run.queries_path.read_text(encoding="utf-8")

    log.start_round(["ипотека"])                   # round 2, not yet on disk
    # The production wait is 20 s, which is right against another process and
    # wrong in a suite that runs on every change: what is under test is that the
    # guard is TAKEN, not how long it is waited for.
    real_guard = config_module.file_guard
    monkeypatch.setattr(
        config_module, "file_guard",
        lambda path, **kw: real_guard(path, **{**kw, "timeout": 0.3}))
    for target, label in ((run.queries_path, "queries.json"),
                          (run.root / "queries.md", "queries.md")):
        held = real_guard(target, label=label)
        held.acquire()
        try:
            with pytest.raises(config_module.GuardBusy):
                run.write_queries(log)
        finally:
            held.release()
    # Refused, not half-written: the file on disk is the one from before.
    assert run.queries_path.read_text(encoding="utf-8") == before
    assert querycraft.QueryLog.load(run.queries_path).rounds[-1].number == 1

    # The replace is atomic, so the reader never sees a truncated file: the
    # temp file carries the pid and a random token and is gone afterwards.
    run.write_queries(log)
    assert querycraft.QueryLog.load(run.queries_path).rounds[-1].number == 2
    leftovers = [p.name for p in run.root.iterdir()
                 if ".tmp" in p.name or ".staging" in p.name]
    assert leftovers == [], leftovers


def test_a_killed_command_s_spend_is_recovered_from_the_folder_s_own_files(tmp_path):
    """`log_fetch` appends to `fetchlog.jsonl` at once and only bumps an
    in-memory counter; `run.json` is written by `finish()`. `persist_active_run`
    covers every exception exit and Ctrl-C -- and nothing covers `taskkill /F`,
    a closed console or a power loss. Measured on a `verify` killed after 6 s:

        fetchlog.jsonl network acts on disk : 2
        run.json counters                   : {'requests': 0}
        run.json parses fine                : True

    and the next command re-armed the run-level brake from zero. The rebuild
    that fixes it existed and was reachable only through a `run.json` that could
    NOT be parsed -- the one case `atomic_write_text` exists to make impossible.
    """
    root = tmp_path / "run"
    run = Run(root, Brief(question="q"))
    run.finish()                                   # a healthy, current run.json

    # Exactly what a hard kill leaves: the evidence is on disk, the summary is
    # stale, and the summary parses perfectly.
    (root / "fetchlog.jsonl").write_text(
        "\n".join(json.dumps({"kind": "fetch", "url": f"https://t.me/x/{i}",
                              "status": 200}) for i in range(1, 3)) + "\n",
        encoding="utf-8")
    (root / "posts.jsonl").write_text(
        json.dumps({"username": "x", "id": 1, "url": "https://t.me/x/1"}) + "\n",
        encoding="utf-8")
    stale = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert stale["counters"] == {}

    recovered = Run.attach(root)
    assert recovered.counters["requests"] == 2
    assert recovered.counters["posts"] == 1
    assert any("behind this folder" in reason for reason in recovered.stop_reasons)

    # It survives the round trip, so the NEXT process arms its ceiling from the
    # real number rather than from zero.
    recovered.finish()
    on_disk = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert on_disk["counters"]["requests"] == 2
    assert Run.attach(root).counters["requests"] == 2

    # And this process's own spend is still added on top, not swallowed by the
    # correction: the recovery is a delta like everything else `finish` merges.
    second = Run.attach(root)
    second.count("requests", 3)
    assert second.finish()["counters"]["requests"] == 5

    # A run whose counters are already current is left alone -- no phantom
    # stop reason, no inflation.
    quiet = Run.attach(root)
    assert quiet.counters["requests"] == 5
    assert not any("behind this folder" in reason for reason in
                   quiet.stop_reasons[len(second.stop_reasons):])
    assert quiet.finish()["counters"]["requests"] == 5


def test_a_recovered_counter_never_walks_backwards(tmp_path):
    """The direction of the count. The safe side is to over-count what left the machine,
    never to under-count it: a fetch log that is SHORTER than `run.json` says --
    a log somebody trimmed, a folder half-copied -- must not lower the spend and
    hand the next command a fresh budget."""
    root = tmp_path / "run"
    run = Run(root, Brief(question="q"))
    run.count("requests", 40)
    run.finish()
    (root / "fetchlog.jsonl").write_text(
        json.dumps({"kind": "fetch", "url": "https://t.me/x/1"}) + "\n",
        encoding="utf-8")

    attached = Run.attach(root)
    assert attached.counters["requests"] == 40
    assert attached.finish()["counters"]["requests"] == 40


def test_run_open_anchors_a_relative_root_before_it_creates_anything(
    tmp_path, monkeypatch
):
    """`tg.py` hands `Run.open` an absolute root now, so this is the guard for
    every other caller: a relative base is anchored before the folder is made,
    and an absolute one is left byte-identical rather than resolved through
    symlinks and short names."""
    monkeypatch.chdir(tmp_path)
    run = Run.open(Brief(question="относительный корень", topic="t"),
                   root=Path("some-project"))
    assert run.root.is_absolute(), run.root
    assert run.root.is_relative_to(Path(tmp_path).resolve())
    assert run.root.is_dir()
    data = json.loads((run.root / "run.json").read_text(encoding="utf-8"))
    assert Path(data["root"]).is_absolute()

    # An absolute base is passed through unchanged.
    plain = Run.open(Brief(question="абсолютный корень", topic="t"),
                     root=tmp_path)
    assert plain.root.is_relative_to(tmp_path)
