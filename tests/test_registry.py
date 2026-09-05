"""Tests for scripts/registry.py: the append-only source log, admission, topics.

No network, no installs. Every file lives under tmp_path.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

SCRIPTS = (Path(__file__).resolve().parent.parent
           / "skills" / "telegram-research" / "scripts")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pytest

from registry import (
    AdmissionRules,
    Registry,
    Source,
    TopicClassifier,
    judge,
    normalise,
)


# --------------------------------------------------------------------------
# append / load: last line wins, but null never erases a real value
# --------------------------------------------------------------------------
def test_load_newest_line_wins(tmp_path):
    reg = Registry(tmp_path / "sources.jsonl")
    reg.append(Source(username="durov", members=100))
    reg.append(Source(username="durov", members=200))
    rec = reg.get("durov")
    assert rec["members"] == 200


def test_load_newer_null_does_not_erase_older_value(tmp_path):
    """A cheap landing-page check (no max_id_seen) must not wipe the value a
    full read paid for. This merge rule is load-bearing -- see registry.py's
    `_merge` docstring."""
    reg = Registry(tmp_path / "sources.jsonl")
    reg.append(Source(username="tdlibchat", type="group", max_id_seen=10000))
    # A later, cheaper check writes a record with no max_id_seen at all.
    reg.append(Source(username="tdlibchat", type="group", members=16674))
    rec = reg.get("tdlibchat")
    assert rec["max_id_seen"] == 10000
    assert rec["members"] == 16674


def test_usernames_matched_case_insensitively_and_at_stripped(tmp_path):
    reg = Registry(tmp_path / "sources.jsonl")
    reg.append(Source(username="@Durov", members=5))
    assert reg.get("durov") is not None
    assert reg.get("DUROV")["members"] == 5
    assert reg.get("@durov")["members"] == 5
    # the stored username itself has the @ stripped
    assert reg.get("durov")["username"] == "Durov"


def test_corrupt_line_does_not_break_load_and_is_named(tmp_path):
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="good1", members=1))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    reg.append(Source(username="good2", members=2))

    loaded = reg.load()
    assert "good1" in loaded
    assert "good2" in loaded
    assert reg.corrupt_lines() == [2]


def test_compact_collapses_to_one_line_per_username_sorted(tmp_path):
    reg = Registry(tmp_path / "sources.jsonl")
    reg.append(Source(username="zebra", members=1))
    reg.append(Source(username="alpha", members=1))
    reg.append(Source(username="alpha", members=2))
    kept = reg.compact()
    assert kept == 2

    lines = reg.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    usernames = [json.loads(l)["username"] for l in lines]
    assert usernames == sorted(usernames)
    assert json.loads(lines[usernames.index("alpha")])["members"] == 2


def test_compact_leaves_the_original_intact_when_the_replace_never_happens(tmp_path):
    """A compaction that fails must leave the log exactly as it was.

    The test this replaced never called `compact()`. It wrote a sibling file
    itself, asserted `Path.with_suffix(".compacting") != reg.path` -- a property
    of `pathlib` -- and would have passed unchanged if `compact()` had truncated
    the file in place. It was also stale on its own premise: the `.compacting`
    name was removed when two concurrent compactions crashed on it, and
    `compact()` goes through `config.atomic_write_text` now.

    So: make the replace fail for real, and check the bytes.
    """
    reg = Registry(tmp_path / "sources.jsonl")
    reg.append(Source(username="alpha", type="channel", members=1))
    reg.append(Source(username="alpha", type="channel", members=2))
    before = reg.path.read_bytes()

    def refuse(path, text, **kwargs):
        raise configmod.AtomicWriteFailed("the destination is held open")

    original = configmod.atomic_write_text
    configmod.atomic_write_text = refuse
    try:
        with pytest.raises(configmod.AtomicWriteFailed):
            reg.compact()
    finally:
        configmod.atomic_write_text = original

    assert reg.path.read_bytes() == before          # byte for byte, not line count
    assert len(before.decode("utf-8").strip().splitlines()) == 2
    # No temp file was left behind in the state directory.
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []
    # And the log still reads back correctly.
    assert reg.load()["alpha"]["members"] == 2


# --------------------------------------------------------------------------
# judge(): admission, with the reason text asserted, not only the boolean
# --------------------------------------------------------------------------
def test_judge_admits_a_good_channel():
    rules = AdmissionRules()
    card = {"username": "durov", "type": "channel", "members": 11110268, "exists": True}
    verdict = judge(card, rules)
    assert verdict.admit is True
    assert verdict.action == "insert"
    assert "admitted" in verdict.reason


def test_judge_rejects_invalid_username():
    rules = AdmissionRules()
    card = {"username": "a!b", "type": "channel", "members": 1000}
    verdict = judge(card, rules)
    assert verdict.admit is False
    assert "not a valid Telegram username" in verdict.reason


def test_judge_rejects_nonexistent_name():
    rules = AdmissionRules()
    card = {"username": "zzqwxnonexist", "type": None, "exists": False}
    verdict = judge(card, rules)
    assert verdict.admit is False
    assert "no such name" in verdict.reason


def test_judge_rejects_unknown_type():
    rules = AdmissionRules()
    card = {"username": "somename", "type": None, "members": 1000, "exists": True}
    verdict = judge(card, rules)
    assert verdict.admit is False
    assert "type is unknown" in verdict.reason


def test_judge_rejects_member_count_below_floor():
    rules = AdmissionRules(min_channel_members=100)
    card = {"username": "smallchan", "type": "channel", "members": 5, "exists": True}
    verdict = judge(card, rules)
    assert verdict.admit is False
    assert "below the floor" in verdict.reason
    assert "100" in verdict.reason


def test_judge_rejects_banned_name():
    rules = AdmissionRules(banned_usernames=("spamchannel",))
    card = {"username": "SpamChannel", "type": "channel", "members": 1000, "exists": True}
    verdict = judge(card, rules)
    assert verdict.admit is False
    assert "ban list" in verdict.reason


# --------------------------------------------------------------------------
# TopicClassifier: multi-label, evidence, normalise() strips ad boilerplate
# --------------------------------------------------------------------------
def test_topic_classifier_is_multi_label():
    vocab = {
        "crypto": ["bitcoin", "crypto"],
        "news": ["news", "breaking"],
    }
    clf = TopicClassifier(vocab)
    topics, evidence = clf.classify("Breaking crypto news today: bitcoin surges")
    assert set(topics) == {"crypto", "news"}
    assert "bitcoin" in evidence["crypto"] or "crypto" in evidence["crypto"]
    assert "news" in evidence["news"] or "breaking" in evidence["news"]


def test_topic_classifier_returns_matched_keyword_as_evidence():
    vocab = {"tech": ["python", "rust"]}
    clf = TopicClassifier(vocab)
    topics, evidence = clf.classify("We write a lot of python here")
    assert topics == ["tech"]
    assert evidence["tech"] == ["python"]


def test_normalise_strips_urls_handles_and_ad_boilerplate():
    text = "Check https://example.com/promo and contact @adseller123 по вопросам рекламы пишите"
    out = normalise(text)
    assert "http" not in out
    assert "@adseller123" not in out
    assert "рекламы" not in out


def test_ad_heavy_card_not_classified_as_advertising():
    """The exact failure normalise() exists to prevent: an ad-heavy channel
    card should not be classified as being 'about advertising'."""
    vocab = {"advertising": ["ad", "advertisement", "ads"]}
    clf = TopicClassifier(vocab)
    card_text = (
        "Best deals daily! Contact @salesbot for ads. По вопросам рекламы "
        "пишите в личку. https://t.me/joinnow"
    )
    topics, _ = clf.classify(card_text)
    assert topics == []


# --------------------------------------------------------------------------
# Scale test: 10 000 lines
# --------------------------------------------------------------------------
def test_registry_scales_to_ten_thousand_lines(tmp_path):
    reg = Registry(tmp_path / "sources.jsonl")
    n = 10_000
    distinct = n // 2  # each username appended twice, newest value should win

    t0 = time.perf_counter()
    with reg.path.open("a", encoding="utf-8", newline="\n") as fh:
        for i in range(n):
            uname = f"user{i % distinct}"
            rec = {
                "username": uname,
                "type": "channel",
                "members": i,
                "first_seen": "2026-01-01",
                "last_checked": "2026-01-01",
            }
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    write_elapsed = time.perf_counter() - t0

    t1 = time.perf_counter()
    loaded = reg.load()
    load_elapsed = time.perf_counter() - t1
    assert len(loaded) == distinct

    t2 = time.perf_counter()
    reg.append(Source(username="freshuser", members=1))
    append_elapsed = time.perf_counter() - t2

    total = write_elapsed + load_elapsed + append_elapsed
    print(
        f"\n[10k registry] write={write_elapsed:.3f}s load={load_elapsed:.3f}s "
        f"append={append_elapsed:.3f}s total={total:.3f}s distinct={len(loaded)}"
    )
    # Generous ceiling: this reports degradation, it does not chase a tight bound.
    assert load_elapsed < 5.0
    assert append_elapsed < 1.0

    collapsed = reg.compact()
    assert collapsed == distinct + 1  # + the freshuser appended above


def test_classifier_ignores_documentation_keys_and_string_values():
    """A prose line in the vocabulary file must not become a topic.

    Measured on the first live run: `topics.json` ships a `_README` key whose
    value is a sentence. Iterating a string yields its CHARACTERS, and a
    single-character keyword matches almost every source, so `_README` attached
    itself to three of the four sources the run admitted.
    """
    classifier = TopicClassifier({
        "_README": "this is documentation, not a topic",
        "housing": ["аренда", "rent"],
        "broken": "a bare string where a list belongs",
    })

    assert set(classifier.vocabulary) == {"housing"}
    assert classifier.skipped == ["broken"]

    topics, evidence = classifier.classify("Квартира в аренду", None)
    assert topics == []          # 'аренда' is not a substring of 'аренду'
    assert "_README" not in topics

    # Substring matching over an inflected language wants stems, not dictionary
    # forms. This is why topics.json says so in its own README.
    stemmed = TopicClassifier({"housing": ["аренд", "rent"]})
    topics, evidence = stemmed.classify("Квартира в аренду", None)
    assert topics == ["housing"]
    assert evidence["housing"] == ["аренд"]


# ==========================================================================
# Regression guards. Two real processes, a killed writer, a Notepad
# round trip and a cp1251 byte. Every test below fails against the code as it
# stood before the repair.
# ==========================================================================
import subprocess
import textwrap

import config as configmod
from registry import RegistryDamaged, _merge

SCRIPTS_DIR = str(SCRIPTS)


def _spawn(tmp_path, name, body, *args):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(path), SCRIPTS_DIR, *[str(a) for a in args]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


# --------------------------------------------------------------------------
# Concurrent appends must not lose whole records
# --------------------------------------------------------------------------
def test_two_processes_appending_lose_no_record(tmp_path):
    """With two real processes and realistic record sizes.

        record size ~307 B, 2 x 300 appends -> physical lines 579 (expected 600),
        load() 578, corrupt [17]
            pid 27504: 290/300 lost [0, 1, 4, 121, 153, 198, 206, 228]
            pid 28936: 288/300 lost [2, 10, 80, 81, 124, 141, 151, 203]

    22 sources gone, one line genuinely spliced, and every survivor well-formed
    JSON so `corrupt_lines()` reported almost nothing. `open(..., "a")` is
    seek-then-write in the Windows CRT and the pair is not atomic; the module
    docstring's claim that "appending is atomic" was simply untrue here.
    """
    path = tmp_path / "sources.jsonl"
    each = 80
    body = """
        import sys
        sys.path.insert(0, sys.argv[1])
        import registry
        reg = registry.Registry(sys.argv[2])
        tag, n = sys.argv[3], int(sys.argv[4])
        for i in range(n):
            reg.append(registry.Source(
                username="%s_%04d" % (tag, i),
                type="channel",
                title="Source number %d for %s" % (i, tag),
                description="x" * 200,
                members=1000 + i,
                found_via="catalog",
            ))
        print("done")
    """
    workers = [_spawn(tmp_path, "a%d.py" % i, body, path, "w%d" % i, each)
               for i in range(2)]
    for w in workers:
        out, err = w.communicate(timeout=300)
        assert w.returncode == 0, err

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2 * each
    loaded = reg_load = Registry(path).load()
    assert len(reg_load) == 2 * each
    assert Registry(path).corrupt_lines() == []
    for tag in ("w0", "w1"):
        assert sum(1 for k in loaded if k.startswith(tag)) == each


# --------------------------------------------------------------------------
# A crash mid-append must not take the NEXT record with it
# --------------------------------------------------------------------------
def test_a_truncated_last_line_does_not_destroy_the_next_append(tmp_path):
    """Measured before the repair:

        {"username": "halfwritten", "type": "grou{"first_seen": ..., "username": "thirdone"}
        usernames recovered: ['goodone']
        >>> 'thirdone' was written by a healthy append and is GONE: True

    The docstring's promise was "a crash mid-write costs at most the line being
    written". It cost the following line as well, because a line with no
    terminator swallows whatever is appended after it.
    """
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="goodone", type="channel", members=500))
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"username": "halfwritten", "type": "grou')      # killed here

    reg.append(Source(username="thirdone", type="channel", members=700))

    loaded = reg.load()
    assert "goodone" in loaded
    assert "thirdone" in loaded            # the healthy record survived
    assert reg.corrupt_lines() == [2]      # exactly one line was lost, not two


def test_compact_refuses_while_a_line_is_unreadable(tmp_path):
    """The second half of the same defect, measured:

        compact() reports kept = 1
        >>> the bytes of 'halfwritten' and 'thirdone' no longer exist anywhere: True
            backup files next to the registry: ['c1.jsonl']

    `compact()` rebuilds from `load()`, which skips corrupt lines, so it deleted
    them permanently -- and returned a number that read as success.
    """
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="goodone", type="channel", members=500))
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"username": "halfwrit\n')

    before = path.read_bytes()
    with pytest.raises(RegistryDamaged) as exc:
        reg.compact()
    assert "line 2" in str(exc.value)
    assert "force=True" in str(exc.value)
    assert path.read_bytes() == before      # nothing was rewritten


def test_forced_compaction_keeps_the_original_bytes(tmp_path):
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="goodone", type="channel", members=500))
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"username": "halfwrit\n')
    before = path.read_bytes()

    kept = reg.compact(force=True)
    assert kept == 1
    backup = path.with_name(path.name + ".bak")
    assert backup.read_bytes() == before     # byte-exact, including the bad line


def test_a_clean_compaction_still_keeps_the_previous_file(tmp_path):
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="alpha", members=1))
    reg.append(Source(username="alpha", members=2))
    before = path.read_bytes()
    assert reg.compact() == 1
    assert path.with_name(path.name + ".bak").read_bytes() == before


# --------------------------------------------------------------------------
# Encoding faults cost one line, not the file
# --------------------------------------------------------------------------
def test_a_cp1251_byte_costs_one_line_not_the_whole_registry(tmp_path):
    """Measured before the repair:

        >>> load() raises UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc0
            the other 8 lines are unreachable; corrupt_lines() also dies

    Decoding happened in the `for line in fh` iteration, OUTSIDE the per-line
    `try`, so the comment promising "one corrupt line must never cost the other
    ten thousand" did not hold for an encoding fault -- a hand edit saved as
    cp1251, which is what several Windows editors do by default.

    CHANGED from an earlier version of this test, which used to assert
    `corrupt_lines() == []` and a title full of U+FFFD, which is the defect: the
    bytes sat INSIDE a JSON string, the line parsed, and the source was admitted
    with a mojibake title, `ok: true` and no damage flag anywhere -- then
    `compact()` wrote the replacement characters back as the stored bytes and
    the original was gone. "The bad line lands in the corrupt bucket like every
    other bad line" is what the module promises, and it now does.
    """
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    for i in range(4):
        reg.append(Source(username=f"good{i}", type="channel", members=100 + i))
    with path.open("ab") as fh:
        cyrillic_in_cp1251 = bytes([0xc0, 0xf0, 0xe5, 0xed, 0xe4, 0xe0])
        fh.write(b'{"username": "cp1251one", "title": "'
                 + cyrillic_in_cp1251 + b'"}\n')
    for i in range(4, 8):
        reg.append(Source(username=f"good{i}", type="channel", members=100 + i))

    loaded = reg.load()
    # The original guarantee, unchanged: the other eight are reachable. `load()`
    # used to die on the byte and take them with it.
    assert all(f"good{i}" in loaded for i in range(8))
    # ... and the line whose bytes are not UTF-8 is damage, not a source.
    assert reg.corrupt_lines() == [5]
    assert "not UTF-8" in dict(reg.problems())[5]
    assert "cp1251one" not in loaded
    report = reg.damage_report()
    assert report["details"][0]["username"] == "cp1251one"   # salvaged anyway

    # Compaction cannot bake U+FFFD into the file, because it refuses to run.
    with pytest.raises(RegistryDamaged):
        reg.compact()
    before = path.read_bytes()
    assert reg.compact(force=True) == 8
    assert path.with_name(path.name + ".bak").read_bytes() == before
    assert b"\xef\xbf\xbd" not in path.read_bytes()


def test_a_replacement_character_that_was_really_written_survives(tmp_path):
    """Strict decoding is about the BYTES, not the characters. A title that
    genuinely holds U+FFFD, encoded properly, is not damage."""
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="oddname", type="channel", members=5,
                      title="a � in the name"))
    assert reg.corrupt_lines() == []
    assert reg.get("oddname")["title"] == "a � in the name"


def test_a_byte_that_breaks_the_json_costs_only_its_own_line(tmp_path):
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="goodone", type="channel", members=1))
    with path.open("ab") as fh:
        fh.write(b'{"username": ' + bytes([0xc0, 0xf0]) + b'"broken"}\n')
    reg.append(Source(username="goodtwo", type="channel", members=2))

    assert sorted(reg.load()) == ["goodone", "goodtwo"]
    assert reg.corrupt_lines() == [2]


def test_a_bom_does_not_eat_the_first_source(tmp_path):
    """One save from Notepad ("UTF-8" there means BOM) cost the
    first record, and the next compaction made that permanent."""
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="firstsource", type="channel", members=1000))
    reg.append(Source(username="secondsource", type="channel", members=2000))

    # the Notepad round trip
    text = path.read_text(encoding="utf-8")
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

    loaded = reg.load()
    assert sorted(loaded) == ["firstsource", "secondsource"]
    assert reg.corrupt_lines() == []


# --------------------------------------------------------------------------
# An append cannot land inside a compaction's window
# --------------------------------------------------------------------------
def test_an_append_cannot_run_while_a_compaction_holds_the_write_guard(tmp_path):
    """Measured before the repair:

        append at t+1.35s (compact ended t+1.53s): survived=False [COMPACTED 50000]

    `compact()` was load -> write sibling -> replace, and anything appended
    between the read and the replace was thrown away with the caller told the
    compaction succeeded. Both operations now take the same cross-process guard.
    """
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="alpha", members=1))

    held = configmod.FileGuard(path.with_name(path.name + ".write"), timeout=0.2)
    held.acquire()
    try:
        impatient = Registry(path, guard_timeout=0.3)
        with pytest.raises(configmod.GuardBusy):
            impatient.append(Source(username="beta", members=2))
        with pytest.raises(configmod.GuardBusy):
            impatient.compact()
    finally:
        held.release()

    reg.append(Source(username="beta", members=2))          # free again
    assert sorted(reg.load()) == ["alpha", "beta"]


def test_an_append_from_another_process_survives_a_compaction(tmp_path):
    """The interleaving is ENFORCED, not hoped for.

    This used to be `time.sleep(0.05)` in the child against a compaction of
    40 000 lines, with nothing making the two overlap: on a fast machine the
    compaction finished first and the test passed without exercising the race at
    all. Now the child blocks on a file the parent creates from inside
    `compact()`, so the append is guaranteed to be attempted while the
    compaction is mid-flight -- and it either serialises on the guard or the
    test fails.
    """
    path = tmp_path / "sources.jsonl"
    marker = tmp_path / "compaction-started"
    reg = Registry(path)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for i in range(40_000):
            fh.write(json.dumps(
                {"username": f"bulk{i % 4000}", "type": "channel", "members": i,
                 "first_seen": "2026-01-01", "last_checked": "2026-01-01"},
                sort_keys=True) + "\n")

    body = """
        import os, sys, time
        sys.path.insert(0, sys.argv[1])
        import registry
        reg = registry.Registry(sys.argv[2])
        marker = sys.argv[3]
        deadline = time.time() + 60
        while not os.path.exists(marker) and time.time() < deadline:
            time.sleep(0.001)
        for i in range(20):
            reg.append(registry.Source(username="late%02d" % i, type="channel",
                                       members=1000 + i))
        print("done")
    """
    appender = _spawn(tmp_path, "late.py", body, path, marker)

    real_load = Registry.load
    started = {"at": None}

    def load_and_release(self):
        # Called from inside compact(), while the write guard is held.
        if started["at"] is None:
            started["at"] = time.perf_counter()
            marker.write_text("go", encoding="utf-8")
        return real_load(self)

    Registry.load = load_and_release
    try:
        kept = reg.compact()
    finally:
        Registry.load = real_load
    compaction_ended = time.perf_counter()

    out, err = appender.communicate(timeout=300)
    assert appender.returncode == 0, err
    assert started["at"] is not None
    # The child was released before the compaction finished, so its appends
    # really were racing it rather than following it.
    assert compaction_ended > started["at"]

    loaded = reg.load()
    missing = [f"late{i:02d}" for i in range(20) if f"late{i:02d}" not in loaded]
    assert missing == []
    assert kept >= 4000


# --------------------------------------------------------------------------
# The group cursor is a high-water mark, not an observation
# --------------------------------------------------------------------------
def test_max_id_seen_never_walks_backwards(tmp_path):
    """Measured before the repair:

        cursor now: 120 (was 91234)
        >>> the group will be re-walked from message 120: 91114 messages re-fetched

    Re-walking a group is one HTTP GET per message. A partial read, a resumed
    walk or a page of recent history writing what IT saw used to rewind it.
    """
    reg = Registry(tmp_path / "sources.jsonl")
    reg.append(Source(username="tdlibchat", type="group", max_id_seen=91234))
    reg.append(Source(username="tdlibchat", type="group", max_id_seen=120))
    assert reg.get("tdlibchat")["max_id_seen"] == 91234

    reg.append(Source(username="tdlibchat", type="group", max_id_seen=91300))
    assert reg.get("tdlibchat")["max_id_seen"] == 91300      # forwards still works


def test_first_seen_keeps_the_earliest_date(tmp_path):
    """`append` stamps today onto any record that arrives without a `first_seen`,
    so a later cheap check used to overwrite the real first sighting."""
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="durov", type="channel", members=1,
                      first_seen="2024-01-01"))
    reg.append(Source(username="durov", type="channel", members=2))
    assert reg.get("durov")["first_seen"] == "2024-01-01"


def test_merge_max_survives_a_non_comparable_value():
    """CHANGED from an earlier version of this test.

    This used to assert `== "oops"`, which is the defect written down as a
    guarantee: `try: max(...) except TypeError: pass` FELL THROUGH to
    `out[k] = v`, so the unreadable value won outright whenever it was newer.
    A high-water mark that a garbage value can lower is not one.
    """
    assert _merge({"max_id_seen": 10}, {"max_id_seen": "oops"})["max_id_seen"] == 10
    # ... in both directions: a readable value still replaces an unreadable one.
    assert _merge({"max_id_seen": "oops"}, {"max_id_seen": 10})["max_id_seen"] == 10
    # A quoted number is a number. Dropping it would rewind the cursor just as
    # surely as trusting it would; refusing to READ it is the guess, not reading it.
    assert _merge({"max_id_seen": 91234}, {"max_id_seen": "120"})["max_id_seen"] == 91234
    assert _merge({"max_id_seen": 120}, {"max_id_seen": "91234"})["max_id_seen"] == 91234
    # And `first_seen` the same way round: a non-date never overwrites a date.
    assert _merge({"first_seen": "2024-01-01"},
                  {"first_seen": 20240102})["first_seen"] == "2024-01-01"


# --------------------------------------------------------------------------
# A duplicate key inside one line
# --------------------------------------------------------------------------
def test_a_duplicate_key_in_one_line_is_reported_not_silently_resolved(tmp_path):
    """`{"members": 5000, "members": 1}` read back as 1 with nothing said. Our
    own writer cannot produce it, so the line was hand-edited or spliced."""
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="goodone", type="channel", members=500))
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"username": "dupone", "members": 5000, "members": 1}\n')

    assert "dupone" not in reg.load()
    assert reg.corrupt_lines() == [2]
    assert "duplicate key" in dict(reg.problems())[2]


# --------------------------------------------------------------------------
# Keyword matching on word boundaries, with stems still working
# --------------------------------------------------------------------------
SHIPPED_SHAPE = {
    "finance_payments": ["tax", "карта", "card"],
    "transport": ["bus", "права", "билет"],
    "food_restaurants": ["market", "бар", "еда"],
    "news_politics": ["law", "policy"],
    "technology_software": ["код", "app"],
    "jobs_hiring": ["cv", "job"],
    "real_estate_rent": ["rent", "аренда"],
}

MISLABELS_NOW_GONE = [
    ("Такси Пхукет, трансфер taxi 24/7", "finance_payments", "tax in taxi"),
    ("Business chat for founders in Bali", "transport", "bus in business"),
    ("Marketing agency: SMM, targeted campaigns", "food_restaurants", "market in marketing"),
    ("English-speaking lawyer in Phuket", "news_politics", "law in lawyer"),
    ("Барахолка Пхукет: продам, куплю", "food_restaurants", "бар in барахолка"),
    ("Barcelona expat community chat", "food_restaurants", "bar in barcelona"),
    ("Промокод на скидку 20 процентов", "technology_software", "код in промокод"),
    ("Победа над бюрократией", "food_restaurants", "еда in победа"),
    ("Мы обсуждаем CVS аптеки", "jobs_hiring", "cv in cvs"),
    ("Bike rental and car rental in Bali", "real_estate_rent", "rent in rental"),
    ("Applied linguistics and appliances", "technology_software", "app in appliances"),
]


@pytest.mark.parametrize("text,wrong_topic,why", MISLABELS_NOW_GONE)
def test_substring_matching_no_longer_mislabels_a_source(text, wrong_topic, why):
    """Twelve mis-fires on realistic channel cards, all executed.

    Labels select sources, and a wrong label is not visible to anyone
    downstream. A keyword now has to START a word; what it may carry after that
    depends on the script.
    """
    topics, _ = TopicClassifier(SHIPPED_SHAPE).classify(text)
    assert wrong_topic not in topics, why


def test_the_words_that_should_match_still_do():
    clf = TopicClassifier(SHIPPED_SHAPE)
    assert "finance_payments" in clf.classify("Оплата картой и tax free")[0]
    assert "transport" in clf.classify("Продаю билет на автобус")[0]
    assert "jobs_hiring" in clf.classify("Ищем на эту работу, пришлите cv")[0]
    assert "real_estate_rent" in clf.classify("Long term rent in Canggu")[0]


def test_a_stemmed_keyword_catches_the_inflections_its_readme_promises():
    """`topics.json` says: prefer STEMS -- "'аренд' catches
    аренда/аренду/аренды/арендовать". The matcher has to make that true."""
    clf = TopicClassifier({"housing": ["аренд"], "school": ["школ"]})
    for text in ("Квартира в аренду", "Помогаем с арендой вилл на Бали",
                 "долгосрочная аренда", "можно арендовать"):
        assert clf.classify(text)[0] == ["housing"], text
    assert clf.classify("Обсуждаем школы и садики для детей")[0] == ["school"]


def test_a_stem_does_not_swallow_an_unrelated_longer_word():
    clf = TopicClassifier({"housing": ["аренд"], "bars": ["бар"]})
    assert clf.classify("Барахолка и барахолки")[0] == []
    assert clf.classify("Хабаровск")[0] == []


def test_latin_keywords_match_the_word_and_its_plural_only():
    clf = TopicClassifier({"t": ["visa", "permit"]})
    assert clf.classify("visa run")[0] == ["t"]
    assert clf.classify("visas and permits")[0] == ["t"]
    assert clf.classify("visage and permitting")[0] == []


def test_a_multi_word_keyword_matches_as_a_phrase():
    clf = TopicClassifier({"visas": ["green card", "миграционная карта"]})
    assert clf.classify("I finally got my green card")[0] == ["visas"]
    assert clf.classify("green cards for everyone")[0] == ["visas"]
    assert clf.classify("green cardboard boxes")[0] == []
    assert clf.classify("Как получить ВНЖ: миграционная карта")[0] == ["visas"]
    assert clf.classify("card is green")[0] == []


def test_the_mislabels_the_matcher_cannot_fix_are_named_not_hidden():
    """Three of the twelve are exact whole-word matches on words that genuinely
    belong to two topics: `policy` (insurance / politics), `права` (author's
    rights / driving licence), `карта` (bank card / migration card). No matcher
    can separate those -- only the vocabulary can, and
    `references/topics.json` is the reader's to edit. This test pins the current
    behaviour so the next person sees the problem instead of rediscovering it.
    """
    clf = TopicClassifier(SHIPPED_SHAPE)
    assert "news_politics" in clf.classify("Health insurance policy comparison")[0]
    assert "transport" in clf.classify("Авторские права и защита контента")[0]
    assert "finance_payments" in clf.classify("Как получить ВНЖ: миграционная карта")[0]


# ==========================================================================
# Regression guards. One test per finding.
# ==========================================================================
from registry import VALID_STATUS


def test_a_card_with_no_member_count_does_not_bypass_the_floor(tmp_path):
    """`if members is not None:` waived the floor for exactly the card most
    likely to need it: a landing page that did not give up its size is not
    evidence of size, and SKILL.md states the floor as an unconditional refusal."""
    rules = AdmissionRules()
    verdict = judge({"username": "tinychan", "type": "channel", "members": None,
                     "exists": True, "status": "alive"}, rules)
    assert verdict.admit is False
    assert "floor of 100" in verdict.reason
    assert judge({"username": "tinygroup", "type": "group", "members": None,
                  "exists": True, "status": "alive"}, rules).admit is False

    # A caller that really wants to admit an unmeasured source says so.
    waived = AdmissionRules(require_members=False)
    assert judge({"username": "tinychan", "type": "channel", "members": None,
                  "exists": True, "status": "alive"}, waived).admit is True


def test_the_admission_floors_are_the_numbers_skill_md_prints():
    """`min_group_members` 50 could be rewritten to 5 with the suite green,
    while the channel floor of 100 was pinned. Both, inclusively, now."""
    rules = AdmissionRules()
    assert (rules.min_channel_members, rules.min_group_members) == (100, 50)
    card = {"exists": True, "status": "alive"}
    assert judge({**card, "username": "grpx", "type": "group", "members": 49},
                 rules).admit is False
    assert judge({**card, "username": "grpx", "type": "group", "members": 50},
                 rules).admit is True
    assert judge({**card, "username": "chnx", "type": "channel", "members": 99},
                 rules).admit is False
    assert judge({**card, "username": "chnx", "type": "channel", "members": 100},
                 rules).admit is True


def test_there_is_one_username_rule_and_both_old_disagreements_are_gone():
    """`tg.py` accepted `abc` and refused `_abcd`; this file did the
    opposite. So `verify abc --write` spent a real GET, verified the name, and
    was then refused by the registry inside the same command."""
    from registry import USERNAME_RE, valid_username

    assert valid_username("durov") and valid_username("abcd")
    assert valid_username("@Durov")                     # the @ is stripped
    assert not valid_username("abc")                    # 3 characters: too short
    assert not valid_username("_abcd")                  # must start with a letter
    assert not valid_username("9abcd")
    assert not valid_username("a" * 33)
    assert not valid_username("a!bcd")
    assert valid_username("a" * 32)
    # The rule the CLI must share, exported so there can only be one copy.
    assert USERNAME_RE.pattern == r"^[A-Za-z][A-Za-z0-9_]{3,31}$"

    rules = AdmissionRules()
    assert judge({"username": "abc", "type": "channel", "members": 900,
                  "exists": True}, rules).admit is False


def test_a_source_that_died_can_finally_be_recorded_as_gone(tmp_path):
    """`exists is False` was refused before any update could be recorded, so
    a source that had died stayed `alive` in the registry for ever and every
    later run kept spending requests on it. `VALID_STATUS` listed values nothing
    in the skill could produce."""
    reg = Registry(tmp_path / "sources.jsonl")
    reg.append(Source(username="flipname", type="channel", members=900,
                      status="alive", max_id_seen=5000))
    known = reg.get("flipname")

    dead = judge({"username": "flipname", "exists": False}, AdmissionRules(), known)
    assert dead.admit is True and dead.action == "update"
    assert dead.record_status == "gone"
    assert dead.warnings and "gone" in dead.warnings[0]

    taken = judge({"username": "flipname", "exists": False, "taken": True},
                  AdmissionRules(), known)
    assert taken.record_status == "private"

    # A name nobody ever knew is still not a source.
    stranger = judge({"username": "neverseen", "exists": False}, AdmissionRules())
    assert stranger.admit is False

    # And the write really lands, cursor and history intact.
    reg.append(Source(username="flipname", status=dead.record_status))
    record = reg.get("flipname")
    assert record["status"] == "gone"
    assert record["max_id_seen"] == 5000
    assert record["type"] == "channel"
    assert record["status"] in VALID_STATUS


def test_a_contradicting_check_cannot_flip_the_stored_type(tmp_path):
    """`type` decides the entire read route and was plain newest-wins. One
    `verify --write` that read an interstitial turned a verified channel into a
    group, and from then on `search` and `history` refused it with exit 6 --
    from a command that printed `ok: true` and `updated: 1`."""
    reg = Registry(tmp_path / "sources.jsonl")
    reg.append(Source(username="chan", type="channel", members=5000,
                      status="alive", max_id_seen=91234))
    reg.append(Source(username="chan", type="group", members=5000, status="alive"))

    record = reg.get("chan")
    assert record["type"] == "channel"                  # the stored type stands
    assert record["type_conflict"]["stored"] == "channel"
    assert record["type_conflict"]["seen"] == "group"
    assert record["max_id_seen"] == 91234               # everything else refreshed

    # `judge` says so too, so a caller can print it rather than discovering it
    # in the read route three commands later.
    verdict = judge({"username": "chan", "type": "group", "members": 5000,
                     "exists": True, "status": "alive"}, AdmissionRules(), record)
    assert verdict.admit is True
    assert verdict.warnings and "stored type stands" in verdict.warnings[0]

    # A caller that really did establish the new type has a way to say so.
    reg.append({"username": "chan", "type": "group", "type_confirmed": True})
    corrected = reg.get("chan")
    assert corrected["type"] == "group"
    assert "type_conflict" not in corrected

    # And a first type is not a conflict: `_merge` skips None, so a later verify
    # still fills in a type nobody had established.
    reg2 = Registry(tmp_path / "second.jsonl")
    reg2.append(Source(username="unknowntype", members=900))
    reg2.append(Source(username="unknowntype", type="channel", members=900))
    assert reg2.get("unknowntype")["type"] == "channel"
    assert "type_conflict" not in reg2.get("unknowntype")


def test_a_corrupt_newest_line_is_never_a_silent_rewind(tmp_path):
    """M10. `load()` skipped corrupt lines, so when the NEWEST line for a
    username was the unreadable one the caller got the PREVIOUS record with
    nothing said -- and `history --since-last` re-fetched 91 114 messages on the
    most expensive surface there is."""
    from registry import DERIVED_FIELDS

    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="goodchan", type="channel", members=500,
                      max_id_seen=120))

    # A truncation that keeps the username: the mark is recovered outright.
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"username": "goodchan", "max_id_seen": 91234, "typ' + "\n")
    record = reg.get("goodchan")
    assert record["max_id_seen"] == 91234           # never rewound
    assert record["damaged_lines"] == [2]           # and never silently

    # Our own writer sorts keys, so a real truncation loses `username` FIRST and
    # keeps `max_id_seen`. That mark cannot be attributed to anybody -- applying
    # it to the wrong source would skip unread messages for ever -- so it is not
    # applied, and every cursor in the file is declared suspect instead.
    other = tmp_path / "real.jsonl"
    reg2 = Registry(other)
    reg2.append(Source(username="goodchan", type="channel", members=500,
                       max_id_seen=120))
    whole = json.dumps(Registry._stamp(Source(username="goodchan", type="channel",
                                              members=500, max_id_seen=91234)),
                       ensure_ascii=False, sort_keys=True)
    assert whole.rindex('"username"') > whole.rindex('"max_id_seen"')
    with other.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(whole[:-12] + "\n")
    record = reg2.get("goodchan")
    assert record["max_id_seen"] == 120
    assert record["cursor_may_be_stale"] is True
    assert record["damaged_lines_unattributed"] == [2]

    report = reg2.damage_report()
    assert report["corrupt_lines"] == [2]
    assert report["details"][0]["max_id_seen"] == 91234
    assert report["cursor_may_be_stale"] is True

    # A clean registry says none of this.
    clean = Registry(tmp_path / "clean.jsonl")
    clean.append(Source(username="fine", type="channel", members=500))
    assert set(clean.get("fine")) & set(DERIVED_FIELDS) == set()
    assert clean.damage_report()["corrupt_lines"] == []


def test_fields_computed_on_read_are_never_written_back(tmp_path):
    """`damaged_lines` and `type_conflict` are observations about the file, not
    facts about the source. `compact()` rebuilds from `load()`, so anything
    derived has to be stripped there or it becomes a stored field nothing
    maintains."""
    from registry import DERIVED_FIELDS

    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="chan", type="channel", members=5000))
    reg.append(Source(username="chan", type="group", members=5000))
    assert "type_conflict" in reg.get("chan")

    reg.compact()
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        record = json.loads(line)
        assert set(record) & set(DERIVED_FIELDS) == set(), record

    # And a record read back out and re-appended does not carry them either.
    reg.append(Source(username="chan", type="channel", members=1))
    reg.append(reg.get("chan"))
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        assert set(json.loads(line)) & set(DERIVED_FIELDS) == set()


def test_the_recovery_instruction_names_a_thing_the_library_really_has(tmp_path):
    """`compact()` tells the operator to pass `force=True`; the CLI has no
    `--force` (that half is the CLI's own repair). The library side must at
    least be there, and the message must not name something that is not."""
    import inspect

    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="goodone", type="channel", members=500))
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"username": "halfwrit' + "\n")

    with pytest.raises(RegistryDamaged) as exc:
        reg.compact()
    message = str(exc.value)
    # Both halves of the recovery route, because the operator and the Python
    # caller reach it by different names. The message named only `force=True`
    # while the CLI had no flag at all; the flag exists now and the sentence
    # says so, so the advice can actually be followed from where it is read.
    assert "tg.py registry compact --force" in message
    assert "force=True" in message
    assert "sources.jsonl.bak" in message

    signature = inspect.signature(Registry.compact)
    assert "force" in signature.parameters
    assert signature.parameters["force"].default is False
    assert signature.parameters["force"].kind is inspect.Parameter.KEYWORD_ONLY

    before = path.read_bytes()
    assert reg.compact(force=True) == 1
    assert path.with_name(path.name + ".bak").read_bytes() == before


# ==========================================================================
# Regression guards. One test per finding; every one of
# them fails against the code as it stood before it.
# ==========================================================================
# The new exception types are imported INSIDE the tests that need them on
# purpose: a module-level import of a name the old code does not have turns the
# whole file into a collection error, and then "how many tests fail against the
# code as it stood" cannot be measured at all.


def test_a_confirmed_type_can_be_written_through_a_source_object(tmp_path):
    """`_merge` refuses a contradicting `type` unless the
    incoming record carries `type_confirmed` -- and `Source`, the class every
    registry writer goes through, had no such field, so the correction could be
    made only by hand-editing the JSONL or by calling the Python API with a raw
    dict. The documented repair (`verify --write` -> `discover.admit` ->
    `Source`) therefore reported `updated: 1` and changed nothing, three runs in
    a row, while `history` and `search` refused the source with exit 6 for ever.
    """
    reg = Registry(tmp_path / "sources.jsonl")
    reg.append(Source(username="telegram", type="group", status="alive",
                      members=100000, max_id_seen=91234))
    assert reg.get("telegram")["type"] == "group"

    written = reg.append(Source(username="telegram", type="channel",
                                status="alive", members=100000,
                                type_confirmed=True))
    assert written["type_confirmed"] is True          # it reaches the line...
    corrected = reg.get("telegram")
    assert corrected["type"] == "channel"             # ... and the merge
    assert "type_conflict" not in corrected
    assert corrected["max_id_seen"] == 91234          # nothing else is disturbed

    # It is a directive, never a stored fact about the source: it must not
    # survive into the collapsed view or into a compacted file.
    assert "type_confirmed" not in corrected
    reg.compact()
    for line in (tmp_path / "sources.jsonl").read_text(encoding="utf-8").splitlines():
        assert "type_confirmed" not in json.loads(line)

    # And an ordinary, unconfirmed contradiction is still refused.
    reg.append(Source(username="telegram", type="group", status="alive"))
    assert reg.get("telegram")["type"] == "channel"
    assert reg.get("telegram")["type_conflict"]["seen"] == "group"

    # A false flag says nothing and is not written.
    plain = reg.append(Source(username="another", type="channel", members=5,
                              type_confirmed=False))
    assert "type_confirmed" not in plain


def test_a_quoted_cursor_cannot_rewind_the_high_water_mark(tmp_path):
    """End to end on a file rather than on `_merge` alone.

    A hand-repaired line -- the repair `RegistryDamaged` explicitly asks for --
    carrying `"max_id_seen": "120"` collapsed to `'120'`: the cursor had rewound
    by 91 114 messages and could never climb back, and `tg.py:988`'s
    `max(cursor, known.get("max_id_seen") or 0)` raised `TypeError` on top.
    """
    path = tmp_path / "sources.jsonl"
    path.write_text('{"username": "chan", "type": "channel", "max_id_seen": 91234}\n'
                    '{"username": "chan", "type": "channel", "max_id_seen": "120"}\n',
                    encoding="utf-8")
    record = Registry(path).get("chan")
    assert record["max_id_seen"] == 91234
    assert max(500, record["max_id_seen"]) == 91234          # tg.py's expression


def test_a_cursor_nothing_can_read_is_dropped_and_named(tmp_path):
    """The other half of it: when there is no older value to fall back on.

    Handing the string out let `tg.py history --write` die with a bare
    `TypeError: '>' not supported between instances of 'str' and 'int'`, which
    is a public entry point raising an undeclared exception as well as an
    unreadable answer.
    """
    path = tmp_path / "sources.jsonl"
    path.write_text('{"username": "chan", "type": "channel", '
                    '"max_id_seen": {"oh": "dear"}, "members": "many"}\n',
                    encoding="utf-8")
    record = Registry(path).get("chan")
    assert "max_id_seen" not in record
    assert "members" not in record
    assert set(record["unreadable_fields"]) == {"max_id_seen", "members"}
    assert max(500, record.get("max_id_seen") or 0) == 500    # tg.py survives it

    # And the flag is derived, so a compaction does not store it.
    from registry import DERIVED_FIELDS

    assert "unreadable_fields" in DERIVED_FIELDS


def test_a_second_compaction_refuses_to_destroy_the_only_backup(tmp_path):
    """A second compaction must not write over the backup the first one left.

        after compact#1 -> .bak has them: True
        after compact#2 -> .bak has them: False

    The operator is told `--force` will "keep the original in sources.jsonl.bak"
    and runs it; the corrupt bytes -- a truncated newest line holding
    `max_id_seen: 91234` -- then exist only there. Some days later the file is
    clean, so `compact` asks for nothing and warns about nothing, and replaces
    the backup with the already-compacted file. Gone from both, permanently.
    """
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="alivechan", type="channel", members=500,
                      max_id_seen=500))
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"max_id_seen": 91234, "type": "channel", "usern' + "\n")

    reg.compact(force=True)
    backup = path.with_name(path.name + ".bak")
    assert b"91234" in backup.read_bytes()

    from registry import WouldDestroy

    kept = backup.read_bytes()
    with pytest.raises(WouldDestroy) as exc:
        reg.compact()
    message = str(exc.value)
    assert str(backup) in message                  # names the file...
    assert "@alivechan" in message                 # ... and what is in it
    assert "--force" in message
    assert backup.read_bytes() == kept             # and destroyed nothing

    # Forcing is still allowed: the operator asked for it in so many words.
    assert reg.compact(force=True) == 1
    assert backup.read_bytes() != kept


def test_an_empty_backup_is_not_a_backup(tmp_path):
    """A zero-byte `.bak` from an interrupted copy holds nothing to lose."""
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="alpha", type="channel", members=1))
    path.with_name(path.name + ".bak").write_bytes(b"")
    assert reg.compact() == 1


def test_the_backup_is_written_through_the_guarded_atomic_helper(tmp_path):
    """`shutil.copyfile` is neither atomic nor guarded: a reader holding
    the destination open on Windows, or a kill between the two writes, left a
    half-written backup that read as the whole thing. The bytes still have to
    survive it exactly -- the point of the backup is the bytes `load()` could
    not read."""
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="alpha", type="channel", members=1))
    with path.open("ab") as fh:
        fh.write(b'{"username": "cp", "title": "' + "Птицы".encode("cp1251") + b'"}\n')
    before = path.read_bytes()

    seen: list[str] = []
    real = configmod.atomic_write_text

    def watched(target, text, **kw):
        seen.append(Path(target).name)
        return real(target, text, **kw)

    configmod.atomic_write_text = watched
    try:
        reg.compact(force=True)
    finally:
        configmod.atomic_write_text = real

    backup = path.with_name(path.name + ".bak")
    assert seen == [backup.name, path.name]         # backup first, then the file
    assert backup.read_bytes() == before            # byte-exact, cp1251 and all
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_a_partial_append_does_not_erase_a_stored_status(tmp_path):
    """`as_dict()` drops `None`, which is what makes a partial append
    safe -- but `status` defaulted to `"unknown"`, so it was written on every
    append and won every merge. A source recorded `alive`, `gone` or `private`
    silently became `unknown`, and `gone` / `private` are exactly what `judge`
    recently learned to produce."""
    reg = Registry(tmp_path / "sources.jsonl")
    reg.append(Source(username="chan", type="channel", members=900, status="alive"))
    reg.append(Source(username="chan", max_id_seen=120))
    record = reg.get("chan")
    assert record["status"] == "alive"
    assert record["max_id_seen"] == 120

    # A caller that really means "unknown" still says so, and it is stored.
    reg.append(Source(username="chan", status="unknown"))
    assert reg.get("chan")["status"] == "unknown"


def test_the_damage_report_is_what_the_refusal_shows_the_operator(tmp_path):
    """`damage_report()` was computed for exactly this purpose and
    called from nothing but the tests: `registry stats` printed a bare
    `corrupt_lines: [2, 7]` and no command anywhere said what was in them. The
    refusal a damaged registry produces is now built from it, so the salvaged
    username and cursor reach the person who has to act on them."""
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="goodchan", type="channel", members=500))
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"username": "lostchan", "max_id_seen": 91234, "typ' + "\n")

    with pytest.raises(RegistryDamaged) as exc:
        reg.compact()
    message = str(exc.value)
    assert "line 2" in message
    assert "@lostchan" in message
    assert "91234" in message

    # And the two answers come from one scan, so they cannot disagree.
    assert reg.problems() == [(item["line"], item["why"])
                              for item in reg.damage_report()["details"]]
    assert reg.corrupt_lines() == reg.damage_report()["corrupt_lines"]


HOSTILE_RECORDS = {
    "username is a number": {"username": 12345},
    "username is missing": {"type": "channel"},
    "a value json cannot write": {"username": "chan", "peer": {"id": object()}},
    "a cursor that is not one": {"username": "chan", "max_id_seen": "later"},
    "a type that decides nothing": {"username": "chan", "type": "supergroup"},
    "a status nobody defined": {"username": "chan", "status": "maybe"},
}


@pytest.mark.parametrize("label", sorted(HOSTILE_RECORDS))
def test_a_hostile_record_is_refused_by_name_and_writes_nothing(tmp_path, label):
    """`{"username": 12345}` left `AttributeError` from `"".lstrip` and a
    value `json` cannot serialise left `TypeError` from inside `append` -- both
    bare, both out of a public entry point, and the second one AFTER the guard
    was taken."""
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    from registry import SourceRefused

    reg.append(Source(username="goodone", type="channel", members=1))
    before = path.read_bytes()

    with pytest.raises(SourceRefused):
        reg.append(HOSTILE_RECORDS[label])
    assert path.read_bytes() == before

    # A batch with one bad record in it writes NONE of it, not half.
    with pytest.raises(SourceRefused):
        reg.append_many([Source(username="fine", type="channel", members=2),
                         HOSTILE_RECORDS[label]])
    assert path.read_bytes() == before
    assert sorted(reg.load()) == ["goodone"]


def test_a_damaged_topic_vocabulary_is_a_named_refusal(tmp_path):
    """A trailing comma in `topics.json` left `json.JSONDecodeError` -- a
    bare `ValueError` -- out of `get_classifier` and out of `tg.py verify`."""
    from registry import VocabularyUnreadable

    path = tmp_path / "topics.json"
    path.write_text('{"housing": ["rent"],}', encoding="utf-8")
    with pytest.raises(VocabularyUnreadable) as exc:
        TopicClassifier.from_file(path)
    assert str(path) in str(exc.value)

    with pytest.raises(VocabularyUnreadable):
        TopicClassifier.from_file(tmp_path / "not-there.json")
    with pytest.raises(VocabularyUnreadable):
        TopicClassifier(["housing", "rent"])


def test_a_member_count_nothing_can_read_is_not_a_member_count():
    """`members: "many"` reached `members < floor` and left a bare
    `TypeError` out of `judge`, which is the gate every candidate passes."""
    rules = AdmissionRules()
    verdict = judge({"username": "somechan", "type": "channel", "members": "many",
                     "exists": True, "status": "alive"}, rules)
    assert verdict.admit is False
    assert "floor of 100" in verdict.reason
    # A quoted number is still a number, and still measured against the floor.
    assert judge({"username": "somechan", "type": "channel", "members": "5",
                  "exists": True, "status": "alive"}, rules).admit is False
    assert judge({"username": "somechan", "type": "channel", "members": "500",
                  "exists": True, "status": "alive"}, rules).admit is True


# --------------------------------------------------------------------------
# Reading the registry must not be able to break writing it
# --------------------------------------------------------------------------
def test_a_reader_in_the_middle_of_iter_raw_cannot_fail_a_compaction(
    tmp_path, monkeypatch
):
    """`iter_raw` opened the file with a plain `open("rb")` and is a GENERATOR,
    so the handle stayed open for as long as the caller walked it -- the whole
    of `load()`, and therefore the whole of almost every command.

    On NTFS CPython's `open()` does not pass FILE_SHARE_DELETE, so one ordinary
    reader was enough to fail `os.replace` over the same name. Measured: a
    reader mid-`iter_raw` plus a `compact()` -> refusal after 2.1 s and exit 9.
    `config.read_bytes_shared` was written for exactly this and every other
    reader in the skill already used it.
    """
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="durov", members=100))
    reg.append(Source(username="tdlibchat", type="group", members=900))

    seen = []
    real = configmod.read_bytes_shared

    def spy(target):
        seen.append(Path(target))
        return real(target)

    monkeypatch.setattr(configmod, "read_bytes_shared", spy)
    rows = list(reg.iter_raw())
    assert len(rows) == 2
    assert path in seen, "iter_raw did not go through the shared reader"

    # And the file can be replaced under a reader that is only half-done.
    monkeypatch.undo()
    walker = reg.iter_raw()
    next(walker)
    assert reg.compact() == 2
    assert sorted(reg.load()) == ["durov", "tdlibchat"]


def test_a_compaction_that_cannot_replace_the_file_takes_its_backup_back(
    tmp_path, monkeypatch
):
    """The backup is written BEFORE the registry is replaced, so a compaction
    that then failed left a `.bak` nobody asked for.

    The next attempt refused with `_refuse_to_lose_the_backup` -- a message
    about an earlier `--force` compaction that never happened -- and offered
    `--force` as the way out, which presents the accident as a deliberate
    replacement. A backup this call created and could not earn is removed
    again; one that was already on disk is somebody else's evidence and is left
    alone.
    """
    path = tmp_path / "sources.jsonl"
    reg = Registry(path)
    reg.append(Source(username="durov", members=100))

    real_write = configmod.atomic_write_text

    def refuse_the_registry(target, text, **kwargs):
        if Path(target) == path:
            raise configmod.AtomicWriteFailed(
                f"could not replace {target}: another process is holding it open")
        return real_write(target, text, **kwargs)

    monkeypatch.setattr(configmod, "atomic_write_text", refuse_the_registry)
    with pytest.raises(configmod.AtomicWriteFailed):
        reg.compact()
    assert not reg.backup_path().exists(), (
        "a failed compaction left its own backup behind, and the next attempt "
        "refuses because of it")

    # So the next attempt is an ordinary compaction, with no `--force` and no
    # story about bytes an earlier run salvaged.
    monkeypatch.undo()
    assert reg.compact() == 1
    assert reg.backup_path().exists()


def test_the_registry_write_guard_outlasts_its_own_staleness_threshold(tmp_path):
    """20 s of waiting against a 120 s staleness threshold: a writer killed
    mid-write blocked every registry write for two full minutes, and each
    attempt burned 20 s of wall clock to say so."""
    reg = Registry(tmp_path / "sources.jsonl")
    guard = reg._guard()
    assert guard.timeout > guard.stale_after, (guard.timeout, guard.stale_after)
    assert guard.stale_after == configmod.GUARD_STALE_AFTER
