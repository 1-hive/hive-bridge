# hive-bridge

Where humans meet a hive: a chat bridge (Telegram first) and the Floor, a read-only live board. It's the human/agent half of One Hive release R7, shared by every hive; each hive brings only its configuration.

**Status (2026-10-19):** extracted from [1-hive](https://github.com/1-hive/1-hive) with its history. Paths and ids are configuration (below); 1-hive runs it with the defaults. Group chats with several humans and bots work as in [`docs/design.md`](docs/design.md), when `telegram-groups.json` exists; without it, the bridge serves only the operator's private chat, as before.

- `telegram-bridge.py`: the operator's inbox and buttons, record pushes (approvals, escalations, unanswered questions, finished tasks, a daily digest), and chat with the chief of staff, who it wakes by fixed rules.
- `telegram-say.py`: send the operator one message from the host.
- `floor.py`, `floor/`: the Floor, a read-only board of goals, tasks and agents, with replay at any record position.

**The design rule:** the bridge reports facts about each message (who sent it, what it replies to, its mentions) and never decides who it's for. One chief of staff, for the whole hive, interprets every human message and acts only through the record; workers never read the chat.

Related: [hive-record](https://github.com/1-hive/hive-record) (R1 + R2), [hive-runtime](https://github.com/1-hive/hive-runtime) (the operating loop), [hive-route](https://github.com/1-hive/hive-route) (R8).

## Configuration

`hive_paths.py` reads the same environment variables as [hive-runtime](https://github.com/1-hive/hive-runtime), with the same defaults (1-hive's layout): `HIVE_ID`, `HIVE_GATEWAY_PORT`, `HIVE_URL`, `HIVE_CONFIG_DIR` (`telegram-token`, `telegram.json`, `telegram-bridge.key`, `floor.key`), `HIVE_WORK_DIR` (the bridge's state in `.telegram/`, the chief of staff's follow-ups and workspace in `cos/`) and `HIVE_WORKSPACE_REPO`. The chat with the chief of staff is the command in `TELEGRAM_CHAT_CMD` (hive-runtime's `cos.sh chat`).

## Group chats

Optional `HIVE_CONFIG_DIR/telegram-groups.json`. Without it, nothing changes for a private-chat hive.

```json
{
  "chats":  [{"name": "work", "chat_id": -1001234567890, "mode": "all"},
             {"name": "protobots", "chat_id": -5437945421, "mode": "mention"}],
  "humans": [{"name": "ben", "user_id": 111, "permissions": ["operator"]},
             {"name": "andy", "user_id": 222, "permissions": ["operator"]}],
  "bots":   [{"name": "protomega2", "user_id": 333, "username": "Protomega2bot", "actor": "worker.protomegabot2"}]
}
```

- **Two modes per chat.** In a `mode: all` chat (the work group) every allowed human's message goes to the chief of staff, the hive's only interpreter of what humans write. In a `mode: mention` chat (a conversation group) only messages that @-mention the bridge's bot or reply to it do, from humans and from the listed bots (a bot may ask the chief of staff for work); everything else there is context only.
- **Waking bots that don't poll.** A bot listed with `"wake": "<chat name>"` is @-mentioned in that chat when the record has something for it: a task assigned or reassigned to it, a result to review, an answer to its question, a nudge, a review of its result that didn't pass. For agents that look at the hive only when asked (OpenClaw with `hive-task`), so nothing has to poll. Test: `tests/wake_sim.py`.
- **A dialogue log.** Every message the bridge sees in a listed chat, humans' and bots', goes to `HIVE_WORK_DIR/.telegram/dialogue.jsonl` (chat, sender, text, reply), outside the task record, for search and for the record's message events later (R7). The message is sent as an envelope of facts: who wrote it, what it replies to and who wrote that, its mentions, the recent thread, and the hive's agents and tasks from the record. The chief of staff's reply is threaded on the message.
- **Bots' messages are thread context only,** never requests. Unknown senders and unlisted groups are ignored. `/board` works in a group.
- **One chief of staff, one queue across every chat.** A reply whose first line is `[[to: <chat name>]]` goes to that group, for example a result announced after a review passes.
- **A task started from a message is threaded there.** When the chief of staff creates one, its reply carries `[[task: <id>]]`. The bridge then posts the task's progress (started, a question, result posted, review verdicts, done) as replies to that message.
- **Buttons** (approve, accept and the rest) stay in the operator chat (`telegram.json`), for any human with the `operator` permission.
- **Shadow evidence:** `HIVE_WORK_DIR/.telegram/addressee-rule.jsonl` logs what a mention-or-reply rule would have picked for each message. The rule never acts.
- **Telegram setup:** add the bot to the group, and turn its privacy mode off (BotFather: `/setprivacy` → Disable). Otherwise Telegram shows it only commands and replies to its own messages.

## Tests

`python3 tests/groups_sim.py` (group chats, the private chat, buttons) and `python3 tests/question_reminders_sim.py` (unanswered-question pushes). Both run against a fake Telegram and record.
