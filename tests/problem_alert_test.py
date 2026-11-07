"""floor.py's watch_problem: an agent whose beacons report a problem for ALERT_AFTER is reported once (the wording may
change meanwhile). Not when it clears, not after a Floor restart, not when it flaps back; only a problem after a day
clear is reported again. Short problems are never reported.

    python tests/problem_alert_test.py
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "floor.py"
tree = ast.parse(SRC.read_text())
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "watch_problem")
saved = []
ns = {"ALERT_AFTER": 600, "QUIET_FOR": 86400, "PROBLEMS": {}, "ALERTED": {}, "save_alerted": lambda: saved.append(1)}
exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
w = ns["watch_problem"]
R = {"kind": "reviewer"}
H = 3600

assert w("reviewer.astra", {**R, "problem": "no verdict from t-1 (rc 1): out of credits"}, 0) is None
assert w("reviewer.astra", {**R, "problem": "no verdict from t-2 (rc 1): out of credits"}, 300) is None
m = w("reviewer.astra", {**R, "problem": "no verdict from t-3 (rc 1): out of credits"}, 600)
assert m and "reviewer.astra (reviewer)" in m and "10 min" in m and "t-3" in m and "won't repeat" in m, m
assert saved, "who was told is saved, so a restart doesn't repeat it"
assert w("reviewer.astra", {**R, "problem": "still"}, 900) is None                 # once
assert w("reviewer.astra", {**R}, 1000) is None                                     # recovery: no message
assert w("reviewer.astra", {**R, "problem": "again"}, 2 * H) is None                # flaps back within a day
assert w("reviewer.astra", {**R, "problem": "again"}, 3 * H) is None                # ... and lasts: still quiet
# a Floor restart: problems in memory are gone, who was told is not
ns["PROBLEMS"].clear()
assert w("reviewer.astra", {**R, "problem": "after restart"}, 4 * H) is None
assert w("reviewer.astra", {**R, "problem": "after restart"}, 4 * H + 700) is None
# clear for a day, then a new problem: reported again
assert w("reviewer.astra", {**R}, 5 * H) is None
assert w("reviewer.astra", {**R}, 30 * H) is None and "reviewer.astra" not in ns["ALERTED"]
assert w("reviewer.astra", {**R, "problem": "auth expired"}, 31 * H) is None
m = w("reviewer.astra", {**R, "problem": "auth expired"}, 31 * H + 600)
assert m and "auth expired" in m, m
# a short problem never reaches the operators
assert w("worker.x", {"kind": "iter", "problem": "can't read a file"}, 0) is None
assert w("worker.x", {"kind": "iter"}, 100) is None and "worker.x" not in ns["PROBLEMS"] and "worker.x" not in ns["ALERTED"]
print("PASSED: reported once after 10 min; silent on recovery, restart and flapping; again only after a day clear")
