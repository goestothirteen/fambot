# Fambot

A buttons-only Telegram bot that keeps one family's dinners, dog walks and home
cover from falling through the cracks. Built for a Telegram group where two of
the five members will never type a command, so **every interaction is a button**.

Three features, one engine: someone opens a request → the bot collects
responses → a human confirms the outcome → the bot reminds → dropouts reopen it.

- **🍜 Dinner** — a 7-day multi-select vote. A day only becomes bookable when
  all five mark it free; a person then taps 🔒 to lock it. The bot never picks.
- **🐕 Rush** — dog-walk help. "Come along" is open to any kid; "take over the
  walk" needs a driver, because taking over means driving.
- **📅 Roster** — home cover on request, plus a rolling Sunday-evening roster
  shared among the three kids by a fairness rule.

No LLM anywhere. Every tap gets the same instant, deterministic answer.

---

## Quickstart (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add your BOT_TOKEN
DB_PATH=./data/fambot.db MEMBERS_FILE=./members.example.json python -m app.main
```

Run the tests — 112 of them, all offline, no network and no Telegram:

```bash
pip install pytest pytest-asyncio
python -m pytest -q
```

## Creating the bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name
   ("Fambot") and a free handle (e.g. `@h5fambot`). Copy the token into `.env`.
2. `/setprivacy` → see **Privacy mode** below before choosing.
3. Add the bot to the family group.
4. Make it a **group admin** — otherwise it cannot pin the roster. Everything
   else still works without admin; pinning just silently no-ops.
5. Send `/setup` in the group. The bot stores the chat id, puts up the
   persistent keyboard, and posts a registration card.
6. Everyone taps their own name once. That is the only setup anyone has to do.
7. Tap **📅 Roster → ⚙️ Set up the Sunday roster** to start the first
   availability round immediately.

`/whoami` is the fallback if registration ever needs doing by hand.

## Privacy mode — read this before `/setprivacy`

The spec asks for both "privacy mode ON" and "a persistent reply keyboard".
**Telegram does not allow both.** A reply-keyboard tap arrives as an ordinary
text message, and with privacy mode ON a bot never receives ordinary text
messages in a group — only commands, replies to itself, and callback taps. With
privacy ON the four bottom-of-screen buttons would do nothing.

So the default is **privacy mode OFF**, and the bot is made deaf in code
instead. `app/main.py:on_text` compares the message against exactly four fixed
label strings and returns immediately on anything else — nothing is parsed,
stored or logged. `tests/test_handlers.py` asserts this against real family
chatter, including near-misses like `dinner`, `Dinner` and `🍜 Dinner extra`.

If you would rather have privacy ON, you can: run `/setprivacy` → Enable, and
use the inline **home board** instead (`/start` posts one, and it carries the
same four buttons as callback buttons, which always reach the bot). The reply
keyboard then becomes decorative. Everything else is unaffected.

## Deploying to the droplet

Matches the conventions already on `188.166.186.215`: app source under
`/home/mark`, no `~/edge/` changes, log rotation and a memory cap from day one.

Long polling means **no inbound port, no TLS, no Caddy vhost**. Outbound HTTPS
to `api.telegram.org` is all it needs.

```bash
ssh mark@188.166.186.215
cd /home/mark
git clone <your-remote> fambot && cd fambot

mkdir -p data
cp .env.example .env && nano .env          # paste BOT_TOKEN
cp members.example.json data/members.json  # edit names/roles if you like

docker compose up -d --build
docker compose logs -f fambot
```

Redeploys, once the remote is set up:

```bash
cd /home/mark/fambot && git pull && docker compose up -d --build
```

**Keep this repo clean on the box.** The 07:00 automation runs `git pull` across
repos and breaks on a dirty tree — `.env`, `data/` and `backups/` are all
gitignored for exactly this reason.

### Nightly backup (fast-follow)

Mirrors the existing `fpl-backup.sh` pattern. From a crontab with docker group
permissions (axolotl's):

```
30 3 * * * /home/mark/fambot/scripts/backup.sh >> /home/mark/fambot/backups/backup.log 2>&1
```

Uses SQLite's `.backup`, which is safe against a live WAL database; a plain `cp`
is not. 14-day retention.

## Operating it

Runtime behaviour lives in a `settings` table, so it changes without a rebuild:

```bash
docker exec fambot python -c "
from app import db; db.init(); db.set_setting('dinner_nag_hours', 8)"
```

| Setting | Default | What it does |
|---|---|---|
| `quiet_start` / `quiet_end` | `23:00` / `09:00` | No pings inside this window |
| `dinner_nag_hours` | `6` | How often non-voters get chased |
| `poll_deadline_hours` | `48` | Dinner poll lifetime |
| `reminder_hour` | `10` | When T-3 / T-1 / day-of reminders go out |
| `help_first_reping_hours` | `3` | First nudge on an unclaimed request |
| `help_reping_hours` | `6` | Nudges after that |
| `roster_horizon` | `5` | Sundays kept in view |
| `roster_min_assigned` | `3` | Auto top-up fires below this |
| `roster_close_grace_seconds` | `120` | Wait after the last kid answers |
| `fairness_weeks` | `8` | Trailing window for counting duties |

Admin commands: `/setup`, `/cancel` (closes anything open). Power-user aliases,
never required: `/dinner`, `/rush`, `/roster`, `/whoami`.

## How it is put together

```
app/
├── main.py        bot init, handler registration, the tick loop
├── config.py      env vars + the defaults behind the settings table
├── db.py          schema, queries, members, jobs, the duty log
├── timeutil.py    UTC storage / SGT display, quiet hours, Sundays
├── tg.py          mentions, edit-or-repost, callback tokens, debouncing
├── boards.py      every user-facing string and keyboard
├── dinner.py      feature A
├── helpreq.py     feature B (both skins)
├── roster.py      feature C, incl. the fairness rule
└── scheduler.py   durable job dispatch
```

Three things worth knowing before you change anything:

**Scheduling is in the database, not in memory.** Every future action is a row
in `jobs (due_at, kind, ref_id, payload)`. A tick loop scans for due rows every
30s. Restart the container and nothing is lost; if it was down for six hours,
the overdue jobs simply run on the next tick. Quiet hours are enforced twice —
when a job is queued *and* when it is dispatched — so an overnight outage can't
dump a backlog of pings at 3am.

**Claims and locks are guarded UPDATEs.** `UPDATE ... WHERE status='open'` plus
a rowcount check is the lock. Two people tapping "I'll do it" in the same
instant produce one winner and one "already grabbed this 😄".

**All copy lives in `boards.py`.** Tone changes happen in one file.

## Where this departs from the spec, and why

1. **Buttons show the tally, not your own tick.** The spec asks for a personal
   checkmark per day. A group message has exactly one inline keyboard shared by
   everyone, so it cannot render differently per person. Instead the button
   shows `Sat 19 Sep · 3/5`, the message body names who picked each day, and
   the tapper gets a private toast (`✅ Sat 19 Sep`). This is more social
   pressure than a private tick would have been, which suits the goal.

2. **`members` is keyed by `slug`, not `user_id`.** Telegram ids are not known
   until people tap their names, and the spec's own §3 calls for seeding from a
   config file first. `user_id` is a nullable unique column.

3. **`window` → `time_window`, `date` → `req_date`.** `window` is a SQLite
   keyword; renaming avoids a class of parser surprise.

4. **Extra tables:** `roster_rounds` and `roster_responses` (an availability
   round spans several Sundays and needs its own identity), `dinner_dropouts`,
   and a `payload` column on `jobs` to distinguish T-3 from T-1.

5. **Roster rounds close on a grace timer.** The spec closes early "when all 3
   have answered". Implemented literally, the third kid's *first* tap ended the
   round while they were still ticking — an integration test caught Luke being
   assigned nothing despite marking all five Sundays free. A round now closes
   120s after the last tap, and any further tap pushes that back.

6. **At the deadline with viable days, the poll stays open.** Closing it would
   disable the very 🔒 buttons the deadline message offers. Nagging stops
   instead.

7. **Extra modules** `timeutil.py` and `tg.py` split out of what the spec drew
   as `db.py`/`boards.py`, to keep Telegram plumbing out of the copy file.

## Not in v1

No LLM or natural-language capture, no Axolotl integration, no Google Calendar
sync, no webhooks. The database is WAL-mode SQLite at a stable path, so a
future read-only reader on the same box (for the 07:45 morning brief) is a
`sqlite3.connect(..., uri=True)` away — no coupling needed now.
