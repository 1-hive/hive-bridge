#!/usr/bin/env python3
"""Decision cards answered by tapping (decision_buttons.py): buttons only for questions offering "word: … / word: …"
answers; a tap records who chose what once, and its line and the chief of staff's message say so; callback data
round-trips within Telegram's 64 bytes; the bridge routes "dq:" taps, refuses non-operators and unknown messages,
edits the message and queues the CoS message (stubbed Telegram).

    python3 tests/decision_buttons_test.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import decision_buttons as db  # noqa: E402

CARD = {"status": "The pin move is built, but I can't push it from pop-os.", "before": "posting the result",
        "questions": ["Should an operator log me in to GitHub on pop-os? yes: I push and post the result / no: see 2",
                      "Or will an operator push the branch from my checkout? yes: I post once it's there / no: I wait",
                      "Anything else I should know?"]}


def unit():
    assert db.options(CARD["questions"][0]) == ["yes", "no"]
    assert db.options(CARD["questions"][2]) == []
    assert db.options("Which one? A: the fast one / B: the safe one / later: decide next week") == ["A", "B", "later"]
    assert db.options("Pick? yes: go / this is prose without a colon") == []     # not every part is an answer
    assert db.options("No question mark yes: a / no: b") == []
    assert db.options("Dup? yes: a / Yes: b") == []
    rows = db.keyboard(CARD, "123456.789")
    assert [[b["text"] for b in r] for r in rows] == [["1 · yes", "1 · no"], ["2 · yes", "2 · no"]], rows
    assert all(len(b["callback_data"].encode()) <= 64 for r in rows for b in r)
    assert db.parse_data(rows[1][0]["callback_data"]) == ("123456.789", 1, 0)
    assert db.parse_data("dq:bad key:1:0") is None and db.parse_data("newrepo:x") is None
    e = db.new_entry("t-1", CARD, -100, 789, "❓ text")
    ok, note, line, cos = db.tap(e, 0, 1, "Andy")
    assert ok and line == "✅ 1 · no (Andy)" and "no" in note, (line, note)
    assert "t-1" in cos and "task.answered" in cos and "telegram:-100/789" in cos and "once the other" in cos, cos
    assert db.tap(e, 0, 0, "Ben")[:2] == (False, "already answered")
    assert db.tap(e, 2, 0, "Ben")[0] is False and db.tap(e, 9, 0, "Ben")[0] is False
    assert [[b["text"] for b in r] for r in db.keyboard(CARD, "k", e["answered"])] == [["2 · yes", "2 · no"]]
    ok, _, _, cos = db.tap(e, 1, 0, "Ben")
    assert ok and "once the other" not in cos, cos
    assert db.keyboard(CARD, "k", e["answered"]) == []


def bridge():
    tmp = Path(tempfile.mkdtemp())
    os.environ["HIVE_WORK_DIR"] = str(tmp / "work")
    spec = importlib.util.spec_from_file_location("bridge", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(b)
    except SystemExit:
        pass
    calls, queued = [], []
    b.safe = lambda method, **kw: calls.append((method, kw)) or {"message_id": 1}
    b.save_state = lambda st: None
    b.is_operator = lambda uid: uid == 1
    b.sender_name = lambda frm: {1: "Andy", 2: "Bot"}.get(frm.get("id"), "?")
    b.CHAT_CMD = "cos chat"
    b.CHAT.put = lambda item: queued.append(item)
    st = {}
    b.offer_buttons(st, "t-1", CARD, -100, {"message_id": 789}, "❓ text")
    key = next(iter(st["decision_cards"]))
    assert calls[-1][0] == "editMessageReplyMarkup" and len(calls[-1][1]["reply_markup"]["inline_keyboard"]) == 2
    b.offer_buttons(st, "t-2", {"questions": ["Free text?"], "status": "x"}, -100, {"message_id": 790}, "t")
    assert len(st["decision_cards"]) == 1                                  # nothing tappable: no buttons
    msg = {"chat": {"id": -100}, "message_id": 789}
    b.on_callback({"id": "c1", "from": {"id": 2}, "message": msg, "data": f"dq:{key}:0:0"}, st)
    assert calls[-1] == ("answerCallbackQuery", {"callback_query_id": "c1", "text": "only an operator can answer this here"})
    b.on_callback({"id": "c2", "from": {"id": 1}, "message": {"chat": {"id": -100}, "message_id": 5}, "data": f"dq:{key}:0:0"}, st)
    assert calls[-1][1]["text"] == "unknown question"
    b.on_callback({"id": "c3", "from": {"id": 1}, "message": msg, "data": f"dq:{key}:0:0"}, st)
    edit = [c for c in calls if c[0] == "editMessageText"][-1][1]
    assert edit["text"].endswith("✅ 1 · yes (Andy)") and len(edit["reply_markup"]["inline_keyboard"]) == 1, edit
    assert queued and "task.answered" in queued[-1][1] and queued[-1][0] == "cos chat", queued
    b.on_callback({"id": "c4", "from": {"id": 1}, "message": msg, "data": f"dq:{key}:0:1"}, st)
    assert calls[-1][1]["text"] == "already answered" and len(queued) == 1


if __name__ == "__main__":
    unit()
    bridge()
    print("PASSED: buttons only for answerable questions; one answer per question, shown and passed to the chief of "
          "staff; non-operators and unknown messages refused; callback data within 64 bytes")
