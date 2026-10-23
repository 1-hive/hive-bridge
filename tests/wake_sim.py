"""push_wakes: a bot with "wake" is @-mentioned in its wake chat when the record has something for it.

    python3 tests/wake_sim.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
BOTS_CHAT = -700


def main():
    cfg, work = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    (cfg / "telegram-token").write_text("TEST")
    (cfg / "telegram.json").write_text(json.dumps({"chat_id": 111, "user_id": 1001}))
    (cfg / "telegram-groups.json").write_text(json.dumps({
        "chats": [{"name": "work", "chat_id": -500, "mode": "all"},
                  {"name": "botbot", "chat_id": BOTS_CHAT, "mode": "mention"}],
        "humans": [{"name": "andy", "user_id": 1002, "permissions": ["operator"]}],
        "bots": [{"name": "oc", "user_id": 2001, "username": "OcBot", "actor": "worker.oc", "wake": "botbot"},
                 {"name": "it", "user_id": 2002, "username": "ItBot", "actor": "worker.it"},
                 {"name": "rv", "user_id": 2003, "username": "RvBot", "actor": "reviewer.rv", "wake": "botbot"}]}))
    os.environ.update(HIVE_CONFIG_DIR=str(cfg), HIVE_WORK_DIR=str(work))
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("bridge", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    sent = []
    b.safe = lambda method, **p: sent.append(p)
    b.log = lambda m: None
    events = [{"position": 1, "type": "actor.registered"}]
    owners = {"t1": "worker.oc", "t2": "worker.it"}
    b.hive = lambda *a: ((True, "\n".join(json.dumps(e) for e in events if e["position"] > int(a[2])))
                         if a[0] == "events" else (True, json.dumps({"task": {"owner": owners.get(a[1])}})))
    b.STATE.parent.mkdir(parents=True, exist_ok=True)
    st = b.load_state()
    b.push_wakes(st)
    assert st["wake_pos"] == 1 and not sent, "first run starts at the head"
    events += [
        {"position": 2, "type": "task.assigned", "task": "t1", "data": {"to": "worker.oc"}},
        {"position": 3, "type": "task.assigned", "task": "t2", "data": {"to": "worker.it"}},       # no wake: Iter
        {"position": 4, "type": "task.nudged", "task": "t1", "data": {"reason": "quiet"}},
        {"position": 5, "type": "review.assigned", "task": "t2", "data": {"reviewer": "reviewer.rv"}},
        {"position": 6, "type": "review.recorded", "task": "t1", "data": {"verdict": "passed"}},   # nothing to do
        {"position": 7, "type": "review.recorded", "task": "t1", "data": {"verdict": "failed"}},
        {"position": 8, "type": "task.answered", "task": "t1", "data": {}},
    ]
    b.push_wakes(st)
    texts = [p["text"] for p in sent]
    assert all(p["chat_id"] == BOTS_CHAT for p in sent), sent
    assert len(texts) == 5, texts
    assert texts[0].startswith("@OcBot hive: a new task for you (t1)"), texts[0]
    assert "a nudge from the supervisor (t1)" in texts[1] and texts[2].startswith("@RvBot hive: a result for you to review (t2)")
    assert "a review of your result (t1)" in texts[3] and "the answer to your question (t1)" in texts[4], texts
    assert st["wake_pos"] == 8
    b.push_wakes(st)
    assert len(sent) == 5, "nothing twice"
    print("PASSED: wakes only bots with wake, for assignment, nudge, review to do, failed review, answer; "
          "not for a passed review or a bot without wake; first run at the head; nothing twice")


if __name__ == "__main__":
    main()
