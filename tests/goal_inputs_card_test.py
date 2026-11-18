#!/usr/bin/env python3
"""A goal's approval card shows the host files approving it grants its agents (hive-runtime inputs.py: the goal
document's hive-inputs block), and an Approve tap can only grant what its card showed: if a revision changed the
inputs since, the goal isn't approved and a current card goes out instead. Goals without inputs, and other cards
about goals, look as before.

    python3 tests/goal_inputs_card_test.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
tmp = Path(tempfile.mkdtemp())
os.environ["HIVE_WORK_DIR"] = str(tmp / "work")
spec = importlib.util.spec_from_file_location("bridge", HERE / "telegram-bridge.py")
b = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(b)
except SystemExit:
    pass

DOC1 = """# Goal

```hive-inputs
~/opt/kev/runs/reader-ft-v1    Kev's served run, for the CPU latency test
```
"""
DOC2 = DOC1.replace("```\n", "~/.cache/huggingface/hub/models--Q    the base <model>\n```\n", 1)
docs = {"p1": DOC1, "p2": DOC2, "p0": "# Goal\n\nNo inputs.\n", "pbad": "```hive-inputs\n~/a why\n"}
pin = lambda k: {"repository": "workspace", "commit_oid": f"sha1:{k}", "path": "projects/p/goals/g.md"}
goals = {"g1": {"id": "g1", "title": "Is Kev worth the GPU?", "objective": "Decide.", "relevance": "Guessed.",
                "goal_pin": pin("p1")},
         "g0": {"id": "g0", "title": "No inputs", "objective": "x", "goal_pin": pin("p0")},
         "gb": {"id": "gb", "title": "Bad block", "objective": "x", "goal_pin": pin("pbad")}}
inbox = []
emitted = []


def hive(*a):
    if a[0] == "goal":
        return True, json.dumps({"goal": goals[a[1]]})
    if a[:3] == ("inbox", "--for", "operator"):
        return True, json.dumps(inbox)
    if a[0] == "emit":
        emitted.append(a)
        return True, json.dumps({"position": 2200})
    return True, "[]"


b.hive = hive
b.result_text = lambda p: docs.get(p["commit_oid"].split(":")[1])
b.ALLOW = {"chat_id": -1, "user_id": 1}
b.CARDS = -1   # the cards chat is the private chat (no HIVE_CARDS_CHAT)
b.allowed = lambda frm, chat: frm.get("id") == 1
b.save_state = lambda st: None
sent, calls = [], []
mid = [100]


def tg(method, **kw):
    sent.append((method, kw))
    mid[0] += 1
    return {"message_id": mid[0]}


b.tg = tg
b.safe = lambda method, **kw: calls.append((method, kw)) or {"message_id": 1}

st = {"notified": [], "pending_reason": {}}
inbox[:] = [{"kind": "goal_proposed", "entity": "goal", "id": "g1", "key": "k1", "position": 2107, "title": "g1"},
            {"kind": "goal_proposed", "entity": "goal", "id": "g0", "key": "k0", "position": 2108, "title": "g0"},
            {"kind": "escalation_open", "entity": "goal", "id": "g1", "key": "k2", "position": 2109,
             "title": "budget_exceeded: wall clock"}]
b.push_inbox(st)
c1, c0, c2 = (kw["text"] for m, kw in sent)
assert "🔓 <b>Approving lets its agents read these host files</b>" in c1 and "<code>~/opt/kev/runs/reader-ft-v1</code>" in c1, c1
assert "Kev's served run" in c1
assert "🔓" not in c0 and "⚠️" not in c0, "a goal without inputs: card as before"
assert "🔓" not in c2, "a budget alert on the goal doesn't list inputs"
assert st["inputs_shown"] == {"-1:101": [["~/opt/kev/runs/reader-ft-v1", "Kev's served run, for the CPU latency test"]],
                              "-1:102": []}, st["inputs_shown"]   # by chat:message

# Approve, unchanged: approved.
msg1 = {"chat": {"id": -1}, "message_id": 101, "text": "card"}
b.on_callback({"id": "c1", "from": {"id": 1}, "message": msg1, "data": "approve:g1"}, st)
assert emitted and emitted[-1][:4] == ("emit", "goal.approved", "--goal", "g1"), emitted

# A revision adds an input after the card went out: the tap doesn't approve; a current card follows.
emitted.clear()
sent.clear()
goals["g1"]["goal_pin"] = pin("p2")
st2 = {"notified": [], "pending_reason": {}, "inputs_shown": {"101": st["inputs_shown"]["-1:101"]}}   # a card from before: bare id
b.on_callback({"id": "c2", "from": {"id": 1}, "message": msg1, "data": "approve:g1"}, st2)
assert not emitted, "not approved"
assert any(m == "answerCallbackQuery" and "changed" in kw.get("text", "") for m, kw in calls)
assert any(m == "editMessageReplyMarkup" and kw.get("message_id") == 101 for m, kw in calls), "old card's buttons go"
new = sent[-1][1]
assert "Not approved: its host inputs changed" in new["text"] and "models--Q" in new["text"] and "&lt;model&gt;" in new["text"]
assert new["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "approve:g1"
new_id = mid[0]
assert len(st2["inputs_shown"][f"-1:{new_id}"]) == 2
b.on_callback({"id": "c3", "from": {"id": 1}, "message": {**msg1, "message_id": new_id}, "data": "approve:g1"}, st2)
assert emitted and emitted[-1][1] == "goal.approved", "the new card approves"

# A card from before this check, for a goal that now has inputs: refused too (it showed none).
emitted.clear()
b.on_callback({"id": "c4", "from": {"id": 1}, "message": {**msg1, "message_id": 7}, "data": "approve:g1"},
              {"notified": [], "pending_reason": {}})
assert not emitted
# ... and for a goal without inputs: approved as before.
b.on_callback({"id": "c5", "from": {"id": 1}, "message": {**msg1, "message_id": 8}, "data": "approve:g0"},
              {"notified": [], "pending_reason": {}})
assert emitted and emitted[-1][3] == "g0"

# A block that isn't closed: the card says the tasks won't launch.
assert "won't launch" in b.goal_text("gb", True)

print("PASSED: approval card lists the goal's host inputs; a tap approves only what its card showed (a changed goal "
      "gets a new card); goals without inputs and other goal cards unchanged")
