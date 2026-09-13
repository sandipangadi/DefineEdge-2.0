# Shoonya local relay

This is the no-extra-cost path for Shoonya when the Prism API key is tied to the user's home IP. The Shoonya API request runs on the user's Windows computer; Render receives only a sanitized NIFTY 5-minute ZIP for Drive publishing.

## One-time setup

1. Download shoonya_local_relay.py, .env.shoonya.example, and start_shoonya_relay.bat into one Windows folder.
2. Make a copy of .env.shoonya.example named .env.shoonya.
3. Fill .env.shoonya with the static Shoonya values and the existing collector password. Keep this file private.
4. Do not change the existing Shoonya Primary IP if it is the public IP allowed for your computer.

## Each collection

1. Double-click start_shoonya_relay.bat.
2. The local page opens at http://127.0.0.1:8765.
3. Generate the current 6-digit TOTP in the Shoonya Authenticator/Chrome extension.
4. Enter it and click Fetch and upload read-only data.
5. The local relay logs in from the allowed home connection, fetches NIFTY 5-minute history in chunks, and uploads a sanitized ZIP to the existing Render/Drive pipeline.

The TOTP is used only for that request and is not saved. The relay has no order placement, modification, cancellation, or position-management code.

## Date range

The example configuration is prefilled for the Red Bar overlap window **2026-02-25 through 2026-05-18**. You can change or clear these dates on the local page. If both are blank, the fallback lookback setting is used.

## Troubleshooting

- 502: retry later; this is a Shoonya gateway/network response.
- Invalid IP: the Prism Primary IP does not match the computer's current public IP.
- Invalid Vendor Code or Invalid AppKey: verify the static values in .env.shoonya.
- Never commit .env.shoonya.
