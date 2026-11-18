#!/usr/bin/env python3
"""The chief of staff's replies say which model wrote them: cos.sh chat's first line "[[model: <model>]]" is taken
off and the reply goes out as "[<model>]" over the text (provider prefix and date suffix dropped). A reply without
that line, or with a model that doesn't look like one, goes out untagged; a limit reply is still recognised under the
line; HIVE_SHOW_MODEL=0 turns the tag off.

    python3 tests/model_tag_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cos_limit_test import LIMIT, load  # noqa: E402


def main():
    b = load()
    sent = []
    b.safe = lambda method, **p: sent.append((p["chat_id"], p["text"], p.get("reply_to_message_id"))) or {}
    b.send_attachments = lambda found, chat_id: []
    b.take_task_lines = lambda reply, target: reply
    answer = {}
    b.ask_cos = lambda item: answer["text"]
    work = {"chat_id": -100, "reply_to": 7}

    assert b.model_tag("claude-opus-5-5") == "[claude-opus-5-5]"
    assert b.model_tag("anthropic/claude-sonnet-5-5-20261101") == "[claude-sonnet-5-5]"
    assert b.model_tag("") == "" and b.model_tag(None) == "" and b.model_tag("two words") == ""
    assert b.take_model("[[model: claude-opus-5-5]]\nHello") == ("[claude-opus-5-5]", "Hello")
    assert b.take_model("Hello\n[[model: x]]") == ("", "Hello\n[[model: x]]")

    # tagged, in reply to the asker's message
    answer["text"] = "[[model: claude-opus-5-5]]\nPong. Nothing is waiting on you."
    b.chat_step(("cos chat", "ben: ping", True, work))
    assert sent == [(-100, "[claude-opus-5-5]\nPong. Nothing is waiting on you.", 7)], sent
    sent.clear()

    # the tag goes on the first piece of a long reply only
    answer["text"] = "[[model: claude-opus-5-5]]\n" + ("word " * 1500)
    b.chat_step(("cos chat", "andy: long", True, None))
    assert len(sent) == 2 and sent[0][1].startswith("[claude-opus-5-5]\nword") and not sent[1][1].startswith("[")
    sent.clear()

    # no model line: untagged, as before
    answer["text"] = "Plain answer."
    b.chat_step(("cos chat", "andy: hi", True, None))
    assert sent == [(1, "Plain answer.", None)], sent
    sent.clear()

    # [[to: ...]] still routes under the model line; a no-op is still dropped
    b.CHATS[-200] = {"chat_id": -200, "name": "Work"}
    answer["text"] = "[[model: claude-fable-5-1]]\n[[to: Work]]\nPosted to Work."
    b.chat_step(("cos chat", "andy: tell work", True, None))
    assert sent == [(-200, "[claude-fable-5-1]\nPosted to Work.", None)], sent
    sent.clear()
    answer["text"] = "[[model: claude-opus-5-5]]\n(no reply)"
    b.chat_step(("cos chat", "ping: nothing", False, None))
    assert sent == [], sent

    # a limit under the line is still a limit: the alert, not the raw line
    answer["text"] = "[[model: claude-opus-5-5]]\n" + LIMIT
    b.chat_step(("cos chat", "andy: hi", True, None))
    assert len(sent) == 1 and sent[0][1].startswith("⛔"), sent
    b.save_cos_limit({})
    sent.clear()

    # turned off
    b.SHOW_MODEL = False
    answer["text"] = "[[model: claude-opus-5-5]]\nOff."
    b.chat_step(("cos chat", "andy: hi", True, None))
    assert sent == [(1, "Off.", None)], sent
    b.SHOW_MODEL = True

    # A worker's question and result: the model the record event names (hive-record A7, data.ext.model)
    assert b.event_model_tag({"data": {"ext": {"model": "claude-opus-5-5"}}}) == "[claude-opus-5-5]"
    assert b.event_model_tag({"data": {"ext": {"cost": {}}}}) == "" and b.event_model_tag(None) == ""
    sent.clear()
    b.result_text = lambda pin: ""
    origin = {"chat_id": -100, "message_id": 5}
    blocked = {"actor": {"id": "worker.claude.1"}, "position": 9,
               "data": {"needs": "decision", "reason": "Which API?", "ext": {"model": "claude-sonnet-5-5"}}}
    b.send_question("t1", origin, blocked)
    assert sent[0][1].startswith("[claude-sonnet-5-5]\n❓ claude.1 needs a decision on t1"), sent
    sent.clear()
    b.send_question("t1", origin, {**blocked, "data": {"needs": "decision", "reason": "Which API?"}})
    assert sent[0][1].startswith("❓ claude.1 needs"), sent

    import json
    calls = []
    b.tg = lambda method, **p: calls.append((method, p)) or {}
    b.tg_file = lambda *a, **p: calls.append(("file", p)) or {}
    b.result_text = lambda pin: "## Summary\nIt works.\n\n## Details\n..."
    res = {"event_id": "e1", "type": "task.result_posted", "refs": [],
           "data": {"ext": {"model": "anthropic/claude-opus-5-5-20261101"}}}
    b.hive = lambda *a: (True, json.dumps({"task": {"current_result": {"event_id": "e1", "pin": {}}}, "events": [res]}))
    b.send_result("t1", origin)
    assert calls[0][0] == "sendMessage" and calls[0][1]["text"].startswith("[claude-opus-5-5]\nt1: done. Summary:"), calls
    print("PASSED: the chief of staff's replies, and workers' questions and results, carry [model] when it is known; "
          "untagged when it isn't")


if __name__ == "__main__":
    main()
