"""floor.py's target_branch: a reviewed result merges into the default branch if that holds its base, else into the
first other branch (not hive/*) that does, as the publisher picks its pull request's. An agent working from its own
line (iter's protomega, which shares no history with master) is merged once its code is on that line, not on master.

    python tests/merge_target_test.py
"""
import ast
import os
import re
import subprocess
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "floor.py"
tree = ast.parse(SRC.read_text())
fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "target_branch")
ns = {"Path": Path, "re": re, "subprocess": subprocess}
exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
target = ns["target_branch"]

env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}


def git(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, env=env, check=True).stdout.strip()


def commit(d, msg):
    git(d, "commit", "-q", "--allow-empty", "-m", msg)
    return git(d, "rev-parse", "HEAD")


with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    git(d, "init", "-q", "-b", "master")
    m1 = commit(d, "master")
    git(d, "checkout", "-q", "--orphan", "protomega")
    p1 = commit(d, "protomega root")
    git(d, "checkout", "-q", "-b", "hive/t-1")
    code = commit(d, "the task's work")
    git(d, "checkout", "-q", "master")

    assert target(d, p1, env) == "protomega"          # base only on protomega (and hive/t-1, which doesn't count)
    assert target(d, m1, env) == "master"             # base on the default branch
    assert target(d, None, env) == "master"           # no base pinned: the default branch
    assert target(d, "not-a-sha", env) == "master"
    on = lambda b: subprocess.run(["git", "-C", str(d), "merge-base", "--is-ancestor", code, b], env=env).returncode == 0
    assert not on(target(d, p1, env))                 # waiting for a merge into protomega
    git(d, "checkout", "-q", "protomega")
    git(d, "merge", "-q", "--no-ff", "-m", "merge t-1", "hive/t-1")
    assert on(target(d, p1, env))                     # merged into protomega: no merge to do, though master lacks it
print("PASSED: a result merges into the branch holding its base (default first, never hive/*)")
