"""floor.py's watch_problem: an agent whose beacons report a problem for ALERT_AFTER is reported once (the wording may
change meanwhile), and as recovered only once its beacons stay clear for CLEAR_AFTER (a restart clears it for a moment).

    python tests/problem_alert_test.py
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "floor.py"
tree = ast.parse(SRC.read_text())
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "watch_problem")
ns = {"ALERT_AFTER": 600, "CLEAR_AFTER": 120, "PROBLEMS": {}}
exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
w = ns["watch_problem"]
R = {"kind": "reviewer"}

assert w("reviewer.astra", {**R, "problem": "no verdict from t-1 (rc 1): out of credits"}, 0) is None
assert w("reviewer.astra", {**R, "problem": "no verdict from t-2 (rc 1): out of credits"}, 300) is None
m = w("reviewer.astra", {**R, "problem": "no verdict from t-3 (rc 1): out of credits"}, 600)
assert m and "reviewer.astra (reviewer)" in m and "10 min" in m and "t-3" in m and "reviews aren't being recorded" in m, m
assert w("reviewer.astra", {**R, "problem": "still"}, 900) is None                 # once
assert w("reviewer.astra", {**R, "problem": None}, 1000) is None                    # a restart: not yet recovered
assert w("reviewer.astra", {**R, "problem": "again"}, 1010) is None                 # back: still the same problem, no new alert
assert w("reviewer.astra", {**R}, 1100) is None
m = w("reviewer.astra", {**R}, 1220)
assert m and m.startswith("✅ reviewer.astra recovered"), m
assert "reviewer.astra" not in ns["PROBLEMS"]
# a short problem never reaches the operators, nor its end
assert w("worker.x", {"kind": "iter", "problem": "can't read a file"}, 0) is None
assert w("worker.x", {"kind": "iter"}, 100) is None and "worker.x" not in ns["PROBLEMS"]
print("PASSED: reported once after 10 min (wording may change), recovered only after 2 min clear, short problems silent")
