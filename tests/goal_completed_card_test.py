#!/usr/bin/env python3
"""A completed goal's card says what the work found, not only what the goal was: the chief of staff's outcome, the
summary's "What acceptance means" section and the relevance line, with the asked objective folded, and the summary
file sent under the card. Before this the operator saw only the goal and had to find the results elsewhere to decide
between Accept and Reopen (1-hive, 2026-11-12). Also: a card whose HTML Telegram refuses goes as plain text, and a
tap on Accept keeps the card's formatting.

    python3 tests/goal_completed_card_test.py
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

SUMMARY = """# Goal summary: is A better than B?

## Answer

**No: A is no better than B.** 51.0% [49.0, 53.0] over 4,000 games.

## What's open

- A larger test.

## What acceptance means

Our player stays `B`. **Merge repo `abc1234` (`hive/t-1`) into `main`.** Expect a small conflict in `x<y>.py`.
"""
PIN = {"repository": "workspace", "commit_oid": "sha1:abc", "path": "projects/p/goals/2026-11-12-g1-summary.md"}
GOAL = {"id": "g1", "title": "Is A better than B?", "objective": "Measure A against B over paired games & report.",
        "relevance": "Wasted if the seeds overlap.", "status": "completed",
        "outcome": "No: A 51.0% [49.0,53.0] vs B over 4,000 games. Nothing to merge but the tooling.",
        "summary": PIN}

goals = {"g1": GOAL}
b.hive = lambda *a: (True, json.dumps({"goal": goals[a[1]]})) if a[0] == "goal" else \
    (True, json.dumps([{"kind": "goal_completed", "entity": "goal", "id": "g1", "key": "k1", "position": 1834}])) \
    if a[:3] == ("inbox", "--for", "operator") else (True, "[]")
b.result_text = lambda pin: SUMMARY if pin == PIN else None
b.ALLOW = {"chat_id": -1, "user_id": 1}
b.save_state = lambda st: None
sent, files = [], []
b.tg = lambda method, **kw: sent.append((method, kw)) or {"message_id": 77}
b.tg_file = lambda method, field, f, **kw: files.append((method, f.name, f.read_text(), kw)) or {"message_id": 78}

st = {"notified": [], "pending_reason": {}}
b.push_inbox(st)
(method, kw), = sent
text = kw["text"]
assert method == "sendMessage" and kw["parse_mode"] == "HTML", kw
assert text.startswith("🏁 Goal completed: accept or reopen\n\n<b>Is A better than B?</b>"), text
assert "<b>Result:</b> No: A 51.0% [49.0,53.0] vs B over 4,000 games." in text, text
assert ("<b>If you accept:</b>\nOur player stays <code>B</code>. <b>Merge repo <code>abc1234</code> "
        "(<code>hive/t-1</code>) into <code>main</code>.</b> Expect a small conflict in <code>x&lt;y&gt;.py</code>.") in text, text
assert "<i>Doesn't count, even if every check passes, when:</i> Wasted if the seeds overlap." in text
assert "<blockquote expandable>Measure A against B over paired games &amp; report.</blockquote>" in text
assert "A larger test" not in text, "only the acceptance section goes on the card; the rest is in the file"
assert text.endswith("<code>goal g1 · @1834</code>"), text
assert kw["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "accept:g1"
(fm, fname, body, fkw), = files
assert fm == "sendDocument" and fname == "2026-11-12-g1-summary.md" and body == SUMMARY, (fm, fname)
assert fkw["reply_to_message_id"] == 77 and fkw["chat_id"] == -1, fkw
assert st["notified"] == ["k1"]

# An unreadable summary: the card says so, and still carries the outcome; no file.
sent.clear(); files.clear()
b.result_text = lambda pin: None
text, summary, _ = b.completed_text("g1")
assert summary is None and "<b>Result:</b>" in text and "couldn't be read here" in text and "If you accept" not in text

# Long fields (at the record's limits: title 200, outcome 500, relevance 300, objective 1000) with markup to escape:
# the card stays under Telegram's limit, dropping the folded objective first, then the acceptance section.
b.result_text = lambda pin: SUMMARY
goals["g2"] = {**GOAL, "id": "g2", "title": "T&" * 100, "outcome": "<o> " * 125, "relevance": "r&" * 150,
               "objective": "x&" * 500}
text, _, _ = b.completed_text("g2")
assert len(text) <= b.CARD_MAX - 200 and "blockquote" not in text and "If you accept" in text, len(text)
b.result_text = lambda pin: SUMMARY.replace("Our player stays", "`a<b>` & " * 300 + "Our player stays")
text, _, _ = b.completed_text("g2")
assert len(text) <= b.CARD_MAX - 200 and "If you accept" not in text and "<b>Result:</b>" in text, len(text)
b.result_text = lambda pin: SUMMARY

# Telegram refuses the HTML: the card goes as plain text, buttons kept.
calls = []
def picky(method, **kw):
    calls.append(kw)
    if kw.get("parse_mode"):
        raise RuntimeError("telegram sendMessage: Bad Request: can't parse entities: Unsupported start tag")
    return {"message_id": 79}
b.tg = picky
assert b.send_card("<b>a &amp; b</b> <code>x&lt;y</code>", [[{"text": "ok"}]])["message_id"] == 79
assert calls[-1]["text"] == "a & b x<y" and "parse_mode" not in calls[-1] and calls[-1]["reply_markup"]

# Accept keeps the card's formatting when it adds the outcome of the tap.
edits = []
b.allowed = lambda frm, chat: True
b.safe = lambda method, **kw: edits.append((method, kw)) or {"ok": True}
b.emit = lambda etype, gid, data=None: (True, f"{etype} {gid} at position 1835")
ents = [{"type": "bold", "offset": 0, "length": 5}]
b.on_callback({"id": "c1", "from": {"id": 1}, "message": {"chat": {"id": -1}, "message_id": 77, "text": "Card.",
                                                          "entities": ents}, "data": "accept:g1"}, {"decided": []})
m, kw = next(e for e in edits if e[0] == "editMessageText")
assert kw["entities"] == ents and kw["text"] == "Card.\n\n✅ goal.accepted g1 at position 1835", kw

# Other inbox cards are unchanged: a proposed goal shows the goal.
assert "<b>Result:</b>" not in b.goal_text("g1")

print("PASSED: a completed goal's card carries its result, what acceptance means and the summary file; long cards "
      "fit; refused HTML falls back to plain text; Accept keeps the formatting")
