# The account — when it may be touched, and the accounting that gates it

- [The rule](#the-rule)
- [How it is reached at all — there is no command](#how-it-is-reached-at-all--there-is-no-command)
- [The measured incident, in full](#the-measured-incident-in-full)
- [The published ceiling, and its provenance](#the-published-ceiling-and-its-provenance)
- [`access_hash` is bound to the login/auth session](#access_hash-is-bound-to-the-loginauth-session)
- [The peer cache — `<state_dir>/peers.json`](#the-peer-cache--state_dirpeersjson)
- [Why an accountless resolve cannot exist](#why-an-accountless-resolve-cannot-exist)
- [`channels.searchPosts` is closed — do not plan around it](#channelssearchposts-is-closed--do-not-plan-around-it)
- [Pacing rules, and the constant in `resolve.py` that enforces each](#pacing-rules-and-the-constant-in-resolvepy-that-enforces-each)
- [Lifting a freeze — and the bound on one](#lifting-a-freeze--and-the-bound-on-one)
- [The single-writer lock](#the-single-writer-lock)
- [Where the state lives](#where-the-state-lives)
- [Credentials](#credentials)

## The rule

**The free surface is the default. The account is what a GROUP needs.** For a channel
the account is never touched at all — `/s/`, `?q=`, `?before=` give complete history and
search with zero account cost (`surfaces.md`, rows 2-4).

**A group has no free search surface, and that is not a matter of price.** Its only
accountless surface reads ONE id you already have (`surfaces.md` row 5), and about one id
in a hundred answers; measured live 2026-08-25, finding ten messages containing one
ordinary word that way would have cost ≈199 000 requests against the 29 327 ids the group
contains. No budget makes that work. So the account does three jobs here, not one:

| job | call | measured cost |
| --- | --- | --- |
| find sources by title or username | `contacts.search` | 1 call, **0 resolves**, and the peer comes back with its `access_hash` |
| search one group's whole history | `messages.search` | 1 call per page of ≤100 hits; one common word on a busy group → 44 hits in one call |
| read a group's recent messages | `messages.getHistory` | 1 call per page of 100. Live 2026-08-26: `history <group> --max-pages 1` → 1 call, 100 messages |

**`contacts.resolveUsername` is off the ordinary path entirely.** It is the one call that
ever cost this account downtime, and `contacts.search` answers the same question — who is
this peer, and what is its access hash — in one call for names it knows. The resolve, its
ledger, its ceilings and its 30 s gap all stay, as the fallback for a name the search box
will not return. Reaching it means writing a script against `AccountSession.resolve`;
no CLI flag can.

**Set `TELEGRAM_RESEARCH_ALLOW_LIVE` on the command, not in your shell profile.** From the
command line the variable is the only switch there is, so a profile that exports it leaves
the account open for every run in every project, including the ones that only meant to read
a channel. `TELEGRAM_RESEARCH_ALLOW_LIVE=1 tg.py search <group> --query "..."` opens it for
exactly one command.

## How it is reached at all — there is no command

**`tg.py` has no account subcommand and that is deliberate.** Nothing on the
command line performs a resolve, a join or a bulk history read. What exists:

* `tg.py budget` — the resolve ledger, printed. No network, no credential.
  `tg.py budget --unfreeze --reason "..."` is the one thing here that WRITES: it
  lifts a freeze first, then appends what it lifted to `<ledger>.freezes.jsonl`
  on a best-effort basis. See "Lifting a freeze" below before using it.
* `python scripts/account.py` — the same, plus the account-call budget, the peer
  cache, `telethon_installed` and `live_enabled_in_env`. It is a **status command
  and nothing else**; it parses no arguments beyond `--help`.
* **The three commands that spend the account**, and there are no others:
  `search <group>` (`contacts.search` first time, then `messages.search`),
  `history <group>` (`messages.getHistory`), and `discover --account-query`
  (one `contacts.search`). None of them resolves and none of them joins; all of
  them refuse before reading the credential unless `TELEGRAM_RESEARCH_ALLOW_LIVE`
  is set, and all are bounded by `--max-pages` and by the shared per-run ceiling.
* `account.AccountSession` — a Python API. Reaching MTProto means writing a
  script against it, having read this file. That is the whole gate, and it is a
  gate made of deliberateness rather than of code.

**Two switches — plus a third layer that lasts exactly as long as Telethon is
absent.** This skill never installs Telethon, and `TelethonTransport.connect()`
imports it at the moment of use, so on a machine that does not have it the live
path cannot reach Telegram at all: it stops at `TelethonMissing`, one line past
the switch check and with nothing on the wire. That layer belongs to the machine
rather than to the design, and it ends the moment somebody runs
`pip install telethon==1.44.0` — for this skill or for anything else sharing the
interpreter — after which the two switches below are all that stands between a
script and the account. `python scripts/account.py` says which side of that line
this machine is on, as `telethon_installed`.

The two switches, and neither implies the other:

1. `allow_live=True` passed to `AccountSession` **in code**, and
2. `TELEGRAM_RESEARCH_ALLOW_LIVE` set to `1`, `true`, `yes` or `on`
   (case-insensitive, stripped).

Anything else — unset, empty, `0`, `false`, `no`, `off`, `2`, `disabled` — is a
refusal, and the refusal message prints the offending value. Presence used to be
enough, which meant `TELEGRAM_RESEARCH_ALLOW_LIVE=0` turned live mode **on**.

**A refusal is not a dry run, and the CLI has no dry run at all.** `tg.py search`,
`tg.py history` and `tg.py discover --account-query` all reach the account through
`tg._open_account`, which raises `UsageError` when the variable is unset: the
command prints `{"ok": false, "error": ...}` and **exits 7**, having read no
credential and printed nothing in the shape of a plan. There is no `--dry-run`
flag anywhere in `tg.py`. The dry run exists only in the Python API, as
`AccountSession(dry_run=True)`, and it too never reads the credential file.

**A dry run does not meet every ceiling.** `resolve()` charges a simulated copy of
the ledger, `history()` reads `_history_stop_reason()` and `join_group()` calls
`check_join()` before answering, so a plan those three refuse is a plan the live
run would refuse too. `search_contacts()` and `search_messages()` return their
`would` block before any ceiling is consulted — so on an exhausted per-run ceiling
or a live freeze, a dry run of either still reports a call that would go through.

**Telethon is constructed with this skill's policy, not its own** (`TELETHON_POLICY`):
`flood_sleep_threshold=0` (it must never sleep a wait off inside the library),
`request_retries=1` (one wire call per call the ledger charges),
`connection_retries=1`, `auto_reconnect=False`, `receive_updates=False`. The
shipped defaults would have retried a resolve five times and swallowed every wait
under 60 seconds — five wire calls charged as one, and the freeze invisible.

**Five error classes carry a wait, not one.** `FloodWaitError`,
`FloodPremiumWaitError`, `SlowModeWaitError`, `FloodTestPhoneWaitError` and
`TakeoutInitDelayError` are siblings, not subclasses, and any of them freezes.
A wait with no readable number is treated as `UNKNOWN_FLOOD_WAIT_SEC = 300` —
a policy floor long enough to break a burst, explicitly **not** a measurement.

**Three calls share one accounting**, in `<state_dir>/account-history.json`:
`messages.getHistory`, `contacts.search` and `messages.search`. They are three
names for the same thing — a call this account makes that is not a resolve —
and giving each its own ceiling would be inventing three measurements where
nobody has taken one. What the file holds is a daily request count and a durable
freeze that outlives the process that earned it. No daily ceiling was invented for it,
because nothing has ever measured one. What bounds a run is
`budgets.max_history_requests_per_run` (**400**) — **its own knob, and an account
ceiling, so it may only fall**. It used to be borrowed from
`max_requests_per_run`, a free-surface knob an override file may legitimately
raise for a big crawl, so `{"budgets": {"max_requests_per_run": 100000}}` raised
the account's getHistory ceiling from 400 to 100 000 with `override_notes` empty
and nothing on stderr. The effective ceiling is the lower of the two and never
above what shipped, the count is per PROCESS rather than per `AccountSession`,
and **`0` means zero** — absent is the only way to say "no ceiling" and this path
never says it. Every page is paced and counted; a page that stopped early reports
`truncated`, so `while page.messages:` cannot read a flood as the end of history.

## The measured incident, in full

2026-08-20, on a real personal account. Sixteen `contacts.resolveUsername` calls in under
seven minutes bought **`A wait of 36468 seconds`** — 10 h 07 m.

- It was **not a ban**. Reading and searching kept working throughout; only resolution
  froze.
- **All sixteen calls returned success** (`-> True`) while writing empty records, so the
  tool that made them reported a good run on an account that was already dead. Nothing in
  that tool counted or paced `resolveUsername` specifically — it was throttled only by a
  flat 1.5 s per-process sleep, the same gap used for every other call.
- Measured on the tool that caused it, while it was running: the numbers, and the reading
  of them, are restated in the module docstring of `resolve.py`.

It is the only call this skill knows of that has ever cost real downtime, and every rule
below exists because of that one incident.

## The published ceiling, and its provenance

Telegram's own documentation page for `contacts.resolveUsername`
(`core.telegram.org/method/contacts.resolveUsername`) documents **no quota at all** — a
grep of its full text for `limit|quota|flood|daily|rate`, case-insensitive, returns zero
matches. Its only error rows are `USERNAME_INVALID` and `USERNAME_NOT_OCCUPIED`; there is
no `FLOOD_WAIT` row and no numeric budget anywhere on the page. Verified 2026-08-23.

The number "200" that circulates as this method's daily limit has exactly **one** published
source: a TDLib maintainer, on Telegram's own issue tracker, 2021-03-15:

> "The limit for username resolving is 200 usernames daily."

That is five years old as of this writing and has never been restated by Telegram or by
anyone else since. It is not on the method's own doc page.

**Why this skill uses 180, not 200, plus a burst rule:**

- 200 is an unrepeated, five-year-old, second-hand claim. There is no reason to plan the
  budget right up against its edge when the number itself was never confirmed a second
  time.
- The one wall this account has actually hit was **16 calls in under 7 minutes** — a burst,
  not a daily total. A daily ceiling alone would not have prevented the incident; a burst
  ceiling would have.
- `resolve.py` therefore enforces `DAILY_RESOLVE_CEILING = 180` **and**
  `BURST_CEILING = 8` calls inside `BURST_WINDOW_SEC = 600` (ten minutes) — half of the
  sixteen that froze the account — **and** `MIN_RESOLVE_GAP_SEC = 30.0` between any two
  resolves, whatever the totals say.

## `access_hash` is bound to the login/auth session

Quoted verbatim from `core.telegram.org/api/peers` (fetched 2026-08-23):

> "Access hashes may not be reused across different accounts or different login/auth
> sessions of the same account: however, they can be reused across different MTProto
> sessions linked to the same login/auth session. This is a core spam prevention feature of
> Telegram."

What follows from that sentence:

- A cached `access_hash` is worthless without knowing which login session produced it. The
  registry field is `peer.auth_session_fingerprint`, not just `peer.access_hash`
  (`registry.py` `Source.peer`, `resolve.py` `session_fingerprint` /
  `peer_is_usable`).
- The cache **dies whole** when the session changes — not gradually, not per-entry. A
  re-login (or a regenerated `TELEGRAM_SESSION` string) invalidates every cached peer at
  once, regardless of how recently each was written.
- `session_fingerprint()` is a truncated SHA-256 of the session string, kept specifically
  so the ledger can answer "is this the same login session" without ever holding the
  secret itself (`resolve.session_fingerprint`).
- `peer_is_usable()` refuses a cached peer unless `id`, `access_hash`, and a fingerprint
  that matches the *current* session are all present (`resolve.peer_is_usable`). A record
  with no fingerprint cannot say which login minted it — an older file, a hand-edited one, a
  hash carried in from another tool — so it is never trusted, and replacing it costs one
  `contacts.search`.

## The peer cache — `<state_dir>/peers.json`

Everything in the section above is why this file exists and why it is stamped.

`contacts.search` returns each chat **with its `access_hash`**, so one call both
finds a peer and hands over the key to read it. `PeerCache` writes those records
next to the ledger, each carrying `auth_session_fingerprint`, and
`resolve.peer_is_usable` is the only thing that reads that stamp: a record minted
under another login is never handed out. Within one login the hash does not
expire, so the second question about a group costs nothing at all — measured
2026-08-25: 2 calls for the first query on a group, **1** for the next.

**It fails open, deliberately, and that is the opposite of the ledger.** A ledger
that cannot be read refuses, because losing what it holds spends the account. A
peer cache that cannot be read answers `{}` and says why in `peer_cache_unreadable`,
because losing what IT holds costs one `contacts.search` — while handing out a
peer from a file we could not parse is the dangerous direction.

**One failure mode, verified live.** A hash Telegram no longer accepts answers
`ChannelInvalidError`, which the transport raises as `PeerUnusable` rather than as
a generic transport failure — named, because the repair is specific: drop the
record, look the name up again for one `contacts.search`, retry. `tg.py search`
does exactly that, **once** per command, and says so with `peer_refreshed: true`.
A second refusal after a fresh look-up means the peer is not readable from this
account, and asking a third time would spend the account on a settled question.
`PeerUnusable` deliberately does **not** latch the run: the latch exists for a
failure nobody understands, and latching here made the repair unreachable.

## Why an accountless resolve cannot exist

`inputPeerChannel` and `inputChannel` both require `access_hash:long` in their MTProto
constructor signature — there is no bare-id constructor for a channel or supergroup peer
(`core.telegram.org/api/peers`). `inputChannelFromMessage` looks like an
escape hatch but is not one: it needs a `peer` you already have access to and a `msg_id`
inside a chat you can already read — it cannot bootstrap a peer from nothing.

Bot-API `getChat` (row 8 of `surfaces.md`) does accept `@username` and returns a numeric
`id`, but that id is a dead end for MTProto: without an `access_hash` bound to the current
login session, `"if you have only a user/channel/supergroup ID without any kind of access
hash, you cannot interact with that peer"` (same page). No amount
of web scraping produces a usable hash, because a hash minted by any other account or
session is void by design — Telegram calls this "a core spam prevention feature", not an
oversight to route around.

## `channels.searchPosts` is closed — do not plan around it

`core.telegram.org/method/channels.searchPosts`, errors verbatim:

```
403 PREMIUM_ACCOUNT_REQUIRED   A premium account is required to execute this action.
```

Even with Premium, a full-text `query` search is not simply free: **"each user has a
limited amount of free full text search slots, after which payment is required"** —
`allow_paid_stars` lets the *caller* name a Stars amount, so the cost is not fixed by the
method and is structurally uncapped. The size of the free slot quota and the Stars price
per search are both NOT ESTABLISHED (Telegram's page never states a number; it links to a
"full flow" page that was not fetched). Hashtag search (`hashtag` param) carries no Stars
note in the parameter table, which suggests it may be free, but Telegram never says so
outright, and `PREMIUM_ACCOUNT_REQUIRED` gates the method as a whole regardless.

This method is also channels-only by its own description ("posts from public channels"),
so it would not reach any of the registry's groups even if Premium and Stars were both
available. **This skill must not plan any workflow around `channels.searchPosts`.**

## Pacing rules, and the constant in `resolve.py` that enforces each

1. **Every resolve is counted in a durable, cross-process ledger before it happens.**
   `ResolveLedger.check_resolve()` reads `resolves` from disk and raises `BudgetExhausted`
   before Telegram is ever asked, once `state.resolves >= DAILY_RESOLVE_CEILING` (180).
   `reserve_resolve()` / `settle_resolve()` write the increment and the gap latch to disk
   **before the call leaves**, so a process killed mid-call leaves the account correctly
   charged rather than free. Every mutation of the ledger is held under a cross-process
   mutex, and a write that cannot land raises instead of returning.
   *This is the code path, not a description of one.* An earlier shape of `resolve()`
   called plain `check_resolve()` and counted afterwards, with `reserve_resolve` reachable
   from the tests and from nothing else: five real calls, ledger total zero.
2. **No burst, regardless of the daily total.** `check_resolve()` also raises
   `BudgetExhausted` once `BURST_CEILING` (8) resolves have landed inside the trailing
   `BURST_WINDOW_SEC` (600 s) — this is the rule the 2026-08-20 incident is missing.
3. **A minimum gap between any two resolves.** `check_resolve()` raises `BudgetExhausted`
   if fewer than `MIN_RESOLVE_GAP_SEC` (30.0 s) have passed since `last_resolve_ts`, even
   when neither ceiling above is close.
4. **The first FloodWait freezes resolving for everything, and is never argued with.**
   `ResolveLedger.freeze(seconds, reason)` records `frozen_until` and every subsequent
   `check_resolve()` raises `ResolveFrozen` with the remaining time until that call. Nothing
   retries early — each retry during a freeze is understood to extend it, per the
   incident's own arithmetic. The freeze carries a **monotonic twin** as well as a wall
   clock deadline and is over only when both have passed, so a clock jumped a day forward
   cannot end it; and a write that started before the freeze can never shorten it, because
   every write re-reads the disk under the guard and floors the deadline at what is there.
   A freeze is nevertheless bounded, and there is now exactly one deliberate, logged way
   to lift one — see "Lifting a freeze" below.
5. **A resolve is counted whether it succeeds or fails.** `record_resolve()` increments on
   every attempt, because whether Telegram penalises a failed resolve differently from a
   successful one is unknown in either direction, and the safe reading of an unknown is
   that it costs the same. The one exception is a call refused **before it left the
   machine** — a transport that is not connected, a paid call blocked at the boundary —
   which is not counted, because it did not happen. The budget is taken after that check
   and before the wire, so there is no window where it could be both.
6. **A reservation is settled by name or not at all.** `reserve_resolve()`
   returns a token and `settle_resolve(token)` closes THAT reservation. It used
   to fall back to "the oldest reservation with no name on it", so a healthy run
   settled the reservation left by a run that had DIED mid-call — deleting the
   only evidence of the death and leaving its own on the books. Settling one
   nobody can find is not a no-op: it means the caller and the ledger disagree,
   and the safe reading is to leave the reservation standing (an unsettled one
   costs a line in `summary()`; a wrongly-cleared one costs the count).
   Unsettled reservations are pruned after `PENDING_TTL_SEC` (3600 s).
7. **Joins are a separate, explicit budget.** `check_join()` / `record_join()` enforce
   `DAILY_JOIN_CEILING` (3), independent of the resolve ledger — joining is never a side
   effect of a search.
8. **Never resolve a name that has not first been verified for free.** A single
   `t.me/<name>` landing GET (`surfaces.md` row 1) settles existence and type at zero
   account cost; `discover.py`'s `verify()` is that check and runs before any candidate can
   reach a resolve. A resolve spent on a name that turns out not to exist is spent at full
   price for nothing, out of the same budget the burst rule protects.

## Lifting a freeze — and the bound on one

**There are two freezes, and everything in this section applies to both.** The
resolve ledger freezes on a resolve FloodWait; `account-history.json` freezes on
a FloodWait from `getHistory`, `contacts.search` or `messages.search` — and since
no ordinary path resolves, the history one is the freeze a command line actually
earns. Both are bounded the same way, both are cleared by `tg.py budget
--unfreeze`, and each writes its own `.freezes.jsonl` beside itself. `tg.py
budget` prints both and reports `frozen: true` when either is on.

Two things were absolute about `frozen_until` and are not any more.

**A freeze is bounded at `MAX_FREEZE_SEC` = 2 days.** `freeze(seconds, reason)`
took any float, and `frozen_until` is `max()`-monotone, so a single bad number
from a clock — or a `nan`, which makes every comparison false and read as NOT
FROZEN — was permanent. Two days is above anything Telegram has been seen to ask
for and far below "for ever", so a longer value is a clock artefact and is clamped
to it with a note saying so. A value that is not a number at all does **not**
cancel the freeze: that is the fail-open direction. It freezes for
`UNREADABLE_FREEZE_SEC` (3600 s) and says why, where `tg.py budget` prints it.

**`tg.py budget --unfreeze --reason "..."` lifts one.** Before it,
`frozen_until` could only grow — `_write_locked` refuses every write that would
shorten it — so the only way back from a freeze written off a wrong clock was
deleting the ledger by hand, which throws away the day's counts, the burst list
and the pending reservations with it. `ResolveLedger.clear_freeze(reason)` lifts
the freeze **first** — one `_mutate(..., may_shorten=True)`, the only write in the
class allowed to shorten a deadline — and appends what it lifted to
`<ledger>.freezes.jsonl` afterwards.

**The lift is guaranteed; the audit line is not.** If that append cannot land —
the guard is busy, the file cannot be written — the freeze stays lifted and the
record comes back with `recorded: false`, which `tg.py budget --unfreeze` prints
in its `cleared` block. A missing audit line does not un-lift anything, so the
honest reading of `recorded: false` is: the account is unfrozen and the decision
to unfreeze it left no trace on disk. Read that field before treating an unfreeze
as audited.

**It is not a way to argue with Telegram**, and it cannot tell a real FloodWait
from a clock artefact — which is why it is logged at all, and why the reason
travels with it. Waiting is the only thing that ends a real one; each retry during
a freeze is understood to extend it, per the 2026-08-20 incident's own arithmetic.

## The single-writer lock

`AccountLock` (`resolve.py`) allows exactly one process to hold the account at a time,
enforced with an `O_EXCL`-created lock file, cross-process and cross-project. This exists
because:

- A per-process throttle is the normal shape (the tool that caused the incident slept a
  flat 1.5 s between requests, inside one process). Two callers against the same account
  halve the effective gap between requests without either one seeing the other — the exact
  defect that let 16 resolves land inside 7 minutes.
- **Assume there is a second writer.** Any other Telegram tool, script or client signed
  into the same account — yours or somebody else's on the same machine — is precisely the
  second caller that per-process throttling cannot see. `AccountLock` is what stands
  between the two of them, and it is worth having even if you believe there is no second
  tool today.
- **Keep the state directory on a local disk.** The lock rests on `O_EXCL`, which is not
  reliable on NFS or SMB, and the liveness half of the stale check only applies to a lock
  taken on this host. A state directory on a network share or a synced folder is a lock
  that can be held twice. `TELEGRAM_RESEARCH_STATE` is what moves it.
- A stale lock (a process that died mid-hold) is only broken after `stale_after` (default
  1800 s), and breaking it is recorded rather than silently overwritten — a lock is a
  safety device, not a queue to jump.

Four properties of the lock that are easy to assume and are not free — each one is the
answer to a way a simpler lock failed:

- **A lock file that cannot be parsed is treated as HELD, not stale**, and its age comes
  from the file's own mtime rather than from a missing `ts` that defaulted to 1970. Another
  tool writes a different schema; respecting it is the entire reason this lock exists.
- **`ts` is refreshed by activity** — every ledger write, every history page (before and
  after it), every resolve and every join — so a long run spending its budget at the
  30 s minimum gap is not declared stale a third of the way through its own work.
  "On every ledger write" was the old rule and it had a hole exactly where it mattered:
  a bulk history read writes no ledger entry at all, went stale at 1800 s, and a second
  process took its lock while it was still paging.
- **`release()` only removes a lock it owns** — pid *and* `since` must match what is on
  disk — so a process whose lock was broken cannot free the new holder's.
- **Breaking is serialised** under its own short-lived guard, so four processes racing one
  stale lock produce exactly one winner.

## Where the state lives

**The default is `~/.telegram-research`** — one directory under your home, outside every
project and outside this skill. The ledger, the lock, the registry, the pacer state,
`account-history.json` and `peers.json` are files inside it. `TELEGRAM_RESEARCH_STATE`
overrides it and names a **DIRECTORY**; pointing it at a file is a configuration error with
a sentence in it. The name is one constant in `config.py`, `STATE_DIR_NAME`.

Outside the skill's folder on purpose. Installing an agent skill copies a folder, and
updating one **replaces that folder wholesale** — `npx skills update telegram-research` does
exactly that. State kept inside it would be deleted by an update with no warning and no
undo: the resolve ledger with the freeze in it, the account lock, the source registry, the
peer cache. An update would look like a machine that had never been frozen, which is the
one thing the ledger exists to prevent.

Not the working directory either. A state directory that follows the shell means one `cd`
creates a second ledger with no freeze in it and a second lock file the first process
cannot see — and both appear silently, since nothing lists them.

**A RELATIVE value of `TELEGRAM_RESEARCH_STATE` is anchored on HOME, not on the shell.**
`expanduser().resolve()` alone turns a relative value absolute *against the current working
directory*, so `TELEGRAM_RESEARCH_STATE=state/_telegram` still named a different folder in
every shell. Measured: a run in one folder wrote a 36 468 s freeze, a run in another read
`frozen_for() == 0`, and the two took DIFFERENT `account.lock` files, so both processes held
"the" account lock at once. `config.anchored_state_path` is the anchor, and it is
deliberately a different one from `config.anchored_env_path`, which anchors
`TELEGRAM_RESEARCH_ENV` and `TELEGRAM_RESEARCH_CONFIG` on the project (a credential file in the
wrong place is loud — it is simply not there; a state directory in the wrong place is
silent). An absolute value is left where it points.

**If the machine cannot say where home is** — neither `HOME` nor `USERPROFILE` set —
nothing is guessed: the skill refuses and asks for `TELEGRAM_RESEARCH_STATE` as an absolute
path. A guess at that point (a temp folder, the skill's own folder, the working directory)
is the same defect wearing a third hat.

A ledger that cannot be read and understood **refuses** rather than reporting a clean slate:
`LedgerUnreadable` is a subclass of `BudgetExhausted`, so every caller that already refuses
on a budget refusal refuses here too. A missing ledger is still a clean slate — that is the
first run, not a damaged one.

## Credentials

`config.CREDENTIAL_NOTICE` is printed verbatim in every error this skill raises about a
missing or broken credential: it names the two places the credential may come from and says
that nothing else is read. This skill copies nothing — two copies of one secret drift apart
and survive every tidy-up, so there is exactly one copy and this skill is not the one
holding it.

**Two sources, tried in this order.**

1. The three variables `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION` in the
   process environment — **all three or none**. This is the recommended route: nothing on
   disk means nothing to commit, sync or copy by accident.
2. Failing that, the file named by **`TELEGRAM_RESEARCH_ENV`** (`config.ENV_CREDENTIAL`) — a
   file you already keep somewhere of your own choosing, outside any folder that is
   committed or cloud-synced.

A *partial* environment is ignored and **never merged with the file**: half a credential
from one place and half from another is exactly the configuration nobody can reason about.
The error says which of the three were set, so a typo in one name is visible rather than
silently demoting the run to the file.

Three behaviours in `config.read_credentials()` are deliberate and non-negotiable:

- **It never searches the disk.** No fallback path, no default location is guessed. A tool
  that goes looking for credentials will eventually find the wrong ones. Reading three
  variables the operator named is not searching, so this rule is untouched by the change.
- **It never asks the operator to create a file, and never creates one itself.** The fix
  for a missing credential is to set the three variables for your user account, or to point
  `TELEGRAM_RESEARCH_ENV` at a file you already have — kept outside any folder that is
  committed or cloud-synced.
- **It fails loudly and specifically** when neither source yields all three — when the
  environment holds none or only some of them and `TELEGRAM_RESEARCH_ENV` is unset, when the
  path it names does not exist, or when that file is missing `TELEGRAM_API_ID`,
  `TELEGRAM_API_HASH`, or `TELEGRAM_SESSION`. Every case raises `ConfigError` with the
  exact missing piece named, never a silent half-run.
