"""The Instinct prediction engine.

Scores your Home Assistant history by time-of-day / day-type / recency to guess
the single action you most likely want right now, then either asks (actionable
notification) or — once a context is proven reliable — just does it. Every
confirm/reject is stored and feeds back into future scores.

No ML: transparent heuristic scoring plus a small SQLite feedback table.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    ACTIVE_STATES,
    CONF_AUTO_CONFIDENCE,
    CONF_AUTO_MIN_SAMPLES,
    CONF_DOMAINS,
    CONF_EXCLUDE_ENTITIES,
    CONF_HISTORY_DAYS,
    CONF_MIN_SCORE,
    CONF_TIME_WINDOW_MINUTES,
    DB_FILENAME,
    DEFAULT_AUTO_CONFIDENCE,
    DEFAULT_AUTO_MIN_SAMPLES,
    DEFAULT_DOMAINS,
    DEFAULT_EXCLUDE_ENTITIES,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_MIN_SCORE,
    DEFAULT_TIME_WINDOW_MINUTES,
    OFF_STATES,
    ON_STATES,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class Candidate:
    entity_id: str
    service: str  # "turn_on" | "turn_off"
    score: float = 0.0
    samples: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def key(self) -> str:
        return f"{self.entity_id}|{self.service}"


class InstinctEngine:
    """Owns scoring, the feedback DB, and the ask/execute loop."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.db_path = hass.config.path(DB_FILENAME)
        # tag -> Candidate awaiting a confirm/reject
        self._pending: dict[str, Candidate] = {}

    # ------------------------------------------------------------- config
    def _opt(self, key: str, default):
        # options override data override default
        if key in self.entry.options:
            return self.entry.options[key]
        if key in self.entry.data:
            return self.entry.data[key]
        return default

    @property
    def notify_service(self) -> str | None:
        from .const import CONF_NOTIFY_SERVICE

        return self._opt(CONF_NOTIFY_SERVICE, None)

    # ------------------------------------------------------------- lifecycle
    async def async_setup(self) -> None:
        await self.hass.async_add_executor_job(self._init_db)

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                ts          REAL,
                context_key TEXT,
                entity_id   TEXT,
                service     TEXT,
                outcome     TEXT
            )
            """
        )
        con.commit()
        con.close()

    # --------------------------------------------------------------- trigger
    async def async_handle_trigger(self) -> None:
        now = dt_util.now()
        _LOGGER.debug("Instinct trigger at %s (%s)", now.strftime("%H:%M"), now.strftime("%a"))

        candidates = await self._score_candidates(now)
        if not candidates:
            await self._notify_plain("Instinct", "Nothing obvious to do right now.")
            return

        best = candidates[0]
        ctx = self._context_key(now)
        conf, samples = await self._confidence(ctx, best)
        best.samples = samples

        _LOGGER.info(
            "Instinct best: %s %s score=%.2f conf=%.2f n=%d reasons=%s",
            best.service, best.entity_id, best.score, conf, samples, best.reasons,
        )

        auto_conf = float(self._opt(CONF_AUTO_CONFIDENCE, DEFAULT_AUTO_CONFIDENCE))
        auto_n = int(self._opt(CONF_AUTO_MIN_SAMPLES, DEFAULT_AUTO_MIN_SAMPLES))

        if conf >= auto_conf and samples >= auto_n:
            await self._execute(best)
            await self._record(ctx, best, "auto")
            await self._notify_plain("Instinct", f"Did it: {self._pretty(best)}.")
            return

        await self._ask(best, ctx)

    # --------------------------------------------------------------- scoring
    async def _score_candidates(self, now: datetime) -> list[Candidate]:
        history_days = int(self._opt(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS))
        window = int(self._opt(CONF_TIME_WINDOW_MINUTES, DEFAULT_TIME_WINDOW_MINUTES))
        min_score = float(self._opt(CONF_MIN_SCORE, DEFAULT_MIN_SCORE))

        start = now - timedelta(days=history_days)
        cur_dow = now.weekday()
        is_weekend = cur_dow >= 5

        entities = self._candidate_entities()
        transitions = await self._get_history(entities, start, now)

        scores: dict[str, Candidate] = {}
        for entity_id, changed_at, from_s, to_s in transitions:
            service = self._infer_service(from_s, to_s)
            if service is None:
                continue

            minutes_off = self._minutes_apart(changed_at, now)
            if minutes_off > window:
                continue
            time_w = 1.0 - (minutes_off / window)

            if changed_at.weekday() == cur_dow:
                day_w = 1.0
            elif (changed_at.weekday() >= 5) == is_weekend:
                day_w = 0.5
            else:
                day_w = 0.2

            age_days = (now - changed_at).total_seconds() / 86400.0
            recency_w = max(0.1, 1.0 - age_days / history_days)

            w = time_w * day_w * recency_w
            cand = scores.get(f"{entity_id}|{service}")
            if cand is None:
                cand = Candidate(entity_id, service)
                scores[cand.key] = cand
            cand.score += w
            cand.samples += 1

        results = []
        for cand in scores.values():
            await self._apply_feedback(cand, now)
            if cand.score < min_score:
                continue
            if not self._is_actionable(cand):
                continue
            cand.reasons.append(f"{cand.samples} similar events")
            results.append(cand)

        results.sort(key=lambda c: c.score, reverse=True)
        return results

    async def _apply_feedback(self, cand: Candidate, now: datetime) -> None:
        ctx = self._context_key(now)
        rows = await self.hass.async_add_executor_job(
            self._feedback_rows, ctx, cand.entity_id, cand.service
        )
        for (outcome,) in rows:
            if outcome in ("confirm", "auto", "override"):
                cand.score *= 1.25
            elif outcome == "reject":
                cand.score *= 0.5

    async def _confidence(self, ctx: str, cand: Candidate) -> tuple[float, int]:
        rows = await self.hass.async_add_executor_job(
            self._feedback_rows, ctx, cand.entity_id, cand.service
        )
        good = sum(1 for (o,) in rows if o in ("confirm", "auto", "override"))
        bad = sum(1 for (o,) in rows if o == "reject")
        n = good + bad
        conf = (good + 1) / (n + 2)  # Laplace-smoothed
        return conf, n

    # ------------------------------------------------------------- execution
    async def _execute(self, cand: Candidate) -> None:
        await self.hass.services.async_call(
            cand.domain, cand.service, {"entity_id": cand.entity_id}, blocking=False
        )
        _LOGGER.info("Instinct executed %s on %s", cand.service, cand.entity_id)

    # --------------------------------------------------------- notifications
    async def _ask(self, cand: Candidate, ctx: str) -> None:
        service = self.notify_service
        if not service:
            _LOGGER.warning("No notify_service configured; executing without confirm")
            await self._execute(cand)
            await self._record(ctx, cand, "auto")
            return

        tag = f"instinct_{int(time.time())}"
        self._pending[tag] = cand

        domain, name = service.split(".", 1) if "." in service else ("notify", service)
        await self.hass.services.async_call(
            domain,
            name,
            {
                "title": "Instinct",
                "message": f"{self._pretty(cand)}?",
                "data": {
                    "tag": tag,
                    "actions": [
                        {"action": f"INSTINCT_YES::{tag}", "title": "✅ Yes"},
                        {"action": f"INSTINCT_NO::{tag}", "title": "❌ No"},
                    ],
                },
            },
            blocking=False,
        )
        _LOGGER.info("Instinct asked: %s (tag=%s)", self._pretty(cand), tag)

        # Expire the pending suggestion after 2 minutes.
        async def _expire(_now):
            self._pending.pop(tag, None)

        from homeassistant.helpers.event import async_call_later

        async_call_later(self.hass, 120, _expire)

    @callback
    def handle_mobile_action(self, event) -> None:
        action = event.data.get("actionName") or event.data.get("action", "")
        if "::" not in action:
            return
        verb, tag = action.split("::", 1)
        cand = self._pending.pop(tag, None)
        if cand is None:
            return
        self.hass.async_create_task(self._resolve(verb, cand))

    async def _resolve(self, verb: str, cand: Candidate) -> None:
        ctx = self._context_key(dt_util.now())
        if verb == "INSTINCT_YES":
            await self._execute(cand)
            await self._record(ctx, cand, "confirm")
            await self._notify_plain("Instinct", f"Done: {self._pretty(cand)}.")
        elif verb == "INSTINCT_NO":
            await self._record(ctx, cand, "reject")
            _LOGGER.info("Instinct rejected: %s", self._pretty(cand))

    async def _notify_plain(self, title: str, message: str) -> None:
        service = self.notify_service
        if not service:
            _LOGGER.info("[instinct] %s: %s", title, message)
            return
        domain, name = service.split(".", 1) if "." in service else ("notify", service)
        await self.hass.services.async_call(
            domain, name, {"title": title, "message": message}, blocking=False
        )

    # --------------------------------------------------------- history/DB io
    async def _get_history(self, entities, start, end):
        """Return [(entity_id, local_dt, from_state, to_state), ...]."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import (
            state_changes_during_period,
        )

        def _query():
            return state_changes_during_period(
                self.hass,
                start,
                end,
                entity_id=None,
            )

        # state_changes_during_period with entity_id=None returns all recorded
        # entities; filter to our candidate set.
        data = await get_instance(self.hass).async_add_executor_job(_query)
        wanted = set(entities)
        out = []
        for entity_id, states in (data or {}).items():
            if entity_id not in wanted:
                continue
            prev = None
            for st in states:
                state = st.state
                lc = st.last_changed
                if lc is None:
                    continue
                local = dt_util.as_local(lc).replace(tzinfo=None)
                if prev is not None and state != prev:
                    out.append((entity_id, local, prev, state))
                prev = state
        return out

    def _feedback_rows(self, ctx: str, entity_id: str, service: str):
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT outcome FROM feedback WHERE context_key=? AND entity_id=? "
            "AND service=?",
            (ctx, entity_id, service),
        ).fetchall()
        con.close()
        return rows

    async def _record(self, ctx: str, cand: Candidate, outcome: str) -> None:
        def _insert():
            con = sqlite3.connect(self.db_path)
            con.execute(
                "INSERT INTO feedback VALUES (?,?,?,?,?)",
                (time.time(), ctx, cand.entity_id, cand.service, outcome),
            )
            con.commit()
            con.close()

        await self.hass.async_add_executor_job(_insert)

    # --------------------------------------------------------------- helpers
    def _context_key(self, now: datetime) -> str:
        part = "we" if now.weekday() >= 5 else "wd"
        return f"{part}:{now.hour:02d}"

    def _candidate_entities(self) -> list[str]:
        domains = self._opt(CONF_DOMAINS, DEFAULT_DOMAINS)
        excludes = self._opt(CONF_EXCLUDE_ENTITIES, DEFAULT_EXCLUDE_ENTITIES)
        out = []
        for state in self.hass.states.async_all():
            eid = state.entity_id
            if eid.split(".", 1)[0] not in domains:
                continue
            if self._excluded(eid, excludes):
                continue
            out.append(eid)
        return out

    @staticmethod
    def _excluded(entity_id: str, excludes) -> bool:
        for ex in excludes:
            if entity_id == ex or entity_id.startswith(ex.rstrip("*")):
                return True
        return False

    @staticmethod
    def _infer_service(from_s: str, to_s: str) -> str | None:
        was_on = from_s in ON_STATES
        now_on = to_s in ON_STATES
        if to_s in OFF_STATES or (was_on and not now_on):
            return "turn_off"
        if to_s in ON_STATES or (not was_on and now_on):
            return "turn_on"
        return None

    def _is_actionable(self, cand: Candidate) -> bool:
        state = self.hass.states.get(cand.entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return False
        currently_on = state.state in ACTIVE_STATES
        if cand.service == "turn_off" and not currently_on:
            return False
        if cand.service == "turn_on" and currently_on:
            return False
        return True

    @staticmethod
    def _minutes_apart(a: datetime, now: datetime) -> float:
        am = a.hour * 60 + a.minute
        nm = now.hour * 60 + now.minute
        d = abs(am - nm)
        return min(d, 1440 - d)

    def _pretty(self, cand: Candidate) -> str:
        state = self.hass.states.get(cand.entity_id)
        name = (
            state.attributes.get("friendly_name", cand.entity_id)
            if state
            else cand.entity_id
        )
        verb = "Turn off" if cand.service == "turn_off" else "Turn on"
        return f"{verb} {name}"
