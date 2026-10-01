#!/usr/bin/env python3
"""1-hive Telegram bridge: the operator's inbox on their phone, with buttons.

Deterministic, no model. It:
- pushes each new operator inbox item (goal to approve or accept, escalation,
  pending exception) once, with buttons;
- turns button taps into signed record events: goal.approved, goal.accepted,
  goal.reopened (asks for the reason as a reply) and goal.abandoned;
- accepts taps and replies only from the configured chat and user.

It signs with its own operator key (~/.config/hive/telegram-bridge.key), registered
on the operator actor with actor.key_added, and marks events `via: telegram`.
It never reads the operator's main key.

Free text that isn't a reply to a reason prompt is handed to `--chat-cmd`, if
given (the chief of staff; 1-hive PLAN D15); otherwise it gets a short note.

    telegram-bridge.py [--poll 30] [--inbox-every 60] [--chat-cmd CMD]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

CFG = Path.home() / ".config" / "hive"
STATE = Path.home() / "work" / "1hive" / ".telegram" / "state.json"
TOKEN = (CFG / "telegram-token").read_text().strip()
ALLOW = json.loads((CFG / "telegram.json").read_text())
ENV = {**os.environ, "HIVE_URL": "http://127.0.0.1:8470", "HIVE_ID": "1-hive",
       "HIVE_KEY_FILE": str(CFG / "telegram-bridge.key"), "HIVE_VIA": "telegram",
       "PATH": f"{Path.home()}/.local/bin:{os.environ.get('PATH', '')}"}


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


def tg(method: str, **params) -> dict:
    data = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v
                                   for k, v in params.items()}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    if not out.get("ok"):
        raise RuntimeError(f"telegram {method}: {out}")
    return out["result"]


def hive(*args: str) -> tuple[bool, str]:
    r = subprocess.run(["hive", *args], capture_output=True, text=True, env=ENV)
    return r.returncode == 0, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"offset": 0, "notified": [], "pending_reason": {}}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    st["notified"] = st["notified"][-500:]
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)


def goal_text(gid: str) -> str:
    ok, out = hive("goal", gid)
    if not ok:
        return ""
    g = json.loads(out)
    g = g.get("goal", g)
    parts = [f"<b>{esc(g.get('title', gid))}</b>"]
    if g.get("objective"):
        parts.append(esc(g["objective"]))
    if g.get("relevance"):
        parts.append(f"<i>Useless if:</i> {esc(g['relevance'])}")
    return "\n\n".join(parts)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def buttons(item: dict) -> list:
    k, gid = item["kind"], item["id"]
    if k == "goal_proposed":
        return [[{"text": "✅ Approve", "callback_data": f"approve:{gid}"},
                 {"text": "🗑 Abandon", "callback_data": f"abandon:{gid}"}]]
    if k == "goal_completed":
        return [[{"text": "✅ Accept", "callback_data": f"accept:{gid}"},
                 {"text": "↩ Reopen", "callback_data": f"reopen:{gid}"}]]
    return []


HEAD = {"goal_proposed": "🆕 Goal to approve", "goal_completed": "🏁 Goal completed: accept or reopen",
        "escalation_open": "⚠️ Escalation", "goal_abandoned_open_tasks": "🧹 Abandoned goal with open tasks",
        "exception_pending": "⚖️ Exception request", "proposal_pending": "📝 Proposal to decide"}


def push_inbox(st: dict) -> None:
    ok, out = hive("inbox", "--for", "operator", "--json")
    if not ok:
        log(f"inbox failed: {out}")
        return
    for item in json.loads(out):
        if item["key"] in st["notified"]:
            continue
        text = f"{HEAD.get(item['kind'], item['kind'])}\n\n"
        text += goal_text(item["id"]) if item["entity"] == "goal" else f"<b>{esc(item['title'])}</b>"
        text += f"\n\n<code>{item['entity']} {item['id']} · @{item['position']}</code>"
        kb = buttons(item)
        tg("sendMessage", chat_id=ALLOW["chat_id"], text=text, parse_mode="HTML",
           **({"reply_markup": {"inline_keyboard": kb}} if kb else {}))
        st["notified"].append(item["key"])
        save_state(st)
        log(f"notified {item['key']}")


def emit(etype: str, gid: str, data: dict | None = None) -> tuple[bool, str]:
    ok, out = hive("emit", etype, "--goal", gid, "--data", json.dumps(data or {}))
    if ok:
        return True, f"{etype} at position {json.loads(out)['position']}"
    return False, out.splitlines()[-1][:300] if out else "refused"


def allowed(frm: dict, chat: dict) -> bool:
    return frm.get("id") == ALLOW["user_id"] and chat.get("id") == ALLOW["chat_id"]


def on_callback(cq: dict, st: dict) -> None:
    msg = cq.get("message") or {}
    if not allowed(cq.get("from") or {}, msg.get("chat") or {}):
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="not allowed")
        log(f"refused callback from {cq.get('from', {}).get('id')}")
        return
    action, _, gid = (cq.get("data") or "").partition(":")
    if action in ("reopen", "abandon"):
        p = tg("sendMessage", chat_id=ALLOW["chat_id"], text=f"Reason to {action} <code>{esc(gid)}</code>? Reply to this message.",
               parse_mode="HTML", reply_markup={"force_reply": True})
        st["pending_reason"][str(p["message_id"])] = [action, gid]
        save_state(st)
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        return
    etype = {"approve": "goal.approved", "accept": "goal.accepted"}.get(action)
    if not etype:
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="unknown action")
        return
    ok, info = emit(etype, gid)
    tg("answerCallbackQuery", callback_query_id=cq["id"], text=("done" if ok else "refused"))
    status = f"\n\n{'✅' if ok else '❌'} {esc(info)}"
    try:
        tg("editMessageText", chat_id=ALLOW["chat_id"], message_id=msg["message_id"],
           text=(msg.get("text") or "") + status)
    except RuntimeError:
        tg("sendMessage", chat_id=ALLOW["chat_id"], text=status.strip())
    log(f"{action} {gid}: {info}")


def on_message(m: dict, st: dict, chat_cmd: str | None) -> None:
    if not allowed(m.get("from") or {}, m.get("chat") or {}):
        log(f"ignored message from {m.get('from', {}).get('id')}")
        return
    text = (m.get("text") or "").strip()
    reply_to = str((m.get("reply_to_message") or {}).get("message_id", ""))
    if reply_to in st["pending_reason"]:
        action, gid = st["pending_reason"].pop(reply_to)
        save_state(st)
        etype = {"reopen": "goal.reopened", "abandon": "goal.abandoned"}[action]
        ok, info = emit(etype, gid, {"reason": text[:500]})
        tg("sendMessage", chat_id=ALLOW["chat_id"], text=f"{'✅' if ok else '❌'} {info}")
        log(f"{action} {gid}: {info}")
        return
    if text in ("/start", "/help"):
        tg("sendMessage", chat_id=ALLOW["chat_id"],
           text="1-hive: I'll message you when a goal needs approval or acceptance, or something escalates. "
                "Use the buttons to decide. /inbox lists what's waiting.")
        return
    if text == "/inbox":
        st["notified"] = []          # re-send everything that's still waiting
        push_inbox(st)
        return
    if chat_cmd:
        r = subprocess.run(chat_cmd, shell=True, input=text, capture_output=True, text=True, timeout=900)
        reply = (r.stdout or r.stderr or "(no reply)").strip()
        for i in range(0, len(reply), 3900):
            tg("sendMessage", chat_id=ALLOW["chat_id"], text=reply[i:i + 3900])
    else:
        tg("sendMessage", chat_id=ALLOW["chat_id"],
           text="Chat with the chief of staff isn't connected yet; buttons and /inbox work.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--poll", type=int, default=30, help="long-poll seconds")
    ap.add_argument("--inbox-every", type=int, default=60)
    ap.add_argument("--chat-cmd", default=os.environ.get("TELEGRAM_CHAT_CMD") or None,
                    help="command that reads a message on stdin and prints the reply (default: $TELEGRAM_CHAT_CMD)")
    a = ap.parse_args()
    st = load_state()
    log("bridge up")
    next_inbox = 0.0
    while True:
        try:
            if time.time() >= next_inbox:
                push_inbox(st)
                next_inbox = time.time() + a.inbox_every
            for u in tg("getUpdates", offset=st["offset"], timeout=a.poll, allowed_updates=["message", "callback_query"]):
                st["offset"] = u["update_id"] + 1
                save_state(st)
                if "callback_query" in u:
                    on_callback(u["callback_query"], st)
                elif "message" in u:
                    on_message(u["message"], st, a.chat_cmd)
        except Exception as e:   # keep the bridge up through network or record errors
            log(f"error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
