# hive-bridge

Where humans meet a hive: a chat bridge (Telegram first) and the Floor, a read-only live board. It's the human/agent half of One Hive release R7, shared by every hive; each hive brings only its configuration.

**Status (2026-10-19):** extracted from [1-hive](https://github.com/1-hive/1-hive) with its history. Paths and ids are configuration (below); 1-hive runs it with the defaults. It still handles one private chat with one human. Next: group chats with several humans, as in [`docs/design.md`](docs/design.md).

- `telegram-bridge.py`: the operator's inbox and buttons, record pushes (approvals, escalations, unanswered questions, finished tasks, a daily digest), and chat with the chief of staff, who it wakes by fixed rules.
- `telegram-say.py`: send the operator one message from the host.
- `floor.py`, `floor/`: the Floor, a read-only board of goals, tasks and agents, with replay at any record position.

**The design rule:** the bridge reports facts about each message (who sent it, what it replies to, its mentions) and never decides who it's for. One chief of staff, for the whole hive, interprets every human message and acts only through the record; workers never read the chat.

Related: [hive-record](https://github.com/1-hive/hive-record) (R1 + R2), [hive-runtime](https://github.com/1-hive/hive-runtime) (the operating loop), [hive-route](https://github.com/1-hive/hive-route) (R8).

## Configuration

`hive_paths.py` reads the same environment variables as [hive-runtime](https://github.com/1-hive/hive-runtime), with the same defaults (1-hive's layout): `HIVE_ID`, `HIVE_GATEWAY_PORT`, `HIVE_URL`, `HIVE_CONFIG_DIR` (`telegram-token`, `telegram.json`, `telegram-bridge.key`, `floor.key`), `HIVE_WORK_DIR` (the bridge's state in `.telegram/`, the chief of staff's follow-ups and workspace in `cos/`) and `HIVE_WORKSPACE_REPO`. The chat with the chief of staff is the command in `TELEGRAM_CHAT_CMD` (hive-runtime's `cos.sh chat`).
