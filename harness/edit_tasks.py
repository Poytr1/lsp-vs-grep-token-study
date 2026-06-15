#!/usr/bin/env python3
"""
Build a local, SWE-bench-style EDIT task set from real bugfix commits in the
modern `requests` repo (runs natively on Python 3.11 — no Docker, which hangs on
this arm64 host when emulating the x86 SWE-bench images).

Each task is a genuine historical fix:
  - base       = the fix commit's PARENT (bug present, fix absent)
  - test_patch = ONLY the test-file changes from the fix commit (adds the
                 regression test that asserts the corrected behavior)
  - target tests = the new/changed test functions (must flip red->green)
  - gold_files = the SOURCE files the fix touched (ground truth for the
                 "did the agent localize every edit site?" recall measurement)
  - problem  = the commit subject/body, i.e. the bug report the engineer had

The agent is given `problem`, edits the source, and we run the target tests in a
native venv. This is the same red->green contract as SWE-bench, built locally.
"""
import json, os, re, subprocess

HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(HARNESS_DIR, "repos", "requests")

# Curated fix commits: each adds at least one named regression test that is RED
# without the fix and GREEN with it, applies cleanly, and needs no `httpbin`
# (network) fixture. Auto-validated red->green natively — see edit/good_shas.json.
COMMITS = [
    "47914226",  # Fix empty netrc entry usage (utils.py)
    "fd628095",  # Preserve leading slashes in request path_url (adapters.py)
    "a4f9a599",  # Port bpo-39057: should_bypass_proxies domain boundary (utils.py)
    "60389df6",  # Trim excess leading path separators (adapters.py)
    "3ff3ff21",  # Fix #6628: JSONDecodeError not deserializable (exceptions.py)
    "c0813a2d",  # Use TLS settings in selecting connection pool (adapters.py)
]


def git(*args):
    return subprocess.run(["git", "-C", REPO_DIR, *args], capture_output=True, text=True).stdout


def enclosing_class(test_file_abs, test_fn):
    """Find the class a test method lives in (or None for a module-level function)."""
    cls = None
    pat_cls = re.compile(r"^class\s+([A-Za-z_]\w*)")
    pat_fn = re.compile(rf"^\s*def\s+{re.escape(test_fn)}\s*\(")
    with open(test_file_abs, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = pat_cls.match(line)
            if m:
                cls = m.group(1)
            if pat_fn.match(line):
                return cls if line[0] in " \t" else None
    return cls


def build_task(short_sha):
    full = git("rev-parse", short_sha).strip()
    parent = git("rev-parse", f"{short_sha}^").strip()
    subject = git("log", "-1", "--pretty=%s", full).strip()
    body = git("log", "-1", "--pretty=%b", full).strip()

    name_only = git("show", "--name-only", "--pretty=", full).splitlines()
    src_files = [f for f in name_only if re.match(r"src/requests/.*\.py$", f)]
    test_files = [f for f in name_only if re.match(r"tests/.*\.py$", f)]

    # new/changed test functions added by this commit
    test_diff = git("show", full, "--", *test_files)
    test_fns = sorted(set(re.findall(r"^\+\s*def\s+(test_\w+)", test_diff, re.M)))

    # To name them as pytest IDs we need their enclosing class. Apply the test
    # patch onto the parent first so the functions exist at known locations.
    git("checkout", "--quiet", parent)
    git("checkout", "--quiet", "--", ".")
    for tf in test_files:
        patch = git("show", full, "--", tf)
        p = subprocess.run(["git", "-C", REPO_DIR, "apply"], input=patch,
                           capture_output=True, text=True)
        if p.returncode != 0:
            subprocess.run(["git", "-C", REPO_DIR, "apply", "--3way"], input=patch,
                           capture_output=True, text=True)

    target_ids = []
    for tf in test_files:
        for fn in test_fns:
            cls = enclosing_class(os.path.join(REPO_DIR, tf), fn)
            target_ids.append(f"{tf}::{cls}::{fn}" if cls else f"{tf}::{fn}")

    # the test patch text (to re-inject at run time)
    test_patch = "".join(git("show", full, "--", tf) for tf in test_files)

    git("checkout", "--quiet", "--", ".")
    git("checkout", "--quiet", "main")

    problem = subject + (("\n\n" + body) if body else "")
    return {
        "instance_id": f"requests@{short_sha}",
        "commit": full, "base_commit": parent,
        "problem_statement": problem,
        "gold_files": src_files,
        "test_files": test_files,
        "target_tests": target_ids,
        "test_patch": test_patch,
    }


if __name__ == "__main__":
    tasks = [build_task(c) for c in COMMITS]
    out = os.path.join(HARNESS_DIR, "edit", "tasks.jsonl")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        for t in tasks:
            f.write(json.dumps(t) + "\n")
    for t in tasks:
        print(f"{t['instance_id']:22s} gold={t['gold_files']} tests={t['target_tests']}")
    print(f"\nwrote {len(tasks)} tasks to {out}")
