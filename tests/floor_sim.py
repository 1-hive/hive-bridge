"""The Floor's bot view against a scratch proto-hive record, with fake beacons and a fake dialogue log.

    uv run --project ~/repos/hive-record python tests/floor_sim.py            # checks, then tears down
    uv run --project ~/repos/hive-record python tests/floor_sim.py --serve    # and keeps the Floor up on :8499

Needs rootless Podman (hiverecord.scratch). Builds the proto-hive's actors, five tasks in different states
(assigned, working, blocked, in review, done with its code not on main), then starts floor.py against it
and checks /api/bots, /api/needs, /api/work, /api/chats, /api/status, beacon signatures and the login cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
from hiverecord import scratch
from hiverecord.canonical import dumps
from hiverecord.conformance import FixtureRepo
from hiverecord.keys import SigningKey

HERE = Path(__file__).resolve().parents[1]
HIVE = [sys.executable, "-m", "hiverecord.cli"]
HIVE_PIN = [sys.executable, "-m", "hivepin.cli"]
BOTS = ["worker.protomegatron", "worker.protomegabot2", "worker.protocosmo2", "worker.protocosmopopos", "worker.protocosmo"]
BEN, ANDY, WORK, PROTOBOTS = 402314199, 260710712, -5555360452, -5437945421


def sh(*cmd, cwd=None, env=None):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"FAILED: {' '.join(map(str, cmd))}\n{r.stdout}{r.stderr}")
    return r.stdout


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def beacon(url, hive_id, keyfile, body):
    key = SigningKey.from_file(keyfile)
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sig = key.sign(f"hive-beacon1\n{hive_id}\n{at}\n{hashlib.sha256(raw).hexdigest()}".encode())
    req = urllib.request.Request(url, data=raw, method="POST", headers={
        "Content-Type": "application/json", "X-Hive-Key": key.public, "X-Hive-Signed-At": at, "X-Hive-Signature": sig})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def get(base, path, **kw):
    r = httpx.get(base + path, timeout=20, **kw)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)


def main():
    serve = "--serve" in sys.argv
    tmp = Path(tempfile.mkdtemp(prefix="floor-sim-"))
    fx = FixtureRepo()
    pg = scratch.up()
    procs = []
    try:
        gitenv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                  "GIT_COMMITTER_EMAIL": "t@t"}
        for name in ("workspace", "hive-appliance"):
            sh("git", "init", "-q", "--bare", "-b", "main", str(tmp / f"{name}.git"))
            seed = tmp / f"seed-{name}"
            sh("git", "clone", "-q", str(tmp / f"{name}.git"), str(seed), env=gitenv)
            (seed / "README.md").write_text(f"{name}\n")
            sh("git", "-C", str(seed), "add", "-A", env=gitenv)
            sh("git", "-C", str(seed), "commit", "-q", "-m", "start", env=gitenv)
            sh("git", "-C", str(seed), "push", "-q", "origin", "HEAD:main", env=gitenv)
        reg = json.loads(fx.registry_path.read_text())
        for name in ("workspace", "hive-appliance"):
            reg["repositories"][name] = {"fetch_urls": [f"file://{tmp}/{name}.git"], "object_format": "sha1",
                                         "allowed_ref_patterns": ["refs/heads/*"], "local_path": str(tmp / f"seed-{name}")}
        registry = tmp / "registry.json"
        registry.write_text(json.dumps(reg, indent=1))
        env = {**os.environ, "HIVEPIN_CACHE_DIRECTORY": str(tmp / "pincache"), "HIVEPIN_REPOSITORY_REGISTRY_PATH": str(registry)}

        secrets = tmp / "secrets.json"
        sh(*HIVE, "db-bootstrap", "--superuser-url", pg.superuser_url, "--db", "floorhive", "--prefix", "fl",
           "--secrets-file", str(secrets))
        pw = json.loads(secrets.read_text())
        url = {r: pg.url(f"fl_{r}", pw[f"fl_{r}"], "floorhive") for r in ("admin", "gateway")}
        names = ["op", "cos", "sup", "disp", "floor", "telegram", *BOTS, "reviewer.astra"]
        pub = {a: sh(*HIVE, "keygen", "--out", str(tmp / f"{a}.key")).strip() for a in names}
        (tmp / "policy.pin").write_bytes(dumps(fx.policy_pin()))
        sh(*HIVE, "init", "--admin-url", url["admin"], "--prefix", "fl", "--hive", "proto-hive", "--profile", "proto-hive",
           "--policy", str(tmp / "policy.pin"), "--mode", "authoritative", "--registry", str(registry),
           "--operator-id", "op", "--operator-key", pub["op"], env=env)
        port = free_port()
        gw = subprocess.Popen([*HIVE, "gateway", "--db-url", url["gateway"], "--registry", str(registry), "--port", str(port)],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        procs.append(gw)
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(base + "/v1/health").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)

        def hive(actor, *args):
            return sh(*HIVE, *args, env={**env, "HIVE_URL": base, "HIVE_ID": "proto-hive", "HIVE_KEY_FILE": str(tmp / f"{actor}.key")})

        def pin_file(repo, clone, path=None):
            out = tmp / f"pin-{time.time_ns()}.json"
            sh(*HIVE_PIN, "mint", repo, *([path] if path else []), "--commit",
               sh("git", "-C", str(clone), "rev-parse", "HEAD").strip(), "--output", str(out), env=env)
            return str(out)

        def commit(repo, path, text, msg, branch="main"):
            c = tmp / f"seed-{repo}"
            sh("git", "-C", str(c), "checkout", "-q", "-B", branch, env=gitenv)
            (c / path).parent.mkdir(parents=True, exist_ok=True)
            (c / path).write_text(text)
            sh("git", "-C", str(c), "add", "-A", env=gitenv)
            sh("git", "-C", str(c), "commit", "-q", "-m", msg, env=gitenv)
            sh("git", "-C", str(c), "push", "-q", "-f", "origin", f"HEAD:{branch}", env=gitenv)
            return c

        classes = {"cos": "chief_of_staff", "sup": "supervisor", "disp": "coordinator", "floor": "instrument",
                   "telegram": "operator", "reviewer.astra": "reviewer", **{b: "worker" for b in BOTS}}
        for a, cls in classes.items():
            hive("op", "emit", "actor.registered", "--data", json.dumps(
                {"actor_id": a, "class": cls, "role": cls, "keys": [pub[a]],
                 "declaration": {"harness": "openclaw" if "cosmopopos" in a or a == "worker.protocosmo" else "iter",
                                 "model_route": "claude-opus-5-5", "sandbox": a}}))
        hive("cos", "emit", "project.opened", "--data", '{"project":"p1","title":"P1"}')

        def task(tid, title, to, repos=()):
            opin = pin_file("workspace", commit("workspace", f"orders/{tid}.md", f"# {title}\n", f"order {tid}"), f"orders/{tid}.md")
            hive("cos", "emit", "task.created", "--task", tid, "--ref", f"order={opin}", "--cause-external",
                 f"telegram:{PROTOBOTS}/{100 + len(tid)}",
                 "--data", json.dumps({"title": title, "project": "p1", "ext": {"kind": "work", "repos": list(repos)}}))
            hive("cos", "emit", "task.assigned", "--task", tid, "--data", json.dumps(
                {"to": to, "ext": {"lease": {"accept_within_seconds": 1800, "checkin_every_seconds": 1800}}}))

        def report(actor, tid, kind):
            p = pin_file("workspace", commit("workspace", f"reports/{tid}-{kind}-{time.time_ns()}.md", kind, kind), None)
            return p

        task("hyperseed-lemma-audit", "Audit the grounding lemma", "worker.protomegatron")
        task("journal-fifo-guard", "Make the journal refuse a FIFO", "worker.protomegabot2", ["hive-appliance"])
        hive("worker.protomegabot2", "emit", "task.accepted", "--task", "journal-fifo-guard")
        p = pin_file("workspace", commit("workspace", "reports/fifo-progress.md", "guard in place", "progress"), "reports/fifo-progress.md")
        hive("worker.protomegabot2", "emit", "task.reported", "--task", "journal-fifo-guard", "--data", '{"kind":"progress"}',
             "--ref", f"report={p}")
        task("wmtm-notes", "Notes on WMTM", "worker.protocosmo2")
        hive("worker.protocosmo2", "emit", "task.accepted", "--task", "wmtm-notes")
        q = pin_file("workspace", commit("workspace", "reports/wmtm-q.md", "which branch?", "question"), "reports/wmtm-q.md")
        hive("worker.protocosmo2", "emit", "task.blocked", "--task", "wmtm-notes", "--ref", f"question={q}",
             "--data", json.dumps({"needs": "decision", "reason": "Which WMTM branch should the notes go on?"}))

        def result(actor, tid, line):
            base_c = sh("git", "-C", str(tmp / "seed-hive-appliance"), "rev-parse", "main").strip()
            c = commit("hive-appliance", f"{tid}.txt", line, tid, branch=f"hive/{tid}")
            code = pin_file("hive-appliance", c)
            sh("git", "-C", str(c), "checkout", "-q", base_c, env=gitenv)
            basep = pin_file("hive-appliance", c)
            sh("git", "-C", str(c), "checkout", "-q", "main", env=gitenv)
            r = pin_file("workspace", commit("workspace", f"reports/{tid}-result.md", "done", "result"), f"reports/{tid}-result.md")
            hive(actor, "emit", "task.result_posted", "--task", tid, "--ref", f"result={r}", "--ref", f"code={code}",
                 "--ref", f"base={basep}")

        def assign_review(tid):
            ev = json.loads(hive("floor", "state"))["tasks"][tid]["current_result"]["event_id"]
            hive("disp", "emit", "review.assigned", "--task", tid,
                 "--data", json.dumps({"reviewer": "reviewer.astra", "result_event": ev}))

        task("readme-record-line", "One line in the README", "worker.protomegabot2", ["hive-appliance"])
        hive("worker.protomegabot2", "emit", "task.accepted", "--task", "readme-record-line")
        result("worker.protomegabot2", "readme-record-line", "record line")
        assign_review("readme-record-line")

        task("tidy-tests", "Tidy the tests", "worker.protomegabot2", ["hive-appliance"])
        hive("worker.protomegabot2", "emit", "task.accepted", "--task", "tidy-tests")
        result("worker.protomegabot2", "tidy-tests", "tidy")
        assign_review("tidy-tests")
        st = json.loads(hive("floor", "state"))
        cr = st["tasks"]["tidy-tests"]["current_result"]
        rv = pin_file("workspace", commit("workspace", "reports/tidy-review.md", "ok", "review"), "reports/tidy-review.md")
        hive("reviewer.astra", "emit", "review.recorded", "--task", "tidy-tests", "--ref", f"review={rv}",
             "--data", json.dumps({"verdict": "passed", "result_event": cr["event_id"]}))
        hive("cos", "emit", "task.closed", "--task", "tidy-tests", "--data", '{"note":"passed"}')
        hive("worker.protomegabot2", "emit", "actor.declared", "--data", json.dumps(
            {"actor_id": "worker.protomegabot2", "declaration": {"harness": "iter main@b81e0d4; hive adapter 1a2b3c4d",
                                                                  "model_route": "claude-opus-5-5", "sandbox": "x"}}))
        st = json.loads(hive("floor", "state"))
        print("state task keys:", sorted(st["tasks"]["readme-record-line"]))
        print("state actor keys:", sorted(next(iter(st["actors"].values()))))
        ev = [json.loads(x) for x in hive("floor", "events", "--task", "tidy-tests").splitlines() if x.strip()]
        print("event keys:", sorted(ev[-1]), "result refs:", [e.get("refs") for e in ev if e["type"] == "task.result_posted"][:1])
        (tmp / "state.json").write_text(json.dumps(st, indent=1))
        (tmp / "events.json").write_text(json.dumps(ev, indent=1))
        print(f"state and events dumped under {tmp}")

        # the Floor's config: clones of the code repository (as up.sh keeps under /var/lib/hive/clones)
        cfg, work = tmp / "config", tmp / "work"
        (cfg).mkdir()
        (work / ".telegram").mkdir(parents=True)
        shutil.copy(tmp / "floor.key", cfg / "floor.key")
        (cfg / "telegram-token").write_text("123:TEST")
        (cfg / "telegram.json").write_text(json.dumps({"chat_id": ANDY, "user_id": ANDY}))
        (cfg / "telegram-groups.json").write_text(json.dumps({
            "chats": [{"name": "work", "chat_id": WORK, "mode": "all", "status": True},
                      {"name": "protobots", "chat_id": PROTOBOTS, "mode": "mention"}],
            "humans": [{"name": "ben", "user_id": BEN, "permissions": ["operator"]},
                       {"name": "andy", "user_id": ANDY, "permissions": ["operator"]}],
            "bots": [{"name": "protomegatron", "user_id": 8562797306, "username": "Protomegabot", "actor": "worker.protomegatron"},
                     {"name": "protomegabot2", "user_id": 8680999952, "username": "Protomega2bot", "actor": "worker.protomegabot2"},
                     {"name": "protocosmo", "user_id": 8927136413, "username": "Protocosmobot", "actor": "worker.protocosmo"}]}))
        (cfg / "floor-session.secret").write_text("s" * 64)
        clones = tmp / "clones"
        clones.mkdir()
        sh("git", "clone", "-q", "--bare", str(tmp / "hive-appliance.git"), str(clones / "hive-appliance"))
        (cfg / "floor.json").write_text(json.dumps({
            "view": "bots", "auth": "telegram", "public_url": "https://floor.example",
            "clones_dir": str(clones),
            "machines": {"vm2": ["cos", "worker.protomegatron", "worker.protomegabot2", "worker.protocosmo"],
                         "pop-os": ["worker.protocosmo2", "worker.protocosmopopos", "reviewer.astra"]},
            "names": {"worker.protomegatron": "ProtomegaTron", "worker.protomegabot2": "ProtoMegaBot2",
                      "worker.protocosmo2": "ProtoCosmo2", "worker.protocosmopopos": "protocosmopopos",
                      "worker.protocosmo": "protocosmo", "reviewer.astra": "Astra", "cos": "Chief of staff"},
            "about": {"worker.protomegatron": "research, formalization", "worker.protomegabot2": "code, hive-appliance"}}))
        now = time.time()
        rows = [
            {"chat": PROTOBOTS, "chat_name": "protobots", "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 240)),
             "from_id": BEN, "bot": False, "id": 501, "from": "ben",
             "text": "@Protomegabot can you check whether the grounding lemma still holds after the d-calculus change?"},
            {"chat": WORK, "chat_name": "work", "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 1100)),
             "from_id": ANDY, "bot": False, "id": 77, "from": "andy", "text": "@cos the journal should refuse a FIFO"},
            {"chat": PROTOBOTS, "chat_name": "protobots", "at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 3000)),
             "from_id": 8927136413, "bot": True, "id": 499, "from": "protocosmo", "text": "Agreed, it can wait for Ben."},
        ]
        (work / ".telegram" / "dialogue.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        fport = int(os.environ.get("FLOOR_SIM_PORT") or free_port())
        fenv = {**env, "HIVE_URL": base, "HIVE_ID": "proto-hive", "HIVE_CONFIG_DIR": str(cfg), "HIVE_WORK_DIR": str(work),
                "FLOOR_KEY_FILE": str(cfg / "floor.key"), "FLOOR_CONFIG": str(cfg / "floor.json"),
                "HIVE_WORKSPACE_REPO": str(tmp / "workspace.git"), "FLOOR_MERGE_TTL": "0"}
        fl = subprocess.Popen([sys.executable, str(HERE / "floor.py"), "--port", str(fport)], env=fenv)
        procs.append(fl)
        fbase = f"http://127.0.0.1:{fport}"
        for _ in range(100):
            try:
                if httpx.get(fbase + "/healthz").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.2)

        # beacons: ProtomegaTron answering Ben's message; ProtoMegaBot2 in an episode; Astra can't run
        hour = [[now - 5000, now - 4000, "claude-opus-5-5"], [now - 3000, now - 2950, "claude-opus-5-5"]]
        assert beacon(fbase + "/api/beacon", "proto-hive", tmp / "worker.protomegatron.key", {
            "v": 1, "actor": "worker.protomegatron", "kind": "iter", "at": now, "started": now - 9000,
            "requests": [{"chat_id": PROTOBOTS, "message_id": 501, "since": now - 230}], "queued": 1,
            "last_answer": now - 3000, "answers": [now - 2950], "last_step": now - 20,
            "episodes": hour + [[now - 230, None, "claude-opus-5-5"]], "pending_actions": 0}) == 204
        assert beacon(fbase + "/api/beacon", "proto-hive", tmp / "worker.protomegabot2.key", {
            "v": 1, "actor": "worker.protomegabot2", "kind": "iter", "at": now, "started": now - 9000, "requests": [],
            "queued": 0, "last_answer": None, "answers": [], "last_step": now - 15,
            "episodes": [[now - 1000, None, "claude-opus-5-5"]], "pending_actions": 0}) == 204
        assert beacon(fbase + "/api/beacon", "proto-hive", tmp / "reviewer.astra.key", {
            "v": 1, "actor": "reviewer.astra", "kind": "reviewer", "at": now, "started": now - 9000,
            "reviewing": None, "problem": "Not logged in. Run codex login."}) == 204
        # a beacon signed by another actor's key, or by an unknown key, is refused
        assert beacon(fbase + "/api/beacon", "proto-hive", tmp / "worker.protomegabot2.key",
                      {"v": 1, "actor": "worker.protomegatron", "kind": "iter", "at": now, "started": now}) == 403
        bad = tmp / "stranger.key"
        SigningKey.generate().to_file(bad)
        assert beacon(fbase + "/api/beacon", "proto-hive", bad, {"v": 1, "actor": "worker.protomegatron", "kind": "iter",
                                                                 "at": now, "started": now}) == 403
        print("beacons: accepted from their own actors, refused otherwise")

        # from outside (Caddy adds X-Forwarded-For): no data without a session
        code, _ = get(fbase, "/api/bots", headers={"X-Forwarded-For": "1.2.3.4"})
        assert code == 401, code
        exp = int(time.time()) + 600
        sig = hmac.new(("s" * 64).encode(), f"login.{BEN}.{exp}".encode(), hashlib.sha256).hexdigest()
        r = httpx.get(f"{fbase}/login?u={BEN}&e={exp}&s={sig}", headers={"X-Forwarded-For": "1.2.3.4"})
        assert r.status_code == 303 and "floor_session" in r.headers.get("set-cookie", ""), (r.status_code, r.headers)
        cookie = r.headers["set-cookie"].split(";")[0]
        code, bots = get(fbase, "/api/bots", headers={"X-Forwarded-For": "1.2.3.4", "Cookie": cookie})
        assert code == 200, (code, bots)
        print("login link -> cookie -> data, from outside")

        def init_data(uid):   # what Telegram hands a Mini App, signed with the bot's token
            from urllib.parse import urlencode
            f = {"auth_date": str(int(time.time())), "query_id": "q1", "user": json.dumps({"id": uid, "first_name": "x"})}
            check = "\n".join(f"{k}={v}" for k, v in sorted(f.items()))
            key = hmac.new(b"WebAppData", b"123:TEST", hashlib.sha256).digest()
            return urlencode({**f, "hash": hmac.new(key, check.encode(), hashlib.sha256).hexdigest()})
        out = {"X-Forwarded-For": "1.2.3.4"}
        assert get(fbase, "/api/all", headers={**out, "X-Telegram-Init-Data": init_data(BEN)})[0] == 200
        assert get(fbase, "/api/all", headers={**out, "X-Telegram-Init-Data": init_data(12345)})[0] == 401
        assert get(fbase, "/api/all", headers={**out, "X-Telegram-Init-Data": init_data(BEN).replace("q1", "q2")})[0] == 401
        r = httpx.post(fbase + "/api/session", headers={**out, "X-Telegram-Init-Data": init_data(ANDY)})
        assert r.status_code == 204 and "floor_session=" in r.headers.get("set-cookie", "")
        print("Mini App data: an operator's opens it, anyone else's or a tampered one doesn't")

        code, bots = get(fbase, "/api/bots")
        assert code == 200, bots
        by = {b["id"]: b for b in bots["bots"]}
        for b in bots["bots"]:
            print(f"  {b['name']:16} {b['now']['state']:8} {b['now']['headline']} | {b['now'].get('detail', '')[:60]}"
                  f" | plate: {[p['stage'] + ' ' + p['task'] for p in b['plate']]}")
        assert by["worker.protomegatron"]["now"]["state"] == "chat", by["worker.protomegatron"]["now"]
        assert "Ben" in by["worker.protomegatron"]["now"]["headline"] and "Protobots" in by["worker.protomegatron"]["now"]["headline"]
        assert "grounding lemma" in by["worker.protomegatron"]["now"]["detail"]
        assert by["worker.protomegabot2"]["now"]["state"] == "task", by["worker.protomegabot2"]["now"]
        assert by["reviewer.astra"]["now"]["state"] == "cantrun"
        assert by["worker.protocosmo2"]["now"]["state"] == "nodata"
        assert any(p["stage"] == "Assigned" for p in by["worker.protomegatron"]["plate"])
        assert any(p["stage"] == "Blocked" for p in by["worker.protocosmo2"]["plate"])
        code, needs = get(fbase, "/api/needs")
        print("needs:", [n["title"] for n in needs["needs"]])
        assert any("Astra" in n["title"] for n in needs["needs"])
        assert any("tidy-tests" in n["title"] or "merge" in n["title"].lower() for n in needs["needs"]), needs
        # a request from the chief of staff to the operators shows; a branch deleted on purpose is no longer a merge to do
        commit("workspace", "hive-requests/more-memory-for-vm2.md", "# More memory\n", "request")
        sh("git", "--git-dir", str(tmp / "hive-appliance.git"), "branch", "-D", "hive/tidy-tests")
        code, needs = get(fbase, "/api/needs")
        titles = [n["title"] for n in needs["needs"]]
        print("needs after:", titles)
        assert "Request from the chief of staff: more memory for vm2" in titles, titles
        assert not any("tidy-tests" in t for t in titles), "a deleted branch isn't a merge to do"
        code, w = get(fbase, "/api/work")
        print("work:", [(s["name"], [i["id"] for i in s["items"]]) for s in w["stages"]])
        code, chats = get(fbase, "/api/chats")
        print("chats:", [(c["name"], c["last"]) for c in chats["chats"]])
        code, one = get(fbase, "/api/bot/worker.protomegabot2")
        assert code == 200 and one["events"], one
        print("timeline:", [e["text"][:50] for e in one["events"][:4]])
        code, status = get(fbase, "/api/status")
        print("status:\n" + status["text"])
        assert "ProtomegaTron" in status["text"]
        code, page = get(fbase, "/")
        assert code == 200 and "<html" in page
        print("ALL FLOOR CHECKS PASSED")
        if serve:
            print(f"serving: {fbase}  (Ctrl-C to stop)")
            while True:
                for b in ("worker.protomegatron", "worker.protomegabot2"):
                    pass
                time.sleep(30)
                n = time.time()
                beacon(fbase + "/api/beacon", "proto-hive", tmp / "worker.protomegabot2.key", {
                    "v": 1, "actor": "worker.protomegabot2", "kind": "iter", "at": n, "started": now - 9000, "requests": [],
                    "queued": 0, "last_answer": None, "answers": [], "last_step": n - 10,
                    "episodes": [[now - 1000, None, "claude-opus-5-5"]], "pending_actions": 0})
    finally:
        for p in procs:
            p.terminate()
        scratch.down(pg.name) if hasattr(scratch, "down") else None


if __name__ == "__main__":
    main()
