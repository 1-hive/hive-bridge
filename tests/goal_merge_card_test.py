#!/usr/bin/env python3
"""Accept + merge (hive-runtime merge.py): a completed goal's card says what its reviewed code would put into main, and
✅ Accept + merge merges exactly that plan, then accepts. Before this the operator merged by hand after accepting
(agents can't push a code repository's main). /merge <goal> does it for a goal accepted already.

Checks: the card's merge section and buttons (mergeable, nothing to merge, a conflict); a tap merges the plan the card
showed and then accepts, with the outcome in the note and the chief of staff told; a changed plan merges nothing and
sends a current card; a failed merge accepts nothing; Accept only says how to merge later; /merge and its button;
/merge alone lists what's waiting; merge_plan and run_merge read merge.py's JSON.

    python3 tests/goal_merge_card_test.py
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
os.environ["HIVE_MERGE_PY"] = ""
spec = importlib.util.spec_from_file_location("bridge", HERE / "telegram-bridge.py")
b = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(b)
except SystemExit:
    pass

goals = {g: {"id": g, "title": f"Goal {g}", "objective": "Do it.", "outcome": "Done.", "status": "completed",
             "status_since": n, "summary": {"repository": "workspace", "commit_oid": "sha1:s", "path": f"{g}-summary.md"}}
         for n, g in enumerate(["gm", "gn", "gc"])}
goals["old"] = {"id": "old", "title": "Accepted earlier", "status": "accepted", "status_since": 9}


def mkplan(goal, digest, mergeable, lines, problems=()):
    return {"goal": goal, "title": goals[goal]["title"], "status": goals[goal]["status"], "digest": digest,
            "mergeable": mergeable, "nothing": not mergeable and not problems, "lines": lines, "problems": list(problems)}


plans = {"gm": mkplan("gm", "d1", True, ["mtg-player main: fast-forward to hive/s1-2-ladder c2135f2",
                                         "  (includes hive/s1-1-harness)"]),
         "gn": mkplan("gn", "d0", False, []),
         "gc": mkplan("gc", "d2", False, ["mtg-player main: CONFLICT merging hive/t3 abc1234"],
                      ["mtg-player: merging hive/t3 into main conflicts in a.txt"]),
         "old": mkplan("old", "d9", True, ["mtg-player main: merge hive/s0-3-alts 3f0635d"])}
inbox, emitted, runs = [], [], []
run_result = {"ok": True}


def hive(*a):
    if a[0] == "goal":
        return True, json.dumps({"goal": goals[a[1]]})
    if a[:3] == ("inbox", "--for", "operator"):
        return True, json.dumps(inbox)
    if a[0] == "state":
        return True, json.dumps({"goals": goals, "tasks": {}})
    if a[0] == "emit":
        emitted.append(a)
        return True, json.dumps({"position": 2300})
    return True, "[]"


def run_merge(gid, digest, via):
    runs.append((gid, digest, via))
    if not run_result["ok"]:
        return False, "mtg-player: not merged (main moved)"
    return True, {"ok": True, "line": "mtg-player main fe7b2e4→c2135f2 (hive/s1-2-ladder)"}


b.hive = hive
b.MERGE_PY = "/fake/merge.py"
b.merge_plan = lambda gid: plans.get(gid)
b.run_merge = run_merge
b.result_text = lambda p: "# Summary\n\n## What acceptance means\n\nStage 1 is done.\n"
b.tg_file = lambda *a, **k: None
b.ALLOW = {"chat_id": -1, "user_id": 1}
b.CARDS = -1   # the cards chat is the private chat (no HIVE_CARDS_CHAT)
b.allowed = lambda frm, chat: frm.get("id") == 1
b.save_state = lambda st: None
b.CHAT_CMD = "cos-chat"
sent, calls = [], []
mid = [100]


def tg(method, **kw):
    sent.append((method, kw))
    mid[0] += 1
    return {"message_id": mid[0]}


b.tg = tg
b.safe = lambda method, **kw: calls.append((method, kw)) or {"message_id": 1}


def kb(kw):
    return [[x["callback_data"] for x in row] for row in (kw.get("reply_markup") or {}).get("inline_keyboard", [])]


def chat():
    out = []
    while not b.CHAT.empty():
        out.append(b.CHAT.get_nowait())
    return out


# Cards: mergeable, nothing to merge, a conflict.
st = {"notified": [], "pending_reason": {}}
inbox[:] = [{"kind": "goal_completed", "entity": "goal", "id": g, "key": f"k{g}", "position": 2200 + i, "title": g}
            for i, g in enumerate(["gm", "gn", "gc"])]
b.push_inbox(st)
cards = [kw for m, kw in sent if m == "sendMessage"]
cm, cn, cc = cards
assert "🔀 <b>Accept + merge</b> also puts its reviewed code into main:" in cm["text"], cm["text"]
assert "<code>mtg-player main: fast-forward to hive/s1-2-ladder c2135f2</code>" in cm["text"], cm["text"]
assert kb(cm) == [["acceptmerge:gm"], ["accept:gm", "reopen:gm"]], kb(cm)
# Nothing to merge: no Accept to tap, it accepts itself in a few days (asking_is_approving_test.py has the rest).
assert "🔀" not in cn["text"] and "accepts itself on" in cn["text"] and kb(cn) == [["reopen:gn"]], (cn["text"], kb(cn))
assert "isn't merged from here" in cc["text"] and "conflicts in a.txt" in cc["text"] and "merge task" in cc["text"]
assert kb(cc) == [["accept:gc", "reopen:gc"]], kb(cc)
assert st["merge_shown"] == {"-1:101": "d1", "-1:102": None, "-1:103": None}, st["merge_shown"]   # by chat:message

# Accept + merge, plan unchanged: merged, then accepted; the note has the outcome; cos is told.
msg = {"chat": {"id": -1}, "message_id": 101, "text": "card"}
b.on_callback({"id": "c1", "from": {"id": 1}, "message": msg, "data": "acceptmerge:gm"}, st)
assert runs == [("gm", "d1", "Telegram: Accept + merge")], runs
assert emitted and emitted[-1][:4] == ("emit", "goal.accepted", "--goal", "gm"), emitted
note = json.loads(emitted[-1][5])["note"]
assert note.startswith("Accept + merge. Merged into main: mtg-player main fe7b2e4→c2135f2"), note
edit = [kw for m, kw in calls if m == "editMessageText"][-1]
assert "✅ goal.accepted at position 2300" in edit["text"] and "🔀 Merged: mtg-player main" in edit["text"], edit
told = chat()
assert len(told) == 1 and "accepted goal gm and merged goal gm's reviewed code" in told[0][1], told
assert "-1:101:acceptmerge" in st["decided"]
# Tapped twice: already done.
runs.clear()
b.on_callback({"id": "c1b", "from": {"id": 1}, "message": msg, "data": "acceptmerge:gm"}, st)
assert not runs

# The plan changed since the card (main moved, another result passed): nothing merged or accepted; a current card.
emitted.clear()
sent.clear()
plans["gm"] = mkplan("gm", "d1b", True, ["mtg-player main: merge hive/s1-2-ladder c2135f2"])
st2 = {"notified": [], "pending_reason": {}, "merge_shown": {"101": "d1"}}
b.on_callback({"id": "c2", "from": {"id": 1}, "message": msg, "data": "acceptmerge:gm"}, st2)
assert not runs and not emitted, (runs, emitted)
new = sent[-1][1]
assert "Not merged or accepted: what would be merged changed" in new["text"] and "merge hive/s1-2-ladder" in new["text"]
assert kb(new)[0] == ["acceptmerge:gm"] and st2["merge_shown"][f"-1:{mid[0]}"] == "d1b", (kb(new), st2["merge_shown"])
assert any(m == "editMessageReplyMarkup" and kw.get("message_id") == 101 for m, kw in calls), "old card's buttons go"

# Merging fails: nothing accepted, the card keeps its buttons.
run_result["ok"] = False
newmsg = {"chat": {"id": -1}, "message_id": mid[0], "text": "card"}
b.on_callback({"id": "c3", "from": {"id": 1}, "message": newmsg, "data": "acceptmerge:gm"}, st2)
assert runs and not emitted, (runs, emitted)
said = [kw["text"] for m, kw in calls if m == "sendMessage"][-1]
assert said.startswith("❌ Not merged or accepted: mtg-player: not merged (main moved)") and "Accept only" in said, said
assert f"-1:{mid[0]}:acceptmerge" not in st2.get("decided", []), "it can be tapped again"
run_result["ok"] = True

# Accept only on a mergeable card: accepted, and the status says how to merge later.
calls.clear()
b.on_callback({"id": "c4", "from": {"id": 1}, "message": newmsg, "data": "accept:gm"}, st2)
assert emitted and emitted[-1][1] == "goal.accepted"
edit = [kw for m, kw in calls if m == "editMessageText"][-1]
assert "send /merge gm to put it into main" in edit["text"], edit
# ... but not on a card that offered no merge.
calls.clear()
b.on_callback({"id": "c5", "from": {"id": 1}, "message": {**msg, "message_id": 102}, "data": "accept:gn"}, st)
assert "/merge" not in [kw for m, kw in calls if m == "editMessageText"][-1]["text"]

# /merge <goal>: a merge card; its button merges, and accepts nothing.
sent.clear()
runs.clear()
emitted.clear()
chat()
st3 = {"notified": [], "pending_reason": {}, "offset": 0}
b.on_message({"message_id": 5, "chat": {"id": -1, "type": "private"}, "from": {"id": 1}, "text": "/merge old"}, st3, None)
card = sent[-1][1]
assert "🔀 <b>Merge goal old</b> (accepted)" in card["text"] and "merge hive/s0-3-alts" in card["text"], card["text"]
assert kb(card) == [["merge:old"]] and st3["merge_shown"][f"-1:{mid[0]}"] == "d9", kb(card)
b.on_callback({"id": "c6", "from": {"id": 1}, "message": {"chat": {"id": -1}, "message_id": mid[0], "text": "c"},
               "data": "merge:old"}, st3)
assert runs == [("old", "d9", "Telegram: /merge")] and not emitted, (runs, emitted)
told = chat()
assert told and told[0][1].startswith("The operator merged goal old's reviewed code"), told

# /merge alone: the recent goals with reviewed code waiting.
sent.clear()
b.on_message({"message_id": 6, "chat": {"id": -1, "type": "private"}, "from": {"id": 1}, "text": "/merge"}, st3, None)
listing = sent[-1][1]["text"]
assert "<code>/merge old</code>" in listing and "<code>/merge gm</code>" in listing and "gn" not in listing, listing

# merge_plan and run_merge read merge.py's JSON (a stand-in script here).
fake = tmp / "merge.py"
fake.write_text("import json, sys\n"
                "a = sys.argv[1:]\n"
                "if a[0] == 'plan': print(json.dumps({'goal': a[1], 'digest': 'dd', 'mergeable': True, 'lines': []}))\n"
                "elif a[3] == 'bad': print(json.dumps({'ok': False, 'refused': 'the plan changed'})); sys.exit(1)\n"
                "else: print(json.dumps({'ok': True, 'line': 'x', 'via': a[5]}))\n")
spec2 = importlib.util.spec_from_file_location("bridge2", HERE / "telegram-bridge.py")
b2 = importlib.util.module_from_spec(spec2)
try:
    spec2.loader.exec_module(b2)
except SystemExit:
    pass
b2.MERGE_PY = str(fake)
assert b2.merge_plan("g")["digest"] == "dd"
assert b2.run_merge("g", "dd", "Telegram: /merge") == (True, {"ok": True, "line": "x", "via": "Telegram: /merge"})
assert b2.run_merge("g", "bad", "v") == (False, "the plan changed")

print("PASSED: completed-goal card shows what Accept + merge merges; the tap merges the plan it showed, then accepts "
      "(changed plan: current card; failure: nothing accepted); Accept only points at /merge; /merge card and list")
