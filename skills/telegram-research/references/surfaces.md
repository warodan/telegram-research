# Telegram web surfaces — operating manual

Everything below was measured against live Telegram between 2026-08-23 and
2026-08-26, not inferred from documentation. The measurements were taken on a
corpus of **58 saved pages**. Two nested subsets of it survive, and neither is
the whole: **32 pages** were kept when the rest were dropped from the public copy
to protect the privacy of third parties, and they live with the pytest suite in
the project repository, at `tests/fixtures/probes/` — outside this skill folder,
because the installer copies the skill folder and nothing else. **What is
installed alongside these notes is the 10 pages `tg.py selftest` parses**, in the
same relative place, `tests/fixtures/probes/` under the skill. So a
`probes/<file>` cited below is very likely NOT in your copy — the measurement
stands, the file simply did not travel. Every count of the form "N of 58" or
"N of 122" is against the full corpus as measured, not against what you have.

Selectors and refusal signals are copied from the code (`scripts/tgweb.py`,
`scripts/tgparse.py`) and were re-checked against it key by key on **2026-08-24**
and again on **2026-09-05**; where an earlier measurement disagreed with the code,
the code wins and the disagreement is flagged at the end of the relevant table.

**Two rows changed meaning in that re-check and are called out in place**: the
service-message selector (it was a class every ordinary message carries) and the `?q=`
"no hits" test (its second clause cancelled the first out, so it never fired on a real
zero-hit page).

- [Surface map](#surface-map)
- [Refusal-signal contract](#refusal-signal-contract)
- [Selector table](#selector-table)
- [Parsing traps](#parsing-traps)
- [The `?q=` cap — measured 2026-08-25](#the-q-cap--measured-2026-08-25)
- [The IP question — stated honestly](#the-ip-question--stated-honestly)
- [Third-party surfaces — narrow roles only](#third-party-surfaces--narrow-roles-only)
- [What is NOT established](#what-is-not-established)
- [Cost of the group path](#cost-of-the-group-path-measured-2026-08-24-and-2026-08-25)
- [lyzem: three modes](#lyzem-three-modes-and-why-fmessages-alone-answers-the-wrong-question)
- [lyzem: the page-size parameter](#lyzem-the-page-size-parameter-and-why-no-yield-figure-survives)

### Where `tg.py` is

`tg.py` in this file is short for `python "$TG"`, where `$TG` is the path to the
skill's own `scripts/tg.py`. The block that resolves it lives at the top of
`SKILL.md` and is deliberately not repeated here; run it once per shell.

## Surface map

| # | surface | URL shape | needs account? | covers channels | covers groups | yields | cost |
|---|---|---|---|---|---|---|---|
| 1 | landing card | `t.me/<name>` | no | yes | yes | title, description, avatar, member/subscriber count, online count, type | 1 GET |
| 2 | channel preview | `t.me/s/<name>` | no | yes | **no (302)** | 20 messages/page, full text, views, reactions, media URLs, numeric chat id | 1 GET / 20 msgs |
| 3 | channel search | `t.me/s/<name>?q=<query>` | no | yes | no (302) | server-side full-text search of the whole channel history — **capped, see "The `?q=` cap" below** | 1 GET per page of hits (`--max-pages`, 5 by default) |
| 4 | channel paging | `t.me/s/<name>?before=<id>` | no | yes | no (302) | backward paging to the channel's first post, page size fixed at 20 | 1 GET / 20 msgs |
| 5 | single-message embed | `t.me/<name>/<id>?embed=1` | no | yes | **yes** | ONE message, for an id you already have: text, timestamp, sender display name, sender username (if public), reply block | 1 GET per id ASKED — and about 1 id in 100 answers |
| 6 | third-party RSS bridge | `tg.i-c-a.su/rss/<name>` | no (theirs) | yes | yes | latest 10 messages; no author field. **Nothing in the skill calls it any more** — it existed to hint a group's newest id, and guessing ids is gone | 1 GET |
| 7 | third-party message search | `lyzem.com/search?q=<term>&f=<mode>&per-page=50` | no | yes | patchy | keyword hits across a thin index. **Ask all three modes** (`groups`, `channels`, `messages`) — see below | 1 GET per mode |
| 8 | Bot API chat lookup | `api.telegram.org/bot<TOKEN>/getChat?chat_id=@<name>` | bot token | yes | yes | numeric chat id — **useless for MTProto reads**, no `access_hash` | 1 call |
| 9 | account search box | `contacts.search` via Telethon | yes | yes | yes | peers matching a TITLE or username, **each with its `access_hash`** — which is why the ordinary path needs no resolve | 1 account call, 0 resolves |
| 10 | account message search | `messages.search` inside one peer | yes | yes | **yes** | server-side full-text search of one group's whole history, with morphology | 1 account call per page of ≤100 hits |
| 11 | account history | `messages.getHistory` on one peer | yes | yes | **yes** | a group's messages newest-first, the only way to answer "what are they talking about now" | 1 account call per 100 messages |

Row 8 needs a bot token (a separate identity from your own user account) and was never
called during the probe; its "no membership requirement" claim is from the docs page text,
not a live test — see "What is NOT established".

**The rule to build on**: channels are free and complete with no account ever touched.
**Groups have no free search surface at all.** Row 5 reads one id you already have; it
cannot find which ids exist, and at the measured density that is not a shortfall but an
impossibility — see "Cost of the group path" below. A group is reached by rows 9-11,
and they are cheap: 1 call to find the peer, then 1 call per page of up to 100 messages,
**0 resolves**. Measured 2026-08-26: 100 recent messages of `hanoi_chats` for **one call**,
against the ~10 000 GETs row 5 would need to find 100 live ids.

## Refusal-signal contract

**Every refusal arrives as HTTP 200. The status code is never the signal.** The
classifiers in `tgweb.py` are the single place that decides what a body means; nothing
downstream should re-derive these from the status line.

| condition | literal signal | where it lives | HTTP status |
|---|---|---|---|
| username does not exist | `<title>Telegram: Contact @name</title>` | landing page `<title>` | 200 |
| username exists | `<title>Telegram: View @name</title>` | landing page `<title>` | 200 |
| it is a channel | `tgme_page_extra` contains `subscribers` | landing page | 200 |
| it is a group | `tgme_page_extra` contains `members` ... `online` | landing page | 200 |
| `/s/` not available (group, or nonexistent name) | `Location: https://t.me/<name>` — redirect target is the bare name, indistinguishable between the two cases | response headers, only visible with redirects OFF | **302** |
| `/s/` available (channel) | body contains `tgme_widget_message_wrap` | `/s/` page body | 200 |
| `?q=` search found nothing | the marker is an **element's class**, and the page carries no `data-post`. A page carrying `data-post` is never silence, whatever its text says | `/s/` page body, `NO_MESSAGES_FOUND` in `tgweb.py` | 200 |
| message missing (embed) | the message div carries the class `err_message` (7 of 7 error pages, and nothing else), **or** the page carries no `data-post` and the literal `Post not found`. A page carrying `data-post` is never missing | embed page body, `POST_NOT_FOUND` in `tgweb.py` | 200 |
| Telegram is rate-limiting | HTTP 429 | response status | 429 |
| Cloudflare / challenge page | **structure first, prose second.** A body carrying `tgme_page_wrap` or `data-post` is Telegram answering and is never an interstitial, whatever words are in it. Only a body carrying neither is matched against `CHALLENGE_MARKERS` — fourteen strings, markup (`cdn-cgi/challenge-platform`, `cf_chl_opt`, `__cf_chl`) ahead of prose, because prose is localised and a script path is not | `challenge_page` and `CHALLENGE_MARKERS` in `tgweb.py` | 403 or 503, and 200 |
| suspiciously small 200 | decoded body under `SUSPICIOUS_BODY_BYTES` = 500 bytes | any response | 200 |
| the surface broke, and this is not an empty result | 5xx after `MAX_RETRIES` = 3 paced attempts, or any 4xx that is not 429 | `FetchFailed` in `tgweb.py` | 5xx / 4xx |
| a truncated or undecompressable body | `IncompleteRead`, a half gzip stream, a deflate body that will not inflate | `TelegramWebError` naming the URL | any |

`.tme_no_messages_found` is the `?q=` search's own "no hits" marker — the CSS-class form of
`NO_MESSAGES_FOUND = "tme_no_messages_found"` in `tgweb.py` — read as a **class on an
element**, not as a substring of the body. As a substring it was a trap in the other
direction: one post whose own text quoted the phrase turned twenty real posts into proven
silence, which is the strongest claim this skill makes. **Telegram serves that notice
INSIDE a `tgme_widget_message_wrap`**, confirmed live on 2026-08-24 against
`t.me/s/durov?q=zzqwxnonexistentterm12345` (HTTP 200, 18 727 bytes; the page is saved in the project
repository at `tests/fixtures/pages/live-2026-08-24-s-durov-q-nohits.html`, not in this folder). The test
therefore used to be `marker in body AND wrap not in body`, which is self-cancelling: a
genuine zero-hit search reported `messages=0, exhausted=True, found_nothing=False` — byte
identical to an outage and to "I read the whole channel". One clause, and only one clause.

**The challenge test is structural for exactly the reason the two rows above it
are.** It used to be a whole-body substring test on user-controlled prose — the
same defect class `search_found_nothing` and `post_missing` were both rewritten to
eliminate. One post quoting a challenge page ("Just a moment, please enable
JavaScript" is what a channel about scraping, bots or Cloudflare writes routinely,
and what a `?q=cloudflare` search surfaces on purpose) made `fetch` raise
`RunAborted` on a page carrying twenty real posts, and `aborted_reason` is sticky,
so every later fetch in the process raised too. `challenge_page` therefore returns
False for any body holding `tgme_page_wrap` or `data-post` before it looks at a
marker at all. The `tgme_page_wrap` half matters as much as the `data-post` half:
a landing card carries no `data-post` either, so gating on messages alone would
still let a channel's own description abort a run. The one place prose is allowed
to speak alone is `stop_signal`'s 403/503 branch, where the status has already
said what the body is and the markers only choose the wording.

**`FetchFailed` and `RunAborted` are different facts.** `RunAborted` is Telegram saying stop
(429, 403/503, a challenge page, a tiny 200): it latches, and every later `fetch` on the same
client raises at once. `FetchFailed` is one request that broke: nothing latches, the next
request works, and the point of the class is that a 502 or a 404 must never reach a parser
and be read as "no such message" or "search finished". Neither subclasses the other.

The rate-limit rows (429, challenge page, tiny 200) are not refusals about one name — they
are `stop_signal()` conditions that abort the whole run. See "the four rules" below.

## Selector table

Checked key by key against `SEL` in `scripts/tgparse.py` on 2026-08-24, and again on
2026-09-05, when `grouped_wrap` and `video_thumb` turned out to be missing from it and
were added. Every key of `SEL` now has a row and every row is a key of `SEL` — with
**three deliberate exceptions, which say so in place**: `err_message`, `data-post` and
`code and quotes` are not `SEL` keys and live in `tgweb.py` and `tgdom.py`. **This table is
the file that rots when Telegram changes its front end, and a layout change should be a
diff to `tgparse.py`'s `SEL` dict and to nothing else** — this copy exists for reading, not
for editing; the code is the source of truth. (The table claimed to be verbatim before and
was not: its `service` row named a class that appears nowhere in the corpus.)

| key | selector | note |
|---|---|---|
| `msg_wrap` | `tgme_widget_message_wrap` | one per message |
| `msg` | `tgme_widget_message` | |
| `msg_text` | `js-message_text` | the post body. Shares the base class `tgme_widget_message_text` with the reply quote; only the `js-` twin is the post itself |
| `reply_text` | `js-message_reply_text` | the quoted text of the message being replied to |
| `owner_name` | `tgme_widget_message_owner_name` | the channel, not a person |
| `author_name` | `tgme_widget_message_author_name` | the sender, on group embeds |
| `from_author` | `tgme_widget_message_from_author` | channel post signature, when signatures are on |
| `views` | `tgme_widget_message_views` | rendered short: `12.5M`, `24M` |
| `reactions` | `tgme_widget_message_reactions` | wraps per-emoji reaction blocks |
| `reaction` | `tgme_reaction` | one reaction block |
| `reply` | `tgme_widget_message_reply` | the reply-to container |
| `err_message` | `err_message` | **not a `SEL` key: `ERR_MESSAGE` in `tgweb.py`.** The class on the message div of a "Post not found" page — 7 of 7 error pages, and nothing else. This, not a substring of the body, is the missing-message test |
| `data-post` | the attribute itself | **not a `SEL` key in this role: `DATA_POST` in `tgweb.py`.** The "is there a message here at all" test. A page that has it is neither missing nor silent, whatever its prose says. `attr_post` below is the same attribute read by the parser as an id |
| code and quotes | `pre`, `blockquote` | **not a `SEL` key: `BLOCK_TAGS` in `tgdom.py`.** Block-level for text extraction: without that, a quoted code block is glued to the words around it and the stored quote is not verbatim |
| `forwarded_from` | `tgme_widget_message_forwarded_from_name` | |
| `photo` | `tgme_widget_message_photo_wrap` | |
| `video` | `tgme_widget_message_video` | |
| `document` | `tgme_widget_message_document` | |
| `voice` | `tgme_widget_message_voice` | |
| `sticker` | `tgme_widget_message_sticker` | |
| `poll` | `tgme_widget_message_poll` | |
| `location` | `tgme_widget_message_location` | |
| `link_preview` | `tgme_widget_message_link_preview` | the thumbnail of a LINKED page — excluded from `media_urls` |
| `user_photo` | `tgme_widget_message_user_photo` | the sender's avatar — also excluded from `media_urls` |
| `grouped_wrap` | `js-message_grouped_wrap` | an album: ONE block carrying ONE `data-post`, with the other items' ids existing nowhere but the `?single` permalinks inside this wrapper. Live 2026-08-25 on `t.me/s/nexta_tv`: ids 27033-27052 under 18 `data-post` attributes, 27043 and 27044 in no other form. This is the key behind `Message.ids` |
| `video_thumb` | `tgme_widget_message_video_thumb` | a video's poster still — an `<i>` inside the player carrying the image as a CSS background. Not the file: no `token=`, and a JPEG. 38 occurrences in the 58 probes, every one a poster. This is the key behind `Message.media_posters` |
| `service` | `service_message` | `/s/` pages only. Occurs **once** in the whole 58-page corpus (Astana_motoriders/97, a pinned event) |
| `bubble` | `tgme_widget_message_bubble` | `?embed=1`: a `not_supported_wrap` that is a **direct child** of this is the embed's service-message marker |
| `not_supported_wrap` | `message_media_not_supported_wrap` | means three different things depending on its parent — see below |
| `not_supported_cont` | `media_not_supported_cont` | the "Please open Telegram to view this post" footer, on 66 of 122 messages. Means **nothing** |
| `video_player` | `tgme_widget_message_video_player` | a `not_supported_wrap` here means this browser cannot play the video: recorded as `unsupported:video` in `media`, never as a service message |
| `not_supported` | `message_media_not_supported_label` | the label text. Localisable, so structure is read instead |
| `page_title` | `tgme_page_title` | landing page fallback for `og:title` |
| `page_extra` | `tgme_page_extra` | `N subscribers` (channel) or `N members, M online` (group) |
| `page_desc` | `tgme_page_description` | landing page fallback for `og:description` |
| `page_photo` | `tgme_page_photo_image` | avatar |
| `more` | `tme_messages_more` | |
| `attr_post` | `data-post` (attribute) | `"<username>/<id>"`, the message id |
| `attr_view` | `data-view` (attribute) | base64url JSON carrying the numeric chat id, channel pages only |
| `attr_peer` | `data-peer` (attribute) | `"c<id>_<hash>"` — served on `?embed=1`, and the only id a GROUP has on any accountless surface. Kept verbatim in `Message.chat_peer`, never folded into `chat_id` |
| `attr_datetime` | `datetime` (attribute) | ISO 8601, UTC |

**A service message is a structure, not a class.** `is_service` is true for exactly **4 of
> **What the denominators here count, because they are not the same number.**
> The 58 probes of the full corpus are 51 HTML pages, 4 `.txt` and 3 `.xml`.
> Counted on 2026-08-25:
> **115** `data-post="` attributes and **122** `tgme_widget_message` occurrences —
> the second is larger because an embed error page carries the div and no
> `data-post`. So "of 122" below means *message divs seen*, not *messages that
> exist*, and a rate quoted against it is a rate over divs. The ratios in this
> file were re-checked and are reproducible; only the word "messages" next to the
> number is loose. Left as measured rather than renumbered — a different counting
> rule gives 116/123, and inventing a third number would be worse than naming the
> ambiguity.

the 122 messages** in the 58 probes, and the rule is: on `/s/` the message div carries
`service_message`; on `?embed=1` a `message_media_not_supported_wrap` stands where the body
would be, i.e. as a direct child of `tgme_widget_message_bubble`. Measured parents of that
wrap across the corpus: 66 under `media_not_supported_cont` (the generic footer), 38 under
`tgme_widget_message_video_player` (an unplayable video), 3 under the bubble (the real
thing). The selector this table used to name, `text_not_supported_wrap`, is a static styling
class on every ordinary message and flagged **122 of 122**.

**The closed `media` vocabulary**: `photo | video | document | voice | sticker | poll |
location | unsupported:video`. Anything else is a bug.

**An album is one block and several ids.** Telegram serves the other items' ids
only as `?single` permalinks inside the block, so a `/s/` page of 20 messages
carrying one 3-item album parsed as 18 records with nothing saying two ids had
gone — and a `?q=` hit whose caption lived on a swallowed id was permalinked to
the album's first id, a link that resolves, to the wrong message.
`Message.ids` now holds every id a block accounts for (`[id]` for an ordinary
post) and `PreviewPage.ids_seen` holds the page's distinct ids. **`is_full` is
decided on `max(ids_seen, blocks_seen)`, never on parsed messages** — an album is
one block with several ids, an unparsed block is a block all the same, and
`_cursors` and `no_message_carries_text` have to ask the same way. Anything
filling a `--count N`, reporting "N posts found", or concluding "the page was
short, so that was the end" reads ids.

**`Message.links`** keeps every anchor in the post text as `{"text", "href"}`, in
document order. Text extraction keeps an anchor's words and drops its
destination: on live `rian_ru`, 41 of 41 anchors had a destination unrecoverable
from the text, including the news story every post cites. `discover --found-via
link` is fed from this field; before it existed that discovery channel mined a
stream the links had already been deleted from. **`Message.media_posters`** names
the entries of `media_urls` that are a still standing in for another file —
`media_urls[0]` of a video post is a JPEG with no `token=`, on a record whose
`media` says `['video']`.

## Parsing traps

| trap | measurement | source |
|---|---|---|
| emoji are NOT `<img>` | `<tg-emoji emoji-id="..."><i class="emoji" style="background-image:url(...)"><b>CHAR</b></i></tg-emoji>`. The PNG is a CSS background on `<i>`, not an `<img>` tag. The character itself is a text node inside `<b>`. **This corrects an older claim** that emoji live in `<img>` tags pointing at `//telegram.org/img/emoji/40/*.png` — that claim does not match the code (`tgparse.py` module docstring, `tgweb.py` module docstring) or the probe fixtures. An extractor that drops the `<i class="emoji">` subtree deletes every emoji silently either way, but reading for an `<img>` finds nothing at all. | `tgparse.py` module docstring, verified against `A01-s-durov.html` |
| custom-emoji reactions carry no character at all | A reaction on a channel page is usually `<tg-emoji emoji-id="5465587407350942612"></tg-emoji>` followed by a count — empty of any character. The parser keys these as `custom:<emoji-id>`; inventing a `?` placeholder would assert a character the page never carried. Standard emoji reactions do carry their character and keep it as the key. | `tgparse._fill_from` (the `reactions` block), measured on `A01-s-durov.html`: keys `custom:<id>` with counts `55.2K`, `18.6K`, `13.4K`, `825`, `220` |
| rounded view strings | Views arrive as `12.5M`, a string, never an integer. `views_raw` keeps the measurement; `views` (via `parse_rounded_count`) is a guess derived from it and can never recover the exact figure. Always keep both. | `tgparse.parse_rounded_count`, `Message.views_raw` |
| `data-view` chat id, `-100` transform unverified | `data-view` is base64url JSON `{"c":...,"p":...,"t":...,"h":...}`, present only on channel `/s/` pages, identical across every message on the page. `c` = the chat's raw numeric id (e.g. `-1006503122` for @durov). **The relationship to the Bot-API `-100...` form (`-1001006503122`) is inferred, never verified against an authoritative id, and the code stores the raw value as measured.** Group embeds carry no `data-view` at all — groups have no id on this surface. | `tgparse._chat_id_from` / `tgparse.decode_data_view`; the `-100...` relationship is not established |
| weak author identity | Display name always present on group embeds and channel signatures; public username present only when the sender has one and links to `t.me/<user>`; **user id never available** on any accountless surface. The record shape (`Message.author_username`) must not require a user id. | `tgparse._fill_from`, `Message.author_name` / `Message.author_username` |
| id gaps are ordinary | On `hanoi_chats`, ids 29326/29327 were live while 29320, 10000, 50000, 200000 all answered `Post not found` the same day, and 124 consecutive empty ids sat between two live messages. So an empty id is never evidence that a group is quiet, and `group --id` reports the ones that answered nothing as `missing_ids` rather than dropping them. Whether gaps are deletions or unrenderable message types is NOT ESTABLISHED. | `tg.cmd_group` |
| the avatar is not the post's media | `media_urls` used to collect every `telesco.pe` URL under the message, so a one-line text post came back claiming an `.mp4` — the sender's animated profile photo. Three subtrees are excluded: `user_photo`, `link_preview` and the reply block. Measured after the fix: **0 of 122 messages claim `media_urls` without `media`.** | `tgparse._media_urls`, A09/95,97,103,106 |
| the paging cursor is not the page's own URL | `_cursors` reads `<link rel="prev">`, then the `tme_messages_more` anchor, and never `rel="canonical"` — which is the page it is already on. C15 went from `before=441` (itself, an infinite loop or a wasted request) to `before=62`. | `tgparse._cursors` |
| `bytes` is the DECODED size | It used to be the compressed length, so a 20 kB gzipped page read as "only 102 bytes" and tripped `stop_signal`'s tiny-body rule, aborting the run. The transfer size is kept separately as `wire_bytes`. The `bytes` in `fetchlog.jsonl` describes the file on disk, byte for byte — originals are written in binary, with no LF→CRLF rewriting. | `tgweb.Response` |
| not every 200 is a peer card | A body is only classified as a personal account if it carries `tgme_page_wrap` and does **not** carry `tgme_page_post` (a single-post page is a message, not a peer). Before that guard, an unrelated 200 — and one real GROUP page — were typed `user` and refused admission. **0 of 58 probes type as `user` now.** | `tgweb.is_peer_card`, `tgparse` peer typing |
| rounded counts never raise | `parse_rounded_count` does a strict full match, then an anchored `number + multiplier` prefix (`24M views` → 24 000 000), then digits-only, else `None`. It strips every Unicode space, U+202F included — Telegram uses it in the message meta block. | `tgparse.parse_rounded_count` |

## The `?q=` cap — measured 2026-08-25

**A `?q=` search cannot establish how much a channel said, and it used to claim
it had.** The surface fills its first page and then stops serving: no further
cursor, and `&before=` past the last hit returns nothing, so following it is not a
fix. Measured on a large news channel of 98 658 posts:

| query | `?q=` served | what a 3-page `history` walk of the last 60 posts found |
| --- | --- | --- |
| a word common in the channel's material | 20 on page 1, 1 on the paging hop — **21 for the whole channel** | the word in **32** of those 60 |

The same 20 + 2 ceiling came back for three more common words on the same
channel and for two unrelated queries on two other channels.

The distinguishing evidence is the **first page**: one that came back short is all
there was; one that came back full and then stopped is this surface's own ceiling.
`read.ReadResult._search_end` therefore refuses to mark a full-first-page stop
`exhausted`. It sets `surface_truncated: true`, `stop_reason: "surface_cap"` and a
`stopped_early` sentence; `tg.py search` raises that to `partial: true` and a
`warning` at the top of its output. Read as complete, 21 would have been reported
as everything a channel of 98 658 posts had said on the subject.

Whether `?q=` pages past 20 hits at all remains NOT ESTABLISHED — this
measurement says it stops, not why.

## The IP question — stated honestly

Nobody has measured where `t.me` breaks a single IP. The 2026-08-23 probe made **about 60
requests to `t.me`, each spaced >=2 seconds apart, and saw no throttling, no 429, no
challenge page.** That is the entire evidence base. It is not a rate limit measurement; it
is one run that happened not to trip anything.

Four rules follow from that absence of data, not from a known number:

1. **One request at a time, with a gap.** No parallel fetching anywhere. `Pacer` (in
   `tgweb.py`) enforces a jittered 2-4 s gap as a **floor**, cross-process, through a state
   file on disk — not merely inside one run. A config may widen that gap and may not
   narrow it; a refused value lands in `pacer.gap_floor_note` and in `override_notes`.
   Before that floor existed, a `TELEGRAM_RESEARCH_CONFIG` setting the gap to 0 removed all
   pacing in silence. It **reserves** its slot under an exclusive
   lock and writes the instant it intends to fire *before* sleeping, so two processes
   cannot compute the same due time and fire together, which is what reading the state
   used to do. A state file it cannot parse costs a full gap and a warning, never a silent
   0 s; a timestamp from an impossible future is repaired rather than obeyed; and if the
   lock cannot be taken it says so — `pacer.serialised_across_processes` is `False` and
   the request is still paced. An honest "I am not serialising" beats a silent one.
2. **Never refetch what you have.** `walk_channel`'s `until_id` and the registry's
   `max_id_seen` exist so a second run picks up where the first stopped rather than
   re-reading pages already paid for. This is the only real defence against an unmeasured
   per-IP ceiling: staying well under it by never asking twice.
3. **Search instead of paging, when a search will answer the question.** `?q=` on a
   channel answered a targeted question in one request that a full page-walk would have
   taken tens of requests to reach. Prefer it whenever the goal is
   "find X", not "have everything".
4. **React to a signal, not a counter.** `stop_signal()` in `tgweb.py` aborts the whole run
   on a 429, a challenge page, or a body under 500 bytes on a surface that should carry
   content. Nothing retries in the hope the surface changed its mind; the run stops and
   reports why.

## Third-party surfaces — narrow roles only

**No third-party service is ever proof of absence.** A service reporting zero or few
results means its own index is thin, never that Telegram has nothing.

| service | role | evidence it is narrow |
|---|---|---|
| `lyzem.com/search?q=<term>&f=messages&per-page=50` | source discovery and jargon/phrasing hints ONLY, at the discovery stage. Never a completeness check. | Self-reports **"51 results" for a single word as common as a large city's name**, measured live 2026-08-25 (the saved page is an authored stand-in, kept in the project repository at `tests/fixtures/probes/C20-lyzem-search.html`) — a term that common returning barely 51 hits says the index is far thinner than a full-text index of Telegram would need to be. Index size, retention, group coverage, rate limits and ToS are all NOT ESTABLISHED. |
| `tg.i-c-a.su/rss/<name>` | **no role. Nothing calls it.** It existed to hand back a group's newest message id, which was only ever useful for guessing which ids to try — and that whole path is gone. | It is a hosted, logged-in third-party MTProto account behind an HTTP facade, not a scrape of the public preview — and it **misdescribes its own source**: its RSS `<channel><link>` field claims `https://t.me/s/hanoi_chats`, a URL that 302-redirects for that group. If it lies about where its own data comes from, nothing about its completeness or its future availability can be assumed. |
| catalogues — tgstat, telemetr, telegramchannels.me, telegago, tlgrm.ru | **none.** Measured 2026-08-25: all of them are closed **at the search step** (registration or payment), telegago answers 403, tlgrm.ru does not come up. tgstat's per-channel profile page is still open and is enrichment, not discovery. | Do not spend requests re-establishing this. The living free surfaces for finding sources are web search on `site:t.me`, `t.me/s/<channel>?q=`, lyzem, and the account's own search box. |

## What is NOT established

- Whether `?q=` search pages past 20 hits with `?before=`, and whether matching is
  substring or token.
- Whether `Post not found` means deletion or an unrenderable message type.
- Whether `cdn4.telesco.pe` media tokens expire.
- Lyzem's index size, retention, group coverage, rate limits, and terms of use.
- Whether Bot API `getChat` actually succeeds for a public group the bot is not a member of
  — the docs state no membership requirement but this was never called live.
- Whether the `data-view` `c` field really is `-(bot_api_id + 100...)`-shaped, or some other
  transform.
- Whether groups beyond the ones measured serve `/s/` at all — 11 groups were tested,
  0 served, including Telegram's own official supergroup `tdlibchat`.
- Where `t.me` actually throttles a single IP — see "The IP question" above.
- **Where `contacts.search` and `messages.search` start being rate-limited.**
  Thirteen calls across one session on 2026-08-25 passed cleanly, with no wait of
  any kind, and that says nothing about a hundred. Both are paced and counted
  against the same per-run ceiling and the same durable freeze as
  `messages.getHistory`, and the gap between them is borrowed policy rather than
  a finding.
- Whether `contacts.search` ever fails to return a name that exists. One
  measurement, one group, matched exactly. If it misses, the resolve is the
  fallback — one call, 30 s gap, under the ledger.


## Cost of the group path, measured 2026-08-24 and 2026-08-25

`?embed=1` costs one request per **id**, not per message. On `hanoi_chats`, a
live public group of 2 835 members:

| ids probed, from the head down | messages found | hit rate |
| --- | --- | --- |
| 29153 .. 29327 (175 ids) | 3 (29327, 29326, 29201) | **1.7 %** |
| 29129 .. 29327 (199 ids), 2026-08-25 | 2 | **1.0 %** |

Between 29325 and 29202 lie **124 consecutive empty ids**, and 29201 answers.
Where the other ids went cannot be established: service events, deleted spam and
message types the widget refuses to render all return the identical `Post not
found` body, so nothing separates them.

### Why this surface was never a search, and why the walk is gone

The 2026-08-25 run above was asked for messages containing one ordinary word. It
spent **200 requests**, returned **2 messages**, and **0** of them contained the
word.
The arithmetic that follows is the whole argument:

| | | |
| --- | --- | --- |
| ids in this group | 29 327 | its newest id |
| ids that answer | ~1 % | measured twice |
| requests to surface 10 messages carrying one ordinary word | **≈199 000** | at the measured density |
| ...against ids that exist at all | 29 327 | **6.8× fewer than the requests needed** |

There is no request budget that makes it work, because the requests needed exceed
the ids that exist. So the head estimator, the catch-up creep, the blind scan and
the density-derived miss tolerance were **deleted** on 2026-08-25, together with
`--start-id`, `--count`, `--until-id`, `--since-last`, `--max-misses`,
`--no-catch-up`, `--catch-up-budget`, `--max-id-budget`, `--allow-blind-estimate`
and `--rss-hint`. What is left is `python "$TG" group <name> --id N`: one GET for one id
somebody already has, out of a permalink, a search hit or a citation.

### What replaced it

`messages.search` through the account, reached by `tg.py search <group>` — the
same command a channel takes, routed by the registry's `type`. Measured live
2026-08-25 on the same group:

| query | account calls | hits | span | resolves |
| --- | --- | --- | --- | --- |
| one common word | 2 (peer + query) | **44** | 2023-04 … 2026-03 | **0** |
| one rare compound word (peer already cached) | 1 | 12 | 2024-09 … 2026-01 | **0** |
| a second common word | 1 | 35 | — | **0** |

`server_total` comes back with the page, so what a complete answer costs is
arithmetic rather than a guess: `ceil(total / 100)` calls.

### The peer cache, and its one failure mode

`contacts.search` returns the peer **and its `access_hash`** in one response, so
the key that used to cost a `contacts.resolveUsername` now arrives as a
by-product. It is stored in `<state>/peers.json`, stamped with the login
session's fingerprint, and reused for ever within that login.

Its one failure mode is a hash Telegram no longer accepts. **Verified live
2026-08-25** by corrupting a stored hash: Telegram answers `ChannelInvalidError`,
which the transport raises as `PeerUnusable`, and the command repairs it inside
itself — drop the record, one `contacts.search`, retry — for a total of 3 calls
and the full 44 hits, with `peer_refreshed: true` in the output. Once per
command: a second refusal after a fresh look-up means the peer is not readable
from this account, and asking again would be spending the account on a question
already answered.

## lyzem: three modes, and why `f=messages` alone answers the wrong question

`lyzem_url` has had a `kind` parameter from the first day and no caller ever set
it, so every request this skill ever made was `f=messages`. Measured live
2026-08-25, the same three queries in each mode:

| query | `f=messages` | `f=groups` / `f=channels` |
| --- | --- | --- |
| three words, one of them a city name | nothing about that city at all | the city's own chat, first line, with its title |
| three words, one of them a country name | three large channels with nothing to do with the subject | two channels named for that country and that subject |
| three words on a technical subject | — | two channels on exactly that subject |

The group and channel modes also carry a **title and a description**, which the
message mode does not carry at all — in that mode even the result's own title
anchor is empty, so a name found there cannot be typed channel-or-group without
another request. `discover` now asks all three, one GET each, and reports what
each returned in `lyzem_kinds`.

Three more measured properties, none of them flattering: lyzem matches its words
by **OR, not as a phrase** (of 50 blocks for one of those three-word queries, zero
matched all three words), so its "10 374 results" counts a union and is not about
the query;
**9 of 30 names were dead**; and a rare word that genuinely does occur in a group
we had already read returned **0** — that group is absent from its index
entirely, while a neighbouring group on the same subject is in it. On a fast series it answers **HTTP 500
rather than 429**, and `tgweb` retries 5xx, so its throttling is invisible from
inside.

## lyzem: the page-size parameter, and why no yield figure survives

**The control is `per-page`, with a hyphen.** The skill sent `per_page` for its
whole life; lyzem ignores an unknown key and serves its default of 10. Measured
2026-08-25 against the live page:

| query | `per_page=50` | `per-page=50` |
| --- | --- | --- |
| one common word, `f=channels` | 10 blocks, 10 peers | 50 blocks, 50 peers |
| a three-word query, `f=messages` | 10 blocks, **4** peers | 50 blocks, **33** peers |

So the one discovery channel that searches message text across channels was
handing stage 2 a fraction of the candidates it was written to see, and stage 3
ten snippets to mine instead of fifty — with `dropped: {}` and no note, in a
module whose stated contract is that nothing is discarded silently.

**Every yield figure measured before 2026-08-25 is therefore wrong low, and none
has been remeasured.** Any number of the form "lyzem gave N usable channel names"
in a note or a report written before that date describes a request that asked for
ten results. Do not carry one forward; measure it again if it matters.

`discover.lyzem_page_param()` reads the control's name out of the page's own
`<select>` and compares it with what we send, so the next rename is a
`silent_cuts` entry on the first request after it happens instead of a fourfold
loss for however long nobody notices. `parse_lyzem` counts the other ways an
answer comes back short — a short page over an index claiming more, blocks
carrying no `t.me` link at all — into the same list.

The claimed count and the useful yield are unrelated in any case. Treat the
number on the page as a property of its index, never as a measure of what
Telegram holds, and never as evidence of absence — the standing rule for every
third-party surface here.
