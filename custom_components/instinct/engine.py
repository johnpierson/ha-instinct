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
    CONF_EXCLUDE_REACTIVE,
    CONF_HISTORY_DAYS,
    CONF_MANUAL_ONLY,
    CONF_MAX_ACTIONS,
    CONF_MIN_SCORE,
    CONF_MULTI_ACTION,
    CONF_TIME_WINDOW_MINUTES,
    DB_FILENAME,
    DEFAULT_AUTO_CONFIDENCE,
    DEFAULT_AUTO_MIN_SAMPLES,
    DEFAULT_DOMAINS,
    DEFAULT_EXCLUDE_ENTITIES,
    DEFAULT_EXCLUDE_REACTIVE,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_MANUAL_ONLY,
    DEFAULT_MAX_ACTIONS,
    DEFAULT_MIN_SCORE,
    DEFAULT_MULTI_ACTION,
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
        # tag -> list[Candidate] awaiting a confirm/reject
        self._pending: dict[str, list[Candidate]] = {}
        # active diagnostic listener unsubscribe, if any
        self._debug_unsub = None
        # always-on observation capture unsubscribe
        self._obs_unsub = None

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
        self._start_observing()

    def _start_observing(self) -> None:
        """Always-on: log every candidate state change with its live context."""
        from homeassistant.const import EVENT_STATE_CHANGED

        @callback
        def _on_change(event) -> None:
            eid = event.data.get("entity_id", "")
            domains = self._opt(CONF_DOMAINS, DEFAULT_DOMAINS)
            excludes = self._opt(CONF_EXCLUDE_ENTITIES, DEFAULT_EXCLUDE_ENTITIES)
            if eid.split(".", 1)[0] not in domains or self._excluded(eid, excludes):
                return
            new = event.data.get("new_state")
            old = event.data.get("old_state")
            if new is None:
                return
            service = self._infer_service(old.state if old else "", new.state)
            if service is None:
                return
            ctx = getattr(new, "context", None)
            has_user = 1 if getattr(ctx, "user_id", None) else 0
            has_parent = 1 if getattr(ctx, "parent_id", None) else 0
            ts = new.last_changed.timestamp()
            self.hass.async_add_executor_job(
                self._insert_obs, ts, eid, service, has_user, has_parent
            )

        self._obs_unsub = self.hass.bus.async_listen(EVENT_STATE_CHANGED, _on_change)

    def _insert_obs(self, ts, eid, service, has_user, has_parent) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?)",
            (ts, eid, service, has_user, has_parent),
        )
        con.commit()
        con.close()

    def async_unload(self) -> None:
        """Stop all listeners on entry unload."""
        self.async_stop_debug()
        if self._obs_unsub is not None:
            self._obs_unsub()
            self._obs_unsub = None

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
        # Live-captured observations. Context (user_id/parent_id) is reliable
        # here (unlike recorder history), so source filters read from this.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS observations (
                ts          REAL,
                entity_id   TEXT,
                service     TEXT,
                has_user    INTEGER,
                has_parent  INTEGER
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS ix_obs_ts ON observations (ts)"
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

        ctx = self._context_key(now)

        # Multi-action: confirm & run the top-N context actions as one batch.
        if self._opt(CONF_MULTI_ACTION, DEFAULT_MULTI_ACTION):
            max_n = int(self._opt(CONF_MAX_ACTIONS, DEFAULT_MAX_ACTIONS))
            batch = candidates[: max(1, max_n)]
            _LOGGER.info(
                "Instinct multi-action batch (%d): %s",
                len(batch), [c.key for c in batch],
            )
            await self._ask(batch, ctx)
            return

        # Single-action (default).
        best = candidates[0]
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

        await self._ask([best], ctx)

    # ------------------------------------------------------------- diagnostic
    async def async_start_debug(self, seconds: int = 120) -> None:
        """Live-log the context of candidate state changes for `seconds`.

        Toggle something in Apple Home / the HA app / a physical switch while
        this is on, then read the log lines tagged `INSTINCT-DEBUG` to see how
        each source is attributed (user_id / parent_id / origin).
        """
        from homeassistant.const import EVENT_STATE_CHANGED
        from homeassistant.helpers.event import async_call_later

        # Restart cleanly if already capturing.
        self.async_stop_debug()

        domains = self._opt(CONF_DOMAINS, DEFAULT_DOMAINS)
        excludes = self._opt(CONF_EXCLUDE_ENTITIES, DEFAULT_EXCLUDE_ENTITIES)

        @callback
        def _on_change(event) -> None:
            eid = event.data.get("entity_id", "")
            if eid.split(".", 1)[0] not in domains or self._excluded(eid, excludes):
                return
            new = event.data.get("new_state")
            old = event.data.get("old_state")
            if new is None:
                return
            new_s = new.state
            old_s = old.state if old else None
            if new_s == old_s:
                return
            ctx = getattr(new, "context", None)
            _LOGGER.warning(
                "INSTINCT-DEBUG %s: %s -> %s | user_id=%s parent_id=%s "
                "ctx_id=%s origin=%s",
                eid, old_s, new_s,
                getattr(ctx, "user_id", None),
                getattr(ctx, "parent_id", None),
                getattr(ctx, "id", None),
                getattr(event, "origin", None),
            )

        self._debug_unsub = self.hass.bus.async_listen(EVENT_STATE_CHANGED, _on_change)
        _LOGGER.warning(
            "INSTINCT-DEBUG capture ON for %ds — trigger something in Apple Home "
            "now, then check the logs for INSTINCT-DEBUG lines.", seconds,
        )

        async def _stop(_now) -> None:
            self.async_stop_debug()

        async_call_later(self.hass, seconds, _stop)

    @callback
    def async_stop_debug(self) -> None:
        if self._debug_unsub is not None:
            self._debug_unsub()
            self._debug_unsub = None
            _LOGGER.warning("INSTINCT-DEBUG capture OFF.")

    # --------------------------------------------------------------- scoring
    async def _score_candidates(self, now: datetime) -> list[Candidate]:
        history_days = int(self._opt(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS))
        window = int(self._opt(CONF_TIME_WINDOW_MINUTES, DEFAULT_TIME_WINDOW_MINUTES))
        min_score = float(self._opt(CONF_MIN_SCORE, DEFAULT_MIN_SCORE))

        start = now - timedelta(days=history_days)
        cur_dow = now.weekday()
        is_weekend = cur_dow >= 5

        manual_only = bool(self._opt(CONF_MANUAL_ONLY, DEFAULT_MANUAL_ONLY))
        exclude_reactive = bool(
            self._opt(CONF_EXCLUDE_REACTIVE, DEFAULT_EXCLUDE_REACTIVE)
        )

        # Source filters need reliable context -> use the live observations log.
        # Without a filter, use recorder history for full day-one coverage.
        if manual_only or exclude_reactive:
            events = await self._get_observations(
                start, manual_only, exclude_reactive
            )
        else:
            entities = self._candidate_entities()
            events = await self._get_history(entities, start, now)

        scores: dict[str, Candidate] = {}
        for entity_id, changed_at, service in events:
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
    async def _ask(self, cands: list[Candidate], ctx: str) -> None:
        """Ask to confirm a batch (single-action passes a list of one)."""
        service = self.notify_service
        if not service:
            _LOGGER.warning("No notify_service configured; executing without confirm")
            for cand in cands:
                await self._execute(cand)
                await self._record(ctx, cand, "auto")
            return

        tag = f"instinct_{int(time.time())}"
        self._pending[tag] = cands

        message = self._pretty_batch(cands)
        domain, name = service.split(".", 1) if "." in service else ("notify", service)
        await self.hass.services.async_call(
            domain,
            name,
            {
                "title": "Instinct",
                "message": message,
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
        _LOGGER.info("Instinct asked: %s (tag=%s)", message, tag)

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
        cands = self._pending.pop(tag, None)
        if cands is None:
            return
        self.hass.async_create_task(self._resolve(verb, cands))

    async def _resolve(self, verb: str, cands: list[Candidate]) -> None:
        ctx = self._context_key(dt_util.now())
        if verb == "INSTINCT_YES":
            for cand in cands:
                await self._execute(cand)
                await self._record(ctx, cand, "confirm")
            done = "; ".join(self._pretty(c) for c in cands)
            await self._notify_plain("Instinct", f"Done: {done}.")
        elif verb == "INSTINCT_NO":
            for cand in cands:
                await self._record(ctx, cand, "reject")
            _LOGGER.info("Instinct rejected: %s", self._pretty_batch(cands))

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
        """Recorder-history events as [(entity_id, local_dt, service), ...].

        Unfiltered — recorder history can't reliably surface context, so source
        filters use _get_observations instead.
        """
        if not entities:
            return []

        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import (
            get_significant_states,
        )

        def _query():
            # get_significant_states takes a list of entity_ids in one query
            # and keeps real state-value changes (drops attribute-only churn).
            return get_significant_states(
                self.hass,
                start,
                end,
                entity_ids=list(entities),
                include_start_time_state=False,
                significant_changes_only=True,
                minimal_response=False,
                no_attributes=True,
            )

        data = await get_instance(self.hass).async_add_executor_job(_query)
        out = []
        for entity_id, states in (data or {}).items():
            prev = None
            for st in states:
                state = getattr(st, "state", None)
                lc = getattr(st, "last_changed", None)
                if state is None or lc is None:
                    continue
                # Keep tz-aware local time so arithmetic with dt_util.now()
                # (also tz-aware) doesn't mix naive/aware datetimes.
                local = dt_util.as_local(lc)
                if prev is not None and state != prev:
                    service = self._infer_service(prev, state)
                    if service is not None:
                        out.append((entity_id, local, service))
                prev = state
        return out

    async def _get_observations(self, start, manual_only, exclude_reactive):
        """Live-captured events as [(entity_id, local_dt, service), ...].

        Context here is reliable, so we can filter by source:
          manual_only     -> keep only has_user=1 (a person acted in HA app/UI)
          exclude_reactive-> drop has_parent=1 (reactive automation/script chain)
        """
        start_ts = start.timestamp()

        def _query():
            sql = "SELECT entity_id, ts, service FROM observations WHERE ts >= ?"
            params: list = [start_ts]
            if manual_only:
                sql += " AND has_user = 1"
            if exclude_reactive:
                sql += " AND has_parent = 0"
            con = sqlite3.connect(self.db_path)
            rows = con.execute(sql, params).fetchall()
            # Opportunistic prune of anything older than the window.
            con.execute("DELETE FROM observations WHERE ts < ?", (start_ts,))
            con.commit()
            con.close()
            return rows

        rows = await self.hass.async_add_executor_job(_query)
        out = []
        for entity_id, ts, service in rows:
            local = dt_util.as_local(dt_util.utc_from_timestamp(ts))
            out.append((entity_id, local, service))
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

    def _pretty_batch(self, cands: list[Candidate]) -> str:
        """One line for one action; a numbered list ending in '?' for many."""
        if len(cands) == 1:
            return f"{self._pretty(cands[0])}?"
        items = "; ".join(self._pretty(c) for c in cands)
        return f"Do {len(cands)} things? {items}"
