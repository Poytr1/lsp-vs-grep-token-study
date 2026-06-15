#!/usr/bin/env python3
"""
Analyze the MULTI-FILE rename study (verified episodes). The headline question
is the one the single-file edit tasks could not answer:

  Does the LSP help the agent find and update EVERY call site on a multi-file
  edit? i.e. does it lift site_recall and reduce missed_site failures?

Metrics per arm:
  - pass@1 (resolved rate)
  - site_recall = mean over episodes of (#gold files fully converted / #gold files)
    >>> THE prediction test: if the LSP can't lift recall, missed_site persists.
  - missed_site rate (share of episodes that failed by leaving a dangling site)
  - tokens-to-success and turns
  - arm-C / D revealed semantic-tool share on these reference-heavy edits
"""
import json, sys
from collections import defaultdict

ARMS = ["A_grep", "B_lsp", "C_both", "D_forced"]
SEM = {"find_references", "find_definition", "document_symbols"}
GREP = {"search_text"}


def main(path):
    recs = [json.loads(l) for l in open(path)]
    by_arm = defaultdict(list)
    for r in recs:
        by_arm[r["arm"]].append(r)

    ntasks = len(set(r["instance"] for r in recs))
    print(f"=== MULTI-FILE RENAME study: {len(recs)} episodes, {ntasks} tasks ===\n")
    print(f"{'arm':<10}{'N':>4}{'pass@1':>8}{'site_recall':>13}{'missed_site':>13}{'tok(res)':>10}{'turns':>7}")
    for a in ARMS:
        rs = by_arm.get(a, [])
        if not rs:
            continue
        n = len(rs)
        res = [r for r in rs if r.get("resolved")]
        recall = sum(r.get("site_recall", 0) for r in rs) / n
        ms = sum(1 for r in rs if r.get("failure") == "missed_site")
        tok = sum(r["tokens_total"] for r in res) / len(res) if res else 0
        turns = sum(r.get("turns", 0) for r in rs) / n
        print(f"{a:<10}{n:>4}{len(res)/n:>8.2f}{recall:>13.3f}{ms/n:>13.2f}{tok:>10.0f}{turns:>7.1f}")

    print("\n=== failure-mode breakdown per arm (counts) ===")
    modes = ["resolved", "missed_site", "broke_import", "no_edit", "patch_conflict", "other"]
    print(f"{'arm':<10}" + "".join(f"{m:>15}" for m in modes))
    for a in ARMS:
        rs = by_arm.get(a, [])
        if not rs:
            continue
        c = defaultdict(int)
        for r in rs:
            c[r.get("failure", "?")] += 1
        print(f"{a:<10}" + "".join(f"{c.get(m,0):>15}" for m in modes))

    print("\n=== arm-C / D revealed tool preference (multi-file edits) ===")
    for a in ["C_both", "D_forced"]:
        rs = by_arm.get(a, [])
        sem = sum(sum(1 for t in r.get("tool_calls", []) if t in SEM) for r in rs)
        grep = sum(sum(1 for t in r.get("tool_calls", []) if t in GREP) for r in rs)
        tot = sem + grep
        print(f"{a:<10} grep={grep:>4} semantic={sem:>4}  semantic_share={sem/tot if tot else 0:.0%}")

    # recall conditioned on outcome — do failures really come from low recall?
    print("\n=== site_recall split by resolved vs failed (all arms pooled) ===")
    res = [r.get("site_recall", 0) for r in recs if r.get("resolved")]
    fail = [r.get("site_recall", 0) for r in recs if not r.get("resolved")]
    mean = lambda xs: sum(xs) / len(xs) if xs else 0
    print(f"  resolved episodes: mean recall {mean(res):.3f} (n={len(res)})")
    print(f"  failed episodes:   mean recall {mean(fail):.3f} (n={len(fail)})")

    print("\n=== per-task resolved + mean recall (which renames are hard) ===")
    bt = defaultdict(lambda: [0, 0, []])
    for r in recs:
        bt[r["instance"]][1] += 1
        bt[r["instance"]][2].append(r.get("site_recall", 0))
        if r.get("resolved"):
            bt[r["instance"]][0] += 1
    for inst, (res_n, tot, recalls) in sorted(bt.items()):
        print(f"  {inst:42s} {res_n}/{tot}  meanRecall={sum(recalls)/len(recalls):.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "runs/rename_opus_verified.jsonl")
