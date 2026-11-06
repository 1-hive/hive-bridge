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
    assert req["title"] == "The chief of staff asks: Ben's PR: before merging the PR" and req["card"]["questions"][1]["answers"][0]["word"] == "yes"
    assert req["text"].startswith("CettaPack M1 works")
    assert q9["group"] == "decide" and req["group"] == "decide"

    # front matter: a card after it still parses; kind: decision without a card is a decision; plain text is a hive fix
    reqs = [{"name": "fm-card", "text": "---\nkind: decision\nasks: ben\n---\n" + CARD.replace("# t-9: question 1", "# PR")},
            {"name": "fm-plain", "text": "---\nkind: decision\n---\n# Should vm2 get more memory?\n"},
            {"name": "fix", "text": "# reviewer.claude ends long reviews without a verdict\n\nDetails.\n"}]
    merges = {f"m-{i}": False for i in range(8)}
    tasks2 = {**{t: {"id": t, "status": "done", "owner": "worker.protocosmo2", "title": f"Work {t}",
                     "latest_review": {"reviewer": "reviewer.claude"}} for t in merges}}
    into = {t: {"into": "main", "repo": "wmtm", "branch": f"hive/{t}", "url": f"https://github.com/o/wmtm/pull/{i + 1}"}
            for i, t in enumerate(merges)}
    f2 = fb.Facts(state={"tasks": tasks2, "actors": {}}, events=[], beacons={}, samples={}, dialogue=[], groups={},
                  conf={}, floor_up=[], now=now, requests=reqs, merged=merges, merge_into=into)
    n2 = fb.needs(f2, [], [], [])
    groups = [n["group"] for n in n2]
    assert groups == ["decide", "decide"] + ["merge"] * 8 + ["ops"], groups
    assert n2[0]["card"] and n2[0]["title"] == "The chief of staff asks: PR: before merging the PR", n2[0]
    assert n2[0]["steps"] == ["Answer in the Work group (naming fm-card): the chief of staff records it and this goes away"]
    assert n2[1]["title"] == "The chief of staff asks: Should vm2 get more memory?", n2[1]["title"]
    assert n2[-1]["title"] == "reviewer.claude ends long reviews without a verdict" and n2[-1]["detail"].startswith("# reviewer")
    m0 = n2[2]
    assert m0["short"] == "wmtm #1" and m0["title"].endswith("(#1)") and "claude passed it" in m0["text"], m0
    into["m-7"] = {"into": "main", "repo": "wmtm", "branch": "hive/t-2026-11-06-p4-proof-makeup",
                   "url": "https://github.com/o/wmtm/compare/main...hive/t-2026-11-06-p4-proof-makeup?expand=1"}
    assert next(n for n in fb.needs(f2, [], [], []) if "m-7" in n["text"])["short"] == "wmtm p4-proof-makeup"
    st = fb.status(f2, [], n2)["text"]
    print(st)
    assert "❓ <b>Decide:</b> PR: before merging the PR · Should vm2 get more memory?" in st, st
    assert '🔀 <b>Merge:</b> <a href="https://github.com/o/wmtm/pull/1">wmtm #1</a>' in st and "· +2 more" in st, st
    assert "🔧 1 hive fix queued for the operators" in st and "without a verdict" not in st
    assert "10 for you" in st, st   # decisions and merges count; hive fixes don't
    print("PASSED: blocked questions and CoS requests with a card show it (title from Before, answers split); others unchanged")


if __name__ == "__main__":
    main()
