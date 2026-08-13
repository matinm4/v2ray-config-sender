#!/usr/bin/env python3
"""Generate a Telethon StringSession to paste into the TELEGRAM_SESSION secret.

Run this on your own machine (never in CI — it needs an interactive login):

    pip install telethon
    python tools/gen_session.py

You will be asked for your api_id, api_hash, phone number, and the login code
Telegram sends you. The printed string is equivalent to a logged-in session:
treat it like a password, store it only as a GitHub Actions secret.
"""

from __future__ import annotations

import os
import sys

try:
    from telethon.sync import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    sys.exit("Telethon is not installed. Run: pip install telethon")


def main() -> int:
    api_id = os.environ.get("TELEGRAM_API_ID") or input("api_id: ").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH") or input("api_hash: ").strip()
    if not api_id.isdigit():
        return print("api_id must be numeric") or 1

    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        me = client.get_me()
        print("\nLogged in as:", getattr(me, "username", None) or me.first_name, f"(id={me.id})")
        print("\n=== TELEGRAM_SESSION (copy the single line below) ===\n")
        print(client.session.save())
        print("\n=== keep this secret — it grants full access to the account ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
