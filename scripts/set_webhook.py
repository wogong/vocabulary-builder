"""Point Telegram at this server (spec §7.1).

Run once after the bot is reachable over HTTPS, and again whenever the public
hostname or TG_WEBHOOK_SECRET changes.

    docker compose exec -T app python scripts/set_webhook.py https://api.example.com
    docker compose exec -T app python scripts/set_webhook.py --delete
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.telegram import Telegram  # noqa: E402


async def run() -> int:
    cfg = Config.from_env()
    if not cfg.tg_bot_token:
        print("TG_BOT_TOKEN is not set.")
        return 1

    telegram = Telegram(cfg.tg_bot_token)
    try:
        if "--delete" in sys.argv:
            await telegram.delete_webhook()
            print("webhook removed")
            return 0

        base = next((a for a in sys.argv[1:] if not a.startswith("-")), "")
        if not base:
            print(__doc__)
            return 2
        if not cfg.tg_webhook_secret:
            print("TG_WEBHOOK_SECRET is not set — refusing to expose an unguarded webhook.")
            return 1
        if not base.startswith("https://"):
            print("Telegram only delivers to HTTPS endpoints.")
            return 1
        if not cfg.tg_allowed_chat_ids:
            print("warning: TG_ALLOWED_CHAT_IDS is empty, so every message will be ignored")

        url = f"{base.rstrip('/')}/tg/{cfg.tg_webhook_secret}"
        await telegram.set_webhook(url, cfg.tg_webhook_secret)
        me = await telegram.call("getMe")
        print(f"webhook set for @{me['username']}")
        print(f"  {url}")
        print("\nSend the bot a message; if nothing happens, check that your chat id is in")
        print(f"  TG_ALLOWED_CHAT_IDS={','.join(str(i) for i in cfg.tg_allowed_chat_ids)}")
        return 0
    finally:
        await telegram.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
