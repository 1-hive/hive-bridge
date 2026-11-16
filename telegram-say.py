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

With HIVE_PR=<pull request URL> and HIVE_PR_SHA=<its reviewed head> as well (the publisher, for a pull request it just
opened), every copy of the message gets a 🔀 Merge button; the key, PR, commit, task and messages go into the bridge's
.telegram/prs.json, and an operator's tap there merges it (the bridge's HIVE_PR_MERGE_CMD).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
import time

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
    pr, sha = os.environ.get("HIVE_PR", ""), os.environ.get("HIVE_PR_SHA", "")
    key = hashlib.sha256(f"{pr} {sha}".encode()).hexdigest()[:16] if pr and sha else ""
    if key:
        for params in sends:
            params["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": "🔀 Merge",
                                                                         "callback_data": f"prmerge:{key}"}]]})
    failed, sent = False, []
    for params in sends:
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                        urllib.parse.urlencode(params).encode(), timeout=30) as r:
                out = json.load(r)
                failed |= not out.get("ok")
                if out.get("ok"):
                    sent.append([params["chat_id"], out["result"]["message_id"]])
        except OSError as e:
            print(f"telegram-say: {params['chat_id']}: {e}", file=sys.stderr)
            failed = True
    if key and sent:
        remember_pr(key, {"url": pr, "sha": sha, "task": os.environ.get("HIVE_TASK", ""), "messages": sent,
                          "at": time.time()})
    if failed:
        sys.exit("Telegram refused the message")


def remember_pr(key: str, pr: dict) -> None:
    """The bridge's prs.json: what a 🔀 Merge tap with this key merges (the last 300 pull requests)."""
    path = hp.WORK_DIR / ".telegram" / "prs.json"
    try:
        prs = json.loads(path.read_text())
    except (OSError, ValueError):
        prs = {}
    prs[key] = pr
    prs = dict(sorted(prs.items(), key=lambda kv: kv[1].get("at", 0))[-300:])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".prs.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(prs, indent=1))
    tmp.chmod(0o644)   # written as the publisher's user (root on proto-hive); the bridge reads it
    tmp.replace(path)


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
