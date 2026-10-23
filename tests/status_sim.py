"""The work group's pinned status and the Floor's links, with a fake Telegram and a fake Floor.

    python3 tests/status_sim.py

Checks: the status message is posted and pinned once, edited in place only when the text changes, sent again
when it was deleted; /board answers with the same text; /floor in an operator's private chat gives the Mini App
button and a login link floor.py accepts; nobody else gets one; the chief of staff's turns are logged.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parents[1]
ANDY, BEN, STRANGER, WORK = 1002, 1003, 1999, -500


def load():
    cfg, work = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    (cfg / "telegram-token").write_text("TEST")
    (cfg / "telegram.json").write_text(json.dumps({"chat_id": ANDY, "user_id": ANDY}))
    (cfg / "floor-session.secret").write_text("k" * 64)
    (cfg / "telegram-groups.json").write_text(json.dumps({
        "chats": [{"name": "work", "chat_id": WORK, "mode": "all", "status": True}],
        "humans": [{"name": "andy", "user_id": ANDY, "permissions": ["operator"]},
                   {"name": "ben", "user_id": BEN, "permissions": ["operator"]}],
        "bots": []}))
    os.environ.update(HIVE_CONFIG_DIR=str(cfg), HIVE_WORK_DIR=str(work), FLOOR_PUBLIC_URL="https://floor.example",
                      FLOOR_BOARD="bots")
    sys.path.insert(0, str(HERE))
    sys.modules.pop("hive_paths", None)
    spec = importlib.util.spec_from_file_location("bridge_status", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    b.ME.update({"id": 3000, "username": "hive_cos_bot"})
    b.STATE.parent.mkdir(parents=True, exist_ok=True)
    return b, cfg, work


def main():
    b, cfg, work = load()
    sent, fail = [], {}

    def tg(method, **p):
        sent.append((method, p))
        if method in fail:
            raise RuntimeError(f"telegram {method}: {fail.pop(method)}")
        return {"message_id": 7000 + len(sent)}
    def safe(method, **p):
        try:
            return tg(method, **p)
        except RuntimeError:
            return None
    b.tg, b.safe = tg, safe
    status = {"text": "<b>Hive now: 1 working</b>\n🟠 <b>bot1</b> working on t1, 3m"}
    b.floor_get = lambda path: status if path == "/api/status" else {}
    st = b.load_state()

    b.push_status(st)
    methods = [m for m, _ in sent]
    assert methods == ["sendMessage", "pinChatMessage"], methods
    first = sent[0][1]
    assert first["chat_id"] == WORK and first["disable_notification"] is True and "Hive now" in first["text"]
    url = first["reply_markup"]["inline_keyboard"][0][0]["url"]
    assert url == "https://t.me/hive_cos_bot?start=floor", url
    mid = st["status_msg"][str(WORK)]["message_id"]
    print("status: posted and pinned once, with an Open the Floor button")

    sent.clear()
    b.push_status(st)
    assert not sent, sent
    status["text"] = "<b>Hive now: 0 working</b>"
    b.push_status(st)
    assert [m for m, _ in sent] == ["editMessageText"] and sent[0][1]["message_id"] == mid, sent
    print("status: same text, nothing sent; new text, edited in place")

    sent.clear()
    status["text"] = "<b>Hive now: 2 working</b>"
    fail["editMessageText"] = "Bad Request: message to edit not found"
    b.push_status(st)
    assert [m for m, _ in sent] == ["editMessageText", "sendMessage", "pinChatMessage"], sent
    assert st["status_msg"][str(WORK)]["message_id"] != mid
    print("status: deleted message, sent again and pinned")

    sent.clear()
    b.on_message({"message_id": 1, "date": int(time.time()), "chat": {"id": BEN, "type": "private"},
                  "from": {"id": BEN, "is_bot": False}, "text": "/start floor"}, st, None)
    assert len(sent) == 1, sent
    rows = sent[0][1]["reply_markup"]["inline_keyboard"]
    assert rows[0][0]["web_app"]["url"] == "https://floor.example/", rows
    link = rows[1][0]["url"]
    q = parse_qs(urlparse(link).query)
    u, e, s = q["u"][0], q["e"][0], q["s"][0]
    want = hmac.new(b"k" * 64, f"login.{u}.{e}".encode(), hashlib.sha256).hexdigest()
    assert u == str(BEN) and int(e) > time.time() and hmac.compare_digest(s, want), link
    print("/start floor (Ben, private): Mini App button and a login link the Floor accepts")

    sent.clear()
    b.on_message({"message_id": 2, "date": int(time.time()), "chat": {"id": STRANGER, "type": "private"},
                  "from": {"id": STRANGER, "is_bot": False}, "text": "/floor"}, st, None)
    assert not sent, sent
    print("/floor from a stranger: ignored")

    sent.clear()
    b.on_message({"message_id": 3, "date": int(time.time()), "chat": {"id": WORK, "type": "group", "title": "w"},
                  "from": {"id": ANDY, "is_bot": False}, "text": "/board"}, st, None)
    assert sent and sent[0][1]["text"] == status["text"], sent
    print("/board in the work group: the same text as the pinned status")

    b.cos_turn("start", time.time() - 5, WORK, True)
    assert (b.STATE.parent / "cos-now.json").is_file()
    b.cos_turn("end", time.time() - 5, WORK, True)
    assert not (b.STATE.parent / "cos-now.json").exists()
    turn = json.loads((b.STATE.parent / "cos-turns.jsonl").read_text().splitlines()[-1])
    assert turn["end"] >= turn["start"] and turn["chat"] == WORK
    print("chief of staff's turns: logged for the Floor")
    print("ALL STATUS CHECKS PASSED")


if __name__ == "__main__":
    main()
