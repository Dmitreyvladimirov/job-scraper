"""Point the Telegram bot at the dashboard's webhook. Run once per URL/secret change.

The same bot token that sends run summaries also receives intake messages — nothing
in this project polls getUpdates, so registering a webhook takes nothing away.

Usage (from the repo root):

    export TELEGRAM_TOKEN=...            # same value as the Railway services use
    export TELEGRAM_WEBHOOK_SECRET=...   # must match the Dashboard service's var
    python scripts/set_telegram_webhook.py https://job-scraper-production-60fd.up.railway.app

Pass --delete instead of a URL to unregister (the bot goes quiet; summaries keep
working, since those are outbound).

allowed_updates is narrowed to the two kinds the bot acts on: without it Telegram
also delivers edited messages, channel posts and every other update type, each of
which would spawn a thread that does nothing.
"""
import json
import os
import sys
import urllib.request

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")


def call(method: str, body: dict) -> dict:
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}",
                                 data=json.dumps(body).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    if not TOKEN:
        print("TELEGRAM_TOKEN is not set")
        return 1
    if len(sys.argv) != 2:
        print(__doc__)
        return 1

    if sys.argv[1] == "--delete":
        print(json.dumps(call("deleteWebhook", {"drop_pending_updates": True}), indent=2))
        return 0

    if not SECRET:
        print("TELEGRAM_WEBHOOK_SECRET is not set — the route answers 503 without it")
        return 1

    base = sys.argv[1].rstrip("/")
    if not base.startswith("https://"):
        print("Telegram only delivers to https:// URLs")
        return 1

    result = call("setWebhook", {
        "url": f"{base}/tg/{SECRET}",
        "secret_token": SECRET,
        "allowed_updates": ["message", "callback_query"],
        # Anything queued while the webhook was unset is stale by definition — a
        # vacancy sent hours ago should not suddenly get scored on registration.
        "drop_pending_updates": True,
    })
    print(json.dumps(result, indent=2))
    info = call("getWebhookInfo", {})
    # The secret appears in the URL, so print only the fields worth reading back.
    result_info = info.get("result", {})
    print(json.dumps({k: v for k, v in result_info.items() if k != "url"}, indent=2))
    print(f"webhook host: {base}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
