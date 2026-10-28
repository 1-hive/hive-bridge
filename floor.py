#!/usr/bin/env python3
"""The Floor: a read-only live board of the hive, in a browser or inside Telegram.

    floor.py [--port 8476] [--bind 127.0.0.1]

Run with the hiverecord tool's Python (it imports the record client). Two views:
- bots (floor/bots.html; floor_bots.py): one card per agent: what it is doing now, what it owes, its day;
  the default for hives of long-lived agents (floor.json "view": "bots");
- goals (floor/index.html): one lane per goal, tasks across the record's states, replay at any position.

API:
  /api/bots, /api/bot/<actor>   the roster, and one actor with its timeline (floor_bots.py)
  /api/needs, /api/work, /api/chats, /api/machines, /api/status   the rest of the bots view; status is the
                                text the Telegram bridge pins in the work group
  POST /api/beacon              a signed beacon from an agent's sidecar or runner (hive-runtime)
  /api/events?after=N, /api/state?at=N, /api/live, /api/task/<id>   the goals view

Sources, never mixed up: the record (authoritative; read as actor `floor`, class instrument, which never
emits); beacons (what each agent's own files say, never authoritative); the bridge's dialogue log and the
chief of staff's turns (outside the record); each attempt's log in its task folder (1-hive's launched workers).

Access: from loopback without a proxy header, open (the bridge, an SSH tunnel). Through a proxy (Caddy adds
X-Forwarded-For), with floor.json "auth": "telegram", every data call needs a session: Telegram's signed
Mini App data (X-Telegram-Init-Data) or a cookie from a login link the bridge gives an operator in a private
chat (/floor). Only the humans in telegram-groups.json with the operator permission, or telegram.json's user.
It holds no key that can approve anything.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from hiverecord.client import Client, HiveError
from hiverecord.keys import SigningKey, verify

import floor_bots as fb
import hive_paths as hp

HERE = Path(__file__).resolve().parent
PAGE = HERE / "floor" / "index.html"
BOTS_PAGE = HERE / "floor" / "bots.html"
WORK = hp.WORK_DIR
ROUTE = WORK / ".route"
CFG = hp.CONFIG_DIR
FLOOR_DIR = Path(os.environ.get("FLOOR_STATE_DIR") or WORK / ".floor")   # its own samples of the beacons
TG_DIR = WORK / ".telegram"

client = Client(hp.HIVE_URL, hp.HIVE_ID,
                SigningKey.from_file(os.environ.get("FLOOR_KEY_FILE", str(CFG / "floor.key"))))
_conf_file = Path(os.environ.get("FLOOR_CONFIG") or CFG / "floor.json")
CONF = json.loads(_conf_file.read_text()) if _conf_file.is_file() else {}
GROUPS = json.loads((CFG / "telegram-groups.json").read_text()) if (CFG / "telegram-groups.json").is_file() else {}
LOCK = threading.Lock()

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
    return {t["id"]: t.get("goal") for t in state()["tasks"].values()}


def api_events(q: dict) -> dict:
    after = int(q.get("after", ["0"])[0])
    gm = goal_map()
    evs = [compact(e, gm) for e in events() if e["position"] > after]
    return {"head": client.head, "events": evs}


def iso(s: str | None) -> float | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def api_state(q: dict) -> dict:
    at = q.get("at", [None])[0]
    out = client.get("/v1/state", {"at": int(at) if at else None})
    s, pos = out["state"], out["at"]
    window = pos - 80   # recently finished goals stay on the board for a while
    goals = {}
    for g in (s.get("goals") or {}).values():
        recent = (g.get("status_since") or 0) > window
        if g["status"] in ("proposed", "active", "completed") or recent:
            goals[g["id"]] = {k: g.get(k) for k in ("id", "title", "status", "budget", "approved_at",
                                                     "escalation", "relevance", "spent", "status_since")}
    tasks = {}
    for t in s["tasks"].values():
        # a hive without goals (proto-hive profile): every task, under no goal
        if t.get("goal") not in goals and (s.get("goals") or t["status"] in ("done", "cancelled", "released")):
            continue
        ext = t.get("ext") or {}
        rev = t.get("latest_review") or {}
        tasks[t["id"]] = {"id": t["id"], "goal": t.get("goal"), "title": t.get("title"), "status": t["status"],
                          "owner": t.get("owner"), "reviewer": t.get("assigned_reviewer"),
                          "attempt": ext.get("attempt"), "restarts": ext.get("restarts", 0),
                          "nudges": ext.get("nudges_since_activity", 0), "escalation": ext.get("escalation"),
                          "block": t.get("block"), "reviews_failed": t.get("reviews_failed_count", 0),
                          "last_activity": t.get("last_activity"), "status_since": t.get("status_since"),
                          "verdict": rev.get("verdict")}
    inbox = client.inbox("operator") if at is None else []
    return {"at": pos, "head": client.head, "goals": goals, "tasks": tasks, "inbox": inbox}


# --- the record, cached ------------------------------------------------------------------------

_events: list[dict] = []
_state: dict = {"at": 0.0, "value": None}


def events() -> list[dict]:
    """Every event, kept in memory and topped up from the last position seen (signatures dropped)."""
    with LOCK:
        after = _events[-1]["position"] if _events else 0
        for e in client.all_events(after=after):
            e.pop("signature", None)
            _events.append(e)
        return list(_events)


def state() -> dict:
    with LOCK:
        if _state["value"] is None or time.time() - _state["at"] > 5:
            _state["value"], _state["at"] = client.state(), time.time()
        return _state["value"]


# --- beacons -----------------------------------------------------------------------------------

BEACONS: dict[str, dict] = {}         # actor -> {"body", "received"}
SAMPLES: dict[str, list[dict]] = {}   # actor -> compact samples of the last day (for the day strip)
FLOOR_UP: list[float] = []            # the Floor's own minutes up: a missing beacon counts only while it listened
SAMPLE_LOG = FLOOR_DIR / "samples.jsonl"


def sample(body: dict, t: float) -> dict:
    return {"t": t, "req": bool(body.get("requests")), "rev": bool(body.get("reviewing")),
            "prob": bool(body.get("problem")) and body.get("kind") == "reviewer",
            "step": body.get("last_step"), "chat": (body.get("chat") or {}).get("chat_id")}


def load_samples() -> None:
    """The last day's samples and up-minutes, from before a restart."""
    if not SAMPLE_LOG.is_file():
        return
    since = time.time() - fb.DAY - 3600
    for line in SAMPLE_LOG.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("t", 0) < since:
            continue
        if "up" in r:
            FLOOR_UP.append(r["t"])
        elif r.get("a"):
            SAMPLES.setdefault(r["a"], []).append({k: v for k, v in r.items() if k != "a"})
            if "last" in r:
                BEACONS[r["a"]] = {"body": r["last"], "received": r["t"]}
    for s in SAMPLES.values():
        for x in s:
            x.pop("last", None)


def keep_log() -> None:
    """Note the Floor is up once a minute; keep two days of samples."""
    while True:
        try:
            FLOOR_DIR.mkdir(parents=True, exist_ok=True)
            t = time.time()
            with LOCK:
                FLOOR_UP.append(t)
                del FLOOR_UP[:-3000]
                with SAMPLE_LOG.open("a") as f:
                    f.write(json.dumps({"t": t, "up": 1}) + "\n")
            if SAMPLE_LOG.stat().st_size > 20_000_000 or int(t) % 3600 < 60:
                since = t - 2 * fb.DAY
                keep = [l for l in SAMPLE_LOG.read_text().splitlines() if json.loads(l).get("t", 0) >= since]
                tmp = SAMPLE_LOG.with_suffix(".tmp")
                tmp.write_text("\n".join(keep) + "\n")
                tmp.replace(SAMPLE_LOG)
        except Exception as e:
            print(f"floor log: {e}", flush=True)
        time.sleep(60)


# --- problems reported in beacons reach the operators -------------------------------------------

ALERT_AFTER = 600          # seconds a problem must last before the operators are told (main: --alert-after)
CLEAR_AFTER = 120          # seconds without one before a reported problem counts as recovered
PROBLEMS: dict[str, dict] = {}   # actor -> {"since", "text", "alerted"}


def say(text: str) -> None:
    """One message to the operators' chat (the bridge's bot and chat, as telegram-say.py), off the request thread."""
    def send():
        try:
            import urllib.parse
            import urllib.request
            token = (CFG / "telegram-token").read_text().strip()
            chat = json.loads((CFG / "telegram.json").read_text())["chat_id"]
            data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000]}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30).read()
        except Exception as e:
            print(f"floor alert not sent: {e}", flush=True)
    threading.Thread(target=send, daemon=True).start()


def watch_problem(actor: str, body: dict, t: float) -> str | None:
    """An agent whose beacons report a problem for ALERT_AFTER seconds is reported once, and again when its beacons
    stop reporting one. The problem's wording may change meanwhile (a reviewer's error names each task it tries),
    so it counts as one problem until it clears. Returns the message to send, if any."""
    problem = (body.get("problem") or "").strip()
    p = PROBLEMS.get(actor)
    if problem:
        if p is None:
            PROBLEMS[actor] = p = {"since": t, "text": problem, "alerted": False}
        p["text"] = problem
        p.pop("clear", None)
        if not p["alerted"] and t - p["since"] >= ALERT_AFTER:
            p["alerted"] = True
            what = "reviewer" if body.get("kind") == "reviewer" else "agent"
            return (f"⚠️ {actor} ({what}) has reported a problem for {int((t - p['since']) // 60)} min: {problem[:300]}"
                    + ("\nIts reviews aren't being recorded until this is fixed." if what == "reviewer" else ""))
        return None
    if p is not None:
        if p["alerted"] and t - p.setdefault("clear", t) < CLEAR_AFTER:
            return None              # a restart clears the problem for a moment: recovered only if it stays clear
        PROBLEMS.pop(actor)
        if p["alerted"]:
            return f"✅ {actor} recovered: its beacons report no problem now (it had one for {int((t - p['since']) // 60)} min)."
    return None


def take_beacon(headers, raw: bytes) -> tuple[int, str]:
    """Accept a beacon signed by an active actor's key, about that actor (hive-runtime's protocol)."""
    if len(raw) > 128_000:
        return 413, "too big"
    key, at, sig = headers.get("X-Hive-Key", ""), headers.get("X-Hive-Signed-At", ""), headers.get("X-Hive-Signature", "")
    try:
        when = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return 400, "bad X-Hive-Signed-At"
    if abs(time.time() - when) > 300:
        return 403, "clock skew over 5 minutes"
    msg = f"hive-beacon1\n{hp.HIVE_ID}\n{at}\n{hashlib.sha256(raw).hexdigest()}".encode()
    if not verify(key, msg, sig):
        return 403, "bad signature"
    actor = next((a for a, v in state()["actors"].items() if v.get("status") == "active" and key in v.get("keys", [])), None)
    if actor is None and time.time() - _state.get("forced", 0) > 30:
        _state["value"], _state["forced"] = None, time.time()   # maybe a key added since the last read (at most every 30 s)
        actor = next((a for a, v in state()["actors"].items() if v.get("status") == "active" and key in v.get("keys", [])), None)
    try:
        body = json.loads(raw)
    except ValueError:
        return 400, "not JSON"
    if not actor or body.get("actor") != actor:
        return 403, "key isn't this actor's"
    t = time.time()
    with LOCK:
        BEACONS[actor] = {"body": body, "received": t}
        alert = watch_problem(actor, body, t)
        if alert:
            say(alert)
        s = sample(body, t)
        SAMPLES.setdefault(actor, []).append(s)
        if len(SAMPLES[actor]) > 4000:
            del SAMPLES[actor][:-3000]
        try:
            FLOOR_DIR.mkdir(parents=True, exist_ok=True)
            with SAMPLE_LOG.open("a") as f:
                # every 10th sample carries the whole beacon, so a restart starts from the last one
                f.write(json.dumps({"a": actor, **s, **({"last": body} if len(SAMPLES[actor]) % 10 == 1 else {})}) + "\n")
        except OSError as e:
            print(f"sample log: {e}", flush=True)
    return 204, ""


# --- the dialogue log and the chief of staff's turns ---------------------------------------------

_dlg: dict = {"key": None, "rows": []}


def dialogue() -> list[dict]:
    """The bridge's dialogue log, the last 26 hours (read again when the file changes)."""
    f = TG_DIR / "dialogue.jsonl"
    try:
        st = f.stat()
    except OSError:
        return []
    key = (st.st_size, st.st_mtime)
    if _dlg["key"] != key:
        with f.open("rb") as h:
            h.seek(max(0, st.st_size - 4_000_000))
            lines = h.read().decode(errors="replace").splitlines()[1 if st.st_size > 4_000_000 else 0:]
        since = time.time() - fb.DAY - 2 * 3600
        rows = []
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if (fb.ts(d.get("at")) or 0) >= since:
                rows.append(d)
        _dlg.update(key=key, rows=rows)
    return _dlg["rows"]


def cos_turns() -> dict:
    """When the chief of staff was answering (the bridge's cos-turns.jsonl), and whether it is now."""
    out: dict = {"turns": [], "busy": None, "last": None}
    try:
        for line in (TG_DIR / "cos-turns.jsonl").read_text().splitlines()[-500:]:
            r = json.loads(line)
            out["turns"].append((r["start"], r["end"]))
            out["last"] = r["end"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        busy = json.loads((TG_DIR / "cos-now.json").read_text())
        if time.time() - busy.get("since", 0) < 1200:
            out["busy"] = busy
            out["turns"].append((busy["since"], None))
    except (OSError, ValueError):
        pass
    return out


# --- merged or not: a reviewed commit on its repository's default branch -------------------------

_merged: dict = {}   # task -> (checked at, True/False/None, the branch it merges into)
MERGE_TTL = int(os.environ.get("FLOOR_MERGE_TTL", 300))   # seconds a merge verdict is reused


MERGE_INTO: dict = {}   # task -> the branch its reviewed code merges into (main, protomega...)


def target_branch(clone: Path, base: str | None, env: dict) -> str:
    """The branch a result merges into, as the publisher picks its pull request's: the default branch if it holds the
    result's base, else the first other branch (not hive/*) that does; an agent working from its own line (sidecar
    "base_branches", e.g. iter's protomega) merges there, not into the default branch."""
    def git(*a):
        return subprocess.run(["git", "-C", str(clone), *a], capture_output=True, text=True, env=env)
    default = git("symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
    if not base or not re.fullmatch(r"[0-9a-f]{7,64}", base):
        return default or "HEAD"
    branches = [b for b in git("for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split()
                if not b.startswith("hive/")]
    for b in ([default] if default in branches else []) + [b for b in branches if b != default]:
        if git("merge-base", "--is-ancestor", base, b).returncode == 0:
            return b
    return default or "HEAD"


def merged_map(st: dict, evs: list[dict]) -> dict:
    """For each task done in the last week whose passed result pinned code: is that commit on the default
    branch of the hive's bare clone (floor.json "clones_dir", kept fresh by main-watch)? Unknown: left out."""
    clones = CONF.get("clones_dir")
    if not clones:
        return {}
    out = {}
    week = time.time() - 7 * fb.DAY
    results = {e["event_id"]: e for e in evs if e["type"] == "task.result_posted"}
    for t in st["tasks"].values():
        cr, lr = t.get("current_result") or {}, t.get("latest_review") or {}
        if t["status"] != "done" or lr.get("verdict") != "passed" or (iso(cr.get("at")) or 0) < week:
            continue
        ev = results.get(cr.get("event_id"))
        code = [r["pin"] for r in (ev or {}).get("refs", []) if r.get("rel") == "code"]
        bases = {r["pin"].get("repository"): r["pin"].get("commit_oid", "").split(":")[-1]
                 for r in (ev or {}).get("refs", []) if r.get("rel") == "base" and r.get("pin")}
        if not code:
            continue
        hit = _merged.get(t["id"])
        if hit and time.time() - hit[0] < MERGE_TTL:
            if hit[1] is not None:
                out[t["id"]] = hit[1]
                MERGE_INTO[t["id"]] = hit[2]
            continue
        verdict, into = True, None
        for pin in code:
            repo, sha = pin.get("repository"), pin.get("commit_oid", "").split(":")[-1]
            clone = Path(clones) / repo
            if not clone.is_dir() or not re.fullmatch(r"[0-9a-f]{7,64}", sha or ""):
                verdict = None
                break
            env = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
            ref = into = target_branch(clone, bases.get(repo), env)
            r = subprocess.run(["git", "-C", str(clone), "merge-base", "--is-ancestor", sha, ref], capture_output=True, env=env)
            if r.returncode == 1:
                # not on main: still waiting for a merge, unless its task branch is gone from the remote
                # (people decided not to merge it and deleted the branch: nothing left to do)
                br = f"refs/heads/hive/{t['id']}"
                gone = subprocess.run(["git", "-C", str(clone), "ls-remote", "--exit-code", "origin", br],
                                      capture_output=True, env=env, timeout=30).returncode == 2
                verdict = None if gone else False
            elif r.returncode != 0:
                verdict = None
                break
        _merged[t["id"]] = (time.time(), verdict, into)
        if verdict is not None:
            out[t["id"]] = verdict
            MERGE_INTO[t["id"]] = into
    return out


# --- the bots view -------------------------------------------------------------------------------

def facts() -> fb.Facts:
    st, evs = state(), events()
    with LOCK:
        beacons = dict(BEACONS)
        samples = {a: list(s) for a, s in SAMPLES.items()}
        up = list(FLOOR_UP)
    return fb.Facts(state=st, events=evs, beacons=beacons, samples=samples, dialogue=dialogue(), groups=GROUPS,
                    conf=CONF, floor_up=up, cos=cos_turns(), merged=merged_map(st, evs), merge_into=dict(MERGE_INTO),
                    requests=open_requests())


def open_requests() -> list[str]:
    """The chief of staff's requests to the operators: hive-requests/*.md on the workspace's main, not yet
    moved into hive-requests/done/ (as the bridge's digest lists them)."""
    r = subprocess.run(["git", "-C", str(hp.WORKSPACE_REPO), "ls-tree", "--name-only", "main", "hive-requests/"],
                       capture_output=True, text=True,
                       env={**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"})
    return [Path(x).stem for x in r.stdout.split() if x.endswith(".md")] if r.returncode == 0 else []


def roster(f: fb.Facts) -> list[dict]:
    return [fb.bot(f, a) for a in fb.roster_order(f)]


def inboxes() -> tuple[list, list]:
    try:
        return client.inbox("operator"), client.inbox("chief_of_staff")
    except HiveError:
        return [], []


def api_bots(q: dict) -> dict:
    f = facts()
    bots = roster(f)
    return {"now": f.now, "hive": hp.HIVE_ID, "head": client.head, "bots": bots, "machines": fb.machines(f, bots)}


def api_bot(actor: str) -> dict:
    f = facts()
    if actor not in f.state.get("actors", {}):
        raise KeyError(actor)
    return {"now": f.now, **fb.bot(f, actor, detail=True)}


def api_needs(q: dict) -> dict:
    f = facts()
    op, cos = inboxes()
    return {"now": f.now, "needs": fb.needs(f, op, cos, roster(f))}


def api_work(q: dict) -> dict:
    f = facts()
    return {"now": f.now, "stages": fb.work(f)}


def api_chats(q: dict) -> dict:
    f = facts()
    return {"now": f.now, "chats": fb.chats(f)}


def api_status(q: dict) -> dict:
    f = facts()
    bots = roster(f)
    op, cos = inboxes()
    return fb.status(f, bots, fb.needs(f, op, cos, bots))


def api_all(q: dict) -> dict:
    """Everything the bots page draws, in one call."""
    f = facts()
    bots = roster(f)
    op, cos = inboxes()
    return {"now": f.now, "hive": hp.HIVE_ID, "head": client.head, "bots": bots, "machines": fb.machines(f, bots),
            "needs": fb.needs(f, op, cos, bots), "stages": fb.work(f), "chats": fb.chats(f)}


# --- agent activity, from attempt logs (1-hive's launched workers) ----------------------------------

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
    s = state()
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


# --- access ----------------------------------------------------------------------------------------

SESSION_DAYS = 30


def secret() -> bytes:
    """floor-session.secret (made by the deployment); without it no session is ever valid."""
    try:
        s = (CFG / "floor-session.secret").read_text().strip()
    except OSError:
        s = ""
    return s.encode() if len(s) >= 32 else os.urandom(32)


def operators() -> set[int]:
    out = {h["user_id"] for h in GROUPS.get("humans", []) if "operator" in h.get("permissions", [])}
    try:
        out.add(json.loads((CFG / "telegram.json").read_text())["user_id"])
    except (OSError, ValueError, KeyError):
        pass
    return out


def sign(text: str) -> str:
    return hmac.new(secret(), text.encode(), hashlib.sha256).hexdigest()


def login_link(user_id: int, base: str, minutes: int = 15) -> str:
    """A one-time-ish login link (valid `minutes`), for the bridge to hand an operator in a private chat."""
    exp = int(time.time()) + minutes * 60
    return f"{base.rstrip('/')}/login?u={user_id}&e={exp}&s={sign(f'login.{user_id}.{exp}')}"


def session_cookie(user_id: int) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    val = f"{user_id}.{exp}.{sign(f'session.{user_id}.{exp}')}"
    return f"floor_session={val}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; Secure; SameSite=Lax"


def check_cookie(raw: str) -> int | None:
    m = re.search(r"(?:^|;\s*)floor_session=(\d+)\.(\d+)\.([0-9a-f]{64})", raw or "")
    if not m:
        return None
    uid, exp, sig = int(m.group(1)), int(m.group(2)), m.group(3)
    if exp < time.time() or not hmac.compare_digest(sig, sign(f"session.{uid}.{exp}")) or uid not in operators():
        return None
    return uid


def check_init_data(init: str) -> int | None:
    """Telegram Mini App data, signed with the bot's token (core.telegram.org/bots/webapps)."""
    if not init:
        return None
    pairs = dict(parse_qs(init, keep_blank_values=True))
    got = (pairs.pop("hash", [""]) or [""])[0]
    check = "\n".join(f"{k}={v[0]}" for k, v in sorted(pairs.items()))
    try:   # the key derived from the bot's token (a deployment can give the Floor this, never the token itself)
        key = bytes.fromhex((CFG / "telegram-webapp.key").read_text().strip())
    except (OSError, ValueError):
        key = hmac.new(b"WebAppData", (CFG / "telegram-token").read_text().strip().encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got):
        return None
    if time.time() - int((pairs.get("auth_date") or ["0"])[0]) > 86400:
        return None
    try:
        uid = int(json.loads(pairs["user"][0])["id"])
    except (KeyError, ValueError, TypeError):
        return None
    return uid if uid in operators() else None


# --- HTTP ----------------------------------------------------------------------------------------

API = {"/api/events": api_events, "/api/state": api_state, "/api/live": api_live, "/api/bots": api_bots,
       "/api/needs": api_needs, "/api/work": api_work, "/api/chats": api_chats, "/api/status": api_status,
       "/api/all": api_all}


class Handler(BaseHTTPRequestHandler):
    def send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, code: int, out) -> None:
        self.send(code, json.dumps(out).encode(), "application/json")

    def proxied(self) -> bool:
        return bool(self.headers.get("X-Forwarded-For")) or self.client_address[0] not in ("127.0.0.1", "::1")

    def allowed(self) -> bool:
        """Open from loopback without a proxy; through one, a session is needed (unless floor.json "auth": "none")."""
        if not self.proxied() or CONF.get("auth") == "none":
            return True
        return (check_cookie(self.headers.get("Cookie", "")) or check_init_data(self.headers.get("X-Telegram-Init-Data", ""))) is not None

    def do_POST(self) -> None:
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 200_000))
        try:
            if u.path == "/api/beacon":
                code, why = take_beacon(self.headers, raw)
                return self.send(code, why.encode(), "text/plain")
            if u.path == "/api/session":   # a Mini App trades its signed data for a cookie
                uid = check_init_data(self.headers.get("X-Telegram-Init-Data", ""))
                if uid is None:
                    return self.json(401, {"error": "not an operator of this hive"})
                return self.send(204, b"", "text/plain", {"Set-Cookie": session_cookie(uid)})
            return self.send(404, b"not found", "text/plain")
        except Exception as e:
            self.json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/healthz":
                return self.send(200, b"ok", "text/plain")
            if u.path in ("/", "/index.html", "/bots", "/goals"):
                bots = u.path == "/bots" or (u.path != "/goals" and CONF.get("view") == "bots")
                return self.send(200, (BOTS_PAGE if bots else PAGE).read_bytes(), "text/html; charset=utf-8")
            if u.path == "/login":
                uid, exp, s = q.get("u", [""])[0], q.get("e", ["0"])[0], q.get("s", [""])[0]
                ok = (uid.isdigit() and exp.isdigit() and int(exp) > time.time()
                      and hmac.compare_digest(s, sign(f"login.{uid}.{exp}")) and int(uid) in operators())
                if not ok:
                    return self.send(403, b"This login link has expired or isn't valid. Ask the bot for a new one: /floor",
                                     "text/plain; charset=utf-8")
                return self.send(303, b"", "text/plain", {"Set-Cookie": session_cookie(int(uid)), "Location": "/"})
            if not u.path.startswith("/api/"):
                return self.send(404, b"not found", "text/plain")
            if not self.allowed():
                return self.json(401, {"error": "sign in: open the Floor from the bot in Telegram (/floor)"})
            if u.path in API:
                out = API[u.path](q)
            elif u.path.startswith("/api/task/"):
                out = api_task(u.path.rsplit("/", 1)[1])
            elif u.path.startswith("/api/bot/"):
                try:
                    out = api_bot(u.path.rsplit("/", 1)[1])
                except KeyError:
                    return self.json(404, {"error": "no such actor"})
            else:
                return self.send(404, b"not found", "text/plain")
            self.json(200, out)
        except HiveError as e:
            self.json(502, {"error": str(e)})
        except Exception as e:   # keep serving; the page shows the error
            self.json(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    global ALERT_AFTER
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8476)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--alert-after", type=int, default=ALERT_AFTER,
                    help="seconds an agent's beacons must report a problem before the operators are told (default 600)")
    a = ap.parse_args()
    ALERT_AFTER = a.alert_after
    client.health()
    load_samples()
    threading.Thread(target=keep_log, daemon=True).start()
    print(f"{hp.HIVE_ID} Floor on http://{a.bind}:{a.port} (view: {CONF.get('view', 'goals')})", flush=True)
    ThreadingHTTPServer((a.bind, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
