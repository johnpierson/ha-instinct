<img src="brands/icon.png" width="96" align="right" alt="Instinct icon" />

# Instinct 🧠

One button. It knows what you meant.

Press the **Action Button** on your Apple Watch → Instinct looks at what you
*usually* do around this time, on this kind of day, given the current state of
your house → it either **asks** ("Turn off Hall Light?") or, once it's learned
that context is reliable, **just does it**.

No cloud AI, no model training. It's transparent heuristic scoring over your
real Home Assistant history plus a small feedback table that learns from every
✅ / ❌ you tap.

> Now a native **Home Assistant custom integration** — installs through HACS,
> configures in the UI, no add-ons required.

## How it works

```
Apple Watch Action Button
      │  (one press)
      ▼
Apple Shortcut ──HTTP POST──► HA Webhook (created by the integration)
                                   │
                                   ▼
                         InstinctEngine
                    1. read recorder history (last 30d)
                    2. score every on/off you've done in a
                       similar time+day+recency context
                    3. drop anything already in target state
                    4. pick the single best action
                       ├─ confident + enough evidence → DO IT
                       └─ otherwise → actionable notification
                                        (✅ / ❌) → learn
```

Three ways to trigger it (all funnel to the same engine):

- **Webhook** — `POST /api/webhook/<id>` (the Apple Watch path).
- **Service** — `instinct.trigger` (call it from any automation/script).
- **Event** — fire `instinct_trigger` however you like.

### Why this design

- **Sparse-data friendly.** Works from day one; no training period.
- **Explainable.** Every suggestion logs its score and reasons. No black box.
- **Safe.** Never suggests turning off something already off. You approve
  everything until a context proves itself (~8 confirmations at >90%).
- **Self-teaching.** The confirmation taps *are* the training data.

## Install (about 10 minutes)

**Prereqs:** Home Assistant with **Recorder** enabled (default) and **HACS**
installed. No AppDaemon, no add-ons.

1. **Add the repo to HACS.**
   HACS → ⋮ → **Custom repositories** → URL
   `https://github.com/johnpierson/ha-instinct`, category **Integration** →
   **Add**.
2. **Download** "Instinct" in HACS, then **restart Home Assistant**.
3. **Add the integration.**
   Settings → Devices & Services → **Add Integration** → *Instinct*. Enter your
   notify service (e.g. `notify.mobile_app_your_iphone`) — find it under
   Developer Tools → Actions.
4. **Grab the webhook URL.**
   A notification titled **"Instinct webhook"** appears with your internal &
   external URLs. That's what the Shortcut calls.
5. **Wire the Apple Watch.**
   Follow [`shortcut/SHORTCUT.md`](shortcut/SHORTCUT.md) — build the Shortcut,
   bind it to the Action Button.

Tune behavior anytime via the integration's **Configure** button (history
window, auto-execute thresholds, controllable domains, exclusions).

## Configuration reference

Set at add-time:

| Field | Meaning |
|---|---|
| `notify_service` | iOS notify service for confirmations (**required**) |

Set later under **Configure**:

| Option | Default | Meaning |
|---|---|---|
| `history_days` | `30` | How far back to learn from |
| `time_window_minutes` | `60` | ± window that counts as "around now" |
| `min_score` | `1.0` | Minimum score to suggest anything |
| `auto_execute_confidence` | `0.9` | Confirm-rate to stop asking |
| `auto_execute_min_samples` | `8` | Min confirmations before auto-acting |
| `manual_only` | `off` | Learn only from person-triggered changes (HA context `user_id`); skips automation/script **and** physical-switch changes. Uses live capture (warms up from install) |
| `exclude_reactive` | `off` | Drop reactive automation/script chains (context `parent_id`); keeps app/Apple Home/physical/time-based. Uses live capture (warms up from install) |
| `multi_action` | `off` | Confirm & run the top-N context actions as one batch (single ✅ does all) |
| `max_actions` | `3` | Cap on actions per press when multi-action is on |
| `domains` | light, switch, fan, cover, media_player | Controllable domains |
| `exclude_entities` | — | Never touch (exact id or `prefix*`) |

## How it feels over time

- **Week 1:** every press asks "Turn off Hall Light?" → tap ✅ / ❌.
- **Later:** in contexts you've confirmed ~8+ times at >90%, it stops asking and
  just does it, showing "Did it: Turn off Hall Light."
- Wrong guess? Tap ❌ and it learns this context isn't that action.

## Two data sources

- **Default (no source filter):** scoring reads 30 days of **recorder history** —
  full coverage from day one.
- **Source filters (`manual_only` / `exclude_reactive`):** these need reliable
  per-change context, which recorder history doesn't expose. So Instinct keeps
  its own **live observation log** (every candidate change + its context) and
  scores from that when a filter is on. Tradeoff: filtered modes **warm up from
  install time** rather than using back-history.

> Why: an action's source (person vs. automation vs. Apple Home) only lives in
> HA's *live* context. Apple Home / HomeKit actions in particular arrive with no
> `user_id` and no `parent_id`, so they can't be singled out — but reactive
> automations (which carry a `parent_id`) can be excluded.

## Data & privacy

Everything runs locally. The learned feedback and observation log live in
`instinct.db` in your HA config folder — **not** inside `custom_components/`, so
HACS updates never wipe your learning. No data leaves your instance.

## Roadmap / ideas

- Long-press → "top 3 menu" instead of a single guess.
- Presence & sun state folded into the context bucket (currently weekend-ness +
  hour).
- Per-person models if multiple people carry a watch.
- Optional swap of the heuristic scorer for a small model once you have months
  of labeled feedback — the DB is already the training set.

## Layout

```
custom_components/instinct/__init__.py      # setup: webhook + service + event
custom_components/instinct/engine.py        # the brain (scoring + learning)
custom_components/instinct/config_flow.py   # UI setup + options
custom_components/instinct/const.py
custom_components/instinct/manifest.json
custom_components/instinct/services.yaml
custom_components/instinct/strings.json
shortcut/SHORTCUT.md                        # Apple Watch wiring
```

## Safety notes

- The webhook id is an unauthenticated secret URL. Keep it private; delete &
  re-add the integration to rotate it.
- Start without `climate` in domains; add it once you trust it.
- Everything is confirm-first until a context earns auto-execute.
