#!/usr/bin/env python3
"""Files the chief of staff sends, and transcripts: a reply's [[file: …]] may name the workspace or the outbox (by
/cos/… or host path) and nothing else, up to 50 MB and more types (zip, docx); the bridge's own messages in a group
go in the dialogue log; transcript.py makes a group's Markdown transcript, by chat name across a supergroup
upgrade, without /board; the outbox is pruned after 7 days.

    python3 tests/outgoing_files_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def load():
    tmp = Path(tempfile.mkdtemp())
    cfg, work, ws, box = tmp / "cfg", tmp / "work", tmp / "cos" / "workspace", tmp / "outbox"
    for d in (cfg, work, ws / ".git", box):
        d.mkdir(parents=True)
    (cfg / "telegram-token").write_text("TEST")
    (cfg / "telegram.json").write_text(json.dumps({"chat_id": 1, "user_id": 8}))
    (cfg / "telegram-groups.json").write_text(json.dumps({
        "chats": [{"name": "work", "chat_id": -100, "mode": "all"}, {"name": "talk", "chat_id": -200, "mode": "mention"}],
        "humans": [{"name": "zar", "user_id": 7}, {"name": "andy", "user_id": 8, "permissions": ["operator"]}]}))
    os.environ.update(HIVE_CONFIG_DIR=str(cfg), HIVE_WORK_DIR=str(work), HIVE_COS_WORKSPACE=str(ws),
                      HIVE_COS_OUTBOX=str(box), HIVE_ID="test-hive")
    sys.path.insert(0, str(HERE))
    sys.modules.pop("hive_paths", None)
    spec = importlib.util.spec_from_file_location("bridge_out", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    b.STATE.parent.mkdir(parents=True, exist_ok=True)
    return b, tmp, ws, box


class Resp:
    def __init__(self, data):
        self.data = json.dumps(data).encode()
    def read(self, n=-1):
        return self.data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def main():
    b, tmp, ws, box = load()
    calls, mid = [], [500]
    BOT = {"id": 99, "is_bot": True, "username": "cos_test_bot"}

    def fake_urlopen(req, timeout=None):
        method = req.full_url.rsplit("/", 1)[1]
        ctype = req.headers.get("Content-type", "")
        if ctype.startswith("multipart/"):
            body = req.data
            params = {}
            for part in body.split(b"--" + ctype.split("boundary=")[1].encode())[1:-1]:
                head, _, val = part.partition(b"\r\n\r\n")
                name = head.decode(errors="replace").split('name="')[1].split('"')[0]
                fn = head.decode(errors="replace").split('filename="')[1].split('"')[0] if b"filename=" in head else None
                params[name] = fn if fn else val[:-2].decode()
            params["_bytes"] = len(body)
        else:
            params = {k: v[0] for k, v in urllib.parse.parse_qs(req.data.decode()).items()}
        calls.append((method, params))
        mid[0] += 1
        res = {"message_id": mid[0], "date": int(time.time()), "chat": {"id": int(params["chat_id"])}, "from": BOT}
        if "text" in params:
            res["text"] = params["text"]
        if "caption" in params:
            res["caption"] = params["caption"]
        if "document" in params:
            res["document"] = {"file_name": params["document"]}
        if params.get("reply_to_message_id"):
            res["reply_to_message"] = {"message_id": int(params["reply_to_message_id"])}
        return Resp({"ok": True, "result": res})
    b.urllib.request.urlopen = fake_urlopen

    # 1. what a reply may attach
    (ws / "notes.md").write_text("# notes\n")
    (ws / ".git" / "config.txt").write_text("x")
    (box / "t1").mkdir()
    (box / "t1" / "result.zip").write_bytes(b"PK\x03\x04zip")
    (box / "report.docx").write_bytes(b"PK docx")
    (box / "id.key").write_text("secret")
    (tmp / "outside.md").write_text("no")
    (box / "link.md").symlink_to(tmp / "outside.md")
    with open(box / "huge.zip", "wb") as f:
        f.truncate(51 * 1024 * 1024)
    assert b.attachment("notes.md") == (ws / "notes.md").resolve()
    assert b.attachment("/cos/workspace/notes.md") == (ws / "notes.md").resolve()
    assert b.attachment("/cos/outbox/t1/result.zip") == (box / "t1" / "result.zip").resolve()
    assert b.attachment(str(box / "report.docx")) == (box / "report.docx").resolve()
    for bad, why in (("/etc/passwd", "only files"), (str(tmp / "outside.md"), "only files"), ("../outside.md", "only files"),
                     ("/cos/outbox/link.md", "only files"), (".git/config.txt", "only files"),
                     ("/cos/outbox/id.key", "not allowed"), ("/cos/outbox/huge.zip", "over 50 MB"),
                     ("/cos/outbox/none.zip", "no such file")):
        try:
            b.attachment(bad)
            raise AssertionError(f"{bad} was allowed")
        except ValueError as e:
            assert why in str(e), (bad, e)

    # 2. a reply in the work group with two files: the text, then each as a document; all three logged
    b.deliver("Here's the result.\n[[file: /cos/outbox/t1/result.zip]]\n[[file: notes.md]]", {"chat_id": -100, "reply_to": 40})
    assert [c[0] for c in calls] == ["sendMessage", "sendDocument", "sendDocument"], calls
    assert calls[1][1]["document"] == "result.zip" and calls[2][1]["document"] == "notes.md", calls
    assert calls[0][1]["reply_to_message_id"] == "40"
    log = [json.loads(x) for x in b.DIALOGUE.read_text().splitlines()]
    assert [e["text"] for e in log] == ["Here's the result.", "result.zip", "notes.md"], log
    assert all(e["sent"] and e["chat_name"] == "work" and e["from"] == "cos_test_bot" for e in log), log
    assert log[0]["reply_to"] == 40 and log[1]["files"] == [{"name": "result.zip"}], log

    # 3. not logged: private chats, and the silent pinned status
    calls.clear()
    b.tg("sendMessage", chat_id=1, text="private")
    b.tg("sendMessage", chat_id=-100, text="status", disable_notification=True)
    assert len(b.DIALOGUE.read_text().splitlines()) == 3

    # 4. the outbox keeps a file 7 days, then drops it and its empty folder
    old = time.time() - 8 * 86400
    os.utime(box / "t1" / "result.zip", (old, old))
    b.prune_outbox()
    assert not (box / "t1").exists() and (box / "report.docx").exists()

    import transcript
    # 5. the transcript: by chat name across the old chat id (message ids reused), no /board, replies quoted right
    b.DIALOGUE.write_text("".join(json.dumps(e) + "\n" for e in [
        {"chat": -5, "chat_name": "work", "at": "2026-10-21T10:00:00+00:00", "from": "andy", "id": 7, "text": "old hello"},
        {"chat": -100, "chat_name": "work", "at": "2026-11-01T10:00:00+00:00", "from": "zar", "id": 7, "text": "new question?"},
        {"chat": -100, "chat_name": "work", "at": "2026-11-01T10:01:00+00:00", "from": "cos_test_bot", "id": 8,
         "sent": True, "bot": True, "reply_to": 7, "text": "An answer."},
        {"chat": -100, "chat_name": "work", "at": "2026-11-01T10:02:00+00:00", "from": "andy", "id": 9, "text": "/board"},
        {"chat": -100, "chat_name": "work", "at": "2026-11-01T10:02:01+00:00", "from": "cos_test_bot", "id": 10,
         "sent": True, "bot": True, "reply_to": 9, "text": "BOARD"},
        {"chat": -200, "chat_name": "talk", "at": "2026-11-01T10:03:00+00:00", "from": "zar", "id": 11, "text": "elsewhere"},
        {"chat": -100, "chat_name": "work", "at": "2026-11-02T09:00:00+00:00", "from": "zar", "id": 12, "text": "",
         "files": [{"name": "plan.pdf"}]},
    ]))
    md = transcript.build(transcript.read(b.DIALOGUE), "work", hive_id="test-hive")
    assert "4 messages, 2026-10-21 to 2026-11-02" in md, md
    assert "old hello" in md and "elsewhere" not in md and "BOARD" not in md and "/board" not in md, md
    assert '**10:01 hive** _(to zar: "new question?")_' in md, md
    assert "## 2026-11-02" in md and "[file: plan.pdf]" in md, md
    week = transcript.build(transcript.read(b.DIALOGUE), "work", 7,
                              now=b.dt.datetime(2026, 11, 3, tzinfo=b.dt.UTC))
    assert "old hello" not in week and "new question?" in week, week

    # 6. the command line, as the chief of staff runs it: a chat that isn't there names the ones that are
    import subprocess
    r = subprocess.run([sys.executable, str(HERE / "transcript.py"), str(b.DIALOGUE), "work", "3650"],
                       capture_output=True, text=True, env={**os.environ, "HIVE_ID": "test-hive"})
    assert r.returncode == 0 and r.stdout.startswith("# test-hive: the work group") and "new question?" in r.stdout, r
    r = subprocess.run([sys.executable, str(HERE / "transcript.py"), str(b.DIALOGUE), "nosuch"], capture_output=True, text=True)
    assert r.returncode != 0 and "chats: work, talk" in r.stderr, r
    print("ok")


if __name__ == "__main__":
    main()
