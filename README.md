# Smart Action 🎯

One button. It figures out what you probably want, and does it.

Press the **Action Button** on your Apple Watch → Home Assistant looks at what
you *usually* do around this time, on this kind of day, given the current state
of your house → it either **asks** ("Turn off Hall Light?") or, once it's
learned that context is reliable, **just does it**.

No cloud AI, no training a model. It's transparent heuristic scoring over your
real HA history plus a small feedback table that learns from every ✅ / ❌ you
tap.

## How it works

```
Apple Watch Action Button
      │  (one press)
      ▼
Apple Shortcut ──HTTP POST──► HA Webhook
                                   │  fires event
                                   ▼
                         smart_action_trigger
                                   │
                                   ▼
                    AppDaemon app  (smart_action.py)
                    1. read recorder history (last 30d)
                    2. score every on/off you've done in a
                       similar time+day+recency context
                    3. drop anything already in target state
                    4. pick the single best action
                       ├─ confident + enough evidence → DO IT
                       └─ otherwise → actionable notification
                                        (✅ / ❌) → learn
```

### Why this design

- **Sparse-data friendly.** ML needs lots of labels; heuristics work from day
  one and degrade gracefully.
- **Explainable.** Every suggestion logs its score and reasons. No black box.
- **Safe.** It never suggests turning off a light that's already off. You
  approve everything until a context proves itself (~8 confirmations at >90%).
- **Self-teaching.** The confirmation taps *are* the training data.

## Install (about 15 minutes)

**Prereqs:** Home Assistant with the **Recorder** enabled (default), the
**AppDaemon** add-on installed (Settings → Add-ons → Add-on Store → AppDaemon),
and **HACS** if you want the one-click install path below.

### Option A — HACS (recommended)

1. In HACS, enable AppDaemon discovery: **HACS → ⋮ → Custom repositories**, or
   turn on AppDaemon apps in HACS settings (they're hidden by default).
2. **Custom repositories → Add**: URL `https://github.com/johnpierson/ha-smart-action`,
   category **AppDaemon**.
3. Find **Smart Action** in HACS → **Download**. HACS copies it into
   `.../appdaemon/apps/smart_action/`. Your learned data lives in a `data/`
   subfolder that HACS preserves across updates.
4. Continue at **step 2** below (configure `apps.yaml`).

### Option B — Manual

1. **Drop in the app.**
   Copy `apps/smart_action/` into your AppDaemon `apps/` folder.

2. **Configure it.**
   Merge `examples/apps.yaml` into your AppDaemon `apps.yaml`. Set
   `notify_service` to your iOS device (Developer Tools → Actions → search
   `notify`, e.g. `notify/mobile_app_johns_iphone`).

3. **Add the trigger automation.**
   Add `homeassistant/automation.yaml` to your HA automations and **change the
   `webhook_id`** to a long random string.

4. **Wire the Apple Watch.**
   Follow `shortcut/SHORTCUT.md` — build the Shortcut, bind it to the Action
   Button.

5. **Restart AppDaemon** and press the button. First presses will ask; it gets
   quieter as it learns.

## Configuration reference

All keys live in `apps.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `notify_service` | — | iOS notify service for confirmations (**required**) |
| `history_days` | `30` | How far back to learn from |
| `time_window_minutes` | `60` | ± window that counts as "around now" |
| `auto_execute_confidence` | `0.9` | Confirm-rate to stop asking |
| `auto_execute_min_samples` | `8` | Min confirmations before auto-acting |
| `min_score` | `1.0` | Minimum score to suggest anything |
| `domains` | light/switch/fan/cover/media_player | Controllable domains |
| `exclude_entities` | `[]` | Never touch (exact id or `prefix*`) |

## Roadmap / ideas

- Multiple candidates: long-press = "give me a menu of the top 3".
- Presence & sun context in the bucket key (currently weekend-ness + hour).
- Per-person models if multiple people carry the watch.
- Optional: swap the heuristic scorer for a small model once you have months of
  labeled feedback — the feedback table is already the training set.

## Layout

```
apps/smart_action/smart_action.py   # the brain (HACS-managed)
apps/smart_action/data/             # learned DB, preserved across updates
hacs.json                           # HACS metadata
examples/apps.yaml                  # config to merge into your apps.yaml
homeassistant/automation.yaml       # webhook → event
shortcut/SHORTCUT.md                # Apple Watch wiring
```

## Safety notes

- The webhook id is an unauthenticated secret URL. Keep it private; rotate it if
  it leaks.
- Start with `climate` commented out. Add domains as you trust it.
- Everything is confirm-first until a context earns auto-execute.
