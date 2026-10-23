"""Simulated Telegram traffic through telegram-bridge.py, with fake Telegram, record and chief of staff.

    python3 tests/groups_sim.py

Checks the private operator chat (unchanged without telegram-groups.json) and group chats
(docs/design.md): facts not decisions, bots as context only, the shadow rule log, threaded replies,
[[to: chat]] routing, and buttons for every human with the operator permission.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
OP_CHAT, OP_USER, ANDY, BEN, STRANGER = 111, 1001, 1002, 1003, 1999
GROUP, BOT1, BOT3, PHILO, COSBOT = -500, 2001, 2003, -600, 3000


def load(groups: bool):
    cfg, work = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    (cfg / "telegram-token").write_text("TEST")
    (cfg / "telegram.json").write_text(json.dumps({"chat_id": OP_CHAT, "user_id": OP_USER}))
    if groups:
        (cfg / "telegram-groups.json").write_text(json.dumps({
            "chats": [{"name": "main", "chat_id": GROUP, "private": False},
                      {"name": "philosophy", "chat_id": PHILO, "mode": "mention"}],
            "humans": [{"name": "andy", "user_id": ANDY, "permissions": ["operator"]},
                       {"name": "ben", "user_id": BEN, "permissions": ["operator"]}],
            "bots": [{"name": "bot1", "user_id": BOT1, "username": "Bot1Bot", "actor": "worker.bot1"},
                     {"name": "bot3", "user_id": BOT3, "username": "Bot3Bot", "actor": "worker.bot3"}]}))
    os.environ.update(HIVE_CONFIG_DIR=str(cfg), HIVE_WORK_DIR=str(work))
    sys.path.insert(0, str(HERE))
    sys.modules.pop("hive_paths", None)   # re-read HIVE_CONFIG_DIR and HIVE_WORK_DIR for this load
    spec = importlib.util.spec_from_file_location(f"bridge{groups}", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    sent = []
    b.tg = lambda method, **p: sent.append((method, p)) or {"message_id": 9000 + len(sent)}
    b.safe = b.tg
    state = {"actors": {"worker.bot1": {"class": "worker", "status": "active", "declaration": {"harness": "iter"}},
                        "worker.bot3": {"class": "worker", "status": "active", "declaration": {"harness": "iter"}}},
             "tasks": {"t7": {"id": "t7", "title": "Fix the journal", "status": "in_review", "owner": "worker.bot3",
                              "current_result": {"event_id": "e7"}, "status_since": "2026-10-19T10:00:00Z"}}}
    b.hive = lambda *a: (True, json.dumps(state)) if a[0] == "state" else (True, "[]")
    b.STATE.parent.mkdir(parents=True, exist_ok=True)
    return b, sent, work


def msg(chat, chat_type, uid, text, mid, reply_to=None, is_bot=False, entities=None):
    m = {"message_id": mid, "date": int(time.time()), "chat": {"id": chat, "type": chat_type, "title": "g"},
         "from": {"id": uid, "is_bot": is_bot, "first_name": f"u{uid}"}, "text": text}
    if reply_to:
        m["reply_to_message"] = reply_to
    if entities:
        m["entities"] = entities
    return m


def mention(text, name):
    i = text.index("@" + name)
    return {"type": "mention", "offset": i, "length": len(name) + 1}


def drain(b, reply):
    """Run the chief-of-staff queue with a stand-in that replies `reply` and returns what it was given."""
    seen = []
    while not b.CHAT.empty():
        cmd, text, human, target = b.CHAT.get()
        seen.append((text, target))
        b.CHAT.task_done()
        b.CHAT.put(("cat >/dev/null; printf %s " + json.dumps(reply), text, human, target))
        break
    return seen


def run_worker_once(b):
    import threading
    t = threading.Thread(target=b.chat_worker, daemon=True)
    t.start()
    b.CHAT.join()


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f": {detail}"))
    if not cond:
        raise SystemExit(1)


def main():
    # 1. without telegram-groups.json: the operator's private chat, exactly as before
    b, sent, _ = load(groups=False)
    st = {"offset": 0, "notified": [], "pending_reason": {}}
    b.on_message(msg(OP_CHAT, "private", OP_USER, "how is t7?", 1), st, "COS")
    item = b.CHAT.get(timeout=5); b.CHAT.task_done()
    check("private chat: the text goes to the chief of staff as is, to the operator chat", item == ("COS", "how is t7?", True, None), item)
    b.on_message(msg(OP_CHAT, "private", STRANGER, "hi", 2), st, "COS")
    check("private chat: a stranger is ignored", b.CHAT.empty())
    b.on_message(msg(GROUP, "group", OP_USER, "hello group", 3), st, "COS")
    check("no groups file: a group isn't served", b.CHAT.empty())

    # 2. with groups
    b, sent, work = load(groups=True)
    st = {"offset": 0, "notified": [], "pending_reason": {}}
    bot_answer = msg(GROUP, "group", BOT1, "Here is my journal fix.", 10, is_bot=True)
    b.on_message(bot_answer, st, "COS")
    check("a bot's message is context only", b.CHAT.empty())
    t1 = "@Bot1Bot please look at the work done by @Bot3Bot and rate it"
    b.on_message(msg(GROUP, "group", ANDY, t1, 11, entities=[mention(t1, "Bot1Bot"), mention(t1, "Bot3Bot")]), st, "COS")
    cmd, text, human, target = b.CHAT.get(timeout=5); b.CHAT.task_done()
    env = json.loads(text[len(b.ENVELOPE_HEAD):])
    check("example 1: envelope from andy, both bots as mentions (facts)", env["from"]["human"] == "andy"
          and env["mentions"] == ["bot1", "bot3"] and env["from"]["permissions"] == ["operator"], env)
    check("example 1: the bot's earlier message is in the thread", any(x["from"] == "bot1" for x in env["thread"]), env["thread"])
    check("example 1: record context lists the agents and t7", [a["actor"] for a in env["context"]["agents"]] == ["worker.bot1", "worker.bot3"]
          and env["context"]["open_tasks"][0]["task"] == "t7", env["context"])
    check("example 1: the reply goes to the group, threaded on the message", target == {"chat_id": GROUP, "reply_to": 11}, target)
    t2 = "this work looks bad, please suggest improvements @Bot3Bot"
    b.on_message(msg(GROUP, "group", BEN, t2, 12, reply_to=bot_answer, entities=[mention(t2, "Bot3Bot")]), st, "COS")
    _, text, _, _ = b.CHAT.get(timeout=5); b.CHAT.task_done()
    env = json.loads(text[len(b.ENVELOPE_HEAD):])
    check("example 2: reply_to says bot1 wrote it, mentions say bot3", env["reply_to"]["from"] == "bot1" and env["mentions"] == ["bot3"], env)
    rules = [json.loads(l) for l in (work / ".telegram" / "addressee-rule.jsonl").read_text().splitlines()]
    check("shadow log: the rule would pick bot1 both times (wrong for example 2)", [r["rule"] for r in rules] == ["bot1", "bot1"], rules)
    b.on_message(msg(GROUP, "group", STRANGER, "spam", 13), st, "COS")
    check("a stranger in the group is ignored", b.CHAT.empty())
    b.on_message(msg(-777, "group", ANDY, "other group", 14), st, "COS")
    check("an unconfigured group is ignored", b.CHAT.empty())

    # 3. replies: threaded, and [[to: chat]] routing
    sent.clear()
    b.CHAT.put(("cat >/dev/null; printf 'Assigned to bot1 as a review of t7.'", "x", True, {"chat_id": GROUP, "reply_to": 11}))
    run_worker_once(b)
    m = [p for meth, p in sent if meth == "sendMessage"][-1]
    check("a reply is threaded on the human's message", m["chat_id"] == GROUP and m["reply_to_message_id"] == 11, m)
    sent.clear()
    b.CHAT.put(("cat >/dev/null; printf '[[to: main]]\\nt7 passed its review.'", "x", False, None))
    run_worker_once(b)
    m = [p for meth, p in sent if meth == "sendMessage"][-1]
    check("[[to: main]] sends a record-driven reply to the group", m["chat_id"] == GROUP and m["text"] == "t7 passed its review."
          and "reply_to_message_id" not in m, m)
    sent.clear()
    b.CHAT.put(("cat >/dev/null; printf 'Nothing else.'", "x", False, None))
    run_worker_once(b)
    m = [p for meth, p in sent if meth == "sendMessage"][-1]
    check("other wakes still reply in the operator chat", m["chat_id"] == OP_CHAT, m)

    # 4. a task started from a group message: [[task: id]] in the reply, then its progress threaded there
    sent.clear()
    b.CHAT.put(("cat >/dev/null; printf 'Asked bot3 to improve it.\\n[[task: t9]]'", "x", True, {"chat_id": GROUP, "reply_to": 12}))
    run_worker_once(b)
    m = [p for meth, p in sent if meth == "sendMessage"][-1]
    check("[[task: t9]] is taken out of the visible reply", m["text"] == "Asked bot3 to improve it." and m["reply_to_message_id"] == 12, m)
    check("t9's origin is remembered", b.load_origins().get("t9") == {"chat_id": GROUP, "message_id": 12}, b.load_origins())
    evs = [{"position": p, "type": t, "task": "t9", "actor": {"id": a}, "data": d} for p, t, a, d in [
        (50, "task.accepted", "worker.bot3", {}), (51, "task.reported", "worker.bot3", {"kind": "progress"}),
        (52, "task.result_posted", "worker.bot3", {}), (53, "review.recorded", "worker.bot1", {"verdict": "passed"}),
        (54, "task.closed", "cos", {}), (55, "task.closed", "cos", {"task": "other"})]]
    evs[-1]["task"] = "t1"
    st["origin_pos"] = 49
    # the result's report, in a workspace repository, for the file sent back on close
    import subprocess
    ws = Path(tempfile.mkdtemp())
    genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    (ws / "r.md").write_text("# The summary\n\nAll of it.\n")
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-qm", "r"], check=True, env=genv)
    head = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    b.WORKSPACE = ws
    t9 = {"task": {"id": "t9", "current_result": {"event_id": "e52", "pin": {"repository": "workspace", "commit_oid": f"sha1:{head}", "path": "r.md"}}},
          "events": [{"event_id": "e52", "type": "task.result_posted", "refs": [{"rel": "code", "pin": {"repository": "app", "commit_oid": "sha1:abcdef1234567890"}}]}]}
    b.hive = lambda *a: ((True, "\n".join(json.dumps(e) for e in evs)) if a[0] == "events"
                         else (True, json.dumps(t9)) if a[:2] == ("task", "t9") else (True, "{}"))
    docs = []
    b.tg_file = lambda method, field, f, **p: docs.append((method, f.read_text(), p)) or {}
    sent.clear()
    b.push_origins(st)
    check("t9's result goes back to its origin message as a file, naming its code", len(docs) == 1
          and docs[0][0] == "sendDocument" and docs[0][1].startswith("# The summary") and docs[0][2]["reply_to_message_id"] == 12
          and docs[0][2]["chat_id"] == GROUP and "app abcdef1234 (branch hive/t9)" in docs[0][2]["caption"], docs)
    lines = [(p["chat_id"], p["reply_to_message_id"], p["text"]) for meth, p in sent if meth == "sendMessage"]
    check("t9's progress is threaded on its origin message", lines == [
        (GROUP, 12, "t9: started"), (GROUP, 12, "t9: result posted, waiting for review"),
        (GROUP, 12, "t9: review passed (worker.bot1)"), (GROUP, 12, "t9: done ✅")], lines)
    check("t9's origin is dropped once done", "t9" not in b.load_origins(), b.load_origins())
    sent.clear()
    b.CHAT.put(("cat >/dev/null; printf 'ok\\n[[task: t10]]'", "x", False, None))
    run_worker_once(b)
    check("a record-driven reply's [[task: ]] line records no origin", "t10" not in b.load_origins(), b.load_origins())

    # 5. a conversation group in mention mode: context and the dialogue log only, unless the chief of staff is asked
    b.ME.update({"id": COSBOT, "username": "CosBot"})
    b.on_message(msg(PHILO, "group", BEN, "Is a hive conscious?", 20), st, "COS")
    b.on_message(msg(PHILO, "group", BOT1, "Only if it dreams.", 21, is_bot=True), st, "COS")
    check("mention mode: plain talk doesn't wake the chief of staff", b.CHAT.empty())
    t3 = "@CosBot make this a task for @Bot3Bot"
    b.on_message(msg(PHILO, "group", BEN, t3, 22, entities=[mention(t3, "CosBot"), mention(t3, "Bot3Bot")]), st, "COS")
    _, text, _, target = b.CHAT.get(timeout=5); b.CHAT.task_done()
    env = json.loads(text[len(b.ENVELOPE_HEAD):])
    check("mention mode: @cos wakes it, with the thread", env["from"]["human"] == "ben" and env["chat"]["name"] == "philosophy"
          and any("dreams" in x["text"] for x in env["thread"]) and target["reply_to"] == 22, env)
    t4 = "@CosBot please assign me a review of t7"
    b.on_message(msg(PHILO, "group", BOT1, t4, 23, is_bot=True, entities=[mention(t4, "CosBot")]), st, "COS")
    _, text, _, _ = b.CHAT.get(timeout=5); b.CHAT.task_done()
    env = json.loads(text[len(b.ENVELOPE_HEAD):])
    check("mention mode: a known bot may ask the chief of staff", env["from"] == {"bot": "bot1", "actor": "worker.bot1"}, env["from"])
    b.on_message(msg(PHILO, "group", BEN, "thanks", 24, reply_to={"message_id": 9, "from": {"id": COSBOT, "is_bot": True}}), st, "COS")
    check("mention mode: a reply to the chief of staff's message wakes it", not b.CHAT.empty())
    b.CHAT.get(timeout=5); b.CHAT.task_done()
    rows = [json.loads(l) for l in (work / ".telegram" / "dialogue.jsonl").read_text().splitlines()]
    check("dialogue log: every message in every chat, humans and bots", [r["id"] for r in rows if r["chat"] == PHILO] == [20, 21, 22, 23, 24]
          and any(r["bot"] for r in rows) and all(r["chat_name"] for r in rows), rows[-3:])

    # 6. buttons: any human with the operator permission, in the operator chat only
    check("andy (operator) may press buttons in the operator chat", b.allowed({"id": ANDY}, {"id": OP_CHAT}))
    check("nobody presses buttons in the group", not b.allowed({"id": ANDY}, {"id": GROUP}))
    check("a stranger may not", not b.allowed({"id": STRANGER}, {"id": OP_CHAT}))
    print("PASSED")


if __name__ == "__main__":
    main()
