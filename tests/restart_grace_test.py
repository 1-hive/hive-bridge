"""floor_bots.now_line: right after the Floor restarts it has only the beacons it saved before, up to a few minutes old;
an agent isn't offline until the Floor has listened OFFLINE_AFTER itself (each beacon comes every 30 s).

    python tests/restart_grace_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import floor_bots as fb

now = 1_800_000_000.0
beacon = {"received": now - 280, "body": {"v": 1, "actor": "worker.x", "kind": "iter", "at": now - 280, "requests": []}}


def facts(started):
    return fb.Facts(state={"tasks": {}, "actors": {}}, events=[], beacons={"worker.x": beacon}, samples={}, dialogue=[],
                    groups={}, conf={}, floor_up=[], now=now, started=started)


assert fb.now_line(facts(now - 20), "worker.x", "worker", [])["state"] != "offline"          # just restarted
assert fb.now_line(facts(now - 3600), "worker.x", "worker", [])["state"] == "offline"        # listening for an hour
print("PASSED: no agent is offline in the Floor's first OFFLINE_AFTER after a restart; after that, a silent one is")
