#!/usr/bin/env python3
"""Taps only where they decide something (2026-11-16).

- Operators' messages to the chief of staff are logged as their words (private chat, another operator's chat, a group);
  nobody else's.
- A proposed goal whose hive-request block quotes those words (in order, any case or punctuation, 3+ words, last 14
  days) and that asks for no host inputs is approved by the bridge itself: its card shows the words and ⏹ Stop. Words
  not said, too short, too old, host inputs, or HIVE_AUTO_START off: the usual Approve card.
- ⏹ Stop abandons the goal, tells the chief of staff to cancel its tasks, and the "abandoned goal with open tasks" card
  for it is held back while it does.
- A completed goal with nothing to merge gets ↩ Reopen only, no reminders, and accepts itself after the days set; one
  with code to merge, or a conflict, or no plan, keeps its buttons and never accepts itself.
- A published pull request's 🔀 Merge (telegram-say.py with HIVE_PR): the message carries the button and prs.json the
  PR; an operator's tap runs the merge command, clears the button on every copy and tells the chief of staff; a second
  tap or another copy says it's done; a refusal is shown and the button kept; non-operators are refused.

    python3 tests/asking_is_approving_test.py
"""
import datetime as dt
import importlib.util
import io
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

pin = lambda k: {"repository": "workspace", "commit_oid": f"sha1:{k}", "path": f"projects/p/goals/{k}.md"}
REQ = "```hive-request\nCan you build the singleton cube decks,\none per colour pair?\n```\n"
docs = {"asked": "# Goal\n\n" + REQ,
        "notsaid": "# Goal\n\n```hive-request\nplease retrain Kev on verification\n```\n",
        "short": "# Goal\n\n```hive-request\ngo ahead\n```\n",
        "inputs": "# Goal\n\n" + REQ + "\n```hive-inputs\n~/opt/kev/runs/x   the run\n```\n",
        "own": "# Goal\n\nThe next step after stage 1.\n"}
goals = {g: {"id": g, "title": f"Goal {g}", "objective": "Do it.", "status": "proposed", "goal_pin": pin(g)} for g in docs}
goals["done1"] = {"id": "done1", "title": "Finished, nothing to merge", "status": "completed", "outcome": "Done."}
goals["done2"] = {"id": "done2", "title": "Finished, code waiting", "status": "completed", "outcome": "Done."}
inbox, emitted = [], []


def hive(*a):
    if a[0] == "goal":
        return True, json.dumps({"goal": goals[a[1]]})
    if a[:3] == ("inbox", "--for", "operator"):
        return True, json.dumps(inbox)
    if a[0] == "emit":
        emitted.append(a)
        gid = a[a.index("--goal") + 1] if "--goal" in a else None
        if a[1] == "goal.approved" and gid:
            goals[gid]["status"] = "active"
        return True, json.dumps({"position": 3000 + len(emitted)})
    if a[0] == "state":
        return True, json.dumps({"actors": {}, "tasks": {}, "goals": goals})
    return True, "[]"


plans = {"done1": {"digest": "d1", "mergeable": False, "nothing": True, "lines": [], "problems": []},
         "done2": {"digest": "d2", "mergeable": True, "nothing": False, "lines": ["mtg-player main: merge hive/t x"],
                   "problems": []}}
b.hive = hive
b.result_text = lambda p: docs.get(p["commit_oid"].split(":")[1])
b.merge_plan = lambda gid: plans.get(gid)
b.completed_text = lambda gid, plan=None: (f"<b>{goals[gid]['title']}</b>", None, "")
b.ALLOW = {"chat_id": -1, "user_id": 1}
b.HUMANS = {1: {"name": "andy", "permissions": ["operator"]}, 2: {"name": "ben", "permissions": ["operator"]},
            3: {"name": "zar", "permissions": []}}
b.CHATS = {-50: {"name": "work", "mode": "all"}}
b.allowed = lambda frm, chat: frm.get("id") == 1 and chat.get("id") == -1
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


def logged():
    return [json.loads(x) for x in b.OPERATOR_WORDS.read_text().splitlines()] if b.OPERATOR_WORDS.exists() else []


# The operators' words: the operator chat, another operator's private chat, a group; not zar's.
st = {"notified": [], "pending_reason": {}, "offset": 0, "threads": {}}
b.STATE.parent.mkdir(parents=True, exist_ok=True)
b.on_message({"message_id": 1, "chat": {"id": -1, "type": "private"}, "from": {"id": 1},
              "text": "Can you build the singleton cube decks — one per colour pair?"}, st, "cos-chat")
b.on_message({"message_id": 2, "chat": {"id": 22, "type": "private"}, "from": {"id": 2}, "text": "ben asks for X"}, st, "cos-chat")
b.on_message({"message_id": 3, "chat": {"id": -50, "type": "supergroup"}, "from": {"id": 3}, "date": 1,
              "text": "zar: please retrain Kev on verification"}, st, "cos-chat")
b.on_message({"message_id": 4, "chat": {"id": -50, "type": "supergroup"}, "from": {"id": 2}, "date": 1,
              "text": "ben in the group"}, st, "cos-chat")
words = logged()
assert [(w["from"], w["via"]) for w in words] == [("andy", "Telegram"), ("ben", "Telegram, private"),
                                                   ("ben", "Telegram, work")], words
assert not any("retrain" in w["text"] for w in words), "a non-operator's words never count"
chat()
sent.clear()

# Proposed goals: asked (started), not said, too short, host inputs, the chief of staff's own.
inbox[:] = [{"kind": "goal_proposed", "entity": "goal", "id": g, "key": f"k{g}", "position": 2900 + i, "title": g}
            for i, g in enumerate(["asked", "notsaid", "short", "inputs", "own"])]
b.push_inbox(st)
cards = {kw["text"].rsplit("goal ", 1)[1].split(" ")[0]: kw for m, kw in sent if m == "sendMessage"}
assert [a[1:4] for a in emitted] == [("goal.approved", "--goal", "asked")], emitted
a = cards["asked"]
assert a["text"].startswith("▶️ Goal started: you asked for it") and kb(a) == [["stop:asked"]], a
assert "Asked by andy</b> (Telegram," in a["text"] and "Can you build the singleton cube decks" in a["text"], a["text"]
for g in ("notsaid", "short", "inputs", "own"):
    assert kb(cards[g]) == [[f"approve:{g}", f"abandon:{g}"]], (g, kb(cards[g]))
assert goals["notsaid"]["status"] == "proposed"

# Too old, or turned off: the usual card.
sent.clear()
emitted.clear()
goals["asked"]["status"] = "proposed"
old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=15)).isoformat(timespec="seconds")
real = b.OPERATOR_WORDS.read_text()
b.OPERATOR_WORDS.write_text("\n".join(json.dumps({**w, "at": old}) for w in words) + "\n")
st2 = {"notified": [], "pending_reason": {}}
inbox[:] = [{"kind": "goal_proposed", "entity": "goal", "id": "asked", "key": "kx", "position": 2950, "title": "asked"}]
b.push_inbox(st2)
assert not emitted and kb(sent[-1][1]) == [["approve:asked", "abandon:asked"]], (emitted, sent[-1])
b.OPERATOR_WORDS.write_text(real)
b.AUTO_START = False
st2 = {"notified": [], "pending_reason": {}}
b.push_inbox(st2)
assert not emitted and kb(sent[-1][1])[0][0] == "approve:asked"
b.AUTO_START = True
assert b.said("build the SINGLETON cube decks!") and not b.said("cube decks")   # punctuation and case; 3+ words
assert not b.said("build the cube decks"), "the words in order and together"

# ⏹ Stop: abandoned with the reason, the chief of staff told, its open-tasks card held back for a while.
emitted.clear()
calls.clear()
goals["asked"]["status"] = "active"
msg = {"chat": {"id": -1}, "message_id": 555, "text": "▶️ Goal started"}
b.on_callback({"id": "c1", "from": {"id": 1}, "message": msg, "data": "stop:asked"}, st)
assert emitted[-1][1:4] == ("goal.abandoned", "--goal", "asked") and \
    json.loads(emitted[-1][5])["reason"] == b.STOP_REASON, emitted
told = chat()
assert len(told) == 1 and "tapped ⏹ Stop on goal asked" in told[0][1] and "task.cancelled" in told[0][1], told
assert any(m == "editMessageText" and "⏹ goal.abandoned" in kw["text"] for m, kw in calls), calls
sent.clear()
inbox[:] = [{"kind": "goal_abandoned_open_tasks", "entity": "goal", "id": "asked", "key": "kab", "position": 3100,
             "title": "asked"}]
b.push_inbox(st)
assert not sent, "no 'abandoned with open tasks' card right after Stop"
b.on_callback({"id": "c1b", "from": {"id": 3}, "message": {**msg, "message_id": 556}, "data": "stop:asked"}, st)
assert emitted[-1][1] == "goal.abandoned" and len([e for e in emitted if e[1] == "goal.abandoned"]) == 1, "not zar"

# Completed: nothing to merge -> Reopen only, no reminder, accepts itself after the days set; code to merge -> as before.
sent.clear()
emitted.clear()
now = [1_800_000_000.0]
b.time.time = lambda: now[0]
st3 = {"notified": [], "pending_reason": {}}
inbox[:] = [{"kind": "goal_completed", "entity": "goal", "id": g, "key": f"k{g}", "position": 3200 + i, "title": g}
            for i, g in enumerate(["done1", "done2"])]
b.push_inbox(st3)
c1, c2 = (kw for m, kw in sent if m == "sendMessage")
assert c1["text"].startswith("🏁 Goal completed\n") and "accepts itself on" in c1["text"] and kb(c1) == [["reopen:done1"]], c1
assert "accept or reopen" in c2["text"] and kb(c2)[0] == ["acceptmerge:done2"], c2
sent.clear()
now[0] += 13 * 3600
b.push_inbox(st3)
texts = [kw["text"] for m, kw in sent]
assert len(texts) == 1 and texts[0].startswith("⏰ Still waiting") and "accept or reopen" in texts[0], "done2 only"
sent.clear()
now[0] += 3 * 86400
b.push_inbox(st3)
acc = [e for e in emitted if e[1] == "goal.accepted"]
assert len(acc) == 1 and acc[0][3] == "done1" and "Accepted on its own: 3 days" in json.loads(acc[0][5])["note"], emitted
assert any("accepted on its own" in kw.get("text", "") for m, kw in calls if m == "sendMessage"), calls
plans["done1"] = None   # merge.py couldn't plan it: a tap decides, never a timer
st4 = {"notified": [], "pending_reason": {}}
sent.clear()
inbox[:] = inbox[:1]
b.push_inbox(st4)
assert kb(sent[-1][1]) == [["accept:done1", "reopen:done1"]], kb(sent[-1][1])

# A published pull request's 🔀 Merge: telegram-say.py puts the button on every copy and remembers the PR.
say_spec = importlib.util.spec_from_file_location("say", HERE / "telegram-say.py")
say = importlib.util.module_from_spec(say_spec)
say_spec.loader.exec_module(say)
cfg = tmp / "cfg"
cfg.mkdir()
(cfg / "telegram-token").write_text("T")
(cfg / "telegram.json").write_text(json.dumps({"chat_id": -1}))
say.CFG = cfg
(b.STATE.parent).mkdir(parents=True, exist_ok=True)
(b.STATE.parent / "origins.json").write_text(json.dumps({"t-9": {"chat_id": -50, "message_id": 7, "status_id": 8}}))
posted, n = [], [900]


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def urlopen(url, data, timeout):
    import urllib.parse
    q = dict(urllib.parse.parse_qsl(data.decode()))
    posted.append(q)
    n[0] += 1
    return Resp(json.dumps({"ok": True, "result": {"message_id": n[0]}}).encode())


say.urllib.request.urlopen = urlopen
SHA = "a" * 40
os.environ.update(HIVE_TASK="t-9", HIVE_PR="https://github.com/o/r/pull/7", HIVE_PR_SHA=SHA)
sys.argv = ["telegram-say.py", "Reviewed and published: t-9 → r. Pull request: https://github.com/o/r/pull/7"]
say.main()
for k in ("HIVE_TASK", "HIVE_PR", "HIVE_PR_SHA"):
    del os.environ[k]
assert [q["chat_id"] for q in posted] == ["-50", "-1"], posted
key = json.loads(posted[0]["reply_markup"])["inline_keyboard"][0][0]["callback_data"].split(":")[1]
assert all(json.loads(q["reply_markup"])["inline_keyboard"][0][0]["callback_data"] == f"prmerge:{key}" for q in posted)
prs = json.loads(b.PRS.read_text())
assert prs[key]["url"].endswith("/pull/7") and prs[key]["sha"] == SHA and prs[key]["task"] == "t-9" \
    and prs[key]["messages"] == [[-50, 901], [-1, 902]], prs

fake = tmp / "merge-pr"
result = tmp / "merge-result"
fake.write_text(f"#!/bin/sh\necho \"$1 $2\" >> {tmp}/merge-args\ncat {result}\n")
fake.chmod(0o755)
b.PR_MERGE_CMD = str(fake)
calls.clear()
pmsg = {"chat": {"id": -50}, "message_id": 901, "text": "Reviewed and published"}
b.on_pr_merge({"id": "p0", "from": {"id": 3}, "message": pmsg}, st, key)
assert not (tmp / "merge-args").exists() and calls[-1][1]["text"] == "only an operator can merge", calls[-1]
result.write_text(json.dumps({"ok": False, "why": "its head moved since the review: not merged"}) + "\n")
b.on_pr_merge({"id": "p1", "from": {"id": 2}, "message": pmsg}, st, key)
assert (tmp / "merge-args").read_text().split() == ["https://github.com/o/r/pull/7", SHA]
said_ = [kw["text"] for m, kw in calls if m == "sendMessage"]
assert said_[-1] == "❌ Not merged (ben tapped): its head moved since the review: not merged", said_
assert not any(m == "editMessageReplyMarkup" for m, kw in calls), "the button stays"
chat()
result.write_text(json.dumps({"ok": True, "merged_sha": "c" * 40, "base": "main"}) + "\n")
b.on_pr_merge({"id": "p2", "from": {"id": 2}, "message": pmsg}, st, key)
cleared = sorted((kw["chat_id"], kw["message_id"]) for m, kw in calls if m == "editMessageReplyMarkup")
assert cleared == [(-50, 901), (-1, 902)], cleared
said_ = [kw["text"] for m, kw in calls if m == "sendMessage"]
assert said_[-1] == f"🔀 Merged by ben into main (merge commit {'c' * 10}): https://github.com/o/r/pull/7", said_
told = chat()
assert len(told) == 1 and "ben (operator) merged the pull request of task t-9" in told[0][1], told
b.on_pr_merge({"id": "p3", "from": {"id": 1}, "message": {**pmsg, "chat": {"id": -1}, "message_id": 902}}, st, key)
assert calls[-1][1]["text"] == "already merged" and len((tmp / "merge-args").read_text().splitlines()) == 2
b.on_callback({"id": "p4", "from": {"id": 1}, "message": pmsg, "data": f"prmerge:{key}"}, st)
assert calls[-1][1]["text"] == "already merged", "routed from on_callback in any chat"
print("PASSED: operators' words logged; a goal quoting them starts with ⏹ Stop (not said, short, old, host inputs, own "
      "initiative or off: Approve card); Stop abandons and tells the chief of staff; nothing to merge accepts itself "
      "after 3 days without reminders; PR 🔀 Merge button posted, merged by an operator's tap, refusals shown")
