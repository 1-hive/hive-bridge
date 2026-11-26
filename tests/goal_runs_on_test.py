#!/usr/bin/env python3
"""A goal runs to its end without asking (hive-runtime docs/chief-of-staff.md, "Running a goal"):

- an active goal with no open task wakes the chief of staff once per idle spell, after 30 minutes; not a goal
  waiting on the operator (escalated), not one that isn't active;
- the digest shows, per active goal, the tasks done since the last digest, the goal log's new lines (only once:
  counted when the digest is sent) and since when it has had no open task;
- a worker's question on a chat-started task waits 10 minutes: answered by the chief of staff by then, it is posted
  as decided (the card, the answer, the buttons marked decided, the answer file); otherwise as a question.

    python3 tests/goal_runs_on_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def load():
    tmp = Path(tempfile.mkdtemp())
    cfg, work, ws = tmp / "cfg", tmp / "work", tmp / "workspace.git"
    cfg.mkdir()
    work.mkdir()
    (cfg / "telegram-token").write_text("TEST")
    (cfg / "telegram.json").write_text(json.dumps({"chat_id": 1, "user_id": 1}))
    os.environ.update(HIVE_CONFIG_DIR=str(cfg), HIVE_WORK_DIR=str(work), HIVE_WORKSPACE_REPO=str(ws))
    os.environ.pop("HIVE_IDLE_GOAL_MINUTES", None)
    os.environ.pop("HIVE_QUESTION_GRACE_MINUTES", None)
    sys.path.insert(0, str(HERE))
    sys.modules.pop("hive_paths", None)
    spec = importlib.util.spec_from_file_location("bridge_goal_runs_on", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    b.STATE.parent.mkdir(parents=True, exist_ok=True)
    b.save_state = lambda st: None
    b.log = lambda m: None
    return b, tmp, ws


def workspace_with_log(tmp: Path, ws: Path, lines: list[str]) -> None:
    """A workspace repository whose main holds the goal file and its log (and a goal whose name ends the same)."""
    src = tmp / "src"
    if not src.exists():
        subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(ws)], check=True)
    goals = src / "projects" / "mtg-player" / "goals"
    goals.mkdir(parents=True, exist_ok=True)
    (goals / "2026-11-19-stage2-learn.md").write_text("# Stage 2\n")
    (goals / "2026-11-19-stage2-learn-log.md").write_text(
        "# Stage 2: goal log\n\n## Plan\n- 2a pilot, exit: imitation >= 40%\n\n## Log\n" + "".join(x + "\n" for x in lines))
    (goals / "2026-11-01-other-stage2-learn-log.md").write_text("- 2026-11-01 10:00 x: not this goal's log\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "log", "--allow-empty"], check=True, env=env)
    subprocess.run(["git", "-C", str(src), "push", "-q", "-f", str(ws), "main"], check=True)


def test_idle_wake(b) -> None:
    clock = [1000.0]
    b.time.time = lambda: clock[0]
    state = {"goals": {"g1": {"id": "g1", "title": "Stage 2", "status": "active", "escalation": None},
                       "g2": {"id": "g2", "title": "Over budget", "status": "active",
                              "escalation": {"to": "operator", "reason": "budget"}},
                       "g3": {"id": "g3", "title": "Done", "status": "completed", "escalation": None}},
             "tasks": {"t1": {"id": "t1", "goal": "g1", "status": "done"}}}
    st: dict = {}

    def run(after: float) -> list[str]:
        clock[0] += after
        b.ping_cos_idle(st, "cos chat", state)
        out = []
        while not b.CHAT.empty():
            out.append(b.CHAT.get()[1])
        return out

    assert run(0) == [] and run(29 * 60) == [], "not before 30 min"
    got = run(60)
    assert len(got) == 1 and "Goal g1" in got[0] and "Running a goal" in got[0] and "30 min" in got[0], got
    assert "Reply with nothing unless the human must do something" in got[0]
    assert run(3600) == [], "once per idle spell"
    assert "g2" not in st["goal_idle"] and "g3" not in st["goal_idle"], "escalated or not active: never idle"
    state["tasks"]["t2"] = {"id": "t2", "goal": "g1", "status": "in_progress"}
    assert run(60) == [] and "g1" not in st["goal_idle"], "a task opening ends the spell"
    state["tasks"]["t2"]["status"] = "done"
    assert run(60) == [] and run(30 * 60) and run(60) == [], "the next spell is told again, once"
    # a task created but never assigned isn't work going on (proto-hive, 2026-11-22: a goal sat two days on three)
    state["tasks"]["t2"]["status"] = "in_progress"
    assert run(60) == []
    state["tasks"]["t2"]["status"] = "done"
    state["tasks"]["t3"] = {"id": "t3", "goal": "g1", "status": "created"}
    got = run(60) + run(30 * 60)
    assert len(got) == 1 and "(t3 created, not assigned)" in got[0], got
    del state["tasks"]["t3"]
    state["goals"]["g1"]["status"] = "completed"
    assert run(60) == [] and st["goal_idle"] == {}
    b.IDLE_GOAL_S = 0
    state["goals"]["g1"]["status"] = "active"
    assert run(3 * 3600) == [] and st["goal_idle"] == {}, "HIVE_IDLE_GOAL_MINUTES=0: off"
    b.IDLE_GOAL_S = 1800


def test_digest(b, tmp: Path, ws: Path) -> None:
    clock = [1_794_000_000.0]
    b.time.time = lambda: clock[0]
    state = {"goals": {"stage2-learn": {"id": "stage2-learn", "title": "Stage 2: learn", "status": "active",
                                        "proposed_at": "2026-11-19T00:00:00Z", "escalation": None,
                                        "budget": {"wall_clock_seconds": 2419200}, "spent": {}}},
             "tasks": {"s2-1": {"id": "s2-1", "goal": "stage2-learn", "title": "Imitation pilot", "status": "done",
                                "status_since": 120},
                       "s2-2": {"id": "s2-2", "goal": "stage2-learn", "title": "Self-play pilot", "status": "done",
                                "status_since": 130}}}   # a record position, as the record keeps it
    b.hive = lambda *a: (True, json.dumps(state)) if a[0] == "state" else (True, "")
    sent = []
    b.tg = lambda method, **p: sent.append(p["text"]) or {}
    lines = ["- 2026-11-12 09:00 2a: imitation pilot first (cheapest early kill)",
             "- 2026-11-12 18:00 2a: pilot at 43% vs ours, passes the 40% kill; self-play next (s2-1 report)"]
    workspace_with_log(tmp, ws, lines)
    st = {"digest_pos": 0, "digest_at": 1_794_000_000.0 - 86400 * 10, "goal_idle": {"stage2-learn": {"since": clock[0] - 3600, "told": True}}}
    b.send_digest(st)
    text = sent[-1]
    assert "done since the last digest (2): Imitation pilot; Self-play pilot" in text, text
    assert "Decided (goal log):" in text and "imitation pilot first" in text and "self-play next" in text, text
    assert "not this goal's log" not in text, "only this goal's log, not one whose name ends the same"
    assert "⏸ nothing worked on since" in text, text
    assert st["log_shown"] == {"stage2-learn": 2} and "log_next" not in st
    # the next digest: only the lines added since, and only tasks done since
    workspace_with_log(tmp, ws, lines + ["- 2026-11-13 08:00 2b: league with past selves, not xmage:10 (cost; s2-2)"])
    state["tasks"]["s2-3"] = {"id": "s2-3", "goal": "stage2-learn", "title": "League run", "status": "in_progress"}
    clock[0] += 86400
    st["digest_pos"] = 200   # after s2-1 and s2-2 were done
    b.send_digest(st)
    text = sent[-1]
    assert "league with past selves" in text and "imitation pilot first" not in text, text
    assert "done since the last digest" not in text and "⏸" not in text, text
    assert st["log_shown"] == {"stage2-learn": 3}
    # many new lines: the first LOG_SHOWN, then a count
    more = [f"- 2026-11-14 0{i}:00 2b: step {i}" for i in range(10)]
    workspace_with_log(tmp, ws, lines + ["- 2026-11-13 08:00 2b: league"] + more)
    b.send_digest(st)
    assert f"… and {10 - b.LOG_SHOWN} more in the goal log" in sent[-1], sent[-1]
    # a digest that fails to send doesn't count its lines as shown
    workspace_with_log(tmp, ws, lines + ["- 2026-11-13 08:00 2b: league"] + more + ["- 2026-11-15 01:00 2b: unseen"])

    def down(method, **p):
        raise OSError("network")
    b.tg = down
    try:
        b.send_digest(st)
    except OSError:
        pass
    assert st["log_shown"] == {"stage2-learn": 13}, st["log_shown"]


def test_long_digest(b) -> None:
    """Over Telegram's 4096 characters the digest goes in parts, split between lines (proto-hive, 2026-11-25: refused
    every 10 minutes for 31 h); only a failed first part means "try again"."""
    clock = [1_794_000_000.0]
    b.time.time = lambda: clock[0]
    goals = {f"g{i}": {"id": f"g{i}", "title": f"Goal {i} " + "x" * 80, "status": "active", "escalation": None,
                       "proposed_at": f"2026-11-{10 + i}T00:00:00Z", "spent": {}} for i in range(12)}
    tasks = {f"t{i}-{k}": {"id": f"t{i}-{k}-" + "y" * 60, "goal": f"g{i}", "status": "assigned"}
             for i in range(12) for k in range(6)}
    state = {"goals": goals, "tasks": tasks}
    b.hive = lambda *a: (True, json.dumps(state)) if a[0] == "state" else (True, "")
    b.goal_log = lambda gid: []
    sent = []
    b.tg = lambda method, **p: sent.append(p["text"]) or {}
    st = {"digest_pos": 0, "digest_at": clock[0] - 86400}
    full, _ = b.digest_text(dict(st))
    assert len(full) > 4096, len(full)
    b.send_digest(st)
    assert len(sent) > 1 and all(len(x) <= 4096 for x in sent), [len(x) for x in sent]
    assert sent[1].startswith(f"<i>({b.hp.HIVE_ID} digest, part 2 of {len(sent)})</i>"), sent[1][:80]
    for x in sent:   # every part's tags are closed in it
        for tag in ("b", "code", "i"):
            assert x.count(f"<{tag}>") == x.count(f"</{tag}>"), (tag, x)
    body = "\n".join(x.split("\n", 1)[1] if i else x for i, x in enumerate(sent))
    assert body == full, "the parts are the digest, in order, nothing lost"
    assert st["digest_at"] == clock[0]
    # one line longer than a message: its tags dropped, cut outside an entity
    parts = b.message_parts("<b>head</b>\n" + "<code>a&amp;b</code> " * 600, 1000)
    assert all(len(p) <= 1000 for p in parts) and parts[0] == "<b>head</b>", [p[:40] for p in parts]
    assert all("<code>" not in p and not p.endswith("&am") and not p.endswith("&") for p in parts[1:]), parts[1][-20:]
    # a later part that fails: logged, the digest still counts as sent (not every part re-sent in 10 minutes)
    logged, calls = [], []

    def second_fails(method, **p):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("telegram sendMessage: Bad Request")
        return {}
    b.tg, b.log = second_fails, logged.append
    clock[0] += 86400
    b.send_digest(st)
    assert st["digest_at"] == clock[0] and any("part 2 of" in m and "not sent" in m for m in logged), logged
    b.log = lambda m: None


CARD_Q = """# question
Status: The pilot passed its early kill at 43%.
Before: the self-play run
1. Use a league of past selves rather than xmage:10? yes: cheaper, faster / no: slower league with xmage:10
Recommend: 1 yes
---
Details.
"""
ANSWER = """# t-1: answer 1 (cos)

Question: league of past selves?

Answer: yes. The goal's budget has no rented compute, and xmage:10 games cost 20x.
Keep xmage:10 in the evaluation ladder.
"""


def test_questions(b) -> None:
    clock = [5000.0]
    b.time.time = lambda: clock[0]
    msgs, files, buttons = [], [], []
    b.safe = lambda method, **p: msgs.append(p) or {"message_id": 77}
    b.tg_file = lambda method, field, f, **p: files.append((Path(f).read_text(), p.get("caption")))
    b.offer_buttons = lambda st, task, c, chat, m, text, decided=False: buttons.append((task, decided))
    b.result_text = lambda pin: {"q.md": CARD_Q, "a.md": ANSWER}.get((pin or {}).get("path"))
    answers: dict = {}
    b.answer_of = lambda task, after: answers.get(task)
    blocked = {"type": "task.blocked", "task": "t-1", "position": 100, "actor": {"id": "worker.claude.1"},
               "data": {"needs": "decision", "reason": "League of past selves?"},
               "refs": [{"rel": "question", "pin": {"path": "q.md"}}]}
    o = {"t-1": {"chat_id": -100, "message_id": 5, "status_id": 6}}
    st = {"q_held": {"t-1@100": {"task": "t-1", "event": blocked, "at": clock[0]}}}
    b.flush_questions(st, o)
    assert msgs == [] and st["q_held"], "held: nothing posted yet"
    clock[0] += 120
    answers["t-1"] = {"type": "task.answered", "task": "t-1", "position": 101,
                      "refs": [{"rel": "answer", "pin": {"path": "a.md"}}]}
    b.flush_questions(st, o)
    assert st["q_held"] == {} and len(msgs) == 1, msgs
    text = msgs[0]["text"]
    assert text.startswith("✅ claude.1 asked on t-1; the chief of staff decided it"), text
    assert "1. Use a league of past selves" in text and "Its answer: yes. The goal's budget" in text, text
    assert "Keep xmage:10 in the evaluation ladder." in text and "Tap to choose otherwise" in text, text
    assert msgs[0]["reply_to_message_id"] == 6 and buttons == [("t-1", True)]
    assert files and files[0][0] == ANSWER and "answer" in files[0][1]
    # not answered within the grace: posted as a question, as before
    msgs.clear(), buttons.clear(), files.clear(), answers.clear()
    st["q_held"] = {"t-1@200": {"task": "t-1", "event": dict(blocked, position=200), "at": clock[0]}}
    clock[0] += 9 * 60
    b.flush_questions(st, o)
    assert msgs == []
    clock[0] += 61
    b.flush_questions(st, o)
    assert len(msgs) == 1 and msgs[0]["text"].startswith("❓ claude.1 needs a decision on t-1"), msgs
    assert buttons == [("t-1", False)] and st["q_held"] == {}
    # a task no longer followed (cancelled): its question is dropped
    st["q_held"] = {"t-9@300": {"task": "t-9", "event": dict(blocked, task="t-9", position=300), "at": 0}}
    msgs.clear()
    b.flush_questions(st, o)
    assert msgs == [] and st["q_held"] == {}
    # the answer's gist
    assert b.answer_gist("# Head\n\n**Answer:** no.\nBecause.") == "no.\nBecause."
    assert b.answer_gist("# Head\nJust this.") == "Just this."


def test_decided_tap() -> None:
    import decision_buttons as db
    c = {"questions": ["Use a league of past selves? yes: cheaper / no: slower"], "before": "the run"}
    entry = db.new_entry("t-1", c, -100, 77, "text")
    entry["decided"] = True
    ok, note, line, cos = db.tap(entry, 0, 1, "andy")
    assert ok and "already answered; the worker went on with your answer" in cos and "\"no\"" in cos, cos
    assert "their choice wins" in cos and "Record task.answered" not in cos
    entry = db.new_entry("t-1", c, -100, 78, "text")
    ok, note, line, cos = db.tap(entry, 0, 0, "andy")
    assert ok and "Record task.answered on t-1" in cos, "an undecided card asks for task.answered, as before"


def test_loop_steps(b) -> None:
    """A step that raises is logged with where, and the loop's other steps still run (2026-11-20: a digest that
    raised before everything else froze the bridge for 38 hours)."""
    logged = []
    b.log = logged.append
    assert b.step("digest", lambda: {}["x"]) is False
    assert len(logged) == 1 and logged[0].startswith("error in digest (goal_runs_on_test.py:") and "KeyError" in logged[0], logged
    ran = []
    assert b.step("inbox", ran.append, 1) is True and ran == [1]


def main():
    b, tmp, ws = load()
    test_loop_steps(b)
    test_idle_wake(b)
    test_digest(b, tmp, ws)
    test_long_digest(b)
    test_questions(b)
    test_decided_tap()
    print("ok")


if __name__ == "__main__":
    main()
