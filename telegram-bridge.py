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

Each goal.approved (a tap here or `op approve`), escalation to the chief of staff,
blocked task (a worker's question) and passed review is passed on to it as a chat
message, so it acts without waiting to be asked; its one-line reply comes back here.
A reply that says nothing ("No response requested.") isn't forwarded.

Follow-ups the operator asks for ("check in 10 minutes and tell me"): the chief of
staff writes a file into its folder `followups/` while answering the operator, and
the bridge wakes it at that time and forwards the reply. Only a file written within
16 minutes of an operator message counts (so the chief of staff can't start pushes
on its own), at most 2 hours ahead and 3 pending (1-hive PLAN D15, 2026-10-14).

A chat reply may carry attachments: a line of its own `[[photo: <path>]]` or `[[file: <path>]]`,
the path inside the chief of staff's workspace clone (relative, or under /cos/workspace). The
line is taken out of the text and the file sent with sendPhoto or sendDocument: at most 5 per
reply, 10 MB each, never a file outside that clone (symlinks resolved), only listed types.

The digest lists results waiting more than an hour for a verdict, with what the review is doing.

An over-budget alert carries a "+4h" button: it raises the goal's wall-clock budget
to 4 hours from now (`op budget` does the same) and resolves the alert.

When a task is done (closed or cancelled), a one-line note is pushed, without buttons
(`--no-feed` turns them off). A completed goal already arrives as an inbox item.

A tap is acknowledged at once (Telegram drops an answer that comes too late, and
the user taps again); a second tap on a message already decided does nothing.

    telegram-bridge.py [--poll 30] [--inbox-every 60] [--remind-hours 12] [--digest-at 08:00] [--chat-cmd CMD]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import subprocess
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import hive_paths as hp

CFG = hp.CONFIG_DIR
STATE = hp.WORK_DIR / ".telegram" / "state.json"
TOKEN = (CFG / "telegram-token").read_text().strip()
ALLOW = json.loads((CFG / "telegram.json").read_text())
# Group chats (hive-bridge docs/design.md), optional: without this file the bridge serves only the
# operator's private chat (ALLOW), as before. With it, allowed humans and known bots in group chats:
# {"chats": [{"name", "chat_id", "private": false, "mode": "all" | "mention"}],
#  "humans": [{"name", "user_id", "permissions": ["operator"]}], "bots": [{"name", "user_id", "username", "actor"}]}
# mode "all" (the work group): every human message goes to the chief of staff. mode "mention" (a conversation
# group): only messages that @-mention the bridge's bot or reply to it, from humans and from known bots.
# Every message seen, in every listed chat, goes to the dialogue log (.telegram/dialogue.jsonl).
_GROUPS_FILE = CFG / "telegram-groups.json"
GROUPS = json.loads(_GROUPS_FILE.read_text()) if _GROUPS_FILE.is_file() else {}
CHATS = {c["chat_id"]: c for c in GROUPS.get("chats", [])}
HUMANS = {h["user_id"]: h for h in GROUPS.get("humans", [])}
BOTS = {b["user_id"]: b for b in GROUPS.get("bots", [])}
BOT_NAMES = {b.get("username", "").lower(): b for b in GROUPS.get("bots", []) if b.get("username")}
THREAD_KEEP = 15        # recent messages per group chat, kept as context for the chief of staff
ME: dict = {}           # the bridge's own bot (getMe), for mention mode
DIALOGUE = STATE.parent / "dialogue.jsonl"   # every message the bridge sees, for the record later (R7)
ENV = hp.tool_env(CFG / "telegram-bridge.key", "telegram")


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


def budget_kb(item: dict) -> list:
    if item["kind"] == "escalation_open" and item["entity"] == "goal" and item["title"].startswith("budget_exceeded"):
        return [[{"text": "⏱ +4h", "callback_data": f"budget:{item['id']}:4"}]]
    return []


HEAD = {"goal_proposed": "🆕 Goal to approve", "goal_completed": "🏁 Goal completed: accept or reopen",
        "escalation_open": "⚠️ Escalation", "goal_abandoned_open_tasks": "🧹 Abandoned goal with open tasks",
        "exception_pending": "⚖️ Exception request", "proposal_pending": "📝 Proposal to decide",
        "task_blocked_unanswered_any": "❓ A worker's question has waited 30+ min (the chief of staff hasn't answered)"}


REMIND_S = 12 * 3600
# A worker's question goes to the chief of staff only; nothing told the operator when it sat (cust-1-coordinator
# waited 16 h on 2026-10-13). Past QUESTION_WAIT_S it comes here too, every QUESTION_REMIND_S until answered.
QUESTION_WAIT_S, QUESTION_REMIND_S = 30 * 60, 2 * 3600


def waiting_questions(st: dict, now: float) -> list:
    ok, out = hive("inbox", "--for", "chief_of_staff", "--json")
    if not ok:
        log(f"chief of staff inbox failed: {out}")
        return []
    items = [i for i in json.loads(out) if i["kind"] == "task_blocked_unanswered_any"]
    seen = st.setdefault("question_seen", {})
    for k in [k for k in seen if k not in {i["key"] for i in items}]:
        del seen[k]                  # answered: forget it
    for i in items:
        seen.setdefault(i["key"], now)
    return [i for i in items if now - seen[i["key"]] >= QUESTION_WAIT_S]


def question_text(tid: str) -> str:
    ok, out = hive("task", tid)
    if not ok:
        return ""
    t = json.loads(out)
    b = t.get("task", t).get("block") or {}
    return f"\n\n<i>Needs {esc(b.get('needs', '?'))}:</i> {esc(b['reason'])}" if b.get("reason") else ""


def push_inbox(st: dict) -> None:
    ok, out = hive("inbox", "--for", "operator", "--json")
    if not ok:
        log(f"inbox failed: {out}")
        return
    sent_at = st.setdefault("notified_at", {})
    now = time.time()
    for item in json.loads(out) + waiting_questions(st, now):
        question = item["kind"] == "task_blocked_unanswered_any"
        # Sent before (and recently): skip. Sent long ago, or before sent_at existed: remind.
        if item["key"] in st["notified"] and now - sent_at.get(item["key"], 0) < (QUESTION_REMIND_S if question else REMIND_S):
            continue
        reminder = item["key"] in st["notified"]
        text = ("⏰ Still waiting for you.\n\n" if reminder else "") + f"{HEAD.get(item['kind'], item['kind'])}\n\n"
        text += goal_text(item["id"]) if item["entity"] == "goal" else f"<b>{esc(item['title'])}</b>"
        if question:
            text += question_text(item["id"])
        text += f"\n\n<code>{item['entity']} {item['id']} · @{item['position']}</code>"
        kb = buttons(item) or budget_kb(item)
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


def sweeps_since(t: float) -> tuple[int, int] | None:
    """Task folders the sweeper removed since time t (tools/sweeper.py): (count, bytes)."""
    f = hp.WORK_DIR / ".sweeper" / "log.jsonl"
    rows = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
    rows = [r for r in rows if dt.datetime.fromisoformat(r["at"].replace("Z", "+00:00")).timestamp() > t]
    return (len(rows), sum(r["bytes"] for r in rows)) if rows else None


WORKSPACE = hp.WORKSPACE_REPO
SLOW_REVIEW_S = 3600


def slow_reviews(state: dict) -> list[str]:
    """Tasks in review whose current result has waited over SLOW_REVIEW_S for a verdict, each with
    what its review is doing (from the Floor; tools/floor.py)."""
    try:
        with urllib.request.urlopen(f"{FLOOR}/api/live", timeout=10) as r:
            live = json.load(r)["live"]
    except Exception:
        live = None
    out = []
    for t in state.get("tasks", {}).values():
        cur = t.get("current_result") or {}
        if t["status"] != "in_review" or not cur.get("at"):
            continue
        if (t.get("latest_review") or {}).get("position", 0) > cur.get("position", 0):
            continue
        waited = time.time() - dt.datetime.fromisoformat(cur["at"].replace("Z", "+00:00")).timestamp()
        if waited < SLOW_REVIEW_S:
            continue
        L = (live or {}).get(t["id"]) or {}
        doing = ("status unknown (the Floor isn't answering)" if live is None
                 else f"waiting for {t.get('assigned_reviewer') or 'a reviewer'} to start" if L.get("waiting_reviewer")
                 else "the reviewer finished without a verdict" if L.get("finished")
                 else f"the reviewer is working (quiet {ago(L['idle_s'])})" if L
                 else "no reviewer running")
        out.append(f"<code>{esc(t['id'])}</code>: {ago(waited)}, {esc(doing)}")
    return out


def open_requests() -> list[str]:
    """The chief of staff's requests for hive changes, on the workspace's main: hive-requests/*.md
    not yet moved into hive-requests/done/."""
    r = subprocess.run(["git", "-C", str(WORKSPACE), "ls-tree", "--name-only", "main", "hive-requests/"],
                       capture_output=True, text=True)
    return [Path(x).stem for x in r.stdout.split() if x.endswith(".md")] if r.returncode == 0 else []


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
    lines = [f"📊 <b>{hp.HIVE_ID} digest</b> · {dt.datetime.now().astimezone():%a %d %b %H:%M}",
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
    swept = sweeps_since(st.get("digest_at", 0))
    if swept:
        lines.append(f"\nDisk: freed {swept[1] / 1e9:.1f} GB from {swept[0]} task folders.")
    slow = slow_reviews(state)
    if slow:
        lines.append("\nWaiting over an hour for a review verdict:" + "".join(f"\n  · {x}" for x in slow))
    reqs = open_requests()
    if reqs:
        lines.append(f"\nOpen requests from the chief of staff (hive-requests/): {len(reqs)}"
                     + "".join(f"\n  · {esc(r)}" for r in reqs))
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


def extend_budget(gid: str, hours: float, via: str) -> tuple[bool, str]:
    """Set the goal's wall-clock budget to `hours` from now, keeping its other dimensions,
    and resolve an open over-budget alert (the same as `op budget`)."""
    ok, out = hive("state")
    if not ok:
        return False, "record unreachable"
    g = json.loads(out)["goals"].get(gid)
    if not g or not g.get("approved_at"):
        return False, f"{gid}: no such approved goal"
    used = time.time() - dt.datetime.fromisoformat(g["approved_at"].replace("Z", "+00:00")).timestamp()
    budget = {**(g.get("budget") or {}), "wall_clock_seconds": int(used + hours * 3600)}
    ok, info = emit("goal.revised", gid, {"reason": f"{hours:g} more hours (operator, via {via})", "budget": budget})
    if ok and (g.get("escalation") or {}).get("code") == "budget_exceeded":
        ok, more = emit("goal.escalation_resolved", gid, {"resolution": f"Budget raised: {hours:g} more hours"})
        info += f"; {more}"
    return ok, info


def is_operator(user_id) -> bool:
    return user_id == ALLOW["user_id"] or "operator" in HUMANS.get(user_id, {}).get("permissions", [])


def allowed(frm: dict, chat: dict) -> bool:
    """The operator chat, from a human with the operator permission: buttons and commands."""
    return chat.get("id") == ALLOW["chat_id"] and is_operator(frm.get("id"))


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
    if action == "budget":
        gid, _, hours = gid.rpartition(":")
        safe("answerCallbackQuery", callback_query_id=cq["id"])
        ok, info = extend_budget(gid, float(hours), "telegram")
        log(f"budget {gid} +{hours}h: {info}")
        st["decided"] = (st["decided"] + [key])[-200:]
        save_state(st)
        safe("editMessageReplyMarkup", chat_id=ALLOW["chat_id"], message_id=msg["message_id"],
             reply_markup={"inline_keyboard": []})
        safe("sendMessage", chat_id=ALLOW["chat_id"], text=f"{'✅' if ok else '❌'} {info}")
        return
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


def sender_name(frm: dict) -> str:
    uid = frm.get("id")
    if uid in HUMANS:
        return HUMANS[uid]["name"]
    if uid in BOTS:
        return BOTS[uid]["name"]
    return frm.get("username") or frm.get("first_name") or str(uid)


def mentions(m: dict) -> list[str]:
    text, out = m.get("text") or m.get("caption") or "", []
    for e in m.get("entities") or m.get("caption_entities") or []:
        if e.get("type") == "mention":
            u = text[e["offset"] + 1:e["offset"] + e["length"]].lower()
            out.append(BOT_NAMES[u]["name"] if u in BOT_NAMES else u)
        elif e.get("type") == "text_mention":
            out.append(sender_name(e.get("user") or {}))
    return out


def remember(st: dict, m: dict) -> None:
    """Keep the group's recent messages, humans' and bots', as thread context, and log every one."""
    th = st.setdefault("threads", {}).setdefault(str(m["chat"]["id"]), [])
    rt = m.get("reply_to_message") or {}
    entry = {"id": m["message_id"], "from": sender_name(m.get("from") or {}),
             "text": (m.get("text") or m.get("caption") or "")[:500], **({"reply_to": rt["message_id"]} if rt else {})}
    th.append(entry)
    del th[:-THREAD_KEEP]
    try:
        with DIALOGUE.open("a") as f:
            f.write(json.dumps({"chat": m["chat"]["id"], "chat_name": CHATS.get(m["chat"]["id"], {}).get("name"),
                                "at": dt.datetime.fromtimestamp(m.get("date", 0), dt.UTC).isoformat(),
                                "from_id": (m.get("from") or {}).get("id"), "bot": bool((m.get("from") or {}).get("is_bot")),
                                **entry, "text": (m.get("text") or m.get("caption") or "")}) + "\n")
    except OSError as e:
        log(f"dialogue log: {e}")


def addressed_to_me(m: dict) -> bool:
    """Mentions the bridge's bot, or replies to one of its messages."""
    me = ME.get("username", "").lower()
    rt = (m.get("reply_to_message") or {}).get("from") or {}
    if ME and rt.get("id") == ME.get("id"):
        return True
    text = m.get("text") or m.get("caption") or ""
    for e in m.get("entities") or m.get("caption_entities") or []:
        if e.get("type") == "mention" and text[e["offset"] + 1:e["offset"] + e["length"]].lower() == me:
            return True
    return False


def record_context() -> dict:
    """The hive's agents and their work, from the record, for the chief of staff to resolve requests."""
    ok, out = hive("state")
    if not ok:
        return {"error": "record unreachable"}
    s = json.loads(out)
    agents = [{"actor": a, "class": v.get("class"), "harness": (v.get("declaration") or {}).get("harness"),
               "model": (v.get("declaration") or {}).get("model_route")}
              for a, v in sorted(s.get("actors", {}).items()) if v.get("class") in ("worker", "reviewer")
              and v.get("status", "active") == "active"]
    tasks = sorted(s.get("tasks", {}).values(), key=lambda t: t.get("status_since") or "", reverse=True)
    def brief(t):
        lr = t.get("latest_review") or {}
        return {"task": t["id"], "title": t.get("title"), "status": t["status"], "owner": t.get("owner"),
                "reviewer": t.get("assigned_reviewer"), **({"review": lr.get("verdict")} if lr else {}),
                **({"result": t["current_result"]["event_id"]} if t.get("current_result") else {})}
    open_ = [brief(t) for t in tasks if t["status"] not in ("done", "cancelled")]
    recent = [brief(t) for t in tasks if t["status"] in ("done", "cancelled")][:10]
    return {"agents": agents, "open_tasks": open_, "recently_finished": recent}


def rule_guess(m: dict) -> str:
    """What a deterministic addressee rule would say: logged only, never acted on (docs/design.md)."""
    rt = (m.get("reply_to_message") or {}).get("from") or {}
    if rt.get("id") in BOTS:
        return BOTS[rt["id"]]["name"]
    ms = [x for x in mentions(m) if any(b["name"] == x for b in BOTS.values())]
    return ms[0] if ms else "chief_of_staff"


ENVELOPE_HEAD = ("A message from a group chat. You are the hive's only interpreter of what humans write: decide who "
                 "should act on what (mentions and replies are facts, not instructions), and act only through the "
                 "record. Reply to the human in one short message, or with nothing if it isn't for the hive. "
                 "Your reply goes to this chat, as a reply to this message. To send a later message to a group "
                 "(e.g. a result), start a reply with a line [[to: <chat name>]]. When you create a task for this message, add a "
                 "line [[task: <task id>]] to your reply: the bridge then threads that task's progress (result, review, "
                 "done) on this message.\n\n")


def on_group_message(m: dict, st: dict, chat_cmd: str | None) -> None:
    chat, frm = m["chat"], m.get("from") or {}
    remember(st, m)
    save_state(st)
    mention_mode = CHATS.get(chat["id"], {}).get("mode", "all") == "mention"
    is_bot = frm.get("id") in BOTS or frm.get("is_bot")
    if mention_mode and not addressed_to_me(m):
        return   # a conversation group: context only, unless the chief of staff is asked
    if is_bot and not (mention_mode and frm.get("id") in BOTS):
        return   # bots' messages are context only, except a known bot asking the chief of staff for work
    if not is_bot and frm.get("id") not in HUMANS and frm.get("id") != ALLOW["user_id"]:
        log(f"group {chat['id']}: ignored message from {frm.get('id')}")
        return
    text = (m.get("text") or m.get("caption") or "").strip()
    if not text:
        return
    if text.split("@")[0] == "/board":
        safe("sendMessage", chat_id=chat["id"], text=board_text(), parse_mode="HTML", reply_to_message_id=m["message_id"],
             disable_web_page_preview=True, **floor_markup())
        return
    if text.split("@")[0] == "/floor" and FLOOR_PUBLIC:
        safe("sendMessage", chat_id=chat["id"], text="The Floor opens from a private chat with me: tap below, then Start.",
             reply_to_message_id=m["message_id"], **floor_markup())
        return
    rt = m.get("reply_to_message") or {}
    env = {"chat": {"id": chat["id"], "name": CHATS.get(chat["id"], {}).get("name", chat.get("title")),
                    "kind": "group", "private": CHATS.get(chat["id"], {}).get("private", False)},
           "message": {"id": m["message_id"], "at": dt.datetime.fromtimestamp(m["date"], dt.UTC).isoformat(), "text": text},
           "from": ({"bot": sender_name(frm), "actor": BOTS.get(frm.get("id"), {}).get("actor")} if is_bot
                    else {"human": sender_name(frm), "permissions": (["operator"] if is_operator(frm.get("id")) else [])}),
           **({"reply_to": {"id": rt["message_id"], "from": sender_name(rt.get("from") or {}),
                            "text": (rt.get("text") or rt.get("caption") or "")[:1000]}} if rt else {}),
           "mentions": mentions(m),
           "thread": st["threads"][str(chat["id"])][:-1],
           "context": record_context()}
    with (STATE.parent / "addressee-rule.jsonl").open("a") as f:   # shadow evidence: the rule never acts
        f.write(json.dumps({"chat": chat["id"], "message": m["message_id"], "from": env["from"].get("human") or env["from"].get("bot"),
                            "rule": rule_guess(m), "mentions": env["mentions"],
                            "reply_to": env.get("reply_to", {}).get("from"), "at": env["message"]["at"]}) + "\n")
    if not chat_cmd:
        return
    if CHAT.unfinished_tasks:
        safe("sendMessage", chat_id=chat["id"], text="(queued: the chief of staff is on another message)",
             reply_to_message_id=m["message_id"])
    CHAT.put((chat_cmd, ENVELOPE_HEAD + json.dumps(env, ensure_ascii=False, indent=1), True,
              {"chat_id": chat["id"], "reply_to": m["message_id"]}))


def on_message(m: dict, st: dict, chat_cmd: str | None) -> None:
    if (m.get("chat") or {}).get("type") in ("group", "supergroup") and (m["chat"]["id"] in CHATS or m["chat"]["id"] == ALLOW["chat_id"]):
        on_group_message(m, st, chat_cmd)
        return
    text = (m.get("text") or "").strip()
    frm, chat = m.get("from") or {}, m.get("chat") or {}
    if chat.get("type") == "private" and is_operator(frm.get("id")) and text.split("@")[0] in ("/floor", "/start floor", "/board"):
        private_floor(chat["id"], frm["id"], text)
        return
    if not allowed(frm, chat):
        log(f"ignored message from {frm.get('id')}")
        return
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
           text=f"{hp.HIVE_ID}: I'll message you when a goal needs approval or acceptance, or something escalates, "
                "and send a digest every day. Use the buttons to decide. /inbox lists what's waiting; "
                "/digest sends the digest now; /board shows what each agent is doing"
                + ("; /floor opens the Floor." if FLOOR_PUBLIC else "."))
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
        CHAT.put((chat_cmd, text, True, None))
    else:
        tg("sendMessage", chat_id=ALLOW["chat_id"],
           text="Chat with the chief of staff isn't connected yet; buttons and /inbox work.")


FLOOR = os.environ.get("FLOOR_URL", "http://127.0.0.1:8476")


def ago(s: float) -> str:
    s = int(s)
    return f"{s // 60}m" if s < 5400 else f"{s // 3600}h {s % 3600 // 60}m"


FLOOR_PUBLIC = os.environ.get("FLOOR_PUBLIC_URL", "").rstrip("/")   # the Floor through its HTTPS proxy, if any
FLOOR_APP = os.environ.get("FLOOR_TG_APP", "")                      # a BotFather Mini App link, e.g. mybot/floor
BOARD_BOTS = os.environ.get("FLOOR_BOARD") == "bots"                 # /board and the pinned status: one line per agent


def floor_get(path: str) -> dict:
    with urllib.request.urlopen(f"{FLOOR}{path}", timeout=20) as r:
        return json.load(r)


def floor_markup() -> dict:
    """A button that opens the Floor: the Mini App link if BotFather has one, else a private chat with this bot
    (/start floor), where the Floor opens inside Telegram (web_app buttons work only in private chats)."""
    if not FLOOR_PUBLIC:
        return {}
    url = f"https://t.me/{FLOOR_APP}" if FLOOR_APP else (f"https://t.me/{ME['username']}?start=floor" if ME.get("username") else "")
    return {"reply_markup": {"inline_keyboard": [[{"text": "Open the Floor", "url": url}]]}} if url else {}


def login_link(user_id: int, minutes: int = 15) -> str:
    """The Floor's login link for a browser (floor.py checks it): valid `minutes`, then a 30-day cookie."""
    secret = (CFG / "floor-session.secret").read_text().strip().encode()
    exp = int(time.time()) + minutes * 60
    sig = hmac.new(secret, f"login.{user_id}.{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{FLOOR_PUBLIC}/login?u={user_id}&e={exp}&s={sig}"


def private_floor(chat_id: int, user_id: int, text: str) -> None:
    """An operator's private chat: /board, or the Floor inside Telegram plus a login link for a browser."""
    if text.split("@")[0] == "/board":
        safe("sendMessage", chat_id=chat_id, text=board_text(), parse_mode="HTML", disable_web_page_preview=True)
        return
    if not FLOOR_PUBLIC:
        safe("sendMessage", chat_id=chat_id, text="This hive's Floor isn't reachable from outside; /board shows what each agent is doing.")
        return
    try:
        link = login_link(user_id)
    except OSError as e:
        log(f"floor login link: {e}")
        link = None
    rows = [[{"text": "Open the Floor", "web_app": {"url": FLOOR_PUBLIC + "/"}}]]
    if link:
        rows.append([{"text": "Open in a browser (link valid 15 min)", "url": link}])
    safe("sendMessage", chat_id=chat_id, text="The Floor: what each agent is doing now, what it owes, and its day. "
         "The browser link signs you in for 30 days on that browser; don't forward it.",
         reply_markup={"inline_keyboard": rows})
    log(f"floor: links for {user_id}")


def push_status(st: dict) -> None:
    """The work group's status message (chats with "status": true): one message, pinned once, edited in place
    when the Floor's text changes, so it never notifies. A deleted message is sent again."""
    chats = [c for c in CHATS.values() if c.get("status")]
    if not chats or not BOARD_BOTS:
        return
    try:
        text = floor_get("/api/status")["text"]
    except Exception as e:
        if time.time() - st.get("status_err_at", 0) > 3600:
            log(f"status: the Floor isn't answering: {e}")
            st["status_err_at"] = time.time()
        return
    msgs = st.setdefault("status_msg", {})
    for c in chats:
        cur = msgs.get(str(c["chat_id"]))
        if cur and cur.get("text") == text:
            continue
        if cur:
            try:
                tg("editMessageText", chat_id=c["chat_id"], message_id=cur["message_id"], text=text, parse_mode="HTML",
                   disable_web_page_preview=True, **floor_markup())
                cur["text"] = text
                save_state(st)
                continue
            except RuntimeError as e:
                if "not modified" in str(e):
                    cur["text"] = text
                    continue
                if not any(x in str(e) for x in ("not found", "can't be edited", "MESSAGE_ID_INVALID")):
                    log(f"status edit in {c['name']}: {e}")
                    continue
        try:
            m = tg("sendMessage", chat_id=c["chat_id"], text=text, parse_mode="HTML", disable_notification=True,
                   disable_web_page_preview=True, **floor_markup())
        except RuntimeError as e:
            log(f"status in {c['name']}: {e}")
            continue
        msgs[str(c["chat_id"])] = {"message_id": m["message_id"], "text": text}
        save_state(st)
        if safe("pinChatMessage", chat_id=c["chat_id"], message_id=m["message_id"], disable_notification=True) is None:
            log(f"status in {c['name']}: posted, but not pinned (make the bot an admin that may pin, or pin it by hand)")
        else:
            log(f"status in {c['name']}: posted and pinned")


def set_menu_button() -> None:
    """In every private chat with this bot, a menu button that opens the Floor inside Telegram."""
    if FLOOR_PUBLIC:
        safe("setChatMenuButton", menu_button={"type": "web_app", "text": "Floor", "web_app": {"url": FLOOR_PUBLIC + "/"}})


def board_text() -> str:
    """What each agent is doing (the Floor's bots status), or, for a hive of goals, open goals and tasks with
    what each agent is doing now (from the Floor, floor.py)."""
    if BOARD_BOTS:
        try:
            return floor_get("/api/status")["text"]
        except Exception as e:
            return f"The Floor isn't answering ({esc(str(e))[:120]}). Check: systemctl status {hp.HIVE_ID}-floor"
    try:
        with urllib.request.urlopen(f"{FLOOR}/api/state", timeout=10) as r:
            st = json.load(r)
        with urllib.request.urlopen(f"{FLOOR}/api/live", timeout=10) as r:
            live = json.load(r)["live"]
    except Exception as e:
        return f"The Floor isn't answering ({esc(str(e))[:120]}). Check: systemctl --user status {hp.HIVE_ID}-floor"
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


CHAT: queue.Queue = queue.Queue()   # (command, message, from the operator?)


PINGS = {   # record event -> (state cursor, message to the chief of staff, or None to skip it)
    "goal.approved": ("approved_pos", lambda e: f"Goal {e['goal']} was approved (position {e['position']}). "
                      "Start it now: orders, tasks, assignments. Reply in one line with what you created."),
    "task.escalated": ("escalated_pos", lambda e: None if (e.get("data") or {}).get("to") != "chief_of_staff" else
                       f"Task {e['task']} was escalated to you (position {e['position']}, "
                       f"{e['data'].get('code')}): {e['data'].get('reason', '')}. Handle it as your doc says "
                       "and resolve it on the record. Reply in one line with what you did, or what the human must decide."),
    "task.blocked": ("blocked_pos", lambda e: f"Task {e['task']} is blocked with a question for you "
                     f"(position {e['position']}, needs {(e.get('data') or {}).get('needs')}): "
                     f"{(e.get('data') or {}).get('reason', '')[:300]} Read the question, then answer it with a file in "
                     "questions/ and task.answered, or tell the human what they must decide. Reply in one line."),
    "review.recorded": ("reviewed_pos", lambda e: None if (e.get("data") or {}).get("verdict") != "passed" else
                        f"Task {e['task']} passed its review (position {e['position']}). Read the review, close "
                        "the task, and complete the goal if it was the last one. Reply in one line with what you did."),
}


def ping_cos(st: dict, chat_cmd: str) -> None:
    """Tell the chief of staff about each goal approved, each escalation to it and each passed
    review, since the last check: nothing else wakes it."""
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
                CHAT.put((chat_cmd, msg, False, None))
                log(f"pinged cos: {etype} {e.get('goal') or e.get('task')}")
    save_state(st)


FOLLOWUPS = hp.WORK_DIR / "cos" / "followups"
FOLLOWUP_WINDOW_S, FOLLOWUP_AHEAD_S, FOLLOWUP_MAX = 16 * 60, 2 * 3600, 3


def check_followups(chat_cmd: str) -> None:
    """Wake the chief of staff for each follow-up the operator asked for that is due.
    A file `followups/<name>.json` is JSON {"at": "<ISO time>", "note": "<what to check>"}. On first
    sight it is accepted (renamed `<name>.ok`) only if written within FOLLOWUP_WINDOW_S of the start
    of a turn on an operator message, for at most FOLLOWUP_AHEAD_S ahead, with fewer than
    FOLLOWUP_MAX pending; otherwise it is deleted. Accepted ones are sent when due."""
    try:
        pending = sorted(FOLLOWUPS.glob("*.ok"))
        new = sorted(FOLLOWUPS.glob("*.json"), key=lambda f: f.stat().st_mtime)
    except OSError as e:
        log(f"followups: {e}")
        return
    human = last_human_turn()
    for f in new:
        try:
            made = f.stat().st_mtime
            at = followup_at(f)
        except (OSError, ValueError, KeyError, TypeError) as e:
            log(f"followup {f.name} dropped: {e}")
            f.unlink(missing_ok=True)
            continue
        why = ("not written while answering the operator" if not human <= made <= human + FOLLOWUP_WINDOW_S
               else "more than 2 hours ahead" if at > made + FOLLOWUP_AHEAD_S
               else "too many pending" if len(pending) >= FOLLOWUP_MAX else None)
        if why:
            log(f"followup {f.name} refused: {why}")
            f.unlink(missing_ok=True)
            continue
        ok = f.with_suffix(".ok")
        f.rename(ok)
        pending.append(ok)
        log(f"followup {f.name} accepted for {time.strftime('%H:%M', time.localtime(at))}")
    for f in pending:
        try:
            at, note = followup_at(f), str(json.loads(f.read_text()).get("note", ""))[:500]
        except (OSError, ValueError, KeyError, TypeError) as e:
            log(f"followup {f.name} dropped: {e}")
            f.unlink(missing_ok=True)
            continue
        if at > time.time():
            continue
        f.unlink(missing_ok=True)
        CHAT.put((chat_cmd, f"The follow-up the human asked for is due ({time.strftime('%H:%M', time.localtime(at))}): "
                            f"{note} Check it now and reply in one line with what you found.", False, None))
        log(f"requested follow-up: {f.name}")


def followup_at(f: Path) -> float:
    at = dt.datetime.fromisoformat(json.loads(f.read_text())["at"].replace("Z", "+00:00"))
    return (at if at.tzinfo else at.astimezone()).timestamp()


HUMAN_TURN = STATE.parent / "last-human-turn"


def last_human_turn() -> float:
    try:
        return float(HUMAN_TURN.read_text())
    except (OSError, ValueError):
        return 0.0


COS_WORKSPACE = hp.WORK_DIR / "cos" / "workspace"
ATTACH_MAX, ATTACH_BYTES = 5, 10 * 1024 * 1024
PHOTO_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
FILE_TYPES = PHOTO_TYPES | {".gif", ".svg", ".pdf", ".txt", ".md", ".csv", ".json", ".log", ".html"}
ATTACH_LINE = re.compile(r"^\s*\[\[(photo|file):\s*(.+?)\s*\]\]\s*$", re.I)


def split_attachments(reply: str) -> tuple[str, list[tuple[str, str]]]:
    """The reply without its attachment lines, and the (kind, path) of each."""
    keep, found = [], []
    for line in reply.splitlines():
        m = ATTACH_LINE.match(line)
        if m:
            found.append((m.group(1).lower(), m.group(2)))
        else:
            keep.append(line)
    return "\n".join(keep).strip(), found


def attachment(raw: str) -> Path:
    """The file a chat reply names, only if it is inside the chief of staff's workspace clone."""
    rel = raw.strip("`'\"")
    if rel.startswith("/cos/workspace/"):
        rel = rel[len("/cos/workspace/"):]
    if rel.startswith("/"):
        raise ValueError("only paths inside the workspace")
    base = COS_WORKSPACE.resolve()
    f = (base / rel).resolve()
    if not f.is_relative_to(base) or ".git" in f.relative_to(base).parts:
        raise ValueError("outside the workspace")
    if f.suffix.lower() not in FILE_TYPES:
        raise ValueError(f"type {f.suffix or '(none)'} not allowed")
    if not f.is_file():
        raise ValueError("no such file")
    if f.stat().st_size > ATTACH_BYTES:
        raise ValueError("over 10 MB")
    return f


def tg_file(method: str, field: str, f: Path, **params) -> dict:
    """A Telegram call with one file upload (multipart/form-data)."""
    boundary = f"----1hive{os.urandom(8).hex()}"
    body = b""
    for k, v in params.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{f.name}\"\r\n"
             f"Content-Type: application/octet-stream\r\n\r\n").encode() + f.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        out = json.loads(e.read() or b"{}")
    if not out.get("ok"):
        raise RuntimeError(f"telegram {method}: {out.get('description', out)}")
    return out["result"]


def send_attachments(found: list[tuple[str, str]], chat_id=None) -> list[str]:
    """Send each named file; return a note for each that couldn't be sent."""
    notes = []
    for i, (kind, raw) in enumerate(found):
        if i >= ATTACH_MAX:
            notes.append(f"(not attached, over {ATTACH_MAX} per reply: {raw})")
            continue
        try:
            f = attachment(raw)
            photo = kind == "photo" and f.suffix.lower() in PHOTO_TYPES
            tg_file("sendPhoto" if photo else "sendDocument", "photo" if photo else "document", f,
                    chat_id=chat_id or ALLOW["chat_id"], caption=f.name)
            log(f"chat: attached {f}")
        except Exception as e:
            notes.append(f"(couldn't attach {raw}: {e})")
            log(f"chat: attachment {raw!r} refused: {e}")
    return notes


NOOP = ("no response requested", "no reply needed", "no response needed", "nothing to add", "(no reply)")


def is_noop(reply: str) -> bool:
    """A reply that says nothing: not worth a Telegram message."""
    r = reply.strip().strip("*_`.").lower()
    return not r or any(r == n.strip(".") or r == n.strip("()") for n in NOOP)


FEED = {   # record event -> one line for the operator: only work that is done
    "task.closed": lambda e, d: "✅ done",
    "task.cancelled": lambda e, d: f"✖️ cancelled: {d.get('reason', '')}",
}


def push_feed(st: dict) -> None:
    """One line per task done since the last check; the first run starts at the head."""
    ok, out = hive("events", "--after", str(st.get("feed_pos", 0)))
    if not ok:
        return
    evs = [json.loads(x) for x in out.splitlines() if x.strip()]
    if "feed_pos" not in st:
        st["feed_pos"] = max([e["position"] for e in evs], default=0)
        save_state(st)
        return
    lines = []
    for e in evs:
        st["feed_pos"] = max(st["feed_pos"], e["position"])
        f = FEED.get(e["type"])
        if f:
            t = time.strftime("%H:%M", time.localtime(dt.datetime.fromisoformat(
                e["recorded_at"].replace("Z", "+00:00")).timestamp()))
            lines.append(f"{t} <b>{esc(e.get('task') or e.get('goal') or '')}</b> {esc(f(e, e.get('data') or {})[:200])}")
    if lines:
        tg("sendMessage", chat_id=ALLOW["chat_id"], text="\n".join(lines), parse_mode="HTML")
        log(f"feed: {len(lines)} lines")
    save_state(st)


TO_LINE = re.compile(r"^\s*\[\[to:\s*(.+?)\s*\]\]\s*$", re.I)
TASK_LINE = re.compile(r"^\s*\[\[task:\s*([a-z0-9][a-z0-9._-]{0,63})\s*\]\]\s*$", re.I)
ORIGINS = STATE.parent / "origins.json"   # task -> the chat message it was started from


def load_origins() -> dict:
    try:
        return json.loads(ORIGINS.read_text())
    except (OSError, ValueError):
        return {}


def save_origins(o: dict) -> None:
    tmp = ORIGINS.with_suffix(".tmp")
    tmp.write_text(json.dumps(o))
    tmp.replace(ORIGINS)


def take_task_lines(reply: str, origin: dict | None) -> str:
    """Remove [[task: <id>]] lines; remember each task's origin message, if the reply answers one."""
    keep, found = [], []
    for line in reply.splitlines():
        m = TASK_LINE.match(line)
        (found.append(m.group(1).lower()) if m else keep.append(line))
    if found and origin and origin.get("reply_to"):
        o = load_origins()
        for t in found:
            o[t] = {"chat_id": origin["chat_id"], "message_id": origin["reply_to"]}
        save_origins(o)
        log(f"chat: tasks {', '.join(found)} started from message {origin['reply_to']} in {origin['chat_id']}")
    return "\n".join(keep).strip()


ORIGIN_NOTES = {   # record event -> the line posted on the task's origin message
    "task.accepted": lambda e, d: "started",
    "task.blocked": lambda e, d: f"has a question ({d.get('needs')}): {d.get('reason', '')}",
    "task.result_posted": lambda e, d: "result posted, waiting for review",
    "review.recorded": lambda e, d: f"review {d.get('verdict')} ({e['actor']['id']})",
    "task.closed": lambda e, d: "done ✅",
    "task.cancelled": lambda e, d: f"cancelled: {d.get('reason', '')}",
}


def push_origins(st: dict) -> None:
    """Thread each chat-started task's progress on the message it came from."""
    o = load_origins()
    ok, out = hive("events", "--after", str(st.get("origin_pos", 0)))
    if not ok:
        return
    evs = [json.loads(x) for x in out.splitlines() if x.strip()]
    if "origin_pos" not in st:     # first run: start at the head
        st["origin_pos"] = max([e["position"] for e in evs], default=0)
        save_state(st)
        return
    for e in evs:
        st["origin_pos"] = max(st["origin_pos"], e["position"])
        t, f = e.get("task"), ORIGIN_NOTES.get(e["type"])
        if not (t and f and t in o):
            continue
        safe("sendMessage", chat_id=o[t]["chat_id"], text=f"{t}: {f(e, e.get('data') or {})}"[:1000],
             reply_to_message_id=o[t]["message_id"], allow_sending_without_reply=True)
        if e["type"] in ("task.closed", "task.cancelled"):
            del o[t]
    save_origins(o)
    save_state(st)


def route_reply(reply: str, target: dict | None) -> tuple[str, dict | None]:
    """A reply that starts with [[to: <chat name>]] goes to that group chat instead (docs/design.md)."""
    first, _, rest = reply.partition("\n")
    m = TO_LINE.match(first)
    if not m:
        return reply, target
    name = m.group(1).lower()
    hit = next((c for c in CHATS.values() if c.get("name", "").lower() == name), None)
    if hit is None:
        log(f"chat: [[to: {m.group(1)}]] names no configured chat; sent to the operator chat")
        return reply, None
    return rest.strip(), {"chat_id": hit["chat_id"], "reply_to": None}


def cos_turn(phase: str, started: float, chat, human: bool) -> None:
    """For the Floor: cos-now.json while the chief of staff answers, and a line per turn in cos-turns.jsonl."""
    try:
        now_f = STATE.parent / "cos-now.json"
        if phase == "start":
            now_f.write_text(json.dumps({"since": started, "chat": chat, "human": human}))
            return
        now_f.unlink(missing_ok=True)
        with (STATE.parent / "cos-turns.jsonl").open("a") as f:
            f.write(json.dumps({"start": started, "end": time.time(), "chat": chat, "human": human}) + "\n")
    except OSError as e:
        log(f"cos turn log: {e}")


def chat_worker() -> None:
    """Chat with the chief of staff, one message at a time across every chat, off the main loop."""
    while True:
        cmd, text, human, target = CHAT.get()
        if human:   # when cos starts on a human's message: the window for follow-ups (check_followups)
            HUMAN_TURN.write_text(str(time.time()))
        chat_id = (target or {}).get("chat_id") or ALLOW["chat_id"]
        started = time.time()
        cos_turn("start", started, chat_id if target else None, human)
        try:
            safe("sendChatAction", chat_id=chat_id, action="typing")
            r = subprocess.run(cmd, shell=True, input=text, capture_output=True, text=True, timeout=960)
            reply = (r.stdout or r.stderr or "(no reply)").strip()
        except subprocess.TimeoutExpired:
            reply = "(the chief of staff didn't answer in 16 minutes; try again)"
        except Exception as e:
            reply = f"(chat failed: {e})"
        cos_turn("end", started, chat_id if target else None, human)
        log(f"chat: {len(text)} chars in, {len(reply)} out")
        reply = take_task_lines(reply, target)
        reply, target = route_reply(reply, target)
        chat_id = (target or {}).get("chat_id") or ALLOW["chat_id"]
        thread = {"reply_to_message_id": target["reply_to"], "allow_sending_without_reply": True} \
            if target and target.get("reply_to") else {}
        reply, found = split_attachments(reply)
        if is_noop(reply) and not found:
            log(f"chat: no-op reply not forwarded: {reply[:60]!r}")
            CHAT.task_done()
            continue
        if not is_noop(reply):
            for i in range(0, len(reply), 3900):
                safe("sendMessage", chat_id=chat_id, text=reply[i:i + 3900], **(thread if i == 0 else {}))
        notes = send_attachments(found, chat_id)
        if notes:
            safe("sendMessage", chat_id=chat_id, text="\n".join(notes))
        CHAT.task_done()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--poll", type=int, default=30, help="long-poll seconds")
    ap.add_argument("--inbox-every", type=int, default=60)
    ap.add_argument("--remind-hours", type=float, default=12, help="re-send a request still waiting after this long")
    ap.add_argument("--digest-at", default="08:00", help="daily digest time, HH:MM local")
    ap.add_argument("--no-feed", action="store_true", help="don't push a note when a task is done")
    ap.add_argument("--chat-cmd", default=os.environ.get("TELEGRAM_CHAT_CMD") or None,
                    help="command that reads a message on stdin and prints the reply (default: $TELEGRAM_CHAT_CMD)")
    a = ap.parse_args()
    global REMIND_S, DIGEST_AT
    REMIND_S = a.remind_hours * 3600
    DIGEST_AT = tuple(int(x) for x in a.digest_at.split(":"))
    st = load_state()
    if CHATS:
        try:
            ME.update(tg("getMe"))
            log(f"bridge bot: @{ME.get('username')}")
        except Exception as e:
            log(f"getMe failed: {e}")
    threading.Thread(target=chat_worker, daemon=True).start()
    set_menu_button()
    log("bridge up")
    next_inbox = 0.0
    while True:
        try:
            # The first run sends one now; afterwards, at each digest time.
            if time.time() >= next_digest(st.get("digest_at", 0)).timestamp():
                send_digest(st)
            if time.time() >= next_inbox:
                push_inbox(st)
                if not a.no_feed:
                    push_feed(st)
                if CHATS:
                    push_origins(st)
                    push_status(st)
                if a.chat_cmd:
                    ping_cos(st, a.chat_cmd)
                    check_followups(a.chat_cmd)
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
