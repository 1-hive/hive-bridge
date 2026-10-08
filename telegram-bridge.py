#!/usr/bin/env python3
"""1-hive Telegram bridge: the operator's inbox on their phone, with buttons.

Deterministic, no model. It:
- pushes each new operator inbox item (goal to approve or accept, escalation,
  pending exception) with buttons, and again as a reminder while it is still
  waiting after --remind-hours (so an old request doesn't get buried);
- turns button taps into signed record events: goal.approved, goal.accepted,
  goal.reopened (asks for the reason as a reply) and goal.abandoned;
- accepts taps and replies only from the configured chat and user;
- sends the digest (PLAN D15) once a day at --digest-at, local time: per goal, its
  status, its tasks and anything open, plus what changed since the last digest.
  Every digest names the next one's time: a digest that doesn't arrive is the alarm
  that the bridge or the record is down. /digest sends one now.

It signs with its own operator key (~/.config/hive/telegram-bridge.key), registered
on the operator actor with actor.key_added, and marks events `via: telegram`.
It never reads the operator's main key.

Free text that isn't a reply to a reason prompt is handed to `--chat-cmd`, if
given (the chief of staff; 1-hive PLAN D15); otherwise it gets a short note. Chat
runs on its own thread, one message at a time, so a slow reply never holds up
buttons, the inbox or the digest.

Each goal.approved (a tap here or `op approve`) is passed on to the chief of
staff as a chat message, so it starts the goal without waiting to be asked; its
one-line reply comes back here.

A tap is acknowledged at once (Telegram drops an answer that comes too late, and
the user taps again); a second tap on a message already decided does nothing.

    telegram-bridge.py [--poll 30] [--inbox-every 60] [--remind-hours 12] [--digest-at 08:00] [--chat-cmd CMD]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import queue
import sys
import threading
import time
import urllib.error
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
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:   # Telegram explains 4xx in the body
        try:
            out = json.loads(e.read())
        except ValueError:
            out = {"description": str(e)}
    if not out.get("ok"):
        raise RuntimeError(f"telegram {method}: {out.get('description', out)}")
    return out["result"]


def safe(method: str, **params) -> dict | None:
    """A cosmetic Telegram call: log a failure, never let it hide a decision."""
    try:
        return tg(method, **params)
    except Exception as e:
        log(f"{e}")
        return None


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
        # The goal's relevance line: how the work could pass every check and still miss the point.
        parts.append(f"<i>Doesn't count, even if every check passes, when:</i> {esc(g['relevance'])}")
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


REMIND_S = 12 * 3600


def push_inbox(st: dict) -> None:
    ok, out = hive("inbox", "--for", "operator", "--json")
    if not ok:
        log(f"inbox failed: {out}")
        return
    sent_at = st.setdefault("notified_at", {})
    now = time.time()
    for item in json.loads(out):
        # Sent before (and recently): skip. Sent long ago, or before sent_at existed: remind.
        if item["key"] in st["notified"] and now - sent_at.get(item["key"], 0) < REMIND_S:
            continue
        reminder = item["key"] in st["notified"]
        text = ("⏰ Still waiting for you.\n\n" if reminder else "") + f"{HEAD.get(item['kind'], item['kind'])}\n\n"
        text += goal_text(item["id"]) if item["entity"] == "goal" else f"<b>{esc(item['title'])}</b>"
        text += f"\n\n<code>{item['entity']} {item['id']} · @{item['position']}</code>"
        kb = buttons(item)
        tg("sendMessage", chat_id=ALLOW["chat_id"], text=text, parse_mode="HTML",
           **({"reply_markup": {"inline_keyboard": kb}} if kb else {}))
        if not reminder:
            st["notified"].append(item["key"])
        sent_at[item["key"]] = now
        save_state(st)
        log(f"{'reminded' if reminder else 'notified'} {item['key']}")


DIGEST_AT = (8, 0)
OPEN = ("assigned", "in_progress", "blocked", "in_review", "created")


def next_digest(after: float) -> dt.datetime:
    """The first digest time (local) strictly after the epoch time `after`."""
    t = dt.datetime.fromtimestamp(after).astimezone()
    d = t.replace(hour=DIGEST_AT[0], minute=DIGEST_AT[1], second=0, microsecond=0)
    return d if d > t else d + dt.timedelta(days=1)


def digest_text(st: dict) -> tuple[str, int] | None:
    """The digest, deterministic from the record: (text, head position)."""
    ok, out = hive("state")
    if not ok:
        return None
    state = json.loads(out)
    since = st.get("digest_pos", 0)
    ok, out = hive("events", "--after", str(since))
    events = [json.loads(line) for line in out.splitlines() if line.strip()] if ok else []
    head = max([e["position"] for e in events], default=since)
    # The first digest lists only goals in progress, not every goal since the hive began.
    changed = {e.get("goal") or (state["tasks"].get(e.get("task") or "") or {}).get("goal")
               for e in events} if "digest_pos" in st else set()
    tasks_by_goal: dict = {}
    for t in state.get("tasks", {}).values():
        tasks_by_goal.setdefault(t.get("goal"), []).append(t)
    lines = [f"📊 <b>1-hive digest</b> · {dt.datetime.now().astimezone():%a %d %b %H:%M}",
             f"{len(events)} events since the last digest (now at position {head})."]
    shown = 0
    for g in sorted(state.get("goals", {}).values(), key=lambda g: g.get("proposed_at") or ""):
        live = g["status"] in ("proposed", "active", "completed")
        if not live and g["id"] not in changed:
            continue
        shown += 1
        ts_ = tasks_by_goal.get(g["id"], [])
        open_ = [t for t in ts_ if t["status"] in OPEN]
        line = f"\n<b>{esc(g['title'])}</b> (<code>{g['id']}</code>): {g['status']}"
        line += f"; tasks {len(ts_) - len(open_)} done, {len(open_)} open"
        for t in open_:
            line += f"\n  · <code>{t['id']}</code> {t['status']}"
            if (t.get("ext") or {}).get("escalation"):
                line += f", escalated to {t['ext']['escalation'].get('to')}"
        if g.get("escalation"):
            line += f"\n  ⚠️ escalated to {g['escalation'].get('to')}: {esc(g['escalation'].get('reason', ''))}"
        if g.get("budget"):
            line += f"\n  budget {g['budget']}, spent {g.get('spent')}"
        lines.append(line)
    if not shown:
        lines.append("\nNo goals in progress, and none changed.")
    nxt = next_digest(time.time())
    lines.append(f"\nNext digest: {nxt:%a %d %b %H:%M}. If none arrives by then, the bridge or the record is down.")
    return "\n".join(lines), head


def send_digest(st: dict) -> None:
    d = digest_text(st)
    if d is None:
        log("digest: record unreachable")
        return
    text, head = d
    tg("sendMessage", chat_id=ALLOW["chat_id"], text=text, parse_mode="HTML")
    st["digest_pos"], st["digest_at"] = head, time.time()
    save_state(st)
    log(f"digest sent (position {head})")


def emit(etype: str, gid: str, data: dict | None = None) -> tuple[bool, str]:
    ok, out = hive("emit", etype, "--goal", gid, "--data", json.dumps(data or {}))
    if ok:
        return True, f"{etype} at position {json.loads(out)['position']}"
    try:   # a refusal comes back as JSON with a code and a reason
        d = json.loads(out)
        d = d.get("refusal", {}).get("data", d) if isinstance(d, dict) else {}
        return False, f"refused: {d.get('code', '?')}: {d.get('reason', out)[:250]}"
    except ValueError:
        return False, (out.splitlines()[-1][:300] if out else "refused")


def allowed(frm: dict, chat: dict) -> bool:
    return frm.get("id") == ALLOW["user_id"] and chat.get("id") == ALLOW["chat_id"]


def on_callback(cq: dict, st: dict) -> None:
    msg = cq.get("message") or {}
    if not allowed(cq.get("from") or {}, msg.get("chat") or {}):
        safe("answerCallbackQuery", callback_query_id=cq["id"], text="not allowed")
        log(f"refused callback from {cq.get('from', {}).get('id')}")
        return
    action, _, gid = (cq.get("data") or "").partition(":")
    key = f"{msg.get('message_id')}:{action}"
    if key in st.setdefault("decided", []):
        safe("answerCallbackQuery", callback_query_id=cq["id"], text="already done")
        return
    if action in ("reopen", "abandon"):
        p = tg("sendMessage", chat_id=ALLOW["chat_id"], text=f"Reason to {action} <code>{esc(gid)}</code>? Reply to this message.",
               parse_mode="HTML", reply_markup={"force_reply": True})
        st["pending_reason"][str(p["message_id"])] = [action, gid]
        save_state(st)
        safe("answerCallbackQuery", callback_query_id=cq["id"])
        return
    etype = {"approve": "goal.approved", "accept": "goal.accepted"}.get(action)
    if not etype:
        safe("answerCallbackQuery", callback_query_id=cq["id"], text="unknown action")
        return
    safe("answerCallbackQuery", callback_query_id=cq["id"])
    ok, info = emit(etype, gid)
    log(f"{action} {gid}: {info}")
    st["decided"] = (st["decided"] + [key])[-200:]
    save_state(st)
    status = f"\n\n{'✅' if ok else '❌'} {info}"
    # The decision is recorded either way: show it, and drop the buttons so it can't be tapped twice.
    if safe("editMessageText", chat_id=ALLOW["chat_id"], message_id=msg["message_id"],
            text=(msg.get("text") or "") + status) is None:
        safe("editMessageReplyMarkup", chat_id=ALLOW["chat_id"], message_id=msg["message_id"],
             reply_markup={"inline_keyboard": []})
        safe("sendMessage", chat_id=ALLOW["chat_id"], text=status.strip())


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
           text="1-hive: I'll message you when a goal needs approval or acceptance, or something escalates, "
                "and send a digest every day. Use the buttons to decide. /inbox lists what's waiting; "
                "/digest sends the digest now; /board shows what each agent is doing.")
        return
    if text == "/board":
        safe("sendMessage", chat_id=ALLOW["chat_id"], text=board_text(), parse_mode="HTML")
        return
    if text == "/digest":
        send_digest(st)
        return
    if text == "/inbox":
        st["notified"] = []          # re-send everything that's still waiting
        push_inbox(st)
        return
    if chat_cmd:
        if CHAT.unfinished_tasks:
            safe("sendMessage", chat_id=ALLOW["chat_id"], text="(queued: the chief of staff is still on your last message)")
        CHAT.put((chat_cmd, text))
    else:
        tg("sendMessage", chat_id=ALLOW["chat_id"],
           text="Chat with the chief of staff isn't connected yet; buttons and /inbox work.")


FLOOR = os.environ.get("FLOOR_URL", "http://127.0.0.1:8476")


def ago(s: float) -> str:
    s = int(s)
    return f"{s // 60}m" if s < 5400 else f"{s // 3600}h {s % 3600 // 60}m"


def board_text() -> str:
    """Open goals and tasks, with what each agent is doing now (from the Floor, tools/floor.py)."""
    try:
        with urllib.request.urlopen(f"{FLOOR}/api/state", timeout=10) as r:
            st = json.load(r)
        with urllib.request.urlopen(f"{FLOOR}/api/live", timeout=10) as r:
            live = json.load(r)["live"]
    except Exception as e:
        return f"The Floor isn't answering ({esc(str(e))[:120]}). Check: systemctl --user status 1-hive-floor"
    lines = []
    for g in st["goals"].values():
        if g["status"] not in ("proposed", "active", "completed"):
            continue
        head = f"<b>{esc(g['title'])}</b> · {g['status']}"
        wall = (g.get("budget") or {}).get("wall_clock_seconds")
        if wall and g.get("approved_at") and g["status"] == "active":
            used = time.time() - dt.datetime.fromisoformat(g["approved_at"].replace("Z", "+00:00")).timestamp()
            head += f" · {ago(used)} of {ago(wall)}"
        lines.append(head)
        for t in st["tasks"].values():
            if t["goal"] != g["id"] or t["status"] in ("done", "released"):
                continue
            L = live.get(t["id"]) or {}
            line = f"  <code>{esc(t['id'])}</code> {t['status'].replace('_', ' ')}"
            if L.get("waiting_reviewer"):
                line += f" · waiting for {esc(t.get('reviewer') or 'a reviewer')}"
                if L.get("since_s") is not None:
                    line += f", {ago(L['since_s'])}"
            elif L.get("finished"):
                line += f" · {L.get('role', 'agent')} finished {ago(L['idle_s'])} ago"
            elif L:
                call = (L.get("calls") or [[None, "", ""]])[-1]
                line += f" · {esc(call[1])} {esc(call[2])[:60]}"
                line += " · active now" if L["idle_s"] < 60 else f" · quiet {ago(L['idle_s'])}"
            lines.append(line)
    return "\n".join(lines) or "Nothing open."


CHAT: queue.Queue = queue.Queue()


PINGS = {   # record event -> (state cursor, message to the chief of staff, or None to skip it)
    "goal.approved": ("approved_pos", lambda e: f"Goal {e['goal']} was approved (position {e['position']}). "
                      "Start it now: orders, tasks, assignments. Reply in one line with what you created."),
    "task.escalated": ("escalated_pos", lambda e: None if (e.get("data") or {}).get("to") != "chief_of_staff" else
                       f"Task {e['task']} was escalated to you (position {e['position']}, "
                       f"{e['data'].get('code')}): {e['data'].get('reason', '')}. Handle it as your doc says "
                       "and resolve it on the record. Reply in one line with what you did, or what the human must decide."),
}


def ping_cos(st: dict, chat_cmd: str) -> None:
    """Tell the chief of staff about each goal approved, and each escalation to it, since the last
    check: nothing else wakes it."""
    for etype, (cursor, text) in PINGS.items():
        first = cursor not in st   # first run: skip past events, don't replay them
        ok, out = hive("events", "--after", str(st.get(cursor, 0)), "--type", etype)
        if not ok:
            continue
        evs = [json.loads(x) for x in out.splitlines() if x.strip()]
        if first:
            st[cursor] = max([e["position"] for e in evs], default=0)
            continue
        for e in evs:
            st[cursor] = max(st[cursor], e["position"])
            msg = text(e)
            if msg:
                CHAT.put((chat_cmd, msg))
                log(f"pinged cos: {etype} {e.get('goal') or e.get('task')}")
    save_state(st)


def chat_worker() -> None:
    """Chat with the chief of staff, one message at a time, off the main loop."""
    while True:
        cmd, text = CHAT.get()
        try:
            safe("sendChatAction", chat_id=ALLOW["chat_id"], action="typing")
            r = subprocess.run(cmd, shell=True, input=text, capture_output=True, text=True, timeout=960)
            reply = (r.stdout or r.stderr or "(no reply)").strip()
        except subprocess.TimeoutExpired:
            reply = "(the chief of staff didn't answer in 16 minutes; try again)"
        except Exception as e:
            reply = f"(chat failed: {e})"
        log(f"chat: {len(text)} chars in, {len(reply)} out")
        for i in range(0, len(reply), 3900):
            safe("sendMessage", chat_id=ALLOW["chat_id"], text=reply[i:i + 3900])
        CHAT.task_done()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--poll", type=int, default=30, help="long-poll seconds")
    ap.add_argument("--inbox-every", type=int, default=60)
    ap.add_argument("--remind-hours", type=float, default=12, help="re-send a request still waiting after this long")
    ap.add_argument("--digest-at", default="08:00", help="daily digest time, HH:MM local")
    ap.add_argument("--chat-cmd", default=os.environ.get("TELEGRAM_CHAT_CMD") or None,
                    help="command that reads a message on stdin and prints the reply (default: $TELEGRAM_CHAT_CMD)")
    a = ap.parse_args()
    global REMIND_S, DIGEST_AT
    REMIND_S = a.remind_hours * 3600
    DIGEST_AT = tuple(int(x) for x in a.digest_at.split(":"))
    st = load_state()
    threading.Thread(target=chat_worker, daemon=True).start()
    log("bridge up")
    next_inbox = 0.0
    while True:
        try:
            # The first run sends one now; afterwards, at each digest time.
            if time.time() >= next_digest(st.get("digest_at", 0)).timestamp():
                send_digest(st)
            if time.time() >= next_inbox:
                push_inbox(st)
                if a.chat_cmd:
                    ping_cos(st, a.chat_cmd)
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
