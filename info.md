# Instinct

One button on your Apple Watch. It knows what you meant.

Press the Action Button → Instinct scores what you usually do around this time,
on this kind of day, given your home's current state → it asks ("Turn off Hall
Light?") or, once a context is proven reliable, just does it.

- Native Home Assistant integration — HACS install, UI config, no add-ons.
- No cloud AI, no model training — transparent heuristic scoring.
- Confirm-first (✅/❌); every tap teaches it.
- Auto-executes only after a context earns >90% confidence over several
  confirmations.
- Learned data stays local and survives updates.

See the README for setup: add via HACS, configure your notify service, grab the
webhook URL from the notification, and bind a Shortcut to the Action Button.
