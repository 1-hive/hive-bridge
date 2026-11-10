"""A group chat's transcript, as Markdown, from the bridge's dialogue log (.telegram/dialogue.jsonl).

    python3 transcript.py <dialogue.jsonl> <chat name> [days] > <file>.md

The log has every message the bridge saw in its group chats, and its own messages there ("sent": true: the chief of
staff's replies, task progress, decision cards). The chief of staff runs this when someone asks for a transcript and
sends the file. Lines are chosen by chat name, so a group Telegram upgraded to a supergroup (a new chat id, message
ids starting again) stays one chat. Left out: /board and the like, and the bridge's answers to them.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

COMMANDS = ("/board", "/floor", "/start", "/help", "/digest", "/inbox")


def read(path: Path) -> list[dict]:
    out = []
    try:
        with path.open() as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def is_command(text: str) -> bool:
    return text.strip().split("@")[0].split(" ")[0] in COMMANDS


def build(lines: list[dict], chat_name: str, days: float | None = None, *, hive_id: str = "hive",
          bot_label: str = "hive", now: dt.datetime | None = None) -> str:
    """The transcript of one chat: a day heading per day, each message with its time (UTC) and sender, and the start
    of the message it answers."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=days)).isoformat() if days else ""
    es = [e for e in lines if e.get("chat_name") == chat_name and e.get("at", "") >= since]
    byid = {(e.get("chat"), e.get("id")): e for e in es if e.get("id") is not None}
    skip = {(e.get("chat"), e.get("id")) for e in es if not e.get("sent") and is_command(e.get("text", ""))}
    keep = []
    for e in sorted(es, key=lambda x: x.get("at", "")):
        key = (e.get("chat"), e.get("id"))
        if key in skip or (e.get("sent") and (e.get("chat"), e.get("reply_to")) in skip):
            continue
        if not (e.get("text") or "").strip() and not e.get("files"):
            continue
        keep.append(e)

    def who(e: dict) -> str:
        return bot_label if e.get("sent") else (e.get("from") or "someone")

    head = f"# {hive_id}: the {chat_name} group\n\n"
    if not keep:
        return head + "No messages" + (f" in the last {days:g} days" if days else "") + ".\n"
    out = [head + f"{len(keep)} messages, {keep[0]['at'][:10]} to {keep[-1]['at'][:10]}. Times are UTC. "
           f"**{bot_label}** is the hive's bot: the chief of staff's replies and the bridge's notices (task progress, "
           f"results, decisions). Attached files are named, not included.\n"]
    day = None
    for e in keep:
        if e["at"][:10] != day:
            day = e["at"][:10]
            out.append(f"\n## {day}\n")
        rt = ""
        q = byid.get((e.get("chat"), e.get("reply_to"))) if e.get("reply_to") is not None else None
        if q:
            first = " ".join((q.get("text") or "").split())
            rt = f" _(to {who(q)}: \"{first[:80]}{'…' if len(first) > 80 else ''}\")_"
        files = "".join(f"\n  [file: {d.get('name')}]" for d in e.get("files") or [] if d.get("name"))
        body = (e.get("text") or "").strip().replace("\n", "\n  ")
        out.append(f"**{e['at'][11:16]} {who(e)}**{rt}\n  {body}{files}\n")
    return "\n".join(out)


def chat_names(lines: list[dict]) -> list[str]:
    seen: dict[str, int] = {}
    for e in lines:
        if e.get("chat_name"):
            seen[e["chat_name"]] = seen.get(e["chat_name"], 0) + 1
    return sorted(seen, key=lambda n: -seen[n])


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__.split("\n\n")[1] + "\nchats: " + ", ".join(chat_names(read(Path(sys.argv[1]))) if len(sys.argv) > 1 else []))
    lines = read(Path(sys.argv[1]))
    if sys.argv[2] not in chat_names(lines):
        sys.exit(f"no chat {sys.argv[2]!r} in the log; chats: {', '.join(chat_names(lines)) or 'none'}")
    print(build(lines, sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else None,
                hive_id=os.environ.get("HIVE_ID") or "hive"))
