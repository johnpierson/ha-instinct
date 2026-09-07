"""
Smart Action — a context-aware "one button" predictor for Home Assistant.

Idea: you press one button (Apple Watch Action Button -> Shortcut -> HA webhook
-> event `smart_action_trigger`). This app looks at what you *usually* do at
this time / day / context, picks the single most likely action, and either:

  * asks you to confirm it (actionable notification on your watch/phone), or
  * just does it silently once it has learned that this context is reliable.

Every confirm / reject / override you give is stored as a labeled example, so
the predictions get better and the confirmations gradually stop.

No ML. This is transparent heuristic scoring over your real HA history plus a
small feedback table. You can read every number it uses.

Author: John Pierson
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import appdaemon.plugins.hass.hassapi as hass


# ---------------------------------------------------------------------------
# Tunables (overridable in apps.yaml)
# ---------------------------------------------------------------------------
DEFAULTS = {
    # How many days of history to learn from.
    "history_days": 30,
    # Time-of-day window (minutes) that counts as "around now".
    "time_window_minutes": 60,
    # Minimum score before we'll even suggest something.
    "min_score": 1.0,
    # Confidence (0-1) at/above which we auto-execute WITHOUT asking.
    "auto_execute_confidence": 0.9,
    # Need at least this many observations in a context bucket before we
    # trust it enough to auto-execute.
    "auto_execute_min_samples": 8,
    # Which domains are candidates for control. Keep it tight & safe.
    "domains": ["light", "switch", "fan", "cover", "climate", "media_player"],
    # Entities to never touch (globs allowed via simple prefix match).
    "exclude_entities": [],
    # notify service for actionable notifications, e.g. "notify/mobile_app_johns_watch"
    "notify_service": None,
    # Where to keep the feedback DB. Defaults next to this app.
    "db_path": None,
}


@dataclass
class Candidate:
    entity_id: str
    service: str  # "turn_on" | "turn_off"
    score: float = 0.0
    samples: int = 0
    reasons: list = field(default_factory=list)

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def key(self) -> str:
        return f"{self.entity_id}|{self.service}"


class SmartAction(hass.Hass):
    # ------------------------------------------------------------------ setup
    def initialize(self):
        self.cfg = {**DEFAULTS, **self.args}

        # Store the learned feedback DB inside a `data/` subdir. HACS is told
        # (via hacs.json `persistent_directory`) to preserve this folder across
        # updates, so your learning survives upgrades.
        db_path = self.cfg["db_path"] or os.path.join(
            os.path.dirname(__file__), "data", "smart_action.db"
        )
        self.db_path = db_path
        self._init_db()

        # Fired by an HA automation off the Shortcut webhook.
        self.listen_event(self.on_trigger, "smart_action_trigger")

        # Feedback from actionable notifications (iOS mobile app).
        self.listen_event(
            self.on_notification_action, "mobile_app_notification_action"
        )

        # Pending suggestion awaiting a confirm/reject, keyed by action tag.
        self._pending: dict[str, Candidate] = {}

        self.log("Smart Action ready. Listening for smart_action_trigger.")

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                ts          REAL,
                context_key TEXT,
                entity_id   TEXT,
                service     TEXT,
                outcome     TEXT   -- 'confirm' | 'reject' | 'override' | 'auto'
            )
            """
        )
        con.commit()
        con.close()

    # --------------------------------------------------------------- trigger
    def on_trigger(self, event_name, data, kwargs):
        """Button pressed. Decide the single best action."""
        now = datetime.now()
        self.log(f"Trigger received at {now:%H:%M} ({now:%a}).")

        candidates = self._score_candidates(now)
        if not candidates:
            self._notify_plain("Smart Action", "Nothing obvious to do right now.")
            return

        best = candidates[0]
        ctx = self._context_key(now)
        conf, samples = self._confidence(ctx, best)
        best.samples = samples

        self.log(
            f"Best: {best.service} {best.entity_id} "
            f"score={best.score:.2f} conf={conf:.2f} n={samples} "
            f"reasons={best.reasons}"
        )

        # Confident enough + enough evidence -> just do it.
        if (
            conf >= self.cfg["auto_execute_confidence"]
            and samples >= self.cfg["auto_execute_min_samples"]
        ):
            self._execute(best)
            self._record(ctx, best, "auto")
            self._notify_plain(
                "Smart Action",
                f"Did it: {self._pretty(best)}.",
            )
            return

        # Otherwise ask for confirmation and learn from the answer.
        self._ask(best, ctx)

    # --------------------------------------------------------------- scoring
    def _score_candidates(self, now: datetime) -> list[Candidate]:
        start = now - timedelta(days=self.cfg["history_days"])
        window = self.cfg["time_window_minutes"]
        cur_dow = now.weekday()
        is_weekend = cur_dow >= 5

        # Aggregate: for each (entity, service) how often did it happen in a
        # similar context, weighted by recency & context similarity.
        scores: dict[str, Candidate] = {}

        for entity_id in self._candidate_entities():
            history = self._get_history(entity_id, start, now)
            for changed_at, from_s, to_s in history:
                service = self._infer_service(from_s, to_s)
                if service is None:
                    continue

                # Time-of-day proximity.
                minutes_off = self._minutes_apart(changed_at, now)
                if minutes_off > window:
                    continue
                time_w = 1.0 - (minutes_off / window)  # 1 at exact, 0 at edge

                # Day similarity: same weekday best, same weekend/weekday next.
                if changed_at.weekday() == cur_dow:
                    day_w = 1.0
                elif (changed_at.weekday() >= 5) == is_weekend:
                    day_w = 0.5
                else:
                    day_w = 0.2

                # Recency: linear decay over the window.
                age_days = (now - changed_at).total_seconds() / 86400.0
                recency_w = max(0.1, 1.0 - age_days / self.cfg["history_days"])

                w = time_w * day_w * recency_w
                cand = scores.get(f"{entity_id}|{service}")
                if cand is None:
                    cand = Candidate(entity_id, service)
                    scores[cand.key] = cand
                cand.score += w
                cand.samples += 1

        # Feedback adjustment + validity filtering.
        results = []
        for cand in scores.values():
            self._apply_feedback(cand, now)
            if cand.score < self.cfg["min_score"]:
                continue
            if not self._is_actionable(cand):
                continue
            cand.reasons.append(f"{cand.samples} similar events")
            results.append(cand)

        results.sort(key=lambda c: c.score, reverse=True)
        return results

    def _apply_feedback(self, cand: Candidate, now: datetime):
        """Boost things you've confirmed here, penalize things you rejected."""
        ctx = self._context_key(now)
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT outcome FROM feedback WHERE context_key=? AND entity_id=? "
            "AND service=?",
            (ctx, cand.entity_id, cand.service),
        ).fetchall()
        con.close()
        for (outcome,) in rows:
            if outcome in ("confirm", "auto", "override"):
                cand.score *= 1.25
            elif outcome == "reject":
                cand.score *= 0.5

    def _confidence(self, ctx: str, cand: Candidate) -> tuple[float, int]:
        """Confidence = confirm-rate in this exact context bucket (smoothed)."""
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT outcome FROM feedback WHERE context_key=? AND entity_id=? "
            "AND service=?",
            (ctx, cand.entity_id, cand.service),
        ).fetchall()
        con.close()
        good = sum(1 for (o,) in rows if o in ("confirm", "auto", "override"))
        bad = sum(1 for (o,) in rows if o == "reject")
        n = good + bad
        # Laplace-smoothed positive rate.
        conf = (good + 1) / (n + 2)
        return conf, n

    # ------------------------------------------------------------- execution
    def _execute(self, cand: Candidate):
        domain = cand.domain
        self.call_service(f"{domain}/{cand.service}", entity_id=cand.entity_id)
        self.log(f"Executed {cand.service} on {cand.entity_id}.")

    # --------------------------------------------------------- notifications
    def _ask(self, cand: Candidate, ctx: str):
        tag = f"smart_action_{int(time.time())}"
        self._pending[tag] = cand

        service = self.cfg["notify_service"]
        if not service:
            # No notifier configured -> fail safe: just execute (v0 fallback).
            self.log("No notify_service set; executing without confirmation.")
            self._execute(cand)
            self._record(ctx, cand, "auto")
            return

        # iOS actionable notification. Buttons post
        # mobile_app_notification_action events with actionName == the action.
        self.call_service(
            service.replace("/", "/"),
            title="Smart Action",
            message=f"{self._pretty(cand)}?",
            data={
                "tag": tag,
                "actions": [
                    {"action": f"SA_YES::{tag}", "title": "✅ Yes"},
                    {"action": f"SA_NO::{tag}", "title": "❌ No"},
                ],
                # store context so we can log it on reply
                "smart_action_ctx": ctx,
            },
        )
        self.log(f"Asked to {self._pretty(cand)} (tag={tag}).")

        # Auto-expire the pending suggestion after 2 minutes.
        self.run_in(self._expire_pending, 120, tag=tag)

    def on_notification_action(self, event_name, data, kwargs):
        action = data.get("actionName") or data.get("action", "")
        if "::" not in action:
            return
        verb, tag = action.split("::", 1)
        cand = self._pending.pop(tag, None)
        if cand is None:
            return
        ctx = self._context_key(datetime.now())
        if verb == "SA_YES":
            self._execute(cand)
            self._record(ctx, cand, "confirm")
            self._notify_plain("Smart Action", f"Done: {self._pretty(cand)}.")
        elif verb == "SA_NO":
            self._record(ctx, cand, "reject")
            self.log(f"User rejected {self._pretty(cand)}.")

    def _expire_pending(self, kwargs):
        self._pending.pop(kwargs.get("tag"), None)

    def _notify_plain(self, title: str, message: str):
        service = self.cfg["notify_service"]
        if not service:
            self.log(f"[notify] {title}: {message}")
            return
        self.call_service(service, title=title, message=message)

    # --------------------------------------------------------------- helpers
    def _record(self, ctx: str, cand: Candidate, outcome: str):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO feedback VALUES (?,?,?,?,?)",
            (time.time(), ctx, cand.entity_id, cand.service, outcome),
        )
        con.commit()
        con.close()

    def _context_key(self, now: datetime) -> str:
        """Coarse bucket: weekend-ness + hour. Keeps buckets dense."""
        part = "we" if now.weekday() >= 5 else "wd"
        return f"{part}:{now.hour:02d}"

    def _candidate_entities(self) -> list[str]:
        out = []
        for domain in self.cfg["domains"]:
            for entity in self.get_state(domain) or {}:
                if self._excluded(entity):
                    continue
                out.append(entity)
        return out

    def _excluded(self, entity_id: str) -> bool:
        for ex in self.cfg["exclude_entities"]:
            if entity_id == ex or entity_id.startswith(ex.rstrip("*")):
                return True
        return False

    def _get_history(self, entity_id: str, start: datetime, end: datetime):
        """Return list of (datetime, from_state, to_state) transitions."""
        data = self.get_history(entity_id=entity_id, start_time=start)
        out = []
        if not data:
            return out
        series = data[0]
        prev = None
        for item in series:
            state = item.get("state")
            last_changed = item.get("last_changed")
            if not last_changed:
                continue
            try:
                dt = datetime.fromisoformat(last_changed).astimezone().replace(
                    tzinfo=None
                )
            except ValueError:
                continue
            if prev is not None and state != prev:
                out.append((dt, prev, state))
            prev = state
        return out

    @staticmethod
    def _infer_service(from_s: str, to_s: str) -> str | None:
        on_states = {"on", "open", "playing", "home", "heat", "cool", "auto"}
        off_states = {"off", "closed", "idle", "paused", "away"}
        was_on = from_s in on_states
        now_on = to_s in on_states
        if to_s in off_states or (was_on and not now_on):
            return "turn_off"
        if to_s in on_states or (not was_on and now_on):
            return "turn_on"
        return None

    def _is_actionable(self, cand: Candidate) -> bool:
        """Don't suggest turning off something already off, etc."""
        state = self.get_state(cand.entity_id)
        if state in (None, "unavailable", "unknown"):
            return False
        on_states = {"on", "open", "playing", "home"}
        currently_on = state in on_states
        if cand.service == "turn_off" and not currently_on:
            return False
        if cand.service == "turn_on" and currently_on:
            return False
        return True

    @staticmethod
    def _minutes_apart(a: datetime, now: datetime) -> float:
        """Circular distance in minutes over a 24h clock."""
        am = a.hour * 60 + a.minute
        nm = now.hour * 60 + now.minute
        d = abs(am - nm)
        return min(d, 1440 - d)

    def _pretty(self, cand: Candidate) -> str:
        name = self.friendly_name(cand.entity_id) or cand.entity_id
        verb = "Turn off" if cand.service == "turn_off" else "Turn on"
        return f"{verb} {name}"

    def friendly_name(self, entity_id: str) -> str:
        return self.get_state(entity_id, attribute="friendly_name") or entity_id
