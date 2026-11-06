"""Decision cards: how a worker's question (and the Floor's list of open decisions) asks a human for a decision.

A question file may start its text with a card, ended by a line that is exactly "---" (the details follow it):

    Status: The pin move is built and checked, but I can't push it to GitHub from pop-os.
    Before: posting the result
    1. Should an operator log me in to GitHub on pop-os? yes: I push and post the result / no: see 2
    2. Or will an operator push the branch from my checkout? yes: I post once it's there
    Recommend: 1 yes
    If nobody answers: the task stays blocked
    ---
    Details, evidence, error text...

Status (the outcome so far in one plain sentence, bad news included) and Before (the action waiting on the answer)
are optional, but a card needs one of them and at least one numbered question, each answerable in a word, with
what each answer leads to. Recommend and "If nobody answers" are optional. The card may sit anywhere in the file (the adapter puts its own
heading above it); it is the block of these lines that ends at the first "---" line after them.

parse(text) -> dict or None; render(card, head) -> the card as chat text. hive-bridge's ❓ notice and the Floor use
both, so a decision looks the same everywhere.
"""
from __future__ import annotations

import re

KEYS = {"status": "status", "before": "before", "recommend": "recommend", "recommended": "recommend",
        "if nobody answers": "if_silent", "if no answer": "if_silent", "default": "if_silent"}
LABEL = re.compile(r"^(status|before|recommend(?:ed)?|if nobody answers|if no answer|default)\s*:\s*(.+)$", re.I)
NUMBERED = re.compile(r"^(\d{1,2})[.)]\s+(.+)$")


def parse(text: str) -> dict | None:
    """The card in text: {status, before, questions: [..], recommend, if_silent}, or None when there is none."""
    lines = [l.strip() for l in (text or "").splitlines()]
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    card, last = {"questions": []}, None
    for line in lines[:end]:
        m, q = LABEL.match(line), NUMBERED.match(line)
        if m:
            last = KEYS[m.group(1).lower()]
            card[last] = m.group(2).strip()
        elif q:
            card["questions"].append(q.group(2).strip())
            last = len(card["questions"]) - 1
        elif last is not None and line and not line.startswith("#"):   # a wrapped line continues the last one
            if isinstance(last, int):
                card["questions"][last] += " " + line
            else:
                card[last] += " " + line
        elif last is not None and not line:
            continue
        else:
            card, last = {"questions": []}, None   # a heading or prose before the card: start again after it
    # a card has questions and says where things stand or what waits (a plain numbered list isn't one)
    return card if card["questions"] and (card.get("status") or card.get("before")) else None


def render(card: dict, head: str) -> str:
    """The card as chat text: the head line (what, for whom), the status, then "Before …:" and the questions."""
    out = [head]
    if card.get("status"):
        out.append(card["status"])
    out.append("")
    if card.get("before"):
        b = card["before"].rstrip(":.")
        out.append(("Before " + b[0].lower() + b[1:] if not b.lower().startswith("before") else b) + ":")
    out += [f"{i}. {q}" for i, q in enumerate(card["questions"], 1)]
    tail = [f"Recommended: {card['recommend']}" if card.get("recommend") else "",
            f"If nobody answers: {card['if_silent']}" if card.get("if_silent") else ""]
    if any(tail):
        out += [""] + [t for t in tail if t]
    return "\n".join(out)
