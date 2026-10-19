"""The unanswered-question pushes: none before 30 min, one at 31 min, a reminder 2 h later, nothing once answered.

    python3 tests/question_reminders_sim.py
"""
import importlib.util, json, os, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parents[1]
_cfg = Path(tempfile.mkdtemp()); (_cfg / "telegram-token").write_text("T"); (_cfg / "telegram.json").write_text(json.dumps({"chat_id": 1, "user_id": 2}))
os.environ.update(HIVE_CONFIG_DIR=str(_cfg), HIVE_WORK_DIR=tempfile.mkdtemp())
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("bridge", HERE / "telegram-bridge.py")
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
Q = {"kind": "task_blocked_unanswered_any", "entity": "task", "id": "t-1", "position": 900, "title": "Some task", "key": "task_blocked_unanswered_any:t-1:900"}
inbox = {"operator": [], "chief_of_staff": [Q]}
def fake_hive(*a):
    if a[0] == "inbox": return True, json.dumps(inbox[a[2]])
    if a[0] == "task": return True, json.dumps({"id": "t-1", "block": {"needs": "decision", "reason": "Accept the <stand-in>?", "answered": False}})
    return False, "?"
sent = []
b.hive = fake_hive; b.tg = lambda m, **p: sent.append(p["text"]); b.save_state = lambda st: None; b.log = lambda m: None
clock = [1000.0]; b.time.time = lambda: clock[0]
st = {"offset": 0, "notified": [], "pending_reason": {}}
def step(dt, label):
    clock[0] += dt; n = len(sent); b.push_inbox(st); print(f"{label:<28} sent={len(sent)-n}", ("| " + sent[-1].replace(chr(10), " ")[:140]) if len(sent) > n else "")
step(0, "t=0 question appears");  step(29*60, "t=29m"); step(2*60, "t=31m (past 30m)"); step(60*60, "t=1h31m"); step(61*60, "t=2h32m (2h later)")
inbox["chief_of_staff"] = []; step(60, "answered"); print("question_seen after answer:", st["question_seen"])
