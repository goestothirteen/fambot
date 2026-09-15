"""Environment configuration and the default settings table contents.

Anything a family might reasonably want to re-tune lives in DEFAULT_SETTINGS
(persisted in the DB, editable at runtime). Anything that is deployment
plumbing lives in an env var.
"""
from __future__ import annotations

import os

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
DB_PATH: str = os.getenv("DB_PATH", "/data/fambot.db")
TZ_NAME: str = os.getenv("TZ", "Asia/Singapore")
MEMBERS_FILE: str = os.getenv("MEMBERS_FILE", "/data/members.json")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
TICK_SECONDS: int = int(os.getenv("TICK_SECONDS", "30"))

# How long to coalesce rapid board edits before re-rendering, in seconds.
# Someone toggling five days in a row should cost one edit, not five.
RENDER_DEBOUNCE_SECONDS: float = float(os.getenv("RENDER_DEBOUNCE_SECONDS", "0.9"))

DEFAULT_SETTINGS: dict[str, str] = {
    "group_chat_id": "",
    "quiet_start": "23:00",
    "quiet_end": "09:00",
    # Dinner
    "dinner_window_days": "7",
    "dinner_nag_hours": "6",
    "poll_deadline_hours": "48",
    "reminder_hour": "10",          # T-3 / T-1 / day-of reminders, SGT
    "dinner_done_hour": "22",       # mark the event completed
    # Help requests
    "help_first_reping_hours": "3",
    "help_reping_hours": "6",
    "help_claimant_reminder_hour": "20",   # evening before
    # Roster
    "roster_horizon": "5",          # Sundays kept in view
    "roster_min_assigned": "3",     # below this, auto top-up fires
    "roster_nag_hours": "12",
    "roster_deadline_hours": "48",
    "roster_topup_hour": "10",
    # Grace period after the last kid answers, so someone mid-way through
    # ticking their Sundays is not cut off by their own first tap.
    "roster_close_grace_seconds": "120",
    "fairness_weeks": "8",          # trailing window for duty counting
    # Internal
    "schema_version": "1",
}

# Coarse time windows for help requests. (start_hour, end_hour, label, blurb)
WINDOWS: dict[str, tuple[int, int, str, str]] = {
    "morning": (7, 11, "Morning", "7-11am"),
    "afternoon": (12, 17, "Afternoon", "12-5pm"),
    "evening": (17, 21, "Evening", "5-9pm"),
}

# The Sunday roster slot is an evening duty: "after 6pm".
ROSTER_WINDOW_HOUR = 18


def missing_token() -> bool:
    return not BOT_TOKEN
