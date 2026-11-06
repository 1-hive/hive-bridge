"""Answer a decision card by tapping: one button per answer under the ❓ notice (hive-bridge's send_question).

A card question offers its answers as "word: what follows / word: what follows" (card.py), e.g.
    1. Should an operator log me in to GitHub on pop-os? yes: I push and post the result / no: see 2
gives the buttons "1 · yes" and "1 · no". A question with no such answers gets no buttons: answer it in the chat.

A tap by a human with the operator permission is shown on the message ("1 · yes — Andy"), that question's buttons go
away, and the chief of staff is told what was chosen so it records task.answered as usual (its answer file, citing
the tapped message) and the worker carries on. The bridge records nothing itself: the answer goes through the
chief of staff, like any other answer. Anyone else's tap is refused with a short note.

    rows = keyboard(card, key)                       # inline_keyboard rows, or [] when nothing is tappable
    entry = new_entry(task, card, chat, message)     # what the bridge keeps in its state under key
    tap(entry, qi, oi, who) -> (ok, note, line, cos) # line: shown on the message; cos: the message for the CoS
"""
from __future__ import annotations

import re

ANSWER = re.compile(r"^\s*([A-Za-z0-9][\w'-]{0,23})\s*:\s*(.+?)\s*$")
LABEL_MAX = 24


def options(question: str) -> list[str]:
    """The answers a question offers: the "word" of each "word: consequence" part after the question mark."""
    q, mark, rest = question.partition("?")
    if not mark:
        return []
    out = []
    for part in rest.split(" / "):
        m = ANSWER.match(part)
        if not m:
            return []                # not every part is an answer: free text, no buttons
        out.append(m.group(1))
    return out if len(out) >= 2 and len({o.lower() for o in out}) == len(out) else []


def keyboard(card: dict, key: str, answered: dict | None = None) -> list[list[dict]]:
    """One row per tappable question (its answers side by side); answered questions are left out."""
    rows = []
    for qi, q in enumerate(card.get("questions") or []):
        if answered and str(qi) in answered:
            continue
        opts = options(q)
        if opts:
            rows.append([{"text": f"{qi + 1} · {o}"[:LABEL_MAX + 4], "callback_data": f"dq:{key}:{qi}:{oi}"}
                         for oi, o in enumerate(opts)])
    return rows


def new_entry(task: str, card: dict, chat: int, message: int, text: str) -> dict:
    return {"task": task, "chat": chat, "message": message, "text": text, "card": card, "answered": {}}


def parse_data(data: str) -> tuple[str, int, int] | None:
    """callback_data "dq:<key>:<question>:<option>" -> (key, question index, option index)."""
    m = re.fullmatch(r"dq:([A-Za-z0-9_.-]{1,40}):(\d{1,2}):(\d{1,2})", data or "")
    return (m.group(1), int(m.group(2)), int(m.group(3))) if m else None


def tap(entry: dict, qi: int, oi: int, who: str) -> tuple[bool, str, str, str]:
    """Record who chose what: (ok, a short note for the tapper, the line added to the message, the CoS's message)."""
    qs = entry["card"].get("questions") or []
    if qi >= len(qs):
        return False, "unknown question", "", ""
    opts = options(qs[qi])
    if oi >= len(opts):
        return False, "unknown answer", "", ""
    if str(qi) in entry["answered"]:
        return False, "already answered", "", ""
    choice = opts[oi]
    entry["answered"][str(qi)] = {"choice": choice, "by": who}
    line = f"✅ {qi + 1} · {choice} ({who})"
    q = qs[qi]
    cos = (f"{who} (operator) answered question {qi + 1} of {entry['task']}'s decision card by tapping \"{choice}\". "
           f"The question: {q} Record task.answered on {entry['task']} with this answer (an answer file saying who "
           f"chose what, citing telegram:{entry['chat']}/{entry['message']})"
           + (", once the other questions on the card are answered too" if len(entry["answered"]) < tappable(entry) else "")
           + ". Reply in one line.")
    return True, f"{choice}: sent to the chief of staff", line, cos


def tappable(entry: dict) -> int:
    return sum(1 for q in entry["card"].get("questions") or [] if options(q))
