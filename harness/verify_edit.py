#!/usr/bin/env python3
"""
Native pytest verifier for the EDIT study. No Docker.

For each episode (a candidate patch produced by edit_harness.py):
  clean tree -> checkout base_commit -> apply the AGENT's patch
             -> apply the task's test_patch (adds the regression test)
             -> run the target tests in the venv
  resolved = all target tests pass AND the patch applied cleanly.

Also classifies non-resolved failures (missed_site / over_edit / wrong_rewrite /
no_edit / patch_conflict) to test whether the reference-completeness recall
ceiling propagates into edit failures.

Usage:
  python verify_edit.py runs/edit_opus.jsonl              # verify + write *_verified.jsonl
  PYBIN=/tmp/modreq_venv/bin/python python verify_edit.py runs/edit_opus.jsonl
"""
import json, os, re, subprocess, sys

HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(HARNESS_DIR, "repos", "requests")
TASKS_PATH = os.path.join(HARNESS_DIR, "edit", "tasks.jsonl")
PYBIN = os.environ.get("PYBIN", "/tmp/modreq_venv/bin/python")

TASKS = {t["instance_id"]: t for t in (json.loads(l) for l in open(TASKS_PATH))}


def git(*args, inp=None):
    return subprocess.run(["git", "-C", REPO_DIR, *args], input=inp,
                          capture_output=True, text=True)


def apply_patch(text, three=True):
    if not text.strip():
        return True
    r = git("apply", inp=text)
    if r.returncode != 0 and three:
        r = git("apply", "--3way", inp=text)
    return r.returncode == 0


def run_tests(ids):
    r = subprocess.run([PYBIN, "-m", "pytest", *ids, "-q", "--no-header",
                        "-p", "no:cacheprovider"], capture_output=True, text=True, cwd=REPO_DIR)
    out = r.stdout.strip().splitlines()
    return r.returncode == 0, (out[-1] if out else r.stderr.strip()[-160:])


def classify(rec, resolved, patch_applied):
    if resolved:
        return "resolved"
    if not rec.get("patch", "").strip():
        return "no_edit"
    if not patch_applied:
        return "patch_conflict"
    gold = set(rec.get("gold_files", []))
    got = set(rec.get("edited_files", []))
    if got and got < gold:
        return "missed_site"
    if got - gold:
        return "over_edit"
    return "wrong_rewrite"


def verify_one(rec):
    task = TASKS.get(rec["instance"])
    if task is None:
        rec["resolved"] = False; rec["failure"] = "unknown_task"; return rec
    git("checkout", "-f", task["base_commit"]); git("clean", "-fdq")
    patch_applied = apply_patch(rec.get("patch", ""))
    # the regression test is added at verify time (agent never saw it)
    apply_patch(task["test_patch"])
    resolved, tail = (False, "patch did not apply") if not patch_applied else run_tests(task["target_tests"])
    git("checkout", "-f", "main"); git("clean", "-fdq")
    rec["resolved"] = bool(resolved)
    rec["test_tail"] = tail
    rec["failure"] = classify(rec, resolved, patch_applied)
    return rec


if __name__ == "__main__":
    path = sys.argv[1]
    recs = [json.loads(l) for l in open(path)]
    out = []
    for rec in recs:
        rec = verify_one(rec)
        out.append(rec)
        print(f"{rec['instance']:20s} {rec['arm']:9s} r{rec.get('rollout','?')} "
              f"resolved={rec['resolved']!s:5s} {rec['failure']:14s} "
              f"edits={rec.get('n_edits',0)} tok={rec.get('tokens_total',0):6d}  [{rec.get('test_tail','')}]")
    git("checkout", "-f", "main"); git("clean", "-fdq")
    outpath = path.replace(".jsonl", "_verified.jsonl")
    with open(outpath, "w") as f:
        for rec in out:
            f.write(json.dumps(rec) + "\n")
    n_res = sum(r["resolved"] for r in out)
    print(f"\n{n_res}/{len(out)} resolved. wrote {outpath}")
