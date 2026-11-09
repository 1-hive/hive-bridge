#!/usr/bin/env python3
"""Files people send reach the chief of staff: a captionless .md in the work group is downloaded and goes in the
envelope with its text; a separate message right after it from the same sender carries it; a reply to the file's
message carries it under reply_to; a photo comes by path only; a private message's file comes as a block.

    python3 tests/incoming_files_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MD = "# Comments\n\nThe worker should drop the shim.\n"


def load():
    tmp = Path(tempfile.mkdtemp())
    cfg, work = tmp / "cfg", tmp / "work"
    cfg.mkdir()
    work.mkdir()
    (cfg / "telegram-token").write_text("TEST")
    (cfg / "telegram.json").write_text(json.dumps({"chat_id": 1, "user_id": 1}))
    (cfg / "telegram-groups.json").write_text(json.dumps({
        "chats": [{"name": "work", "chat_id": -100, "mode": "all"}],
        "humans": [{"name": "zar", "user_id": 7}, {"name": "andy", "user_id": 8, "permissions": ["operator"]}]}))
    os.environ.update(HIVE_CONFIG_DIR=str(cfg), HIVE_WORK_DIR=str(work))
    sys.path.insert(0, str(HERE))
    sys.modules.pop("hive_paths", None)
    spec = importlib.util.spec_from_file_location("bridge_files", HERE / "telegram-bridge.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    b.STATE.parent.mkdir(parents=True, exist_ok=True)
    return b


class Resp:
    def __init__(self, data):
        self.data = data
    def read(self, n=-1):
        return self.data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def main():
    b = load()
    blobs = {"fid-md": MD.encode(), "fid-photo": b"\xff\xd8jpeg"}
    downloads = []

    def fake_tg(method, **p):
        assert method == "getFile", method
        return {"file_path": f"documents/{p['file_id']}"}

    def fake_urlopen(url, timeout=None):
        fid = url.rsplit("/", 1)[1]
        downloads.append(fid)
        return Resp(blobs[fid])
    b.tg = fake_tg
    b.urllib.request.urlopen = fake_urlopen
    b.save_state = lambda st: None
    b.record_context = lambda: {}
    b.safe = lambda *a, **k: {}
    st = {"threads": {}, "pending_reason": {}}
    chat = {"id": -100, "type": "supergroup"}
    zar, andy = {"id": 7, "first_name": "Zar"}, {"id": 8, "first_name": "Andy"}
    doc = {"message_id": 313, "date": 1, "chat": chat, "from": zar,
           "document": {"file_id": "fid-md", "file_name": "codex comments.md", "mime_type": "text/markdown", "file_size": len(MD)}}

    def envelope():
        item = b.CHAT.get_nowait()
        return json.loads(item[1][len(b.ENVELOPE_HEAD):])

    # 1. the captionless file itself goes to the chief of staff, with its text
    b.on_group_message(doc, st, "chat")
    env = envelope()
    f = env["message"]["files"][0]
    assert f["name"] == "codex comments.md" and f["text"] == MD, f
    p = Path(f["path"])
    assert p.read_text() == MD and oct(p.stat().st_mode & 0o777) == "0o640", p
    assert oct(p.parent.stat().st_mode & 0o777) == "0o750"

    # 2. the sender's next message (not a reply) carries the file, downloaded once
    b.on_group_message({"message_id": 314, "date": 2, "chat": chat, "from": zar,
                        "text": "@cos my codex has some comments on it"}, st, "chat")
    env = envelope()
    f = env["message"]["files"][0]
    assert f["from_message"] == 313 and f["text"] == MD, f
    assert downloads == ["fid-md"], downloads
    assert env["thread"][-1]["files"][0]["name"] == "codex comments.md", env["thread"]

    # 3. someone else's reply to the file's message: under reply_to, not message
    b.on_group_message({"message_id": 316, "date": 3, "chat": chat, "from": andy, "text": "can you see it here?",
                        "reply_to_message": doc}, st, "chat")
    env = envelope()
    assert "files" not in env["message"], env["message"]
    assert env["reply_to"]["files"][0]["text"] == MD, env["reply_to"]

    # 4. andy's own next message doesn't pick up zar's file
    b.on_group_message({"message_id": 317, "date": 4, "chat": chat, "from": andy, "text": "hello"}, st, "chat")
    assert "files" not in envelope()["message"]

    # 5. a photo with a caption: path, no text; a voice note is named, not downloaded
    b.on_group_message({"message_id": 318, "date": 5, "chat": chat, "from": andy, "caption": "this?",
                        "photo": [{"file_id": "fid-small", "file_size": 10, "width": 9, "height": 9},
                                  {"file_id": "fid-photo", "file_size": 900, "width": 90, "height": 90}],
                        "voice": {"file_id": "fid-voice", "mime_type": "audio/ogg"}}, st, "chat")
    fs = envelope()["message"]["files"]
    assert fs[0]["name"] == "photo-318.jpg" and "text" not in fs[0] and Path(fs[0]["path"]).is_file(), fs
    assert "not downloaded" in fs[1]["note"] and "path" not in fs[1], fs
    assert downloads == ["fid-md", "fid-photo"], downloads

    # 6. a file over 20 MB is listed with why, not dropped
    b.on_group_message({"message_id": 319, "date": 6, "chat": chat, "from": andy,
                        "document": {"file_id": "fid-big", "file_name": "big.pdf", "file_size": 30 * 1024 * 1024}}, st, "chat")
    f = envelope()["message"]["files"][0]
    assert "over 20 MB" in f["note"] and "path" not in f, f

    # 7. the operator's private chat: the file as a block after the caption
    b.on_message({"message_id": 50, "date": 7, "chat": {"id": 1, "type": "private"}, "from": {"id": 1},
                  "caption": "read this",
                  "document": {"file_id": "fid-md", "file_name": "notes.md", "file_size": len(MD)}}, st, "chat")
    msg = b.CHAT.get_nowait()[1]
    assert msg.startswith("read this\n\n[attached file: notes.md, ") and MD in msg and "[end of notes.md]" in msg, msg
    print("incoming_files_test: ok")


if __name__ == "__main__":
    main()
