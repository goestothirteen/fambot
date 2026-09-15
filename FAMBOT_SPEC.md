# Fambot — v1 Specification

**Status:** approved for build · **Owner:** Mark · **Target host:** DigitalOcean droplet (188.166.186.215) · **Date:** 2026-09-15

This document is self-contained. It is the complete context needed to build, test, and deploy Fambot v1. No prior conversation is required.

> **Build note (2026-09-15):** the shipped implementation follows this spec with seven documented departures — see "Where this departs from the spec, and why" in `README.md`. The most significant are per-day buttons showing an aggregate tally rather than a per-user checkmark (a Telegram platform constraint), and roster rounds closing on a grace timer rather than instantly.

---

## 1. Background & purpose

Mark's family of five — Dad, Mom, Mark, his sister Dawn, and his brother Luke — are all busy and struggle to coordinate three recurring things:

1. **Family dinners.** They aim for ~3 dinners/month but nobody consistently organises them. Ad-hoc "I'm free" messages fizzle out or people back out.
2. **Dog walking.** Dad walks the family dog **Rush** daily. Occasionally he needs help — either someone to *accompany* him, or someone to *take over* the walk entirely. Taking over requires driving, and **Mark is the only other driver** in the family. Today Dad asks at the last minute in chat and often gets no reply.
3. **Home / dog coverage.** A helper is around Mon–Sat all day, so coverage is rarely needed — but Mom sometimes needs one of the three kids home (e.g. Sunday evenings after 6pm when she goes upstairs, or when the helper has an appointment). She needs an easy way to request cover, plus a fair rotating roster for the recurring Sunday-evening slot.

**Critical constraint:** Mom and Dad are non-technical. If the bot requires typing commands or navigating anything complicated, they will not use it. Every interaction must be **buttons only** — zero typing.

The family already uses Telegram and has an active family group chat. The bot lives in that group.

## 2. Product principles

1. **One primitive, three skins.** Dinner polls, help requests, and roster slots are all the same engine: *someone opens a request → the bot collects responses → an outcome is confirmed → the bot reminds → dropouts are handled.* Build the engine once.
2. **Bot surfaces, humans decide.** The bot never auto-picks a dinner date and never force-assigns a duty someone didn't opt into. It highlights viable options; a human taps to confirm.
3. **Zero typing.** Persistent reply keyboard + inline buttons for everything. Slash commands exist only as power-user aliases.
4. **Deterministic, no LLM.** No AI calls anywhere in v1. Button taps get instant, identical responses every time. (This is a deliberate architecture decision, not a limitation.)
5. **One live board per activity.** The bot edits a single message per poll/request rather than posting new messages, so the group chat stays clean.
6. **Public accountability.** Nags and claims happen in the group by @mention — social visibility is the driving force that makes this work.

## 3. Users & roles

| Person | Role flags | Notes |
|---|---|---|
| Dad | `parent` | Primary dog-walk requester |
| Mom | `parent` | Primary coverage requester |
| Mark | `kid`, `driver`, `admin` | Only non-Dad driver; bot admin |
| Dawn (sister) | `kid` | |
| Luke (brother) | `kid` | |

- All five vote in dinner polls; dinner requires **all 5** available.
- Help requests are answerable by the three `kid` members (parents can claim too if they want — don't block it, just target pings at kids).
- "Take over walk" requests are claimable **only by members with the `driver` flag** (v1: Mark). "Accompany" is claimable by any kid.
- Roster duty applies to the three `kid` members only.
- `admin` can cancel anything, edit member config, and run maintenance commands.

Member records (Telegram user IDs, display names, role flags) live in the database, seeded from a config file at first run. Use placeholder IDs in code; Mark fills real IDs at deploy (easiest path: a `/whoami` command that replies with the sender's user ID, so each family member can be registered by tapping once in DM or group).

## 4. Interface layout

### 4.1 Persistent reply keyboard (the "board")

Shown to everyone in the group, always available:

```
[ 🍜 Dinner ]  [ 🐕 Rush ]
[ 📅 Roster ]  [ ❓ Help ]
```

- **🍜 Dinner** → start (or view) the dinner poll
- **🐕 Rush** → dog-walk help flow (request or view open requests)
- **📅 Roster** → view roster / request coverage / set up roster
- **❓ Help** → one short message explaining the three buttons in plain language

If a flow is already active, tapping its button shows the live board (re-sends/bumps it) with context actions instead of starting a duplicate.

### 4.2 General button rules

- All flows are inline keyboards attached to bot messages.
- Every multi-step flow has a **⬅ Back** and **✖ Cancel** where sensible.
- Confirmations are explicit ("Request posted ✅") and short.
- Language: simple, warm English. Emoji as visual anchors (🍜🐕📅⭐🔒✅❌). No jargon.
- Date buttons show weekday + date: `Fri 19 Sep`. "Today" / "Tomorrow" labels where applicable.
- Timezone everywhere: **Asia/Singapore**. All stored timestamps UTC; all displayed times SGT.

## 5. Feature A — Dinner scheduler

### 5.1 Flow

1. **Initiate.** Anyone taps 🍜 Dinner → if no active poll, bot asks "Start a dinner vote for the next 7 days?" `[ ✅ Start ] [ ✖ Never mind ]`.
2. **Poll board.** Bot posts one message listing the next 7 dates as **multi-select toggle buttons**. Each member taps every day they can make dinner. Tapping again un-toggles. The board live-edits to show the tally:

```
🍜 FAMILY DINNER VOTE
Tap ALL days you can do dinner (evening).

Fri 19 Sep  ⭐ 5/5  ← everyone free!
Sat 20 Sep     3/5
Sun 21 Sep     4/5
Mon 22 Sep     1/5
...
Voted: Mark ✅ Mom ✅ Dad ✅ Dawn ✅ Luke ✅
```

   (Per-day toggle buttons carry a personal checkmark state per user; the tally line is aggregate. A "🙅 No days work for me" button lets someone explicitly vote "none" so they count as *voted*.)
3. **All-5 rule.** A day is only viable when **all 5** members marked it. Viable days get a ⭐ and a dedicated **🔒 Lock in <day>** button appears under the board (one per viable day). *The bot never auto-locks.*
4. **Lock.** Anyone taps 🔒 → bot confirms once (`Lock dinner for Fri 19 Sep? [ ✅ Yes ] [ ⬅ Back ]`) → on yes: poll closes, board replaced with a confirmation message, dinner event created.
5. **Nags.** Every **6 hours**, the bot @mentions members who haven't voted yet, in the group: "🍜 Waiting on @Dawn and @Luke to vote for dinner!" Respect **quiet hours 23:00–09:00 SGT** (skip, don't queue-burst). Stop nagging a member once they've voted (any day, or "no days").
6. **Deadline.** Default poll lifetime **48h**. At deadline with no lock:
   - If ⭐ days exist: bot posts "Time's up — these days work for everyone:" with the 🔒 buttons. No further nags.
   - If none: "😔 No day works for all 5. `[ 🔁 Extend 7 more days ] [ ✖ Drop it ]`". Extend re-opens with the following 7 dates, votes reset.
7. **Reminders (after lock).** At **T-3 days, T-1 day, and day-of 10:00 SGT**: "🍜 Family dinner Fri 19 Sep! Still good?" with `[ ✅ Still on ] [ ❌ Can't make it ]` per member. (Skip whichever T- reminders fall in the past if dinner was locked late.)
8. **Dropout.** Any ❌ → bot announces "@Luke can't make Fri anymore 😔" and asks the group: `[ 🔁 Revote ] [ ✖ Cancel dinner ]`. Revote starts a fresh 7-day poll from tomorrow.
9. **Done.** On dinner day at 22:00 SGT, mark the event completed. (Optional nicety: "Hope dinner was good! 🥢")

### 5.2 Rules & edge cases

- Only **one active poll and one upcoming locked dinner** at a time. Tapping 🍜 with a locked dinner shows its details + `[ ❌ Can't make it ] [ ✖ Cancel dinner (admin) ]`.
- The 7 candidate days start **tomorrow** (never today — dinner needs runway).
- Vote toggles must be idempotent and race-safe (two people tapping simultaneously → both recorded; board re-render debounced ~2s).
- If Telegram edit fails (message too old/deleted), re-post the board and store the new message_id.

## 6. Feature B — Help requests (dog walk & home coverage)

One engine, two entry points. A help request = *(requester, task type, date, time window, claim rules)*.

### 6.1 Dog walk (entry: 🐕 Rush)

1. Requester (usually Dad) taps 🐕 → `[ 🆘 I need help with Rush ] [ 👀 See open requests ]`
2. **Date:** `[ Today ] [ Tomorrow ] [ 📆 Pick a day ]` (pick-a-day shows next 14 days as buttons).
3. **Time:** coarse buttons — `[ 🌅 Morning ] [ ☀️ Afternoon ] [ 🌆 Evening ]` (display as Morning ≈ 7–11am, Afternoon ≈ 12–5pm, Evening ≈ 5–9pm; no minute-level input in v1 — keep it button-simple).
4. **Type:** `[ 👥 Accompany me ] [ 🚗 Take over the walk ]`
5. Bot posts to the group:

```
🐕 Dad needs help with Rush
📆 Thu 18 Sep, Evening
Type: Take over the walk (driver needed)

[ 🙋 I'll do it ]
```

   - *Accompany* → button visible/claimable by all kids.
   - *Take over* → tap by a non-driver gets a gentle toast ("This one needs a driver 🚗"); only `driver` members can claim.
6. **Claim.** First valid tap wins (atomic). Board edits to "✅ Mark's got it — thanks!" Requester also gets the confirmation in the same message. Late tappers get "Already claimed 😄".
7. **Re-pings if unclaimed:** at +3h, then every 6h (quiet hours respected), escalating copy ("Still nobody free to help Dad on Thu? 🥺"). Stop at the time window itself; mark expired-unclaimed and tell the requester.
8. **Reminders to claimant:** day before at 20:00 SGT + 1h before window start: "🐕 Reminder: you're helping Dad with Rush this evening!" `[ 👍 On it ] [ ❌ I can't anymore ]`. A ❌ un-claims, re-opens the request, and re-pings the group.
9. **Cancel:** requester (or admin) can cancel from the board at any time; claimant gets notified.

### 6.2 Home coverage (entry: 📅 Roster → Request cover)

Identical engine, different skin:

- Requester (usually Mom) picks date + time window the same way, plus a **task label** from buttons: `[ 🐕 Watch the dogs ] [ 🏠 Watch the house ] [ 🐕🏠 Both ]`.
- Claimable by all kids. Same claim / re-ping / reminder / unclaim behaviour.
- Board copy: "🏠 Mom needs someone home — Sun 21 Sep, Evening (watch the dogs)".

## 7. Feature C — Sunday roster (rolling)

Covers the recurring Sunday-evening slot (after 6pm) among the three kids. **Rolling, not calendar-month:** the bot keeps the next **5 Sundays** covered at all times.

### 7.1 Setup & top-up

- **Manual start:** 📅 Roster → `[ ⚙️ Set up roster ]` (admin/parents) → bot immediately runs an availability round for the next 5 Sundays. This is how the system starts *today* — no waiting for month boundaries.
- **Auto top-up:** a daily check — whenever **fewer than 3 future Sundays** are assigned, automatically run an availability round for the unassigned Sundays in the 5-week horizon.

### 7.2 Availability round

1. Bot posts a multi-select board to the group (same toggle mechanics as the dinner poll), targeted at the three kids:

```
📅 SUNDAY DUTY — who's free? (evenings after 6pm)
Sun 21 Sep · Sun 28 Sep · Sun 5 Oct · Sun 12 Oct · Sun 19 Oct
Tap all Sundays you CAN do.
```
2. Nag non-responders every **12h** (quiet hours respected), deadline **48h**.
3. **Assignment** at deadline (or when all 3 have answered): for each Sunday, among the kids who marked themselves free, pick the one with the **fewest total duties in the trailing 8 weeks** (duty_log); tie-break by longest-ago last duty; final tie-break random. One kid can hold multiple Sundays if others are unavailable.
4. Unfillable Sundays (nobody free) are flagged to the group + parents: "⚠️ Sun 5 Oct has no cover" with a `[ 🙋 I'll take it ]` button that stays open.
5. Bot posts the resulting roster and **pins it** (re-pin on every change; unpin stale roster messages).

### 7.3 Roster view & swaps

- 📅 Roster → `[ 👀 View roster ]` shows the next 2 weeks: all assigned Sundays + any open help requests, e.g. `Sun 21 Sep — Dawn (home duty) · Thu 18 Sep — Mark (Rush, take over)`.
- Assignee can tap `[ 🔁 Can't do my Sunday ]` on their slot → bot re-opens that date to the other two kids as a claimable request; if claimed, duty transfers (duty_log credits the replacement).
- Reminder to the assignee: Saturday 20:00 SGT + Sunday 12:00 SGT.

## 8. Notifications summary (all SGT, quiet hours 23:00–09:00)

| Trigger | Schedule | Channel |
|---|---|---|
| Dinner vote nag | every 6h until voted | group @mention |
| Dinner poll deadline | 48h after open | group |
| Dinner reminders | T-3d, T-1d, day-of 10:00 | group |
| Help request re-ping | +3h, then every 6h until claimed/expired | group @mention |
| Claimant reminder | day before 20:00, 1h before window | group (mention claimant) |
| Roster availability nag | every 12h until answered (48h max) | group @mention |
| Roster duty reminder | Sat 20:00, Sun 12:00 | group (mention assignee) |
| Roster top-up check | daily 10:00 | internal |

All scheduled jobs must survive restarts: derive due jobs from DB state on startup (don't persist an in-memory scheduler as source of truth). A minute-resolution tick loop that scans for due work is fine and simplest.

## 9. Technical design

### 9.1 Stack

- **Python 3.12**, **python-telegram-bot v21+** (async).
- **SQLite** (WAL mode), file at `/data/fambot.db` (volume-mounted).
- **APScheduler** (or a simple asyncio tick loop) for the schedule table scan — in-process, no Redis, no Postgres. *(Host RAM is tight: ~1.5 GB of 1.9 GB used. Target footprint < 100 MB.)*
- **Long polling**, not webhooks → **no inbound ports, no reverse-proxy/Caddy changes, no TLS** needed. Outbound HTTPS to api.telegram.org only.
- Single Docker container via docker-compose.

### 9.2 Telegram specifics

- Bot token from **@BotFather** (Mark creates; delivered via `.env`, never committed).
- **Privacy mode ON** — the bot must not read normal group messages. It only handles button callbacks, the reply-keyboard taps (which arrive as text messages matching the four fixed labels), and explicit `/` commands. (Reply-keyboard labels are exact-match strings; ignore all other text.)
- Register the bot in the family group; store `chat_id` in settings on first `/setup` (admin runs once in the group).
- `callback_data` budget is 64 bytes — use compact codes like `d:v:2026-09-19` (dinner vote toggle), `d:lock:2026-09-19`, `h:claim:<req_id>`, `r:av:<date>`. Version-prefix (`v1|`) optional but keep total short.
- Commands (power-user aliases only, never required): `/dinner`, `/rush`, `/roster`, `/whoami`, `/setup` (admin), `/cancel` (admin).
- Handle `message is not modified` edit errors silently; on message-too-old edit failures, re-post the board.

### 9.3 Data model (SQLite)

See `app/db.py` for the shipped schema, which follows this design with the
departures listed in `README.md` (slug-keyed members, `time_window`/`req_date`
column names, and the extra `roster_rounds` / `roster_responses` /
`dinner_dropouts` tables plus `jobs.payload`).

```sql
CREATE TABLE members (
  user_id      INTEGER PRIMARY KEY,     -- Telegram user id
  name         TEXT NOT NULL,           -- display name, e.g. 'Dad'
  is_parent    INTEGER NOT NULL DEFAULT 0,
  is_kid       INTEGER NOT NULL DEFAULT 0,
  is_driver    INTEGER NOT NULL DEFAULT 0,
  is_admin     INTEGER NOT NULL DEFAULT 0,
  active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE settings (              -- single-row key/value
  key TEXT PRIMARY KEY, value TEXT
);  -- keys: group_chat_id, quiet_start='23:00', quiet_end='09:00',
    -- dinner_nag_hours='6', poll_deadline_hours='48', roster_horizon='5', ...

CREATE TABLE dinner_polls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,              -- open | locked | expired | cancelled
  opened_by INTEGER, opened_at TEXT,
  deadline_at TEXT,
  message_id INTEGER,                -- live board message
  locked_date TEXT                   -- ISO date once locked
);

CREATE TABLE dinner_votes (
  poll_id INTEGER, user_id INTEGER, vote_date TEXT,  -- 'NONE' allowed
  PRIMARY KEY (poll_id, user_id, vote_date)
);

CREATE TABLE dinner_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  poll_id INTEGER, dinner_date TEXT, status TEXT,    -- upcoming | done | cancelled
  message_id INTEGER
);

CREATE TABLE help_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                -- dog_accompany | dog_takeover | coverage
  task_label TEXT,                   -- for coverage: dogs | house | both
  requested_by INTEGER NOT NULL,
  date TEXT NOT NULL, window TEXT NOT NULL,   -- morning | afternoon | evening
  status TEXT NOT NULL,              -- open | claimed | done | expired | cancelled
  claimed_by INTEGER, claimed_at TEXT,
  message_id INTEGER, created_at TEXT
);

CREATE TABLE roster_slots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  duty_date TEXT NOT NULL UNIQUE,    -- the Sunday
  assignee INTEGER,                  -- NULL = unfilled
  status TEXT NOT NULL,              -- collecting | assigned | open_swap | done | unfilled
  message_id INTEGER
);

CREATE TABLE roster_availability (
  duty_date TEXT, user_id INTEGER, available INTEGER,
  PRIMARY KEY (duty_date, user_id)
);

CREATE TABLE duty_log (              -- fairness source of truth
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  duty_date TEXT NOT NULL,
  kind TEXT NOT NULL                 -- roster | coverage | dog_takeover | dog_accompany
);

CREATE TABLE jobs (                  -- durable schedule
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  due_at TEXT NOT NULL,              -- UTC ISO
  kind TEXT NOT NULL,                -- nag_dinner | remind_dinner | reping_help | ...
  ref_id INTEGER,                    -- poll/request/slot id
  done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_jobs_due ON jobs (done, due_at);
```

Claims and locks use `UPDATE ... WHERE status='open'`-style guards (check rowcount) for atomicity.

### 9.4 Repo layout

```
fambot/
├── README.md              # quickstart + deploy steps (below)
├── FAMBOT_SPEC.md         # this file
├── docker-compose.yml
├── Dockerfile
├── .env.example           # BOT_TOKEN=..., TZ=Asia/Singapore
├── requirements.txt
└── app/
    ├── main.py            # bot init, handlers registration, tick loop
    ├── config.py          # env + settings access
    ├── db.py              # schema, migrations, helpers
    ├── boards.py          # message rendering (all user-facing text lives here)
    ├── dinner.py          # feature A
    ├── helpreq.py         # feature B
    ├── roster.py          # feature C
    └── scheduler.py       # jobs table scan + dispatch
```

Keep **all user-facing copy in `boards.py`** so tone/wording can be tweaked in one place.

### 9.5 Docker / deploy (droplet specifics)

- Deploy dir: **`/home/mark/fambot`** (sibling to `fpl-bot`, `splittowin`, etc.), owned by `mark`.
- **Make it a git repo from day one** and keep it clean — droplet repos must never hold uncommitted files (a 07:00 automation on the box runs `git pull` in repos and breaks on dirty trees). Push to Mark's remote; deploy = `git pull && docker compose up -d --build`.
- docker-compose:
  - service `fambot`, `restart: unless-stopped`
  - volume `./data:/data` for the SQLite file
  - `env_file: .env` (gitignored; `.env.example` committed)
  - **log rotation from day one:** `logging: driver: json-file, options: {max-size: "10m", max-file: "3"}` (existing containers on the box were flagged for missing this)
  - `TZ=Asia/Singapore`
- **Do not touch** `~/edge/` (shared Caddy) — Fambot has no inbound traffic.
- Memory: set a compose `mem_limit: 150m` as a guard.
- Backup: nightly `sqlite3 /data/fambot.db ".backup ..."` or file copy into `./backups/` (gitignored), 14-day retention — mirror the existing `fpl-backup.sh` pattern (runs from axolotl's crontab with docker group perms). Acceptable as a fast-follow after v1 works.

### 9.6 First-run / onboarding sequence

1. Mark creates the bot via @BotFather → puts token in `.env`.
2. Deploy container; bot starts in long polling.
3. Mark adds the bot to the family group.
4. Mark sends `/setup` in the group → bot stores `group_chat_id`, posts the persistent reply keyboard, and replies with a short registration prompt: each family member taps a `[ 🙋 That's me ]`-style button row (Dad / Mom / Mark / Dawn / Luke) once → their `user_id` is captured against the right member record. (`/whoami` exists as a fallback.)
5. Bot posts a 3-line plain-English intro of the three buttons.
6. Mark (or Mom/Dad) taps 📅 → ⚙️ Set up roster to start the first availability round immediately.

## 10. Acceptance checklist (v1 done means)

- [x] All five members registered via buttons; roles correct.
- [x] Dinner: full cycle works — initiate → 5 people multi-select vote → ⭐ appears only at 5/5 → 🔒 lock → T-reminders fire → ❌ dropout triggers revote/cancel → revote works.
- [x] Dinner: nags @mention only non-voters, every 6h, silent 23:00–09:00.
- [x] Dog walk: takeover claimable only by driver; accompany by any kid; first-tap-wins race-safe; unclaim re-opens.
- [x] Coverage request: full cycle incl. task label.
- [x] Roster: manual setup starts an availability round *today*; assignment respects availability + trailing-8-week fairness; roster pinned; swap flow works; auto top-up fires when <3 future Sundays assigned.
- [x] All schedules survive `docker compose restart` (jobs derived from DB).
- [x] Bot ignores all free text; only fixed labels + callbacks handled. *(Privacy mode: see README — Telegram cannot combine privacy mode ON with a working reply keyboard, so the guard is enforced in code.)*
- [x] Container ≤ 150 MB RAM, log rotation configured, repo clean & pushed.

## 11. Explicit non-goals for v1 (v2 parking lot)

- Any LLM/AI features — incl. natural-language capture ("anyone free to walk rush tmr?" → auto-draft a request).
- Axolotl integration: Mark's personal Hermes agent (same box) reading Fambot's SQLite/localhost endpoint so his 07:45 morning brief can include "you're on Sunday duty" / "2 people haven't voted". Zero coupling in v1; design DB so read-only access from another local process is trivial (it is, with SQLite WAL).
- Google Calendar sync for locked dinners.
- Minute-level time pickers, recurring non-Sunday rosters, stats dashboards.
- Webhooks/inbound HTTP of any kind.

## 12. Open items for Mark (not blockers to start coding)

1. Create bot with @BotFather → token into `.env`. Suggested handle: anything free, e.g. `@h5fambot`. Display name "Fambot" (rename anytime).
2. Confirm final button labels/copy tone (defaults above are fine to build with).
3. Real Telegram user IDs — captured at onboarding, nothing needed upfront.
