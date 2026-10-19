# hive-bridge: design

Status: agreed 2026-10-19; built in v0.2.0 (see the README's "Group chats"). Not yet built: a task's origin message on the record (the open question below). Until then, the chief of staff routes results with `[[to: <chat>]]`.

`hive-bridge` is where humans meet a hive: a chat bridge (Telegram first) and the read-only Floor. It's the human/agent half of the release plan's R7, shared by every hive. A hive brings only configuration. Today's `tools/telegram-bridge.py` is its starting point; it handles one private chat with one human.

## The rule: one interpreter

**The chief of staff is the only agent that interprets what humans write.** The bridge does no addressee classification; it only reports facts about each message.

Deterministic rules fail on ordinary requests:
- "@bot1, please look at the work done by @bot3 and rate it": two bots are mentioned, but only bot1 should act. bot3 is the subject.
- A reply to bot1 saying "this work looks bad, please suggest improvements @bot3": the reply goes to bot1, yet bot3 should act, with bot1's work as its input.

This departs from the roadmap's "deterministic addressee classification" (M1 channel discipline). The other parts of channel discipline stay deterministic: transport identity, one public response owner, and no-op suppression.

**One chief of staff for the whole hive, across every chat.** It's a single record actor with one key, and it remembers and knows about every group (decided 2026-10-19):
- **Memory that outlives a conversation:** the record holds every task, with the chat it came from. The chief of staff also keeps its own notes, hive-wide and per chat, in its workspace: who asked for what, open threads, how each person likes to work. A restart or context compaction loses nothing it needs.
- **It knows which chat it's answering.** Every envelope names its chat, and every reply goes back to the message that woke it. Work started in one group can be referred to from another ("the fix bot2 did for Ben's group").
- **What crosses between groups.** By default the chief of staff may mention another group's tasks and results, but never quote its messages. A chat can be marked `private` in the config, and then nothing from it is shown in other chats.

**Workers never read the chat as input.** They take work only from the record, as assigned tasks. A message can therefore never be misread by the wrong bot: exactly one agent reads messages, and everything else acts on the record.

## What the bridge does, all of it deterministic

1. **Identity, from the transport.** Each chat update gives the sender's platform id. The bridge looks it up in the hive's config:
   - **A human on the allow-list:** passed on, with that human's name and permissions.
   - **A known bot** (the hive's own workers, or other bots): never passed on as a request. It's kept only as thread context.
   - **Anyone else:** ignored and logged.
2. **Facts, not decisions.** For each human message, the bridge builds a message envelope (below): the reply-to chain, mentions, attachments, and the recent thread. It doesn't judge who the message is for.
3. **Waking the chief of staff,** one message at a time across all chats (a single queue, so its memory stays consistent), with the envelope plus a context block (below). If the chief of staff is busy, the message queues, and the human sees that it's queued.
4. **Replies, in thread.** The chief of staff's reply goes back as a reply to the message that woke it. Empty or no-op replies are dropped (no-op suppression), and the chief of staff stays silent on small talk between humans.
5. **Operator actions as record events,** from buttons or commands, signed with the bridge's own operator key. Which buttons exist depends on the profile: goals (approve, accept, reopen, abandon, +budget) appear only on hives whose profile has goals. Only allow-listed humans with the operator permission may press them.
6. **Pushes from the record, by fixed rules:** the inbox (approvals, escalations, unanswered questions with reminders), the feed of finished tasks, and the digest. No agent decides what reaches a human.
7. **Wake rules from the record:** a goal approved, an escalation to the chief of staff, a blocked task, a passed review. They wake the chief of staff with a fixed prompt, as in 1-hive today.

## Message envelope (bridge to chief of staff)

```json
{
  "chat": {"id": "<platform chat id>", "name": "…", "kind": "group | private", "private": false},
  "message": {"id": "…", "at": "<RFC 3339>", "text": "…", "attachments": ["…"]},
  "from": {"human": "andy", "permissions": ["operator"]},
  "reply_to": {"id": "…", "from": "bot1", "text": "…", "task": "<record task id, if the bridge knows it>"},
  "mentions": ["bot1", "bot3"],
  "thread": [{"id": "…", "from": "…", "text": "…"}]
}
```

`mentions` and `reply_to` are facts for the chief of staff to weigh, not instructions.

## Context block

With each envelope, the bridge also sends:
- **the hive's workers and reviewers:** name, what each does, and which harness and model;
- **each one's open and recent tasks,** with their latest results (from the record);
- **the record tasks started from any chat,** each with its origin chat, this chat's first, so "this work" can be resolved to a specific result.

## What the chief of staff does with it

It acts **only through the record**, then replies in the thread:
- **A request for work** becomes `task.created` and `task.assigned` to the right worker. The task carries the message's origin (chat and message id), so the bridge can thread updates and results back.
- **"Rate or review X's work"** becomes `review.assigned` for the result in question, with the asked bot as reviewer. If that bot isn't registered as a reviewer, it becomes a task to that bot instead.
- **"Improve on X's work"** becomes a task for the other bot, with X's result pinned as input.
- **When unsure,** it asks one short question in the thread instead of guessing.
- **Not for the hive** (people talking to each other): no reply.

## Results back into the chat

When a task started from a chat message posts its result or closes, the bridge posts a short line in the thread, as a reply to the origin message. A worker that has its own chat identity may post its full answer itself, as a reply to the same origin message, which the task carries. Either way, the worker gets the origin message only through the task, never by reading the chat.

## Shadow evidence for the roadmap

If wanted, the bridge also logs what a simple rule would have decided ("mentioned or replied-to bot is the addressee"), next to the chief of staff's actual decision. The rule never acts. The comparison shows on real messages whether the rules would have been good enough (roadmap rule 3).

## Configuration, per hive

```yaml
record: {url: http://127.0.0.1:8470, hive: <id>, key_file: <bridge operator key>}
profile: core | 1-hive          # decides which buttons and pushes exist
chats:
  - {id: <platform chat id>, name: <short name>, kind: group, private: false}
humans:
  - {name: ben,  platform_id: <id>, permissions: [operator]}
  - {name: andy, platform_id: <id>, permissions: [operator]}
bots:                           # known bot ids: context only, never requests
  - {name: bot1, platform_id: <id>, actor: worker.bot1}
chief_of_staff: {actor: cos, command: "<reads the envelope on stdin, prints the reply>", timeout_s: 960}
pushes: {digest_at: "08:00", question_wait_min: 30, question_remind_h: 2}
```

The chief of staff is any command that reads the envelope and prints a reply: Claude Code (1-hive), an Iter bot, or OpenClaw. Hives choose their own; the interface stays the same.

## Failure behaviour

- **The chief of staff is down or times out:** the bridge replies "the chief of staff didn't answer; try again" in the thread. Nothing is lost, since the message stays in the bridge's log.
- **The record is unreachable:** pushes and buttons pause; chat still reaches the chief of staff, which says it can't act.
- **An unknown sender in a group:** ignored, never forwarded.

## Open questions

- **Where the task's origin goes on the record.** The core profile has no field for it yet: an `ext.origin` field in a profile, or an amendment.
- **Slack:** the same envelope, a second transport.
