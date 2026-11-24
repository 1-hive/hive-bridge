#!/usr/bin/env python3
"""Merging on an operator's standing order (HIVE_AUTO_MERGE_REPOS, 2026-11-24).

- Off unless repositories are named (and the merge command is set).
- Only those repositories' pull requests, published in the last 14 days, oldest first, at most 4 merge runs a step.
- Merged: the line in reply to the PR's message, its 🔀 buttons cleared on every copy, the chief of staff told; a tap
  afterwards says it's merged already.
- Found merged already (merged on GitHub by hand): remembered and its buttons cleared, nothing said.
- Closed (superseded): buttons cleared, nothing said, not tried again.
- Refused (e.g. 405 while main moves, or a conflict): tried again every 10 min, 3 tries; then the chat and the chief of
  staff are told once and it's left alone. A new reviewed commit for the same pull request starts over.

    python3 tests/auto_merge_test.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
tmp = Path(tempfile.mkdtemp())
os.environ["HIVE_WORK_DIR"] = str(tmp / "work")
os.environ["HIVE_MERGE_PY"] = ""
spec = importlib.util.spec_from_file_location("bridge", HERE / "telegram-bridge.py")
b = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(b)
except SystemExit:
    pass

calls = []
b.safe = lambda method, **kw: calls.append((method, kw)) or {"message_id": 1}
b.save_state = lambda st: None
b.CHAT_CMD = "cos-chat"
b.ALLOW = {"chat_id": -1, "user_id": 1}
b.HUMANS = {1: {"name": "andy", "permissions": ["operator"]}}
b.PRS = tmp / "prs.json"
results = tmp / "results.json"
args = tmp / "merge-args"
fake = tmp / "merge-pr"
fake.write_text(f"#!/usr/bin/env python3\nimport json, sys\nopen({str(args)!r}, 'a').write(sys.argv[1] + ' ' + sys.argv[2] + '\\n')\n"
                f"print(json.dumps(json.load(open({str(results)!r}))[sys.argv[1]]))\n")
fake.chmod(0o755)


def chat():
    out = []
    while not b.CHAT.empty():
        out.append(b.CHAT.get_nowait())
    return out


def merged_args():
    return args.read_text().split("\n")[:-1] if args.exists() else []


now = time.time()
SHA = {n: f"{n:x}" * 40 for n in range(1, 16)}
url = lambda repo, n: f"https://github.com/{repo}/pull/{n}"
prs = {
    "k7": {"url": url("o/r", 7), "sha": SHA[1][:40], "task": "t-7", "at": now - 900, "messages": [[-50, 701], [-1, 702]]},
    "k8": {"url": url("o/r", 8), "sha": SHA[2][:40], "task": "t-8", "at": now - 800, "messages": [[-50, 801]]},
    "k3": {"url": url("x/y", 3), "sha": SHA[3][:40], "task": "t-x", "at": now - 950, "messages": [[-50, 301]]},
    "k2": {"url": url("o/r", 2), "sha": SHA[4][:40], "task": "t-old", "at": now - 20 * 86400, "messages": [[-50, 201]]},
    "k9": {"url": url("o/r", 9), "sha": SHA[5][:40], "task": "t-9", "at": now - 700, "messages": [[-50, 901]]},
    "k10": {"url": url("o/r", 10), "sha": SHA[6][:40], "task": "t-10", "at": now - 600, "messages": [[-50, 1001]]},
    "k11": {"url": url("o/r", 11), "sha": SHA[7][:40], "task": "t-11", "at": now - 500, "messages": [[-50, 1101]]},
}
b.PRS.write_text(json.dumps(prs))
results.write_text(json.dumps({
    url("o/r", 7): {"ok": True, "merged_sha": "c" * 40, "base": "main"},
    url("o/r", 8): {"ok": True, "merged_sha": "d" * 40, "base": "main"},
    url("o/r", 9): {"ok": False, "why": "it was closed (e.g. a later pull request superseded it)"},
    url("o/r", 10): {"ok": False, "why": "it was already merged", "already": True},
    url("o/r", 11): {"ok": False, "why": "GitHub won't merge it: Pull Request is not mergeable"},
}))
b.PR_MERGE_CMD = str(fake)
st = {}

b.AUTO_MERGE = set()
b.auto_merge_prs(st)
assert not merged_args() and not calls, "off without repositories"

b.AUTO_MERGE, b.AUTO_MERGE_BY = {"o/r"}, "andy"
b.auto_merge_prs(st)
assert [a.split()[0] for a in merged_args()] == [url("o/r", n) for n in (7, 8, 9, 10)], merged_args()   # 4 a step
said = [(kw["chat_id"], kw.get("reply_to_message_id"), kw["text"]) for m, kw in calls if m == "sendMessage"]
assert said == [(-50, 701, f"🔀 Merged automatically, on andy's standing order, into main (merge commit {'c' * 10}): {url('o/r', 7)}"),
                (-50, 801, f"🔀 Merged automatically, on andy's standing order, into main (merge commit {'d' * 10}): {url('o/r', 8)}")], said
cleared = sorted((kw["chat_id"], kw["message_id"]) for m, kw in calls if m == "editMessageReplyMarkup")
assert cleared == [(-50, 701), (-50, 801), (-50, 901), (-50, 1001), (-1, 702)], cleared
told = chat()
assert len(told) == 2 and all("standing order" in t[1] and "No reply needed" in t[1] for t in told), told
assert "task t-7" in told[0][1] and "task t-8" in told[1][1] and "o/r" in told[0][1], told
assert set(st["prs_merged"]) == {"k7", "k8", "k10"} and st["auto_merge"]["k9"]["n"] == 3, st

calls.clear()
b.auto_merge_prs(st)
assert [a.split()[0] for a in merged_args()[4:]] == [url("o/r", 11)], "next step: the 5th; done ones not again"
assert not calls and not chat() and st["auto_merge"]["k11"]["n"] == 1, (calls, st)
b.auto_merge_prs(st)
assert len(merged_args()) == 5, "not again within 10 minutes"
for _ in range(2):
    st["auto_merge"]["k11"]["at"] -= 601
    b.auto_merge_prs(st)
assert len(merged_args()) == 7 and st["auto_merge"]["k11"]["n"] == 3
said = [(kw["chat_id"], kw.get("reply_to_message_id"), kw["text"]) for m, kw in calls if m == "sendMessage"]
assert len(said) == 1 and said[0][:2] == (-50, 1101) and said[0][2].startswith(
    "❌ Not merged automatically: GitHub won't merge it: Pull Request is not mergeable"), said
told = chat()
assert len(told) == 1 and "task t-11" in told[0][1] and "rebased and published again" in told[0][1], told
st["auto_merge"]["k11"]["at"] -= 601
b.auto_merge_prs(st)
assert len(merged_args()) == 7, "given up on that commit"

prs["k11"]["sha"] = SHA[8][:40]   # republished with a new reviewed commit
b.PRS.write_text(json.dumps(prs))
results.write_text(json.dumps({**json.loads(results.read_text()),
                               url("o/r", 11): {"ok": True, "merged_sha": "e" * 40, "base": "main"}}))
b.auto_merge_prs(st)
assert merged_args()[-1] == f"{url('o/r', 11)} {SHA[8][:40]}" and "k11" in st["prs_merged"] and "k11" not in st["auto_merge"]
assert len(chat()) == 1

calls.clear()
b.on_pr_merge({"id": "p1", "from": {"id": 1}, "message": {"chat": {"id": -50}, "message_id": 701}}, st, "k7")
assert calls[-1][1]["text"] == "already merged" and len(merged_args()) == 8, "a tap after it: merged already"

b.AUTO_MERGE = {"*"}
b.auto_merge_prs(st)
assert merged_args()[-1].startswith(url("x/y", 3)), "* takes every repository"
print("PASSED: standing-order merges: off by default; named repositories only, last 14 days, oldest first, 4 a step; "
      "merged ones said, buttons cleared, chief of staff told; already merged and closed quietly; refusals 3 tries "
      "then said once; a new reviewed commit starts over; a tap afterwards says it's merged")
