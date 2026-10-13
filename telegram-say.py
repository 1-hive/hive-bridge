#!/usr/bin/env python3
"""Send the operator one plain message in the 1-hive Telegram chat, from the host.

    telegram-say.py "text"        (or the text on stdin)

For the operator's own sessions on the host (infrastructure questions, a step only the operator
can run) while they are away. It uses the bridge's bot and chat (~/.config/hive/telegram-token,
telegram.json) and touches nothing on the record. A reply goes to the chief of staff, like any
free text in that chat, so say in the message what a reply should be about.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CFG = Path.home() / ".config" / "hive"


def main() -> None:
    text = " ".join(sys.argv[1:]) or sys.stdin.read()
    if not text.strip():
        sys.exit("nothing to send")
    token = (CFG / "telegram-token").read_text().strip()
    chat = json.loads((CFG / "telegram.json").read_text())["chat_id"]
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30) as r:
        if not json.load(r).get("ok"):
            sys.exit("Telegram refused the message")


if __name__ == "__main__":
    main()
