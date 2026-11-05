#!/usr/bin/env python3
"""New repositories by button: a chief-of-staff request with `kind: new-repository` is posted once, as a reply to
the message that asked, with Create / Decline; only an operator's tap counts, once; Create runs HIVE_NEW_REPO_CMD
with the name and description, records hive.registry_changed with the digest it printed (citing the asking
message) and tells the chief of staff; a failing command records nothing; Decline runs nothing.

    python3 tests/new_repo_buttons_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
WORK_CHAT, BEN, ZAR = -100123, 402, 181


def sh(*cmd, cwd=None):
    subprocess.run(cmd, check=True, capture_output=True, cwd=cwd)


def load(tmp: Path, cmd: Path):
    cfg, work = tmp / "cfg", tmp / "work"
    cfg.mkdir()
    work.mkdir()
    (cfg / "telegram-token").write_text("TEST")
    (cfg / "telegram.json").write_text(json.dumps({"chat_id": 1, "user_id": 1}))
    (cfg / "telegram-groups.json").write_text(json.dumps({
        "chats": [{"name": "work", "chat_id": WORK_CHAT, "mode": "all"}],
        "humans": [{"name": "ben", "user_id": BEN, "permissions": ["operator"]},
                   {"name": "zar", "user_id": ZAR, "permissions": []}]}))
    ws = tmp / "workspace.git"
    os.environ.update(HIVE_CONFIG_DIR=str(cfg), HIVE_WORK_DIR=str(work), HIVE_WORKSPACE_REPO=str(ws),
                      HIVE_NEW_REPO_CMD=f"{cmd} --flag")
    sys.path.insert(0, str(HERE))
    sys.modules.pop("hive_paths", None)
    spec = importlib.util.spec_from_file_location("bridge_newrepo", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    b.STATE.parent.mkdir(parents=True, exist_ok=True)
    return b


def request(tmp: Path, name: str, body: str) -> None:
    clone = tmp / "ws"
    (clone / "hive-requests").mkdir(parents=True, exist_ok=True)
    (clone / "hive-requests" / name).write_text(body)
    sh("git", "-C", str(clone), "add", "-A")
    sh("git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", name)
    sh("git", "-C", str(clone), "push", "-q", "origin", "HEAD:main")


def main():
    tmp = Path(tempfile.mkdtemp())
    calls = tmp / "calls.txt"
    cmd = tmp / "new-repo"
    cmd.write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >> {calls}
[ "$2" = broken ] && {{ echo "new-repo: GitHub refused"; exit 1; }}
echo "== GitHub"
echo "ready: https://github.com/agents/$2 (staged). Record it now, or every pin is refused:"
echo "registry_digest=sha256:abc"
""")
    cmd.chmod(0o755)
    sh("git", "init", "-q", "--bare", "-b", "main", str(tmp / "workspace.git"))
    sh("git", "clone", "-q", str(tmp / "workspace.git"), str(tmp / "ws"))
    b = load(tmp, cmd)
    b.CHAT_CMD = "cos chat"
    b.save_state = lambda st: None
    sent, edits, answers, emitted = [], [], [], []

    def tg(method, **p):
        if method == "sendMessage":
            sent.append(p)
            return {"message_id": 500 + len(sent)}
        if method == "editMessageText":
            edits.append(p)
        if method == "answerCallbackQuery":
            answers.append(p.get("text"))
        return {}
    b.tg = tg
    b.hive = lambda *a: (emitted.append(a), (True, json.dumps({"position": 950})))[1]

    request(tmp, "2026-11-05-other.md", "# Request: something else\n")
    request(tmp, "2026-11-05-new-repo-mettadinc.md",
            "---\nkind: new-repository\nname: MeTTaDinC\ndescription: MeTTaDinC on Cetta\nasked_by: ben\n"
            f"cause: telegram:{WORK_CHAT}/132\n---\n# Request: new repository: mettadinc\n")
    st = {}
    b.push_repo_requests(st)
    b.push_repo_requests(st)
    assert len(sent) == 1, sent
    m = sent[0]
    assert m["chat_id"] == WORK_CHAT and m["reply_parameters"]["message_id"] == 132, m
    assert "MeTTaDinC" in m["text"] and "Asked by ben" in m["text"]
    key = next(iter(st["repo_requests"]))

    def tap(user, action="newrepo", mid=None):
        b.on_callback({"id": "cq", "from": {"id": user}, "data": f"{action}:{key}",
                       "message": {"message_id": mid or st["repo_requests"][key]["message"],
                                   "chat": {"id": st["repo_requests"][key]["chat"]}}}, st)

    tap(ZAR)
    assert answers[-1] == "only an operator can decide this" and not calls.exists()
    tap(BEN, mid=999)
    assert answers[-1] == "unknown request"
    tap(BEN)
    assert calls.read_text().splitlines() == ["--flag MeTTaDinC MeTTaDinC on Cetta"], calls.read_text()
    e = emitted[-1]
    assert e[:2] == ("emit", "hive.registry_changed") and json.loads(e[3])["registry_digest"] == "sha256:abc", e
    assert e[4:] == ("--cause-external", f"telegram:{WORK_CHAT}/132"), e
    assert "✅ https://github.com/agents/MeTTaDinC is ready and registered (position 950) (ben)." in edits[-1]["text"]
    cos = b.CHAT.get()[1]
    assert "mettadinc is ready and registered" in cos and 'repos: ["mettadinc"]' in cos, cos
    tap(BEN)
    assert answers[-1] == "already decided" and len(calls.read_text().splitlines()) == 1

    # Decline: nothing runs; a failing command: nothing recorded
    request(tmp, "2026-11-06-new-repo-x.md", "---\nkind: new-repository\nname: x-thing\n---\n")
    request(tmp, "2026-11-06-new-repo-broken.md", "---\nkind: new-repository\nname: broken\n---\n")
    b.push_repo_requests(st)
    assert len(sent) == 3 and all(s["chat_id"] == 1 and "reply_parameters" not in s for s in sent[1:]), sent[1:]
    keys = {v["name"]: k for k, v in st["repo_requests"].items()}
    key = keys["x-thing"]
    tap(BEN, "norepo")
    assert len(calls.read_text().splitlines()) == 1 and "Declined by ben" in edits[-1]["text"]
    assert "declined the new repository x-thing" in b.CHAT.get()[1]
    n = len(emitted)
    key = keys["broken"]
    tap(BEN)
    assert len(emitted) == n and "❌ creating it failed" in edits[-1]["text"], edits[-1]
    assert "failed" in b.CHAT.get()[1] and st["repo_requests"][key]["state"] == "failed"
    print("PASSED: posted once as a reply to the asking message; non-operators and stale messages refused; Create runs "
          "the command, records the registry with its digest citing the request, tells the chief of staff; one tap "
          "only; Decline runs nothing; a failure records nothing")


if __name__ == "__main__":
    main()
