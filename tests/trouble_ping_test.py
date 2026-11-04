#!/usr/bin/env python3
"""The bridge tells the chief of staff when the Floor shows an agent offline or unable to run for 10 minutes (once,
whatever the wording), and when it recovers; a short spell is never mentioned.

    python3 tests/trouble_ping_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def load():
    tmp = Path(tempfile.mkdtemp())
    cfg, work = tmp / "cfg", tmp / "work"
    cfg.mkdir()
    work.mkdir()
    (cfg / "telegram-token").write_text("TEST")
    (cfg / "telegram.json").write_text(json.dumps({"chat_id": 1, "user_id": 1}))
    os.environ.update(HIVE_CONFIG_DIR=str(cfg), HIVE_WORK_DIR=str(work), FLOOR_BOARD="bots")
    sys.path.insert(0, str(HERE))
    sys.modules.pop("hive_paths", None)
    spec = importlib.util.spec_from_file_location("bridge_trouble", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    b.STATE.parent.mkdir(parents=True, exist_ok=True)
    return b


def main():
    b = load()
    clock = [1000.0]
    floor = {"trouble": []}
    b.time.time = lambda: clock[0]
    b.floor_get = lambda path: floor
    b.save_state = lambda st: None
    st = {}

    def run(after: float) -> list[str]:
        clock[0] += after
        b.ping_cos_trouble(st, "cos chat")
        out = []
        while not b.CHAT.empty():
            out.append(b.CHAT.get()[1])
        return out

    astra = {"id": "reviewer.astra", "name": "Astra", "state": "cantrun",
             "detail": "no verdict from the review of t-1 (rc 1): ERROR: Your workspace is out of credits."}
    assert run(0) == []
    floor["trouble"] = [astra]
    assert run(60) == [], "not at once"
    assert run(300) == [], "not before 10 min"
    floor["trouble"] = [dict(astra, detail="no verdict from the review of t-2 (rc 1): 47,632")]
    got = run(300)
    assert len(got) == 1 and "Astra (reviewer.astra) can't run" in got[0] and "47,632" in got[0], got
    assert run(600) == [], "told once"
    floor["trouble"] = []
    got = run(60)
    assert len(got) == 1 and "reviewer.astra recovered" in got[0], got
    # a short spell: never mentioned, not even its recovery
    floor["trouble"] = [dict(astra, state="offline", detail="No word from its runner since 20:00 UTC.")]
    assert run(60) == [] and run(300) == []
    floor["trouble"] = []
    assert run(60) == [] and st["trouble"] == {}
    # a Floor that doesn't answer changes nothing
    floor["trouble"] = [astra]
    run(0)

    def down(path):
        raise OSError("refused")
    b.floor_get = down
    assert run(900) == [] and "reviewer.astra" in st["trouble"]
    print("PASSED: the chief of staff hears of an agent in trouble after 10 min, once, and of its recovery; "
          "short spells are silent")


if __name__ == "__main__":
    main()
