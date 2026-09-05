"""Tests for scripts/config.py: paths, credential loading and secret redaction.

No network, no installs. Every file lives under tmp_path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPTS = (Path(__file__).resolve().parent.parent
           / "skills" / "telegram-research" / "scripts")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pytest

from config import (
    ENV_CREDENTIAL,
    ENV_STATE,
    Config,
    ConfigError,
    load,
    read_credentials,
    redact,
    redact_obj,
)


# --------------------------------------------------------------------------
# read_credentials
# --------------------------------------------------------------------------
def test_read_credentials_raises_naming_env_var_when_unset(monkeypatch):
    monkeypatch.delenv(ENV_CREDENTIAL, raising=False)
    cfg = Config(credential_path=None)
    with pytest.raises(ConfigError) as exc:
        read_credentials(cfg)
    msg = str(exc.value)
    assert ENV_CREDENTIAL in msg
    # nothing is searched for on disk when the env var is unset
    assert "searched for on disk" in msg


def test_the_environment_supplies_the_credential_without_any_file(monkeypatch):
    """A credential file is optional: all three variables in the environment are
    enough and `TELEGRAM_RESEARCH_ENV` need not be set at all. That is the route
    for anyone whose project folder is committed or cloud-synced, where no
    credential file may live."""
    monkeypatch.delenv(ENV_CREDENTIAL, raising=False)
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abcdef0123456789abcdef0123456789")
    monkeypatch.setenv("TELEGRAM_SESSION", "1AQANOTsomefakesessionvalue1234567890")

    values = read_credentials(Config(credential_path=None))
    assert values["TELEGRAM_API_ID"] == "123456"
    assert sorted(values) == ["TELEGRAM_API_HASH", "TELEGRAM_API_ID",
                              "TELEGRAM_SESSION"]


def test_the_environment_wins_over_the_file_when_both_are_present(tmp_path,
                                                                  monkeypatch):
    """Order is environment first, file second -- not a merge, and not the file."""
    cred_file = tmp_path / "creds.env"
    cred_file.write_text(
        "TELEGRAM_API_ID=999999\n"
        "TELEGRAM_API_HASH=ffffffffffffffffffffffffffffffff\n"
        "TELEGRAM_SESSION=1AQAfromthefile00000000000000000000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abcdef0123456789abcdef0123456789")
    monkeypatch.setenv("TELEGRAM_SESSION", "1AQANOTsomefakesessionvalue1234567890")

    values = read_credentials(Config(credential_path=cred_file))
    assert values["TELEGRAM_API_ID"] == "123456"
    assert values["TELEGRAM_SESSION"] == "1AQANOTsomefakesessionvalue1234567890"


def test_a_partial_environment_is_ignored_rather_than_merged(monkeypatch):
    """Two of three is not a credential. Half from the environment and half from
    the file is exactly the configuration nobody can reason about, so the
    partial set is dropped and named in the error instead."""
    monkeypatch.delenv(ENV_CREDENTIAL, raising=False)
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abcdef0123456789abcdef0123456789")

    with pytest.raises(ConfigError) as exc:
        read_credentials(Config(credential_path=None))
    msg = str(exc.value)
    assert "TELEGRAM_API_ID" in msg and "TELEGRAM_API_HASH" in msg
    assert "not all three" in msg


def test_a_partial_environment_does_not_fill_gaps_in_the_file(tmp_path,
                                                              monkeypatch):
    """The file is short of TELEGRAM_SESSION and the environment has it. They are
    still not combined: the failure names the missing key and says the partial
    environment was ignored."""
    cred_file = tmp_path / "creds.env"
    cred_file.write_text(
        "TELEGRAM_API_ID=123456\n"
        "TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_SESSION", "1AQANOTsomefakesessionvalue1234567890")

    with pytest.raises(ConfigError) as exc:
        read_credentials(Config(credential_path=cred_file))
    msg = str(exc.value)
    assert "TELEGRAM_SESSION" in msg
    assert "ignored" in msg


def test_a_blank_environment_variable_does_not_count_as_set(tmp_path, monkeypatch):
    """An empty or whitespace-only variable is absence, not a value. Otherwise an
    exported-but-empty TELEGRAM_SESSION would shadow a perfectly good file."""
    cred_file = tmp_path / "creds.env"
    cred_file.write_text(
        "TELEGRAM_API_ID=123456\n"
        "TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789\n"
        "TELEGRAM_SESSION=1AQAfromthefile00000000000000000000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abcdef0123456789abcdef0123456789")
    monkeypatch.setenv("TELEGRAM_SESSION", "   ")

    values = read_credentials(Config(credential_path=cred_file))
    assert values["TELEGRAM_SESSION"] == "1AQAfromthefile00000000000000000000"


def test_read_credentials_raises_naming_missing_keys(tmp_path):
    cred_file = tmp_path / "creds.env"
    cred_file.write_text("TELEGRAM_API_ID=12345\n", encoding="utf-8")
    cfg = Config(credential_path=cred_file)
    with pytest.raises(ConfigError) as exc:
        read_credentials(cfg)
    msg = str(exc.value)
    assert "TELEGRAM_API_HASH" in msg
    assert "TELEGRAM_SESSION" in msg


def test_read_credentials_parses_well_formed_file(tmp_path):
    cred_file = tmp_path / "creds.env"
    cred_file.write_text(
        "# a leading comment\n"
        "TELEGRAM_API_ID=123456\n"
        'TELEGRAM_API_HASH="abcdef0123456789abcdef0123456789"\n'
        "TELEGRAM_SESSION='1AQANOTsomefakesessionvalue1234567890'\n"
        "\n"
        "# trailing comment\n",
        encoding="utf-8",
    )
    cfg = Config(credential_path=cred_file)
    values = read_credentials(cfg)
    assert values["TELEGRAM_API_ID"] == "123456"
    assert values["TELEGRAM_API_HASH"] == "abcdef0123456789abcdef0123456789"
    assert values["TELEGRAM_SESSION"] == "1AQANOTsomefakesessionvalue1234567890"


# --------------------------------------------------------------------------
# redact / redact_obj -- the test the spec demands by name
# --------------------------------------------------------------------------
def test_redact_scrubs_stringsession_and_api_hash_everywhere():
    # 353 characters, the measured length of a real Telethon v1 StringSession.
    session_token = "1" + "A" * 352
    api_hash_token = "0123456789abcdef" * 2  # api_hash-shaped: 32 lowercase hex chars

    nested = {
        "credentials": {
            "session_string": session_token,
            "hash_value": api_hash_token,
        },
        "note": "call support if this breaks",
    }

    # 1. a JSON dump
    dumped = json.dumps(nested)
    redacted_dump = redact(dumped)
    assert session_token not in redacted_dump
    assert api_hash_token not in redacted_dump

    # 2. an exception message
    try:
        raise RuntimeError(f"login failed for session {session_token} hash {api_hash_token}")
    except RuntimeError as exc:
        redacted_exc = redact(str(exc))
    assert session_token not in redacted_exc
    assert api_hash_token not in redacted_exc

    # 3. a nested structure via redact_obj
    redacted_obj = redact_obj(nested)
    flattened = json.dumps(redacted_obj)
    assert session_token not in flattened
    assert api_hash_token not in flattened

    # key-based redaction: a SECRET_KEYS-named key is wiped regardless of its value
    keyed = {"TELEGRAM_SESSION": "short-harmless-value"}
    assert redact_obj(keyed)["TELEGRAM_SESSION"] == "<redacted>"

    # a harmless ordinary sentence is not mangled
    ordinary = "The report ships tomorrow at 3pm."
    assert redact(ordinary) == ordinary


# --------------------------------------------------------------------------
# Config.load
# --------------------------------------------------------------------------
def test_config_load_honours_state_env_and_derives_paths(tmp_path, monkeypatch):
    state_dir = tmp_path / "mystate"
    monkeypatch.setenv(ENV_STATE, str(state_dir))
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)
    monkeypatch.delenv(ENV_CREDENTIAL, raising=False)

    cfg = load()

    assert cfg.state_dir == state_dir
    assert cfg.registry_path == state_dir / "sources.jsonl"
    assert cfg.ledger_path == state_dir / "resolve-ledger.json"
    assert cfg.lock_path == state_dir / "account.lock"


# ==========================================================================
# Regression guards. Every test below fails against the code as it
# stood before the repair; each one names the finding it guards.
# ==========================================================================
import os
import subprocess
import sys
import time

import config as config_module


# --------------------------------------------------------------------------
# State does not follow the shell
# --------------------------------------------------------------------------
def test_state_dir_ignores_the_working_directory(tmp_path, monkeypatch):
    """One `cd` used to walk past a ten-hour freeze.

    With TELEGRAM_RESEARCH_STATE unset -- the documented default -- the ledger and
    the lock were resolved against `Path.cwd()`, so a run started from anywhere
    but the repo root got a brand-new, empty, unfrozen ledger AND a different
    lock file, which is both safety rules failing at once.
    """
    monkeypatch.delenv(ENV_STATE, raising=False)
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)

    sub = tmp_path / "somewhere" / "deeper"
    sub.mkdir(parents=True)

    monkeypatch.chdir(tmp_path)
    from_here = load().state_dir
    monkeypatch.chdir(sub)
    from_there = load().state_dir

    assert from_here == from_there
    assert tmp_path not in from_here.parents
    # anchored on the home directory, which no `cd` and no reinstall can move
    assert from_here == Path.home() / config_module.STATE_DIR_NAME


def test_the_default_state_dir_is_outside_the_skill_and_outside_the_project(
        monkeypatch):
    """The state outlives `npx skills update`, which is the point of it.

    The default used to be `<repo>/store/_telegram`, resolved by walking up from
    the skill's own folder for a `.git` or a `CLAUDE.md` -- and falling back to
    THE SKILL'S OWN FOLDER when it found neither, which is what an installed
    skill looks like. Everything the safety rules stand on lived there: the
    resolve ledger with the 36 468 s freeze in it, `account.lock`, the source
    registry, the peer cache. `npx skills update` replaces that folder
    wholesale, so an update looked like a clean machine that had never been
    frozen -- and there is no undo, because the folder is not in anybody's git.
    """
    monkeypatch.delenv(ENV_STATE, raising=False)
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)

    default = config_module.default_state_dir()

    assert default == Path.home() / config_module.STATE_DIR_NAME
    assert default.is_absolute()
    skill = config_module.skill_root()
    assert skill not in default.parents and default != skill
    assert config_module.repo_root() not in default.parents
    # one constant decides the name, so renaming the skill costs one line
    assert default.name == config_module.STATE_DIR_NAME


def test_root_argument_does_not_move_the_state_dir(tmp_path, monkeypatch):
    """`tg.py` passes `--root`, whose default is `"."`. That default must
    not be able to relocate the ledger."""
    monkeypatch.delenv(ENV_STATE, raising=False)
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)
    cfg = load(tmp_path)
    assert cfg.root == tmp_path          # run folders still honour it
    assert tmp_path not in cfg.state_dir.parents
    assert cfg.state_dir == config_module.default_state_dir()


# --------------------------------------------------------------------------
# The env var names a DIRECTORY
# --------------------------------------------------------------------------
def test_state_env_pointing_at_a_file_is_a_config_error_not_a_traceback(tmp_path, monkeypatch):
    """Pointing the variable at a file instead of a directory used to raise a raw
    `FileExistsError [WinError 183]` out of `mkdir`."""
    a_file = tmp_path / "sources.jsonl"
    a_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv(ENV_STATE, str(a_file))
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)

    with pytest.raises(ConfigError) as exc:
        load()
    msg = str(exc.value)
    assert ENV_STATE in msg
    assert "DIRECTORY" in msg


def test_ensure_dirs_on_a_file_path_is_a_config_error(tmp_path):
    a_file = tmp_path / "notadir"
    a_file.write_text("x", encoding="utf-8")
    cfg = Config(state_dir=a_file)
    with pytest.raises(ConfigError) as exc:
        cfg.ensure_dirs()
    assert "DIRECTORY" in str(exc.value)


# --------------------------------------------------------------------------
# The override file is validated, and safety numbers are clamped
# --------------------------------------------------------------------------
def _override(tmp_path, monkeypatch, payload) -> None:
    path = tmp_path / "override.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                    encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(path))
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))


def test_override_cannot_raise_the_account_ceilings(tmp_path, monkeypatch):
    """A JSON override rewrote every safety number with no validation and no
    record: ceiling 100000, burst 10000, minimum gap 0. Every rule
    `references/account.md` calls non-negotiable, off, silently."""
    _override(tmp_path, monkeypatch, {"budgets": {
        "daily_resolve_ceiling": 100000,
        "burst_ceiling": 10000,
        "min_resolve_gap_sec": 0,
        "daily_join_ceiling": 500,
        "burst_window_sec": 1,
    }})
    cfg = load()
    assert cfg.budgets.daily_resolve_ceiling == 180
    assert cfg.budgets.burst_ceiling == 8
    assert cfg.budgets.min_resolve_gap_sec == 30.0
    assert cfg.budgets.daily_join_ceiling == 3
    assert cfg.budgets.burst_window_sec == 600
    assert len(cfg.override_notes) == 5
    assert any("clamped" in note for note in cfg.override_notes)


def test_override_may_still_tighten_the_account_budgets(tmp_path, monkeypatch):
    _override(tmp_path, monkeypatch, {"budgets": {
        "daily_resolve_ceiling": 20, "min_resolve_gap_sec": 90.0,
    }})
    cfg = load()
    assert cfg.budgets.daily_resolve_ceiling == 20
    assert cfg.budgets.min_resolve_gap_sec == 90.0
    assert cfg.override_notes == []


def test_override_null_is_refused_instead_of_failing_inside_the_ceiling_check(tmp_path, monkeypatch):
    """`null` used to land on the dataclass and raise
    `TypeError: '>=' not supported between 'int' and 'NoneType'` from inside
    `check_resolve` -- the one place that must not fail."""
    _override(tmp_path, monkeypatch, {"budgets": {"daily_resolve_ceiling": None}})
    with pytest.raises(ConfigError) as exc:
        load()
    assert "daily_resolve_ceiling" in str(exc.value)


def test_override_rejects_wrong_types_and_unknown_keys(tmp_path, monkeypatch):
    _override(tmp_path, monkeypatch, {"budgets": {"burst_ceiling": "lots"}})
    with pytest.raises(ConfigError):
        load()
    _override(tmp_path, monkeypatch, {"budgets": {"burst_celing": 4}})
    with pytest.raises(ConfigError) as exc:
        load()
    assert "burst_celing" in str(exc.value)
    _override(tmp_path, monkeypatch, {"budgets": {"max_requests_per_run": -1}})
    with pytest.raises(ConfigError):
        load()


def test_malformed_override_file_raises_config_error_not_json_error(tmp_path, monkeypatch):
    """It used to escape as a raw JSONDecodeError; this module promises
    ConfigError for anything the operator has to fix."""
    _override(tmp_path, monkeypatch, '{"budgets": {"burst_ceiling": 4,')
    with pytest.raises(ConfigError) as exc:
        load()
    assert "not valid JSON" in str(exc.value)


# --------------------------------------------------------------------------
# The scrubber is case-blind and shape-blind
# --------------------------------------------------------------------------
def test_redact_catches_an_uppercase_api_hash():
    upper = "0123456789ABCDEF0123456789ABCDEF"
    assert upper not in redact("TELEGRAM_API_HASH=" + upper)
    assert upper not in redact("api_hash: " + upper)
    assert upper not in redact("the hash is " + upper + " apparently")
    mixed = "0123456789AbCdEf0123456789aBcDeF"
    assert mixed not in redact("API_HASH=" + mixed)


def test_redact_catches_the_key_forms_of_api_id_and_session():
    assert "9182736" not in redact("api_id: 9182736")
    assert "9182736" not in redact("TELEGRAM_API_ID=9182736")
    assert "9182736" not in redact('{"api_id": 9182736}')
    assert "sekret" not in redact("string_session = sekret_value_here")
    assert "sekret" not in redact("bot_token=sekret_value_here")


def test_a_bare_api_id_is_not_scrubbable_and_we_say_so():
    """Kept honest by a test rather than by a comment alone.

    An api_id is a bare integer. Any pattern wide enough to catch it would eat
    member counts, message ids and years out of every fetched post. It is
    covered by its KEY forms only; the real defence is never letting it into a
    string. This test exists so that nobody later "fixes" it by adding a digit
    pattern and quietly starts redacting the corpus.
    """
    prose = "The channel has 9182736 members and the post id is 1234567."
    assert redact(prose) == prose


def test_redact_catches_a_telegram_bot_token():
    token = "1234567890:AAH" + "b" * 32
    assert token not in redact("we tried " + token + " by mistake")


def test_a_credential_file_written_through_redact_leaks_nothing(tmp_path):
    """The exact three-way leak, reproduced and closed.

    All three values reached disk before -- an uppercase api_hash (the hex
    pattern had no `re.I`), a session string not starting with Telethon's
    version byte, and the api_id.
    """
    session = "2BVtsOMTQ5" + "Q" * 60
    api_hash = "FEDCBA9876543210FEDCBA9876543210"
    api_id = "12345678"
    blob = (
        "TELEGRAM_API_ID=" + api_id + "\n"
        "TELEGRAM_API_HASH=" + api_hash + "\n"
        "TELEGRAM_SESSION=" + session + "\n"
        "login failed with api_hash " + api_hash + " for session " + session + "\n"
    )
    out = tmp_path / "error.md"
    out.write_text(redact(blob), encoding="utf-8")
    written = out.read_text(encoding="utf-8")
    assert api_hash not in written
    assert "TELEGRAM_API_ID=" + api_id not in written
    assert "TELEGRAM_SESSION=" + session not in written


# --------------------------------------------------------------------------
# redact_obj(obj, *, protect=())
# --------------------------------------------------------------------------
def test_redact_obj_protect_passes_named_values_through_untouched():
    """Our own fields stay redacted, fetched content does not get
    rewritten. A 32-hex string inside a post is a post."""
    hexish = "0123456789abcdef0123456789abcdef"
    payload = {
        "ok": True,
        "detail": "internal note " + hexish,
        "results": [
            {"text": "the md5 of the fixture is " + hexish, "author_name": "Bar Bar"},
        ],
        "messages": ["another " + hexish],
    }
    protected = redact_obj(payload, protect={"text", "messages", "results", "author_name"})
    assert protected["results"][0]["text"].endswith(hexish)
    assert protected["messages"][0].endswith(hexish)
    assert hexish not in protected["detail"]           # ours is still scrubbed

    # and with no `protect`, the same structure is scrubbed throughout
    plain = redact_obj(payload)
    assert hexish not in json.dumps(plain, ensure_ascii=False)


def test_redact_obj_protect_reaches_all_the_way_down():
    hexish = "abcdefabcdefabcdefabcdefabcdef00"
    payload = {"a": {"b": [{"c": {"text": hexish}}]}}
    out = redact_obj(payload, protect=("text",))
    assert out["a"]["b"][0]["c"]["text"] == hexish


def test_redact_obj_protect_never_wins_over_a_credential_key():
    out = redact_obj({"TELEGRAM_SESSION": "anything"}, protect={"TELEGRAM_SESSION"})
    assert out["TELEGRAM_SESSION"] == "<redacted>"


def test_redact_obj_single_argument_calls_are_unchanged():
    hexish = "0123456789abcdef0123456789abcdef"
    assert redact_obj({"x": hexish})["x"] == "<redacted>"
    assert redact_obj(["a", hexish]) == ["a", "<redacted>"]
    assert redact_obj(7) == 7
    assert redact_obj(None) is None
    # protect accepts any iterable of names, including a bare string
    assert redact_obj({"text": hexish}, protect="text")["text"] == hexish


def test_redact_obj_survives_a_non_string_key():
    """`k.upper()` used to raise on any dict whose key was not a string."""
    assert redact_obj({1: "fine", (2, 3): "also fine"})[1] == "fine"


# --------------------------------------------------------------------------
# The Windows file primitives the state layer is built on
# --------------------------------------------------------------------------
def test_read_bytes_shared_matches_a_plain_read(tmp_path):
    path = tmp_path / "f.bin"
    payload = b"\x00\x01hello\xff\n"
    path.write_bytes(payload)
    assert config_module.read_bytes_shared(path) == payload
    with pytest.raises(FileNotFoundError):
        config_module.read_bytes_shared(tmp_path / "missing.bin")


def test_atomic_write_survives_a_reader_holding_the_file_open(tmp_path):
    """CPython's `open()` does not request FILE_SHARE_DELETE, so on NTFS a
    reader blocks `os.replace` over the file it is reading -- and the ledger
    is exactly the file other processes read. A single unretried `os.replace` raised
    PermissionError [WinError 5] here and the FloodWait freeze never reached
    disk, leaving a `<name>.<pid>.tmp` orphan behind each time.
    """
    path = tmp_path / "ledger.json"
    path.write_text("{}", encoding="utf-8")
    reader = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time;p=sys.argv[1];fh=open(p,'rb');"
         "open(p+'.ready','w').close();time.sleep(1.0);fh.close()",
         str(path)],
    )
    try:
        for _ in range(300):
            if (tmp_path / "ledger.json.ready").exists():
                break
            time.sleep(0.01)
        config_module.atomic_write_text(path, '{"written": true}')
        assert json.loads(path.read_text(encoding="utf-8"))["written"] is True
        assert [p.name for p in tmp_path.glob("*.tmp")] == []
    finally:
        reader.kill()
        reader.wait()


def test_atomic_write_leaves_no_temp_file_when_it_fails(tmp_path, monkeypatch):
    path = tmp_path / "f.json"

    def always_busy(src, dst):
        raise PermissionError(5, "held open")

    monkeypatch.setattr(config_module.os, "replace", always_busy)
    with pytest.raises(config_module.AtomicWriteFailed):
        config_module.atomic_write_text(path, "x", attempts=2, delay=0.001)
    assert list(tmp_path.glob("*")) == []
    assert not path.exists()


def test_file_guard_is_exclusive_and_refuses_rather_than_proceeding(tmp_path):
    path = tmp_path / "g.lock"
    first = config_module.FileGuard(path, timeout=0.2, poll=0.01)
    first.acquire()
    try:
        with pytest.raises(config_module.GuardBusy):
            config_module.FileGuard(path, timeout=0.2, poll=0.01).acquire()
    finally:
        first.release()
    assert not path.exists()
    second = config_module.FileGuard(path, timeout=0.2)
    second.acquire()          # free again
    second.release()


def test_file_guard_breaks_a_guard_whose_owner_died(tmp_path):
    path = tmp_path / "g.lock"
    path.write_text("999999 0", encoding="utf-8")
    os.utime(path, (time.time() - 500, time.time() - 500))
    guard = config_module.FileGuard(path, timeout=0.5, stale_after=60.0)
    guard.acquire()
    guard.release()


# ==========================================================================
# Repair regressions
# ==========================================================================
# One test per finding, each of them red against the code as it was.


def test_state_pointing_at_a_file_that_does_not_exist_yet_is_refused(tmp_path, monkeypatch):
    """The existing-file check only fires on a machine where the file is
    already there. On a fresh one `mkdir` made a DIRECTORY called
    `sources.jsonl`, and the registry then lived at `sources.jsonl/sources.jsonl`."""
    target = tmp_path / "st" / "sources.jsonl"
    monkeypatch.setenv(ENV_STATE, str(target))
    with pytest.raises(ConfigError) as exc:
        load()
    assert "sources.jsonl" in str(exc.value)
    assert str(tmp_path / "st") in str(exc.value)      # names what was meant
    assert not target.exists()                          # and creates nothing

    for name in ("resolve-ledger.json", "account.lock", "notes.txt"):
        monkeypatch.setenv(ENV_STATE, str(tmp_path / "st" / name))
        with pytest.raises(ConfigError):
            load()

    # A plain directory name is still fine, however deep, and is still created.
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "deep" / "state"))
    cfg = load()
    cfg.ensure_dirs()
    assert (tmp_path / "deep" / "state" / "pace").is_dir()


def test_each_state_directory_failure_gets_the_advice_that_fits_it(tmp_path):
    """Every `OSError` used to get "point it at a folder, not at a file",
    so a UNC host that does not resolve and a permission failure were both
    diagnosed as the operator having named a file."""
    from config import _mkdir_advice

    target = Path(r"C:\somewhere\state")
    denied = PermissionError(13, "Access is denied")
    denied.winerror = 5
    unreachable = OSError(22, "The network path was not found")
    unreachable.winerror = 53

    assert "may not create" in _mkdir_advice(denied, target)
    assert "not at a file" not in _mkdir_advice(denied, target)
    assert "could not be reached" in _mkdir_advice(unreachable, target)
    assert "not at a file" not in _mkdir_advice(unreachable, target)
    # The case the sentence was written for still gets it.
    assert "not at a file" in _mkdir_advice(NotADirectoryError(20, "no"), target)


def test_a_misspelled_override_container_is_an_error_not_a_silence(tmp_path, monkeypatch):
    """`budgets.<typo>` was a loud error naming every budget; `{"budget":
    {...}}` and `{"Budgets": {...}}` were accepted and did nothing, and the
    operator believed all three depth rows had moved."""
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    for payload in ({"budget": {"max_rounds": 9}},
                    {"Budgets": {"max_rounds": 9}},
                    {"budgets": {"max_rounds": 9}, "allow_paid_stars": True}):
        path = tmp_path / "ovr.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(path))
        with pytest.raises(ConfigError) as exc:
            load()
        assert "top-level" in str(exc.value)

    # And the real container still works.
    path = tmp_path / "ok.json"
    path.write_text(json.dumps({"budgets": {"max_rounds": 9}}), encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(path))
    assert load().budgets.max_rounds == 9


def test_a_topics_vocabulary_that_is_not_there_is_refused(tmp_path, monkeypatch):
    """m11. A typo cost BOTH the override and the shipped default: every source
    admitted after that carried no topics, with nothing saying why."""
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    path = tmp_path / "ovr.json"
    path.write_text(json.dumps({"topics_vocabulary": str(tmp_path / "nope.json")}),
                    encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(path))
    with pytest.raises(ConfigError) as exc:
        load()
    assert "topics_vocabulary" in str(exc.value)

    real = tmp_path / "topics.json"
    real.write_text(json.dumps({"rent": ["аренда"]}), encoding="utf-8")
    path.write_text(json.dumps({"topics_vocabulary": str(real)}), encoding="utf-8")
    assert load().topics_vocabulary == real


def test_a_clamped_account_ceiling_is_announced_and_not_only_recorded(
        tmp_path, monkeypatch, capsys):
    """The explanation went into `Config.override_notes`, whose only reader
    is nothing that prints. An operator who set
    1000 resolves was clamped to 180 with no word anywhere."""
    import config as config_module

    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    path = tmp_path / "ovr.json"
    path.write_text(json.dumps({"budgets": {"daily_resolve_ceiling": 100000,
                                            "min_resolve_gap_sec": 0}}),
                    encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(path))
    monkeypatch.setattr(config_module, "_ANNOUNCED", set())
    capsys.readouterr()
    cfg = load()
    err = capsys.readouterr().err

    assert cfg.budgets.daily_resolve_ceiling == 180
    assert cfg.budgets.min_resolve_gap_sec == 30.0
    assert "daily_resolve_ceiling" in err and "180" in err
    assert "min_resolve_gap_sec" in err
    # The warning goes to stderr, never into the JSON an agent parses.
    assert capsys.readouterr().out == ""
    assert len(cfg.override_notes) == 2


def test_local_time_follows_the_machine_and_can_be_pinned(monkeypatch):
    """A fixed `timezone(timedelta(...))` was compiled into `run.py` and
    `registry.py`: right on one machine, an hour out on any other, and unable to
    follow DST. Run-folder names and `first_seen` all ride on it."""
    from datetime import datetime

    import config as config_module

    monkeypatch.delenv(config_module.ENV_TZ, raising=False)
    machine = datetime.now().astimezone().utcoffset()
    assert config_module.local_tz().utcoffset(None) == machine
    assert config_module.today_local() == datetime.now().astimezone().date().isoformat()

    monkeypatch.setenv(config_module.ENV_TZ, "+00:00")
    assert config_module.now_local().endswith("+00:00")
    monkeypatch.setenv(config_module.ENV_TZ, "-03:30")
    assert config_module.now_local().endswith("-03:30")
    monkeypatch.setenv(config_module.ENV_TZ, "UTC")
    assert config_module.now_local().endswith("+00:00")
    monkeypatch.setenv(config_module.ENV_TZ, "local")
    assert config_module.local_tz().utcoffset(None) == machine

    monkeypatch.setenv(config_module.ENV_TZ, "Kyzylorda")
    with pytest.raises(ConfigError):
        config_module.local_tz()
    monkeypatch.setenv(config_module.ENV_TZ, "+99:00")
    with pytest.raises(ConfigError):
        config_module.local_tz()


def test_a_config_cannot_narrow_the_free_surface_gap_either(tmp_path, monkeypatch,
                                                            capsys):
    """`Pacer` enforces the 2-4 s floor; this is the half that says so.

    `Pacer` accepted `min_gap=0, max_gap=0` in silence -- 8 waits in 0.046 s,
    measured -- and `min_gap_sec` / `max_gap_sec` are reachable from an override
    file, so one config sent every request to `t.me` out back to back. Widening
    is still allowed: slowing down is never the dangerous direction.
    """
    import config as config_module

    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    monkeypatch.setattr(config_module, "_ANNOUNCED", set())
    path = tmp_path / "ovr.json"
    path.write_text(json.dumps({"budgets": {"min_gap_sec": 0, "max_gap_sec": 0}}),
                    encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(path))
    capsys.readouterr()
    cfg = load()
    err = capsys.readouterr().err

    assert (cfg.budgets.min_gap_sec, cfg.budgets.max_gap_sec) == (2.0, 4.0)
    assert "min_gap_sec" in err and "max_gap_sec" in err
    assert len(cfg.override_notes) == 2

    path.write_text(json.dumps({"budgets": {"min_gap_sec": 6, "max_gap_sec": 12}}),
                    encoding="utf-8")
    wider = load()
    assert (wider.budgets.min_gap_sec, wider.budgets.max_gap_sec) == (6, 12)
    assert wider.override_notes == []


def test_the_shipped_budget_numbers_skill_md_prints_are_the_ones_in_the_code():
    """A doc that states four numbers and tests two of them is a doc that drifts.
    `max_pages_per_channel` 25 could be rewritten to 2500 with the suite green."""
    import config as config_module

    shipped = config_module.Budgets()
    assert shipped.max_pages_per_channel == 25
    assert shipped.max_requests_per_run == 400
    assert shipped.max_rounds == 3
    assert shipped.min_new_posts_per_round == 3
    assert shipped.daily_resolve_ceiling == 180
    assert shipped.daily_join_ceiling == 3


# ==========================================================================
# Repair regressions, second round
# ==========================================================================
# The first three pin behaviour the code already had and NO test held: a
# mutation run deleted each of these rules and the suite stayed green (703
# passed, three times). They are green against the old code by construction --
# what they kill is the mutant, so the mutation they survive is their proof,
# not an old-code failure count.


def test_two_writers_of_one_file_never_collide_on_the_temp_name(tmp_path):
    """`<name>.tmp` turns two concurrent saves into a FileNotFoundError.

    The rule lives in `atomic_write_text`'s docstring and nowhere else: the temp
    name carries the pid AND a random token, because one fixed `.tmp` collides
    between processes and a pid-only name collides between threads. Measured
    with the name mutated to `<name>.tmp`: 16 failed writes out of 480, each
    one a `FileNotFoundError` on the rename -- an `OSError`, not an
    `AtomicWriteFailed`, so it escapes `ResolveLedger._write_locked` and
    `Run.finish` and reaches `tg.py` as exit 7 about a file nobody typed.
    """
    import threading

    target = tmp_path / "run.json"
    failures: list[BaseException] = []
    barrier = threading.Barrier(6)

    def writer(n: int) -> None:
        barrier.wait()
        for i in range(40):
            try:
                config_module.atomic_write_text(
                    target, json.dumps({"writer": n, "i": i}))
            except BaseException as exc:          # noqa: BLE001 -- what it is IS the point
                failures.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert failures == [], f"{len(failures)} of 240 writes collided: {failures[:3]}"
    assert json.loads(target.read_text(encoding="utf-8"))["writer"] in range(6)
    assert [p.name for p in tmp_path.glob("*.tmp")] == []


def test_append_lines_starts_on_a_fresh_line_after_an_interrupted_write(tmp_path):
    """One interrupted append destroys TWO records, not one.

    `Registry._heal_last_line` has this test; `config.append_lines`, which is
    what `posts.jsonl`, `fetchlog.jsonl` and `registry-delta.jsonl` go through,
    did not -- and the mutation (`needs_newline = False`) welded the healthy
    record onto the half-written one with the whole suite green. Downstream sees
    only "posts.jsonl has unparseable line(s)", one line short of two posts.
    """
    path = tmp_path / "posts.jsonl"
    path.write_bytes(b'{"id": 1}\n{"id": 2, "text": "half')

    written = config_module.append_lines(path, [json.dumps({"id": 3, "text": "healthy"})])

    assert written == 1
    lines = path.read_text(encoding="utf-8").splitlines()
    readable = []
    for line in lines:
        try:
            readable.append(json.loads(line))
        except ValueError:
            continue
    assert [r["id"] for r in readable] == [1, 3], (
        "the record appended after an interrupted write must parse on its own")
    assert lines[-1] == json.dumps({"id": 3, "text": "healthy"})


def test_file_guard_does_not_break_a_guard_that_is_still_fresh(tmp_path):
    """The staleness rule, from the other side: this half was missing.

    `test_file_guard_breaks_a_guard_whose_owner_died` proves only the breaking
    direction, and the exclusivity test keeps an OS handle open -- which on NTFS
    makes `unlink` fail, so the mutation (`if False: return False`) was refused
    by the platform rather than by the rule and the suite stayed green. Here the
    lock file is written OUT OF BAND, with no handle held on it: a holder on a
    shared volume, a holder mid read-modify-write, any non-CPython writer. Then
    breaking it is a decision the code has to refuse on its own, on every
    platform.
    """
    lock = tmp_path / "sources.jsonl.write"
    lock.write_text("99999 %.3f\n" % time.time(), encoding="utf-8")
    fresh_mtime = lock.stat().st_mtime

    with pytest.raises(config_module.GuardBusy):
        config_module.FileGuard(lock, timeout=0.5, stale_after=120.0,
                                poll=0.01).acquire()

    assert lock.exists(), "a zero-second-old guard was broken and a second writer let in"
    assert lock.stat().st_mtime == fresh_mtime      # not touched, not re-created


def test_a_guard_is_released_only_by_the_object_that_holds_it(tmp_path):
    """`release()` deleted the guard file whether or not it owned it.

    The `unlink` sat outside the "did we ever acquire" test, so a `FileGuard`
    that never acquired -- and one whose file had been broken as stale by
    another process while this one was suspended -- removed the CURRENT
    holder's guard, after which a third process could `O_EXCL`-create it and two
    writers ran the read-modify-write at once. The primitive under
    `Registry.append`, `Run.write_posts`, `guarded_append` and
    `ResolveLedger._mutate`.

    Driven through a holder that keeps no OS handle on the file, because a
    holder that keeps one is protected by NTFS refusing the `unlink` and the
    same test would say nothing on POSIX.
    """
    lock = tmp_path / "sources.jsonl.write"

    # (a) a guard this object never took
    lock.write_text("4242 %.3f\n" % time.time(), encoding="utf-8")
    config_module.FileGuard(lock, timeout=0.2).release()
    assert lock.exists(), "a FileGuard that never acquired deleted the holder's guard"
    lock.unlink()

    # (b) A holds it, is broken as stale by B, and then finishes its own work
    first = config_module.FileGuard(lock, timeout=0.2, stale_after=120.0)
    first.acquire()
    lock.write_text("4242 %.3f\n" % time.time(), encoding="utf-8")   # B took it
    first.release()
    assert lock.exists(), "the released guard belonged to the process still writing"

    # ... and it still refuses a third writer, which is the whole point
    with pytest.raises(config_module.GuardBusy):
        config_module.FileGuard(lock, timeout=0.3, stale_after=120.0,
                                poll=0.01).acquire()

    # The holder that DOES own it still cleans up after itself.
    lock.unlink()
    mine = config_module.FileGuard(lock, timeout=0.2)
    mine.acquire()
    mine.release()
    assert not lock.exists()


# --------------------------------------------------------------------------
# Every path out of the environment is anchored
# --------------------------------------------------------------------------
def test_a_relative_state_variable_does_not_follow_the_shell(tmp_path, monkeypatch):
    """Two shells, two ledgers, two account locks.

    A relative `TELEGRAM_RESEARCH_STATE=state/_telegram` was used verbatim, so a
    run in `a/` wrote a FLOOD_PREMIUM_WAIT_36468 freeze and a run in `b/` read
    `frozen_for() == 0`, saw an empty resolve ledger, and took a DIFFERENT
    `account.lock`: both processes holding "the" account lock at once. It is
    verbatim the disaster the state directory exists to prevent; the repair had
    been applied to the default and not to the variable.
    """
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)
    monkeypatch.delenv(ENV_CREDENTIAL, raising=False)
    monkeypatch.setenv(ENV_STATE, "state/_telegram")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    monkeypatch.chdir(tmp_path / "a")
    from_a = load()
    monkeypatch.chdir(tmp_path / "b")
    from_b = load()

    assert from_a.state_dir == from_b.state_dir
    assert from_a.lock_path == from_b.lock_path
    assert from_a.ledger_path == from_b.ledger_path
    assert from_a.state_dir.is_absolute()
    assert tmp_path not in from_a.state_dir.parents
    # anchored where the default is anchored: on the home directory
    assert from_a.state_dir == Path.home() / "state" / "_telegram"


def test_a_relative_state_variable_does_not_follow_the_project_root_either(
        tmp_path, monkeypatch):
    """The half of it that the move of the default could have re-opened.

    `repo_root()` is allowed to fall back to the working directory now -- a run
    folder belongs in the project the operator is in -- so anchoring the state
    variable there would put the shell back in charge of the ledger the moment
    the skill is installed outside a project. State has its own anchor,
    `anchored_state_path`, and this pins it: whatever `repo_root()` says, and
    wherever the shell is, a relative `TELEGRAM_RESEARCH_STATE` lands under HOME.
    """
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)
    monkeypatch.delenv(ENV_CREDENTIAL, raising=False)
    monkeypatch.setenv(ENV_STATE, "tg-state")

    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()

    monkeypatch.setattr(config_module, "repo_root", lambda: project_a)
    monkeypatch.chdir(project_a)
    from_a = load()
    monkeypatch.setattr(config_module, "repo_root", lambda: project_b)
    monkeypatch.chdir(project_b)
    from_b = load()

    assert from_a.state_dir == from_b.state_dir == Path.home() / "tg-state"
    assert from_a.lock_path == from_b.lock_path
    assert project_a not in from_a.state_dir.parents
    assert project_b not in from_b.state_dir.parents


def test_a_machine_that_cannot_say_where_home_is_names_the_variable_that_fixes_it(
        tmp_path, monkeypatch):
    """No HOME, no USERPROFILE: `Path.home()` raises, and a guess would be the
    original defect wearing a third hat -- a temp folder, the skill's own
    folder or the working directory, each of them a second empty ledger nobody
    sees. The default refuses and names `TELEGRAM_RESEARCH_STATE`; the variable
    itself still works, because an operator who set it has already answered the
    question."""
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)
    monkeypatch.delenv(ENV_CREDENTIAL, raising=False)

    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(config_module.Path, "home", staticmethod(no_home))

    monkeypatch.delenv(ENV_STATE, raising=False)
    with pytest.raises(ConfigError) as exc:
        load()
    msg = str(exc.value)
    assert ENV_STATE in msg
    assert "home" in msg.lower()

    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    assert load().state_dir == tmp_path / "state"


def test_a_tilde_in_a_path_variable_is_expanded_not_taken_literally(tmp_path,
                                                                     monkeypatch):
    """`~/tg-state` gave `state_dir = ~\\tg-state`, `is_absolute() == False`: a
    literal directory named `~` under whatever the working directory was. There
    was no `expanduser()` anywhere in `load()`."""
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)
    monkeypatch.setenv(ENV_STATE, "~/tg-state")
    monkeypatch.setenv(ENV_CREDENTIAL, "~/telegram.env")
    monkeypatch.chdir(tmp_path)

    cfg = load()

    home = Path.home()
    assert cfg.state_dir == (home / "tg-state").resolve()
    assert cfg.credential_path == (home / "telegram.env").resolve()
    assert "~" not in str(cfg.state_dir)
    assert not (tmp_path / "~").exists()


def test_the_credential_and_override_variables_are_anchored_too(tmp_path, monkeypatch):
    """`TELEGRAM_RESEARCH_STATE`, `TELEGRAM_RESEARCH_ENV`, `TELEGRAM_RESEARCH_CONFIG`
    -- and any other. A credential or a budget file that follows the shell is
    the same defect wearing another variable's name.

    `repo_root()` is pinned here rather than left to whatever the machine
    running the tests looks like: on a copy installed outside any project it
    answers `Path.cwd()`, and a test that quietly depended on it would pass on
    one machine and fail on another.
    """
    monkeypatch.setattr(config_module, "repo_root", lambda: tmp_path / "project")
    (tmp_path / "project").mkdir()
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    ovr = tmp_path / "ovr.json"
    ovr.write_text(json.dumps({"budgets": {"max_rounds": 6}}), encoding="utf-8")
    cred = tmp_path / "telegram.env"
    cred.write_text("TELEGRAM_API_ID=1\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", "ovr.json")
    monkeypatch.setenv(ENV_CREDENTIAL, "telegram.env")
    with pytest.raises(ConfigError):
        load()                       # relative: anchored on the repo, not on cwd

    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(ovr))
    monkeypatch.setenv(ENV_CREDENTIAL, str(cred))
    cfg = load()
    assert cfg.budgets.max_rounds == 6
    assert cfg.credential_path == cred.resolve()
    assert cfg.credential_path.is_absolute()


# --------------------------------------------------------------------------
# What the reader does with a folder and with a BOM
# --------------------------------------------------------------------------
def test_an_override_variable_pointing_at_a_directory_is_a_sentence(tmp_path,
                                                                    monkeypatch):
    """`path.exists()` is true for a directory and `read_text` on one
    raises `PermissionError` / `IsADirectoryError` -- neither a `ValueError` nor
    a `ConfigError`, against a docstring promising exactly the opposite. Pointing
    the variable at the folder that holds the JSON is a one-character slip."""
    folder = tmp_path / "cfgdir"
    folder.mkdir()
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(folder))

    with pytest.raises(ConfigError) as exc:
        load()
    assert "cfgdir" in str(exc.value)
    assert "folder" in str(exc.value) or "FILE" in str(exc.value)


def test_a_utf8_bom_does_not_hide_the_budgets_or_the_credential(tmp_path, monkeypatch):
    """Notepad and `Set-Content -Encoding UTF8` under Windows
    PowerShell 5.1 both write a BOM by default.

    The override file became "not valid JSON" and every budget silently stayed
    at its shipped default. The credential file was worse: the BOM welded onto
    the first key, so the operator was told the file "is missing
    TELEGRAM_API_ID" about a file that has it on line 1 -- live mode
    unreachable, and the message pointing at the wrong thing.
    """
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    ovr = tmp_path / "ovr.json"
    ovr.write_text(json.dumps({"budgets": {"max_rounds": 6}}), encoding="utf-8-sig")
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(ovr))
    assert ovr.read_bytes()[:3] == b"\xef\xbb\xbf"

    assert load().budgets.max_rounds == 6

    cred = tmp_path / "telegram.env"
    cred.write_text(
        "TELEGRAM_API_ID=123456\n"
        "TELEGRAM_API_HASH=abcdef0123456789abcdef0123456789\n"
        "TELEGRAM_SESSION=1AQANOTsomefakesessionvalue1234567890\n",
        encoding="utf-8-sig",
    )
    values = read_credentials(Config(credential_path=cred))
    assert values["TELEGRAM_API_ID"] == "123456"
    assert sorted(values) == ["TELEGRAM_API_HASH", "TELEGRAM_API_ID", "TELEGRAM_SESSION"]


def test_a_credential_variable_pointing_at_a_directory_is_a_sentence(tmp_path):
    """The same hole as the one above, in the reader next door: `exists()` is true for
    a folder and the `read_text` that follows raised a raw OSError."""
    folder = tmp_path / "envdir"
    folder.mkdir()
    with pytest.raises(ConfigError) as exc:
        read_credentials(Config(credential_path=folder))
    assert "envdir" in str(exc.value)


# --------------------------------------------------------------------------
# The numbers an override file is allowed to set
# --------------------------------------------------------------------------
def test_a_non_finite_budget_is_refused_instead_of_disabling_a_ceiling(tmp_path,
                                                                       monkeypatch):
    """`json.loads` accepts `NaN`, `Infinity` and `-Infinity` by default and
    all three pass `isinstance(x, float)`. `NaN < 0` is False, so a NaN walked
    through the negativity check onto the dataclass -- and NaN makes every
    comparison false, so the ceiling holding it refuses nothing at all."""
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    path = tmp_path / "ovr.json"
    for literal in ("NaN", "Infinity", "-Infinity"):
        path.write_text('{"budgets": {"min_gap_sec": %s}}' % literal, encoding="utf-8")
        monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(path))
        with pytest.raises(ConfigError) as exc:
            load()
        assert "min_gap_sec" in str(exc.value)
        assert "finite" in str(exc.value)


def test_an_override_cannot_raise_the_history_ceiling(tmp_path, monkeypatch, capsys):
    """The account's getHistory ceiling was BORROWED from
    `max_requests_per_run`, a free-surface budget an override file may set to
    anything: `{"budgets": {"max_requests_per_run": 100000}}` -- a plausible
    thing to write for a big free crawl -- raised the account's ceiling from 400
    to 100000 with `override_notes` empty, so nothing was printed on stderr and
    nothing appeared in `tg.py config`. The knob `_history_ceiling` actually
    looked for did not exist on `Budgets` at all, so it could not be set."""
    monkeypatch.setattr(config_module, "_ANNOUNCED", set())
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    path = tmp_path / "ovr.json"
    path.write_text(json.dumps({"budgets": {"max_history_requests_per_run": 100000}}),
                    encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(path))
    capsys.readouterr()

    cfg = load()
    err = capsys.readouterr().err

    assert config_module.Budgets().max_history_requests_per_run == 400
    assert cfg.budgets.max_history_requests_per_run == 400
    assert "max_history_requests_per_run" in err        # clamped, and said out loud
    assert len(cfg.override_notes) == 1

    # ... and lowering it is still allowed, as with every other account ceiling
    path.write_text(json.dumps({"budgets": {"max_history_requests_per_run": 5}}),
                    encoding="utf-8")
    tightened = load()
    assert tightened.budgets.max_history_requests_per_run == 5
    assert tightened.override_notes == []


def test_an_unmapped_drive_is_not_diagnosed_as_a_file():
    """Second order. `_WIN_UNREACHABLE` did not contain 3, the code
    Windows actually returns for an unmapped drive letter, so even once
    `ensure_dirs()` was called the case fell through to the generic advice."""
    from config import _mkdir_advice

    unmapped = OSError(2, "The system cannot find the path specified")
    unmapped.winerror = 3
    advice = _mkdir_advice(unmapped, Path(r"Q:\nowhere\state"))
    assert "could not be reached" in advice
    assert "not at a file" not in advice


# --------------------------------------------------------------------------
# A path written INSIDE an override file is anchored on that file
# --------------------------------------------------------------------------
def test_a_relative_vocabulary_is_read_next_to_its_own_file_not_next_to_the_shell(
        tmp_path, monkeypatch):
    """`Path(vocab).exists()` asked the shell, and the shell answered differently
    in every directory.

    Measured on the code as it stood, with one override file kept outside the
    repository naming `topics/ru.json`:

    * from the config's own folder -- the operator's vocabulary, stored as the
      RELATIVE `topics\\ru.json`, so every later reader resolves it against
      *their* cwd in turn;
    * from anywhere else -- a ConfigError saying it does not exist;
    * from a folder that happens to hold a `topics/ru.json` of its own -- **that
      one, loaded in silence**. The vocabulary decides the topics of everything
      the run admits to the knowledge base, so this is a wrong answer rather
      than a failure, and nothing anywhere says so.

    The anchor is the override file's own directory: a relative path inside a
    configuration file is that file's statement about its own neighbourhood.
    `repo_root()` -- the anchor `anchored_env_path` uses, because a variable has
    no file to be relative to -- would send exactly the config-outside-the-repo
    case above INTO the repository, where a file of that name may exist and be
    something else entirely.
    """
    outside = tmp_path / "elsewhere"
    (outside / "topics").mkdir(parents=True)
    real = outside / "topics" / "ru.json"
    real.write_text(json.dumps({"rent": ["аренда"]}), encoding="utf-8")
    ovr = outside / "telegram.json"
    ovr.write_text(json.dumps({"topics_vocabulary": "topics/ru.json"}), encoding="utf-8")

    decoy_dir = tmp_path / "somewhere-else"
    (decoy_dir / "topics").mkdir(parents=True)
    (decoy_dir / "topics" / "ru.json").write_text(json.dumps({"WRONG": ["decoy"]}),
                                                  encoding="utf-8")

    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(ovr))
    monkeypatch.delenv(ENV_CREDENTIAL, raising=False)

    seen = []
    for where in (outside, tmp_path, decoy_dir):
        monkeypatch.chdir(where)
        seen.append(load().topics_vocabulary)

    assert seen[0] == seen[1] == seen[2] == real.resolve()
    assert all(p.is_absolute() for p in seen)
    assert json.loads(seen[2].read_text(encoding="utf-8")) == {"rent": ["аренда"]}, (
        "the vocabulary sitting in the shell's directory was loaded instead")


def test_a_vocabulary_that_is_missing_names_the_path_it_actually_looked_at(
        tmp_path, monkeypatch):
    """A path that resolves to nothing used to get a silent `.exists()` false and
    an error quoting the operator's own two words back at them -- so the message
    said the vocabulary was missing rather than that it had been looked for
    somewhere they did not intend."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    ovr = outside / "telegram.json"
    ovr.write_text(json.dumps({"topics_vocabulary": "topics/missing.json"}),
                   encoding="utf-8")
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(ovr))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError) as exc:
        load()
    message = str(exc.value)
    assert str(outside / "topics" / "missing.json") in message, (
        "the error must name the path that was tried, not the two words in the file")
    assert str(outside) in message              # ... and the anchor it was tried against
    assert "topics/missing.json" in message     # ... and what the operator wrote


def test_an_absolute_vocabulary_is_still_taken_exactly_as_written(tmp_path, monkeypatch):
    """Anchoring is for RELATIVE values. An absolute path -- and a `~` one -- names
    what it names, wherever the override file lives."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    real = tmp_path / "vocab.json"
    real.write_text(json.dumps({"rent": ["аренда"]}), encoding="utf-8")
    ovr = outside / "telegram.json"
    ovr.write_text(json.dumps({"topics_vocabulary": str(real)}), encoding="utf-8")
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    monkeypatch.setenv("TELEGRAM_RESEARCH_CONFIG", str(ovr))
    monkeypatch.chdir(outside)
    assert load().topics_vocabulary == real.resolve()


# --------------------------------------------------------------------------
# repo_root(): a global install has no project above it
# --------------------------------------------------------------------------
# Measured 2026-09-05 by running the acceptance gate from a project folder with
# the skill installed globally: the run folder was created at
# `<home>/.claude/telegram-runs/...` instead of under the project. The walk up
# from the skill's own folder had found `~/.claude/CLAUDE.md` -- Claude Code's
# own user-memory file, which most operators have -- and declared the home
# directory to be the project. The control case, the same layout with no memory
# file above the skill, landed correctly in the working directory.
def _install(root: Path, *segments: str) -> Path:
    """A skill folder at `root/<segments>`, carrying the two things
    `skill_root()` would find: a `scripts/` folder and a `SKILL.md`."""
    skill = root.joinpath(*segments)
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("skill", encoding="utf-8")
    return skill


def test_a_global_install_never_walks_up_into_the_home_directory(
        tmp_path, monkeypatch):
    """`~/.claude/CLAUDE.md` is user memory, not a project marker.

    This is the whole defect: `npx skills add -g` puts the skill in
    `~/.claude/skills/telegram-research`, the walk up hits `~/.claude/CLAUDE.md`
    one level above it, and every run folder is then created in the operator's
    HOME while the operator is standing in a repository. The working directory
    is the only honest answer for an install that belongs to the machine.
    """
    home = tmp_path / "home"
    skill = _install(home, ".claude", "skills", "telegram-research")
    (home / ".claude" / "CLAUDE.md").write_text("# memory", encoding="utf-8")
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(config_module, "skill_root", lambda: skill)
    monkeypatch.chdir(project)

    root = config_module.repo_root()
    assert root == project.resolve(), (
        "a run folder belongs in the project the operator is in, never ~/.claude")
    assert home not in root.parents and root != home


def test_an_agents_install_is_global_too(tmp_path, monkeypatch):
    """The installer's default target is `.agents/skills/`, symlinked into
    `.claude/skills/`, so the walk has to be stopped for both names -- and a
    `~/.agents` that somebody keeps in git is the same trap wearing `.git`."""
    home = tmp_path / "home"
    skill = _install(home, ".agents", "skills", "telegram-research")
    (home / ".agents" / ".git").mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# project", encoding="utf-8")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(config_module, "skill_root", lambda: skill)
    monkeypatch.chdir(project)

    assert config_module.repo_root() == project.resolve()


def test_a_project_install_still_finds_the_project_above_it(
        tmp_path, monkeypatch):
    """The control case, and the behaviour the fix must not cost.

    A skill installed INTO a repository is found by the walk from wherever the
    operator happens to `cd` to, which is the point of walking at all: the run
    folder follows the project, not the shell.
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("# memory", encoding="utf-8")
    project = tmp_path / "work" / "some-repo"
    skill = _install(project, ".claude", "skills", "telegram-research")
    (project / ".git").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(config_module, "skill_root", lambda: skill)
    monkeypatch.chdir(elsewhere)

    assert config_module.repo_root() == project.resolve()
    assert config_module.is_global_install(skill) is False


def test_a_global_install_with_no_memory_file_answers_the_same_way(
        tmp_path, monkeypatch):
    """The control the bug report ran: nothing above the skill to find, so the
    working directory was already the answer. The fix must not change it -- both
    layouts now take the same branch, so a `~/.claude/CLAUDE.md` appearing later
    cannot move anybody's run folder."""
    home = tmp_path / "home"
    skill = _install(home, ".claude", "skills", "telegram-research")
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(config_module, "skill_root", lambda: skill)
    monkeypatch.chdir(project)

    assert config_module.repo_root() == project.resolve()


def test_is_global_install_reads_the_home_directory_and_not_the_name(
        tmp_path, monkeypatch):
    """`.claude` is a marker only under HOME. A repository of somebody's that
    holds a `.claude/skills/` or a `.agents/skills/` -- which is exactly what a
    PROJECT install looks like -- is not global and must keep its walk.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    assert config_module.is_global_install(home / ".claude") is True
    assert config_module.is_global_install(
        home / ".claude" / "skills" / "telegram-research") is True
    assert config_module.is_global_install(
        home / ".agents" / "skills" / "telegram-research") is True
    assert config_module.is_global_install(
        tmp_path / "repo" / ".claude" / "skills" / "telegram-research") is False
    assert config_module.is_global_install(
        tmp_path / "repo" / ".agents" / "skills" / "telegram-research") is False
    assert config_module.is_global_install(home / "code" / "project") is False


def test_a_machine_without_a_home_directory_does_not_break_the_walk(
        tmp_path, monkeypatch):
    """`Path.home()` raises where neither HOME nor USERPROFILE is set.
    `repo_root()` is not where that refusal belongs -- `home_dir()` already
    makes it, loudly, wherever the STATE is decided. Here the answer is simply
    "not a global install", and the ordinary walk runs.
    """
    def _no_home():
        raise RuntimeError("Could not determine home directory.")

    project = tmp_path / "project"
    skill = _install(project, "skills", "telegram-research")
    (project / ".git").mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(_no_home))
    monkeypatch.setattr(config_module, "skill_root", lambda: skill)
    monkeypatch.chdir(project)

    assert config_module.is_global_install(skill) is False
    assert config_module.repo_root() == project.resolve()


def test_the_run_root_a_global_install_loads_is_the_working_directory(
        tmp_path, monkeypatch):
    """The end of the chain, which is where the defect was actually seen:
    `load()` with no `--root` takes `cfg.root` from `repo_root()`, and
    `<root>/telegram-runs/` is where every run folder is created."""
    home = tmp_path / "home"
    skill = _install(home, ".claude", "skills", "telegram-research")
    (home / ".claude" / "CLAUDE.md").write_text("# memory", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)
    monkeypatch.delenv(ENV_CREDENTIAL, raising=False)
    monkeypatch.setenv(ENV_STATE, str(tmp_path / "state"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(config_module, "skill_root", lambda: skill)
    monkeypatch.chdir(project)

    cfg = load()
    assert cfg.root == project.resolve()
    assert home not in cfg.root.parents


# --------------------------------------------------------------------------
# The write guard's two numbers, and the loop that waits on them
# --------------------------------------------------------------------------
def test_a_dead_writers_guard_is_broken_before_the_wait_runs_out(tmp_path):
    """The wait was 20 s and the staleness threshold 120 s, so the waiter always
    gave up a hundred seconds before the guard could be broken.

    Measured with a writer killed mid-write: every subsequent write -- the
    registry, `posts.jsonl`, `fetchlog.jsonl`, `run.json`, `queries.json`,
    notes -- refused at exactly 20.0 s, and went on refusing for two minutes.
    Each refusal burned 20 s of wall clock, some of it after the network
    requests it was trying to record had already been paid for.
    """
    import os
    import time

    import config as config_module

    assert config_module.GUARD_TIMEOUT > config_module.GUARD_STALE_AFTER, (
        "a waiter that gives up before the guard it waits on can be broken "
        "turns one killed writer into a total outage"
    )
    target = tmp_path / "state" / "posts.jsonl"
    target.parent.mkdir(parents=True)

    # Exactly what a killed writer leaves: the guard file, and nobody holding it.
    dead = config_module.file_guard(target, label="posts")
    dead.path.write_bytes(b"999999 0.000 deadbeef\n")
    aged = time.time() - (config_module.GUARD_STALE_AFTER + 1.0)
    os.utime(dead.path, (aged, aged))

    started = time.monotonic()
    assert config_module.guarded_append(target, ['{"kind": "fetch"}']) == 1
    assert time.monotonic() - started < 5.0, "the write waited out the threshold"
    assert target.read_text(encoding="utf-8").strip() == '{"kind": "fetch"}'


def test_a_guard_whose_stat_keeps_failing_times_out_instead_of_spinning(
    tmp_path, monkeypatch
):
    """`_break_if_stale` answered True whenever `stat` merely FAILED, and the
    caller's answer to True is `continue` -- straight back to `os.open`, with no
    deadline check and no sleep in between.

    Measured with a `stat` that raises every time: 200 000 turns of the loop in
    13 s at a 0.2 s timeout, one core at 100 %, and the bounded wait never
    returned at all. A guard that reads as stale and does not go away is a
    broken `stat`, not a slot about to free.
    """
    import time

    import config as config_module

    guard_path = tmp_path / "state" / "sources.jsonl.write"
    guard_path.parent.mkdir(parents=True)
    guard_path.write_bytes(b"1 0.000 aabbccdd\n")

    real_stat = Path.stat
    turns = {"n": 0}

    def broken_stat(self, *args, **kwargs):
        if str(self) == str(guard_path):
            turns["n"] += 1
            if turns["n"] > 2000:
                raise AssertionError(
                    f"`acquire` span {turns['n']} times on one unreadable guard "
                    "with its timeout never firing"
                )
            raise PermissionError(5, "access is denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", broken_stat)
    guard = config_module.FileGuard(guard_path, timeout=0.2, poll=0.01,
                                    stale_after=20.0, label="registry")
    started = time.monotonic()
    with pytest.raises(config_module.GuardBusy) as refusal:
        guard.acquire()
    assert time.monotonic() - started < 5.0
    assert turns["n"] < 200, f"{turns['n']} stat calls for a 0.2 s wait"

    # And the refusal says what to do about it: which file, and that it clears
    # itself. It used to name the file and stop there.
    message = str(refusal.value)
    assert str(guard_path) in message
    assert "20 s old" in message, message


# --------------------------------------------------------------------------
# What the state directory is readable by
# --------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_state_directory_is_not_readable_by_the_whole_machine(tmp_path):
    """The peer cache under the state directory holds an `access_hash` per
    peer, and the directory was created with the ambient umask -- world-readable
    on any shared Linux box."""
    import stat as stat_module

    cfg = Config(state_dir=tmp_path / "state")
    cfg.ensure_dirs()
    mode = stat_module.S_IMODE(os.stat(cfg.state_dir).st_mode)
    assert mode == 0o700, oct(mode)


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows HANDLE path")
def test_a_failed_open_osfhandle_does_not_leak_the_windows_handle(
    tmp_path, monkeypatch
):
    """`read_bytes_shared` opens a raw HANDLE and hands it to
    `msvcrt.open_osfhandle`, which takes ownership of it. If that call raised,
    nothing closed the handle -- and the fallback then opened the file AGAIN
    through `open()`, so the process kept a handle on it for good. That is the
    exact condition this function exists to avoid."""
    import ctypes
    import ctypes.wintypes as wintypes
    import msvcrt

    import config as config_module

    path = tmp_path / "sources.jsonl"
    path.write_bytes(b"x" * 32)

    def refuse(*args, **kwargs):
        raise OSError("no free file descriptor")

    monkeypatch.setattr(msvcrt, "open_osfhandle", refuse)

    # The signatures are declared, not assumed: with the default `c_int`
    # return type `GetCurrentProcess` hands back a truncated pseudo-handle,
    # the count call fails, and the counter reads 0 both times -- a leak test
    # that cannot see a leak.
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetProcessHandleCount.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(wintypes.DWORD)]
    k32.GetProcessHandleCount.restype = wintypes.BOOL

    def handles() -> int:
        count = wintypes.DWORD()
        assert k32.GetProcessHandleCount(k32.GetCurrentProcess(),
                                         ctypes.byref(count))
        return count.value

    before = handles()
    for _ in range(300):
        assert config_module.read_bytes_shared(path) == b"x" * 32
    leaked = handles() - before
    assert leaked < 50, f"{leaked} handles left open by 300 failed reads"


def test_the_three_state_guards_read_both_constants_at_call_time(tmp_path, monkeypatch):
    """Half a pair frozen at import is the same outage, harder to see.

    `timeout > stale_after` is the invariant that lets a waiter outlive a dead
    writer's guard. The registry, the resolve ledger and the history log took
    the timeout as a default argument -- bound once, at import -- while reading
    its partner at call time, so moving both moved only one and inverted the
    pair silently.
    """
    import config as config_module
    import account as account_module
    import registry as registry_module
    import resolve as resolve_module

    monkeypatch.setattr(config_module, "GUARD_TIMEOUT", 0.3)
    monkeypatch.setattr(config_module, "GUARD_STALE_AFTER", 0.2)

    guards = [
        registry_module.Registry(tmp_path / "sources.jsonl")._guard(),
        resolve_module.ResolveLedger(tmp_path / "ledger.json", daily_ceiling=1,
                                     burst_ceiling=1, burst_window=1, min_gap=1,
                                     join_ceiling=1)._guard(),
        account_module.HistoryLog(tmp_path / "history.json")._guard(),
    ]
    for guard in guards:
        assert (guard.timeout, guard.stale_after) == (0.3, 0.2), guard.label
        assert guard.timeout > guard.stale_after
