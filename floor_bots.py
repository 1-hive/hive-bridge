"""What each bot is doing: the Floor's bot view, computed from facts by fixed rules (hive-bridge).

Pure functions over three sources, never mixed up:
- the record (authoritative): what each actor owes ("on its plate"), and the marks on its day;
- beacons from each agent's sidecar or runner (never authoritative): what its process is doing now,
  from its own files (an Iter agent's request queue and route hook, an OpenClaw agent's sessions);
- the bridge's dialogue log (outside the record): who asked what, in which chat, for the text of a line.

No model decides any of it, and no agent writes its own status. floor.py serves it; the Telegram
bridge posts /api/status as the work group's pinned message.
"""
from __future__ import annotations

import datetime as dt
import html
import re
import time

DAY = 24 * 3600
OFFLINE_AFTER = 150          # seconds without a beacon (they come every 30 s)
STEP_RECENT = 120            # a step this recent means the agent is working
STUCK_AFTER = 15 * 60        # a request in progress, no step for this long
SAMPLE_SPAN = 30             # each beacon sample stands for the 30 s before it
WORK_END = ("task.result_posted", "task.blocked", "task.closed", "task.cancelled", "task.released", "task.reassigned")

KINDS = {   # state -> (colour key, word for the day strip)
    "task": "task", "review": "task", "chat": "chat", "private": "chat", "own": "own",
    "wait": "idle", "idle": "idle", "offline": "offline", "cantrun": "offline", "stuck": "bad", "nodata": "idle",
}
EMOJI = {"task": "🟠", "review": "🟠", "chat": "🔵", "private": "🔵", "own": "🟣", "wait": "⚪", "idle": "⚪",
         "offline": "⚫", "cantrun": "🔴", "stuck": "🔴", "nodata": "▫️"}


def ts(s: str | None) -> float | None:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def dur(s: float | None) -> str:
    if s is None:
        return "?"
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h < 48 else f"{h // 24}d {h % 24}h"


def model_name(m: str | None) -> str:
    """claude-opus-5-5 -> Opus 5.5; gpt-6-astra stays."""
    if not m:
        return ""
    x = re.match(r"^(?:[a-z-]+/)?claude-([a-z]+)-(\d+)-(\d+)", m)
    return f"{x.group(1).capitalize()} {x.group(2)}.{x.group(3)}" if x else m.split("/")[-1]


def first_line(text: str, n: int = 140) -> str:
    line = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    line = re.sub(r"^@\w+[,:]?\s+", "", line)     # "@Protomegabot can you" -> "can you"
    return line if len(line) <= n else line[:n - 1].rstrip() + "…"


class Facts:
    """Everything the rules read, gathered once per request by floor.py."""

    def __init__(self, *, state: dict, events: list[dict], beacons: dict, samples: dict, dialogue: list[dict],
                 groups: dict, conf: dict, floor_up: list[float], cos: dict | None = None,
                 merged: dict | None = None, requests: list[str] | None = None, now: float | None = None):
        self.state, self.events, self.beacons, self.samples = state, events, beacons, samples
        self.dialogue, self.groups, self.conf, self.floor_up = dialogue, groups, conf, floor_up
        self.cos = cos or {}
        self.merged = merged or {}           # task -> False when its reviewed code isn't on main yet
        self.requests = requests or []       # the chief of staff's open requests to the operators (hive-requests/)
        self.now = now or time.time()
        self.chats = {c["chat_id"]: c for c in groups.get("chats", [])}
        self.humans = {h["user_id"]: h for h in groups.get("humans", [])}
        self.bot_ids = {b["user_id"]: b.get("actor") for b in groups.get("bots", []) if b.get("actor")}
        self.msgs = {(d.get("chat"), d.get("id")): d for d in dialogue}
        self.by_task: dict[str, list[dict]] = {}
        for e in events:
            if e.get("task"):
                self.by_task.setdefault(e["task"], []).append(e)

    # --- names -------------------------------------------------------------------------------
    def name(self, actor: str | None) -> str:
        if not actor:
            return "?"
        n = (self.conf.get("names") or {}).get(actor)
        return n or actor.split(".", 1)[-1]

    def chat_name(self, chat_id) -> str:
        c = self.chats.get(chat_id)
        if not c:
            return "another chat"
        return (self.conf.get("chat_names") or {}).get(c["name"]) or c["name"][:1].upper() + c["name"][1:]

    def person(self, d: dict) -> str:
        """The sender of a dialogue line, as people know them."""
        h = self.humans.get(d.get("from_id"))
        if h:
            return h["name"][:1].upper() + h["name"][1:]
        actor = self.bot_ids.get(d.get("from_id"))
        return self.name(actor) if actor else (d.get("from") or "someone")

    def origin(self, tid: str) -> dict | None:
        """The chat message a task was asked for in (its first event's cause, A5)."""
        for e in self.by_task.get(tid, [])[:2]:
            m = re.match(r"^telegram:(-?\d+)/(\d+)$", ((e.get("cause") or {}).get("external") or ""))
            if m:
                chat, mid = int(m.group(1)), int(m.group(2))
                d = self.msgs.get((chat, mid))
                return {"chat": chat, "message": mid, "chat_name": self.chat_name(chat),
                        "from": self.person(d) if d else None, "text": first_line(d["text"]) if d else None}
        return None

    def origin_text(self, tid: str) -> str:
        o = self.origin(tid)
        if not o:
            return ""
        return f"from {o['from']} in {o['chat_name']}" if o["from"] else f"from {o['chat_name']}"


# --- record ----------------------------------------------------------------------------------------

def work_windows(f: Facts, actor: str) -> list[tuple[float, float]]:
    """When the actor held an accepted task it was working on (accepted or unblocked or sent back by a
    failed review, until it posted a result, blocked, or lost the task)."""
    out = []
    for tid, evs in f.by_task.items():
        start, owner = None, None
        for e in evs:
            t, typ, d = ts(e["recorded_at"]), e["type"], e.get("data") or {}
            if typ in ("task.assigned", "task.reassigned"):
                owner = d.get("to")
                if start is not None and owner != actor:
                    out.append((start, t))
                    start = None
            if owner != actor:
                continue
            if typ in ("task.accepted", "task.unblocked") and start is None:
                start = t
            elif typ == "review.recorded" and d.get("verdict") != "passed" and start is None:
                start = t
            elif typ in WORK_END and start is not None:
                out.append((start, t))
                start = None
        if start is not None:
            out.append((start, f.now))
    return sorted(out)


def marks(f: Facts, actor: str, cls: str) -> list[list]:
    since = f.now - DAY
    out = []
    for e in f.events:
        t = ts(e["recorded_at"])
        if t < since:
            continue
        typ, d, who = e["type"], e.get("data") or {}, e["actor"]["id"]
        if typ == "actor.declared" and d.get("actor_id") == actor:
            out.append([t, "code", "declared: " + ((d.get("declaration") or {}).get("harness") or "")[:80]])
        elif typ == "task.result_posted" and who == actor:
            out.append([t, "result", f"result posted: {e.get('task')}"])
        elif typ in ("task.assigned", "task.reassigned") and d.get("to") == actor:
            out.append([t, "assigned", f"assigned: {e.get('task')}"])
        elif typ == "task.blocked" and who == actor:
            out.append([t, "question", f"asked: {first_line(d.get('reason', ''), 80)}"])
        elif typ == "review.recorded" and who == actor:
            out.append([t, "result", f"review {d.get('verdict')}: {e.get('task')}"])
    return out


def plate(f: Facts, actor: str, cls: str) -> list[dict]:
    """What the actor owes, from the record."""
    out = []
    tasks = f.state.get("tasks", {}).values()
    for t in sorted(tasks, key=lambda t: t.get("assigned_at") or t.get("created_at") or ""):
        st, ext = t["status"], t.get("ext") or {}
        if cls == "worker" and t.get("owner") == actor:
            if st == "assigned":
                since = ts(t.get("assigned_at"))
                left = (ext.get("lease") or {}).get("accept_within_seconds")
                sub = f"assigned {dur(f.now - since)} ago, not picked up yet"
                if left:
                    rem = since + left - f.now
                    sub += f"; accept within {dur(rem)}" if rem > 0 else f"; its lease ran out {dur(-rem)} ago"
                out.append({"stage": "Assigned", "task": t["id"], "sub": sub})
            elif st == "in_progress":
                la = ts((t.get("last_activity") or {}).get("at"))
                sub = f"last word {dur(f.now - la)} ago" if la else ""
                if ext.get("nudges_since_activity"):
                    sub += f"; nudged {ext['nudges_since_activity']}×"
                out.append({"stage": "Working", "task": t["id"], "sub": sub})
            elif st == "blocked":
                b = t.get("block") or {}
                sub = (f"its question was answered" if b.get("answered") else
                       f"needs {b.get('needs', '?')}: “{first_line(b.get('reason', ''), 100)}”")
                out.append({"stage": "Blocked", "task": t["id"], "sub": sub})
            elif st == "in_review":
                cr = t.get("current_result") or {}
                rv = t.get("assigned_reviewer")
                waited = f.now - ts(cr.get("at")) if cr.get("at") else None
                sub = (f"waiting for {f.name(rv)} for {dur(waited)}" if rv else f"waiting for a reviewer, {dur(waited)}")
                b = f.beacons.get(rv) or {}
                if rv and (b.get("body") or {}).get("problem"):
                    sub += f"; {f.name(rv)} can't run"
                out.append({"stage": "In review", "task": t["id"], "sub": sub})
            elif st == "done" and f.merged.get(t["id"]) is False:
                out.append({"stage": "Merge", "task": t["id"], "sub": "review passed; its code isn't on main yet"})
        elif cls == "reviewer" and t.get("assigned_reviewer") == actor and st == "in_review":
            cr = t.get("current_result") or {}
            lr = t.get("latest_review") or {}
            if lr.get("reviewer") == actor and lr.get("position", 0) > cr.get("position", 0):
                continue
            waited = f.now - ts(cr.get("at")) if cr.get("at") else None
            out.append({"stage": "To review", "task": t["id"],
                        "sub": f"{f.name(t.get('owner'))}'s result, waiting {dur(waited)}"})
    if cls == "chief_of_staff":
        blocked = [t for t in tasks if t["status"] == "blocked" and not (t.get("block") or {}).get("answered")]
        passed = [t for t in tasks if t["status"] == "in_review" and (t.get("latest_review") or {}).get("verdict") == "passed"]
        esc = [t for t in tasks if ((t.get("ext") or {}).get("escalation") or {}).get("to") == "chief_of_staff"]
        for t in blocked:
            out.append({"stage": "Question", "task": t["id"], "sub": f"from {f.name(t.get('owner'))}"})
        for t in passed:
            out.append({"stage": "To close", "task": t["id"], "sub": "review passed"})
        for t in esc:
            out.append({"stage": "Escalated", "task": t["id"], "sub": (t["ext"]["escalation"].get("reason") or "")[:100]})
    return out


# --- beacons ---------------------------------------------------------------------------------------

def request_line(f: Facts, req: dict) -> tuple[str, str, str]:
    """(state, headline, detail) for a chat request the agent is answering."""
    chat, mid = req.get("chat_id"), req.get("message_id")
    if chat is None:   # its sidecar can see a request in progress but not read it
        return "chat", "Answering a chat message", ""
    if isinstance(chat, int) and chat > 0:
        return "private", "In a private chat", "Content not shown: direct messages stay private."
    d = f.msgs.get((chat, mid))
    where = f.chat_name(chat)
    if d:
        return "chat", f"Answering {f.person(d)} in {where}", f"“{first_line(d.get('text', ''))}”"
    return "chat", f"Answering a message in {where}", ""


def now_line(f: Facts, actor: str, cls: str, plate_: list[dict]) -> dict:
    """The headline: the first rule that applies (States sheet: offline › can't run › stuck › task › chat › own ›
    waiting › idle)."""
    b = f.beacons.get(actor)
    body = (b or {}).get("body") or {}
    age = f.now - b["received"] if b else None
    working = [p for p in plate_ if p["stage"] == "Working"]
    blocked = [p for p in plate_ if p["stage"] == "Blocked"]
    if cls == "chief_of_staff":
        if f.cos.get("busy"):
            c = f.cos["busy"]
            where = f.chat_name(c.get("chat")) if c.get("chat") else "the operators' chat"
            return {"state": "task", "headline": f"Handling a message in {where}", "since": c.get("since"),
                    "facts": f"{dur(f.now - (c.get('since') or f.now))}", "active": True}
        last = f.cos.get("last")
        return {"state": "idle", "headline": "Idle", "since": last,
                "detail": f"last turn {dur(f.now - last)} ago" if last else "", "active": False}
    if not b:
        said = last_said(f, actor)
        sender = "runner" if cls == "reviewer" else "sidecar"
        if said:
            return {"state": "nodata", "headline": f"Last spoke in {said['where']} {dur(f.now - said['at'])} ago",
                    "detail": f"No activity data: its {sender} sends no beacons yet.", "active": False}
        return {"state": "nodata", "headline": "No activity data",
                "detail": f"Its {sender} sends no beacons yet; what it owes below is still the record's.", "active": False}
    if age > OFFLINE_AFTER:
        return {"state": "offline", "headline": f"Offline for {dur(age)}", "since": b["received"],
                "detail": f"No word from its {'runner' if cls == 'reviewer' else 'sidecar'} since "
                          f"{time.strftime('%H:%M', time.gmtime(b['received']))} UTC.", "active": False}
    step = body.get("last_step")
    ep = next((e for e in reversed(body.get("episodes") or []) if e[1] is None), None)
    model = model_name(ep[2] if ep else None) or model_name(((f.state.get("actors") or {}).get(actor, {})
                                                             .get("declaration") or {}).get("model_route"))
    if body.get("kind") == "reviewer" and body.get("problem") and not body.get("reviewing"):
        return {"state": "cantrun", "headline": "Can't run", "detail": body["problem"][:200], "active": False}
    if body.get("reviewing"):
        r = body["reviewing"]
        return {"state": "review", "headline": f"Reviewing {r.get('task')}",
                "detail": f"{f.name((f.state['tasks'].get(r.get('task')) or {}).get('owner'))}'s result",
                "since": r.get("since"), "facts": dur(f.now - (r.get("since") or f.now)), "active": True}
    reqs = body.get("requests") or []
    if reqs:
        r = min(reqs, key=lambda r: r.get("since") or f.now)
        state, head, detail = request_line(f, r)
        since = r.get("since") or f.now
        quiet = f.now - step if step else None
        if f.now - since > STUCK_AFTER and (quiet is None or quiet > STUCK_AFTER):
            return {"state": "stuck", "headline": "Maybe stuck", "since": since,
                    "detail": f"{head}, but no step for {dur(quiet)}", "active": False}
        facts = [dur(f.now - since)] + ([model] if model else []) + ([f"last step {dur(quiet)} ago"] if quiet is not None else [])
        if len(reqs) > 1:
            facts.append(f"{len(reqs)} requests open")
        return {"state": state, "headline": head, "detail": detail, "since": since, "facts": " · ".join(facts), "active": True}
    busy = ep is not None or (step and f.now - step < STEP_RECENT)
    if busy:
        since = ep[0] if ep else step
        facts = [dur(f.now - since)] + ([model] if model else []) + ([f"last step {dur(f.now - step)} ago"] if step else [])
        if working:
            tid = working[0]["task"]
            o = f.origin_text(tid)
            return {"state": "task", "headline": f"Working on {tid}",
                    "detail": ((f.state["tasks"][tid].get("title") or "") + (f" · asked {o}" if o else "")),
                    "since": since, "facts": " · ".join(facts), "active": True}
        if body.get("chat") and body["chat"].get("chat_id") is not None:
            state, head, detail = request_line(f, {"chat_id": body["chat"]["chat_id"]})
            return {"state": state, "headline": head.replace("Answering a message in", "Chatting in"), "detail": detail,
                    "since": since, "facts": " · ".join(facts), "active": True}
        return {"state": "own", "headline": "On its own agenda",
                "detail": "Working on something that came neither from chat nor from the hive.",
                "since": since, "facts": " · ".join(facts), "active": True}
    last = max([x for x in (step, body.get("last_answer")) if x] or [0]) or None
    if blocked:
        return {"state": "wait", "headline": "Waiting for an answer", "detail": blocked[0]["sub"],
                "since": last, "facts": f"idle {dur(f.now - last)}" if last else "", "active": False}
    said = last_said(f, actor)
    detail = (f"Last: spoke in {said['where']} at {time.strftime('%H:%M', time.gmtime(said['at']))} UTC" if said else "")
    return {"state": "idle", "headline": f"Idle for {dur(f.now - last)}" if last else "Idle", "detail": detail,
            "since": last, "facts": "free for record work" if not working else "", "active": False}


def last_said(f: Facts, actor: str) -> dict | None:
    ids = [u for u, a in f.bot_ids.items() if a == actor]
    for d in reversed(f.dialogue):
        if d.get("from_id") in ids:
            return {"where": f.chat_name(d.get("chat")), "at": ts(d.get("at")), "text": first_line(d.get("text", ""))}
    return None


# --- the day strip -----------------------------------------------------------------------------------

def day(f: Facts, actor: str, cls: str) -> dict:
    """The last 24 hours as segments [start, end, kind], kind one of task, chat, own, offline."""
    since = f.now - DAY
    wins = work_windows(f, actor)
    in_work = lambda t: any(a <= t <= b for a, b in wins)
    overlaps = lambda x, y: any(a <= y and x <= b for a, b in wins)
    body = ((f.beacons.get(actor) or {}).get("body") or {})
    samples = [s for s in f.samples.get(actor, []) if s["t"] >= since]
    segs = []
    if cls == "chief_of_staff":
        segs = [[max(a, since), b or f.now, "task"] for a, b in f.cos.get("turns", []) if (b or f.now) >= since]
    elif body.get("episodes") is not None:
        answers = body.get("answers") or []
        chat_samples = [s["t"] for s in samples if s.get("req")]
        for a, b, _m in body.get("episodes") or []:
            b = b or f.now
            if b < since:
                continue
            # a chat request answered in it, else a task held during it (accepted seconds after it began), else its own
            kind = ("chat" if any(a - 5 <= x <= b + 120 for x in answers) or any(a <= x <= b + SAMPLE_SPAN for x in chat_samples)
                    else "task" if overlaps(a, b) else "own")
            segs.append([max(a, since), b, kind])
    else:
        for s in samples:
            active = s.get("req") or s.get("rev") or (s.get("step") and s["t"] - s["step"] < 90)
            if not active:
                continue
            kind = "task" if (s.get("rev") or in_work(s["t"])) else "chat" if (s.get("req") or s.get("chat")) else "own"
            segs.append([s["t"] - SAMPLE_SPAN, s["t"], kind])
    # what the beacons say about being reachable, while the Floor was up to hear them
    if samples or f.beacons.get(actor):
        times = [s["t"] for s in samples]
        prob = [s for s in samples if s.get("prob") and not s.get("rev")]
        for s in prob:
            segs.append([s["t"] - SAMPLE_SPAN, s["t"], "offline"])
        up = [t for t in f.floor_up if t >= since]
        points = sorted(times + [f.now])
        prev = times[0] if times else None
        for t in points[1:] if times else []:
            if t - prev > OFFLINE_AFTER and any(prev < u < t for u in up):
                segs.append([prev + SAMPLE_SPAN, t if t != f.now else f.now, "offline"])
            prev = t
    segs = merge(sorted(segs))
    split = {"task": 0.0, "chat": 0.0, "own": 0.0, "offline": 0.0}
    for a, b, k in segs:
        split[k] = split.get(k, 0) + (b - a)
    split["idle"] = max(0.0, DAY - sum(split.values()))
    return {"from": since, "to": f.now, "segs": segs, "marks": marks(f, actor, cls), "split": split}


def merge(segs: list[list]) -> list[list]:
    out: list[list] = []
    for a, b, k in segs:
        if out and out[-1][2] == k and a <= out[-1][1] + 45:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b, k])
    return out


# --- the roster ------------------------------------------------------------------------------------

def initials(name: str) -> str:
    caps = re.findall(r"[A-Z]", name)
    digit = re.findall(r"\d", name)
    if digit:
        return (caps[0] if caps else name[0].upper()) + digit[-1]
    return "".join(caps[:2]) if len(caps) >= 2 else name[:2].capitalize()


def harness(decl: dict) -> str:
    h = (decl.get("harness") or "").split()
    w = h[0].lower() if h else ""
    return {"iter": "Iter", "openclaw": "OpenClaw", "claude-code": "Claude Code", "codex": "Codex"}.get(w, w)


def roster_order(f: Facts) -> list[str]:
    actors = f.state.get("actors", {})
    want = [a for a, v in actors.items() if v.get("status", "active") == "active"
            and v.get("class") in ("worker", "reviewer", "chief_of_staff")]
    order = f.conf.get("order") or list((f.conf.get("names") or {}).keys())
    rank = {a: i for i, a in enumerate(order)}
    cls_rank = {"worker": 0, "reviewer": 1, "chief_of_staff": 2}
    return sorted(want, key=lambda a: (cls_rank.get(actors[a]["class"], 3), rank.get(a, 99), a))


def machine_of(f: Facts, actor: str) -> str | None:
    for m, actors in (f.conf.get("machines") or {}).items():
        if actor in actors:
            return m
    return None


def bot(f: Facts, actor: str, detail: bool = False) -> dict:
    a = f.state["actors"][actor]
    cls, decl = a["class"], a.get("declaration") or {}
    name = f.name(actor)
    p = plate(f, actor, cls)
    now = now_line(f, actor, cls, p)
    body = ((f.beacons.get(actor) or {}).get("body") or {})
    d = day(f, actor, cls)
    code = [m for m in d["marks"] if m[1] == "code"]
    out = {
        "id": actor, "name": name, "class": cls, "initials": (f.conf.get("initials") or {}).get(actor) or initials(name),
        "machine": machine_of(f, actor), "harness": harness(decl), "model": model_name(decl.get("model_route")),
        "about": (f.conf.get("about") or {}).get(actor, ""), "now": {**now, "kind": KINDS.get(now["state"], "idle")},
        "plate": p, "queued": body.get("queued") or 0, "day": d,
        "code": {"at": code[-1][0], "text": f"own code changed at {time.strftime('%H:%M', time.gmtime(code[-1][0]))} UTC"}
        if code else None,
        "decl": decl.get("harness") or "",
        "note": body.get("problem") if body.get("kind") != "reviewer" else None,   # e.g. its sidecar can't read a file
        "beacon_age": (f.now - f.beacons[actor]["received"]) if actor in f.beacons else None,
    }
    if detail:
        out["events"] = timeline(f, actor, cls)
    return out


def timeline(f: Facts, actor: str, cls: str) -> list[dict]:
    """What the actor did in the last 24 hours, newest first, each line with its source."""
    since = f.now - DAY
    out = []
    owned = {t["id"] for t in f.state.get("tasks", {}).values() if t.get("owner") == actor or t.get("assigned_reviewer") == actor}
    for e in f.events:
        t = ts(e["recorded_at"])
        if t < since:
            continue
        typ, d, who, tid = e["type"], e.get("data") or {}, e["actor"]["id"], e.get("task")
        mine = who == actor or tid in owned or d.get("actor_id") == actor or d.get("to") == actor
        if not mine or typ.startswith(("route.", "gateway.")):
            continue
        out.append({"at": t, "src": "record", "text": event_text(f, e, actor)})
    for d in f.dialogue:
        t = ts(d.get("at"))
        if t and t >= since and f.bot_ids.get(d.get("from_id")) == actor:
            out.append({"at": t, "src": "chat", "text": f"Said in {f.chat_name(d.get('chat'))}: “{first_line(d.get('text', ''), 120)}”"})
    body = ((f.beacons.get(actor) or {}).get("body") or {})
    for a, b, m in body.get("episodes") or []:
        if (b or f.now) >= since:
            out.append({"at": a, "src": "log", "text": f"Worked {dur((b or f.now) - a)}" + (f" on {model_name(m)}" if m else "")
                        + ("" if b else " (still going)")})
    for s in transitions(f, actor):
        out.append(s)
    out.sort(key=lambda x: -x["at"])
    return out[:80]


def transitions(f: Facts, actor: str) -> list[dict]:
    out, prev = [], None
    up = f.floor_up
    for s in f.samples.get(actor, []):
        if prev and s["t"] - prev > OFFLINE_AFTER and any(prev < u < s["t"] for u in up):
            out.append({"at": prev, "src": "log", "text": "Went quiet: no beacon"})
            out.append({"at": s["t"], "src": "log", "text": f"Back: beacons again after {dur(s['t'] - prev)}"})
        prev = s["t"]
    return out


def event_text(f: Facts, e: dict, actor: str) -> str:
    typ, d, who, tid = e["type"], e.get("data") or {}, e["actor"]["id"], e.get("task") or ""
    by = "" if who == actor else f" ({f.name(who)})"
    o = f.origin_text(tid) if typ == "task.assigned" else ""
    texts = {
        "task.assigned": f"Assigned {tid}{by}" + (f", asked {o}" if o else ""),
        "task.reassigned": f"Reassigned {tid}{by}",
        "task.accepted": f"Accepted {tid}",
        "task.reported": f"{(d.get('kind') or 'report').capitalize()} on {tid}",
        "task.blocked": f"Blocked on {tid}, needs {d.get('needs')}: “{first_line(d.get('reason', ''), 100)}”",
        "task.answered": f"Its question on {tid} was answered{by}",
        "task.unblocked": f"Continued {tid}",
        "task.result_posted": f"Posted its result for {tid}",
        "review.assigned": f"Review of {tid} assigned to {f.name(d.get('reviewer'))}{by}",
        "review.recorded": f"Review of {tid}: {d.get('verdict')}{by}",
        "task.closed": f"{tid} closed{by}",
        "task.cancelled": f"{tid} cancelled{by}: {first_line(d.get('reason', ''), 80)}",
        "task.nudged": f"Nudged on {tid} by the supervisor",
        "task.escalated": f"{tid} escalated to {d.get('to')}: {first_line(d.get('reason', ''), 80)}",
        "actor.declared": "Re-declared: " + ((d.get("declaration") or {}).get("harness") or "")[:100],
        "actor.registered": "Registered on the hive",
        "task.created": f"{tid} created{by}" + (f", asked {f.origin_text(tid)}" if f.origin_text(tid) else ""),
        "task.escalation_resolved": f"Escalation on {tid} resolved{by}: {first_line(d.get('resolution', ''), 80)}",
    }
    return texts.get(typ, f"{typ} {tid}".strip() + by)


# --- the rest of the page --------------------------------------------------------------------------

def needs(f: Facts, inbox: list[dict], cos_inbox: list[dict], roster: list[dict]) -> list[dict]:
    """What the humans should act on, by fixed rules."""
    out = []
    for i in inbox:
        out.append({"tone": "bad" if "escalation" in i["kind"] else "warn", "title": i.get("title") or i["kind"],
                    "text": i["kind"].replace("_", " ") + f" · {i['entity']} {i['id']}", "who": "operators"})
    for i in cos_inbox:
        if i["kind"] != "task_blocked_unanswered_any":
            continue
        t = f.state["tasks"].get(i["id"]) or {}
        b = t.get("block") or {}
        since = ts((next((e for e in reversed(f.by_task.get(i["id"], [])) if e["type"] == "task.blocked"), {}) or {}).get("recorded_at"))
        if since and f.now - since < 30 * 60:
            continue
        out.append({"tone": "warn", "title": f"{f.name(t.get('owner'))} asks: {first_line(b.get('reason', ''), 100)}",
                    "text": f"needs {b.get('needs')} on {i['id']}; unanswered for {dur(f.now - since) if since else '?'}",
                    "who": "operators"})
    for b in roster:
        if b["class"] != "reviewer" or not b["plate"] or b["now"]["state"] not in ("cantrun", "offline", "nodata"):
            continue
        waits = [f.now - ts((t.get("current_result") or {}).get("at")) for t in f.state.get("tasks", {}).values()
                 if t.get("assigned_reviewer") == b["id"] and t["status"] == "in_review" and (t.get("current_result") or {}).get("at")]
        oldest = max(waits, default=0)
        n = f"{len(b['plate'])} result{'s' if len(b['plate']) > 1 else ''} waiting, the oldest {dur(oldest)}"
        if b["now"]["state"] == "nodata":
            if oldest > 3600:   # no beacons: maybe it isn't running at all
                out.append({"tone": "bad", "title": f"{b['name']} hasn't reviewed for {dur(oldest)}",
                            "text": f"{n}; its runner sends no beacons: is it running?", "who": "operators"})
        else:
            out.append({"tone": "bad", "title": f"{b['name']} can't review",
                        "text": f"{b['now'].get('detail') or b['now']['headline']} · {n}", "who": "operators"})
    for tid, merged in f.merged.items():
        if merged is False:
            t = f.state["tasks"][tid]
            out.append({"tone": "ok", "title": f"Merge {tid}", "text": f"{f.name(t.get('owner'))}'s code passed review; "
                        f"its branch hive/{tid} isn't on main yet: open and merge the pull request (a merge commit), "
                        "or delete the branch if it shouldn't be merged", "who": "operators"})
    for name in f.requests:
        out.append({"tone": "warn", "title": f"Request from the chief of staff: {name.replace('-', ' ')}",
                    "text": f"hive-requests/{name}.md in the workspace; move it to hive-requests/done/ once handled",
                    "who": "operators"})
    return out


STAGES = [("Assigned, not picked up", "assigned"), ("Working", "in_progress"), ("Blocked on a question", "blocked"),
          ("In review", "in_review"), ("Passed, waiting for a merge", "merge"), ("Done today", "done")]


def work(f: Facts) -> list[dict]:
    out = []
    tasks = list(f.state.get("tasks", {}).values())
    for name, key in STAGES:
        items = []
        for t in tasks:
            st = t["status"]
            if key == "merge":
                hit = st == "done" and f.merged.get(t["id"]) is False
            elif key == "done":
                closed = ts((next((e for e in reversed(f.by_task.get(t["id"], []))
                                   if e["type"] in ("task.closed", "task.cancelled")), {}) or {}).get("recorded_at"))
                hit = st in ("done", "cancelled") and closed and f.now - closed < DAY and f.merged.get(t["id"]) is not False
            else:
                hit = st == key
            if not hit:
                continue
            since = ts(t.get("assigned_at") if key == "assigned" else
                       (t.get("current_result") or {}).get("at") if key == "in_review" else
                       (t.get("last_activity") or {}).get("at"))
            line = [f.name(t.get("owner"))]
            o = f.origin_text(t["id"])
            if o:
                line.append(o)
            if key == "in_review":
                line.append(f"waiting for {f.name(t.get('assigned_reviewer'))}" if t.get("assigned_reviewer") else "no reviewer yet")
            if key == "blocked":
                line.append(first_line((t.get("block") or {}).get("reason", ""), 80))
            items.append({"id": t["id"], "title": t.get("title") or "", "time": dur(f.now - since) if since else "",
                          "line": " · ".join(line)})
        out.append({"name": name, "key": key, "items": items})
    return out


def chats(f: Facts) -> list[dict]:
    """Each logged chat's last 12 hours: messages per hour, who spoke, the last line."""
    out = []
    for c in f.groups.get("chats", []):
        rows = [d for d in f.dialogue if d.get("chat") == c["chat_id"]]
        recent = [d for d in rows if (ts(d.get("at")) or 0) >= f.now - 12 * 3600]
        bars = [0] * 12
        for d in recent:
            bars[min(11, int((ts(d["at"]) - (f.now - 12 * 3600)) // 3600))] += 1
        who = []
        for d in reversed(recent):
            p = f.person(d)
            if p not in who:
                who.append(p)
        last = rows[-1] if rows else None
        out.append({"name": f.chat_name(c["chat_id"]), "who": who[:6], "count": len(recent), "bars": bars,
                    "last_at": ts(last["at"]) if last else None,
                    "last": f"{f.person(last)}: {first_line(last.get('text', ''), 120)}" if last else ""})
    return out


def machines(f: Facts, roster: list[dict]) -> list[dict]:
    out = []
    by = {b["id"]: b for b in roster}
    for m, actors in (f.conf.get("machines") or {}).items():
        mine = [by[a] for a in actors if a in by]
        heard = [b for b in mine if b["beacon_age"] is not None]
        if not heard:
            status = "no beacons yet"
            ok = None
        elif all(b["beacon_age"] > OFFLINE_AFTER for b in heard):
            status = f"unreachable for {dur(min(b['beacon_age'] for b in heard))}"
            ok = False
        else:
            status, ok = "online", True
        out.append({"name": m, "status": status, "ok": ok, "runs": [b["name"] for b in mine]})
    return out


def status(f: Facts, roster: list[dict], needs_: list[dict]) -> dict:
    """The work group's pinned message (HTML), and its first line on its own."""
    working = sum(1 for b in roster if b["now"]["state"] in ("task", "review") and b["class"] != "chief_of_staff")
    chatting = sum(1 for b in roster if b["now"]["state"] in ("chat", "private"))
    bad = [b["name"] for b in roster if b["now"]["state"] in ("cantrun", "stuck", "offline")]
    parts = [f"{working} working"] + ([f"{chatting} chatting"] if chatting else [])
    if needs_:
        parts.append(f"{len(needs_)} for you")
    word = {"cantrun": "can't run", "stuck": "maybe stuck", "offline": "offline"}
    parts += [f"{b['name']} {word[b['now']['state']]}" for b in roster if b["now"]["state"] in word]
    head = "Hive now: " + " · ".join(parts)
    e = lambda s: html.escape(s, quote=False)
    lines = [f"<b>{e(head)}</b>"]
    for b in roster:
        n = b["now"]
        bit = n["headline"][:1].lower() + n["headline"][1:]
        tail = dur(f.now - n["since"]) if n.get("since") and n["state"] in ("chat", "private", "task", "review") else ""
        lines.append(f"{EMOJI.get(n['state'], '▫️')} <b>{e(b['name'])}</b> {e(bit)}" + (f", {tail}" if tail else ""))
    wait = []
    for b in roster:
        for p in b["plate"]:
            if p["stage"] == "Assigned":
                t = f.state["tasks"].get(p["task"]) or {}
                wait.append(f"{p['task']} waits for {b['name']} to pick it up ({dur(f.now - ts(t.get('assigned_at')))})"
                            if t.get("assigned_at") else f"{p['task']} waits for {b['name']}")
            elif p["stage"] == "In review":
                t = f.state["tasks"].get(p["task"]) or {}
                at = ts((t.get("current_result") or {}).get("at"))
                wait.append(f"{p['task']} waits for {f.name(t.get('assigned_reviewer'))}" + (f" ({dur(f.now - at)})" if at else ""))
    if wait:
        lines.append("\n⏳ " + e(" · ".join(wait))[:900])
    if needs_:
        lines.append("👉 <b>For you:</b> " + e(" · ".join(n["title"] for n in needs_))[:900])
    lines.append(f"<i>{time.strftime('%H:%M', time.gmtime(f.now))} UTC · this message updates itself</i>")
    return {"text": "\n".join(lines), "head": head, "at": f.now}
