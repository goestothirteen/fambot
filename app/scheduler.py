"""Durable scheduling.

Spec 8: scheduled work must survive restarts, so the `jobs` table is the
only source of truth. Nothing lives in memory. A tick loop scans for due
rows and dispatches them; if the container is down for six hours, the
overdue jobs simply run on the next tick.

Feature modules register handlers with @on("kind"), which keeps this module
free of imports from them and the dependency graph acyclic.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from telegram import Bot

from app import db
from app import timeutil as t

log = logging.getLogger(__name__)

Handler = Callable[[Bot, "object"], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}

# Jobs that ping the family. Quiet hours are applied when these are queued,
# but also here at dispatch: if the bot was offline overnight, a nag that
# came due at 02:00 must wait for 09:00 rather than fire the moment we boot.
QUIET_KINDS = {"dinner_nag", "dinner_remind", "help_reping", "help_remind",
               "roster_nag", "roster_remind", "roster_deadline", "roster_close"}


def on(kind: str):
    """Register the coroutine that runs a job of this kind."""
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return deco


async def run_due(bot: Bot, limit: int = 50) -> int:
    """Run everything that has come due. Returns how many jobs fired."""
    jobs = db.due_jobs(limit)
    fired = 0
    for job in jobs:
        if job["kind"] in QUIET_KINDS:
            qs, qe = db.quiet()
            now = t.now_utc()
            if t.in_quiet(now, qs, qe):
                db.x("UPDATE jobs SET due_at = ? WHERE id = ?",
                     (t.iso(t.shift_out_of_quiet(now, qs, qe)), job["id"]))
                continue
        handler = HANDLERS.get(job["kind"])
        if handler is None:
            log.warning("No handler for job kind %r (id=%s) - discarding",
                        job["kind"], job["id"])
            db.finish_job(job["id"])
            continue
        try:
            await handler(bot, job)
        except Exception:
            # One bad job must never stall the queue. Mark it done and move on;
            # the next scheduled instance of the same thing will still fire.
            log.exception("Job %s (%s) failed", job["id"], job["kind"])
        finally:
            db.finish_job(job["id"])
            fired += 1
    return fired


async def tick(context) -> None:
    """PTB JobQueue callback. Wired up in main.py."""
    try:
        n = await run_due(context.bot)
        if n:
            log.info("Ran %d due job(s)", n)
    except Exception:
        log.exception("Scheduler tick failed")
