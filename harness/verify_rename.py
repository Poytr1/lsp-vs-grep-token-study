#!/usr/bin/env python3
"""
Verifier for MULTI-FILE rename tasks. No Docker.

For each episode (a candidate patch from the agent on a rename task):
  clean tree (incl. __pycache__) -> base=main -> apply the AGENT's patch
  -> resolved iff: (1) `import requests` succeeds, AND
                   (2) NEW_NAME is importable from its defining module, AND
                   (3) OLD_NAME no longer appears anywhere under src/requests/.

Crucially we also measure PARTIAL RECALL, which is the whole point of these
tasks: of the gold source files that referenced OLD_NAME, how many did the
agent fully convert? This lets us quantify the recall->edit-failure link
directly (not just pass/fail):
  site_recall = (#gold files with ZERO residual OLD_NAME after the patch) / (#gold files)
  residual_files = the gold files where OLD_NAME still lingers (the missed sites)

Failure classification:
  resolved      = passes all three gates
  missed_site   = NOT resolved AND at least one gold file still has OLD_NAME
                  (the recall miss — the prediction's target failure mode)
  broke_import  = OLD_NAME fully gone but `import requests` fails (a rename done
                  wrong: e.g. renamed a call but not the def, or vice versa)
  no_edit       = empty patch
"""
import json, os, re, subprocess, sys

HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(HARNESS_DIR, "repos", "requests")
SRC_DIR = os.path.join(REPO_DIR, "src", "requests")
TASKS_PATH = os.path.join(HARNESS_DIR, "edit", "rename_tasks.jsonl")
PYBIN = os.environ.get("PYBIN", "/tmp/modreq_venv/bin/python")

TASKS = {t["instance_id"]: t for t in (json.loads(l) for l in open(TASKS_PATH))}


def git(*args, inp=None):
    return subprocess.run(["git", "-C", REPO_DIR, *args], input=inp, capture_output=True, text=True)


def clean():
    git("checkout", "-f", "main")
    git("clean", "-fdqx")          # -x also clears __pycache__ (stale .pyc poisons import)


def count_in_file(rel, name):
    p = os.path.join(REPO_DIR, rel)
    if not os.path.isfile(p):
        return 0
    return len(re.findall(rf"\b{re.escape(name)}\b", open(p, encoding="utf-8", errors="replace").read()))


def grep_src(name):
    total = 0
    for root, _, fs in os.walk(SRC_DIR):
        for f in fs:
            if f.endswith(".py"):
                total += len(re.findall(rf"\b{re.escape(name)}\b",
                                        open(os.path.join(root, f), encoding="utf-8", errors="replace").read()))
    return total


def verify_one(rec):
    task = TASKS.get(rec["instance"])
    if task is None:
        rec["resolved"] = False; rec["failure"] = "unknown_task"; return rec
    old, new = task["old_name"], task["new_name"]
    gold = task["gold_files"]

    clean()
    applied = True
    if rec.get("patch", "").strip():
        r = git("apply", inp=rec["patch"])
        if r.returncode != 0:
            r = git("apply", "--3way", inp=rec["patch"])
        applied = r.returncode == 0
    else:
        applied = False  # no_edit

    # partial recall: gold files fully converted (no OLD_NAME left)
    residual = [f for f in gold if count_in_file(f, old) > 0] if applied else gold
    site_recall = (len(gold) - len(residual)) / len(gold) if gold else 0.0
    old_left_total = grep_src(old) if applied else None

    import_ok = False
    if applied:
        imp = subprocess.run([PYBIN, "-B", "-c", f"import requests; from requests import *; "
                              f"import importlib; m=importlib.import_module('requests'); "],
                             capture_output=True, text=True, cwd=REPO_DIR)
        import_ok = imp.returncode == 0

    new_present = grep_src(new) > 0 if applied else False
    resolved = bool(applied and import_ok and (old_left_total == 0) and new_present)

    if resolved:
        failure = "resolved"
    elif not rec.get("patch", "").strip():
        failure = "no_edit"
    elif not applied:
        failure = "patch_conflict"
    elif residual:
        failure = "missed_site"        # the recall miss
    elif not import_ok:
        failure = "broke_import"
    else:
        failure = "other"

    clean()
    rec["resolved"] = resolved
    rec["site_recall"] = round(site_recall, 3)
    rec["residual_files"] = residual
    rec["old_left_total"] = old_left_total
    rec["failure"] = failure
    return rec


if __name__ == "__main__":
    path = sys.argv[1]
    recs = [json.loads(l) for l in open(path)]
    out = []
    for rec in recs:
        rec = verify_one(rec)
        out.append(rec)
        print(f"{rec['instance']:42s} {rec['arm']:9s} r{rec.get('rollout','?')} "
              f"resolved={rec['resolved']!s:5s} {rec['failure']:13s} "
              f"recall={rec['site_recall']:.2f} residual={rec.get('residual_files')} "
              f"tok={rec.get('tokens_total',0):6d}")
    clean()
    outpath = path.replace(".jsonl", "_verified.jsonl")
    with open(outpath, "w") as f:
        for rec in out:
            f.write(json.dumps(rec) + "\n")
    n = sum(r["resolved"] for r in out)
    print(f"\n{n}/{len(out)} resolved. wrote {outpath}")
