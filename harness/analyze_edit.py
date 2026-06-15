#!/usr/bin/env python3
"""
Analyze the EDIT study (verified episodes). Answers:
  - pass@1 (resolved rate) per arm  -> does the LSP help the agent SUCCEED at edits?
  - failure-mode breakdown per arm  -> esp. missed_site rate (the recall->edit
    prediction: if the LSP can't lift recall, missed_site failures persist across arms)
  - tokens-to-success (mean tokens on resolved episodes) per arm, at iso-success
  - arm-C revealed tool preference (grep vs semantic) on edit tasks
"""
import json, sys, os
from collections import defaultdict

ARMS = ["A_grep", "B_lsp", "C_both", "D_forced"]
SEM = {"find_references", "find_definition", "document_symbols"}
GREP = {"search_text"}


def load(path):
    return [json.loads(l) for l in open(path)]


def main(path):
    recs = load(path)
    by_arm = defaultdict(list)
    for r in recs:
        by_arm[r["arm"]].append(r)

    print(f"=== EDIT study: {len(recs)} episodes, {len(set(r['instance'] for r in recs))} tasks ===\n")
    print(f"{'arm':<10}{'N':>4}{'pass@1':>8}{'resolved':>10}{'tok(res)':>10}{'turns':>7}{'edits':>7}")
    for a in ARMS:
        rs = by_arm.get(a, [])
        if not rs:
            continue
        n = len(rs)
        res = [r for r in rs if r.get("resolved")]
        p1 = len(res) / n
        tok = sum(r["tokens_total"] for r in res) / len(res) if res else 0
        turns = sum(r.get("turns", 0) for r in rs) / n
        edits = sum(r.get("n_edits", 0) for r in rs) / n
        print(f"{a:<10}{n:>4}{p1:>8.2f}{len(res):>7}/{n:<2}{tok:>10.0f}{turns:>7.1f}{edits:>7.1f}")

    print("\n=== failure-mode breakdown per arm (counts) ===")
    modes = ["resolved", "missed_site", "wrong_rewrite", "over_edit", "no_edit", "patch_conflict"]
    print(f"{'arm':<10}" + "".join(f"{m:>15}" for m in modes))
    for a in ARMS:
        rs = by_arm.get(a, [])
        if not rs:
            continue
        c = defaultdict(int)
        for r in rs:
            c[r.get("failure", "?")] += 1
        print(f"{a:<10}" + "".join(f"{c.get(m,0):>15}" for m in modes))

    # The recall->edit prediction: missed_site among NON-resolved
    print("\n=== missed_site rate (share of failures that are localization/recall misses) ===")
    for a in ARMS:
        rs = by_arm.get(a, [])
        fails = [r for r in rs if not r.get("resolved")]
        ms = [r for r in fails if r.get("failure") == "missed_site"]
        rate = len(ms) / len(fails) if fails else 0
        print(f"{a:<10} failures={len(fails):>2}  missed_site={len(ms):>2}  rate={rate:.2f}")

    # arm-C revealed preference on edit tasks
    print("\n=== arm-C revealed tool preference (edit tasks) ===")
    for a in ["C_both", "D_forced"]:
        rs = by_arm.get(a, [])
        sem = sum(sum(1 for t in r.get("tool_calls", []) if t in SEM) for r in rs)
        grep = sum(sum(1 for t in r.get("tool_calls", []) if t in GREP) for r in rs)
        tot = sem + grep
        print(f"{a:<10} grep={grep:>4} semantic={sem:>4}  semantic_share={sem/tot if tot else 0:.0%}")

    # per-task resolved counts (which tasks are hard for everyone)
    print("\n=== per-task resolved (across all arms/rollouts) ===")
    bt = defaultdict(lambda: [0, 0])
    for r in recs:
        bt[r["instance"]][1] += 1
        if r.get("resolved"):
            bt[r["instance"]][0] += 1
    for inst, (res, tot) in sorted(bt.items()):
        print(f"  {inst:22s} {res}/{tot}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "runs/edit_opus_verified.jsonl")
