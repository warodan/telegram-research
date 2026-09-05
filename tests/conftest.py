"""Shared plumbing for the telegram-research test suite.

Everything here runs offline. The suite sits in the repository root, beside the
skill rather than inside it, so that none of it ships to whoever installs the
skill. `skills/telegram-research/scripts/` is added to `sys.path` because every
script in it imports flat (`import tgweb`, not `from . import tgweb`).
`FakeWeb` gives `read.py` a drop-in stand-in for
`tgweb.TelegramWeb` that answers from a dict instead of the network, so the
orchestration functions in `read.py` can be driven end to end with zero HTTP
calls anywhere in this suite.
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = REPO_ROOT / "skills" / "telegram-research"
SCRIPTS = SKILL_ROOT / "scripts"
# The full probe corpus lives HERE, in the repository, not in the skill folder:
# the installer copies the skill folder verbatim to whoever installs it, and
# 32 saved pages are development weight nobody who runs the skill ever reads.
# What travels with the skill is the 10 probes `tg.py selftest` opens, in the
# same relative place (`<skill>/tests/fixtures/probes/`), so that command --
# a production path, not a test -- works from an installed copy with no file
# from this repository. The env override stays, for pointing either the suite
# or `selftest` at a corpus kept somewhere else on purpose.
#
# What is here is itself a SUBSET. The measurements quoted throughout this
# suite and in `references/surfaces.md` were taken on the original corpus of 58
# saved pages; pages were dropped from the public copy to protect the privacy
# of third parties, and 32 remain. A count of the form "N of 58" is a
# measurement, not a claim about this directory.
PROBES = Path(os.environ.get("TELEGRAM_RESEARCH_PROBES")
              or (REPO_ROOT / "tests" / "fixtures" / "probes"))

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import tgweb  # noqa: E402 -- must follow the sys.path edit above


@pytest.fixture
def fixtures() -> Path:
    """The directory holding the 32 saved probe pages -- the test corpus."""
    assert PROBES.is_dir(), f"probes directory not found: {PROBES}"
    return PROBES


@pytest.fixture
def probe(fixtures: Path):
    """`probe(name)` -> the file's text, decoded as utf-8 with errors replaced.

    `errors="replace"` matches how `tgweb._decode` reads a live response: a
    saved probe must be readable the same way a fresh fetch would be, even if
    a byte in it does not round-trip cleanly.
    """

    def _read(name: str) -> str:
        path = fixtures / name
        return path.read_text(encoding="utf-8", errors="replace")

    return _read


def landing_url(username: str) -> str:
    return f"{tgweb.BASE}/{tgweb._uname(username)}"


def preview_url(username: str, *, query: str | None = None, before: int | None = None,
                after: int | None = None) -> str:
    params: dict[str, str] = {}
    if query:
        params["q"] = query
    if before is not None:
        params["before"] = str(before)
    if after is not None:
        params["after"] = str(after)
    url = f"{tgweb.BASE}/s/{tgweb._uname(username)}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return url


def embed_url(username: str, message_id: int) -> str:
    return f"{tgweb.BASE}/{tgweb._uname(username)}/{int(message_id)}?embed=1"


class FakeWeb:
    """Answers `TelegramWeb`'s surface from a caller-supplied mapping.

    `responses` maps an exact URL (built the same way `TelegramWeb` builds it)
    to a `tgweb.Response`, or to a plain dict of `Response` kwargs (`status`,
    `body`, `location`, `bytes`, ...) which gets wrapped into one lazily. A URL
    that was never mapped raises `KeyError` naming the URL, so a test with a
    wrong assumption about what read.py requests fails loudly instead of
    hanging or silently returning nothing.

    `follow` is honoured the way `TelegramWeb.fetch` honours it: `landing` and
    `embed` pass `follow=True` and this class chases `Response.location`
    through the mapping until a non-redirect turns up; `preview` passes
    `follow=False` and the 3xx (a group, or a name that does not exist) comes
    back untouched, exactly like the real `_NoRedirect`
    handler leaves it for the caller to classify.
    """

    def __init__(self, responses: dict):
        self.responses = dict(responses)
        self.request_count = 0
        self.calls: list[str] = []

    def _lookup(self, url: str) -> tgweb.Response:
        entry = self.responses.get(url)
        if entry is None:
            raise KeyError(f"FakeWeb has no mapped response for {url!r}")
        if isinstance(entry, tgweb.Response):
            return entry
        kwargs = dict(entry)
        kwargs.setdefault("url", url)
        kwargs.setdefault("body", "")
        kwargs.setdefault("bytes", len(kwargs["body"].encode("utf-8")))
        return tgweb.Response(**kwargs)

    def fetch(self, url: str, *, follow: bool = False, save_as: str | None = None) -> tgweb.Response:
        self.calls.append(url)
        self.request_count += 1
        resp = self._lookup(url)
        chased: set[str] = {url}
        while follow and resp.redirected and resp.location:
            if resp.location in chased:
                raise RuntimeError(f"FakeWeb redirect loop at {resp.location!r}")
            chased.add(resp.location)
            resp = self._lookup(resp.location)
        return resp

    # -- surfaces, built through the same helpers a test uses to key its map
    def landing(self, username: str, *, save_as: str | None = None) -> tgweb.Response:
        return self.fetch(landing_url(username), follow=True, save_as=save_as)

    def preview(
        self,
        username: str,
        *,
        query: str | None = None,
        before: int | None = None,
        after: int | None = None,
        save_as: str | None = None,
    ) -> tgweb.Response:
        url = preview_url(username, query=query, before=before, after=after)
        return self.fetch(url, follow=False, save_as=save_as)

    def embed(self, username: str, message_id: int, *, save_as: str | None = None) -> tgweb.Response:
        return self.fetch(embed_url(username, message_id), follow=True, save_as=save_as)


# --------------------------------------------------------------------------
# Driving the CLI end to end, with no network anywhere
# --------------------------------------------------------------------------
# `FakeWeb` above stands in for `TelegramWeb` and is right for testing `read.py`.
# It is the wrong tool for testing `tg.py`, because the things `tg.py` gets wrong
# live BELOW it: whether a page reaches `notes/sources/`, whether the fetch log
# records the act, whether a stop signal becomes JSON instead of a traceback.
# So the fake goes one layer lower -- at `urllib.request.build_opener` -- and
# everything above it is the real code.


class _FakeHandle:
    """What `urlopen` returns: a context manager with `read`, `status`, `headers`."""

    def __init__(self, url: str, status: int, body: bytes, headers: dict):
        self.url = url
        self.status = status
        self._body = body
        self._pos = 0
        self.headers = dict(headers)

    def read(self, amt: int | None = None) -> bytes:
        """`http.client`'s signature: `read(n)` returns AT MOST n bytes.

        `tgweb.fetch` reads a body in bounded chunks, so that a body far larger
        than any real page cannot be buffered whole and a body that only
        trickles cannot run for ever. A handle that ignored the size would make
        both of those untestable and would not be what urllib hands it.
        """
        if amt is None or amt < 0:
            chunk, self._pos = self._body[self._pos:], len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + amt]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSite:
    """A dict of URL -> canned response, served through the real transport.

    `follow` is honoured the way urllib honours it, because that is what
    `tgweb.fetch` depends on: with no redirect handler installed a 3xx is chased;
    with `_NoRedirect` installed urllib raises `HTTPError`, which `tgweb` catches
    and hands to the caller as a 302 to classify.
    """

    def __init__(self):
        self.pages: dict[str, tuple[int, bytes, dict]] = {}
        self.requested: list[str] = []

    def add(self, url: str, body: str = "", *, status: int = 200,
            location: str | None = None, headers: dict | None = None) -> None:
        head = {"content-type": "text/html; charset=utf-8"}
        if location:
            head["location"] = location
        head.update(headers or {})
        self.pages[url] = (status, body.encode("utf-8"), head)

    def add_bytes(self, url: str, body: bytes, *, status: int = 200,
                  headers: dict | None = None) -> None:
        head = {"content-type": "text/html; charset=utf-8"}
        head.update(headers or {})
        self.pages[url] = (status, body, head)

    # -- the opener --------------------------------------------------------
    def _entry(self, url: str):
        if url not in self.pages:
            raise AssertionError(f"FakeSite has no page for {url!r}")
        return self.pages[url]

    def opener(self, *handlers):
        follow = not handlers          # tgweb passes _NoRedirect when follow=False
        site = self

        class _Opener:
            def open(self, req, timeout=None):
                url = req.full_url if hasattr(req, "full_url") else str(req)
                site.requested.append(url)
                status, body, headers = site._entry(url)
                seen = {url}
                while follow and status in (301, 302, 303, 307, 308):
                    nxt = headers.get("location")
                    if not nxt or nxt in seen:
                        break
                    seen.add(nxt)
                    site.requested.append(nxt)
                    status, body, headers = site._entry(nxt)
                    url = nxt
                if status >= 300:
                    raise urllib.error.HTTPError(
                        url, status, "fake", headers, io.BytesIO(body)
                    )
                return _FakeHandle(url, status, body, headers)

        return _Opener()


@pytest.fixture
def site(monkeypatch) -> FakeSite:
    """The offline internet, plus a pacer that does not sleep for four seconds."""
    fake = FakeSite()
    monkeypatch.setattr(urllib.request, "build_opener", fake.opener)
    monkeypatch.setattr(tgweb.Pacer, "wait", lambda self: 0.0)
    return fake


@dataclass
class CliResult:
    exit_code: int
    stdout: str
    json: dict | None


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    """Run `tg.py` in-process against a scratch state directory.

    In-process rather than through a subprocess so a traceback is a test failure
    with a stack rather than a string to grep, and so nothing inherits this
    machine's real `TELEGRAM_RESEARCH_*` variables.
    """
    monkeypatch.setenv("TELEGRAM_RESEARCH_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("TELEGRAM_RESEARCH_CONFIG", raising=False)
    monkeypatch.delenv("TELEGRAM_RESEARCH_ENV", raising=False)
    # The live switch was the one variable this fixture did not clear, which made
    # every CLI test's answer depend on how the operator's shell happened to be
    # configured. Harmless while `tg.py` reads no account path -- and exactly the
    # kind of machine-dependent test this suite has been burnt by before.
    monkeypatch.delenv("TELEGRAM_RESEARCH_ALLOW_LIVE", raising=False)
    import tg

    def run(*argv) -> CliResult:
        capsys.readouterr()
        code = tg.main([str(a) for a in argv])
        out = capsys.readouterr().out
        try:
            payload = json.loads(out)
        except ValueError:
            payload = None
        return CliResult(code, out, payload)

    return run


# --------------------------------------------------------------------------
# The offline guarantee, enforced rather than promised
# --------------------------------------------------------------------------
# The docstring at the top of this file has always said "everything here runs
# offline", and until 2026-08-25 nothing made that true: four CLI tests ran
# `tg.main()` with the real opener installed, so a command that regressed into
# fetching would have reached `t.me` from a test run and nobody would have seen
# it -- the request would simply have succeeded.
#
# Loopback is left open on purpose. The suite's two-process tests are real
# processes, and blocking every socket would make this guard a source of
# failures unrelated to the thing it guards against. What must never happen is
# a connection leaving the local host.
_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


def _host_of(address) -> str:
    if isinstance(address, (tuple, list)) and address:
        return str(address[0])
    return str(address)


@pytest.fixture(autouse=True, scope="session")
def no_outbound_network():
    """Fail loudly on any connection to anywhere but the local host."""
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection

    def refuse(where: str, address):
        raise AssertionError(
            f"the test suite tried to open a network connection to {where!r} "
            "-- this suite is offline by contract. Something under test is "
            "reaching the real Telegram instead of a fake: check that the test "
            "installs `FakeWeb`, a stubbed opener or a stubbed telethon, and "
            "never the real one. (Loopback is allowed; this was not loopback.)"
        )

    def guarded_connect(self, address):
        host = _host_of(address)
        if host not in _ALLOWED_HOSTS:
            refuse(host, address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        host = _host_of(address)
        if host not in _ALLOWED_HOSTS:
            refuse(host, address)
        return real_connect_ex(self, address)

    def guarded_create(address, *args, **kwargs):
        host = _host_of(address)
        if host not in _ALLOWED_HOSTS:
            refuse(host, address)
        return real_create(address, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
        socket.create_connection = real_create


# --------------------------------------------------------------------------
# The machine's own credential must not reach the suite
# --------------------------------------------------------------------------
CREDENTIAL_VARS = ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION")


@pytest.fixture(autouse=True)
def _no_ambient_credential(monkeypatch):
    """Strip the three credential variables from the environment for every test.

    `read_credentials` accepts the credential straight from the environment, so
    on a machine that has all three set as user variables the five tests that
    exercise the FILE path stop raising: the suite goes red there and stays green
    in a shell without them, and the failure reads as a code bug when it is a
    test-isolation bug. A test that wants them sets them itself.
    """
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
