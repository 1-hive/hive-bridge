#!/usr/bin/env python3
"""1-hive Floor: a read-only live board of the hive, for the operator's browser.

    floor.py [--port 8476] [--bind 127.0.0.1]

Run with the hiverecord tool's Python (it imports the record client). It serves
tools/floor/index.html and a small JSON API:

  /api/events?after=N   the record, compacted (position, time, actor, type, goal, task, note)
  /api/state?at=N       the board at a position: the gateway's own fold, trimmed
  /api/live             what each agent with an open task is doing now, from its attempt log
  /api/task/<id>        a task's history from the record, plus its agent's recent tool calls

Two sources, never mixed up:
- the record (authoritative): goals, tasks, owners, reviews, budgets, escalations.
  Reads are signed as actor `floor` (class instrument), which never emits;
- each attempt's log in its task folder (worker-N.jsonl, review-N.jsonl, codex-N.log):
  the agent's last tool call, activity per minute, tokens. Never authoritative.

Loopback only by default. It holds no key that can approve anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from hiverecord.client import Client, HiveError
from hiverecord.keys import SigningKey

HERE = Path(__file__).resolve().parent
PAGE = HERE / "floor" / "index.html"
WORK = Path.home() / "work" / "1hive"
ROUTE = WORK / ".route"
CFG = Path(os.environ.get("HIVE_CONFIG_DIR", Path.home() / ".config" / "hive"))

client = Client(os.environ.get("HIVE_URL", "http://127.0.0.1:8470"), os.environ.get("HIVE_ID", "1-hive"),
                SigningKey.from_file(os.environ.get("FLOOR_KEY_FILE", str(CFG / "floor.key"))))

NOTE_KEYS = ("kind", "verdict", "code", "to", "reviewer", "reason", "resolution", "outcome", "note", "title")


def compact(e: dict, goal_of: dict) -> dict:
    d = e.get("data") or {}
    note = ""
    for k in NOTE_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v:
            note = v
            break
    if e["type"] == "goal.revised" and d.get("budget"):
        note = f"budget {json.dumps(d['budget'])}" + (f": {d['reason']}" if d.get("reason") else "")
    task = e.get("task") or d.get("task") or (d.get("refused") or {}).get("task")
    return {"p": e["position"], "at": e["recorded_at"], "actor": e["actor"]["id"], "cls": e["actor"]["class"],
            "type": e["type"], "task": task, "goal": e.get("goal") or goal_of.get(task or ""),
            "note": note[:160]}


def goal_map() -> dict:
    return {t["id"]: t.get("goal") for t in client.state()["tasks"].values()}


def api_events(q: dict) -> dict:
    after = int(q.get("after", ["0"])[0])
    gm = goal_map()
    evs = [compact(e, gm) for e in client.all_events(after=after)]
    return {"head": client.head, "events": evs}


def iso(s: str | None) -> float | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def api_state(q: dict) -> dict:
    at = q.get("at", [None])[0]
    out = client.get("/v1/state", {"at": int(at) if at else None})
    s, pos = out["state"], out["at"]
    window = pos - 80   # recently finished goals stay on the board for a while
    goals = {}
    for g in s["goals"].values():
        recent = (g.get("status_since") or 0) > window
        if g["status"] in ("proposed", "active", "completed") or recent:
            goals[g["id"]] = {k: g.get(k) for k in ("id", "title", "status", "budget", "approved_at",
                                                     "escalation", "relevance", "spent", "status_since")}
    tasks = {}
    for t in s["tasks"].values():
        if t.get("goal") not in goals:
            continue
        ext = t.get("ext") or {}
        rev = t.get("latest_review") or {}
        tasks[t["id"]] = {"id": t["id"], "goal": t["goal"], "title": t.get("title"), "status": t["status"],
                          "owner": t.get("owner"), "reviewer": t.get("assigned_reviewer"),
                          "attempt": ext.get("attempt"), "restarts": ext.get("restarts", 0),
                          "nudges": ext.get("nudges_since_activity", 0), "escalation": ext.get("escalation"),
                          "block": t.get("block"), "reviews_failed": t.get("reviews_failed_count", 0),
                          "last_activity": t.get("last_activity"), "status_since": t.get("status_since"),
                          "verdict": rev.get("verdict")}
    inbox = client.inbox("operator") if at is None else []
    return {"at": pos, "head": client.head, "goals": goals, "tasks": tasks, "inbox": inbox}


# --- agent activity, from attempt logs ---------------------------------------------------------

LOG_RE = re.compile(r"^(worker|review|codex)-(\d+)\.(jsonl|log)$")


def latest_log(task: str) -> tuple[Path, str] | None:
    """The newest attempt log for a task, worker or reviewer side."""
    best = None
    for d, role in ((WORK / task, "worker"), (WORK / f"{task}-review", "reviewer")):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if LOG_RE.match(f.name):
                m = f.stat().st_mtime
                if best is None or m > best[0]:
                    best = (m, f, role)
    return (best[1], best[2]) if best else None


def attempt_meta(task: str, role: str) -> dict:
    d = ROUTE / (task if role == "worker" else f"{task}-review")
    files = sorted(d.glob("*.attempt.json"), key=lambda f: f.stat().st_mtime) if d.is_dir() else []
    if not files:
        return {}
    a = json.loads(files[-1].read_text())
    return {k: a.get(k) for k in ("attempt", "model", "effort", "harness", "route_id", "started_at")}


def tool_summary(name: str, inp: dict) -> str:
    for k in ("description", "command", "file_path", "pattern", "url", "query", "prompt"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return " ".join(v.split())[:140]
    return ""


_cache: dict = {}


def read_claude_log(f: Path) -> dict:
    """Parse a stream-json log; cached by (size, mtime)."""
    st = f.stat()
    key = (str(f), st.st_size, st.st_mtime)
    if _cache.get(str(f), (None,))[0] == key:
        return _cache[str(f)][1]
    calls, minutes, out_tokens, cost, last_text, done = [], Counter(), 0, None, "", False
    for line in f.open(errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts = d.get("timestamp")
        if ts:
            minutes[int(iso(ts) // 60)] += 1
        if d.get("type") == "assistant":
            msg = d.get("message") or {}
            out_tokens += (msg.get("usage") or {}).get("output_tokens", 0)
            for c in msg.get("content") or []:
                if c.get("type") == "tool_use":
                    calls.append([ts, c.get("name"), tool_summary(c.get("name", ""), c.get("input") or {})])
                elif c.get("type") == "text" and c.get("text", "").strip():
                    last_text = " ".join(c["text"].split())[:220]
        elif d.get("type") == "result":
            cost, done = d.get("total_cost_usd"), True
    out = {"calls": calls[-14:], "tool_calls": len(calls), "minutes": dict(minutes), "output_tokens": out_tokens,
           "cost_usd": cost, "last_text": last_text, "finished": done}
    _cache[str(f)] = (key, out)
    return out


def read_codex_log(f: Path) -> dict:
    lines = [l.rstrip() for l in f.open(errors="replace")][-400:]
    calls, tokens = [], 0
    for i, l in enumerate(lines):
        if l.startswith("exec") and i + 1 < len(lines):
            calls.append([None, "exec", " ".join(lines[i + 1].split())[:140]])
        if l == "tokens used" and i + 1 < len(lines):
            tokens = int(re.sub(r"\D", "", lines[i + 1]) or 0)
    text = next((l for l in reversed(lines) if l and not l.startswith(("+", "-", "@@"))), "")
    return {"calls": calls[-14:], "tool_calls": len(calls), "minutes": {}, "output_tokens": tokens,
            "cost_usd": None, "last_text": text[:220], "finished": "tokens used" in lines}


def activity(task: str) -> dict | None:
    found = latest_log(task)
    if not found:
        return None
    f, role = found
    data = read_codex_log(f) if f.suffix == ".log" else read_claude_log(f)
    now = time.time()
    m0 = int(now // 60)
    spark = [data["minutes"].get(m0 - 59 + i, 0) for i in range(60)]
    return {"role": role, "log": f.name, "idle_s": int(now - f.stat().st_mtime), "spark": spark,
            **attempt_meta(task, role), **{k: v for k, v in data.items() if k != "minutes"}}


def api_live(q: dict) -> dict:
    s = client.state()
    live = {}
    for t in s["tasks"].values():
        if t["status"] in ("assigned", "in_progress", "in_review", "blocked"):
            a = activity(t["id"])
            if t["status"] == "in_review" and (not a or a["role"] != "reviewer"):
                # In review with no reviewer log: the reviewer hasn't started. Say so, with how long.
                a = {**(a or {}), "role": "reviewer", "waiting_reviewer": True,
                     "since_s": int(time.time() - iso((t.get("current_result") or {}).get("at")))
                     if (t.get("current_result") or {}).get("at") else None}
            if a:
                live[t["id"]] = a
    return {"head": client.head, "live": live, "now": time.time()}


def api_task(tid: str) -> dict:
    view = client.task(tid)
    gm = goal_map()
    hist = [compact(e, gm) for e in view.get("events", [])]
    return {"task": view.get("task") or view, "history": hist, "live": activity(tid)}


# --- HTTP ----------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/events":
                out = api_events(q)
            elif u.path == "/api/state":
                out = api_state(q)
            elif u.path == "/api/live":
                out = api_live(q)
            elif u.path.startswith("/api/task/"):
                out = api_task(u.path.rsplit("/", 1)[1])
            else:
                return self.send(404, b"not found", "text/plain")
            self.send(200, json.dumps(out).encode(), "application/json")
        except HiveError as e:
            self.send(502, json.dumps({"error": str(e)}).encode(), "application/json")
        except Exception as e:   # keep serving; the page shows the error
            self.send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8476)
    ap.add_argument("--bind", default="127.0.0.1")
    a = ap.parse_args()
    client.health()
    print(f"1-hive Floor on http://{a.bind}:{a.port}", flush=True)
    ThreadingHTTPServer((a.bind, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
