#!/usr/bin/env python3
"""Cards in a group (HIVE_CARDS_CHAT, e.g. the proto-hive's Work group), and standing goals:

- inbox cards and the digest go to the cards chat; a tap there counts from any operator, never from anyone else, and
  never in another group; the card is edited in place (in that chat) and names who tapped;
- a Reopen's reason prompt goes to the same chat, and only an operator's reply to it is taken as the reason;
  /merge, /inbox and /digest from an operator there are handled, not passed to the chief of staff;
- state is keyed by chat and message, and a card sent before (bare message id, private chat) still works;
- standing goals (standing-*): no "start it" ping when approved, no idle wake, +30 days on an over-budget card;
- a question waiting 30 minutes isn't carded again in the group when it is already on its task's thread.

    python3 tests/cards_chat_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
WORK, OTHER, PRIVATE = -1001, -2002, 77
ANDY, BEN, ZAR = 11, 12, 13


def load(cards: int | None):
    tmp = Path(tempfile.mkdtemp())
    cfg, work = tmp / "cfg", tmp / "work"
    cfg.mkdir()
    work.mkdir()
    (cfg / "telegram-token").write_text("TEST")
    (cfg / "telegram.json").write_text(json.dumps({"chat_id": PRIVATE, "user_id": ANDY}))
    (cfg / "telegram-groups.json").write_text(json.dumps({
        "chats": [{"name": "work", "chat_id": WORK, "mode": "all"}, {"name": "protobots", "chat_id": OTHER, "mode": "mention"}],
        "humans": [{"name": "andy", "user_id": ANDY, "permissions": ["operator"]},
                   {"name": "ben", "user_id": BEN, "permissions": ["operator"]},
                   {"name": "zar", "user_id": ZAR, "permissions": []}]}))
    os.environ.update(HIVE_CONFIG_DIR=str(cfg), HIVE_WORK_DIR=str(work))
    os.environ.pop("HIVE_CARDS_CHAT", None)
    if cards:
        os.environ["HIVE_CARDS_CHAT"] = str(cards)
    sys.path.insert(0, str(HERE))
    sys.modules.pop("hive_paths", None)
    spec = importlib.util.spec_from_file_location(f"bridge_cards_{cards}", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    b.STATE.parent.mkdir(parents=True, exist_ok=True)
    b.save_state = lambda st: None
    b.log = lambda m: None
    return b


class Tg:
    """Telegram, recorded: every call, and message ids per chat."""

    def __init__(self):
        self.calls, self.next = [], 500

    def __call__(self, method, **p):
        self.calls.append((method, p))
        self.next += 1
        return {"message_id": self.next, "chat": {"id": p.get("chat_id")}, "text": p.get("text", "")}

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


def wire(b):
    t = Tg()
    b.tg = t
    b.safe = t
    emitted = []

    def emit(etype, gid, data=None):
        emitted.append((etype, gid, data))
        return True, f"ok: {etype} {gid}"
    b.emit = emit
    return t, emitted


def cq(chat, who, data, mid=900, text="🆕 card"):
    return {"id": "cb", "from": {"id": who}, "data": data,
            "message": {"message_id": mid, "chat": {"id": chat}, "text": text}}


def test_cards_chat() -> None:
    b = load(WORK)
    assert b.CARDS == WORK
    t, emitted = wire(b)
    st = {"offset": 0, "notified": [], "pending_reason": {}}
    # cards go to the Work group
    b.send_card("🆕 Goal proposed", [[{"text": "Approve", "callback_data": "approve:g1"}]])
    assert t.sent()[-1]["chat_id"] == WORK
    # a non-operator's tap, and a tap in another group: refused, nothing recorded
    b.on_callback(cq(WORK, ZAR, "approve:g1"), st)
    b.on_callback(cq(OTHER, BEN, "approve:g1"), st)
    assert emitted == [] and [p.get("text") for p in t.sent("answerCallbackQuery")] == ["not allowed", "not allowed"]
    # Ben (operator) approves in the Work group: recorded, the card edited there, naming him
    b.inputs_changed = lambda st, msg, gid: False
    b.on_callback(cq(WORK, BEN, "approve:g1"), st)
    assert emitted == [("goal.approved", "g1", None)], emitted
    edit = t.sent("editMessageText")[-1]
    assert edit["chat_id"] == WORK and edit["message_id"] == 900 and edit["text"].endswith("(ben)"), edit
    assert f"{WORK}:900:approve" in st["decided"]
    b.on_callback(cq(WORK, ANDY, "approve:g1"), st)
    assert len(emitted) == 1 and t.sent("answerCallbackQuery")[-1]["text"] == "already done"
    # Reopen: the reason prompt in the same chat; Zar's reply isn't the reason, Andy's is
    b.on_callback(cq(WORK, ANDY, "reopen:g2", mid=901), st)
    prompt = t.sent()[-1]
    assert prompt["chat_id"] == WORK and "Reason to reopen" in prompt["text"] and "andy" in prompt["text"]
    (k,) = list(st["pending_reason"])
    assert k.startswith(f"{WORK}:"), k
    pid = int(k.split(":")[1])
    zar_reply = {"chat": {"id": WORK, "type": "supergroup"}, "from": {"id": ZAR}, "text": "nah", "message_id": 5,
                 "reply_to_message": {"message_id": pid}}
    assert b.take_reason(zar_reply, st) is False and st["pending_reason"]
    andy_reply = dict(zar_reply, **{"from": {"id": ANDY}, "text": "the ladder numbers are off"})
    assert b.take_reason(andy_reply, st) is True
    assert emitted[-1] == ("goal.reopened", "g2", {"reason": "the ladder numbers are off"}) and st["pending_reason"] == {}
    assert t.sent()[-1]["chat_id"] == WORK and t.sent()[-1]["text"].endswith("(andy)")
    # commands in the Work group, from operators only; on_message keeps them from the chief of staff
    calls = []
    b.merge_command = lambda st, text, chat=None: calls.append(("merge", text, chat))
    b.send_digest = lambda st: calls.append(("digest",))
    b.push_inbox = lambda st: calls.append(("inbox",))
    b.on_group_message = lambda m, st, cmd: calls.append(("cos", m.get("text")))
    for who, text in ((ANDY, "/merge g1"), (BEN, "/digest@protohive_cos_bot"), (ANDY, "/inbox"), (ZAR, "/merge g1"),
                      (ANDY, "let's do stage 2")):
        b.on_message({"chat": {"id": WORK, "type": "supergroup"}, "from": {"id": who}, "text": text, "message_id": 9}, st, "cos")
    assert calls == [("merge", "/merge g1", WORK), ("digest",), ("inbox",), ("cos", "/merge g1"), ("cos", "let's do stage 2")], calls
    # card state by chat and message
    b.remember_merge(st, {"message_id": 950, "chat": {"id": WORK}}, {"digest": "d1", "mergeable": True})
    assert b.card_shown(st, "merge_shown", WORK, 950) == "d1" and b.card_shown(st, "merge_shown", OTHER, 950) is None


def test_private_default_and_legacy() -> None:
    b = load(None)
    assert b.CARDS == PRIVATE
    t, emitted = wire(b)
    b.inputs_changed = lambda st, msg, gid: False
    # a card from before: decided and merge_shown keyed by the bare message id, in the private chat
    st = {"offset": 0, "notified": [], "pending_reason": {"321": ["abandon", "g9"]}, "decided": ["700:approve"],
          "merge_shown": {"710": "dd"}}
    b.on_callback(cq(PRIVATE, ANDY, "approve:g1", mid=700), st)
    assert emitted == [] and t.sent("answerCallbackQuery")[-1]["text"] == "already done"
    assert b.card_shown(st, "merge_shown", PRIVATE, 710) == "dd" and b.card_shown(st, "merge_shown", WORK, 710) is None
    assert b.take_reason({"chat": {"id": PRIVATE, "type": "private"}, "from": {"id": ANDY}, "text": "not needed",
                          "message_id": 3, "reply_to_message": {"message_id": 321}}, st)
    assert emitted[-1] == ("goal.abandoned", "g9", {"reason": "not needed"})
    b.send_card("x", [])
    assert t.sent()[-1]["chat_id"] == PRIVATE
    # Ben, an operator, tapping in the private chat of the bridge's main operator: allowed (as before, by id)
    assert b.allowed({"id": BEN}, {"id": PRIVATE}) and not b.allowed({"id": BEN}, {"id": WORK})


def test_standing_goals() -> None:
    b = load(WORK)
    ping = b.PINGS["goal.approved"][1]
    assert ping({"goal": "standing-wmtm", "position": 5}) is None
    assert "Start it now" in ping({"goal": "stage2-learn", "position": 6})
    clock = [1000.0]
    b.time.time = lambda: clock[0]
    st: dict = {}
    state = {"goals": {"standing-wmtm": {"id": "standing-wmtm", "status": "active", "title": "WMTM"},
                       "big": {"id": "big", "status": "active", "title": "Big"}}, "tasks": {}}
    b.ping_cos_idle(st, "cos", state)
    clock[0] += 3600
    b.ping_cos_idle(st, "cos", state)
    got = []
    while not b.CHAT.empty():
        got.append(b.CHAT.get()[1])
    assert len(got) == 1 and "Goal big" in got[0] and "standing-wmtm" not in st["goal_idle"], got
    over = {"kind": "escalation_open", "entity": "goal", "title": "budget_exceeded: wall clock", "id": "standing-wmtm"}
    assert b.budget_kb(over) == [[{"text": "⏱ +30 days", "callback_data": "budget:standing-wmtm:720"}]]
    assert b.budget_kb(dict(over, id="big"))[0][0]["text"] == "⏱ +4h"


def test_threaded_question_not_carded_again() -> None:
    b = load(WORK)
    t, _ = wire(b)
    q = {"kind": "task_blocked_unanswered_any", "entity": "task", "id": "t-7", "position": 40, "title": "T7",
         "key": "task_blocked_unanswered_any:t-7:40"}
    b.hive = lambda *a: (True, json.dumps([])) if a[:3] == ("inbox", "--for", "operator") else (True, "{}")
    b.waiting_questions = lambda st, now: [q]
    b.question_text = lambda tid: ""
    b.load_origins = lambda: {"t-7": {"chat_id": WORK, "message_id": 3}}
    st = {"offset": 0, "notified": [], "pending_reason": {}}
    b.push_inbox(st)
    assert t.sent() == [], "already on its thread in the Work group"
    b.load_origins = lambda: {}
    b.push_inbox(st)
    assert len(t.sent()) == 1 and t.sent()[0]["chat_id"] == WORK, "a question with no thread is carded"


def main():
    test_cards_chat()
    test_private_default_and_legacy()
    test_standing_goals()
    test_threaded_question_not_carded_again()
    print("ok")


if __name__ == "__main__":
    main()
