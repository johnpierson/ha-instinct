# Smart Action

One button on your Apple Watch. It figures out what you probably want and does
it — learning from your Home Assistant history.

Press the Action Button → Smart Action scores what you usually do around this
time, on this kind of day, given your home's current state → it asks
("Turn off Hall Light?") or, once a context is proven reliable, just does it.

- No cloud AI, no model training — transparent heuristic scoring.
- Confirm-first (✅/❌); every tap teaches it.
- Auto-executes only after a context earns >90% confidence over several
  confirmations.

See the README for setup: AppDaemon app + one HA webhook automation + an Apple
Shortcut bound to the Action Button.
