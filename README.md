# hive-bridge

Where humans meet a hive: a chat bridge (Telegram first) and the Floor, a read-only live board. It's the human/agent half of One Hive release R7, shared by every hive; each hive brings only its configuration.

**Status (2026-10-23):** v0.5.0 adds the Floor's bots view and the pinned status (below). Earlier (2026-10-19): extracted from [1-hive](https://github.com/1-hive/1-hive) with its history. Paths and ids are configuration (below); 1-hive runs it with the defaults. Group chats with several humans and bots work as in [`docs/design.md`](docs/design.md), when `telegram-groups.json` exists; without it, the bridge serves only the operator's private chat, as before.

- `telegram-bridge.py`: the operator's inbox and buttons, record pushes (approvals, escalations, unanswered questions, finished tasks, a daily digest), and chat with the chief of staff, who it wakes by fixed rules.
- `telegram-say.py`: send the operator one message from the host.
- `floor.py`, `floor/`, `floor_bots.py`: the Floor, a read-only board, in a browser or inside Telegram. Two views: **bots** (one card per agent: what it is doing now, what it owes, its last 24 hours; for hives of long-lived agents) and **goals** (one lane per goal, with replay at any record position).

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
- **Bots propose tasks through the record.** A bot that is asked for real work in any chat (private ones included) proposes it with its hive tool; the record holds the proposal, the bridge wakes the chief of staff for it, and once approved, the task's progress and result are threaded on the chat message the proposal cited.
- **The result comes back where the work was asked.** When a task started from a chat message closes, its result report is posted on that message as a file to download, naming the code it changed (repository, commit, the task's branch).
- **Work never reaches a bot through chat.** Telling a bot it has a task is a control signal, not conversation (One Hive's channel discipline: "runtime is not speech"), so the bridge doesn't do it: the bot's sidecar delivers it through the bot's own runtime (hive-runtime: the Iter channel, or an OpenClaw gateway's webhook). A Telegram @-mention wake was tried in v0.4.1 and removed in v0.4.2.
- **A dialogue log.** Every message the bridge sees in a listed chat, humans' and bots', goes to `HIVE_WORK_DIR/.telegram/dialogue.jsonl` (chat, sender, text, reply), outside the task record, for search and for the record's message events later (R7). The message is sent as an envelope of facts: who wrote it, what it replies to and who wrote that, its mentions, the recent thread, and the hive's agents and tasks from the record. The chief of staff's reply is threaded on the message.
- **Bots' messages are thread context only,** never requests. Unknown senders and unlisted groups are ignored. `/board` works in a group.
- **One chief of staff, one queue across every chat.** A reply whose first line is `[[to: <chat name>]]` goes to that group, for example a result announced after a review passes.
- **A task started from a message is threaded there.** When the chief of staff creates one, its reply carries `[[task: <id>]]`. The bridge then posts the task's progress (started, a question, result posted, review verdicts, done) as replies to that message.
- **Buttons** (approve, accept and the rest) stay in the operator chat (`telegram.json`), for any human with the `operator` permission.
- **Shadow evidence:** `HIVE_WORK_DIR/.telegram/addressee-rule.jsonl` logs what a mention-or-reply rule would have picked for each message. The rule never acts.
- **Telegram setup:** add the bot to the group, and turn its privacy mode off (BotFather: `/setprivacy` → Disable). Otherwise Telegram shows it only commands and replies to its own messages.

## The Floor's bots view

Three sources, never mixed up, and every line from a fixed rule (`floor_bots.py`; no model, and no agent writes its own status):
- **Now**, from **beacons**: each agent's sidecar or runner (hive-runtime v0.5.0, config `"beacon"`) posts every 30 s what the agent's own files say: the chat request it is answering (chat and message ids only), its queue, its route-hook episodes, its last step, an OpenClaw session's chat, a reviewer's current review or last failure. `POST /api/beacon`, signed with the agent's record key under the prefix `hive-beacon1` (never a valid record write). Never authoritative. No beacon for 150 s: offline. **Problems reach the operators:** when an agent's beacons report a problem for 10 minutes on end (`floor.py --alert-after`; e.g. a reviewer whose review command keeps failing, as when its model account is out of credits), the Floor messages the operators' chat once, with the error, and again once its beacons have reported none for 2 minutes. Test: `python3 tests/problem_alert_test.py`.
- **On its plate**, from the record: tasks assigned, in progress, blocked, in review, passed but not merged (the reviewed commit isn't on its branch in the hive's bare clone, floor.json `clones_dir`: the default branch if that holds the result's base, else the first other branch that does, as the publisher picks its pull request's, e.g. iter's `protomega`).
- **For you** (the needs), each saying what to do in numbered steps: a passed result to merge (title: whose change, which repository, into which branch; a link that opens its pull request on GitHub, from the registry's GitHub fetch URL, onto the branch holding it, hive/<task> or -r2...), a chief of staff's request (its own title and first lines, the whole file folded under it; closed by telling the chief of staff in the Work group), a question or escalation (answered in the Work group).
- **Who asked what**, from the bridge's dialogue log: a request's or a task's chat message (a task's `cause.external`, `telegram:<chat>/<message>`) gives the person, the chat and the first line. Direct messages: never their content.

`floor.json` (in `HIVE_CONFIG_DIR`, or `FLOOR_CONFIG`): `view` (`bots` | `goals`), `names`, `initials`, `about`, `machines` ({machine: [actors]}), `chat_names`, `clones_dir`, `auth` (`telegram`, the default through a proxy, or `none`). `FLOOR_STATE_DIR` holds its beacon samples (default `HIVE_WORK_DIR/.floor`).

**Access.** From loopback without a proxy header: open (the bridge, an SSH tunnel). Through an HTTPS proxy (it sets X-Forwarded-For): only the hive's operators, by Telegram Mini App data signed with the bot's token (the Floor needs only the derived key, `telegram-webapp.key`), or by a 30-day cookie from a 15-minute login link the bridge gives an operator in a private chat (`/floor`; shared secret `floor-session.secret`). Run the Floor as its own user, without the bridge's keys.

**In Telegram** (bridge, with `FLOOR_BOARD=bots`, `FLOOR_URL`, `FLOOR_PUBLIC_URL`): a chat with `"status": true` in `telegram-groups.json` gets one status message, pinned once and edited in place when it changes (no notifications); `/board` answers with the same text; `/floor` (or `/start floor`) in an operator's private chat opens the Floor inside Telegram and gives a login link for a browser; the bot's menu button opens it too. `FLOOR_TG_APP` (a BotFather Mini App, `bot/app`) makes the group's button open it in one tap. The chief of staff's turns are logged for the Floor (`.telegram/cos-turns.jsonl`, `cos-now.json`).

## Tests

`python3 tests/groups_sim.py` (group chats, the private chat, buttons), `python3 tests/question_reminders_sim.py` (unanswered-question pushes) and `python3 tests/status_sim.py` (the pinned status, `/floor`, login links) run against a fake Telegram and record. `uv run --project ~/repos/hive-record python tests/floor_sim.py [--serve]` runs the Floor's bots view against a scratch proto-hive record (rootless Podman), with beacons, a dialogue log and both kinds of sign-in.
