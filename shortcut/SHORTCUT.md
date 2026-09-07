# Apple Watch Action Button → Home Assistant

You need two tiny pieces: a **Shortcut** that calls the webhook, and the
**Action Button** bound to it. Confirmations come back as normal iOS
notifications with Yes/No buttons — those work on the Watch automatically.

## 1. Get your webhook URL

It's your HA URL + `/api/webhook/` + the `webhook_id` from
`homeassistant/automation.yaml`:

```
https://YOUR-HA-URL/api/webhook/smart-action-CHANGE-ME-to-a-long-random-string
```

- On the same Wi-Fi/VPN as HA: use the local URL (e.g. `http://homeassistant.local:8123/...`).
- Away from home: use your Nabu Casa / reverse-proxy HTTPS URL and set
  `local_only: false` in the automation (already done in the sample).

No auth header is needed — webhooks are unauthenticated by design, which is why
the id must be long and secret.

## 2. Build the Shortcut

1. Open **Shortcuts** app → **+** → name it `Smart Action`.
2. Add action **Get Contents of URL**.
3. Set URL to your webhook URL from step 1.
4. Expand **Show More**:
   - **Method:** `POST`
   - **Request Body:** `JSON` (can be empty `{}` — the app doesn't need a payload)
5. (Optional) Add a final **Show Notification** "Sent 👍" so you get haptic
   feedback that it fired.

Test it: run the Shortcut once. You should see the confirmation notification
(or, later, "Did it: …") on your phone/watch.

## 3. Bind the Action Button

On iPhone: **Settings → Action Button → scroll to Shortcut → choose
`Smart Action`.**

Now one press of the Action Button = one smart action.

## How it feels over time

- **Week 1:** every press asks "Turn off Hall Light?" → tap ✅ / ❌.
- **Later:** in contexts you've confirmed ~8+ times at >90%, it stops asking and
  just does it, showing "Did it: Turn off Hall Light."
- Wrong guess? Tap ❌ and it learns that this context isn't that action.

## Troubleshooting

- **Nothing happens:** check the automation fired (Settings → Automations →
  Traces) and that AppDaemon logs show `Trigger received`.
- **No confirmation notification:** verify `notify_service` in `apps.yaml`
  matches your device (Developer Tools → Actions → search `notify`).
- **"Nothing obvious to do":** you don't have enough history yet in this
  time-of-day/day bucket, or the usual target is already in the desired state.
