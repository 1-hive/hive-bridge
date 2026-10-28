#!/usr/bin/env python3
"""Send the operator one plain message in the 1-hive Telegram chat, from the host.

    telegram-say.py "text"        (or the text on stdin)

For the operator's own sessions on the host (infrastructure questions, a step only the operator
can run) while they are away. It uses the bridge's bot and chat (~/.config/hive/telegram-token,
telegram.json) and touches nothing on the record. A reply goes to the chief of staff, like any
free text in that chat, so say in the message what a reply should be about.

With HIVE_TASK=<task id> in the environment (hive-runtime's publisher sets it for a published result), the
message also goes to the chat the task was started from, as a reply to its status message (the bridge's
origins.json), so whoever asked sees it there.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import hive_paths as hp

CFG = hp.CONFIG_DIR


def main() -> None:
    text = " ".join(sys.argv[1:]) or sys.stdin.read()
    if not text.strip():
        sys.exit("nothing to send")
    token = (CFG / "telegram-token").read_text().strip()
    chat = json.loads((CFG / "telegram.json").read_text())["chat_id"]
    sends = [{"chat_id": chat, "text": text[:4000]}]
    origin = origin_of(os.environ.get("HIVE_TASK", ""))
    if origin and origin.get("chat_id") != chat:
        sends.insert(0, {"chat_id": origin["chat_id"], "text": text[:4000], "allow_sending_without_reply": "true",
                         "reply_to_message_id": origin.get("status_id") or origin["message_id"]})
    failed = False
    for params in sends:
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                        urllib.parse.urlencode(params).encode(), timeout=30) as r:
                failed |= not json.load(r).get("ok")
        except OSError as e:
            print(f"telegram-say: {params['chat_id']}: {e}", file=sys.stderr)
            failed = True
    if failed:
        sys.exit("Telegram refused the message")


def origin_of(task: str) -> dict | None:
    """The chat message a task was started from, as the bridge keeps it."""
    if not task:
        return None
    try:
        return json.loads((hp.WORK_DIR / ".telegram" / "origins.json").read_text()).get(task)
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    main()
