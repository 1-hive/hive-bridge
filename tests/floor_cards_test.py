#!/usr/bin/env python3
"""The Floor's "Needs you" shows decision cards (card.py): a blocked task whose question file holds a card, and a
chief-of-staff request written as one, get a title from "Before", the status, each question with its answers
split out, and the tap hint; a question without a card keeps the old one-line form.

    python3 tests/floor_cards_test.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import floor_bots as fb  # noqa: E402

CARD = """# t-9: question 1

Status: CettaPack M1 works but isn't faster: 0.94x with guards counted.
Before: merging the PR
1. It adds your PDF note to the public repo. OK? yes: merge as is / no: remove it first
2. Do you want an M2? yes: I file it / no: we stop here
Recommend: 1 yes, 2 no
If nobody answers: the PR waits
---
details
"""


def main():
    now = time.time()
    tasks = {"t-9": {"id": "t-9", "status": "blocked", "owner": "worker.protomegabot2", "block": {"needs": "decision", "reason": "r"}},
             "t-8": {"id": "t-8", "status": "blocked", "owner": "worker.protocosmo2", "block": {"needs": "decision", "reason": "Plain question?"}}}
    evs = [{"type": "task.blocked", "task": t, "recorded_at": "2026-11-06T10:00:00Z"} for t in tasks]
    f = fb.Facts(state={"tasks": tasks, "actors": {}}, events=evs, beacons={}, samples={}, dialogue=[], groups={},
                 conf={}, floor_up=[], now=now, questions={"t-9": CARD, "t-8": "no card here"},
                 requests=[{"name": "2026-11-06-ben", "text": CARD.replace("# t-9: question 1", "# Request: Ben's PR")}])
    cos = [{"kind": "task_blocked_unanswered_any", "id": t} for t in tasks]
    out = fb.needs(f, [], cos, [])
    q9 = next(n for n in out if "t-9" in n["text"])
    assert q9["title"] == "protomegabot2 asks: before merging the PR", q9["title"]
    c = q9["card"]
    assert c["status"].startswith("CettaPack M1 works") and c["before"] == "merging the PR"
    assert c["questions"][0]["q"] == "It adds your PDF note to the public repo. OK?", c["questions"][0]
    assert c["questions"][0]["answers"] == [{"word": "yes", "leads": "merge as is"}, {"word": "no", "leads": "remove it first"}]
    assert c["recommend"] == "1 yes, 2 no" and c["if_silent"] == "the PR waits"
    assert q9["steps"][0].startswith("Tap an answer"), q9["steps"]
    q8 = next(n for n in out if "t-8" in n["text"])
    assert q8["card"] is None and q8["title"] == "protocosmo2 asks: Plain question?" and q8["steps"][0].startswith("Answer in")
    req = next(n for n in out if n["title"].startswith("The chief of staff asks"))
    assert req["title"] == "The chief of staff asks: before merging the PR" and req["card"]["questions"][1]["answers"][0]["word"] == "yes"
    assert req["text"].startswith("CettaPack M1 works")
    print("PASSED: blocked questions and CoS requests with a card show it (title from Before, answers split); others unchanged")


if __name__ == "__main__":
    main()
