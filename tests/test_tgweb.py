"""tgweb.py -- classification of Telegram's HTTP surfaces, and the pacer.

Ground truth for every assertion here is the saved probe pages in
`tests/fixtures/probes/`, taken off live Telegram surfaces. The one thing this module exists to get right is that a refusal
arrives as HTTP 200 just as often as it arrives any other way, so the tests
below deliberately include bodies where the status code alone would mislead.
"""

from __future__ import annotations

import contextlib
import gzip
import http.client
import io
import json
import math
import os
import re
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import tgweb

# One live page, saved on 2026-08-24, that settles what a zero-hit `?q=` search
# looks like. The probes directory is frozen, so it lives with the repair.
#
# `TELEGRAM_RESEARCH_PAGES` is the same escape hatch `conftest.py` gives the
# probes corpus, and for the same reason its docstring already states: the suite
# has to be runnable against a copy of the skill sitting somewhere else. Without
# it that claim was false -- relocating the skill and setting
# TELEGRAM_RESEARCH_PROBES still failed here, because this path had no override.
PAGES = Path(os.environ.get("TELEGRAM_RESEARCH_PAGES")
                or (Path(__file__).resolve().parents[0] / "fixtures" / "pages"))
NO_HITS_PAGE = PAGES / "live-2026-08-24-s-durov-q-nohits.html"


# --------------------------------------------------------------------------
# Real servers, not mocks. Every HTTP finding pinned below was
# produced this way, and two of them (a dropped connection, a half gzip stream)
# cannot be reproduced any other way: they are properties of the socket, not of
# a Response object a test built by hand.
# --------------------------------------------------------------------------
@contextlib.contextmanager
def http_server(respond):
    """`respond(handler)` writes one response. Yields the base URL."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):                       # noqa: N802 - BaseHTTPRequestHandler API
            respond(self)

        def log_message(self, *args):           # keep pytest output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@contextlib.contextmanager
def raw_server(payload: bytes):
    """A socket that writes `payload` and hangs up. For truncated responses."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                conn.sendall(payload)
            except OSError:
                pass
            finally:
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()


def web(tmp_path, **kwargs):
    """A TelegramWeb that never sleeps: no pacing, no retry backoff."""
    kwargs.setdefault("pacer", tgweb.NullPacer())
    kwargs.setdefault("retry_backoff", 0.0)
    return tgweb.TelegramWeb(tmp_path, **kwargs)


# --------------------------------------------------------------------------
# landing-page classifiers, all four verified fixtures
# --------------------------------------------------------------------------
def test_landing_channel_durov(probe):
    body = probe("C01-landing-durov.html")
    assert tgweb.username_exists(body) is True
    assert tgweb.peer_type(body) == "channel"
    assert tgweb.member_count(body) == 11_110_268
    assert tgweb.online_count(body) is None


def test_landing_group_tdlibchat(probe):
    body = probe("A18-landing-tdlibchat.html")
    assert tgweb.username_exists(body) is True
    assert tgweb.peer_type(body) == "group"
    assert tgweb.member_count(body) == 16_674
    assert tgweb.online_count(body) == 362


def test_landing_nonexistent(probe):
    body = probe("C02-landing-nonexistent.html")
    assert tgweb.username_exists(body) is False
    assert tgweb.peer_type(body) is None
    assert tgweb.member_count(body) is None
    assert tgweb.online_count(body) is None


def test_landing_group_hanoi_chats(probe):
    # A03 is labelled "s-hanoi_chats" but its content is the landing card
    # reached after the /s/ redirect -- the same page A18 is, for a different
    # group.
    body = probe("A03-s-hanoi_chats.html")
    assert tgweb.username_exists(body) is True
    assert tgweb.peer_type(body) == "group"
    assert tgweb.member_count(body) == 2_832
    assert tgweb.online_count(body) == 37


# --------------------------------------------------------------------------
# preview_available -- the /s/ surface
# --------------------------------------------------------------------------
def test_preview_available_true_for_200_channel_body(probe):
    body = probe("A01-s-durov.html")
    resp = tgweb.Response(url="https://t.me/s/durov", status=200, body=body, bytes=len(body.encode("utf-8")))
    assert tgweb.preview_available(resp) is True


def test_preview_available_false_for_synthetic_302():
    # Measured on a real group's /s/ on 2026-08-23: it answers 302 to its own
    # landing URL with an empty body. The saved header/body pair that recorded
    # it was dropped from the corpus (it carried a live session cookie), and it
    # was never loaded here anyway: an empty body has nothing to read.
    resp = tgweb.Response(
        url="https://t.me/s/hanoi_chats",
        status=302,
        body="",
        location="https://t.me/hanoi_chats",
        bytes=0,
    )
    assert resp.redirected is True
    assert tgweb.preview_available(resp) is False


# --------------------------------------------------------------------------
# search_found_nothing. No saved probe hit this branch -- durov's rare-word search
# (C15) still matched 7 posts -- so this test used to build its input from
# tgweb's own constant, which meant it could not tell a wrong constant from a
# right one: replacing the constant with a string Telegram never sends left the
# whole suite green. It is settled against a real page further down, in
# test_search_found_nothing_on_the_real_zero_hit_page.
# --------------------------------------------------------------------------
def test_search_found_nothing_true():
    body = f'<html><body><div class="{tgweb.NO_MESSAGES_FOUND}">No results</div></body></html>'
    assert tgweb.search_found_nothing(body) is True


def test_search_found_nothing_false_on_a_real_results_page(probe):
    body = probe("A01-s-durov.html")
    assert tgweb.search_found_nothing(body) is False


# --------------------------------------------------------------------------
# post_missing
# --------------------------------------------------------------------------
def test_post_missing_true(probe):
    body = probe("C26-embed-hanoi-29320.html")
    assert tgweb.post_missing(body) is True


def test_post_missing_false(probe):
    body = probe("C26-embed-hanoi-29327.html")
    assert tgweb.post_missing(body) is False


# --------------------------------------------------------------------------
# stop_signal
# --------------------------------------------------------------------------
def test_stop_signal_429():
    resp = tgweb.Response(url="https://t.me/s/durov", status=429, body="", bytes=0)
    signal = tgweb.stop_signal(resp)
    assert signal is not None
    assert "429" in signal


def test_stop_signal_challenge_page():
    body = "<html><body>Just a moment... checking your browser (cf-browser-verification)</body></html>"
    resp = tgweb.Response(url="https://t.me/s/durov", status=403, body=body, bytes=len(body))
    signal = tgweb.stop_signal(resp)
    assert signal is not None
    assert "challenge" in signal.lower() or "blocked" in signal.lower()


def test_stop_signal_12_byte_body():
    resp = tgweb.Response(url="https://t.me/s/durov", status=200, body="tiny", bytes=12)
    signal = tgweb.stop_signal(resp)
    assert signal is not None
    assert "12" in signal


def test_stop_signal_none_for_a_healthy_page(probe):
    body = probe("C01-landing-durov.html")
    resp = tgweb.Response(url="https://t.me/durov", status=200, body=body, bytes=len(body.encode("utf-8")))
    assert tgweb.stop_signal(resp) is None


# --------------------------------------------------------------------------
# response_record -- never carries the body
# --------------------------------------------------------------------------
def test_response_record_never_contains_the_body(probe):
    body = probe("C01-landing-durov.html")
    resp = tgweb.Response(
        url="https://t.me/durov",
        status=200,
        body=body,
        headers={"content-type": "text/html; charset=utf-8", "location": None},
        bytes=len(body.encode("utf-8")),
        elapsed_ms=123,
    )
    record = tgweb.response_record(resp)
    assert "body" not in record
    dumped = json.dumps(record)
    # The landing page's title text is distinctive enough that its presence in
    # the JSON dump would mean the body leaked in some other field.
    assert "Telegram: View @durov" not in dumped


# --------------------------------------------------------------------------
# Pacer -- cross-process state file
# --------------------------------------------------------------------------
def test_pacer_writes_state_file(tmp_path):
    pacer = tgweb.FastPacer(tmp_path, min_gap=0.01, max_gap=0.01, batch_size=0, batch_rest=0.0)
    assert not pacer.path.exists()
    slept = pacer.wait()
    assert slept <= 0.02
    assert pacer.path.exists()
    state = json.loads(pacer.path.read_text(encoding="utf-8"))
    assert state["count"] == 1
    assert state["last"] > 0.0


def test_pacer_second_instance_sees_the_first_ones_reservation(tmp_path):
    # A fresh Pacer instance, same state_dir, same host -> same state file, and
    # the second one must wait out the first one's gap. The previous version of
    # this test asserted `slept <= 0.02`, an UPPER bound, which a pacer that
    # never sleeps at all also satisfies -- a mutation run made exactly that
    # change and the whole suite stayed green.
    first = tgweb.FastPacer(tmp_path, min_gap=0.2, max_gap=0.2, batch_size=0, batch_rest=0.0)
    first.wait()
    second = tgweb.FastPacer(tmp_path, min_gap=0.2, max_gap=0.2, batch_size=0, batch_rest=0.0)
    assert second.path == first.path

    started = time.time()
    slept = second.wait()
    elapsed = time.time() - started
    assert slept >= 0.15, "the second instance did not wait out the first one's gap"
    assert elapsed >= 0.15
    state, readable = second._read()
    assert readable is True
    assert state["count"] == 2


# The gap this test paces at. Small on purpose: the assertions below are on the
# SCHEDULE the pacer hands out, not on when the OS got round to waking a
# sleeping process, so the gap does not have to dominate scheduler jitter and
# the suite does not have to spend ten seconds proving one property.
PACER_GAP = 0.5

PACER_CHILD = textwrap.dedent(
    """
    import json, sys, time
    sys.path.insert(0, sys.argv[1])
    import tgweb

    state_dir, out, tag, gap = sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5])
    reserved = []

    class Recording(tgweb.FastPacer):
        # The real _reserve, with the instant it claimed written down. What this
        # class promises is the schedule it hands out; when the OS gets round to
        # waking the process afterwards is a property of the machine. `wait()`
        # is untouched and still calls this.
        def _reserve(self):
            due, chosen = super()._reserve()
            reserved.append(due)
            return due, chosen

    pacer = Recording(state_dir, min_gap=gap, max_gap=gap, batch_size=0, batch_rest=0.0)
    fired, notes = [], []
    for _ in range(3):
        pacer.wait()
        fired.append(time.time())
        notes.append(pacer.last_warning)
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"tag": tag, "reserved": reserved,
                             "fired": fired, "notes": notes}) + "\\n")
    """
)


def test_pacer_serialises_across_two_real_processes(tmp_path):
    """The defect this class exists to prevent, driven by two real processes.

    Measured before the fix, with min_gap = max_gap = 2.0: 3 of 7 requests fired
    under 1.0 s after the previous one, and one process died outright with
    `PermissionError: [WinError 5] ... 'pace-t.me.tmp' -> 'pace-t.me.json'`
    because every process shared one fixed temp-file name.

    **What this asserts, and why it is not the obvious thing.** The obvious
    thing -- take a wall-clock stamp after each `wait()` and require the stamps
    to be a gap apart -- measures when each child got round to recording a
    stamp, not when its request would have gone out. If one process is preempted
    between `wait()` returning and `time.time()` and the other is not, the
    recorded interval is `gap + delta_b - delta_a`, which drops under any fixed
    floor on scheduler jitter alone, with the pacer behaving perfectly. That is
    a test about the host, and a test that changes colour under load is worse
    than no test: the next real pacing regression gets waved through as "that
    one is flaky". It was observed going red once during a full-suite run under
    heavy parallel load, and 16 attempts to reproduce it (10 idle, 6 under six
    CPU spinners) never did -- which is exactly the signature.

    So the assertions are the two things `Pacer` actually promises, and neither
    can be moved by a late wake-up:

    1. **The reservations are a gap apart, across processes.** `_reserve` claims
       the next firing instant under an exclusive lock and writes it back before
       releasing, so `due(n+1) >= due(n) + gap` by construction. That is the
       invariant, and it is what the shipped read-then-wait version broke: two
       processes read the SAME `last`, computed the same due and fired together.
    2. **Nobody fires before the instant it reserved.** Jitter can only push a
       wake-up later, never earlier, so this catches a `wait()` that does not
       wait while tolerating a machine under load.

    Together those two also make the run take five gaps of real time, which is
    asserted last. The degraded paths stay safe under all three: a lock that
    could not be taken paces a full gap from `now` and only makes gaps longer,
    and any warning a child recorded is carried into the failure message so a
    red here can be read rather than guessed at.
    """
    scripts = str(Path(tgweb.__file__).parent)
    out = tmp_path / "stamps.jsonl"
    state = tmp_path / "state"
    state.mkdir()
    # A stale .pyc served an old constant during this repair pass, in an
    # ordinary run rather than under mutation. The child gets a fresh compile.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", PACER_CHILD, scripts, str(state), str(out),
             tag, str(PACER_GAP)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        for tag in ("A", "B")
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode("utf-8", "replace")

    runs = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(runs) == 2, runs
    said = [note for run in runs for note in run["notes"] if note]

    # 1. the schedule the two processes agreed on
    reserved = sorted(due for run in runs for due in run["reserved"])
    assert len(reserved) == 6
    gaps = [b - a for a, b in zip(reserved, reserved[1:])]
    assert min(gaps) >= PACER_GAP - 1e-6, f"reservations collided: {gaps} {said}"

    # 2. and nobody jumped its slot. The tolerance is clock granularity between
    # two time.time() calls, not a jitter allowance: a wait() that did not wait
    # is a whole gap early, three orders of magnitude outside this.
    for run in runs:
        for due, fired in zip(run["reserved"], run["fired"]):
            assert fired >= due - 0.05, (run["tag"], fired - due, said)

    # 3. so the six requests really did span five gaps of wall clock
    last_fired = max(f for run in runs for f in run["fired"])
    assert last_fired - reserved[0] >= 5 * PACER_GAP - 1e-6, said


def test_pacer_does_not_silently_stop_pacing_on_a_corrupt_state_file(tmp_path):
    # `_read` used to swallow OSError/ValueError and answer `{"last": 0.0}`, so
    # a truncated state file disabled pacing entirely and said nothing.
    for junk in ('{"last": 176', "", "not json at all", "[1,2,3]", '{"last": "soon"}'):
        pacer = tgweb.FastPacer(tmp_path, min_gap=0.2, max_gap=0.2, batch_size=0, batch_rest=0.0)
        pacer.path.write_text(junk, encoding="utf-8")
        slept = pacer.wait()
        assert slept >= 0.15, (junk, slept)
        assert pacer.last_warning is not None, junk


def test_a_pacer_that_cannot_take_the_lock_paces_a_full_gap_and_says_so(tmp_path, monkeypatch):
    """The branch that makes "the machine was loaded" a safe answer, not a scary one.

    A lock it cannot take within PACE_LOCK_TIMEOUT means another process is
    mid-reservation. The class does not pretend it serialised: it paces a full
    gap from the later of `last` and now, drops
    `serialised_across_processes` and says so. Every one of those makes the gap
    LONGER, never shorter -- which is why contention cannot be the explanation
    for a request firing early, and why the two-process test above does not
    treat a warning as a failure. Nothing exercised this path before.
    """
    # A queue two gaps deep, so `last` is genuinely in the future -- with a
    # reservation that merely equals `now` the two readings coincide and the
    # test proves nothing. `blocked`'s sleep_cap is 5 s, comfortably past the
    # 2 s standing reservation, so the "no queue can explain this" branch is
    # not what is being measured here. Nothing sleeps: `_reserve` only claims.
    ahead = tgweb.FastPacer(tmp_path, min_gap=2.0, max_gap=2.0, batch_size=0, batch_rest=0.0)
    ahead._reserve()
    standing, _gap = ahead._reserve()
    assert standing > time.time() + 1.5, "the queue is not standing in the future"

    blocked = tgweb.FastPacer(tmp_path, min_gap=0.5, max_gap=0.5, batch_size=0, batch_rest=0.0)
    assert blocked.sleep_cap > 2.0
    monkeypatch.setattr(tgweb.FastPacer, "_acquire", lambda self: False)

    due, gap = blocked._reserve()
    assert blocked.serialised_across_processes is False
    assert blocked.last_warning is not None
    assert "NOT serialised" in blocked.last_warning
    assert due >= standing + gap - 1e-6, "a lock timeout must not shorten the gap"
    assert due >= time.time()


def test_pacer_does_not_obey_a_timestamp_from_the_future(tmp_path):
    # A `last` one day ahead -- a clock change, or a state file copied between
    # machines -- used to make wait() sleep for 86 402 s (24.0 h), uncapped.
    pacer = tgweb.FastPacer(tmp_path, min_gap=0.05, max_gap=0.05, batch_size=0, batch_rest=0.0)
    pacer.path.write_text(
        json.dumps({"last": time.time() + 86_400, "count": 1}), encoding="utf-8"
    )
    started = time.time()
    slept = pacer.wait()
    assert slept <= pacer.sleep_cap + pacer.max_gap
    assert time.time() - started < 2.0
    assert pacer.last_warning is not None
    # and the bad number is repaired, so it does not cost every later request too
    state, _ = pacer._read()
    assert state["last"] < time.time() + 60


def test_pacer_reserves_rather_than_reading(tmp_path):
    # The reservation is what makes two processes take different slots: the
    # instant a caller INTENDS to fire is written before it sleeps, so the next
    # caller computes its slot from that instant, not from the last firing.
    # Reading alone is what shipped, and two processes then read the same
    # `last`, slept to the same instant and fired together.
    pacer = tgweb.FastPacer(tmp_path, min_gap=0.5, max_gap=0.5, batch_size=0, batch_rest=0.0)
    pacer.wait()                                    # nothing to pace against yet
    before = time.time()
    due, _gap = pacer._reserve()
    state, readable = pacer._read()
    assert readable is True
    assert state["last"] == due
    assert due >= before + 0.4, "the reservation must stand a full gap ahead"
    assert due > time.time(), "and it must be written BEFORE the sleep, not after"


def test_pacer_releases_its_lock(tmp_path):
    pacer = tgweb.FastPacer(tmp_path, min_gap=0.0, max_gap=0.0, batch_size=0, batch_rest=0.0)
    pacer.wait()
    assert not pacer.lock_path.exists()
    assert pacer.serialised_across_processes is True


def test_null_pacer_never_sleeps():
    pacer = tgweb.NullPacer()
    assert pacer.wait() == 0.0
    # It says what it is rather than claiming a guarantee it does not give.
    assert pacer.serialised_across_processes is False


# --------------------------------------------------------------------------
# fetch() against a real HTTP server -- statuses
# --------------------------------------------------------------------------
def _plain(handler, status, body=b"<html><body>x</body></html>", headers=()):
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    for key, value in headers:
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


@pytest.mark.parametrize("status", [500, 502, 504])
def test_a_5xx_raises_fetch_failed_after_its_retries(tmp_path, status):
    # Before this, `fetch` caught the HTTPError, kept the nginx error body and
    # returned an ordinary Response. The parsers found no messages in it, and a
    # group walk reported "N empty ids in a row -- treated as the end of what
    # this surface will serve". An outage was rendered as evidence of absence.
    seen = []
    with http_server(lambda h: (seen.append(h.path), _plain(h, status, b"<html>502 Bad Gateway</html>"))) as base:
        client = web(tmp_path)
        with pytest.raises(tgweb.FetchFailed) as excinfo:
            client.fetch(f"{base}/s/durov")
    assert str(status) in str(excinfo.value)
    assert base in str(excinfo.value)
    assert len(seen) == tgweb.MAX_RETRIES, "a 5xx must be retried before it is fatal"


def test_an_unexpected_4xx_raises_fetch_failed_without_retrying(tmp_path):
    seen = []
    with http_server(lambda h: (seen.append(h.path), _plain(h, 404, b"<html>404</html>"))) as base:
        client = web(tmp_path)
        with pytest.raises(tgweb.FetchFailed):
            client.fetch(f"{base}/durov/1?embed=1")
    assert len(seen) == 1, "a 404 is not a transport wobble; retrying it is noise"


def test_fetch_failed_is_not_run_aborted(tmp_path):
    # `read.py` and every caller above it depend on the distinction: FetchFailed
    # is one request that could not be read, RunAborted is Telegram telling the
    # run to stop.
    assert not issubclass(tgweb.FetchFailed, tgweb.RunAborted)
    assert not issubclass(tgweb.RunAborted, tgweb.FetchFailed)
    with http_server(lambda h: _plain(h, 500, b"<html>500</html>")) as base:
        client = web(tmp_path)
        with pytest.raises(tgweb.FetchFailed):
            client.fetch(f"{base}/s/durov")
        assert client.aborted_reason is None, "a 5xx must not abort the whole run"


def test_429_still_aborts_the_run(tmp_path):
    with http_server(lambda h: _plain(h, 429, b"<html>too many</html>")) as base:
        client = web(tmp_path)
        with pytest.raises(tgweb.RunAborted):
            client.fetch(f"{base}/s/durov")
        assert client.aborted_reason is not None


@pytest.mark.parametrize("status", [403, 503])
def test_403_and_503_still_abort_the_run(tmp_path, status):
    with http_server(lambda h: _plain(h, status, b"<html>nope</html>")) as base:
        client = web(tmp_path)
        with pytest.raises(tgweb.RunAborted):
            client.fetch(f"{base}/s/durov")


def test_a_302_is_still_data_and_not_a_failure(tmp_path):
    # The whole read route depends on this: `/s/` answers 302 for a group and
    # for a name that does not exist, and the 302 is the measurement.
    def respond(handler):
        handler.send_response(302)
        handler.send_header("Location", "http://127.0.0.1/hanoi_chats")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    with http_server(respond) as base:
        resp = web(tmp_path).fetch(f"{base}/s/hanoi_chats", follow=False)
    assert resp.status == 302
    assert resp.redirected is True
    assert tgweb.preview_available(resp) is False


# --------------------------------------------------------------------------
# fetch() against a real socket -- truncated responses
# --------------------------------------------------------------------------
def test_a_dropped_connection_becomes_a_telegram_web_error_naming_the_url(tmp_path):
    # http.client.IncompleteRead is an HTTPException, which none of the old
    # except clauses caught; it escaped fetch() raw, with no URL in it.
    head = (b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n"
            b"Content-Length: 5000\r\n\r\n")
    with raw_server(head + b"x" * 1200) as base:
        client = web(tmp_path)
        with pytest.raises(tgweb.TelegramWebError) as excinfo:
            client.fetch(f"{base}/s/durov")
    assert base in str(excinfo.value)


def test_a_half_gzip_stream_becomes_a_telegram_web_error_naming_the_url(tmp_path):
    # gzip.decompress raises EOFError on a truncated stream. EOFError is neither
    # an OSError nor a zlib.error, so it escaped both _decode and fetch().
    full = gzip.compress(b"<html><body>" + b"y" * 20000 + b"</body></html>")
    half = full[: len(full) // 2]
    head = (b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n"
            b"Content-Encoding: gzip\r\nContent-Length: " +
            str(len(half)).encode() + b"\r\n\r\n")
    with raw_server(head + half) as base:
        client = web(tmp_path)
        with pytest.raises(tgweb.TelegramWebError) as excinfo:
            client.fetch(f"{base}/s/durov")
    assert base in str(excinfo.value)


def test_a_truncated_deflate_body_never_reaches_the_parsers(tmp_path):
    # This one did not raise at all: _decode caught zlib.error and passed,
    # leaving the still-compressed bytes to be decoded with errors="replace".
    # The parsers then saw a page with no Post-not-found and no message wrap --
    # i.e. "empty", i.e. absence -- the same failure mode by another road.
    import zlib

    full = zlib.compress(b"<html><body>" + b"z" * 20000 + b"</body></html>")
    half = full[: len(full) // 2]
    head = (b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n"
            b"Content-Encoding: deflate\r\nContent-Length: " +
            str(len(half)).encode() + b"\r\n\r\n")
    with raw_server(head + half) as base:
        client = web(tmp_path)
        with pytest.raises(tgweb.TelegramWebError):
            client.fetch(f"{base}/s/durov")


# --------------------------------------------------------------------------
# saved originals -- bytes, and nobody's page overwritten
# --------------------------------------------------------------------------
def test_saved_original_is_the_bytes_that_were_served(tmp_path):
    # write_text on Windows rewrote every LF as CRLF, so the "original" was not
    # the page Telegram served and its size did not match the fetch log.
    body = "<html>\n<body>\nпривет\n" + "строка текста\n" * 100 + "</body>\n</html>\n"
    payload = body.encode("utf-8")
    assert len(payload) > tgweb.SUSPICIOUS_BODY_BYTES
    with http_server(lambda h: _plain(h, 200, payload)) as base:
        client = web(tmp_path, sources_dir=tmp_path / "sources")
        resp = client.fetch(f"{base}/s/durov", save_as="durov-head.html")

    saved = Path(resp.headers["x-saved-as"])
    assert saved.read_bytes() == payload
    assert b"\r\n" not in saved.read_bytes()
    assert resp.bytes == saved.stat().st_size, "the fetch log must describe the file on disk"


def test_response_bytes_is_the_decoded_size_not_the_wire_size(tmp_path):
    # `bytes` was len(raw), the on-the-wire size after gzip. A 20 050-character
    # page came back as bytes=102 and aborted the run with "HTTP 200 with only
    # 102 bytes". SUSPICIOUS_BODY_BYTES is documented against uncompressed sizes.
    plain = ("<html><body>" + "a" * 20_000 + "</body></html>").encode("utf-8")
    packed = gzip.compress(plain)
    assert len(packed) < tgweb.SUSPICIOUS_BODY_BYTES

    def respond(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Content-Length", str(len(packed)))
        handler.end_headers()
        handler.wfile.write(packed)

    with http_server(respond) as base:
        resp = web(tmp_path).fetch(f"{base}/s/durov")     # used to raise RunAborted
    assert resp.bytes == len(plain)
    assert resp.wire_bytes == len(packed)


def test_two_queries_do_not_overwrite_each_others_saved_original(tmp_path):
    # read._slug truncates the query at 24 characters, so two related Russian
    # queries in one run arrive here under the SAME label. The second page used
    # to replace the first silently, after which every quote from the first
    # query cited a file that did not contain it.
    pages = [b"<html><body>PAGE FOR CHEAP" + b" ." * 400 + b"</body></html>",
             b"<html><body>PAGE FOR EXPENSIVE" + b" ." * 400 + b"</body></html>"]
    served = iter(pages)
    with http_server(lambda h: _plain(h, 200, next(served))) as base:
        client = web(tmp_path, sources_dir=tmp_path / "sources")
        label = "chat-q-аренда-квартиры-недорого--0.html"
        first = client.fetch(f"{base}/a", save_as=label)
        second = client.fetch(f"{base}/b", save_as=label)

    path_a = Path(first.headers["x-saved-as"])
    path_b = Path(second.headers["x-saved-as"])
    assert path_a != path_b
    assert path_a.read_bytes() == pages[0]
    assert path_b.read_bytes() == pages[1]


def test_refetching_the_same_page_reuses_its_file(tmp_path):
    page = b"<html><body>same" + b" ." * 400 + b"</body></html>"
    with http_server(lambda h: _plain(h, 200, page)) as base:
        client = web(tmp_path, sources_dir=tmp_path / "sources")
        first = client.fetch(f"{base}/a", save_as="durov-head.html")
        second = client.fetch(f"{base}/a", save_as="durov-head.html")
    assert first.headers["x-saved-as"] == second.headers["x-saved-as"]
    assert len(list((tmp_path / "sources").iterdir())) == 1


def test_the_page_that_aborts_the_run_is_saved_to_disk(tmp_path):
    # stop_signal used to be evaluated before the save, so the single most
    # useful page to have on disk was the one page guaranteed never written.
    with http_server(lambda h: _plain(h, 200, b"tiny")) as base:
        client = web(tmp_path, sources_dir=tmp_path / "sources")
        with pytest.raises(tgweb.RunAborted):
            client.fetch(f"{base}/s/durov", save_as="durov-aborted.html")
    saved = list((tmp_path / "sources").iterdir())
    assert [p.name for p in saved] == ["durov-aborted.html"]
    assert saved[0].read_bytes() == b"tiny"


def test_the_fetch_log_sees_the_failing_response(tmp_path):
    logged = []
    with http_server(lambda h: _plain(h, 500, b"<html>500</html>")) as base:
        client = web(tmp_path, on_fetch=logged.append)
        with pytest.raises(tgweb.FetchFailed):
            client.fetch(f"{base}/s/durov")
    assert logged, "a failed fetch must still leave an audit trail"
    assert logged[-1].status == 500


# --------------------------------------------------------------------------
# the no-results marker, settled live on 2026-08-24
# --------------------------------------------------------------------------
def test_search_found_nothing_on_the_real_zero_hit_page():
    """The one live GET this constant rests on.

    `t.me/s/durov?q=zzqwxnonexistentterm12345`, HTTP 200, 18 727 bytes. The
    constant was right; the test around it was not. Telegram serves the notice
    INSIDE a `tgme_widget_message_wrap`, so `and MSG_WRAP not in body` cancelled
    the only condition that identifies a zero-hit search, and this returned
    False on every real one. A genuine zero-hit search therefore came back as
    `messages=0, exhausted=True, found_nothing=False` -- indistinguishable from
    a 502 outage and from "I read everything there was".
    """
    body = NO_HITS_PAGE.read_text(encoding="utf-8", errors="replace")
    assert tgweb.NO_MESSAGES_FOUND in body
    assert tgweb.MSG_WRAP in body               # the notice lives inside a wrap
    assert tgweb.search_found_nothing(body) is True


def test_the_no_results_marker_is_the_string_telegram_actually_sends():
    # The old test built its input from the constant, so replacing the constant
    # with "banana_marker_telegram_never_sends" left all 53 tests green. This
    # one reads the literal markup off the saved live page instead.
    body = NO_HITS_PAGE.read_text(encoding="utf-8", errors="replace")
    assert 'class="tme_no_messages_found">No posts found</div>' in body
    assert tgweb.NO_MESSAGES_FOUND == "tme_no_messages_found"


# --------------------------------------------------------------------------
# peer_type -- an unexpected body is not a personal account
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["C22-nontme-analytics.html",
                                  "D08-nontme-docs-botapi.html",
                                  "D02-nontme-docs-peers.html"])
def test_a_page_that_is_not_a_t_me_card_is_not_typed_at_all(probe, name):
    # All three came back `taken=True type=user`, and "it is a personal account"
    # is exactly the verdict that makes a run drop a source quietly instead of
    # reporting that something went wrong.
    #
    # These three are authored fixtures, not saved pages: the captures they
    # replace were 1.7 MB of two other sites' HTML, which this repository has no
    # business republishing. The first assertion below is what keeps them honest
    # -- each one still carries the readable, non-`Telegram: ` `og:title` that
    # was the whole cause of the wrong verdict, so an empty file would not pass.
    body = probe(name)
    title = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', body, re.I)
    assert title and title.group(1) and not title.group(1).startswith("Telegram: "), (
        f"{name} no longer reproduces the body that used to type as `user`")
    assert tgweb.is_peer_card(body) is False
    assert tgweb.peer_type(body) is None
    assert tgweb.name_taken(body) is False


def test_a_single_message_page_is_not_a_personal_account(probe):
    # C14 is t.me/tdlibchat/10000 without ?embed=1 -- a real t.me URL for a real
    # GROUP, which used to type as `user`.
    body = probe("C14-single-tdlibchat-10000.html")
    assert tgweb.peer_type(body) is None
    assert tgweb.name_taken(body) is True       # the name IS taken; it is a group's


def test_the_user_verdict_still_fires_for_a_real_personal_account(probe):
    # Built from the real C02 contact card by replacing only og:title, which is
    # the exact difference peer_type's docstring measured live on 2026-08-24:
    # a real account puts the person's display name there, a free name puts
    # the literal "Telegram: Contact @name".
    body = probe("C02-landing-nonexistent.html")
    assert tgweb.peer_type(body) is None
    taken = body.replace(
        'property="og:title" content="Telegram: Contact @zzqwxnonexistentchannel12345"',
        'property="og:title" content="Алекс Пример | Туры по Азии"',
    )
    assert taken != body, "the og:title line moved; the fixture needs rereading"
    assert tgweb.peer_type(taken) == "user"
    assert tgweb.name_taken(taken) is True


def test_every_real_landing_card_still_classifies(probe):
    assert tgweb.peer_type(probe("C01-landing-durov.html")) == "channel"
    assert tgweb.peer_type(probe("A18-landing-tdlibchat.html")) == "group"
    assert tgweb.peer_type(probe("A03-s-hanoi_chats.html")) == "group"
    assert tgweb.peer_type(probe("C02-landing-nonexistent.html")) is None


def test_a_single_post_page_cannot_say_whether_the_name_is_a_channel(probe):
    """`username_exists` may only answer off a peer card.

    `t.me/tdlibchat/10000` serves `<title>Telegram: View @tdlibchat</title>`,
    the same title the landing page serves, and this answered True off it. The
    "not every 200 is a peer card" rule was applied to the `user` verdict and to
    nothing else. `taken` still answers True: a post exists under the name,
    which settles the narrower question it asks.
    """
    body = probe("C14-single-tdlibchat-10000.html")
    assert tgweb.is_single_post_page(body) is True
    assert tgweb.is_peer_card(body) is False
    assert tgweb.username_exists(body) is None
    assert tgweb.name_taken(body) is True
    # and a real card is untouched
    card = probe("A18-landing-tdlibchat.html")
    assert tgweb.is_single_post_page(card) is False
    assert tgweb.username_exists(card) is True


def test_every_real_landing_card_still_answers_exists(probe):
    for name in ("C01-landing-durov.html", "A18-landing-tdlibchat.html",
                 "A03-s-hanoi_chats.html", "A17-s-tdlibchat.html"):
        assert tgweb.username_exists(probe(name)) is True, name
    assert tgweb.username_exists(probe("C02-landing-nonexistent.html")) is False


# --------------------------------------------------------------------------
# the classifiers are structural: user text can never answer for the page
# --------------------------------------------------------------------------
def test_no_embed_page_in_the_corpus_carries_a_message_wrap(probe):
    """The fact that made both `?embed=1` classifiers collapse to a substring.

    `MSG_WRAP not in body` was the second clause of `post_missing` and of
    `parse_embed`'s guard, and it is true on EVERY embed page -- the 9 with a
    real message and the 7 with an error alike -- so on the only surface that
    uses it the clause never did anything.
    """
    for name in ("C05-embed-durov-523.html", "C07-embed-tdlibchat-1.html",
                 "C10-embed-tdlibchat-10000.html", "C16-embed-hanoi-1000.html",
                 "C26-embed-hanoi-29327.html", "C08-embed-tdlibchat-50000.html",
                 "C26-embed-hanoi-29320.html"):
        assert tgweb.MSG_WRAP not in probe(name), name


def test_post_missing_reads_the_error_class_not_the_english_words(probe):
    body = probe("C08-embed-tdlibchat-50000.html")
    assert tgweb.ERR_MESSAGE in body
    assert tgweb.post_missing(body) is True
    # The prose is localisable -- the request sends Accept-Language: en,ru;q=0.9
    # -- and the class is not, so the verdict survives a wording change.
    assert tgweb.post_missing(body.replace(tgweb.POST_NOT_FOUND, "Message not found")) is True
    # and the structural marker alone is enough, with no English anywhere
    assert tgweb.post_missing(body.replace(tgweb.POST_NOT_FOUND, "Сообщение не найдено")) is True


def test_the_error_class_marks_exactly_the_seven_error_pages(fixtures):
    # `err_message` occurs 7 times in the 32 probes, on the 7 "Post not found"
    # pages, and nowhere else. That is what makes it usable as the marker.
    carriers = sorted(
        p.name for p in fixtures.iterdir()
        if tgweb._has_class(p.read_bytes().decode("utf-8", "replace"), tgweb.ERR_MESSAGE)
    )
    assert len(carriers) == 7, carriers
    for name in carriers:
        body = (fixtures / name).read_text(encoding="utf-8", errors="replace")
        assert tgweb.DATA_POST not in body, name
        assert tgweb.POST_NOT_FOUND in body, name


def test_a_post_quoting_post_not_found_is_not_a_missing_post(probe):
    body = probe("C10-embed-tdlibchat-10000.html")
    poisoned = body.replace("Default group permissions", "answers Post not found", 1)
    assert poisoned != body
    assert tgweb.POST_NOT_FOUND in poisoned
    assert tgweb.post_missing(poisoned) is False


def test_search_found_nothing_is_not_a_substring_search(probe):
    """One post containing the literal marker used to assert a whole page's
    silence -- and SKILL.md defines that as a genuine zero-hit search."""
    body = probe("A01-s-durov.html")
    assert tgweb.DATA_POST in body
    poisoned = body.replace("</body>", f"<div>{tgweb.NO_MESSAGES_FOUND}</div></body>")
    assert tgweb.NO_MESSAGES_FOUND in poisoned
    assert tgweb.search_found_nothing(poisoned) is False


def test_search_found_nothing_still_fires_on_the_real_page():
    body = NO_HITS_PAGE.read_text(encoding="utf-8", errors="replace")
    assert tgweb.DATA_POST not in body           # a real zero-hit page has no message
    assert tgweb.search_found_nothing(body) is True


def test_has_class_matches_a_class_token_not_a_substring():
    assert tgweb._has_class('<div class="a err_message b">x</div>', "err_message")
    assert tgweb._has_class("<div class='err_message'>x</div>", "err_message")
    assert not tgweb._has_class('<div class="err_message_2">x</div>', "err_message")
    assert not tgweb._has_class("<div>err_message</div>", "err_message")


def test_embed_unreadable_is_the_third_answer(probe):
    live = probe("C26-embed-hanoi-29327.html")
    gone = probe("C26-embed-hanoi-29320.html")
    wall = "<html><body>Join this group to view</body></html>"
    assert (tgweb.post_missing(live), tgweb.embed_unreadable(live)) == (False, False)
    assert (tgweb.post_missing(gone), tgweb.embed_unreadable(gone)) == (True, False)
    assert (tgweb.post_missing(wall), tgweb.embed_unreadable(wall)) == (False, True)


# --------------------------------------------------------------------------
# the gap floor -- a config file may widen the gap, never narrow it
# --------------------------------------------------------------------------
def test_a_config_cannot_lower_the_t_me_gap(tmp_path):
    """`TELEGRAM_RESEARCH_CONFIG` reaches `budgets.min_gap_sec` / `max_gap_sec`,
    and this class was the last place that could refuse a zero.

    Measured before the fix: `Pacer(d, min_gap=0, max_gap=0)` ran eight waits in
    0.046 s with `last_warning` still None, i.e. `--depth deep history` firing
    up to 800 requests at t.me as fast as the socket allows, against a host
    whose rate limit has never been measured.
    """
    pacer = tgweb.Pacer(tmp_path, min_gap=0.0, max_gap=0.0)
    assert pacer.min_gap == tgweb.DEFAULT_MIN_GAP
    assert pacer.max_gap == tgweb.DEFAULT_MAX_GAP
    assert pacer.gap_floor_note is not None
    # and it is the gap actually used, not merely the attribute
    _due, gap = pacer._reserve()
    assert gap >= tgweb.DEFAULT_MIN_GAP


@pytest.mark.parametrize("low, high", [(-5.0, -1.0), (0.5, 1.0), (0.0, 4.0), (2.0, 0.0)])
def test_every_gap_below_the_shipped_default_is_raised(tmp_path, low, high):
    pacer = tgweb.Pacer(tmp_path, min_gap=low, max_gap=high)
    assert pacer.min_gap >= tgweb.DEFAULT_MIN_GAP
    assert pacer.max_gap >= tgweb.DEFAULT_MAX_GAP
    assert pacer.gap_floor_note is not None


def test_a_wider_gap_is_accepted_untouched(tmp_path):
    pacer = tgweb.Pacer(tmp_path, min_gap=10.0, max_gap=20.0)
    assert (pacer.min_gap, pacer.max_gap) == (10.0, 20.0)
    assert pacer.gap_floor_note is None


def test_a_non_numeric_gap_falls_back_to_the_shipped_default(tmp_path):
    pacer = tgweb.Pacer(tmp_path, min_gap="fast", max_gap=None)
    assert (pacer.min_gap, pacer.max_gap) == (tgweb.DEFAULT_MIN_GAP, tgweb.DEFAULT_MAX_GAP)
    assert pacer.gap_floor_note is not None


def test_only_the_test_only_subclasses_lift_the_floor(tmp_path):
    assert tgweb.Pacer.enforce_gap_floor is True
    assert tgweb.FastPacer.enforce_gap_floor is False
    fast = tgweb.FastPacer(tmp_path, min_gap=0.01, max_gap=0.01)
    assert (fast.min_gap, fast.max_gap) == (0.01, 0.01)
    assert fast.gap_floor_note is None


# --------------------------------------------------------------------------
# every network act is counted and logged
# --------------------------------------------------------------------------
def _script_opener(monkeypatch, script, calls, effective=None):
    """Drive fetch() through a scripted opener. `script` items are
    `(status, body, headers)` or an exception to raise."""

    class _Handle:
        def __init__(self, url, status, body, headers):
            self.url = url
            self.status = status
            self._body = body
            self.headers = dict(headers)

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            calls.append(req.full_url)
            item = script[min(len(calls) - 1, len(script) - 1)]
            if isinstance(item, Exception):
                raise item
            status, body, headers = item
            if status >= 300:
                raise urllib.error.HTTPError(
                    req.full_url, status, "fake", headers, io.BytesIO(body)
                )
            return _Handle(effective or req.full_url, status, body, headers)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: _Opener())


HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}
PAGE = b"<html><body>" + b"x" * 2000 + b"</body></html>"


def test_a_transport_failure_is_a_network_act_and_is_logged(tmp_path, monkeypatch):
    """`fetchlog.jsonl` promises one line per network act.

    A dropped connection, a timeout or a half gzip stream was retried up to
    MAX_RETRIES times -- three real requests to t.me -- while `request_count`
    stayed 0, `on_fetch` was never called and the fetch log got no line. The
    5xx path next door accounted for its retries exactly, which is what made
    the asymmetry invisible on inspection: a flaky link during a deep run put
    twice the requests on the wire that `run.json` admitted to.
    """
    calls, logged = [], []
    _script_opener(monkeypatch, [http.client.IncompleteRead(b"abc")] * 5, calls)
    client = web(tmp_path, on_fetch=logged.append)
    with pytest.raises(tgweb.TelegramWebError):
        client.fetch("https://t.me/s/durov")

    assert len(calls) == tgweb.MAX_RETRIES
    assert client.request_count == len(calls)
    assert len(logged) == len(calls), "every request that reached t.me needs a line"
    assert [r.attempt for r in logged] == [1, 2, 3]
    assert all(r.status == 0 for r in logged)
    assert all("IncompleteRead" in (r.error or "") for r in logged)


def test_the_fetch_log_length_always_equals_the_request_count(tmp_path, monkeypatch):
    # The invariant the ceiling and `run.json` both rest on. It held on the 5xx
    # path and not on the transport path, and nothing asserted it either way.
    for script in (
        [http.client.IncompleteRead(b"abc")] * 5,
        [(502, PAGE, HTML_HEADERS)] * 5,
        [(502, PAGE, HTML_HEADERS), (200, PAGE, HTML_HEADERS)],
        [(200, PAGE, HTML_HEADERS)],
        [TimeoutError("timed out")] * 5,
    ):
        calls, logged = [], []
        _script_opener(monkeypatch, script, calls)
        client = web(tmp_path, on_fetch=logged.append)
        try:
            client.fetch("https://t.me/s/durov")
        except (tgweb.TelegramWebError, tgweb.FetchFailed):
            pass
        assert client.request_count == len(calls) == len(logged), script[0]


def test_a_retried_error_page_does_not_take_the_real_pages_filename(tmp_path, monkeypatch):
    """`notes/sources/` is what a `research` pass reads as evidence.

    The body is saved before the status is judged, so attempt 1's 502 landed at
    `<label>.html` and the page that actually answered at `<label>-2.html`.
    `Message.source_file` follows `x-saved-as` and still pointed at the right
    file, which is exactly why nothing caught it.
    """
    real = b"<html><body>REAL PAGE" + b" ." * 400 + b"</body></html>"
    calls = []
    _script_opener(monkeypatch, [(502, PAGE, HTML_HEADERS), (200, real, HTML_HEADERS)], calls)
    client = web(tmp_path, sources_dir=tmp_path / "sources")
    resp = client.fetch("https://t.me/s/durov", save_as="durov-head.html")

    assert Path(resp.headers["x-saved-as"]).name == "durov-head.html"
    assert (tmp_path / "sources" / "durov-head.html").read_bytes() == real
    assert (tmp_path / "sources" / "durov-head-http502.html").read_bytes() == PAGE


def test_the_page_that_aborts_the_run_still_keeps_its_own_name(tmp_path):
    # A 200 that trips the small-body stop signal is the real answer for that
    # URL: there is no successful attempt to collide with, so it keeps the name.
    with http_server(lambda h: _plain(h, 200, b"tiny")) as base:
        client = web(tmp_path, sources_dir=tmp_path / "sources")
        with pytest.raises(tgweb.RunAborted):
            client.fetch(f"{base}/s/durov", save_as="durov-aborted.html")
    assert [p.name for p in (tmp_path / "sources").iterdir()] == ["durov-aborted.html"]


# --------------------------------------------------------------------------
# where the body came from, and what it was encoded as
# --------------------------------------------------------------------------
def test_the_effective_url_of_a_followed_redirect_is_recorded(tmp_path, monkeypatch):
    """`follow=True` is every group read and every landing fetch, and urllib's
    answer to "which URL served this" was thrown away -- so nothing anywhere
    could ask whether the body in hand came from the URL that was requested."""
    calls = []
    _script_opener(monkeypatch, [(200, PAGE, HTML_HEADERS)], calls,
                   effective="https://t.me/joinchat/AAAAA")
    resp = web(tmp_path).fetch("https://t.me/somegroup", follow=True)
    assert resp.url == "https://t.me/somegroup"
    assert resp.url_effective == "https://t.me/joinchat/AAAAA"
    assert resp.followed_elsewhere is True


def test_a_body_served_from_the_url_that_was_asked_for_is_not_flagged(tmp_path, monkeypatch):
    calls = []
    _script_opener(monkeypatch, [(200, PAGE, HTML_HEADERS)], calls)
    resp = web(tmp_path).fetch("https://t.me/somegroup", follow=True)
    assert resp.followed_elsewhere is False


def test_a_meta_charset_page_is_decoded_by_its_own_declaration():
    # Only Content-Type was read. A page declaring windows-1251 in the document
    # alone came back as a run of U+FFFD, silently, with every Cyrillic word in
    # it destroyed and no flag anywhere on the Response.
    text = '<html><head><meta charset="windows-1251"></head><body>Привет</body></html>'
    raw = text.encode("cp1251")
    assert tgweb._decode_text(raw, {"content-type": "text/html"}) == text
    # The header still wins where it says something: a proxy that transcodes
    # rewrites the header and not the document.
    assert "Привет" in tgweb._decode_text(
        text.encode("utf-8"), {"content-type": "text/html; charset=utf-8"}
    )


def test_the_saved_original_is_the_bytes_that_came_off_the_wire(tmp_path):
    # The body was decoded and re-encoded as UTF-8 before it was written, so the
    # "original" was not what the server sent whenever the page was not UTF-8.
    # Measured: a 631-byte windows-1251 page landed on disk as 646 bytes.
    text = ('<html><head><meta charset="windows-1251"></head><body>Привет '
            + "текст " * 200 + "</body></html>")
    payload = text.encode("cp1251")

    def respond(handler):                    # no charset in the header: the
        handler.send_response(200)           # document's own declaration is
        handler.send_header("Content-Type", "text/html")     # all there is
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    with http_server(respond) as base:
        client = web(tmp_path, sources_dir=tmp_path / "sources")
        resp = client.fetch(f"{base}/x", save_as="cp1251.html")
    saved = Path(resp.headers["x-saved-as"])
    assert saved.read_bytes() == payload
    assert resp.bytes == len(payload) == saved.stat().st_size
    assert "Привет" in resp.body


def test_an_encoding_we_never_asked_for_never_reaches_a_parser(tmp_path):
    # Only gzip and deflate are advertised and only those two are understood.
    # A body under any other encoding used to be handed on still compressed --
    # a page with no markers in it, i.e. an "empty surface", i.e. absence.
    body = b"\x1b\x00\x00\x00" + b"not really a page" * 40

    def respond(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.send_header("Content-Encoding", "br")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    with http_server(respond) as base:
        with pytest.raises(tgweb.TelegramWebError):
            web(tmp_path).fetch(f"{base}/s/durov")


# --------------------------------------------------------------------------
# The gap floor and NaN
#
# `json.loads` accepts the bare literals `NaN`, `Infinity` and `-Infinity`, both
# pass `isinstance(x, float)`, and every comparison against NaN is false -- so
# `if low < DEFAULT_MIN_GAP` read a poisoned config as "nothing to enforce".
# Measured end to end through `TELEGRAM_RESEARCH_CONFIG` before the repair: ten
# `wait()` calls in 0.067 s with `gap_floor_note` and `last_warning` both None.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_gap_cannot_remove_the_pacing_floor(tmp_path, bad):
    pacer = tgweb.Pacer(tmp_path, min_gap=bad, max_gap=bad)
    assert pacer.min_gap == tgweb.DEFAULT_MIN_GAP
    assert pacer.max_gap == tgweb.DEFAULT_MAX_GAP
    assert pacer.gap_floor_note is not None
    _due, gap = pacer._reserve()
    assert math.isfinite(gap)
    assert gap >= tgweb.DEFAULT_MIN_GAP


def test_a_nan_gap_is_reachable_from_a_config_file_and_still_refused(tmp_path):
    # The input exactly as a config file delivers it: `json.loads` is what reads
    # TELEGRAM_RESEARCH_CONFIG, and it accepts this literal by default.
    budgets = json.loads('{"min_gap_sec": NaN, "max_gap_sec": Infinity}')
    assert math.isnan(budgets["min_gap_sec"])
    pacer = tgweb.Pacer(tmp_path, min_gap=budgets["min_gap_sec"],
                        max_gap=budgets["max_gap_sec"])
    assert (pacer.min_gap, pacer.max_gap) == (tgweb.DEFAULT_MIN_GAP, tgweb.DEFAULT_MAX_GAP)
    assert pacer.gap_floor_note is not None


def test_ten_requests_with_a_nan_gap_are_still_paced_apart(tmp_path):
    """The measurement, without spending 30 s asleep to make it.

    A reservation is the instant a caller intends to fire, so ten of them are
    the schedule ten requests would keep. With NaN through the floor,
    `random.uniform(nan, nan)` is `nan` and `max(now, floor + nan)` is `now`:
    all ten reservations landed on the same instant, i.e. every request fired
    with no gap at all, at a host whose rate limit has never been measured.
    """
    pacer = tgweb.Pacer(tmp_path, min_gap=float("nan"), max_gap=float("nan"))
    dues = [pacer._reserve()[0] for _ in range(10)]
    assert all(math.isfinite(d) for d in dues)
    assert dues[-1] - dues[0] >= 9 * tgweb.DEFAULT_MIN_GAP


# --------------------------------------------------------------------------
# The challenge detector: narrower, and right
#
# CHALLENGE_2026 is Cloudflare's current interstitial, reduced to its markers:
# the `cdn-cgi/challenge-platform` script, "Verifying you are human", the
# "needs to review the security of your connection" line and the `<noscript>`
# reading "Enable JavaScript and cookies to continue". The wording pair this
# module used to test for ("just a moment" AND "enable javascript") does not
# occur in it -- the "Just a moment" title belongs to an older revision of the
# page -- so the old detector let it through as an ordinary body.
# --------------------------------------------------------------------------
CHALLENGE_2026 = (
    "<!DOCTYPE html><html lang=\"en-US\"><head><title>t.me</title>"
    "<meta http-equiv=\"refresh\" content=\"390\"></head><body class=\"no-js\">"
    "<div class=\"main-wrapper\" role=\"main\"><div class=\"main-content\">"
    "<h1 class=\"zone-name-title h1\">t.me</h1>"
    "<h2 class=\"h2\" id=\"challenge-running\">Verifying you are human. This may "
    "take a few seconds.</h2>"
    "<noscript><div id=\"challenge-error-title\">Enable JavaScript and cookies to "
    "continue</div></noscript>"
    "<div id=\"challenge-body-text\" class=\"core-msg spacer\">t.me needs to review "
    "the security of your connection before proceeding.</div>"
    "</div></div>"
    "<script src=\"/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=9d3\">"
    "</script><div class=\"footer\"><div class=\"ray-id\">Ray ID: <code>9d3f0f0af</code>"
    "</div></div>" + "<!-- cf padding -->" * 40 + "</body></html>"
)
CHALLENGE_LEGACY = (
    "<html><head><title>Just a moment...</title></head><body>"
    "<div class=\"cf-browser-verification cf-im-under-attack\">Checking your browser "
    "before accessing t.me. Please enable JavaScript and cookies to continue."
    "</div>" + "<!-- padding -->" * 40 + "</body></html>"
)


def _quoting_a_challenge(body: str) -> str:
    """Plant a challenge page's own words into the first post's TEXT.

    The sentence any channel about scraping, bot development or Cloudflare
    writes routinely -- and exactly what a `?q=cloudflare` search surfaces on
    purpose.
    """
    start = body.find("js-message_text")
    opened = body.find(">", start)
    quote = ("Just a moment: verifying you are human, please enable JavaScript "
             "and cookies to continue -- see /cdn-cgi/challenge-platform/. ")
    return body[: opened + 1] + quote + body[opened + 1 :]


def test_a_post_that_quotes_a_challenge_page_is_not_a_challenge_page(probe):
    """A whole-body substring test on user prose, at HTTP 200.

    The exact defect class `search_found_nothing` and `post_missing` were both
    rewritten to eliminate. One post quoting a challenge page raised
    `RunAborted` on a body carrying twenty real posts, and `aborted_reason` is
    sticky, so every later fetch in the process raised too.
    """
    poisoned = _quoting_a_challenge(probe("A01-s-durov.html"))
    assert "just a moment" in poisoned.lower()
    assert "enable javascript" in poisoned.lower()
    assert tgweb.challenge_page(poisoned) is False

    resp = tgweb.Response(url="https://t.me/s/durov", status=200, body=poisoned,
                          bytes=len(poisoned.encode("utf-8")))
    assert tgweb.stop_signal(resp) is None


def test_a_peer_card_that_mentions_a_challenge_is_not_a_challenge(probe):
    # A landing card carries no `data-post` either, so a `data-post`-only gate
    # would still let a channel's own description abort a run.
    body = probe("C01-landing-durov.html")
    assert tgweb.DATA_POST not in body
    poisoned = body.replace(
        "Founder of Telegram.",
        "Just a moment -- enable JavaScript and cookies to continue", 1,
    )
    assert poisoned != body
    assert tgweb.challenge_page(poisoned) is False
    resp = tgweb.Response(url="https://t.me/durov", status=200, body=poisoned,
                          bytes=len(poisoned.encode("utf-8")))
    assert tgweb.stop_signal(resp) is None


def test_a_page_of_posts_quoting_a_challenge_does_not_abort_the_whole_run(tmp_path, probe):
    """`aborted_reason` is sticky: the second fetch is where the damage lands."""
    payload = _quoting_a_challenge(probe("A01-s-durov.html")).encode("utf-8")
    with http_server(lambda h: _plain(h, 200, payload)) as base:
        client = web(tmp_path)
        first = client.fetch(f"{base}/s/durov")
        assert first.status == 200
        assert client.aborted_reason is None
        second = client.fetch(f"{base}/s/durov")     # used to raise RunAborted
        assert second.status == 200


@pytest.mark.parametrize("page", [CHALLENGE_2026, CHALLENGE_LEGACY])
def test_a_real_interstitial_at_200_still_stops_the_run(page):
    """Narrower is only half the repair; the markers also have to be current.

    CHALLENGE_2026 carries neither half of the old wording pair, so the old
    detector returned None for it: the run carried on, the parsers found no
    messages in it, and an interstitial was read as an empty surface -- absence.
    """
    assert tgweb.challenge_page(page) is True
    resp = tgweb.Response(url="https://t.me/s/durov", status=200, body=page,
                          bytes=len(page.encode("utf-8")))
    signal = tgweb.stop_signal(resp)
    assert signal is not None
    assert "challenge" in signal.lower()


def test_the_old_wording_pair_would_have_missed_todays_page():
    # Stated as an assertion rather than a claim in a report: this is what makes
    # the test above fail on the old code rather than merely restate it.
    low = CHALLENGE_2026.lower()
    assert not ("just a moment" in low and "enable javascript" in low)


@pytest.mark.parametrize("status", [403, 503])
def test_a_challenge_at_403_or_503_is_named_as_one(status):
    resp = tgweb.Response(url="https://t.me/s/durov", status=status,
                          body=CHALLENGE_2026, bytes=len(CHALLENGE_2026))
    assert "challenge" in tgweb.stop_signal(resp)
    plain = tgweb.Response(url="https://t.me/s/durov", status=status,
                           body="<html>nope</html>", bytes=20)
    assert "challenge" not in tgweb.stop_signal(plain)


# --------------------------------------------------------------------------
# A challenge page must not take the clean evidence filename
# --------------------------------------------------------------------------
def test_a_challenge_page_never_takes_the_clean_evidence_filename(tmp_path):
    """`notes/sources/<label>.html` is what a `research` pass reads as the page.

    `_label_for` keyed on the status alone, and the one refusal this module
    recognises at status 200 is a challenge -- so an interstitial served for
    `durov-q-bitcoin.html` landed under that name and kept it: a later
    successful re-run writes different content, which `_write_original` gives a
    numbered sibling rather than an overwrite.
    """
    payload = CHALLENGE_2026.encode("utf-8")
    assert len(payload) > tgweb.SUSPICIOUS_BODY_BYTES     # not the small-body signal
    with http_server(lambda h: _plain(h, 200, payload)) as base:
        client = web(tmp_path, sources_dir=tmp_path / "sources")
        with pytest.raises(tgweb.RunAborted):
            client.fetch(f"{base}/s/durov?q=bitcoin", save_as="durov-q-bitcoin.html")

    saved = sorted(p.name for p in (tmp_path / "sources").iterdir())
    assert saved == ["durov-q-bitcoin-challenge.html"]
    assert not (tmp_path / "sources" / "durov-q-bitcoin.html").exists()
    # and the page itself is still on disk -- the one page most worth having
    assert (tmp_path / "sources" / saved[0]).read_bytes() == payload


def test_the_small_body_stop_signal_still_keeps_the_clean_name(tmp_path):
    # Deliberately unchanged: that body IS the answer this URL gave, there is no
    # successful attempt to collide with. Recorded
    # here so the difference reads as a decision rather than an oversight.
    with http_server(lambda h: _plain(h, 200, b"tiny")) as base:
        client = web(tmp_path, sources_dir=tmp_path / "sources")
        with pytest.raises(tgweb.RunAborted):
            client.fetch(f"{base}/s/durov", save_as="durov-head.html")
    assert [p.name for p in (tmp_path / "sources").iterdir()] == ["durov-head.html"]


# --------------------------------------------------------------------------
# search_found_nothing -- the `data-post` half, which nothing exercised
# --------------------------------------------------------------------------
def test_a_page_carrying_real_posts_can_never_assert_its_own_silence(probe):
    """`found_nothing: true` is the strongest positive claim the skill makes.

    Deleting `if DATA_POST in body: return False` left all 703 tests green:
    `test_search_found_nothing_is_not_a_substring_search` plants the marker into
    a post's TEXT, where `_has_class` never matches either way. Planted as an
    element's CLASS on a page that also carries messages -- the only input that
    separates the two halves -- twenty real posts came back as proven silence.
    """
    body = probe("A01-s-durov.html")
    poisoned = body.replace(
        "</body>",
        '<div class="tgme_widget_message_centered">'
        '<div class="%s">No posts found</div></div></body>' % tgweb.NO_MESSAGES_FOUND,
    )
    assert poisoned != body
    assert tgweb._has_class(poisoned, tgweb.NO_MESSAGES_FOUND) is True   # a real element
    assert tgweb.DATA_POST in poisoned                                   # and real posts
    assert tgweb.search_found_nothing(poisoned) is False


# --------------------------------------------------------------------------
# peer_type -- an unreadable card is not a person
# --------------------------------------------------------------------------
def test_a_channel_card_that_is_not_in_english_is_not_a_personal_account(probe):
    """`subscriber` / `member` are English words; the fall-through was not None.

    Below them sits the `og:title` test, which is not a test for a personal
    account at all -- it is "the title is not the literal `Telegram: Contact
    @name`", which every real channel's title also satisfies. So a channel card
    served in another language typed as `user`, the one verdict the docstring
    says must not reach the registry, while `member_count` returned None at the
    same moment and left the members guard nothing to refuse it with.

    Two comments in this file insist nothing may depend on the
    `Accept-Language: en,ru;q=0.9` header. This function did.
    """
    body = probe("C01-landing-durov.html")
    assert tgweb.peer_type(body) == "channel"
    localised = body.replace("subscribers", "подписчиков")
    assert localised != body
    assert "subscriber" not in tgweb._page_extra(localised).lower()

    assert tgweb.peer_type(localised) is None      # not "user"
    assert tgweb.member_count(localised) is None
    assert tgweb.is_peer_card(localised) is True   # it is still plainly a card


def test_a_free_name_and_a_real_account_are_unchanged(probe):
    # The `user` verdict survives, because a contact card carries no
    # `tgme_page_extra` at all -- which is what makes the repair free.
    body = probe("C02-landing-nonexistent.html")
    assert tgweb._page_extra(body) is None
    assert tgweb.peer_type(body) is None
    taken = body.replace(
        'property="og:title" content="Telegram: Contact @zzqwxnonexistentchannel12345"',
        'property="og:title" content="Алекс Пример | Туры по Азии"',
    )
    assert taken != body
    assert tgweb.peer_type(taken) == "user"
