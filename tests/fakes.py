"""Stand-ins the suite drives the real code with.

They used to ship inside the skill -- `FakeTransport` in `account.py`, the two
loosened pacers in `tgweb.py` -- where a reader could mistake either for a
supported mode and every installed copy carried them. Nothing in the skill ever
constructed one, so they live here instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPTS = (Path(__file__).resolve().parent.parent
           / "skills" / "telegram-research" / "scripts")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from account import (  # noqa: E402
    FloodWait,
    PeerNotFound,
    PeerUnusable,
    Transport,
    _assert_free,
)
from tgweb import Pacer  # noqa: E402


class FakeTransport(Transport):
    """In-memory transport. Every safety rule in `account.py` is proved through it.

    Scriptable three ways, which is exactly what the rules need: answer with a
    peer, raise a FloodWait of N seconds, raise not-found. It records every call
    it receives, so a test can assert on what was NOT sent, which is the more
    important half here.
    """

    def __init__(self, peers: dict | None = None):
        self.peers: dict[str, dict] = dict(peers or {})
        self.floods: dict[str, int] = {}          # username, "*" for any, "history",
        #                                           "contacts.search", "messages.search"
        self.missing: set[str] = set()
        self.pages: dict[int, list] = {}          # peer id -> message records
        self.contacts: dict[str, list] = {}       # query -> peer records
        self.hits: dict[tuple, dict] = {}         # (peer id, query) -> {messages, total}
        self.stale: set = set()                   # access hashes Telegram refuses
        self.resolve_calls: list[dict] = []
        self.history_calls: list[dict] = []
        self.contacts_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.join_calls: list[dict] = []
        self.closed = False

    # -- scripting ---------------------------------------------------------
    def answer_with(self, username: str, peer_id: int, access_hash: int = 1234567890):
        self.peers[username] = {"id": int(peer_id), "access_hash": int(access_hash)}
        return self

    def flood_on(self, username: str, seconds: int = 36468):
        self.floods[username] = int(seconds)
        return self

    def not_found(self, username: str):
        self.missing.add(username)
        return self

    def with_history(self, peer_id: int, messages: list):
        self.pages[int(peer_id)] = list(messages)
        return self

    def with_contacts(self, query: str, rows: list):
        self.contacts[query] = list(rows)
        return self

    def with_hits(self, peer_id: int, query: str, messages: list, total: int | None = None):
        self.hits[(int(peer_id), query)] = {
            "messages": list(messages),
            "total": len(messages) if total is None else int(total),
        }
        return self

    def stale_peer(self, access_hash: int):
        """Script the one failure a permanent peer cache can cause.

        Keyed on the HASH, not the peer: a fake that refuses the whole peer
        cannot show the repair working.
        """
        self.stale.add(int(access_hash))
        return self

    # -- the two operations ------------------------------------------------
    def resolve_username(self, username: str, *, options: dict | None = None) -> dict:
        options = _assert_free(options)
        self.resolve_calls.append({"username": username, "options": options})
        seconds = self.floods.get(username, self.floods.get("*"))
        if seconds:
            raise FloodWait(seconds, f"contacts.resolveUsername @{username}")
        if username in self.missing or username not in self.peers:
            raise PeerNotFound(f"@{username} does not resolve")
        return dict(self.peers[username])

    def fetch_history(self, peer: dict, *, limit: int = 100, offset_id: int = 0,
                      options: dict | None = None) -> list[dict]:
        options = _assert_free(options)
        self.history_calls.append(
            {"peer": dict(peer), "limit": limit, "offset_id": offset_id, "options": options}
        )
        seconds = self.floods.get("history")
        if seconds:
            raise FloodWait(seconds, "messages.getHistory")
        rows = list(self.pages.get(int(peer.get("id", 0)), []))
        if offset_id:
            rows = [r for r in rows if int(r.get("id", 0)) < int(offset_id)]
        return rows[:limit]

    def search_contacts(self, query: str, *, limit: int = 50,
                        options: dict | None = None) -> list[dict]:
        options = _assert_free(options)
        self.contacts_calls.append({"query": query, "limit": limit, "options": options})
        seconds = self.floods.get("contacts.search")
        if seconds:
            raise FloodWait(seconds, "contacts.search")
        return [dict(row) for row in self.contacts.get(query, [])][:limit]

    def search_messages(self, peer: dict, query: str, *, limit: int = 50,
                        add_offset: int = 0, options: dict | None = None) -> dict:
        options = _assert_free(options)
        self.search_calls.append({"peer": dict(peer), "query": query, "limit": limit,
                                  "add_offset": add_offset, "options": options})
        seconds = self.floods.get("messages.search")
        if seconds:
            raise FloodWait(seconds, "messages.search")
        peer_id = int(peer.get("id", 0))
        if int(peer.get("access_hash", 0)) in self.stale:
            raise PeerUnusable(
                "messages.search: Telegram refused this peer (ChannelInvalidError). "
                "The cached access_hash is stale or the peer is not reachable "
                "from this account; look the name up again with contacts.search."
            )
        found = self.hits.get((peer_id, query), {"messages": [], "total": 0})
        rows = list(found["messages"])[add_offset:add_offset + limit]
        return {"messages": rows, "total": int(found["total"])}

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        """The real transport has one, so the stand-in has one: a caller that
        connects and then fails before the session opens must still close it."""
        self.closed = True

    # -- beyond the protocol -----------------------------------------------
    def join_group(self, peer: dict, *, options: dict | None = None) -> dict:
        options = _assert_free(options)
        self.join_calls.append({"peer": dict(peer), "options": options})
        return {"joined": True, "peer_id": int(peer.get("id", 0))}


class FastPacer(Pacer):
    """A Pacer with the gap floor lifted. Tests only, and only for the pacer's
    own tests: they exercise the reservation logic, which is about ordering and
    not about duration, and at the shipped 2-4 s gap the suite would spend
    minutes asleep to prove nothing extra.

    Never reachable from the CLI: `tg.py build_web` constructs `Pacer`, and
    nothing reads a class name out of the config. Anything that fetches for real
    uses `Pacer` and gets the floor.
    """

    enforce_gap_floor = False


class NullPacer(FastPacer):
    """A pacer that never sleeps. Tests only -- never reachable from the CLI."""

    def __init__(self) -> None:  # noqa: D107 - deliberately does not call super
        self.min_gap = self.max_gap = 0.0
        self.batch_size = 0
        self.batch_rest = 0.0
        self.path = Path(os.devnull)
        self.lock_path = Path(os.devnull)
        self.serialised_across_processes = False
        self.last_warning = None
        self.gap_floor_note = None

    def wait(self) -> float:
        return 0.0
