#!/usr/bin/env python3
"""
Build MULTI-FILE refactor (rename) EDIT tasks — the genuine test of the
recall->edit-failure prediction that the single-file tasks (edit_tasks.py)
could not exercise.

Each task renames an internal `requests` helper that is imported and called
across several source files. To complete it the agent must find and update
EVERY call site; missing even one leaves the old name dangling, which the
verifier catches (import failure or a residual-name grep). This is exactly
`find_references`' home turf — if the LSP ever helps an edit, it should help here.

A task is fully synthetic-but-real (no Docker, runs native on py3.11):
  base       = current main (the symbol exists under its OLD name everywhere)
  problem    = "rename OLD_NAME -> NEW_NAME everywhere; update all call sites"
  gold_files = the SOURCE files that reference OLD_NAME (ground truth for the
               recall measurement: did the agent localize every edit site?)
  verify     = (1) `import requests` succeeds, (2) NEW_NAME is defined and
               OLD_NAME no longer appears anywhere under src/requests/.

The test-file reference to OLD_NAME is handled by the task's test_patch (the
verifier renames it), so the agent edits source only — mirroring SWE-bench,
where the test changes ship with the task.
"""
import json, os, re, subprocess

HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(HARNESS_DIR, "repos", "requests")
SRC_DIR = os.path.join(REPO_DIR, "src", "requests")
TESTS_DIR = os.path.join(REPO_DIR, "tests")

# Internal `requests` helpers (clean, non-overload defs) used across multiple
# source files. Renaming each forces the agent to update every call site.
# Verification is by import + residual-name grep (deterministic and complete:
# any missed source site leaves the old name behind, which grep catches, and any
# missed import raises ImportError), plus a smoke `import requests`. We do not
# rely on per-helper behavior tests — most of requests' suite needs the network
# (httpbin) fixture, and import+grep already fully covers a pure rename.
REFACTORS = [
    ("to_native_string", "to_native_str"),
    ("extract_cookies_to_jar", "harvest_cookies_to_jar"),
    ("get_auth_from_url", "auth_from_url"),
    ("default_hooks", "make_default_hooks"),
    ("requote_uri", "requote_url"),
    ("get_environ_proxies", "environ_proxies"),
]


def git(*args):
    return subprocess.run(["git", "-C", REPO_DIR, *args], capture_output=True, text=True)


def src_sites(name):
    """Source files (repo-relative) that reference `name`, with hit counts."""
    out = {}
    for root, _, fs in os.walk(SRC_DIR):
        for f in fs:
            if not f.endswith(".py"):
                continue
            p = os.path.join(root, f)
            txt = open(p, encoding="utf-8", errors="replace").read()
            n = len(re.findall(rf"\b{re.escape(name)}\b", txt))
            if n:
                out[os.path.relpath(p, REPO_DIR)] = n
    return out


def test_sites(name):
    out = {}
    for root, _, fs in os.walk(TESTS_DIR):
        for f in fs:
            if not f.endswith(".py"):
                continue
            p = os.path.join(root, f)
            txt = open(p, encoding="utf-8", errors="replace").read()
            if re.search(rf"\b{re.escape(name)}\b", txt):
                out[os.path.relpath(p, REPO_DIR)] = txt
    return out


def make_test_patch(old, new, tfiles):
    """A patch that renames old->new in the test files (applied at verify time)."""
    git("checkout", "-f", "main"); git("clean", "-fdqx")
    for rel in tfiles:
        p = os.path.join(REPO_DIR, rel)
        txt = open(p, encoding="utf-8", errors="replace").read()
        open(p, "w", encoding="utf-8").write(re.sub(rf"\b{re.escape(old)}\b", new, txt))
    diff = git("diff").stdout
    git("checkout", "-f", "main"); git("clean", "-fdqx")
    return diff


if __name__ == "__main__":
    git("checkout", "-f", "main"); git("clean", "-fdqx")
    tasks = []
    for old, new in REFACTORS:
        ssites = src_sites(old)
        tfiles = list(test_sites(old).keys())
        test_patch = make_test_patch(old, new, tfiles) if tfiles else ""
        tasks.append({
            "instance_id": f"rename:{old}->{new}",
            "kind": "rename",
            "base_commit": "main",
            "old_name": old, "new_name": new,
            "problem_statement": (
                f"Refactor: the internal helper `{old}` is being renamed to `{new}` for naming "
                f"consistency. Rename its definition and update EVERY call site and import across the "
                f"`requests` source so that `{old}` no longer appears anywhere under src/requests/ and "
                f"`{new}` is used instead. Do not change behavior; this is a pure rename. Do not edit tests."
            ),
            "gold_files": sorted(ssites.keys()),
            "gold_site_counts": ssites,
            "test_files": tfiles,
            "test_patch": test_patch,
        })
    out = os.path.join(HARNESS_DIR, "edit", "rename_tasks.jsonl")
    with open(out, "w") as f:
        for t in tasks:
            f.write(json.dumps(t) + "\n")
    for t in tasks:
        print(f"{t['instance_id']:42s} gold_files={len(t['gold_files'])} "
              f"sites={sum(t['gold_site_counts'].values())} :: {t['gold_files']}")
    print(f"\nwrote {len(tasks)} rename tasks to {out}")
