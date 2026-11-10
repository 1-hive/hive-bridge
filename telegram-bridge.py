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
the path inside the chief of staff's workspace clone (relative, or under /cos/workspace) or its
outbox (HIVE_COS_OUTBOX, or /cos/outbox), where it copies other files to send. The line is taken
out of the text and the file sent with sendPhoto or sendDocument: at most 5 per reply, 50 MB each
(Telegram's limit for a bot), never a file outside those two (symlinks resolved), only listed
types. Outbox files go after 7 days.

Transcripts: the dialogue log (.telegram/dialogue.jsonl) has every message in the group chats,
the bridge's own included; transcript.py makes one group's Markdown transcript from it (the chief of
staff runs it when asked, and sends the file like any other).

The digest lists results waiting more than an hour for a verdict, with what the review is doing.

An over-budget alert carries a "+4h" button: it raises the goal's wall-clock budget
to 4 hours from now (`op budget` does the same) and resolves the alert.

New repositories (with HIVE_NEW_REPO_CMD): a chief-of-staff request with `kind: new-repository` is posted with
Create / Decline buttons in the chat that asked (any operator may tap there); Create runs the command and
records hive.registry_changed with the digest it prints. See "New repositories" below main's helpers.

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

import card
import hive_paths as hp
import decision_buttons as db

CFG = hp.CONFIG_DIR
STATE = hp.WORK_DIR / ".telegram" / "state.json"
TOKEN = (CFG / "telegram-token").read_text().strip()
ALLOW = json.loads((CFG / "telegram.json").read_text())
# Group chats (hive-bridge docs/design.md), optional: without this file the bridge serves only the
# operator's private chat (ALLOW), as before. With it, allowed humans and known bots in group chats:
# {"chats": [{"name", "chat_id", "private": false, "mode": "all" | "mention"}],
#  "humans": [{"name", "user_id", "username", "permissions": ["operator"]}], "bots": [{"name", "user_id", "username", "actor"}]}
# mode "all" (the work group): every human message goes to the chief of staff. mode "mention" (a conversation
# group): only messages that @-mention the bridge's bot or reply to it, from humans and from known bots.
# Every message seen, in every listed chat, goes to the dialogue log (.telegram/dialogue.jsonl).
_GROUPS_FILE = CFG / "telegram-groups.json"
GROUPS = json.loads(_GROUPS_FILE.read_text()) if _GROUPS_FILE.is_file() else {}
CHATS = {c["chat_id"]: c for c in GROUPS.get("chats", [])}
HUMANS = {h["user_id"]: h for h in GROUPS.get("humans", [])}
# The humans the chief of staff may tag: "@<username>" in a plain-text reply notifies them (telegram-groups.json)
PEOPLE = [{"name": h["name"], "handle": f"@{h['username']}", **({"operator": True} if "operator" in h.get("permissions", []) else {})}
          for h in GROUPS.get("humans", []) if h.get("username")]
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
    note_sent(method, params, out["result"])
    return out["result"]


def note_sent(method: str, params: dict, res) -> None:
    """The bridge's own messages in a group chat go in the dialogue log too (replies, task progress, decision cards),
    so a transcript has both sides. Not the pinned status (sent silently and edited in place)."""
    if method not in ("sendMessage", "sendPhoto", "sendDocument") or params.get("disable_notification") \
            or not isinstance(res, dict):
        return
    try:
        cid = int(params.get("chat_id"))
    except (TypeError, ValueError):
        return
    if cid not in CHATS:
        return
    doc = res.get("document") or {}
    rt = (res.get("reply_to_message") or {}).get("message_id")
    try:
        with DIALOGUE.open("a") as f:
            f.write(json.dumps({"chat": cid, "chat_name": CHATS[cid].get("name"),
                                "at": dt.datetime.fromtimestamp(res.get("date", 0), dt.UTC).isoformat(),
                                "from_id": (res.get("from") or {}).get("id"), "bot": True, "sent": True,
                                "id": res.get("message_id"), "from": sender_name(res.get("from") or {}),
                                "text": res.get("text") or res.get("caption") or "",
                                **({"reply_to": rt} if rt else {}),
                                **({"files": [{"name": doc["file_name"]}]} if doc.get("file_name") else {})}) + "\n")
    except OSError as e:
        log(f"dialogue log: {e}")


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


# New repositories (hive-runtime new-repo.py): the chief of staff files a request whose front matter says
# `kind: new-repository`; the bridge posts it with Create / Decline buttons as a reply to the message that asked
# (or in the operator chat), and any human with the operator permission may tap. Create runs NEW_REPO_CMD NAME
# DESCRIPTION (e.g. proto-hive's deploy/new-repo through one sudo rule), records hive.registry_changed with the
# digest it prints (this bridge's operator key: the SPEC lets only an operator record it) and tells the chief of
# staff. Off unless HIVE_NEW_REPO_CMD is set. Agents never hold the GitHub credential; a human decides each time.
NEW_REPO_CMD = os.environ.get("HIVE_NEW_REPO_CMD", "")
CHAT_CMD: str | None = None   # --chat-cmd, set in main()
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CAUSE_RE = re.compile(r"^telegram:(-?\d+)/(\d+)$")


def front_matter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip().lower()] = v.strip().strip('"').strip("'")
    return {}


def repo_requests() -> list[dict]:
    """Open new-repository requests on the workspace's main (hive-requests/*.md, not in a subfolder)."""
    r = subprocess.run(["git", "-C", str(WORKSPACE), "ls-tree", "--name-only", "main", "hive-requests/"],
                       capture_output=True, text=True)
    out = []
    for path in r.stdout.split() if r.returncode == 0 else []:
        if not path.endswith(".md"):
            continue
        body = subprocess.run(["git", "-C", str(WORKSPACE), "show", f"main:{path}"], capture_output=True, text=True).stdout
        fm = front_matter(body)
        if fm.get("kind") != "new-repository" or not REPO_NAME.match(fm.get("name", "")):
            continue
        out.append({"key": hashlib.sha256(path.encode()).hexdigest()[:10], "path": path, "name": fm["name"],
                    "description": fm.get("description", "")[:300], "asked_by": fm.get("asked_by", ""),
                    "cause": fm.get("cause", "")})
    return out


def push_repo_requests(st: dict) -> None:
    if not NEW_REPO_CMD:
        return
    seen = st.setdefault("repo_requests", {})
    for q in repo_requests():
        if q["key"] in seen:
            continue
        m = CAUSE_RE.match(q["cause"])
        chat = int(m.group(1)) if m and int(m.group(1)) in CHATS else ALLOW["chat_id"]
        reply = ({"reply_parameters": {"message_id": int(m.group(2)), "allow_sending_without_reply": True}}
                 if m and chat != ALLOW["chat_id"] else {})
        text = (f"🆕 <b>New repository: {esc(q['name'])}</b>" + (f"\n{esc(q['description'])}" if q["description"] else "")
                + (f"\nAsked by {esc(q['asked_by'])}." if q["asked_by"] else "")
                + "\n\nCreate makes it under the agents' GitHub account and registers it, so the agents can work in it "
                  "and their work is reviewed before it reaches GitHub. An operator taps.")
        kb = [[{"text": "✅ Create", "callback_data": f"newrepo:{q['key']}"},
               {"text": "✖ Decline", "callback_data": f"norepo:{q['key']}"}]]
        sent = safe("sendMessage", chat_id=chat, text=text, parse_mode="HTML", reply_markup={"inline_keyboard": kb}, **reply)
        if sent is None and reply:   # e.g. the origin message is gone: post without the reply
            sent = safe("sendMessage", chat_id=chat, text=text, parse_mode="HTML", reply_markup={"inline_keyboard": kb})
        if sent is None:
            log(f"couldn't post the new repository request {q['path']}")
            continue
        seen[q["key"]] = {**q, "state": "asked", "chat": chat, "message": sent["message_id"], "text": text}
        save_state(st)
        log(f"posted new repository request {q['name']} ({q['path']})")


def create_repo(q: dict, who: str) -> tuple[bool, str]:
    """Run NEW_REPO_CMD, then record the registry it made. (ok, a line for the chat)."""
    try:
        r = subprocess.run([*NEW_REPO_CMD.split(), q["name"], q["description"]], capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"couldn't run the new-repository command: {e}"
    out = (r.stdout or "") + (r.stderr or "")
    digest = next((x.split("=", 1)[1].strip() for x in reversed(out.splitlines()) if x.startswith("registry_digest=")), "")
    url = next((x.split()[1] for x in out.splitlines() if x.startswith("ready: ")), "")
    if r.returncode or not digest.startswith("sha256:"):
        last = [x for x in out.splitlines() if x.strip()][-3:]
        return False, "creating it failed: " + " / ".join(last)[-400:]
    args = ["emit", "hive.registry_changed", "--data",
            json.dumps({"registry_digest": digest, "reason": f"new repository {q['name'].lower()} ({who}, via telegram)"})]
    if CAUSE_RE.match(q["cause"]):
        args += ["--cause-external", q["cause"]]
    ok, rec = hive(*args)
    if not ok:
        return False, (f"{url or q['name']} was created, but recording the registry failed, so every pin is refused "
                       f"until an operator records {digest}: {rec.splitlines()[-1][:200] if rec else '?'}")
    return True, f"{url} is ready and registered (position {json.loads(rec)['position']})"


def on_repo_callback(cq: dict, st: dict, action: str, key: str) -> None:
    frm, msg = cq.get("from") or {}, cq.get("message") or {}
    q = st.setdefault("repo_requests", {}).get(key)
    chat = (msg.get("chat") or {}).get("id")
    if not q or chat != q["chat"] or msg.get("message_id") != q["message"]:
        safe("answerCallbackQuery", callback_query_id=cq["id"], text="unknown request")
        return
    if not is_operator(frm.get("id")):
        safe("answerCallbackQuery", callback_query_id=cq["id"], text="only an operator can decide this")
        log(f"refused new repository tap from {frm.get('id')}")
        return
    if q["state"] != "asked":
        safe("answerCallbackQuery", callback_query_id=cq["id"], text="already decided")
        return
    who = sender_name(frm)
    q["state"] = "creating" if action == "newrepo" else "declined"
    save_state(st)
    safe("answerCallbackQuery", callback_query_id=cq["id"], text="creating it…" if action == "newrepo" else "declined")
    if action == "norepo":
        line = f"✖ Declined by {who}."
        cos = (f"{who} (operator) declined the new repository {q['name']} (request {q['path']}). Tell whoever asked, "
               "and move the request to hive-requests/closed/. Reply in one line.")
    else:
        safe("editMessageText", chat_id=chat, message_id=q["message"], text=q["text"] + f"\n\n⏳ {esc(who)} tapped Create…",
             parse_mode="HTML")
        ok, info = create_repo(q, who)
        q["state"] = "done" if ok else "failed"
        line = f"{'✅' if ok else '❌'} {info} ({who})."
        cos = (f"Operators: the new repository {q['name'].lower()} is ready and registered, staged ({info}; {who} tapped "
               f"Create). Assign code work in it now (repos: [\"{q['name'].lower()}\"]); workers clone it from GitHub. "
               f"Move the request {q['path']} to hive-requests/closed/ and tell whoever asked. Reply in one line."
               if ok else
               f"Creating the new repository {q['name']} failed ({who} tapped Create): {info}. Tell the operators what it "
               "says; don't retry it yourself. Reply in one line.")
        log(f"new repository {q['name']}: {info}")
    save_state(st)
    safe("editMessageText", chat_id=chat, message_id=q["message"], text=q["text"] + "\n\n" + esc(line), parse_mode="HTML")
    if CHAT_CMD:
        CHAT.put((CHAT_CMD, cos, False, None))


# Decision cards answered by tapping (decision_buttons.py): the ❓ notice of a worker's card gets one button per
# answer; an operator's tap is shown on the message and passed to the chief of staff, which records the answer.
def offer_buttons(st: dict, task: str, c: dict, chat: int, message: dict | None, text: str) -> None:
    """Put the card's answer buttons on the ❓ message just sent (when any question offers answers)."""
    if not message or not message.get("message_id"):
        return
    st.setdefault("decision_cards", {})
    key = f"{abs(chat) % 1000000}.{message['message_id']}"   # unique per message, fits callback_data's 64 bytes
    rows = db.keyboard(c, key)
    if not rows:
        return
    if safe("editMessageReplyMarkup", chat_id=chat, message_id=message["message_id"],
            reply_markup={"inline_keyboard": rows}) is None:
        return
    cards = st["decision_cards"]
    cards[key] = db.new_entry(task, c, chat, message["message_id"], text)
    for old in sorted(cards, key=lambda k: cards[k]["message"])[:-200]:   # keep the last 200
        cards.pop(old, None)
    save_state(st)


def on_decision_callback(cq: dict, st: dict) -> None:
    frm, msg = cq.get("from") or {}, cq.get("message") or {}
    parsed = db.parse_data(cq.get("data") or "")
    entry = (st.get("decision_cards") or {}).get(parsed[0]) if parsed else None
    if not entry or (msg.get("chat") or {}).get("id") != entry["chat"] or msg.get("message_id") != entry["message"]:
        safe("answerCallbackQuery", callback_query_id=cq["id"], text="unknown question")
        return
    if not is_operator(frm.get("id")):
        safe("answerCallbackQuery", callback_query_id=cq["id"], text="only an operator can answer this here")
        log(f"refused decision tap from {frm.get('id')}")
        return
    who = sender_name(frm)
    ok, note, line, cos = db.tap(entry, parsed[1], parsed[2], who)
    safe("answerCallbackQuery", callback_query_id=cq["id"], text=note)
    if not ok:
        return
    entry["text"] += "\n" + line
    save_state(st)
    log(f"decision {entry['task']} q{parsed[1] + 1}: {line}")
    rows = db.keyboard(entry["card"], parsed[0], entry["answered"])
    if safe("editMessageText", chat_id=entry["chat"], message_id=entry["message"], text=entry["text"],
            disable_web_page_preview=True, reply_markup={"inline_keyboard": rows}) is None:
        safe("sendMessage", chat_id=entry["chat"], text=line, reply_to_message_id=entry["message"],
             allow_sending_without_reply=True)
    if CHAT_CMD:
        CHAT.put((CHAT_CMD, cos, False, None))


def on_callback(cq: dict, st: dict) -> None:
    msg = cq.get("message") or {}
    act, _, rest = (cq.get("data") or "").partition(":")
    if act in ("newrepo", "norepo"):   # posted in group chats too: any operator may decide (on_repo_callback)
        return on_repo_callback(cq, st, act, rest)
    if act == "dq":                     # a decision card's answer button, in any chat (on_decision_callback)
        return on_decision_callback(cq, st)
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
    files = [{k: d[k] for k in ("name", "path", "text_path", "note") if d.get(k)} for d in files_of(m, inline=False)]
    entry = {"id": m["message_id"], "from": sender_name(m.get("from") or {}),
             "text": (m.get("text") or m.get("caption") or "")[:500], **({"reply_to": rt["message_id"]} if rt else {}),
             **({"files": files} if files else {})}
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
                 "done) on this message. Files people attach are under message.files (and reply_to.files): a text "
                 "file's content is inline under text, and any file can be read at its path; thread entries name theirs.\n\n")


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
    if not text and not message_files(m):
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
    files = files_of(m)
    # A file sent on its own, then a separate message about it (Telegram sends a file and its text apart unless
    # the text is a caption): the sender's file-only message just before this one comes with it
    th = st["threads"][str(chat["id"])]
    prev = th[-2] if len(th) > 1 else {}
    if (not files and prev.get("files") and not prev.get("text") and prev.get("from") == sender_name(frm)
            and prev.get("id") != rt.get("message_id")):
        files = [{"from_message": prev["id"], **d} for d in prev["files"]]
        for d in files:
            p = Path(d.get("text_path") or d.get("path") or "")
            if (d.get("text_path") or is_text(p.name)) and p.is_file():
                d.update(read_text(p))
    rt_files = files_of(rt) if rt else []
    env = {"chat": {"id": chat["id"], "name": CHATS.get(chat["id"], {}).get("name", chat.get("title")),
                    "kind": "group", "private": CHATS.get(chat["id"], {}).get("private", False)},
           "message": {"id": m["message_id"], "at": dt.datetime.fromtimestamp(m["date"], dt.UTC).isoformat(), "text": text,
                       **({"files": files} if files else {})},
           "from": ({"bot": sender_name(frm), "actor": BOTS.get(frm.get("id"), {}).get("actor")} if is_bot
                    else {"human": sender_name(frm), "permissions": (["operator"] if is_operator(frm.get("id")) else [])}),
           **({"reply_to": {"id": rt["message_id"], "from": sender_name(rt.get("from") or {}),
                            "text": (rt.get("text") or rt.get("caption") or "")[:1000],
                            **({"files": rt_files} if rt_files else {})}} if rt else {}),
           "mentions": mentions(m),
           "people": PEOPLE,
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
    if m.get("migrate_to_chat_id") and (m.get("chat") or {}).get("id") in CHATS:
        # Telegram turned a configured group into a supergroup (e.g. a setting changed): it has a new id, and
        # until telegram-groups.json names it, every message there is ignored
        old, new = m["chat"]["id"], m["migrate_to_chat_id"]
        log(f"group {old} ({CHATS[old].get('name')}) became supergroup {new}: put {new} in telegram-groups.json")
        safe("sendMessage", chat_id=ALLOW["chat_id"], text=f"⚠️ Telegram upgraded the {CHATS[old].get('name')} group to a "
             f"supergroup with a new id, {new}. The bridge ignores it until telegram-groups.json has chat_id {new} "
             f"in place of {old} (then restart hive-telegram).")
        return
    if (m.get("chat") or {}).get("type") in ("group", "supergroup") and (m["chat"]["id"] in CHATS or m["chat"]["id"] == ALLOW["chat_id"]):
        on_group_message(m, st, chat_cmd)
        return
    text = (m.get("text") or m.get("caption") or "").strip()
    frm, chat = m.get("from") or {}, m.get("chat") or {}
    if chat.get("type") == "private" and is_operator(frm.get("id")) and text.split("@")[0] in ("/floor", "/start floor", "/board"):
        private_floor(chat["id"], frm["id"], text)
        return
    if chat.get("type") == "private" and chat.get("id") != ALLOW["chat_id"] and is_operator(frm.get("id")):
        other_operator(m, frm, chat, text, chat_cmd)
        return
    if not allowed(frm, chat):
        log(f"ignored message from {frm.get('id')} in {chat.get('type')} {chat.get('id')} {chat.get('title')!r}")
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
        CHAT.put((chat_cmd, text + files_block(files_of(m)), True, None))
    else:
        tg("sendMessage", chat_id=ALLOW["chat_id"],
           text="Chat with the chief of staff isn't connected yet; buttons and /inbox work.")


def other_operator(m: dict, frm: dict, chat: dict, text: str, chat_cmd: str | None) -> None:
    """Another operator's private chat (telegram-groups.json humans with the operator permission, e.g. Ben): help, and
    the chief of staff, whose reply comes back here. The buttons, inbox and digest stay in telegram.json's chat."""
    who = (HUMANS.get(frm.get("id")) or {}).get("name") or frm.get("first_name") or str(frm.get("id"))
    if text.split("@")[0] in ("/start", "/help") or not (text or message_files(m)):
        safe("sendMessage", chat_id=chat["id"], text=f"{hp.HIVE_ID}: write here to talk to the chief of staff, the "
             "hive's coordinator; it answers in this chat. /floor opens the Floor (what each agent is doing now, what "
             "it owes, and what needs you); /board shows the same in a message.")
        return
    if not chat_cmd:
        safe("sendMessage", chat_id=chat["id"], text="Chat with the chief of staff isn't connected; /floor and /board work.")
        return
    if CHAT.unfinished_tasks:
        safe("sendMessage", chat_id=chat["id"], text="(queued: the chief of staff is on another message)")
    CHAT.put((chat_cmd, f"A private message to you from {who}, an operator of the hive. Your reply goes back to "
              f"{who} in that private chat.\n\n{text}{files_block(files_of(m))}", True, {"chat_id": chat["id"], "reply_to": m["message_id"]}))
    log(f"private message from operator {who} to the chief of staff")


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
        if cur and cur.get("pinned") is not True and time.time() - cur.get("pin_tried", 0) > 600:
            # posted when the bot couldn't pin (a group upgraded to a supergroup drops its rights), or before this was
            # recorded: retry
            cur["pin_tried"] = time.time()
            if safe("pinChatMessage", chat_id=c["chat_id"], message_id=cur["message_id"], disable_notification=True) is not None:
                cur["pinned"] = True
                log(f"status in {c['name']}: pinned")
            save_state(st)
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
        pinned = safe("pinChatMessage", chat_id=c["chat_id"], message_id=m["message_id"], disable_notification=True) is not None
        msgs[str(c["chat_id"])] = {"message_id": m["message_id"], "text": text, "pinned": pinned, "pin_tried": time.time()}
        save_state(st)
        log(f"status in {c['name']}: posted and pinned" if pinned else
            f"status in {c['name']}: posted, but not pinned (make the bot an admin that may pin, or pin it by hand; "
            "it retries every 10 min)")


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
    "proposal.submitted": ("proposed_pos", lambda e: None if (e.get("data") or {}).get("approver_class") != "chief_of_staff" else
                           f"{e['actor']['id']} proposes a task, {(e.get('data') or {}).get('proposal_id')} (position "
                           f"{e['position']}): \"{((e.get('data') or {}).get('proposed') or {}).get('data', {}).get('title', '')}\""
                           + (f", asked in chat ({(e.get('cause') or {}).get('external')})" if (e.get('cause') or {}).get('external') else "")
                           + ". Read its order and decide as your doc says: approve and assign it, or reject it with a "
                           "reason. Reply in one line with what you did."),
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


TROUBLE_AFTER = 600   # seconds an agent must stay offline or unable to run before the chief of staff is told
TROUBLE_QUIET = 86400   # an agent it was told about is not mentioned again until it has been fine this long


def ping_cos_trouble(st: dict, chat_cmd: str) -> None:
    """Tell the chief of staff when an agent has been offline or unable to run (the Floor, from its beacons) for
    TROUBLE_AFTER seconds, once: the record can't show it, so without this it learns of it only when a human quotes
    the board. The operators get the Floor's own alert and see the agent on the Floor, so the chief of staff is asked
    to fix it if it can and otherwise say nothing. Not again when it recovers, flaps back, or the bridge restarts,
    until it has been fine for TROUBLE_QUIET."""
    try:
        now_bad = {b["id"]: b for b in floor_get("/api/status").get("trouble") or []}
    except Exception:
        return   # push_status logs a Floor that isn't answering
    seen = st.setdefault("trouble", {})   # actor -> {"since", "told", "clear_since"}
    t = time.time()
    for a, b in now_bad.items():
        p = seen.setdefault(a, {"since": t, "told": False})
        p.pop("clear_since", None)
        if not p["told"] and t - p["since"] >= TROUBLE_AFTER:
            p["told"] = True
            word = "can't run" if b["state"] == "cantrun" else "is offline"
            CHAT.put((chat_cmd, f"The Floor says {b['name']} ({a}) {word}, for {int((t - p['since']) // 60)} min now: "
                                f"{b['detail'][:300]} This is from its runner's beacons, not the record. The operators "
                                "already got one alert about it and see it on the Floor, so don't post about it in the "
                                "chat. If you can fix it yourself (your doc, The Floor), do, and reply in one line with "
                                "what you did; otherwise reply with nothing. You won't be told again while it lasts.",
                         False, None))
            log(f"pinged cos: {a} {b['state']}")
    for a in [a for a in seen if a not in now_bad]:
        p = seen[a]
        if not p["told"]:
            seen.pop(a)   # a short spell: never mentioned
        elif "clear_since" not in p:
            p["clear_since"] = t
        elif t - p["clear_since"] >= TROUBLE_QUIET:
            seen.pop(a)
    save_state(st)


def relay_floor_alerts(st: dict) -> None:
    """Send the operators the Floor's alerts (an agent reporting a problem for 10 min, and its recovery). A Floor
    without the bot's token (it faces the internet) queues them at /api/alerts; each is sent once."""
    try:
        alerts = floor_get(f"/api/alerts?after={st.get('floor_alert_t', 0)}").get("alerts") or []
    except Exception:
        return   # an older Floor, or one that isn't answering (push_status logs that)
    for a in alerts:
        try:
            tg("sendMessage", chat_id=ALLOW["chat_id"], text=a["text"][:4000])
        except Exception as e:
            log(f"floor alert not sent: {e}")
            break
        st["floor_alert_t"] = a["t"]
        save_state(st)
        log(f"floor alert sent: {a['text'][:80]}")


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


# Files people send: a document or a photo in a chat the bridge serves is downloaded here (the Bot API hands a bot
# files up to 20 MB) and shown to the chief of staff with the message: a text file (.md, .txt, code, ...) inline, up
# to INLINE_CHARS, and every file by its path. The folder is group-readable: on vm2 the user cos is in group hive.
INBOX = STATE.parent / "attachments"
INBOX_BYTES, INBOX_KEEP_DAYS, INLINE_CHARS = 20 * 1024 * 1024, 5, 60_000
TEXT_TYPES = {".md", ".markdown", ".txt", ".text", ".rst", ".org", ".tex", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
              ".yml", ".toml", ".ini", ".cfg", ".conf", ".log", ".diff", ".patch", ".html", ".xml", ".py", ".metta",
              ".sh", ".js", ".ts", ".rs", ".go", ".c", ".h", ".cpp", ".hpp", ".java", ".scm", ".pl", ".lean", ".sql"}


def message_files(m: dict) -> list[dict]:
    """The files a Telegram message carries: a document, the largest size of a photo; other media by kind only."""
    out = []
    if m.get("document"):
        d = m["document"]
        out.append({"id": d["file_id"], "name": d.get("file_name") or f"file-{m['message_id']}",
                    "type": d.get("mime_type"), "bytes": d.get("file_size")})
    if m.get("photo"):
        p = max(m["photo"], key=lambda p: (p.get("file_size") or 0, p.get("width", 0) * p.get("height", 0)))
        out.append({"id": p["file_id"], "name": f"photo-{m['message_id']}.jpg", "type": "image/jpeg",
                    "bytes": p.get("file_size")})
    for kind in ("audio", "voice", "video", "video_note", "animation", "sticker"):
        if m.get(kind):
            out.append({"name": (m[kind].get("file_name") or kind), "type": m[kind].get("mime_type"),
                        "note": f"a {kind.replace('_', ' ')}: not downloaded, ask for it as a file"})
    return out


def fetch_file(chat_id, message_id, f: dict) -> Path:
    """Download one file into INBOX, once (named after its chat and message)."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", f["name"]).strip("._") or "file"
    dest = INBOX / f"{str(chat_id).lstrip('-')}-{message_id}-{safe_name[:100]}"
    if dest.is_file():
        return dest
    if (f.get("bytes") or 0) > INBOX_BYTES:
        raise ValueError("over 20 MB, Telegram doesn't hand those to a bot")
    info = tg("getFile", file_id=f["id"])
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}", timeout=120) as r:
        data = r.read(INBOX_BYTES + 1)
    if len(data) > INBOX_BYTES:
        raise ValueError("over 20 MB")
    INBOX.mkdir(mode=0o750, parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    tmp.chmod(0o640)
    tmp.replace(dest)
    prune_inbox()
    return dest


def prune_inbox() -> None:
    cut = time.time() - INBOX_KEEP_DAYS * 86400
    for p in INBOX.glob("*"):
        try:
            if p.stat().st_mtime < cut:
                p.unlink()
        except OSError:
            pass


def read_text(p: Path) -> dict:
    text = p.read_bytes().decode("utf-8", errors="replace")
    return {"text": text[:INLINE_CHARS],
            **({"truncated": f"first {INLINE_CHARS} of {len(text)} characters; the rest is in the file at path"}
               if len(text) > INLINE_CHARS else {})}


def pdf_text(p: Path) -> Path | str:
    """A PDF's text beside it (`<file>.txt`, pdftotext -layout from poppler-utils), so agents without a PDF reader
    read it as text; or why there is none."""
    out = p.with_name(p.name + ".txt")
    if out.is_file():
        return out
    try:
        r = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(p), str(out)], capture_output=True,
                           text=True, timeout=120)
    except FileNotFoundError:
        return "no text copy: pdftotext (poppler-utils) isn't installed"
    except subprocess.TimeoutExpired:
        out.unlink(missing_ok=True)
        return "no text copy: pdftotext took over 2 minutes"
    if r.returncode != 0 or not out.is_file():
        out.unlink(missing_ok=True)
        return f"no text copy: pdftotext failed ({(r.stderr or '').strip()[:200]})"
    out.chmod(0o640)
    if not out.read_text(errors="replace").strip():
        out.unlink()
        return "no text copy: the PDF has no text layer (scanned?); read it as a PDF at path"
    return out


def is_text(name: str, mime: str | None = None) -> bool:
    return Path(name).suffix.lower() in TEXT_TYPES or (mime or "").startswith("text/")


def files_of(m: dict, inline: bool = True) -> list[dict]:
    """The message's files for the chief of staff: name, type, size, the local path, and a text file's content.
    A file that can't be downloaded is listed with why, never silently dropped."""
    out = []
    for f in message_files(m):
        d = {k: f[k] for k in ("name", "type", "bytes", "note") if f.get(k)}
        if "id" in f:
            try:
                p = fetch_file(m["chat"]["id"], m["message_id"], f)
                d["path"] = str(p)
                if inline and is_text(f["name"], f.get("type")):
                    d.update(read_text(p))
                elif p.suffix.lower() == ".pdf" or f.get("type") == "application/pdf":
                    t = pdf_text(p)
                    if isinstance(t, Path):
                        d["text_path"] = str(t)
                        if inline:
                            d.update(read_text(t))
                    else:
                        d["note"] = t
            except Exception as e:
                d["note"] = f"couldn't download: {e}"
                log(f"attachment {f['name']!r} in message {m.get('message_id')}: {e}")
        out.append(d)
    return out


def files_block(files: list[dict]) -> str:
    """Attached files as text after a private message to the chief of staff."""
    parts = []
    for d in files:
        head = f"[attached file: {d['name']}" + (f", {d['path']}" if d.get("path") else "") + \
               (f"; its text: {d['text_path']}" if d.get("text_path") else "") + \
               (f"; {d['note']}" if d.get("note") else "") + (f"; {d['truncated']}" if d.get("truncated") else "") + "]"
        parts.append(head + (f"\n{d['text']}\n[end of {d['name']}]" if d.get("text") else ""))
    return "\n\n" + "\n\n".join(parts) if parts else ""


# The files a chat reply may attach: the chief of staff's workspace clone, and its outbox, a folder it copies other
# files into (from a task's result branch, say). Where cos's own folder is private to it (proto-hive: /var/lib/hive/cos,
# 700), the outbox is the only one the bridge can read: HIVE_COS_OUTBOX, a folder cos writes and the bridge's group
# reads. As cos sees them in a container: /cos/workspace and /cos/outbox.
COS_WORKSPACE = Path(os.environ.get("HIVE_COS_WORKSPACE") or hp.WORK_DIR / "cos" / "workspace")
COS_OUTBOX = Path(os.environ.get("HIVE_COS_OUTBOX") or hp.WORK_DIR / "cos" / "outbox")
OUTBOX_KEEP_DAYS = 7
ATTACH_MAX, ATTACH_BYTES, PHOTO_BYTES = 5, 50 * 1024 * 1024, 10 * 1024 * 1024   # Bot API: 50 MB a file, 10 MB a photo
PHOTO_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
FILE_TYPES = PHOTO_TYPES | {".gif", ".svg", ".pdf", ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".log", ".html",
                            ".yaml", ".yml", ".toml", ".xml", ".tex", ".bib", ".ipynb", ".py", ".patch", ".diff",
                            ".zip", ".gz", ".tgz", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".mp4", ".webm"}
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
    """The file a chat reply names, only if it is inside the chief of staff's workspace clone or its outbox: a path
    relative to the workspace, /cos/workspace/… or /cos/outbox/…, or the host path of either."""
    rel = raw.strip("`'\"")
    for prefix, base in (("/cos/workspace/", COS_WORKSPACE), ("/cos/outbox/", COS_OUTBOX)):
        if rel.startswith(prefix):
            cand = base / rel[len(prefix):]
            break
    else:
        cand = Path(rel) if rel.startswith("/") else COS_WORKSPACE / rel
    f = cand.resolve()
    base = next((b.resolve() for b in (COS_WORKSPACE, COS_OUTBOX) if f.is_relative_to(b.resolve())), None)
    if base is None or ".git" in f.relative_to(base).parts:
        raise ValueError("only files in the workspace or the outbox")
    if f.suffix.lower() not in FILE_TYPES:
        raise ValueError(f"type {f.suffix or '(none)'} not allowed")
    if not f.is_file():
        raise ValueError("no such file")
    if f.stat().st_size > ATTACH_BYTES:
        raise ValueError(f"over {ATTACH_BYTES // 2**20} MB, Telegram's limit for a bot")
    return f


def prune_outbox() -> None:
    """Files in the outbox go after OUTBOX_KEEP_DAYS, and folders left empty."""
    cut = time.time() - OUTBOX_KEEP_DAYS * 86400
    if not COS_OUTBOX.is_dir():
        return
    for p in sorted(COS_OUTBOX.rglob("*"), key=lambda q: -len(q.parts)):
        try:
            if p.is_file() and p.stat().st_mtime < cut:
                p.unlink()
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass


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
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        out = json.loads(e.read() or b"{}")
    if not out.get("ok"):
        raise RuntimeError(f"telegram {method}: {out.get('description', out)}")
    note_sent(method, params, out["result"])
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
            photo = kind == "photo" and f.suffix.lower() in PHOTO_TYPES and f.stat().st_size <= PHOTO_BYTES
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


ORIGIN_NOTES = {   # record event -> its step in the task's status message on its origin message
    "task.accepted": lambda e, d: "started",
    "task.blocked": lambda e, d: f"has a question ({d.get('needs')})",   # the question itself: send_question
    "task.result_posted": lambda e, d: "result posted, waiting for review",
    "review.recorded": lambda e, d: f"review {{n}} {d.get('verdict')}",
    "task.closed": lambda e, d: "done ✅",
    "task.cancelled": lambda e, d: f"cancelled: {cut(d.get('reason', ''), 300)}",
}
ORIGIN_KEEP = 7 * 86400   # a done task's origin is kept this long, for its PR link (telegram-say.py with HIVE_TASK)


def status_text(task: str, steps: list) -> str:
    """One line for the task's status message: its steps so far, a posted result shown only while it waits."""
    shown = [text for i, (kind, text) in enumerate(steps)
             if kind != "task.result_posted" or i == len(steps) - 1]
    return f"{task}: {' → '.join(shown)}"[:4000]


STATUS_EDIT_FOR = 47 * 3600   # Telegram lets a bot edit its group message for 48 hours


QUESTION_MAX = 1200   # a longer question is shortened in the chat when its question file goes with it
NEEDS = {"decision": "a decision", "information": "information", "access": "access", "external": "something outside the hive"}


def chunks(text: str, n: int = 3900) -> list[str]:
    """text in pieces of at most n characters (a Telegram message holds 4096), each ended at a paragraph, line or
    word break when one is near: nothing dropped."""
    out = []
    while len(text) > n:
        head = text[:n]
        i = next((j for brk in ("\n\n", "\n", " ") if (j := head.rfind(brk)) > n // 2), n)
        out.append(text[:i].rstrip())
        text = text[i:].lstrip()
    return out + [text] if text else out


def send_question(task: str, origin: dict, e: dict, st: dict | None = None) -> None:
    """A worker's question, as its own reply under the task's status message: a header saying what it needs, the
    question as the worker put it, then the worker's question file (options, recommendation, evidence). A long
    question is shortened in the chat only when that file goes with it; without the file it is sent whole."""
    d = e.get("data") or {}
    reason = (d.get("reason") or "").strip() or "(no question text; see the record)"
    pin = next((r.get("pin") for r in e.get("refs") or [] if r.get("rel") == "question"), None)
    full = result_text(pin or {})
    who = str((e.get("actor") or {}).get("id") or "").removeprefix("worker.")
    head = f"❓ {who or task} needs {NEEDS.get(d.get('needs'), 'an answer')}" + (f" on {task}" if who else "")
    c = card.parse(full or "")
    if c:   # the worker filled in a decision card: show it, the details stay in the file
        text = f"{card.render(c, head)}\n\nDetails in the file below. The chief of staff answers it, or brings it to you."
    else:
        body = cut(reason, QUESTION_MAX) if full else reason
        text = (f"{head}\n\n{body}\n\n"
                + ("The full question, with options and evidence, is in the file below. " if full else "")
                + "The chief of staff answers it, or brings it to you.")
    reply = {"reply_to_message_id": origin.get("status_id") or origin["message_id"], "allow_sending_without_reply": True}
    for i, part in enumerate(chunks(text)):
        m = safe("sendMessage", chat_id=origin["chat_id"], text=part, disable_web_page_preview=True, **(reply if i == 0 else {}))
        if i == 0 and c and st is not None:   # a card's answers as buttons an operator can tap (decision_buttons.py)
            offer_buttons(st, task, c, origin["chat_id"], m, part)
    if full:
        f = STATE.parent / "questions" / f"{task}-{e.get('position', 0)}.md"
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(full)
            tg_file("sendDocument", "document", f, chat_id=origin["chat_id"], caption=f"{task}: the full question", **reply)
        except Exception as ex:
            log(f"question of {task}: {ex}")


def show_status(task: str, origin: dict) -> None:
    """The task's one status message: posted on its origin message the first time, edited in place after. A
    message too old to edit, or one that can't be edited (deleted), is replaced by a new one, which then counts."""
    text = status_text(task, origin.get("steps") or [])
    if origin.get("status_id") and time.time() - origin.get("status_at", time.time()) < STATUS_EDIT_FOR:
        try:
            tg("editMessageText", chat_id=origin["chat_id"], message_id=origin["status_id"], text=text)
            return
        except Exception as e:
            if "not modified" in str(e):
                return
            log(f"status of {task}: {e}; posting a new one")
    m = safe("sendMessage", chat_id=origin["chat_id"], text=text, reply_to_message_id=origin["message_id"],
             allow_sending_without_reply=True)
    if m and m.get("message_id"):
        origin["status_id"], origin["status_at"] = m["message_id"], time.time()


def push_origins(st: dict) -> None:
    """Each chat-started task's progress, as one status message on the message it came from, edited as it moves;
    a question and the result as messages of their own."""
    o, touched = load_origins(), set()
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
        if e["type"] == "task.created" and e.get("on_proposal") and e.get("task") not in o:
            # a task a bot proposed from a chat message: its progress and result go back to that message
            src = next((x for x in evs if x["type"] == "proposal.submitted"
                        and (x.get("data") or {}).get("proposal_id") == e["on_proposal"]), None)
            if src is None:
                okp, outp = hive("events", "--type", "proposal.submitted")
                src = next((x for x in (json.loads(l) for l in outp.splitlines() if l.strip()) if okp
                            and (x.get("data") or {}).get("proposal_id") == e["on_proposal"]), None) if okp else None
            m = re.match(r"^telegram:(-?\d+)/(\d+)$", ((src or {}).get("cause") or {}).get("external") or "")
            if m:
                o[e["task"]] = {"chat_id": int(m.group(1)), "message_id": int(m.group(2))}
                log(f"origin of {e['task']}: proposal {e['on_proposal']}, message {m.group(2)} in {m.group(1)}")
        elif e["type"] == "task.created" and e.get("task") not in o:
            # a task filed citing a chat message (e.g. the chief of staff refiling a bot's proposal as its own order)
            m = re.match(r"^telegram:(-?\d+)/(\d+)$", (e.get("cause") or {}).get("external") or "")
            if m:
                o[e["task"]] = {"chat_id": int(m.group(1)), "message_id": int(m.group(2))}
                log(f"origin of {e['task']}: message {m.group(2)} in {m.group(1)}")
        t, f = e.get("task"), ORIGIN_NOTES.get(e["type"])
        if not (t and f and t in o):
            continue
        steps, d = o[t].setdefault("steps", []), e.get("data") or {}
        text = f(e, d).replace("{n}", str(1 + sum(k == "review.recorded" for k, _ in steps)))
        steps.append([e["type"], text])
        touched.add(t)
        if e["type"] == "task.blocked":   # a question needs someone: its own message, not only a step
            show_status(t, o[t])
            touched.discard(t)
            send_question(t, o[t], e, st)
        if e["type"] == "task.closed":
            show_status(t, o[t])
            touched.discard(t)
            send_result(t, o[t])
            o[t]["closed"] = time.time()
        if e["type"] == "task.cancelled":
            show_status(t, o[t])
            touched.discard(t)
            del o[t]
    for t in touched:
        show_status(t, o[t])
    for t in [t for t, x in o.items() if time.time() - x.get("closed", time.time()) > ORIGIN_KEEP]:
        del o[t]
    save_origins(o)
    save_state(st)


def result_text(pin: dict) -> str | None:
    """A workspace pin's file, from the workspace repository at its commit."""
    if (pin or {}).get("repository") != "workspace" or not pin.get("path"):
        return None
    commit = str(pin.get("commit_oid", "")).split(":")[-1]
    r = subprocess.run(["git", "-C", str(WORKSPACE), "show", f"{commit}:{pin['path']}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


SUMMARY_HEAD = re.compile(r"^(#{1,6})\s*summary\s*:?\s*$", re.I)
SUMMARY_MAX, FALLBACK_CHARS = 3500, 800   # a Telegram message holds 4096 characters


def cut(text: str, n: int) -> str:
    """text, at most about n characters, ended at a paragraph, line or word break when one is near."""
    if len(text) <= n:
        return text
    head = text[:n]
    for brk in ("\n\n", "\n", " "):
        i = head.rfind(brk)
        if i > n // 2:
            return head[:i].rstrip() + " …"
    return head.rstrip() + "…"


def report_summary(text: str) -> tuple[str, bool]:
    """A result report's "## Summary" section (any heading level), up to the next heading at its level or
    above, and True; a report without one gives its first ~800 characters and False."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = SUMMARY_HEAD.match(line.strip())
        if not m:
            continue
        body = []
        for nxt in lines[i + 1:]:
            h = re.match(r"^(#{1,6})\s", nxt)
            if h and len(h.group(1)) <= len(m.group(1)):
                break
            body.append(nxt)
        found = "\n".join(body).strip()
        if found:
            return cut(found, SUMMARY_MAX), True
    return cut(text.strip(), FALLBACK_CHARS), False


def send_result(task: str, origin: dict) -> None:
    """A chat-started task is done: its result goes back to the message it came from as two messages, the
    report's summary as text to read there, then the full report as a file to download, with the code it
    changed named (repository, commit, the task's branch)."""
    ok, out = hive("task", task)
    if not ok:
        return
    j = json.loads(out)
    t = j.get("task", j)
    cr = t.get("current_result") or {}
    text = result_text(cr.get("pin") or {})
    if not text:
        return
    code = []
    res = next((ev for ev in reversed(j.get("events") or []) if ev.get("event_id") == cr.get("event_id")), None)
    for r in (res or {}).get("refs") or []:
        if r.get("rel") == "code":
            code.append(f"{r['pin']['repository']} {str(r['pin']['commit_oid']).split(':')[-1][:10]} (branch hive/{task})")
    f = STATE.parent / "results" / f"{task}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    summary, found = report_summary(text)
    label = "Summary:" if found else "The report has no summary; it starts:"
    caption = f"{task}: the full report" + (f"; code: {', '.join(code)}" if code else "")

    def deliver(chat_id, reply_to, note=""):
        reply = {"reply_to_message_id": reply_to, "allow_sending_without_reply": "true"} if reply_to else {}
        tg("sendMessage", chat_id=chat_id, text=f"{task}: done{note}. {label}\n\n{summary}", disable_web_page_preview=True, **reply)
        tg_file("sendDocument", "document", f, chat_id=chat_id, caption=f"{caption}{note}"[:1000], **reply)

    try:
        deliver(origin["chat_id"], origin["message_id"])
        log(f"result of {task} sent to {origin['chat_id']}" + ("" if found else " (no summary in the report)"))
    except Exception as ex:
        # the origin may be a chat this bot can't post in (a private chat with another bot): the work group gets it
        work = next((c for c in CHATS.values() if c.get("mode", "all") == "all" and not c.get("private")), None)
        log(f"result of {task}: {ex}" + (f"; sending it to {work['chat_id']}" if work else ""))
        if work and work["chat_id"] != origin["chat_id"]:
            try:
                deliver(work["chat_id"], None, f" (asked in chat {origin['chat_id']}, which I can't post in)")
            except Exception as ex2:
                log(f"result of {task} to the work group: {ex2}")


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


# The chief of staff's Claude account at a usage or spend limit: claude prints a line such as "You've hit your
# individual spend limit · ask your admin to raise it ... resets 2pm (America/Los_Angeles)" instead of an answer.
COS_LIMIT_RE = re.compile(r"^(you(['’]ve| have) (hit|reached) your [\w ]*limit|claude (ai )?usage limit reached|"
                          r"credit balance is too low|api error\b.*\b(insufficient_quota|usage limit|spend limit))", re.I)
COS_LIMIT = STATE.parent / "cos-limit.json"   # {"since", "line", "tried", "told": [chat ids], "held": [items]}
COS_LIMIT_RETRY = 1200   # seconds between tries of the oldest held message while at the limit
COS_LIMIT_HOLD = 50      # held messages kept; past that the oldest background ping is dropped
COS_LIMIT_ADMIN = "https://claude.ai/admin-settings/usage"


def limit_line(reply: str) -> str | None:
    """The limit message, if the reply is nothing but one (short, every line a limit line); else None."""
    lines = [x.strip() for x in reply.splitlines() if x.strip()]
    if not lines or len(reply) > 600 or not all(COS_LIMIT_RE.search(x) for x in lines):
        return None
    return lines[0][:300]


def cos_limit() -> dict:
    try:
        return json.loads(COS_LIMIT.read_text())
    except (OSError, ValueError):
        return {}


def save_cos_limit(lim: dict) -> None:
    try:
        if lim:
            COS_LIMIT.write_text(json.dumps(lim))
        else:
            COS_LIMIT.unlink(missing_ok=True)
    except OSError as e:
        log(f"cos limit state: {e}")


def hold_for_limit(lim: dict, item: tuple) -> None:
    """Keep a message for when the chief of staff is back. A human writing in a chat other than the operators' (who
    got the alert) is told once per limit spell, instead of seeing nothing or the raw limit line."""
    cmd, text, human, target = item
    held = lim.setdefault("held", [])
    held.append([cmd, text, human, target])
    while len(held) > COS_LIMIT_HOLD:
        bg = next((i for i, h in enumerate(held) if not h[2]), 0)
        held.pop(bg)
    chat_id = (target or {}).get("chat_id") or ALLOW["chat_id"]
    told = lim.setdefault("told", [ALLOW["chat_id"]])
    if human and chat_id not in told:
        told.append(chat_id)
        thread = {"reply_to_message_id": target["reply_to"], "allow_sending_without_reply": True} \
            if target and target.get("reply_to") else {}
        safe("sendMessage", chat_id=chat_id, text="(The chief of staff is out of usage for now. Your message is saved "
             "and it will answer when it's back; the operators know.)", **thread)


def ask_cos(item: tuple) -> str:
    cmd, text, human, target = item
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
    return reply


def deliver(reply: str, target) -> None:
    reply = take_task_lines(reply, target)
    reply, target = route_reply(reply, target)
    chat_id = (target or {}).get("chat_id") or ALLOW["chat_id"]
    thread = {"reply_to_message_id": target["reply_to"], "allow_sending_without_reply": True} \
        if target and target.get("reply_to") else {}
    reply, found = split_attachments(reply)
    if is_noop(reply) and not found:
        log(f"chat: no-op reply not forwarded: {reply[:60]!r}")
        return
    if not is_noop(reply):
        for i, part in enumerate(chunks(reply)):
            safe("sendMessage", chat_id=chat_id, text=part, **(thread if i == 0 else {}))
    notes = send_attachments(found, chat_id)
    if notes:
        safe("sendMessage", chat_id=chat_id, text="\n".join(notes))
    if found:
        prune_outbox()


def chat_step(item: tuple | None) -> None:
    """One message to the chief of staff (item), or, with none waiting (None), a retry of the oldest message held
    while its account is at a usage limit. At a limit the operators are alerted once, messages are held (not run)
    and tried again every COS_LIMIT_RETRY seconds; when one is answered the operators hear it's back and the rest
    are answered in order."""
    lim = cos_limit()
    retry = item is None
    if retry:
        if not lim or not lim.get("held") or time.time() - lim.get("tried", 0) < COS_LIMIT_RETRY:
            return
        item = tuple(lim["held"][0])
    elif lim:
        hold_for_limit(lim, item)
        save_cos_limit(lim)
        log("chat: held (chief of staff at a usage limit)")
        return
    reply = ask_cos(item)
    line = limit_line(reply)
    if line:
        if not lim:
            lim = {"since": time.time(), "told": [ALLOW["chat_id"]], "held": []}
            safe("sendMessage", chat_id=ALLOW["chat_id"], text=(
                f"⛔ The chief of staff can't answer: its Claude account is at a usage limit.\n{line}\n\n"
                f"Messages to it are held and answered when it's back (it tries again every "
                f"{COS_LIMIT_RETRY // 60} min). To get it back sooner, raise the limit at {COS_LIMIT_ADMIN}"))
            log(f"chat: chief of staff at a usage limit: {line}")
        lim.update(line=line, tried=time.time())
        if not retry:
            hold_for_limit(lim, item)
        save_cos_limit(lim)
        return
    if lim:
        rest = lim.get("held", [])[1 if retry else 0:]
        save_cos_limit({})
        hours = (time.time() - lim.get("since", time.time())) / 3600
        safe("sendMessage", chat_id=ALLOW["chat_id"], text=(
            f"✅ The chief of staff is back after {hours:.1f} h"
            + (f"; answering {len(rest) + (1 if retry else 0)} held messages now." if rest or retry else ".")))
        log(f"chat: chief of staff back; {len(rest)} more held")
        for h in rest:
            CHAT.put(tuple(h))
    deliver(reply, item[3])


def chat_worker() -> None:
    """Chat with the chief of staff, one message at a time across every chat, off the main loop."""
    while True:
        try:
            item = CHAT.get(timeout=60)
        except queue.Empty:
            item = None
        try:
            chat_step(item)
        except Exception as e:   # keep the worker up
            log(f"chat: {e}")
        if item is not None:
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
    global REMIND_S, DIGEST_AT, CHAT_CMD
    CHAT_CMD = a.chat_cmd
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
                push_repo_requests(st)
                relay_floor_alerts(st)
                if a.chat_cmd:
                    ping_cos(st, a.chat_cmd)
                    check_followups(a.chat_cmd)
                    if BOARD_BOTS:
                        ping_cos_trouble(st, a.chat_cmd)
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
