#!/usr/bin/env python3
"""The chief of staff's account at a usage limit: the operators get one alert (not the raw limit line as a reply),
messages are held, a group asker is told once, the oldest held message is retried every 20 min, and when it's
answered the operators hear it's back and the rest are answered in order. A real answer that mentions a limit is
delivered as usual.

    python3 tests/cos_limit_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LIMIT = ("You've hit your individual spend limit · ask your admin to raise it at https://claude.ai/admin-settings/usage"
         " · your weekly limit resets 2pm (America/Los_Angeles)")


def load():
    tmp = Path(tempfile.mkdtemp())
    cfg, work = tmp / "cfg", tmp / "work"
    cfg.mkdir()
    work.mkdir()
    (cfg / "telegram-token").write_text("TEST")
    (cfg / "telegram.json").write_text(json.dumps({"chat_id": 1, "user_id": 1}))
    os.environ.update(HIVE_CONFIG_DIR=str(cfg), HIVE_WORK_DIR=str(work))
    sys.path.insert(0, str(HERE))
    sys.modules.pop("hive_paths", None)
    spec = importlib.util.spec_from_file_location("bridge_limit", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    b.STATE.parent.mkdir(parents=True, exist_ok=True)
    return b


def main():
    b = load()
    clock = [1000.0]
    b.time.time = lambda: clock[0]
    sent, asked = [], []
    answer = {"text": LIMIT + "\n" + LIMIT}   # cos.sh retries without --continue: the line comes twice

    def fake_safe(method, **p):
        if method == "sendMessage":
            sent.append((p["chat_id"], p["text"]))
        return {}
    b.safe = fake_safe
    b.ask_cos = lambda item: asked.append(item[1]) or answer["text"]
    b.send_attachments = lambda found, chat_id: []
    b.take_task_lines = lambda reply, target: reply
    work = {"chat_id": -100, "reply_to": 7}

    def drain():
        while not b.CHAT.empty():
            b.chat_step(b.CHAT.get())

    assert b.limit_line(LIMIT + "\n" + LIMIT).startswith("You've hit your individual spend limit")
    assert b.limit_line("Claude AI usage limit reached|1762560000") and b.limit_line("Credit balance is too low")
    assert b.limit_line("Astra hit its OpenAI quota.\nI've moved the review to Fable.") is None
    assert b.limit_line("ok") is None and b.limit_line("") is None

    # Ben in the Work group: the limit is hit; operators alerted once, Ben told once, no raw line anywhere
    b.chat_step(("cos chat", "ben: status?", True, work))
    assert [c for c, _ in sent] == [1, -100], sent
    assert sent[0][1].startswith("⛔ The chief of staff can't answer") and "spend limit" in sent[0][1]
    assert "admin-settings/usage" in sent[0][1]
    assert "out of usage" in sent[1][1] and LIMIT not in sent[1][1]
    sent.clear()

    # while limited: messages are held without running; no second alert or notice
    b.chat_step(("cos chat", "ben: and now?", True, work))
    b.chat_step(("cos chat", "ping: review passed", False, None))
    b.chat_step(("cos chat", "andy: hi", True, None))
    assert sent == [] and asked == ["ben: status?"], (sent, asked)
    assert len(b.cos_limit()["held"]) == 4

    # retries: not before 20 min; still limited at 20 min, nothing sent
    clock[0] += 600
    b.chat_step(None)
    assert asked == ["ben: status?"]
    clock[0] += 600
    b.chat_step(None)
    assert asked == ["ben: status?"] * 2 and sent == [] and len(b.cos_limit()["held"]) == 4

    # back: the oldest is answered, the operators hear it, the other three are answered in order
    clock[0] += 1200
    answer["text"] = "All fine."
    b.chat_step(None)
    assert sent[0] == (1, "✅ The chief of staff is back after 0.7 h; answering 4 held messages now."), sent
    assert sent[1] == (-100, "All fine.")
    drain()
    assert asked[-3:] == ["ben: and now?", "ping: review passed", "andy: hi"], asked
    assert b.cos_limit() == {} and not b.COS_LIMIT.exists()
    assert [c for c, _ in sent[2:]] == [-100, 1, 1], sent

    # a real answer that mentions a limit is delivered as usual
    sent.clear()
    answer["text"] = "Sol hit your usage limit earlier, so I moved its review to Fable; it passed and the PR is open."
    b.chat_step(("cos chat", "andy: what happened?", True, None))
    assert sent == [(1, answer["text"])] and b.cos_limit() == {}, sent
    print("PASSED: one alert at a usage limit, messages held and the asker told once, retried every 20 min, "
          "answered in order when it's back; real answers mentioning a limit pass through")


if __name__ == "__main__":
    main()
