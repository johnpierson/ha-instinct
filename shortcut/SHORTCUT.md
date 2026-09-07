# Apple Watch Action Button → Instinct

Two tiny pieces: a **Shortcut** that calls Instinct's webhook, and the **Action
Button** bound to it. Confirmations come back as normal iOS notifications with
Yes/No buttons — those show on the Watch automatically.

## 1. Get your webhook URL

After you add the Instinct integration, HA posts a notification titled
**"Instinct webhook"** with your URLs. It looks like:

```
https://YOUR-HA-URL/api/webhook/<random-id>
```

- On the same Wi-Fi/VPN as HA: use the **Internal** URL.
- Away from home: use the **External** URL (needs Nabu Casa or a reverse proxy).

No auth header is needed — webhooks are unauthenticated by design, which is why
the id is long and secret. To rotate it, delete and re-add the integration.

## 2. Build the Shortcut

1. Open **Shortcuts** → **+** → name it `Instinct`.
2. Add **Get Contents of URL**.
3. Set the URL to your webhook URL from step 1.
4. Expand **Show More**:
   - **Method:** `POST`
   - **Request Body:** `JSON`, empty `{}` (no payload needed)
5. (Optional) Add **Show Notification** "Sent 👍" for haptic confirmation.

Test it: run the Shortcut once. You should get the confirmation notification
(or later, "Did it: …").

## 3. Bind the Action Button

iPhone: **Settings → Action Button → scroll to Shortcut → choose `Instinct`.**

One press = one instinctive action.

## How it feels over time

- **Week 1:** every press asks "Turn off Hall Light?" → tap ✅ / ❌.
- **Later:** in contexts you've confirmed ~8+ times at >90%, it stops asking and
  just does it.
- Wrong guess? Tap ❌ and it learns.

## Troubleshooting

- **Nothing happens:** check HA logs for `instinct` at debug, and confirm the
  webhook URL is exactly right.
- **No confirmation notification:** verify the notify service in the
  integration's setup matches your device (Developer Tools → Actions →
  `notify.mobile_app_...`).
- **"Nothing obvious to do":** not enough history in this time/day bucket yet,
  or the usual target is already in the desired state.
- **Prefer an automation instead of the webhook?** Call the `instinct.trigger`
  service from any automation — same result.
