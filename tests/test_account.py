"""Tests for the MTProto fallback.

Offline, stdlib only, no Telethon and no network. That is not a limitation of
the suite, it is the point of it: the account path cannot be tested against the
account, because testing it against the account is the accident it exists to
prevent. Everything below runs through `FakeTransport`, which records what would
have been sent so that a test can assert on what was NOT sent.

`allow_live=True` appears in several tests. It never means a live call: the
transport is always the fake or the `telethon` stub at the bottom of this file.

**How the second switch is driven, and why it is not a keyword.** Until
2026-08-25 these tests handed `AccountSession` an `env={...}` dict, which meant
the switch could be turned on from code with the variable absent from the
environment -- and that keyword was itself a finding, because two switches whose
whole argument is that they are independent must not both live in one line of
Python. The check now reads `os.environ` at the moment of use, so the tests
drive `os.environ` -- but through `sealed_environment`, which replaces it with an
ordinary dict for the duration of each test. A dict write is not `putenv`:
`TELEGRAM_RESEARCH_ALLOW_LIVE` is never set in the real process environment, no
child process can inherit it, and no test can leak it into the next one.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

SCRIPTS = (Path(__file__).resolve().parent.parent
           / "skills" / "telegram-research" / "scripts")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import account                                  # noqa: E402
import config as configmod                      # noqa: E402
import tgparse                                  # noqa: E402
from resolve import (                           # noqa: E402
    AccountBusy,
    AccountLock,
    BudgetExhausted,
    ResolveFrozen,
    ResolveLedger,
    peer_is_usable,
    session_fingerprint,
)

# Shaped like a real StringSession (leading "1", long, opaque) so that it hits
# the same redaction pattern a real one would.
# A real Telethon v1 StringSession is 353 characters: the version byte plus
# base64 of the packed dc id, ip, port and 256-byte auth key. Measured, not
# guessed. A 73-character stand-in let the bare-session redaction pattern be
# loose enough to eat ordinary URLs out of fetched corpus text and no test
# noticed, so the fixture is the real length now.
FAKE_SESSION = "1" + "A9zK" * 88
FP = session_fingerprint(FAKE_SESSION)

GROUP = {"exists": True, "type": "group", "username": "tdlibchat"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def sealed_environment(monkeypatch):
    """Every test in this file runs against a private COPY of the environment.

    Two jobs. It removes `TELEGRAM_RESEARCH_ALLOW_LIVE` before every test, so no
    answer here can depend on how the host happens to be configured -- the
    lesson of the test that asserted "Telethon is not installed", which is a fact
    about the interpreter rather than about the code. And it makes `os.environ` an ordinary dict, so a
    test that turns the live switch on performs a dict write rather than a
    `putenv`: the real process environment is untouched, a subprocess started by
    a test inherits nothing, and the next test starts clean.

    It also resets the per-process history counter, which is deliberately global:
    a run is a process, not an `AccountSession` object.
    """
    fake_env = dict(os.environ)
    fake_env.pop(account.ENV_ALLOW_LIVE, None)
    monkeypatch.setattr(os, "environ", fake_env)
    account.reset_history_requests_this_process()
    yield fake_env
    account.reset_history_requests_this_process()


def turn_the_environment_switch_on(value: str = "1") -> None:
    """Set the second switch for this test only, inside the sealed environment.

    The assertion is what keeps the promise in the module docstring: if the
    autouse fixture ever stops applying, this refuses rather than quietly setting
    the live switch on the machine that runs the suite.
    """
    assert type(os.environ) is dict, (
        "the environment is not sealed -- refusing to set the live switch for real"
    )
    os.environ[account.ENV_ALLOW_LIVE] = value


def make_cfg(tmp_path) -> configmod.Config:
    cfg = configmod.Config(state_dir=Path(tmp_path))
    cfg.ensure_dirs()
    return cfg


def make_ledger(cfg, **kw) -> ResolveLedger:
    params = dict(daily_ceiling=180, burst_ceiling=100, burst_window=600,
                  min_gap=0.0, join_ceiling=3)
    params.update(kw)
    return ResolveLedger(cfg.ledger_path, **params)


def make_session(tmp_path, transport=None, *, live=False, cfg=None, ledger=None,
                 options=None, sleeps=None, fingerprint=FP):
    cfg = cfg if cfg is not None else make_cfg(tmp_path)
    transport = transport if transport is not None else account.FakeTransport()
    ledger = ledger if ledger is not None else make_ledger(cfg)
    sleeps = [] if sleeps is None else sleeps
    if live:
        turn_the_environment_switch_on()
    return account.AccountSession(
        transport,
        cfg=cfg,
        ledger=ledger,
        fingerprint=fingerprint,
        dry_run=not live,
        allow_live=live,
        options=options,
        sleep=sleeps.append,
    )


def req(username="tdlibchat", evidence=GROUP, cached_peer=None) -> account.SourceRequest:
    if evidence is GROUP:
        evidence = dict(GROUP, username=username)
    return account.SourceRequest(username=username, evidence=evidence,
                                 cached_peer=cached_peer)


def good_peer(peer_id=1006503122, access_hash=42, fingerprint=FP) -> dict:
    return {"id": peer_id, "access_hash": access_hash,
            "auth_session_fingerprint": fingerprint}


# --------------------------------------------------------------------------
# 1. The lazy import
# --------------------------------------------------------------------------
def test_module_imports_on_a_machine_without_telethon():
    """A fresh interpreter imports the module and never pulls Telethon in.

    The whole free surface of the skill would go down with the account path if
    this regressed, which is why it is checked in a subprocess rather than by
    trusting this process not to have imported it earlier.
    """
    code = (
        "import sys; sys.path.insert(0, %r); import account; "
        "print('telethon' in sys.modules)" % str(SCRIPTS)
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False"


def test_missing_telethon_error_names_the_pin_and_refuses_to_install(monkeypatch):
    """The import failure is forced, not borrowed from the environment.

    This test used to assert that Telethon was absent here, which is a fact about
    the interpreter and not about the code. Whether the library is installed is a
    property of the machine the suite runs on, never a switch this skill owns, so
    an assertion like that inverts the moment anything outside the skill installs
    it -- and it inverts exactly when the account path becomes live-capable and
    these tests start to matter. `None` in `sys.modules` makes `import telethon`
    raise whether or not the library is present, so the refusal is exercised
    either way.
    """
    monkeypatch.setitem(sys.modules, "telethon", None)
    with pytest.raises(account.TelethonMissing) as exc:
        account._import_telethon()
    message = str(exc.value)
    assert "telethon==1.44.0" in message
    assert "does not install it" in message
    assert "3.14" in message                      # support is unclaimed, and says so
    assert "codeberg.org/Lonami/Telethon" in message


def test_telethon_presence_is_reported_without_importing_it(monkeypatch):
    """Status has to answer "is the library there", and asking must not import it.

    `assert telethon_installed() in (True, False)` was a tautology: the function
    returns a bool, so hardcoding it to `True` left the suite green -- confirmed
    by mutation. The answer is asserted against a stubbed `find_spec` rather than
    against the host environment, because the previous version of this same test
    asserted a fact about the environment instead: installed or not is a property
    of the interpreter, not a switch this skill owns, and a test that reads it
    inverts the moment anything outside the skill installs the library.
    """
    import importlib.util

    monkeypatch.delitem(sys.modules, "telethon", raising=False)
    asked: list[str] = []

    def found(name):
        asked.append(name)
        return SimpleNamespace(name=name)

    monkeypatch.setattr(importlib.util, "find_spec", found)
    assert account.telethon_installed() is True
    assert asked == ["telethon"]

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert account.telethon_installed() is False

    def refuses(name):
        raise ValueError("__spec__ is not set")

    monkeypatch.setattr(importlib.util, "find_spec", refuses)
    assert account.telethon_installed() is False    # unanswerable is "no", not a crash
    assert "telethon" not in sys.modules            # find_spec executes nothing


# --------------------------------------------------------------------------
# 2. The seam
# --------------------------------------------------------------------------
def test_transport_protocol_carries_the_reading_operations_and_no_join():
    """Reading cannot call a join the protocol does not have.

    The set is checked EXACTLY, not by containment: the guarantee is that
    `join_group` is absent, and `assert "join_group" not in public` passes for a
    protocol that grew `delete_messages` as well.
    """
    public = {name for name, value in vars(account.Transport).items()
              if callable(value) and not name.startswith("_")}
    assert public == {"resolve_username", "fetch_history",
                      "search_contacts", "search_messages"}
    assert isinstance(account.FakeTransport(), account.Transport)


def test_fake_transport_scripts_all_three_answers():
    fake = account.FakeTransport().answer_with("tdlibchat", 1006503122)
    assert fake.resolve_username("tdlibchat")["id"] == 1006503122
    fake.not_found("nobody")
    with pytest.raises(account.PeerNotFound):
        fake.resolve_username("nobody")
    fake.flood_on("hanoi_chats", 36468)
    with pytest.raises(account.FloodWait) as exc:
        fake.resolve_username("hanoi_chats")
    assert exc.value.seconds == 36468


# --------------------------------------------------------------------------
# 3. allow_paid_stars is never passed
# --------------------------------------------------------------------------
def test_config_cannot_switch_on_spending(tmp_path):
    """Both merge layers say spend; the outgoing call still carries None."""
    cfg = make_cfg(tmp_path)
    cfg.call_options = {"allow_paid_stars": 5000}          # a config file trying it
    fake = account.FakeTransport().answer_with("tdlibchat", 1006503122)
    session = make_session(tmp_path, fake, live=True, cfg=cfg,
                           options={"allow_paid_stars": True})   # a caller trying it
    with session as live:
        peer = live.resolve(req())
    assert peer["id"] == 1006503122
    assert fake.resolve_calls[0]["options"]["allow_paid_stars"] is None
    assert account.ALLOW_PAID_STARS is None


def test_forced_value_is_written_after_every_layer():
    merged = account.free_call_options({"allow_paid_stars": 1}, {"allow_paid_stars": 2},
                                       {"limit": 100})
    assert merged["allow_paid_stars"] is None
    assert merged["limit"] == 100


def test_transport_boundary_refuses_a_hand_built_paid_call():
    """The second gate, for a caller that skipped free_call_options entirely."""
    fake = account.FakeTransport().answer_with("tdlibchat", 1)
    with pytest.raises(account.PaidCallRefused):
        fake.resolve_username("tdlibchat", options={"allow_paid_stars": 1})
    with pytest.raises(account.PaidCallRefused):
        fake.fetch_history({"id": 1}, options={"allow_paid_stars": 1})
    assert fake.resolve_calls == [] and fake.history_calls == []


def test_history_call_also_carries_the_forced_value(tmp_path):
    fake = account.FakeTransport().with_history(7, [{"id": 3, "text": "hi"}])
    session = make_session(tmp_path, fake, live=True, options={"allow_paid_stars": 9})
    with session as live:
        page = live.history(req(), good_peer(peer_id=7))
    assert page.messages == [{"id": 3, "text": "hi"}]
    assert fake.history_calls[0]["options"]["allow_paid_stars"] is None


# --------------------------------------------------------------------------
# 4. Evidence from the free surface, or no resolve
# --------------------------------------------------------------------------
def test_resolve_without_evidence_is_an_exception_not_a_log_line(tmp_path):
    fake = account.FakeTransport().answer_with("tdlibchat", 1)
    session = make_session(tmp_path, fake, live=True)
    with session as live:
        with pytest.raises(account.EvidenceRequired):
            live.resolve(req(evidence=None))
    assert fake.resolve_calls == []


@pytest.mark.parametrize("evidence", [
    {"exists": False, "type": "group"},           # the card said the name is free
    {"exists": None, "type": "group"},            # nobody looked
    {"exists": True, "type": None},               # the card did not parse
    {"exists": True, "type": "unknown"},
    {"exists": True, "type": "group", "username": "someoneelse"},
])
def test_weak_evidence_is_refused(tmp_path, evidence):
    fake = account.FakeTransport().answer_with("tdlibchat", 1)
    session = make_session(tmp_path, fake, live=True)
    with session as live:
        with pytest.raises(account.EvidenceRequired):
            live.resolve(req(evidence=evidence))
    assert fake.resolve_calls == []


def test_a_real_peercard_is_accepted_as_evidence(tmp_path):
    """The measured tdlibchat card, straight off the fixture values."""
    card = tgparse.PeerCard(username="tdlibchat", exists=True, type="group",
                            members=16674, online=362)
    fake = account.FakeTransport().answer_with("tdlibchat", 1006503122)
    session = make_session(tmp_path, fake, live=True)
    with session as live:
        peer = live.resolve(account.SourceRequest.from_card(card))
    assert peer["id"] == 1006503122
    assert len(fake.resolve_calls) == 1


def test_a_channel_never_reaches_the_account_path(tmp_path):
    """Measured: /s/<name> gives a channel 20 messages a page for nothing."""
    fake = account.FakeTransport().answer_with("durov", 1)
    session = make_session(tmp_path, fake, live=True)
    card = tgparse.PeerCard(username="durov", exists=True, type="channel",
                            members=11110268)
    with session as live:
        with pytest.raises(account.WrongSurface):
            live.resolve(account.SourceRequest.from_card(card))
    assert fake.resolve_calls == []


# --------------------------------------------------------------------------
# 5. The ledger, before and after
# --------------------------------------------------------------------------
def test_a_successful_resolve_is_counted(tmp_path):
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport().answer_with("tdlibchat", 1006503122)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        live.resolve(req())
    assert ledger.read().resolves == 1


def test_a_failed_resolve_is_counted_too(tmp_path):
    """Whether Telegram charges less for a miss is not established either way."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport().not_found("tdlibchat")
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(account.PeerNotFound):
            live.resolve(req())
    assert ledger.read().resolves == 1


def test_the_check_happens_before_the_call_not_after(tmp_path):
    """A ledger already at its ceiling means the transport is never touched."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg, daily_ceiling=0)
    fake = account.FakeTransport().answer_with("tdlibchat", 1)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(BudgetExhausted):
            live.resolve(req())
    assert fake.resolve_calls == []


def test_a_frozen_ledger_from_an_earlier_run_blocks_every_resolve(tmp_path):
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    ledger.freeze(36468, "an earlier run met the wall")
    fake = account.FakeTransport().answer_with("tdlibchat", 1)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(ResolveFrozen):
            live.resolve(req())
    assert fake.resolve_calls == []


def test_the_minimum_gap_pauses_instead_of_refusing(tmp_path):
    """30 s between resolves is a pause; the ceilings are what refuse."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg, min_gap=30.0)
    ledger.record_resolve("earlier", ok=True)
    sleeps: list[float] = []
    fake = account.FakeTransport().answer_with("tdlibchat", 1)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger,
                      sleeps=sleeps) as live:
        live.resolve(req())
    assert len(fake.resolve_calls) == 1
    assert sleeps and 25.0 <= sleeps[0] <= 30.0


# --------------------------------------------------------------------------
# 6. The first FloodWait stops resolving for the whole run
# --------------------------------------------------------------------------
def test_first_floodwait_stops_every_remaining_resolve(tmp_path):
    """Four sources, one flood, exactly one resolve attempted.

    Two of them already hold a valid peer and keep working, which is the half of
    the rule that matters: a freeze must not turn into a dead run.
    """
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport()
    fake.answer_with("hanoi_chats", 2832).flood_on("tdlibchat", 36468)
    requests = [
        req("cached_one", cached_peer=good_peer(peer_id=11)),
        req("tdlibchat"),                          # floods here
        req("hanoi_chats"),                        # must not be attempted
        req("cached_two", cached_peer=good_peer(peer_id=22)),
    ]
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        report = live.prepare(requests)

    assert [c["username"] for c in fake.resolve_calls] == ["tdlibchat"]
    assert sorted(report.peers) == ["cached_one", "cached_two"]
    assert sorted(report.from_cache) == ["cached_one", "cached_two"]
    assert sorted(report.skipped) == ["hanoi_chats", "tdlibchat"]
    assert report.frozen is True

    state = ledger.read()
    assert state.resolves == 1                     # the flooded call still counted
    assert ledger.summary()["frozen"] is True
    assert ledger.summary()["frozen_for_sec"] > 36000
    assert "FloodWait on resolve of @tdlibchat" in state.frozen_reason


class UnpersistedFreezeLedger(ResolveLedger):
    """A ledger whose `freeze` records the call but writes nothing.

    It stands in for the disk being read-only, or the write racing another
    process. The run-local latch has to stop the burst on its own, without the
    file agreeing, because the burst is what did the damage on 2026-08-20.
    """

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.freezes: list[tuple] = []

    def freeze(self, seconds, reason, now=None):
        self.freezes.append((int(seconds), reason))
        return self.read()


def test_the_first_floodwait_calls_freeze_and_latches_the_run(tmp_path):
    cfg = make_cfg(tmp_path)
    ledger = UnpersistedFreezeLedger(cfg.ledger_path, daily_ceiling=180,
                                     burst_ceiling=100, burst_window=600,
                                     min_gap=0.0, join_ceiling=3)
    fake = account.FakeTransport()
    fake.answer_with("hanoi_chats", 2832).flood_on("tdlibchat", 36468)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        report = live.prepare([req("tdlibchat"), req("hanoi_chats")])

    assert ledger.freezes == [(36468, "FloodWait on resolve of @tdlibchat")]
    assert [c["username"] for c in fake.resolve_calls] == ["tdlibchat"]
    assert report.frozen is True
    assert "hanoi_chats" in report.skipped


def test_the_freeze_survives_into_the_next_session(tmp_path):
    """The count lives on disk, so the second caller cannot un-know the ban."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport().flood_on("tdlibchat", 36468)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        live.prepare([req("tdlibchat")])

    fake2 = account.FakeTransport().answer_with("hanoi_chats", 2832)
    with make_session(tmp_path, fake2, live=True, cfg=cfg,
                      ledger=make_ledger(cfg)) as second:
        report = second.prepare([req("hanoi_chats")])
    assert fake2.resolve_calls == []
    assert report.frozen is True


# --------------------------------------------------------------------------
# 7. The peer cache and the login-session fingerprint
# --------------------------------------------------------------------------
def test_a_usable_cached_peer_costs_nothing(tmp_path):
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport().answer_with("tdlibchat", 1)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        report = live.prepare([req(cached_peer=good_peer())])
    assert report.from_cache == ["tdlibchat"]
    assert fake.resolve_calls == []
    assert ledger.read().resolves == 0


def test_a_fingerprint_mismatch_is_discarded_silently_and_re_earned(tmp_path):
    """Telegram: access hashes are not reusable across login sessions."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    stale = good_peer(peer_id=999, fingerprint=session_fingerprint("1previous-login"))
    fake = account.FakeTransport().answer_with("tdlibchat", 1006503122)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        report = live.prepare([req(cached_peer=stale)])
    assert report.cache_discarded == ["tdlibchat"]
    assert report.resolved == ["tdlibchat"]
    assert report.peers["tdlibchat"]["id"] == 1006503122
    assert len(fake.resolve_calls) == 1
    assert report.skipped == {}                    # discarding is not an error


def test_a_resolved_peer_is_stamped_with_the_current_session(tmp_path):
    fake = account.FakeTransport().answer_with("tdlibchat", 1006503122, 777)
    with make_session(tmp_path, fake, live=True) as live:
        peer = live.resolve(req())
    assert peer == {"id": 1006503122, "access_hash": 777,
                    "auth_session_fingerprint": FP}
    assert peer_is_usable(peer, FP) is True
    assert peer_is_usable(peer, "another-login") is False


def test_history_refuses_a_peer_from_another_login(tmp_path):
    fake = account.FakeTransport().with_history(7, [{"id": 1}])
    with make_session(tmp_path, fake, live=True) as live:
        with pytest.raises(account.PeerUnusable):
            live.history(req(), good_peer(peer_id=7, fingerprint="stale"))
    assert fake.history_calls == []


# --------------------------------------------------------------------------
# 8. Joining is explicit, budgeted, and unreachable from a read
# --------------------------------------------------------------------------
def test_reading_and_searching_never_join(tmp_path):
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport().answer_with("tdlibchat", 7).with_history(7, [{"id": 1}])
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        report = live.prepare([req()])
        live.history(req(), report.peers["tdlibchat"])
    assert fake.join_calls == []
    assert ledger.read().joins == 0


def test_no_read_path_can_even_name_a_join():
    """Structural, not behavioural: only one method mentions joining at all.

    A behavioural test proves that today's read path does not join. This proves
    that a future one cannot start joining without the diff showing up in a
    method whose name is `join_group`.
    """
    tokens = {"check_join", "record_join", "join_group"}
    tree = ast.parse(Path(account.__file__).read_text(encoding="utf-8"))
    session_cls = next(node for node in tree.body
                       if isinstance(node, ast.ClassDef) and node.name == "AccountSession")
    mentions = {}
    for func in session_cls.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names: set[str] = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.update(t for t in tokens if t in node.value)
        if names & tokens:
            mentions[func.name] = names & tokens
    assert set(mentions) == {"join_group"}, mentions


def test_join_is_counted_against_its_own_daily_ceiling(tmp_path):
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg, join_ceiling=1)
    fake = account.FakeTransport()
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        assert live.join_group(req(), good_peer())["joined"] is True
        with pytest.raises(BudgetExhausted):
            live.join_group(req("hanoi_chats"), good_peer(peer_id=2832))
    assert len(fake.join_calls) == 1
    assert ledger.read().joins == 1


def test_join_does_not_spend_the_resolve_budget(tmp_path):
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport()
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        live.join_group(req(), good_peer())
    assert fake.resolve_calls == []
    assert ledger.read().resolves == 0


# --------------------------------------------------------------------------
# 9. One writer per account
# --------------------------------------------------------------------------
def test_a_second_caller_gets_account_busy_not_a_slower_run(tmp_path):
    cfg = make_cfg(tmp_path)
    first = account.AccountSession(
        account.FakeTransport(), cfg=cfg, ledger=make_ledger(cfg), fingerprint=FP,
        lock=AccountLock(cfg.lock_path),
    )
    second = account.AccountSession(
        account.FakeTransport(), cfg=cfg, ledger=make_ledger(cfg), fingerprint=FP,
        lock=AccountLock(cfg.lock_path),
    )
    with first:
        with pytest.raises(AccountBusy):
            second.__enter__()
    with second:                                   # the lock is free again
        pass


def test_nothing_spends_outside_the_lock(tmp_path):
    fake = account.FakeTransport().answer_with("tdlibchat", 1)
    session = make_session(tmp_path, fake, live=True)
    with pytest.raises(account.AccountError):
        session.resolve(req())
    with pytest.raises(account.AccountError):
        session.prepare([req()])
    assert fake.resolve_calls == []


# --------------------------------------------------------------------------
# 10. The credential reaches nothing
# --------------------------------------------------------------------------
def test_an_error_carrying_a_session_string_does_not_print_it():
    err = account.TransportError(f"login failed with session {FAKE_SESSION} at dc2")
    assert FAKE_SESSION not in str(err)
    assert "<redacted>" in str(err)


def test_a_leaking_transport_is_wrapped_and_scrubbed(tmp_path):
    class LeakyTransport(account.Transport):
        def resolve_username(self, username, *, options=None):
            raise RuntimeError(f"reset by peer; TELEGRAM_SESSION={FAKE_SESSION}")

        def fetch_history(self, peer, *, limit=100, offset_id=0, options=None):
            return []

    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    with make_session(tmp_path, LeakyTransport(), live=True, cfg=cfg,
                      ledger=ledger) as live:
        with pytest.raises(account.TransportError) as exc:
            live.resolve(req())
    assert FAKE_SESSION not in str(exc.value)
    assert FAKE_SESSION not in repr(exc.value)
    # The cause is dropped on purpose: a traceback prints causes, and the cause
    # is the object that was carrying the credential.
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True
    assert ledger.read().resolves == 1             # it still happened, so it counts


def test_reports_and_summaries_carry_no_credential(tmp_path):
    class LeakyNotFound(account.Transport):
        def resolve_username(self, username, *, options=None):
            raise account.PeerNotFound(f"no such name, session {FAKE_SESSION}")

        def fetch_history(self, peer, *, limit=100, offset_id=0, options=None):
            return []

    with make_session(tmp_path, LeakyNotFound(), live=True) as live:
        report = live.prepare([req()])
        blob = repr(report.as_dict()) + repr(live.summary())
    assert FAKE_SESSION not in blob
    assert "<redacted>" in repr(report.as_dict())
    assert live.summary()["session_fingerprint"] == FP     # a hash, not the secret
    assert FAKE_SESSION not in FP


# --------------------------------------------------------------------------
# 11. Dry run is the default, and live needs both switches
# --------------------------------------------------------------------------
def test_dry_run_is_the_default(tmp_path):
    session = account.AccountSession(account.FakeTransport(), cfg=make_cfg(tmp_path),
                                     fingerprint=FP)
    assert session.dry_run is True


def test_dry_run_makes_zero_transport_calls(tmp_path):
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport().answer_with("tdlibchat", 7).with_history(7, [{"id": 1}])
    with make_session(tmp_path, fake, cfg=cfg, ledger=ledger) as dry:
        report = dry.prepare([req(), req("hanoi_chats")])
        page = dry.history(req(), good_peer(peer_id=7))
        joined = dry.join_group(req(), good_peer(peer_id=7))

    assert fake.resolve_calls == [] and fake.history_calls == [] and fake.join_calls == []
    assert sorted(report.would_resolve) == ["hanoi_chats", "tdlibchat"]
    assert report.peers == {}
    assert page.dry_run is True and page.messages == []
    assert page.would["call"] == "messages.getHistory"
    assert page.would["options"]["allow_paid_stars"] is None
    assert joined == {"would_join": "tdlibchat", "dry_run": True}
    # Nothing was spent, so nothing was recorded.
    state = ledger.read()
    assert state.resolves == 0 and state.joins == 0


def test_dry_run_still_refuses_what_live_would_refuse(tmp_path):
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg, daily_ceiling=0)
    with make_session(tmp_path, cfg=cfg, ledger=ledger) as dry:
        report = dry.prepare([req()])
    assert report.would_resolve == []
    assert "ceiling" in report.skipped["tdlibchat"]


def test_live_mode_needs_the_code_switch(tmp_path):
    turn_the_environment_switch_on()
    with pytest.raises(account.LiveModeRefused):
        account.AccountSession(account.FakeTransport(), cfg=make_cfg(tmp_path),
                               fingerprint=FP, dry_run=False)


def test_live_mode_needs_the_environment_switch(tmp_path):
    with pytest.raises(account.LiveModeRefused) as exc:
        account.AccountSession(account.FakeTransport(), cfg=make_cfg(tmp_path),
                               fingerprint=FP, dry_run=False, allow_live=True)
    assert account.ENV_ALLOW_LIVE in str(exc.value)


def test_live_mode_needs_both_switches_together(tmp_path):
    turn_the_environment_switch_on()
    session = account.AccountSession(
        account.FakeTransport(), cfg=make_cfg(tmp_path), fingerprint=FP,
        dry_run=False, allow_live=True,
    )
    assert session.dry_run is False


def test_dry_run_never_reads_the_credential_file(tmp_path):
    """A dry run takes the fingerprint from the ledger, which is only a hash."""
    cfg = make_cfg(tmp_path)
    cfg.credential_path = Path(tmp_path) / "does-not-exist.env"
    ledger = make_ledger(cfg)
    ledger.fingerprint = FP
    ledger.write(ledger.read())
    session = account.AccountSession(account.FakeTransport(), cfg=cfg, ledger=ledger)
    assert session.fingerprint == FP


def test_history_flood_stops_the_run_without_freezing_resolves(tmp_path):
    """The 36 468 s freeze was measured on resolveUsername, not on history."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport().with_history(7, [{"id": 1}])
    fake.floods["history"] = 300
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        first = live.history(req(), good_peer(peer_id=7))
        second = live.history(req(), good_peer(peer_id=7))
    assert first.stopped and "300" in first.stopped
    assert second.messages == []
    assert len(fake.history_calls) == 1            # the second one never left
    assert ledger.summary()["frozen"] is False


# --------------------------------------------------------------------------
# 12. The real transport, driven against a stubbed Telethon
#
# Until 2026-08-24 no test constructed `TelethonTransport` at all, so every
# mutation inside it was invisible: putting the live session string into
# `__repr__` -- the exact thing that class comment says `__repr__` exists to
# prevent -- was a green build. The stub below is a faithful-enough Telethon to
# drive the class end to end without a socket: it answers coroutines, it raises
# the error classes at their real names and inheritance, and it records what was
# constructed and what was sent.
# --------------------------------------------------------------------------
class StubRPCError(Exception):
    pass


class StubFloodError(StubRPCError):
    """`FloodError` -- the 420 family's base, as in rpcbaseerrors.py."""


def _make_wait_error(name):
    def __init__(self, request=None, capture=0):
        self.request = request
        self.seconds = int(capture)
        Exception.__init__(self, "A wait of %d seconds is required" % self.seconds)

    return type(name, (StubFloodError,), {"__init__": __init__})


class StubRequest:
    """Every functions.* request: keeps what it was handed so a test can look."""

    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        for key, value in kwargs.items():
            setattr(self, key, value)


class StubInputPeerChannel:
    def __init__(self, channel_id, access_hash):
        self.channel_id = channel_id
        self.access_hash = access_hash


def make_telethon_stub(monkeypatch, *, answer=None, raises=None, authorized=True,
                       disconnect_returns=None):
    """Install a stub `telethon` in sys.modules and return the handle to it.

    `answer` is what `client(request)` returns; `raises` is what it raises
    instead. `disconnect_returns` is None by default because that is what the
    real `disconnect()` returns when the loop is idle, which is always the case
    here -- and `run_until_complete(None)` is a TypeError.
    """
    import types as pytypes

    handle = SimpleNamespace(constructed=[], sent=[], connected=0, disconnected=0,
                             authorized_calls=0)

    errors = pytypes.ModuleType("telethon.errors")
    errors.RPCError = StubRPCError
    errors.FloodError = StubFloodError
    for name in account.FLOOD_ERROR_NAMES:
        setattr(errors, name, _make_wait_error(name))
    # A sibling this pin does not name: it must still be read as a wait.
    errors.TwoFaConfirmWaitError = _make_wait_error("TwoFaConfirmWaitError")
    errors.UsernameNotOccupiedError = type("UsernameNotOccupiedError", (StubRPCError,), {})
    errors.UsernameInvalidError = type("UsernameInvalidError", (StubRPCError,), {})

    class StubClient:
        def __init__(self, session, api_id, api_hash, **kwargs):
            handle.constructed.append(
                {"session": session, "api_id": api_id, "api_hash": api_hash,
                 "kwargs": dict(kwargs)}
            )

        async def connect(self):
            handle.connected += 1

        async def is_user_authorized(self):
            handle.authorized_calls += 1
            return authorized

        def disconnect(self):
            handle.disconnected += 1
            return disconnect_returns

        async def __call__(self, request):
            handle.sent.append(request)
            if raises is not None:
                raise raises
            return answer

    class StubStringSession:
        def __init__(self, string=None):
            self.string = string

    sessions = pytypes.ModuleType("telethon.sessions")
    sessions.StringSession = StubStringSession

    functions = pytypes.ModuleType("telethon.tl.functions")
    for group, names in {
        # `SearchRequest` twice, deliberately: `contacts.search` and
        # `messages.search` are different calls that share a name, and until
        # 2026-08-26 the stub carried neither -- so `TelethonTransport`'s two
        # search methods could not be driven here at all, and the branch that
        # turns Telegram's real refusal into `PeerUnusable` was covered by
        # nothing. A stub that lacks a request is not a passing test, it is an
        # untested method.
        "contacts": ["ResolveUsernameRequest", "SearchRequest"],
        "messages": ["GetHistoryRequest", "SearchRequest"],
        "channels": ["JoinChannelRequest"],
    }.items():
        module = pytypes.ModuleType("telethon.tl.functions." + group)
        for name in names:
            setattr(module, name, type(name, (StubRequest,), {}))
        setattr(functions, group, module)

    types_mod = pytypes.ModuleType("telethon.tl.types")
    types_mod.InputPeerChannel = StubInputPeerChannel
    types_mod.InputMessagesFilterEmpty = type("InputMessagesFilterEmpty", (), {})

    tl = pytypes.ModuleType("telethon.tl")
    tl.functions = functions
    tl.types = types_mod

    telethon = pytypes.ModuleType("telethon")
    telethon.__version__ = "1.44.0"
    telethon.TelegramClient = StubClient
    telethon.errors = errors
    telethon.sessions = sessions
    telethon.tl = tl

    for name, module in {
        "telethon": telethon, "telethon.errors": errors,
        "telethon.sessions": sessions, "telethon.tl": tl,
        "telethon.tl.functions": functions, "telethon.tl.types": types_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    handle.errors = errors
    return handle


def connected_transport(monkeypatch, **kw):
    """A connected `TelethonTransport` over the stub, and the stub's handle.

    Both switches, because `connect()` is itself a wire call -- the handshake
    plus `is_user_authorized` -- and since 2026-08-25 it refuses without them.
    Here they open the door onto a stub in `sys.modules`, and onto nothing else.
    """
    handle = make_telethon_stub(monkeypatch, **kw)
    turn_the_environment_switch_on()
    transport = account.TelethonTransport(1234567, "deadbeef" * 4, FAKE_SESSION,
                                          allow_live=True)
    return handle, transport.connect()


def test_the_real_client_is_built_with_our_policy_not_telethons(monkeypatch):
    """Telethon's defaults retry a resolve five times and sleep off any wait under 60 s.

    Measured against the pin: `request_retries=5`, `flood_sleep_threshold=60`.
    A FLOOD_WAIT_17 on a resolve is slept off and re-sent up to five times inside
    one `client(...)` call, so the ledger charges one resolve for five wire calls
    and never sees a wait. That is the 2026-08-20 signature exactly.
    """
    handle, transport = connected_transport(monkeypatch, answer=SimpleNamespace(chats=[]))
    kwargs = handle.constructed[0]["kwargs"]
    assert kwargs["flood_sleep_threshold"] == 0     # never sleep a wait off in there
    assert kwargs["request_retries"] == 1           # one wire call per accounted call
    assert kwargs["auto_reconnect"] is False
    assert kwargs["receive_updates"] is False
    assert handle.connected == 1 and handle.authorized_calls == 1
    transport.close()


def test_the_policy_constant_holds_the_four_frozen_values():
    """Named separately so a diff that loosens one of them is a diff somebody reads."""
    assert account.TELETHON_POLICY["flood_sleep_threshold"] == 0
    assert account.TELETHON_POLICY["request_retries"] == 1
    assert account.TELETHON_POLICY["auto_reconnect"] is False
    assert account.TELETHON_POLICY["receive_updates"] is False


@pytest.mark.parametrize(
    "error_name", list(account.FLOOD_ERROR_NAMES) + ["TwoFaConfirmWaitError"])
def test_every_wait_carrying_error_becomes_a_flood_wait(monkeypatch, error_name):
    """They are siblings under FloodError, not subclasses of FloodWaitError.

    Catching only `FloodWaitError` let `FLOOD_PREMIUM_WAIT_36468` -- the same ten
    hours under another class name -- through as a generic transport failure:
    counted, re-raised, never frozen, and the next source resolved anyway.
    """
    stub_errors = make_telethon_stub(monkeypatch).errors
    error = getattr(stub_errors, error_name)(None, 36468)
    handle, transport = connected_transport(monkeypatch, raises=error)
    with pytest.raises(account.FloodWait) as exc:
        transport.resolve_username("tdlibchat")
    assert exc.value.seconds == 36468
    assert len(handle.sent) == 1                   # one wire call, not five
    transport.close()


def test_a_wait_without_a_readable_number_still_stops_us(monkeypatch):
    """Telegram said wait. An unreadable number is not permission to continue."""
    stub_errors = make_telethon_stub(monkeypatch).errors
    broken = stub_errors.FloodWaitError(None, 0)
    broken.seconds = None
    handle, transport = connected_transport(monkeypatch, raises=broken)
    with pytest.raises(account.FloodWait) as exc:
        transport.resolve_username("tdlibchat")
    assert exc.value.seconds == account.UNKNOWN_FLOOD_WAIT_SEC
    transport.close()


def test_the_transports_repr_carries_no_credential(monkeypatch):
    """A repr lands in tracebacks and in pytest output. This one carries a boolean."""
    handle, transport = connected_transport(monkeypatch, answer=SimpleNamespace(chats=[]))
    text = repr(transport)
    assert FAKE_SESSION not in text
    assert "deadbeef" not in text
    assert "1234567" not in text
    assert text == "<TelethonTransport connected=True>"
    transport.close()
    assert repr(transport) == "<TelethonTransport connected=False>"


def test_the_real_transport_forces_the_free_option_on_every_call(monkeypatch):
    """Both gates, on the class that actually talks to Telegram."""
    peer = {"id": 7, "access_hash": 9}
    handle, transport = connected_transport(
        monkeypatch, answer=SimpleNamespace(chats=[], messages=[], users=[]))
    for call in (
        lambda: transport.resolve_username("tdlibchat", options={"allow_paid_stars": 5}),
        lambda: transport.fetch_history(peer, options={"allow_paid_stars": 5}),
        lambda: transport.join_group(peer, options={"allow_paid_stars": 5}),
        # The two search calls were added 2026-08-25 and this loop was not
        # extended with them, so a test whose name says "on every call" covered
        # three methods out of five. `_assert_free` is the second gate -- the one
        # that catches a hand-built options dict handed straight to a transport
        # -- and on the two newest methods it was enforced on nobody.
        lambda: transport.search_contacts("слово", options={"allow_paid_stars": 5}),
        lambda: transport.search_messages(peer, "слово", options={"allow_paid_stars": 5}),
    ):
        with pytest.raises(account.PaidCallRefused):
            call()
    assert handle.sent == []                       # none of them left the machine
    # And the loop above covers the whole surface, so the next method added to
    # the transport cannot quietly skip the gate the way these two did.
    public = {name for name, value in vars(account.TelethonTransport).items()
              if callable(value) and not name.startswith("_")
              and name not in ("connect", "close", "connected")}
    assert public == {"resolve_username", "fetch_history", "search_contacts",
                      "search_messages", "join_group"}, public
    transport.close()


def test_the_real_transport_drops_the_cause_and_scrubs_the_message(monkeypatch):
    """A chained cause is printed by every traceback, and can carry the login."""
    leak = RuntimeError("reset by peer; TELEGRAM_SESSION=" + FAKE_SESSION)
    handle, transport = connected_transport(monkeypatch, raises=leak)
    for call, kind in (
        (lambda: transport.resolve_username("tdlibchat"), "resolve"),
        (lambda: transport.fetch_history({"id": 7, "access_hash": 9}), "history"),
        (lambda: transport.join_group({"id": 7, "access_hash": 9}), "join"),
    ):
        with pytest.raises(account.TransportError) as exc:
            call()
        assert FAKE_SESSION not in str(exc.value), kind
        assert exc.value.__cause__ is None, kind
        assert exc.value.__suppress_context__ is True, kind
    transport.close()


def test_connect_refuses_a_session_that_does_not_authorise(monkeypatch):
    """The authorisation check is the only thing that notices a revoked session."""
    handle = make_telethon_stub(monkeypatch, authorized=False)
    turn_the_environment_switch_on()
    transport = account.TelethonTransport(1, "hash", FAKE_SESSION, allow_live=True)
    with pytest.raises(account.TransportError) as exc:
        transport.connect()
    assert "does not authorise" in str(exc.value)
    assert handle.authorized_calls == 1
    assert transport._client is None               # and it did not stay half-open


def test_close_survives_a_disconnect_that_returns_none(monkeypatch):
    """`disconnect()` returns None when the loop is idle, which is always, here.

    `run_until_complete(None)` is a TypeError, so the shipped close() could not
    close anything -- and closing is what puts the connection inside the lock.
    """
    handle, transport = connected_transport(monkeypatch, disconnect_returns=None)
    loop = transport._loop
    transport.close()
    assert handle.disconnected == 1
    assert loop.is_closed()
    transport.close()                              # idempotent, and still no throw
    assert handle.disconnected == 1


def test_a_string_peer_never_reaches_telethon(monkeypatch):
    """A `str` where an InputPeer belongs is an unbudgeted contacts.resolveUsername.

    `telethon/client/users.py:44` resolves every request before sending it, and
    `get_input_entity` issues `contacts.ResolveUsernameRequest` for a string. The
    id/access_hash pair short-circuits that path; nothing else does.
    """
    handle, transport = connected_transport(monkeypatch, answer=SimpleNamespace(messages=[]))
    for peer in ({"id": "tdlibchat", "access_hash": 9},
                 {"id": 7, "access_hash": "abc"},
                 {"id": 7}):
        with pytest.raises(account.TransportError) as exc:
            transport.fetch_history(peer)
        assert "resolveUsername" in str(exc.value)
        with pytest.raises(account.TransportError):
            transport.join_group(peer)
    assert handle.sent == []
    transport.close()


def test_the_peer_that_reaches_telethon_is_an_input_peer(monkeypatch):
    handle, transport = connected_transport(
        monkeypatch, answer=SimpleNamespace(messages=[], users=[], chats=[]))
    transport.fetch_history({"id": 7, "access_hash": 9}, limit=5, offset_id=3)
    sent = handle.sent[0]
    assert isinstance(sent.peer, StubInputPeerChannel)
    assert (sent.peer.channel_id, sent.peer.access_hash) == (7, 9)
    assert sent.limit == 5 and sent.offset_id == 3
    transport.close()


def test_history_records_the_author_from_the_entities_in_the_same_response(monkeypatch):
    """A raw GetHistoryRequest never runs `_finish_init`, so `msg.sender` is always None.

    The senders were arriving in `res.users` / `res.chats` and being thrown away
    with the response, which for a skill whose output is "what people said" is
    not a cosmetic loss.
    """
    user = SimpleNamespace(id=777, first_name="Ada", last_name="L", username="ada")
    channel = SimpleNamespace(id=1006503122, title="tdlib chat", username="tdlibchat")
    messages = [
        SimpleNamespace(id=5, message="hi", date=None, sender_id=777, reply_to=None),
        SimpleNamespace(id=6, message="post", date=None,
                        sender_id=-1001006503122, reply_to=None),
        SimpleNamespace(id=7, message="anon", date=None, sender_id=None, reply_to=None),
    ]
    answer = SimpleNamespace(messages=messages, users=[user], chats=[channel])
    handle, transport = connected_transport(monkeypatch, answer=answer)
    rows = transport.fetch_history({"id": 1006503122, "access_hash": 9})
    assert [r["author_name"] for r in rows] == ["Ada L", "tdlib chat", None]
    assert [r["author_username"] for r in rows] == ["ada", "tdlibchat", None]
    assert [r["author_id"] for r in rows] == [777, -1001006503122, None]
    transport.close()


def test_import_telethon_collects_the_wait_errors_by_name(monkeypatch):
    """Looked up defensively: a pin that renames one must still import."""
    make_telethon_stub(monkeypatch)
    tl = account._import_telethon()
    assert {c.__name__ for c in tl.flood_errors} == set(account.FLOOD_ERROR_NAMES)
    del sys.modules["telethon"].errors.SlowModeWaitError
    tl = account._import_telethon()
    assert "SlowModeWaitError" not in {c.__name__ for c in tl.flood_errors}
    assert tl.flood_base is StubFloodError         # the net that still catches it


# --------------------------------------------------------------------------
# 13. The lock covers all four spending methods
#
# `test_nothing_spends_outside_the_lock` exercised `resolve` and `prepare`.
# `history` and `join_group` carry the same `_require_open()` guard and had no
# test, so deleting it from either of them was a green build -- on the file's
# single most important structural rule.
# --------------------------------------------------------------------------
def test_history_requires_the_lock(tmp_path):
    fake = account.FakeTransport().with_history(7, [{"id": 1}])
    session = make_session(tmp_path, fake, live=True)
    with pytest.raises(account.AccountError) as exc:
        session.history(req(), good_peer(peer_id=7))
    assert "account lock" in str(exc.value)
    assert fake.history_calls == []


def test_join_requires_the_lock(tmp_path):
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport()
    session = make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger)
    with pytest.raises(account.AccountError):
        session.join_group(req(), good_peer())
    assert fake.join_calls == []
    assert ledger.read().joins == 0


def test_prepare_checks_evidence_before_it_touches_a_cached_peer(tmp_path):
    """The cached-peer path is inside the evidence gate, not around it."""
    fake = account.FakeTransport().answer_with("durov", 1)
    card = tgparse.PeerCard(username="durov", exists=True, type="channel")
    request = account.SourceRequest(username="durov", evidence=card,
                                    cached_peer=good_peer(peer_id=5))
    with make_session(tmp_path, fake, live=True) as live:
        with pytest.raises(account.WrongSurface):
            live.prepare([request])
    assert fake.resolve_calls == []


# --------------------------------------------------------------------------
# 14. What the ledger is told, and the run-local latch
# --------------------------------------------------------------------------
class RecordingLedger(ResolveLedger):
    """Remembers the arguments of every `record_resolve`, then behaves normally."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.records: list[tuple] = []

    def record_resolve(self, username, ok=True, *, token=None, now=None):
        self.records.append((username, bool(ok)))
        return super().record_resolve(username, ok, token=token, now=now)


def test_a_flooded_resolve_is_recorded_as_a_failure(tmp_path):
    """`ok=False` is the audit trail of the one call that has cost downtime."""
    cfg = make_cfg(tmp_path)
    ledger = RecordingLedger(cfg.ledger_path, daily_ceiling=180, burst_ceiling=100,
                             burst_window=600, min_gap=0.0, join_ceiling=3)
    fake = account.FakeTransport().flood_on("tdlibchat", 36468)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(ResolveFrozen):
            live.resolve(req())
    assert ledger.records == [("tdlibchat", False)]


def test_the_run_latch_stops_a_second_resolve_even_if_the_disk_forgot(tmp_path):
    """The burst is what did the damage, so the latch must not need the file."""
    cfg = make_cfg(tmp_path)
    ledger = UnpersistedFreezeLedger(cfg.ledger_path, daily_ceiling=180,
                                     burst_ceiling=100, burst_window=600,
                                     min_gap=0.0, join_ceiling=3)
    fake = account.FakeTransport().flood_on("tdlibchat", 36468)
    fake.answer_with("hanoi_chats", 2832)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(ResolveFrozen):
            live.resolve(req())
        with pytest.raises(ResolveFrozen):
            live.resolve(req("hanoi_chats"))       # a different name, same run
    assert [c["username"] for c in fake.resolve_calls] == ["tdlibchat"]


def test_a_peer_is_stamped_with_our_fingerprint_not_the_transports(tmp_path):
    """The transport does not get to say which login session minted the hash."""

    class LyingTransport(account.FakeTransport):
        def resolve_username(self, username, *, options=None):
            self.resolve_calls.append({"username": username, "options": options})
            return {"id": 5, "access_hash": 6, "auth_session_fingerprint": "another-login"}

    with make_session(tmp_path, LyingTransport(), live=True) as live:
        peer = live.resolve(req())
    assert peer["auth_session_fingerprint"] == FP
    assert peer_is_usable(peer, FP) is True


# --------------------------------------------------------------------------
# 15. Errors that used to lose the budget, the count, or the credential
# --------------------------------------------------------------------------
class ExplodingTransport(account.FakeTransport):
    """Fails the ordinary way -- a reset connection -- carrying a session string."""

    def __init__(self, boom: str = "*", **kw):
        super().__init__(**kw)
        self.boom = boom

    def _explode(self):
        raise RuntimeError("reset by peer; TELEGRAM_SESSION=" + FAKE_SESSION)

    def resolve_username(self, username, *, options=None):
        if self.boom in (username, "*"):
            self.resolve_calls.append({"username": username, "options": options})
            self._explode()
        return super().resolve_username(username, options=options)

    def fetch_history(self, peer, *, limit=100, offset_id=0, options=None):
        self.history_calls.append({"peer": dict(peer), "limit": limit})
        self._explode()

    def join_group(self, peer, *, options=None):
        self.join_calls.append({"peer": dict(peer), "options": options})
        self._explode()


def test_a_join_that_fails_the_ordinary_way_is_still_counted_and_scrubbed(tmp_path):
    """The request left the machine and the identity wore it: that is what is counted.

    Counting only `AccountError` let a transport failing in the ordinary way
    bypass the daily ceiling of 3 indefinitely, and forward its own message --
    credential included -- while doing it.
    """
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = ExplodingTransport()
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(account.TransportError) as exc:
            live.join_group(req(), good_peer())
    assert ledger.read().joins == 1
    assert FAKE_SESSION not in str(exc.value)
    assert exc.value.__cause__ is None


def test_a_join_that_fails_with_one_of_our_own_errors_is_counted_too(tmp_path):
    """Same rule, other branch: the request left the machine either way."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)

    class FloodingJoin(account.FakeTransport):
        def join_group(self, peer, *, options=None):
            self.join_calls.append({"peer": dict(peer), "options": options})
            raise account.FloodWait(300, "channels.joinChannel")

    with make_session(tmp_path, FloodingJoin(), live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(account.FloodWait):
            live.join_group(req(), good_peer())
    assert ledger.read().joins == 1


def test_history_that_fails_the_ordinary_way_is_wrapped_and_scrubbed(tmp_path):
    """The class docstring's promise, applied to the method that did not keep it."""
    cfg = make_cfg(tmp_path)
    fake = ExplodingTransport()
    with make_session(tmp_path, fake, live=True, cfg=cfg) as live:
        with pytest.raises(account.TransportError) as exc:
            live.history(req(), good_peer(peer_id=7))
        assert FAKE_SESSION not in str(exc.value)
        assert exc.value.__cause__ is None
        # It happened, so it is counted, and the run stops reading.
        stopped = live.history(req(), good_peer(peer_id=7))
    assert stopped.stopped and stopped.truncated is True
    assert len(fake.history_calls) == 1
    log = account.HistoryLog(Path(cfg.state_dir) / account.HISTORY_STATE_FILE)
    assert log.read()["requests"] == 1


def test_prepare_keeps_the_peers_it_paid_for_when_a_later_source_explodes(tmp_path):
    """Seven paid-for peers used to die with the exception raised by the eighth."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = ExplodingTransport(boom="hanoi_chats")
    fake.answer_with("tdlibchat", 1006503122).answer_with("durovs_chat", 3)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        report = live.prepare([req(), req("hanoi_chats"), req("durovs_chat")])
    assert report.resolved == ["tdlibchat"]
    assert report.peers["tdlibchat"]["id"] == 1006503122
    assert "hanoi_chats" in report.skipped and "durovs_chat" in report.skipped
    # The third name is never tried: something we do not understand happened.
    assert [c["username"] for c in fake.resolve_calls] == ["tdlibchat", "hanoi_chats"]
    assert FAKE_SESSION not in repr(report.as_dict())
    assert ledger.read().resolves == 2


def test_a_bad_source_still_hands_back_the_report(tmp_path):
    """Fail loudly, yes. Destroy the run's paid-for work on the way out, no."""
    fake = account.FakeTransport().answer_with("tdlibchat", 1006503122)
    bad = account.SourceRequest(username="durov",
                                evidence={"exists": True, "type": "channel",
                                          "username": "durov"})
    with make_session(tmp_path, fake, live=True) as live:
        with pytest.raises(account.WrongSurface) as exc:
            live.prepare([req(), bad])
    assert exc.value.report.peers["tdlibchat"]["id"] == 1006503122
    assert exc.value.report.resolved == ["tdlibchat"]


# --------------------------------------------------------------------------
# 16. The environment switch parses its value
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "", " ",
                                   "disabled", "2", "yes please"])
def test_a_switch_that_does_not_say_yes_refuses_live_mode(tmp_path, value):
    """`TELEGRAM_RESEARCH_ALLOW_LIVE=0` used to turn live mode ON.

    Presence was the whole test. Anyone turning the switch off the way switches
    are turned off -- 0, false, off, in a shell profile or a CI variable --
    turned it on, and `tg.py budget` agreed that they had left it on.
    """
    turn_the_environment_switch_on(value)
    with pytest.raises(account.LiveModeRefused) as exc:
        account.AccountSession(account.FakeTransport(), cfg=make_cfg(tmp_path),
                               fingerprint=FP, dry_run=False, allow_live=True)
    assert account.ENV_ALLOW_LIVE in str(exc.value)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_a_switch_that_says_yes_allows_live_mode(tmp_path, value):
    turn_the_environment_switch_on(value)
    session = account.AccountSession(
        account.FakeTransport(), cfg=make_cfg(tmp_path), fingerprint=FP,
        dry_run=False, allow_live=True,
    )
    assert session.dry_run is False


def test_env_flag_reads_only_a_value_that_says_yes():
    assert [account.env_flag(v) for v in ("1", "true", "On", "YES")] == [True] * 4
    assert [account.env_flag(v) for v in ("0", "false", "no", "off", "", None, " ",
                                          "disabled", "-1")] == [False] * 9


# --------------------------------------------------------------------------
# 17. messages.getHistory is accounted: a durable wait, a count, a ceiling, a gap
# --------------------------------------------------------------------------
def history_log_of(cfg) -> account.HistoryLog:
    return account.HistoryLog(Path(cfg.state_dir) / account.HISTORY_STATE_FILE)


def test_a_history_flood_outlives_the_process_that_earned_it(tmp_path):
    """A ten-hour wait used to live in a run-local attribute and nowhere else.

    The next `AccountSession` -- a retry, the next source, the same process one
    second later -- knew nothing and called getHistory again immediately.
    """
    cfg = make_cfg(tmp_path)
    first_transport = account.FakeTransport().with_history(7, [{"id": 1}])
    first_transport.floods["history"] = 36468
    with make_session(tmp_path, first_transport, live=True, cfg=cfg) as live:
        page = live.history(req(), good_peer(peer_id=7))
    assert page.stopped and page.truncated is True
    assert history_log_of(cfg).frozen_for() > 36000

    second_transport = account.FakeTransport().with_history(7, [{"id": 1}])
    with make_session(tmp_path, second_transport, live=True, cfg=cfg) as second:
        page2 = second.history(req(), good_peer(peer_id=7))
    assert second_transport.history_calls == []     # the next run waits it out
    assert "frozen" in page2.stopped
    # And the resolve ledger is left alone: the 36 468 s was measured on
    # resolveUsername, and history sharing that counter is not established.
    assert make_ledger(cfg).summary()["frozen"] is False


def test_every_history_page_is_counted_on_disk(tmp_path):
    cfg = make_cfg(tmp_path)
    fake = account.FakeTransport().with_history(7, [{"id": 3}, {"id": 2}, {"id": 1}])
    with make_session(tmp_path, fake, live=True, cfg=cfg) as live:
        for _ in range(3):
            live.history(req(), good_peer(peer_id=7), limit=1)
    assert len(fake.history_calls) == 3
    assert history_log_of(cfg).read()["requests"] == 3
    assert history_log_of(cfg).summary()["history_requests_today"] == 3


def test_history_stops_at_the_run_ceiling(tmp_path):
    """Five hundred pages under one lock used to be recorded nowhere and refused never."""
    cfg = make_cfg(tmp_path)
    cfg.budgets.max_requests_per_run = 2
    fake = account.FakeTransport().with_history(7, [{"id": 1}])
    with make_session(tmp_path, fake, live=True, cfg=cfg) as live:
        pages = [live.history(req(), good_peer(peer_id=7)) for _ in range(4)]
    assert len(fake.history_calls) == 2
    assert [p.truncated for p in pages] == [False, False, True, True]
    assert "ceiling is 2" in pages[2].stopped


def test_history_pages_are_paced(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.budgets.min_gap_sec = 2.0
    sleeps: list[float] = []
    fake = account.FakeTransport().with_history(7, [{"id": 1}])
    with make_session(tmp_path, fake, live=True, cfg=cfg, sleeps=sleeps) as live:
        live.history(req(), good_peer(peer_id=7))
        live.history(req(), good_peer(peer_id=7))
    assert len(fake.history_calls) == 2
    assert sleeps and 0 < sleeps[-1] <= 2.0


def test_an_unreadable_history_state_file_refuses_rather_than_permits(tmp_path):
    """Fail closed. A file we cannot read is not permission to spend."""
    cfg = make_cfg(tmp_path)
    path = Path(cfg.state_dir) / account.HISTORY_STATE_FILE
    path.write_text("{truncated", encoding="utf-8")
    fake = account.FakeTransport().with_history(7, [{"id": 1}])
    with make_session(tmp_path, fake, live=True, cfg=cfg) as live:
        with pytest.raises(account.StateUnreadable):
            live.history(req(), good_peer(peer_id=7))
        assert "unreadable" in live.summary()["history"]      # and it says so
    assert fake.history_calls == []


def test_a_history_state_file_that_never_existed_is_not_a_broken_one(tmp_path):
    cfg = make_cfg(tmp_path)
    log = history_log_of(cfg)
    assert log.frozen_for() == 0
    assert log.read()["requests"] == 0


# --------------------------------------------------------------------------
# 18. Budget arithmetic: a pause is not a wall, and a duplicate is not a purchase
# --------------------------------------------------------------------------
def test_a_name_already_resolved_in_this_run_is_not_resolved_again(tmp_path):
    """Out of a burst ceiling of 8, a duplicated candidate used to cost a name."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport().answer_with("tdlibchat", 1006503122)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        first = live.prepare([req(), req(), req()])
        second = live.prepare([req()])
    assert len(fake.resolve_calls) == 1
    assert ledger.read().resolves == 1
    assert first.resolved == ["tdlibchat"]
    assert first.from_session == ["tdlibchat", "tdlibchat"]
    assert second.from_session == ["tdlibchat"]
    assert second.peers["tdlibchat"]["id"] == 1006503122


def test_a_non_dyadic_minimum_gap_does_not_refuse_the_call_it_waited_for(tmp_path):
    """`last + 30.1` read back as a gap is 30.09999990463257: short, by float.

    30.0 is exact and every other value is not, so the shipped configuration hid
    a branch that any `TELEGRAM_RESEARCH_CONFIG` override would have walked into --
    reporting "budget exhausted" with the budget untouched.
    """
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg, min_gap=30.1)
    ledger.record_resolve("earlier", ok=True)
    sleeps: list[float] = []
    fake = account.FakeTransport().answer_with("tdlibchat", 1)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger,
                      sleeps=sleeps) as live:
        live.resolve(req())
    assert len(fake.resolve_calls) == 1
    assert sleeps and 25.0 <= sleeps[0] <= 30.2


class GapOnlyLedger(ResolveLedger):
    """Refuses right now and allows a gap later: what a minimum gap does."""

    opens_at = 0.0

    def check_resolve(self, now=None, **kw):
        now = time.time() if now is None else now
        if now < self.opens_at:
            raise BudgetExhausted(
                "only 3 s since the last resolve; the minimum gap is 30 s.")
        return None


def test_a_pause_does_not_end_the_run_but_a_ceiling_does(tmp_path):
    """A transient refusal latched as terminal ended a run over a wait of seconds."""
    cfg = make_cfg(tmp_path)
    transient = GapOnlyLedger(cfg.ledger_path, daily_ceiling=180, burst_ceiling=100,
                              burst_window=600, min_gap=30.0, join_ceiling=3)
    transient.opens_at = time.time() + 5.0
    fake = account.FakeTransport().answer_with("tdlibchat", 1)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=transient) as live:
        report = live.prepare([req()])
        assert live._budget_stop is False           # the run is still alive
    assert "minimum gap" in report.skipped["tdlibchat"]

    permanent = make_ledger(cfg, daily_ceiling=0)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=permanent) as live:
        live.prepare([req()])
        assert live._budget_stop is True            # a ceiling is a wall
    assert fake.resolve_calls == []


# --------------------------------------------------------------------------
# 19. What a dry run is allowed to promise
# --------------------------------------------------------------------------
def test_a_dry_run_says_whose_login_session_it_judged_the_cache_against(tmp_path):
    """It takes the fingerprint from the ledger, which may be two logins old.

    The plan the operator approves ("everything is cached, this costs nothing")
    is not the plan that runs if the operator has logged in again since. A dry run
    deliberately never reads the credential, so the honest thing it can do is say
    that the fingerprint is unverified.
    """
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    dry = account.AccountSession(account.FakeTransport(), cfg=cfg, ledger=ledger)
    assert dry.fingerprint_verified is False
    assert dry.summary()["fingerprint_source"] == "ledger"
    with dry:
        report = dry.prepare([req(cached_peer=good_peer())])
    assert report.fingerprint_verified is False
    assert report.as_dict()["fingerprint_verified"] is False

    supplied = make_session(tmp_path, cfg=cfg)
    assert supplied.fingerprint_verified is True


# --------------------------------------------------------------------------
# 20. The lock covers the connection, not just the object
# --------------------------------------------------------------------------
def test_the_session_closes_the_transport_before_it_drops_the_lock(tmp_path):
    """An authorised MTProto connection outliving the lock is a second writer.

    `AccountSession` never closed the transport, so after the `with` block the
    process still held an open, authorised connection while another process was
    free to take the lock and connect alongside it.
    """
    events: list[str] = []

    class ClosingTransport(account.FakeTransport):
        def close(self):
            events.append("close")

    class LoudLock:
        def acquire(self, *a, **kw):
            events.append("acquire")

        def release(self):
            events.append("release")

    session = account.AccountSession(
        ClosingTransport(), cfg=make_cfg(tmp_path), ledger=make_ledger(make_cfg(tmp_path)),
        lock=LoudLock(), fingerprint=FP,
    )
    with session:
        pass
    assert events == ["acquire", "close", "release"]


def test_a_transport_that_cannot_close_is_not_a_failure(tmp_path):
    """FakeTransport has no close(), and a broken close() must not eat the lock."""
    events: list[str] = []

    class AngryTransport(account.FakeTransport):
        def close(self):
            raise RuntimeError("TELEGRAM_SESSION=" + FAKE_SESSION)

    class LoudLock:
        def acquire(self, *a, **kw):
            events.append("acquire")

        def release(self):
            events.append("release")

    session = account.AccountSession(
        AngryTransport(), cfg=make_cfg(tmp_path), lock=LoudLock(), fingerprint=FP)
    with session:
        pass
    assert events == ["acquire", "release"]


# --------------------------------------------------------------------------
# 21. The status entry point, which is what an agent runs and what logs keep
#
# `main()` had no test at all. Two added lines that printed the credential file
# verbatim -- api_id, api_hash and the session string -- left the whole suite
# green.
# --------------------------------------------------------------------------
def test_status_prints_only_the_keys_it_declares(tmp_path):
    cfg = make_cfg(tmp_path)
    payload = account.status(cfg)
    assert set(payload) == set(account.STATUS_KEYS)
    assert payload["telethon_pin"] == account.TELETHON_PIN


def test_status_carries_no_credential_even_when_asked_for_one(tmp_path, monkeypatch):
    """The credential is not in the payload, and would be redacted if it were."""
    cred = Path(tmp_path) / "telegram.env"
    cred.write_text(
        "TELEGRAM_API_ID=1234567\n"
        "TELEGRAM_API_HASH=deadbeefcafebabe0123456789abcdef\n"
        "TELEGRAM_SESSION=" + FAKE_SESSION + "\n",
        encoding="utf-8",
    )
    cfg = make_cfg(tmp_path)
    cfg.credential_path = cred
    blob = json.dumps(account.status(cfg))
    assert FAKE_SESSION not in blob
    assert "deadbeefcafebabe0123456789abcdef" not in blob
    assert "credentials" not in blob


def test_main_prints_the_status_and_parses_the_live_switch(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_RESEARCH_STATE", str(tmp_path))
    monkeypatch.setenv(account.ENV_ALLOW_LIVE, "0")
    assert account.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == set(account.STATUS_KEYS)
    assert payload["live_enabled_in_env"] is False        # "0" is off, not "set"
    monkeypatch.setenv(account.ENV_ALLOW_LIVE, "1")
    account.main([])
    assert json.loads(capsys.readouterr().out)["live_enabled_in_env"] is True


def test_a_page_that_stopped_says_it_is_truncated(tmp_path):
    """`while page.messages:` reads a stopped page as the end of the group."""
    cfg = make_cfg(tmp_path)
    fake = account.FakeTransport().with_history(7, [{"id": 1}])
    fake.floods["history"] = 300
    with make_session(tmp_path, fake, live=True, cfg=cfg) as live:
        page = live.history(req(), good_peer(peer_id=7))
    assert page.messages == [] and page.truncated is True
    assert page.as_dict()["truncated"] is True
    good = account.HistoryPage(username="tdlibchat", messages=[{"id": 1}])
    assert good.truncated is False


# ==========================================================================
# 22. Adversarial regression guards.
#
# Every test below was written against the failure it pins, and every one of
# them fails on the code as it stood before the repair. They are adversarial by
# construction: they kill a process mid-call, break the accounting write, age a
# lock, damage a ledger mid-run, move the clock and take the environment switch
# away between two calls of the same run.
# ==========================================================================

# -- a resolve is counted, durably, BEFORE the call leaves -------------------
class KilledMidCall(account.FakeTransport):
    """The request is on the wire and the process dies.

    `KeyboardInterrupt` is a `BaseException`, so no `except Exception` in the
    module catches it -- which is the whole point: Ctrl-C, a supervisor timeout,
    VS Code stopping the task, and SIGKILL, which catches nothing at all.
    """

    def resolve_username(self, username, *, options=None):
        self.resolve_calls.append({"username": username, "options": options})
        raise KeyboardInterrupt("the request is in flight and we are gone")


def test_a_resolve_killed_mid_call_is_still_charged_to_the_account(tmp_path):
    """The 2026-08-20 signature, with the ledger rebuilt.

    `check -> call -> record` charged nothing when the process died during the
    call: five real `contacts.resolveUsername` calls, ledger total zero, the
    minimum-gap latch never armed, and the retry thirty seconds later reading a
    virgin budget. `reserve_resolve`/`settle_resolve` existed, were tested, and
    were called by nothing.
    """
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = KilledMidCall()
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(KeyboardInterrupt):
            live.resolve(req("victim"))

    assert fake.resolve_calls, "the call did leave the machine"
    fresh = ResolveLedger(cfg.ledger_path)          # another process's view
    state = fresh.read()
    assert state.resolves == 1
    assert state.last_resolve_ts > 0                # the 30 s latch is armed too
    assert fresh.summary()["pending_resolves"] == 1  # and it says the run died mid-call


def test_the_charge_is_on_disk_while_the_call_is_still_in_flight(tmp_path):
    """Not "counted afterwards" but "counted before": asked from inside the call.

    The transport reads the ledger through a brand-new object, which is what a
    second process would see, at the only moment that matters.
    """
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    seen: dict = {}

    class LooksAtTheLedger(account.FakeTransport):
        def resolve_username(self, username, *, options=None):
            state = ResolveLedger(cfg.ledger_path).read()
            seen["resolves"] = state.resolves
            seen["pending"] = len(state.pending or [])
            return super().resolve_username(username, options=options)

    fake = LooksAtTheLedger().answer_with("tdlibchat", 1006503122)
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        live.resolve(req())

    assert seen == {"resolves": 1, "pending": 1}
    # ... and settling afterwards neither double-counts nor leaves the audit open.
    assert ledger.read().resolves == 1
    assert ledger.read().pending == []


def test_a_failed_settle_does_not_replace_the_error_the_caller_is_handling(tmp_path):
    """The charge is already durable, so a settle that cannot write is a lost
    audit line and nothing more. It must not turn a `PeerNotFound` into a
    `LedgerWriteFailed` and it must not lose the count."""
    from resolve import LedgerWriteFailed

    cfg = make_cfg(tmp_path)

    class SettleFails(ResolveLedger):
        def record_resolve(self, username, ok=True, *, token=None, now=None):
            raise LedgerWriteFailed("the guard is busy")

    ledger = SettleFails(cfg.ledger_path, daily_ceiling=180, burst_ceiling=100,
                         burst_window=600, min_gap=0.0, join_ceiling=3)
    fake = account.FakeTransport().not_found("tdlibchat")
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(account.PeerNotFound):
            live.resolve(req())
    assert ResolveLedger(cfg.ledger_path).read().resolves == 1


# -- the freeze happens before the accounting --------------------------------
class AccountingWriteFails(ResolveLedger):
    """`record_resolve` cannot write. `freeze` is healthy."""

    def record_resolve(self, username, ok=True, *, token=None, now=None):
        from resolve import LedgerWriteFailed

        raise LedgerWriteFailed("guard busy; nothing was recorded")


def test_a_floodwait_freezes_even_when_the_accounting_write_fails(tmp_path):
    """Ten hours of downtime, remembered nowhere.

    `record_resolve` ran BEFORE `freeze`, and it can raise: the cross-process
    guard is busy for 10 s, or `atomic_write_text` cannot land (44 failed writes
    out of 300 under two writers, measured). Control left the `except FloodWait`
    block, so `freeze()` never ran, `_frozen` was never latched, and the next
    process resolved immediately -- which is what extends the ban.
    """
    cfg = make_cfg(tmp_path)
    ledger = AccountingWriteFails(cfg.ledger_path, daily_ceiling=180,
                                  burst_ceiling=100, burst_window=600,
                                  min_gap=0.0, join_ceiling=3)
    fake = account.FakeTransport().flood_on("tdlibchat", 36468)
    session = make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger)
    with session as live:
        with pytest.raises(ResolveFrozen):
            live.resolve(req())

    assert session._frozen is True                   # the run stops resolving
    durable = ResolveLedger(cfg.ledger_path)         # and so does the next one
    assert durable.summary()["frozen"] is True
    assert durable.summary()["frozen_for_sec"] > 36000
    assert "FloodWait on resolve of @tdlibchat" in durable.read().frozen_reason


def test_a_freeze_that_cannot_be_written_says_that_it_is_not_on_disk(tmp_path):
    """When the safety-critical write itself fails, the caller is told which of
    the two happened. A run-local latch dies with the process, and the next
    process cannot see a wait nobody managed to record."""
    from resolve import LedgerWriteFailed

    cfg = make_cfg(tmp_path)

    class FreezeWriteFails(ResolveLedger):
        def freeze(self, seconds, reason, now=None):
            raise LedgerWriteFailed("could not replace the ledger")

    ledger = FreezeWriteFails(cfg.ledger_path, daily_ceiling=180, burst_ceiling=100,
                              burst_window=600, min_gap=0.0, join_ceiling=3)
    fake = account.FakeTransport().flood_on("tdlibchat", 36468)
    session = make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger)
    with session as live:
        with pytest.raises(ResolveFrozen) as exc:
            live.resolve(req())
    assert "NOT on disk" in str(exc.value)
    assert "36468" in str(exc.value)
    assert session._frozen is True                   # this run stops regardless


# -- a live run refreshes its own lock ---------------------------------------
def _age_the_lock(lock_path: Path, seconds: float = 10_000.0) -> None:
    """Make a held lock look as old as real elapsed time would make it."""
    info = json.loads(lock_path.read_text(encoding="utf-8"))
    info["ts"] = time.time() - seconds
    lock_path.write_text(json.dumps(info), encoding="utf-8")
    old = time.time() - seconds
    import os as osmod

    osmod.utime(lock_path, (old, old))


def test_a_history_page_refreshes_the_account_lock(tmp_path):
    """The one job this file exists for lost the lock.

    `AccountLock.touch()` was reached only from `ResolveLedger._write_locked`, and
    a run that pages group history writes no ledger entry: `ts` and the file's
    mtime both stayed at acquire time, and after `stale_after` (1800 s default)
    a second process took the account mid-run. At the shipped pace a `deep` run's
    800-request ceiling is ~2000 s of paging, so the staleness is inside the
    working range of the only workflow the module has.
    """
    cfg = make_cfg(tmp_path)
    lock = AccountLock(cfg.lock_path, stale_after=100.0)
    fake = account.FakeTransport().with_history(7, [{"id": 3}, {"id": 2}, {"id": 1}])
    turn_the_environment_switch_on()
    session = account.AccountSession(
        fake, cfg=cfg, ledger=make_ledger(cfg), lock=lock, fingerprint=FP,
        dry_run=False, allow_live=True, sleep=lambda _s: None,
    )
    with session as live:
        _age_the_lock(cfg.lock_path)                 # as if it had been paging
        live.history(req(), good_peer(peer_id=7), limit=1)

        thief = AccountLock(cfg.lock_path, stale_after=100.0, owner="other-tool")
        with pytest.raises(AccountBusy):
            thief.acquire()
        assert lock.owns_the_file() is True
    assert len(fake.history_calls) == 1


def test_the_lock_is_refreshed_before_the_page_as_well_as_after(tmp_path):
    """A page can sit on the 30 s transport timeout, so the heartbeat comes
    first as well as last. Asserted through a transport that looks at the lock
    from inside the call."""
    cfg = make_cfg(tmp_path)
    lock = AccountLock(cfg.lock_path, stale_after=100.0)
    seen: dict = {}

    class LooksAtTheLock(account.FakeTransport):
        def fetch_history(self, peer, *, limit=100, offset_id=0, options=None):
            seen["ts"] = json.loads(cfg.lock_path.read_text(encoding="utf-8"))["ts"]
            return super().fetch_history(peer, limit=limit, offset_id=offset_id,
                                         options=options)

    fake = LooksAtTheLock().with_history(7, [{"id": 1}])
    turn_the_environment_switch_on()
    session = account.AccountSession(
        fake, cfg=cfg, ledger=make_ledger(cfg), lock=lock, fingerprint=FP,
        dry_run=False, allow_live=True, sleep=lambda _s: None,
    )
    with session as live:
        _age_the_lock(cfg.lock_path)
        aged = json.loads(cfg.lock_path.read_text(encoding="utf-8"))["ts"]
        live.history(req(), good_peer(peer_id=7))
    assert seen["ts"] > aged, "the lock was not refreshed before the call went out"


# -- the history state is written the way the ledger is ----------------------
def test_a_history_write_can_never_shorten_a_freeze(tmp_path):
    """The first half: 36 467 s -> 0 s.

    `HistoryLog.write` was `tmp.write_text` + `os.replace` with no guard and no
    floor, so a second writer whose `record_request` had read a moment earlier
    put `frozen_until = 0.0` back over a live ten-hour wait. `resolve.py:483-486`
    says this exact failure was measured on the ledger with Telegram's 36 468 s
    still running; this is the same bug in the file next to it.
    """
    path = Path(tmp_path) / account.HISTORY_STATE_FILE
    a = account.HistoryLog(path)
    b = account.HistoryLog(path)

    stale = b.read()                                 # B reads first...
    a.freeze(36468, "FloodWait on messages.getHistory for @tdlibchat")   # ...A freezes
    stale["requests"] += 1
    b.write(stale)                                   # ...and B writes its copy back

    assert a.frozen_for() > 36000
    assert account.HistoryLog(path).summary()["history_frozen"] is True


def test_a_history_write_that_cannot_land_is_our_own_error(tmp_path, monkeypatch):
    """A raw `PermissionError [WinError 5]` used to escape a module whose stated
    contract is that every exception it raises is one of its own redacted types.
    Driven by making the write fail rather than by holding a handle open, so the
    test says the same thing on every platform."""
    path = Path(tmp_path) / account.HISTORY_STATE_FILE
    log = account.HistoryLog(path)
    monkeypatch.setattr(configmod, "atomic_write_text",
                        lambda *a, **k: (_ for _ in ()).throw(
                            configmod.AtomicWriteFailed("someone has it open")))
    with pytest.raises(account.StateWriteFailed) as exc:
        log.freeze(36468, "FloodWait")
    assert isinstance(exc.value, account.AccountError)


def test_a_history_flood_that_cannot_be_written_still_returns_a_page(tmp_path,
                                                                     monkeypatch):
    """The caller of `history()` is a pagination loop, so the failure to record a
    wait belongs in the page it gets back, not in a traceback -- and the page has
    to say the wait is not on disk."""
    cfg = make_cfg(tmp_path)
    fake = account.FakeTransport().with_history(7, [{"id": 1}])
    fake.floods["history"] = 36468
    with make_session(tmp_path, fake, live=True, cfg=cfg) as live:
        monkeypatch.setattr(configmod, "atomic_write_text",
                            lambda *a, **k: (_ for _ in ()).throw(
                                configmod.AtomicWriteFailed("someone has it open")))
        page = live.history(req(), good_peer(peer_id=7))
    assert page.truncated is True
    assert "NOT on disk" in page.stopped
    assert "36468" in page.stopped


# -- prepare() never loses what the run already paid for ---------------------
def test_prepare_keeps_its_peers_when_the_ledger_dies_mid_run(tmp_path):
    """Two access hashes out of a budget of 180, gone.

    `LedgerUnreadable` is a `BudgetExhausted`, deliberately -- and `prepare`'s
    own `except BudgetExhausted` handler called `_next_slot()`, which reads the
    ledger, which raised `LedgerUnreadable` a second time from inside the
    handler. It propagated out of `prepare` and the report, a local variable,
    died with it: every peer already paid for lost, and the retry re-buys them.
    """
    cfg = make_cfg(tmp_path)

    class TruncatedByAnotherWriter(ResolveLedger):
        """The documented interrupted-write case, on the second resolve."""

        landed = 0

        def record_resolve(self, username, ok=True, *, token=None, now=None):
            state = super().record_resolve(username, ok, token=token, now=now)
            TruncatedByAnotherWriter.landed += 1
            if TruncatedByAnotherWriter.landed == 2:
                self.path.write_text("", encoding="utf-8")
            return state

    ledger = TruncatedByAnotherWriter(cfg.ledger_path, daily_ceiling=180,
                                      burst_ceiling=100, burst_window=600,
                                      min_gap=0.0, join_ceiling=3)
    fake = account.FakeTransport()
    for i, name in enumerate(("one", "two", "three", "four")):
        fake.answer_with(name, 100 + i)

    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        report = live.prepare([req(n) for n in ("one", "two", "three", "four")])

    assert report.resolved == ["one", "two"]
    assert report.peers["one"]["id"] == 100 and report.peers["two"]["id"] == 101
    assert sorted(report.skipped) == ["four", "three"]
    assert "cannot be read" in report.skipped["three"]
    # A damaged ledger is a wall, not a pause: the run stops spending.
    assert live._budget_stop is True


# -- the switches are facts, not attributes ----------------------------------
def test_the_second_switch_cannot_be_passed_in_as_a_keyword(tmp_path):
    """`env={"TELEGRAM_RESEARCH_ALLOW_LIVE": "1"}` reached
    the transport with the variable absent from the environment -- two switches
    whose entire argument is that they are independent, collapsed into one line
    of Python. The keyword is gone; nothing reads a caller-supplied environment.
    """
    with pytest.raises(TypeError):
        account.AccountSession(account.FakeTransport(), cfg=make_cfg(tmp_path),
                               fingerprint=FP, dry_run=False, allow_live=True,
                               env={account.ENV_ALLOW_LIVE: "1"})
    assert "env" not in account.AccountSession.__init__.__code__.co_varnames


def test_dry_run_cannot_be_switched_off_by_assignment(tmp_path):
    """One attribute assignment on a session constructed
    with no switches at all put a real `contacts.resolveUsername` on the wire."""
    fake = account.FakeTransport().answer_with("tdlibchat", 1)
    session = account.AccountSession(fake, cfg=make_cfg(tmp_path), fingerprint=FP)
    assert session.dry_run is True
    with pytest.raises(AttributeError):
        session.dry_run = False
    with session as still_dry:
        assert still_dry.resolve(req()) is None
    assert fake.resolve_calls == []


def test_the_environment_switch_is_read_at_the_call_not_at_construction(tmp_path):
    """The switches were checked once, in `__init__`, and every call afterwards
    consulted `dry_run` alone. An operator who turns the switch off does not
    expect the run that is already going to keep spending."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = account.FakeTransport().answer_with("tdlibchat", 1).with_history(
        7, [{"id": 1}])
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        live.resolve(req())                          # the switch is on: this works
        del os.environ[account.ENV_ALLOW_LIVE]       # ... and now it is not
        with pytest.raises(account.LiveModeRefused):
            live.resolve(req("hanoi_chats"))
        with pytest.raises(account.LiveModeRefused):
            live.history(req(), good_peer(peer_id=7))
        with pytest.raises(account.LiveModeRefused):
            live.join_group(req(), good_peer())
    assert [c["username"] for c in fake.resolve_calls] == ["tdlibchat"]
    assert fake.history_calls == [] and fake.join_calls == []
    assert ledger.read().resolves == 1


# -- the transport is gated too ----------------------------------------------
def test_the_real_transport_cannot_connect_without_both_switches(monkeypatch):
    """`connect()` is two calls on the account -- the
    MTProto handshake and `is_user_authorized` -- and it consulted no switch at
    all. Four lines of the module's own public API therefore reached Telegram
    with no lock, no ledger and no dry-run default:
    `TelethonTransport(*config.read_credentials(...)).connect().resolve_username(...)`.
    """
    handle = make_telethon_stub(monkeypatch)
    for allow_live, env_on in ((False, False), (True, False), (False, True)):
        if env_on:
            turn_the_environment_switch_on()
        else:
            os.environ.pop(account.ENV_ALLOW_LIVE, None)
        transport = account.TelethonTransport(1234567, "deadbeef" * 4, FAKE_SESSION,
                                              allow_live=allow_live)
        with pytest.raises(account.LiveModeRefused):
            transport.connect()
    assert handle.connected == 0 and handle.authorized_calls == 0
    assert transport._client is None


# -- a dry run describes a run that can happen -------------------------------
def test_a_dry_run_meets_the_same_ceilings_the_live_run_would(tmp_path):
    """Thirty sources, a plan promising thirty resolves.

    In dry run every source saw the same on-disk ledger, so source 30 was
    planned against the state source 1 saw. The live run of the same list
    resolves eight and refuses twenty-two on the burst ceiling -- and the
    preview exists precisely so a human can decide whether the run is safe.
    Asserted by running both and comparing them.
    """
    def thirty():
        return [req("src%d" % i) for i in range(30)]

    def budgets(cfg):
        return ResolveLedger(cfg.ledger_path, daily_ceiling=180, burst_ceiling=8,
                             burst_window=600, min_gap=30.0, join_ceiling=3)

    dry_cfg = make_cfg(tmp_path / "dry")
    with make_session(tmp_path, cfg=dry_cfg, ledger=budgets(dry_cfg)) as dry:
        planned = dry.prepare(thirty())

    live_cfg = make_cfg(tmp_path / "live")
    fake = account.FakeTransport()
    for i in range(30):
        fake.answer_with("src%d" % i, 1000 + i)
    with make_session(tmp_path, fake, live=True, cfg=live_cfg,
                      ledger=budgets(live_cfg)) as live:
        happened = live.prepare(thirty())

    assert len(planned.would_resolve) == len(happened.resolved) == 8
    assert len(planned.skipped) == len(happened.skipped) == 22
    assert planned.would_resolve == happened.resolved
    assert "burst ceiling is 8" in list(planned.skipped.values())[0]
    # The preview spends nothing: the simulation is a copy, never the file.
    assert ResolveLedger(dry_cfg.ledger_path).read().resolves == 0
    assert ResolveLedger(live_cfg.ledger_path).read().resolves == 8


def test_a_dry_run_charges_a_repeated_name_once(tmp_path):
    """The live run holds the peer it paid for and does not buy it twice; the
    preview must not report a cost the run would not pay either."""
    cfg = make_cfg(tmp_path)
    ledger = ResolveLedger(cfg.ledger_path, daily_ceiling=180, burst_ceiling=2,
                           burst_window=600, min_gap=0.0, join_ceiling=3)
    with make_session(tmp_path, cfg=cfg, ledger=ledger) as dry:
        report = dry.prepare([req(), req(), req(), req()])
    assert report.skipped == {}
    assert report.would_resolve == ["tdlibchat"] * 4


# -- m10 / m11: the history state keeps the ledger's clock rules -------------
def test_a_history_file_stamped_with_tomorrow_keeps_its_count(tmp_path):
    """`state["date"] != _today()` is an inequality, not
    a rollover test. `resolve._roll_day` was repaired for exactly this and this
    copy was left behind: a file stamped with tomorrow -- a clock that ran ahead,
    an NTP correction, a restored snapshot -- had today's count zeroed."""
    path = Path(tmp_path) / account.HISTORY_STATE_FILE
    log = account.HistoryLog(path)
    for _ in range(40):
        log.record_request()
    state = json.loads(path.read_text(encoding="utf-8"))
    state["date"] = "2099-01-01"
    path.write_text(json.dumps(state), encoding="utf-8")

    assert account.HistoryLog(path).read()["requests"] == 40
    assert account.HistoryLog(path).read()["date"] == "2099-01-01"


def test_a_history_file_stamped_with_yesterday_still_rolls_over(tmp_path):
    path = Path(tmp_path) / account.HISTORY_STATE_FILE
    log = account.HistoryLog(path)
    log.record_request()
    state = json.loads(path.read_text(encoding="utf-8"))
    state["date"] = "2000-01-01"
    path.write_text(json.dumps(state), encoding="utf-8")
    assert account.HistoryLog(path).read()["requests"] == 0


def test_a_history_freeze_survives_a_clock_jumped_forward(tmp_path):
    """The resolve freeze carries a monotonic twin so
    that "a clock jumped a day forward cannot end it"; the history freeze was
    wall-clock only, so any forward correction larger than the remaining wait
    ended a wait Telegram was still enforcing."""
    path = Path(tmp_path) / account.HISTORY_STATE_FILE
    log = account.HistoryLog(path)
    log.freeze(36468, "FloodWait on messages.getHistory")
    assert log.frozen_for() > 36000
    assert log.frozen_for(now=time.time() + 11 * 3600) > 36000
    assert account.HistoryLog(path).frozen_for(now=time.time() + 10 * 86400) > 36000


def test_a_history_request_can_be_recorded_against_another_day(tmp_path):
    """`record_request(now=...)` accepted `now` and ignored it, unlike every
    sibling: a caller simulating another day silently got today."""
    path = Path(tmp_path) / account.HISTORY_STATE_FILE
    log = account.HistoryLog(path)
    tomorrow = time.time() + 86_400
    state = log.record_request(now=tomorrow)
    assert state["date"] == account._day_of(tomorrow)
    assert state["date"] != account._day_of(None)


# -- m12: nothing is charged for a call that never left ----------------------
def test_a_transport_that_is_not_connected_charges_nothing(tmp_path):
    """`TransportError("transport is not connected")` is
    raised before anything is sent, but from INSIDE the call -- so with the
    accounting now durable and taken first, eight sources against an unconnected
    transport would exhaust the burst ceiling and arm the 30 s gap latch with
    zero packets sent. The readiness question is asked before the budget."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    never_connected = account.TelethonTransport(1234567, "deadbeef" * 4, FAKE_SESSION)
    assert never_connected.connected is False
    with make_session(tmp_path, never_connected, live=True, cfg=cfg,
                      ledger=ledger) as live:
        for name in ("a", "b", "c"):
            with pytest.raises(account.TransportError) as exc:
                live.resolve(req(name))
            assert "not connected" in str(exc.value)
    state = ledger.read()
    assert state.resolves == 0
    assert state.last_resolve_ts == 0.0              # the gap latch is not armed
    assert state.pending == []


# -- m13: the credential does not leak out of a constructor ------------------
def test_swapped_credentials_do_not_put_the_api_hash_in_the_error():
    """`int(api_id)` ran outside every handler, and
    `TelethonTransport(hash, id, session)` -- two adjacent strings out of one
    dict -- raised a bare `ValueError` quoting the api_hash verbatim. A plain
    `ValueError` is not an `AccountError`, so nothing redacted it."""
    api_hash = "0123456789abcdef0123456789abcdef"
    with pytest.raises(account.TransportError) as exc:
        account.TelethonTransport(api_hash, 1234567, FAKE_SESSION)
    assert api_hash not in str(exc.value)
    assert api_hash not in repr(exc.value)
    assert "swapped" in str(exc.value)


# -- m14: the CLI answers a configuration error with a sentence --------------
def test_main_answers_a_configuration_error_with_a_sentence(tmp_path, capsys):
    """`SKILL.md`: "Pointing `TELEGRAM_RESEARCH_STATE` at a
    *file* is a configuration error with a sentence, not a traceback." It was a
    traceback, at exit 1, with the sentence inside it."""
    a_file = Path(tmp_path) / "sources.jsonl"
    a_file.write_text("{}", encoding="utf-8")
    os.environ["TELEGRAM_RESEARCH_STATE"] = str(a_file)

    code = account.main([])
    out = capsys.readouterr()
    assert code == 7                                  # operator error, not exit 1
    payload = json.loads(out.out)                     # stdout stays JSON
    assert payload["ok"] is False
    assert payload["error_type"] == "ConfigError"
    assert "names a DIRECTORY" in payload["error"]
    assert "Traceback" not in out.out


def test_main_still_prints_the_status_when_the_configuration_is_sound(tmp_path,
                                                                      capsys):
    os.environ["TELEGRAM_RESEARCH_STATE"] = str(tmp_path)
    assert account.main([]) == 0
    assert set(json.loads(capsys.readouterr().out)) == set(account.STATUS_KEYS)


# -- m15: the history ceiling belongs to the run -----------------------------
def test_the_history_ceiling_belongs_to_the_run_not_to_the_session(tmp_path):
    """`_history_requests` was an instance attribute, so
    a script that opens an `AccountSession` per source multiplied
    `max_requests_per_run` by the number of sources: with the ceiling at 3, a
    second session constructed one line later fetched a fourth page and the
    durable counter went 3 -> 4 without anything ever refusing."""
    cfg = make_cfg(tmp_path)
    cfg.budgets.max_requests_per_run = 3
    peer = good_peer(peer_id=7)

    first = account.FakeTransport().with_history(7, [{"id": 1}])
    with make_session(tmp_path, first, live=True, cfg=cfg) as live:
        pages = [live.history(req(), peer) for _ in range(5)]
    assert len(first.history_calls) == 3
    assert [p.truncated for p in pages] == [False, False, False, True, True]

    second = account.FakeTransport().with_history(7, [{"id": 1}])
    with make_session(tmp_path, second, live=True, cfg=cfg) as live:
        page = live.history(req(), peer)
    assert second.history_calls == []
    assert page.truncated is True and "ceiling is 3" in page.stopped
    assert history_log_of(cfg).read()["requests"] == 3
    assert account.history_requests_this_process() == 3


# -- m17: closing a transport nobody connected -------------------------------
def test_close_on_a_never_connected_transport_leaves_the_loop_alone(tmp_path):
    """`close()` ended with
    `asyncio.set_event_loop(self._previous_loop)`, and that field is None on an
    instance that never connected -- and on the second `close()`, which is
    documented as safe. `AccountSession.__exit__` closes the transport
    unconditionally, so a failed `connect()` took the caller's loop with it."""
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        never = account.TelethonTransport(1, "hash", FAKE_SESSION)
        never.close()
        assert asyncio.get_event_loop() is loop
        never.close()                                 # still idempotent
        assert asyncio.get_event_loop() is loop
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def test_a_connected_transport_still_puts_the_previous_loop_back(monkeypatch):
    """The other half: what `close()` restores when there IS something to
    restore."""
    import asyncio

    outer = asyncio.new_event_loop()
    asyncio.set_event_loop(outer)
    try:
        handle, transport = connected_transport(monkeypatch,
                                                answer=SimpleNamespace(chats=[]))
        assert asyncio.get_event_loop() is not outer
        transport.close()
        assert asyncio.get_event_loop() is outer
    finally:
        asyncio.set_event_loop(None)
        outer.close()


# ==========================================================================
# 23. Adversarial regression guards, second round.
#
# Each of these was red against the code as it stood. They kill a process
# mid-call, hand the join path a peer
# out of a hand-edited registry, page history from two sessions in one process,
# and poison the state file with the two JSON literals `json.loads` accepts and
# nobody expects.
# ==========================================================================


# -- zero means zero, never "unlimited" --------------------------------------
def test_a_history_ceiling_of_zero_fetches_no_pages_at_all(tmp_path):
    """`0` meant two things at once and truth-tested away.

    `_history_ceiling()` returned `0` both for "the operator asked for zero" and
    for "there is no ceiling", and `_history_stop_reason` asked `if ceiling and
    spent >= ceiling`, so a configured budget of zero switched the ceiling OFF
    on the one path in the skill that spends the account. An operator who wants
    a run that makes no account calls writes exactly this, and `_apply_override`
    accepts it: only a negative value is refused.
    """
    cfg = make_cfg(tmp_path)
    cfg.budgets.max_history_requests_per_run = 0
    cfg.budgets.max_requests_per_run = 0
    fake = account.FakeTransport().with_history(7, [{"id": 1}])

    with make_session(tmp_path, fake, live=True, cfg=cfg) as live:
        pages = [live.history(req(), good_peer(peer_id=7)) for _ in range(3)]

    assert fake.history_calls == [], "a ceiling of 0 let getHistory through"
    assert [p.truncated for p in pages] == [True, True, True]
    assert "0" in pages[0].stopped and "ceiling" in pages[0].stopped
    assert history_log_of(cfg).read()["requests"] == 0
    assert account.history_requests_this_process() == 0


def test_an_override_cannot_raise_the_gethistory_ceiling(tmp_path):
    """The account ceiling was borrowed from a free-surface knob.

    `max_requests_per_run` is in neither clamp set -- an override file may set it
    to anything, which is right for the free surface and wrong for the account:
    `{"budgets": {"max_requests_per_run": 100000}}` raised the getHistory
    ceiling from 400 to 100000 for the same run, with `override_notes` empty so
    nothing was said on stderr or in `tg.py config`.
    """
    cfg = make_cfg(tmp_path)
    session = make_session(tmp_path, cfg=cfg)

    cfg.budgets.max_requests_per_run = 100000
    assert session._history_ceiling() == 400          # never above what shipped

    # The clamp in `_apply_override` is one of the two doors; this is the other.
    # A caller that hands `AccountSession` a hand-built `Budgets` -- a script, a
    # test, a future command with its own object -- must not be able to raise
    # the account's ceiling either.
    cfg.budgets.max_history_requests_per_run = 100000
    assert session._history_ceiling() == 400

    # Either knob may still LOWER it: slower and smaller is never the dangerous
    # direction, and the lower of the two is what applies.
    cfg.budgets.max_history_requests_per_run = 25
    assert session._history_ceiling() == 25
    cfg.budgets.max_history_requests_per_run = 400
    cfg.budgets.max_requests_per_run = 12
    assert session._history_ceiling() == 12

    # A configuration carrying neither number is not permission to page forever.
    bare = make_session(tmp_path, cfg=SimpleNamespace(
        budgets=SimpleNamespace(), state_dir=Path(tmp_path), ledger_path=cfg.ledger_path,
        lock_path=cfg.lock_path))
    assert bare._history_ceiling() == 400


# -- a call that reached Telegram is charged ---------------------------------
class KilledMidJoin(account.FakeTransport):
    """The join is on the wire and the process is gone. `KeyboardInterrupt` is a
    `BaseException`, so no `except Exception` in the module catches it."""

    def join_group(self, peer, *, options=None):
        self.join_calls.append({"peer": dict(peer), "options": options})
        raise KeyboardInterrupt("the join is in flight and we are gone")


def test_a_join_killed_mid_call_is_still_charged_to_the_daily_ceiling(tmp_path):
    """`check -> call -> record`, and Ctrl-C between the second and
    the third: Telegram recorded the join, the ledger did not. Three of those
    and the account has joined three groups with `joins_today = 0`, so the
    ceiling that exists to bound joins never fires. `resolve()` was repaired for
    exactly this shape; `join_group` was left on the old order."""
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    fake = KilledMidJoin()

    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(KeyboardInterrupt):
            live.join_group(req(), good_peer())

    assert fake.join_calls, "the join did leave the machine"
    assert ResolveLedger(cfg.ledger_path).read().joins == 1


def test_a_history_page_killed_mid_call_is_still_counted(tmp_path):
    """The same shape one method over: `history()` counts after the call, so a
    Ctrl-C mid-page loses the durable daily audit count of a request Telegram
    served. Lower stakes than the join ceiling, same rule."""
    cfg = make_cfg(tmp_path)

    class KilledMidPage(account.FakeTransport):
        def fetch_history(self, peer, *, limit=100, offset_id=0, options=None):
            self.history_calls.append({"peer": dict(peer), "limit": limit})
            raise KeyboardInterrupt("the page is in flight and we are gone")

    fake = KilledMidPage()
    with make_session(tmp_path, fake, live=True, cfg=cfg) as live:
        with pytest.raises(KeyboardInterrupt):
            live.history(req(), good_peer(peer_id=7))

    assert fake.history_calls, "the page did leave the machine"
    assert history_log_of(cfg).read()["requests"] == 1
    assert account.history_requests_this_process() == 1


# -- a call that never left is charged nothing -------------------------------
class RefusesBeforeTheWire(account.FakeTransport):
    """A transport that refuses a string peer the way the real one does.

    `TelethonTransport._input_peer` raises before a byte is sent, because
    handing Telethon a string peer would make it call `contacts.resolveUsername`
    outside the ledger. Nothing reaches the network, so nothing may be charged.
    """

    def join_group(self, peer, *, options=None):
        if isinstance(peer.get("id"), str) or isinstance(peer.get("access_hash"), str):
            raise account.TransportError(
                "a peer reached the transport without a numeric id and access_hash. "
                "The call was not sent."
            )
        return super().join_group(peer, options=options)


def test_a_join_refused_on_this_machine_costs_nothing(tmp_path):
    """The mirror image of the one above. Both `except` branches charged
    the ceiling for ANY exception out of the transport, including the ones
    raised before a byte is sent -- so three local refusals exhausted the day's
    join budget of 3 with nothing on the wire.

    Reachable from real data: `resolve.peer_is_usable` tests `id` and
    `access_hash` for truth only, never for numeric type, so a registry record
    holding strings -- a hand-edited file, a JSON round trip through a
    stringifying writer -- passes the gate and is refused inside the transport.
    """
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    stringy = {"id": "1006503122", "access_hash": "42",
               "auth_session_fingerprint": FP}
    assert peer_is_usable(stringy, FP) is True        # which is why this is reachable

    fake = RefusesBeforeTheWire()
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        for _ in range(3):
            with pytest.raises(account.PeerUnusable):
                live.join_group(req(), stringy)

    assert fake.join_calls == []
    assert ResolveLedger(cfg.ledger_path).read().joins == 0
    # ... and the budget is intact, so a good peer still joins afterwards
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=make_ledger(cfg)) as live:
        assert live.join_group(req(), good_peer())["joined"] is True


# -- the pacing timestamp belongs to the run, like the count -----------------
def test_history_pacing_holds_across_two_sessions_in_one_process(tmp_path):
    """`_PROCESS_HISTORY_REQUESTS` was lifted to module scope so a
    script opening a session per source could not multiply the ceiling; the
    pacing timestamp was left an instance attribute, so that same script sent
    the first getHistory of every session with no gap at all after the last one
    of the previous session -- eight sources, seven of fifteen gaps zero."""
    cfg = make_cfg(tmp_path)
    cfg.budgets.min_gap_sec = 2.0
    sleeps: list[float] = []

    first = account.FakeTransport().with_history(7, [{"id": 1}])
    with make_session(tmp_path, first, live=True, cfg=cfg, sleeps=sleeps) as live:
        live.history(req(), good_peer(peer_id=7))
    assert sleeps == []                     # nothing came before it

    second = account.FakeTransport().with_history(7, [{"id": 1}])
    with make_session(tmp_path, second, live=True, cfg=cfg, sleeps=sleeps) as live:
        live.history(req(), good_peer(peer_id=7))

    assert len(second.history_calls) == 1
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 2.0, (
        "the first page of a new session went out with no gap after the last "
        "page of the previous one")


# -- the two JSON literals nobody expects ------------------------------------
@pytest.mark.parametrize("literal", ["1e999", "-1e999", "NaN"])
def test_a_non_finite_history_state_is_one_of_our_own_errors(tmp_path, literal):
    """`json.loads` accepts `Infinity` and `NaN` by default, and
    `read()` coerced `frozen_until` with `float(...)`, so `int(left)` raised
    `OverflowError` / `ValueError` -- neither an `AccountError`. Every caller
    (`HistoryLog.summary`, `AccountSession.summary`, `main()`'s exit-7 branch)
    catches `AccountError` only, so the module's "fail closed with our own
    redacted types" contract broke on a hand-edited file.

    And had it NOT raised, NaN would be worse than a crash: every comparison
    against it is false, so a poisoned `frozen_until` reads as "not frozen"
    while Telegram is still counting.
    """
    path = Path(tmp_path) / account.HISTORY_STATE_FILE
    path.write_text('{"frozen_until": %s, "requests": 0}' % literal, encoding="utf-8")
    log = account.HistoryLog(path)

    with pytest.raises(account.StateUnreadable):
        log.frozen_for()
    with pytest.raises(account.StateUnreadable):
        log.read()
    with pytest.raises(account.AccountError):
        log.summary()


def test_a_moment_in_time_no_calendar_has_is_our_own_error_too(tmp_path):
    """The argument half: a hostile number reaching `datetime.fromtimestamp`
    answers with a bare `ValueError` / `OverflowError` / `OSError`, and no
    caller of this module catches any of those."""
    path = Path(tmp_path) / account.HISTORY_STATE_FILE
    log = account.HistoryLog(path)
    for hostile in (float("inf"), float("nan"), 1e308):
        with pytest.raises(account.AccountError):
            log.frozen_for(now=hostile)
    with pytest.raises(account.AccountError):
        log.freeze(float("nan"), "FloodWait with an unreadable number")
    assert not path.exists(), "nothing was written for a freeze we could not record"


# -- status() asks the configuration first -----------------------------------
def test_status_answers_a_state_dir_it_cannot_create_with_a_sentence(tmp_path,
                                                                      capsys):
    """`Config.ensure_dirs()` and `_mkdir_advice` exist to turn every
    OSError from creating the state directory into a ConfigError with the
    sentence that fits it. `status()` skipped them, so the first thing to touch
    the disk was a bare `mkdir` in `HistoryLog.__init__`, and
    `python scripts/account.py` answered with a raw traceback at exit 9 --
    against a docstring promising "a configuration error with a sentence, not a
    traceback"."""
    a_file = Path(tmp_path) / "notadir"
    a_file.write_text("x", encoding="utf-8")
    os.environ["TELEGRAM_RESEARCH_STATE"] = str(a_file / "state")

    code = account.main([])
    out = capsys.readouterr()

    assert code == 7                                   # operator error, not 9
    payload = json.loads(out.out)
    assert payload["ok"] is False
    assert payload["error_type"] == "ConfigError"
    assert "state directory" in payload["error"]
    assert "Traceback" not in out.out and "Traceback" not in out.err


# -- the reservation token is honoured or it does not exist ------------------
class TokenLedger(ResolveLedger):
    """Freezes the settlement signature from this file's side.

    `check_resolve(reserve=True)` hands back a token; `record_resolve` settles
    THAT reservation. The ledger itself is tested in `test_resolve.py`, so
    this double is where `account.py`'s half of the rule is pinned.
    """

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.reserved: list = []
        self.settled: list = []

    def check_resolve(self, now=None, *, reserve=False, username=""):
        token = super().check_resolve(now=now, reserve=reserve, username=username)
        if reserve:
            self.reserved.append((token, username))
        return token

    def record_resolve(self, username, ok=True, *, token=None, now=None):
        self.settled.append((username, bool(ok), token))
        return super().record_resolve(username, ok, token=token, now=now)


def test_the_reservation_token_is_the_one_that_gets_settled(tmp_path):
    """`check_resolve(reserve=True)` reserves with an EMPTY username
    (`resolve.py:657`), so `record_resolve` matched nothing by name and always
    took its oldest-un-named fallback: a stale pending left by a killed run was
    settled in place of the live one. Counts stayed right; the audit trail that
    says a run died mid-call did not."""
    cfg = make_cfg(tmp_path)
    ledger = TokenLedger(cfg.ledger_path, daily_ceiling=180, burst_ceiling=100,
                         burst_window=600, min_gap=0.0, join_ceiling=3)
    fake = account.FakeTransport().answer_with("tdlibchat", 1006503122)

    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        live.resolve(req())

    assert len(ledger.reserved) == 1 and ledger.reserved[0][0]
    assert ledger.reserved[0][1] == "tdlibchat", (
        "the reservation was taken without the name it is for")
    assert ledger.settled == [("tdlibchat", True, ledger.reserved[0][0])], (
        "the settlement did not carry the token of the reservation it closes")


def test_a_failed_resolve_settles_its_own_token_too(tmp_path):
    """The failure path settles the same way: `ok=False` is the audit trail of
    the one call that has ever cost this account downtime."""
    cfg = make_cfg(tmp_path)
    ledger = TokenLedger(cfg.ledger_path, daily_ceiling=180, burst_ceiling=100,
                         burst_window=600, min_gap=0.0, join_ceiling=3)
    fake = account.FakeTransport().not_found("tdlibchat")

    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(account.PeerNotFound):
            live.resolve(req())

    assert ledger.settled == [("tdlibchat", False, ledger.reserved[0][0])]
    assert ledger.reserved[0][1] == "tdlibchat"


def test_a_history_log_that_cannot_create_its_directory_is_our_own_error(tmp_path):
    """The constructor half. `HistoryLog.__init__` did a bare
    `mkdir`, so it was the first thing in the module to touch the disk and it
    answered an unusable `TELEGRAM_RESEARCH_STATE` with a raw OSError -- out of a
    module whose contract is that every exception it raises is one of its own.
    `status()` now asks `Config.ensure_dirs()` first; this is the backstop for
    every other caller."""
    a_file = Path(tmp_path) / "notadir"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(account.StateWriteFailed) as exc:
        account.HistoryLog(a_file / "state" / account.HISTORY_STATE_FILE)
    assert isinstance(exc.value, account.AccountError)
    assert "could not be created" in str(exc.value)


# -- the half that has to survive the crash: the reservation is NAMED --------
def test_the_reservation_on_disk_carries_the_name_it_is_for(tmp_path):
    """`check_resolve(reserve=True)` was called without `username=`, so every
    reservation the working path took was anonymous.

    After a crash the pending record is the ONLY evidence of which name was
    mid-resolve -- the peer was never returned, the report died with the
    process, and the daily count says one resolve happened without saying on
    what. Without the name the recovery path cannot tell one abandoned
    reservation from another. Asked from inside the call, through a fresh
    ledger object, which is what a second process sees.
    """
    cfg = make_cfg(tmp_path)
    ledger = make_ledger(cfg)
    seen: dict = {}

    class LooksAtTheLedgerThenDies(account.FakeTransport):
        def resolve_username(self, username, *, options=None):
            self.resolve_calls.append({"username": username, "options": options})
            seen["pending"] = ResolveLedger(cfg.ledger_path).read().pending
            raise KeyboardInterrupt("the request is in flight and we are gone")

    fake = LooksAtTheLedgerThenDies()
    with make_session(tmp_path, fake, live=True, cfg=cfg, ledger=ledger) as live:
        with pytest.raises(KeyboardInterrupt):
            live.resolve(req("hanoi_chats"))

    assert [p.get("username") for p in seen["pending"]] == ["hanoi_chats"]
    # ... and it outlives the run that took it, which is the whole point
    abandoned = ResolveLedger(cfg.ledger_path).pending_resolves()
    assert [p.get("username") for p in abandoned] == ["hanoi_chats"]


def test_a_healthy_resolve_leaves_the_dead_run_s_reservation_alone(tmp_path):
    """Two runs, one process's worth of evidence between them.

    Run A is killed with `@hanoi_chats` on the wire. Run B resolves
    `@tdlibchat` and settles. Before the token both reservations were anonymous and
    `record_resolve` settled the oldest nameless one it could find, so B's
    settlement cleared A's record: `summary()` then reported a pending resolve
    against the call that succeeded, while the only durable trace of a run
    dying mid-call had been quietly removed by an unrelated healthy call.
    """
    cfg = make_cfg(tmp_path)

    class DiesMidCall(account.FakeTransport):
        def resolve_username(self, username, *, options=None):
            self.resolve_calls.append({"username": username, "options": options})
            raise KeyboardInterrupt("killed with the call on the wire")

    with make_session(tmp_path, DiesMidCall(), live=True, cfg=cfg,
                      ledger=make_ledger(cfg)) as dying:
        with pytest.raises(KeyboardInterrupt):
            dying.resolve(req("hanoi_chats"))

    healthy = account.FakeTransport().answer_with("tdlibchat", 1006503122)
    with make_session(tmp_path, healthy, live=True, cfg=cfg,
                      ledger=make_ledger(cfg)) as second:
        assert second.resolve(req("tdlibchat"))["id"] == 1006503122

    state = ResolveLedger(cfg.ledger_path).read()
    assert state.resolves == 2                       # both calls left the machine
    assert [p.get("username") for p in state.pending] == ["hanoi_chats"], (
        "the healthy run settled the reservation the dead run left behind")


# ==========================================================================
# Search: the two calls that replaced the resolve on the ordinary path
# ==========================================================================
# Measured 2026-08-25 on a live account, and every number below comes from that
# measurement rather than from a design: `contacts.search` answered with the peer
# AND its access_hash in one call, so eight lookups cost eight calls and **zero**
# resolves; `messages.search` then answered a one-word query inside a 29 000-id
# group with 44 hits for one call. The accountless route to the same question
# spent 200 GETs, returned 2 messages and matched the word zero times.


PEER_ROWS = [
    {"username": "hanoi_chats", "id": 1931920118, "access_hash": 77,
     "type": "group", "title": "Большой чат | Общение",
     "participants": 2835, "verified": False, "scam": False},
    {"username": "vietnam_ru", "id": 42, "access_hash": 99, "type": "channel",
     "title": "Новостной канал", "participants": 51000, "verified": False, "scam": False},
]

HITS = [{"id": 28569, "date": "2026-03-12T00:49:08+00:00", "text": "первое найденное сообщение",
         "author_name": "кто-то", "author_username": None, "author_id": 7,
         "reply_to_id": None, "via": "mtproto"},
        {"id": 15597, "date": "2024-09-29T18:42:51+00:00", "text": "второе найденное сообщение",
         "author_name": None, "author_username": None, "author_id": 8,
         "reply_to_id": None, "via": "mtproto"}]


def searching(tmp_path, **kw):
    fake = account.FakeTransport().with_contacts("тестовый запрос", PEER_ROWS)
    fake.with_hits(1931920118, "слово", HITS, total=44)
    return fake, make_session(tmp_path, fake, live=True, **kw)


def test_contacts_search_hands_over_the_peer_without_a_single_resolve(tmp_path):
    """The whole repair, in one assertion: `resolve_calls` stays empty.

    `contacts.resolveUsername` is the only call that has ever cost this account
    downtime -- 16 of them in 7 minutes bought a 36 468 s freeze, and all 16
    returned success on an account that was already dead. The search box answers
    with the same access_hash for the same one call, so the resolve is not needed
    for any name it returns.
    """
    fake, session = searching(tmp_path)
    with session as live:
        found = live.search_contacts("тестовый запрос")
    assert fake.resolve_calls == [], "a search path resolved a username"
    assert found["requests"] == 1
    assert [row["username"] for row in found["peers"]] == ["hanoi_chats", "vietnam_ru"]
    assert found["peers_cached"] == 2


def test_the_peer_a_search_paid_for_is_on_disk_for_the_next_process(tmp_path):
    """The cache is what makes the second question about a group cost nothing.

    An access hash does not expire while the login lives, so a peer found once is
    usable for ever -- but only within that login: Telegram documents access
    hashes as not reusable across sessions, which is why the record is stamped
    and `peer_is_usable` is the only reader of the stamp.
    """
    cfg = make_cfg(tmp_path)
    fake, session = searching(tmp_path, cfg=cfg)
    with session as live:
        live.search_contacts("тестовый запрос")
    fresh = account.PeerCache(Path(cfg.state_dir) / account.PEER_CACHE_FILE)
    assert fresh.get("hanoi_chats", FP)["access_hash"] == 77
    assert fresh.get("hanoi_chats", "another-login") is None, \
        "a peer from one login was handed to another"
    assert fresh.get("nobody_here", FP) is None


def test_messages_search_answers_a_group_in_one_call_and_says_what_it_left(tmp_path):
    fake, session = searching(tmp_path)
    with session as live:
        peer = {"id": 1931920118, "access_hash": 77, "type": "group",
                "auth_session_fingerprint": FP}
        page = live.search_messages("hanoi_chats", peer, "слово", limit=50)
    assert fake.resolve_calls == [] and len(fake.search_calls) == 1
    assert page["requests"] == 1
    assert [m["id"] for m in page["messages"]] == [28569, 15597]
    # The server's own count of matches. `?q=` can never say this about itself,
    # which is why a capped web search has to warn and this one can state it.
    assert page["total"] == 44


def test_a_channel_is_never_searched_through_the_account(tmp_path):
    """The oldest rule in the module, applied to the new call.

    `t.me/s/<name>?q=` searches a channel's whole history for free and with no
    identity attached, so pointing the account at one buys risk and nothing else.
    """
    fake, session = searching(tmp_path)
    with session as live:
        peer = {"id": 42, "access_hash": 99, "type": "channel",
                "auth_session_fingerprint": FP}
        with pytest.raises(account.WrongSurface):
            live.search_messages("vietnam_ru", peer, "слово")
    assert fake.search_calls == [], "the refused channel still reached the wire"


def test_a_peer_from_another_login_is_refused_before_the_wire(tmp_path):
    fake, session = searching(tmp_path)
    with session as live:
        peer = {"id": 1931920118, "access_hash": 77, "type": "group",
                "auth_session_fingerprint": "some-other-login"}
        with pytest.raises(account.PeerUnusable):
            live.search_messages("hanoi_chats", peer, "слово")
    assert fake.search_calls == []


def test_a_stale_access_hash_is_named_rather_than_reported_as_a_failure(tmp_path):
    """The one failure a permanent peer cache can introduce, and its repair.

    A hash Telegram no longer accepts is not an unexplained transport error: it
    is a cache entry to drop and one `contacts.search` to spend. Named as its own
    type so the caller can do exactly that instead of stopping the run.
    """
    fake, session = searching(tmp_path)
    fake.stale_peer(77)
    with session as live:
        peer = {"id": 1931920118, "access_hash": 77, "type": "group",
                "auth_session_fingerprint": FP}
        with pytest.raises(account.PeerUnusable) as exc:
            live.search_messages("hanoi_chats", peer, "слово")
    assert "contacts.search" in str(exc.value)


def test_a_stale_hash_does_not_latch_the_run_that_can_repair_it(tmp_path):
    """The latch is for a failure we do not understand, and this one we do.

    Measured live 2026-08-25: latching on `PeerUnusable` made the repair
    unreachable -- the run dropped the stale record, asked for the peer again,
    was refused LOCALLY by its own latch, and answered `found: 0` about a group
    holding 44 matches for that word. The call is still counted: it left the
    machine and Telegram answered it.
    """
    fake, session = searching(tmp_path)
    fake.stale_peer(77)
    with session as live:
        with pytest.raises(account.PeerUnusable):
            live.search_messages("hanoi_chats",
                                 {"id": 1931920118, "access_hash": 77, "type": "group",
                                  "auth_session_fingerprint": FP}, "слово")
        assert live.account_calls == 1, "a call Telegram answered went uncounted"
        # The run may carry on -- which is the whole point.
        again = live.search_contacts("тестовый запрос")
        assert not again.get("stopped"), again
    assert len(fake.contacts_calls) == 1


def test_a_flood_on_a_search_outlives_the_process_that_earned_it(tmp_path):
    """A wait written to a run-local attribute is a wait the next process ignores.

    Same rule the history page already had, and the same order of writes: the
    run-local latch, then the durable freeze, then the audit count -- so a
    failing disk cannot lose the downtime.
    """
    cfg = make_cfg(tmp_path)
    fake, session = searching(tmp_path, cfg=cfg)
    fake.floods["messages.search"] = 36468
    with session as live:
        peer = {"id": 1931920118, "access_hash": 77, "type": "group",
                "auth_session_fingerprint": FP}
        page = live.search_messages("hanoi_chats", peer, "слово")
        assert page["stopped"] and "36468" in page["stopped"]
        # And the run stops calling: the second query does not reach the wire.
        again = live.search_contacts("что угодно")
        assert again["stopped"]
    assert len(fake.search_calls) == 1 and fake.contacts_calls == []
    left = account.HistoryLog(
        Path(cfg.state_dir) / account.HISTORY_STATE_FILE).frozen_for()
    assert left > 36000, "the wait is not on disk for the next process"


def test_one_ceiling_covers_history_and_both_searches(tmp_path):
    """Three names for the same thing: a call this account makes that is not a
    resolve. Three separate ceilings would be three invented measurements."""
    cfg = make_cfg(tmp_path)
    cfg.budgets.max_history_requests_per_run = 2
    fake, session = searching(tmp_path, cfg=cfg)
    peer = {"id": 1931920118, "access_hash": 77, "type": "group",
            "auth_session_fingerprint": FP}
    with session as live:
        assert live.search_contacts("тестовый запрос")["requests"] == 1
        assert live.search_messages("hanoi_chats", peer, "слово")["requests"] == 1
        third = live.search_messages("hanoi_chats", peer, "слово")
    assert third["stopped"] and "ceiling is 2" in third["stopped"]
    assert len(fake.search_calls) == 1, "the ceiling did not stop the wire call"


def test_a_dry_run_searches_nothing_and_says_what_it_would_send(tmp_path):
    fake = account.FakeTransport().with_contacts("слово", PEER_ROWS)
    session = make_session(tmp_path, fake, live=False)
    with session as preview:
        planned = preview.search_contacts("слово")
    assert planned["dry_run"] is True and planned["requests"] == 0
    assert planned["would"]["call"] == "contacts.search"
    assert fake.contacts_calls == []


def test_neither_search_can_be_made_to_spend_stars(tmp_path):
    """`allow_paid_stars` is forced to None after every config layer has spoken,
    and checked again at the transport boundary."""
    cfg = make_cfg(tmp_path)
    cfg.call_options = {"allow_paid_stars": 5000}
    fake, session = searching(tmp_path, cfg=cfg, options={"allow_paid_stars": True})
    with session as live:
        live.search_contacts("тестовый запрос")
        live.search_messages("hanoi_chats",
                             {"id": 1931920118, "access_hash": 77, "type": "group",
                              "auth_session_fingerprint": FP}, "слово")
    assert fake.contacts_calls[0]["options"]["allow_paid_stars"] is None
    assert fake.search_calls[0]["options"]["allow_paid_stars"] is None


def test_an_empty_query_is_refused_rather_than_answered_with_nothing(tmp_path):
    """`found: 0` from a search nobody ran is the silence this skill exists not
    to produce -- the same refusal `?q=` already makes on the free surface."""
    fake, session = searching(tmp_path)
    with session as live:
        with pytest.raises(account.AccountError):
            live.search_contacts("   ")
        with pytest.raises(account.AccountError):
            live.search_messages("hanoi_chats",
                                 {"id": 1931920118, "access_hash": 77,
                                  "type": "group", "auth_session_fingerprint": FP}, "")
    assert fake.contacts_calls == [] and fake.search_calls == []


def test_a_peer_cache_that_cannot_be_read_is_empty_rather_than_trusted(tmp_path):
    """The ledger fails closed because losing what it holds spends the account.
    Losing what THIS holds costs one `contacts.search`, and handing out a peer
    from a file we could not parse is the dangerous direction."""
    path = Path(tmp_path) / "peers.json"
    path.write_text("{not json", encoding="utf-8")
    cache = account.PeerCache(path)
    assert cache.get("hanoi_chats", FP) is None
    assert cache.read() == {}
    # It names the file and carries the reason, rather than going quietly empty.
    assert str(path) in cache.unreadable and "could not be read" in cache.unreadable
    assert cache.summary()["peer_cache_unreadable"]


def test_a_dropped_peer_is_really_gone(tmp_path):
    cache = account.PeerCache(Path(tmp_path) / "peers.json")
    cache.put(PEER_ROWS, FP)
    assert cache.drop("hanoi_chats") is True
    assert cache.get("hanoi_chats", FP) is None
    assert cache.get("vietnam_ru", FP) is not None, "drop took the wrong record"
    assert cache.drop("hanoi_chats") is False


def test_a_peer_that_could_never_be_handed_out_is_not_stored(tmp_path):
    """A record `peer_is_usable` would refuse on the way out is a lie about what
    the cache holds -- and `peers_cached: 3` over one usable peer is a number a
    report would carry."""
    cache = account.PeerCache(Path(tmp_path) / "peers.json")
    written = cache.put([
        {"username": "good", "id": 1, "access_hash": 2, "type": "group"},
        {"username": "", "id": 3, "access_hash": 4, "type": "group"},
        {"username": "nohash", "id": 5, "access_hash": None, "type": "group"},
    ], FP)
    assert written == 1
    assert set(cache.read()) == {"good"}


def test_a_peer_record_types_a_supergroup_and_skips_what_cannot_be_a_source():
    """`megagroup` is the only field that separates a public group from a channel
    on this surface, and it has to land in the SAME word the free landing card
    settles -- `verify` reads members for a group and subscribers for a channel."""
    group = SimpleNamespace(id=1, access_hash=7, username="hanoi_chats",
                            title="Чат", megagroup=True, broadcast=False,
                            participants_count=2835, verified=False, scam=False)
    channel = SimpleNamespace(id=2, access_hash=8, username="vietnam_ru",
                              title="Канал", megagroup=False, broadcast=True,
                              participants_count=51000, verified=True, scam=False)
    nameless = SimpleNamespace(id=3, access_hash=9, username=None, usernames=(),
                               title="private", megagroup=True)
    hashless = SimpleNamespace(id=4, access_hash=None, username="x", title="y",
                               megagroup=True)
    assert account._peer_record(group)["type"] == "group"
    assert account._peer_record(group)["participants"] == 2835
    assert account._peer_record(channel)["type"] == "channel"
    assert account._peer_record(nameless) is None
    assert account._peer_record(hashless) is None


# --------------------------------------------------------------------------
# Promises nothing was pinning: found by mutating the code and re-running
# --------------------------------------------------------------------------
def test_a_stale_peer_error_from_the_real_transport_is_named_not_generic(monkeypatch):
    """`PEER_STALE_ERROR_NAMES` lives in `TelethonTransport`, and every other
    test of the search path drives `FakeTransport` -- which raises `PeerUnusable`
    itself. So the branch that TRANSLATES Telegram's real refusal was covered by
    nothing, and `ChannelInvalidError` appeared in this tree only inside a
    docstring.

    It matters because the two outcomes are handled oppositely: `PeerUnusable`
    drops the cached hash and re-looks-up for one `contacts.search`, while a
    `TransportError` latches the run and stops it. Verified live 2026-08-25 by
    corrupting a stored hash; this is the offline pin under that measurement.
    """
    stale = type("ChannelInvalidError", (StubRPCError,), {})
    handle, transport = connected_transport(monkeypatch, raises=stale("CHANNEL_INVALID"))
    try:
        with pytest.raises(account.PeerUnusable) as exc:
            transport.search_messages({"id": 1, "access_hash": 2}, "слово")
        assert "contacts.search" in str(exc.value)
    finally:
        transport.close()

    # And an error OUTSIDE that family stays a transport failure, so the branch
    # cannot be widened into "every failure is a stale peer" and go unnoticed.
    other = type("SomethingElseError", (StubRPCError,), {})
    handle, transport = connected_transport(monkeypatch, raises=other("boom"))
    try:
        with pytest.raises(account.TransportError):
            transport.search_messages({"id": 1, "access_hash": 2}, "слово")
    finally:
        transport.close()


def test_the_fake_transport_pages_by_add_offset_like_telegram_does():
    """`add_offset` was asserted NOWHERE in this tree -- zero occurrences.

    It is the pagination parameter of `messages.search`, and `tg.py` pages with
    `add_offset=len(messages)`. A fake that ignored it would return page 1
    forever while every paging test stayed green, so a real paging bug would
    reach a report as duplicated or missing hits.
    """
    rows = [{"id": i, "date": None, "text": str(i), "author_id": None,
             "author_name": None, "author_username": None, "reply_to_id": None,
             "via": "mtproto"} for i in range(10)]
    fake = account.FakeTransport().with_hits(7, "слово", rows, total=10)
    peer = {"id": 7, "access_hash": 1}
    first = fake.search_messages(peer, "слово", limit=4, add_offset=0)
    second = fake.search_messages(peer, "слово", limit=4, add_offset=4)
    assert [m["id"] for m in first["messages"]] == [0, 1, 2, 3]
    assert [m["id"] for m in second["messages"]] == [4, 5, 6, 7]
    assert fake.search_calls[1]["add_offset"] == 4
    assert first["total"] == second["total"] == 10


# --------------------------------------------------------------------------
# The real transport, ANSWERING -- not only failing
# --------------------------------------------------------------------------
# The stub was taught both `SearchRequest`s and then driven in one direction
# only: every call to it went through `raises=`, and every `answer=` in this file
# is empty (`chats=[]`, `messages=[]`). So the half of each method that turns a
# response INTO something -- peer records, message records, the server's total --
# ran under no test at all, and a method returning a constant passed the whole
# suite. What follows lets it answer, and looks both at what it built and at what
# it put on the wire.


class _StubChat:
    """Shaped like a Telethon `Channel`: what `_peer_record` reads off a result."""

    def __init__(self, **kw):
        self.id = kw.get("id")
        self.access_hash = kw.get("access_hash")
        self.username = kw.get("username")
        self.usernames = kw.get("usernames", ())
        self.title = kw.get("title")
        self.megagroup = kw.get("megagroup", False)
        self.participants_count = kw.get("participants_count")
        self.verified = kw.get("verified", False)
        self.scam = kw.get("scam", False)


class _StubUser:
    def __init__(self, uid, username=None, first_name=None):
        self.id = uid
        self.username = username
        self.first_name = first_name
        self.last_name = None


class _StubMessage:
    def __init__(self, mid, text, sender_id=None, when=None, reply_to_msg_id=None):
        self.id = mid
        self.message = text
        self.sender_id = sender_id
        self.date = when
        self.reply_to = (SimpleNamespace(reply_to_msg_id=reply_to_msg_id)
                         if reply_to_msg_id else None)


def test_the_real_transport_builds_peer_records_out_of_the_chats_it_is_sent(monkeypatch):
    """`search_contacts` exists to turn `res.chats` into peer records.

    Nothing exercised that: with `chats=[]` everywhere, a method that simply
    returned `[]` passed the entire suite -- and `[]` from the search box is
    exactly what "this name does not exist" looks like to every caller above it,
    which is the one answer this skill must never fake.
    """
    answer = SimpleNamespace(
        chats=[
            # `scam=True` on this one and False on the next, so the fixture holds
            # both states. With False everywhere a hard-wired False passed, and
            # this field is not cosmetic: `verify` writes it into the registry
            # beside `username`, `type` and `participants`, so a group Telegram
            # has flagged for fraud would be admitted as clean and the next run
            # would read it as a source.
            _StubChat(id=1931920118, access_hash=77, username="hanoi_chats",
                      title="Big chat", megagroup=True, participants_count=2835,
                      scam=True),
            _StubChat(id=42, access_hash=99, username="vietnam_ru",
                      title="News channel", megagroup=False, participants_count=51000,
                      verified=True),
            # No username: it cannot be linked, verified or re-found, so it is
            # not a source and must not reach the caller.
            _StubChat(id=5, access_hash=6, username=None, title="private"),
            # No access_hash: nothing could be asked about it without a resolve.
            _StubChat(id=7, access_hash=None, username="hashless", title="x"),
        ],
        users=[], messages=[],
    )
    handle, transport = connected_transport(monkeypatch, answer=answer)
    try:
        rows = transport.search_contacts("test query", limit=25)
    finally:
        transport.close()

    assert [r["username"] for r in rows] == ["hanoi_chats", "vietnam_ru"]
    assert rows[0]["type"] == "group" and rows[1]["type"] == "channel"
    assert rows[0]["access_hash"] == 77 and rows[0]["id"] == 1931920118
    assert rows[0]["participants"] == 2835
    assert rows[0]["scam"] is True and rows[0]["verified"] is False
    assert rows[1]["verified"] is True and rows[1]["scam"] is False
    # What actually went on the wire, not what we meant to send.
    sent = handle.sent[0]
    assert sent.q == "test query", sent.kwargs
    assert sent.limit == 25, sent.kwargs


def test_the_real_transport_sends_the_query_and_the_offset_it_was_given(monkeypatch):
    """`q` and `add_offset` are the two fields that decide WHAT comes back.

    `add_offset` is the pagination parameter, and the only test naming it drove
    `FakeTransport` -- so the real request could carry a constant 0, page one
    would come back for ever, and the suite would stay green. `q` had the same
    hole from the other side: every test looked at a raised exception, none at
    the request.
    """
    answer = SimpleNamespace(messages=[], users=[], chats=[], count=0)
    handle, transport = connected_transport(monkeypatch, answer=answer)
    try:
        # 37, deliberately not 100: `MTPROTO_PAGE` is 100, so a mutant hard-wiring
        # `limit=100` would satisfy an assertion made against a call that asked
        # for 100. A field is only checked when its fixture value is one no
        # constant would produce by accident -- which is why `q` and `add_offset`
        # below were already catching their mutants and this line was not.
        transport.search_messages({"id": 1931920118, "access_hash": 77},
                                  "visaran", limit=37, add_offset=50)
    finally:
        transport.close()
    sent = handle.sent[0]
    assert sent.q == "visaran", sent.kwargs
    assert sent.add_offset == 50, sent.kwargs
    assert sent.limit == 37, sent.kwargs
    # The peer is a numeric InputPeerChannel, never a string Telethon would
    # resolve behind our back -- the rule the whole ledger rests on.
    assert isinstance(sent.peer, StubInputPeerChannel)
    assert (sent.peer.channel_id, sent.peer.access_hash) == (1931920118, 77)


def test_the_real_transport_reports_the_servers_total_not_the_page_size(monkeypatch):
    """`count` is the server's own number of matches, and `complete` is computed
    from it: a total that is always `len(rows)` makes every first page look like
    the whole answer.

    That is the `?q=` failure this route exists to avoid -- 21 hits reported as
    what a channel said about a subject -- arriving through the account instead.
    """
    when = datetime(2026, 3, 12, 0, 49, 8, tzinfo=timezone.utc)
    answer = SimpleNamespace(
        messages=[_StubMessage(28569, "visa question", sender_id=7, when=when),
                  _StubMessage(15597, "visa runs", sender_id=7, when=when)],
        users=[_StubUser(7, username="someone", first_name="Somebody")],
        chats=[], count=44,
    )
    handle, transport = connected_transport(monkeypatch, answer=answer)
    try:
        page = transport.search_messages({"id": 1, "access_hash": 2}, "visa")
    finally:
        transport.close()
    assert page["total"] == 44, "the page size was reported as the total"
    assert [m["id"] for m in page["messages"]] == [28569, 15597]
    assert page["messages"][0]["text"] == "visa question"
    assert page["messages"][0]["date"] == when.isoformat()
    # The senders travel in the same response; a raw request never populates
    # `msg.sender`, so the who used to be thrown away with it.
    assert page["messages"][0]["author_username"] == "someone"
    assert page["messages"][0]["author_name"] == "Somebody"


def test_a_response_that_carries_no_count_is_its_own_total(monkeypatch):
    """The other half of the same rule, written in a comment and checked nowhere:
    Telegram omits `count` on the small answer it gives when everything fits, and
    then the page IS the total. Without this, `total` could be read straight off
    `res.count` and a complete small answer would report `total: None` -- which
    `complete` reads as "not complete", for an answer that is.
    """
    answer = SimpleNamespace(
        messages=[_StubMessage(1, "one"), _StubMessage(2, "two")],
        users=[], chats=[],
    )
    assert not hasattr(answer, "count")
    handle, transport = connected_transport(monkeypatch, answer=answer)
    try:
        page = transport.search_messages({"id": 1, "access_hash": 2}, "visa")
    finally:
        transport.close()
    assert page["total"] == 2
