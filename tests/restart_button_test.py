#!/usr/bin/env python3
"""A task the supervisor escalated to the operator as repeated_failure (or stuck) gets a Restart button; a tap
resolves the escalation with "Restart:", which the supervisor (hive-runtime v0.5.59) takes as the operator's
request for a fresh attempt, the same as `op restart`. Other escalations get no button; a second tap does nothing.

    python3 tests/restart_button_test.py
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

item = lambda entity, tid, title: {"kind": "escalation_open", "entity": entity, "id": tid, "title": title}
kb = b.budget_kb(item("task", "best-1-adopt-screen", "repeated_failure: Adopt joint-attacks-blocks"))
assert kb == [[{"text": "🔁 Restart", "callback_data": "restart:best-1-adopt-screen"}]], kb
assert b.budget_kb(item("task", "t1", "stuck: silent after 2 nudges"))
assert not b.budget_kb(item("task", "t1", "other: reviewer reviewer.codex.1's process ended without a verdict"))
assert not b.budget_kb(item("task", "t" * 60, "repeated_failure: x")), "callback data over 64 bytes"
assert b.budget_kb(item("goal", "g1", "budget_exceeded: wall clock"))[0][0]["text"] == "⏱ +4h"

calls, emitted = [], []
b.ALLOW = {"chat_id": -1, "user_id": 1}
b.allowed = lambda frm, chat: frm.get("id") == 1
b.safe = lambda method, **kw: calls.append((method, kw)) or {"message_id": 1}
b.save_state = lambda st: None
b.hive = lambda *a: emitted.append(a) or (True, json.dumps({"position": 1700}))
st = {}
msg = {"chat": {"id": -1}, "message_id": 55, "text": "⚠️ Escalation"}
b.on_callback({"id": "c0", "from": {"id": 2}, "message": msg, "data": "restart:t1"}, st)
assert not emitted and calls[-1][1].get("text") == "not allowed", calls
b.on_callback({"id": "c1", "from": {"id": 1}, "message": msg, "data": "restart:t1"}, st)
assert emitted == [("emit", "task.escalation_resolved", "--task", "t1", "--data", '{"resolution": "Restart:"}')], emitted
assert calls[-1][0] == "sendMessage" and calls[-1][1]["text"].startswith("✅ t1: escalation resolved at position 1700")
assert any(c[0] == "editMessageReplyMarkup" for c in calls)
b.on_callback({"id": "c2", "from": {"id": 1}, "message": msg, "data": "restart:t1"}, st)
assert len(emitted) == 1 and calls[-1][1]["text"] == "already done"
b.hive = lambda *a: (False, "refused: ILLEGAL_TRANSITION: not escalated")
b.on_callback({"id": "c3", "from": {"id": 1}, "message": {**msg, "message_id": 56}, "data": "restart:t2"}, st)
assert calls[-1][1]["text"].startswith("❌ t2: refused"), calls[-1]

print("PASSED: Restart button on repeated_failure/stuck task escalations only; a tap resolves with \"Restart:\" "
      "once; refusals reported")
