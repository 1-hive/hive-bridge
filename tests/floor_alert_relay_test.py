#!/usr/bin/env python3
"""A Floor without the bot's token (proto-hive keeps it from the internet-facing Floor) queues its operator alerts at
/api/alerts instead of failing to send them; the bridge sends each one once, in order, and keeps one that failed.

    python3 tests/floor_alert_relay_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def floor_say_queues():
    import floor
    cfg = Path(tempfile.mkdtemp())
    floor.CFG = cfg   # no telegram-token here
    floor.ALERTS.clear()
    floor.say("⚠️ reviewer.astra (reviewer) has reported a problem for 10 min: out of credits")
    floor.say("✅ reviewer.astra recovered")
    got = floor.api_alerts({})["alerts"]
    assert [a["text"].split()[0] for a in got] == ["⚠️", "✅"], got
    assert floor.api_alerts({"after": [str(got[0]["t"])]})["alerts"] == got[1:] or got[0]["t"] == got[1]["t"]
    for i in range(60):
        floor.say(f"x{i}")
    assert len(floor.ALERTS) == 50, "kept bounded"


def bridge_relays():
    tmp = Path(tempfile.mkdtemp())
    cfg, work = tmp / "cfg", tmp / "work"
    cfg.mkdir(); work.mkdir()
    (cfg / "telegram-token").write_text("TEST")
    (cfg / "telegram.json").write_text(json.dumps({"chat_id": 7, "user_id": 1}))
    os.environ.update(HIVE_CONFIG_DIR=str(cfg), HIVE_WORK_DIR=str(work), FLOOR_BOARD="bots")
    sys.modules.pop("hive_paths", None)
    spec = importlib.util.spec_from_file_location("bridge_alerts", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    b.save_state = lambda st: None
    queue = [{"t": 10.0, "text": "one"}, {"t": 20.0, "text": "two"}]
    asked, sent, fail = [], [], [False]
    b.floor_get = lambda path: (asked.append(path), {"alerts": [a for a in queue if a["t"] > float(path.split("=")[1])]})[1]

    def tg(method, **p):
        if fail[0]:
            raise RuntimeError("telegram down")
        sent.append((p["chat_id"], p["text"]))
    b.tg = tg
    st = {}
    b.relay_floor_alerts(st)
    assert sent == [(7, "one"), (7, "two")] and st["floor_alert_t"] == 20.0, (sent, st)
    b.relay_floor_alerts(st)
    assert len(sent) == 2, "each sent once"
    queue.append({"t": 30.0, "text": "three"})
    fail[0] = True
    b.relay_floor_alerts(st)
    assert st["floor_alert_t"] == 20.0, "a failed send is kept for the next round"
    fail[0] = False
    b.relay_floor_alerts(st)
    assert sent[-1] == (7, "three") and len(sent) == 3
    b.floor_get = lambda path: (_ for _ in ()).throw(OSError("404"))
    b.relay_floor_alerts(st)   # an older Floor without /api/alerts: nothing happens


if __name__ == "__main__":
    floor_say_queues()
    bridge_relays()
    print("floor_alert_relay_test: ok")
