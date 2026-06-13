#!/usr/bin/env python3
"""Analyze the clean 4-arm v1 dataset (72 episodes)."""
import json, sys
from collections import defaultdict
from statistics import mean

PATH = sys.argv[1] if len(sys.argv) > 1 else "runs/v1_all_arms.jsonl"
rows = [json.loads(l) for l in open(PATH)]
ARM_ORDER = ["A_grep", "B_lsp", "C_both", "D_forced"]
LABEL = {"A_grep": "A grep-only", "B_lsp": "B lsp-only",
         "C_both": "C both(free)", "D_forced": "D forced-sem"}

by_arm = defaultdict(list)
for r in rows:
    by_arm[r["arm"]].append(r)

print("="*82)
print(f"CLEAN 4-ARM RESULTS  —  {len(rows)} episodes  ({PATH.split('/')[-1]})")
print("6 psf/requests localization tasks × 4 arms × 3 rollouts · model claude-opus-4.8")
print("="*82)
print(f"{'arm':14s} {'n':>3s} {'succ%':>6s} {'T2S(succ)':>10s} {'tok(all)':>9s} {'calls':>6s} {'turns':>6s} {'errs':>5s}")
for arm in ARM_ORDER:
    rs = by_arm.get(arm, [])
    if not rs: continue
    errs = [r for r in rs if r.get("error")]
    valid = [r for r in rs if not r.get("error")]
    succ = [r for r in valid if r["success"]]
    sr = 100*len(succ)/len(valid) if valid else float('nan')
    t2s = mean([r["tokens_total"] for r in succ]) if succ else float('nan')
    tall = mean([r["tokens_total"] for r in valid]) if valid else float('nan')
    calls = mean([r["n_tool_calls"] for r in valid]) if valid else float('nan')
    turns = mean([r["turns"] for r in valid]) if valid else float('nan')
    print(f"{LABEL[arm]:14s} {len(rs):3d} {sr:5.0f}% {t2s:10.0f} {tall:9.0f} {calls:6.1f} {turns:6.1f} {len(errs):5d}")

# Iso-accuracy: per-instance mean tokens among rollouts that SUCCEEDED, then compare arms
print()
print("="*82)
print("PER-INSTANCE mean tokens-to-success (successful rollouts only); '-' = none solved")
print("="*82)
by_inst = defaultdict(lambda: defaultdict(list))
for r in rows:
    if not r.get("error"):
        by_inst[r["instance"]][r["arm"]].append(r)
hdr = f"{'instance':22s}" + "".join(f"{LABEL[a].split()[0]:>9s}" for a in ARM_ORDER)
print(hdr)
arm_t2s = defaultdict(list)
for inst in sorted(by_inst):
    line = f"{inst:22s}"
    for a in ARM_ORDER:
        succ = [r["tokens_total"] for r in by_inst[inst][a] if r["success"]]
        if succ:
            v = mean(succ); arm_t2s[a].append(v); line += f"{v:9.0f}"
        else:
            line += f"{'-':>9s}"
    print(line)
print("-"*82)
line = f"{'MEAN T2S (per-inst)':22s}"
for a in ARM_ORDER:
    line += f"{mean(arm_t2s[a]):9.0f}" if arm_t2s[a] else f"{'-':>9s}"
print(line)

# Success counts per instance/arm
print()
print("="*82)
print("SUCCESS COUNT /3 per instance × arm")
print("="*82)
print(f"{'instance':22s}" + "".join(f"{LABEL[a].split()[0]:>9s}" for a in ARM_ORDER))
for inst in sorted(by_inst):
    line = f"{inst:22s}"
    for a in ARM_ORDER:
        rs = by_inst[inst][a]
        line += f"{sum(r['success'] for r in rs):>8d}/{len(rs) if rs else 3}"[-9:] if False else f"{sum(r['success'] for r in rs):>7d}/3"
    print(line)

# The key C-vs-D contrast: does forcing semantic-first change tokens / success?
print()
print("="*82)
print("KEY CONTRASTS")
print("="*82)
def arm_stats(a):
    valid = [r for r in by_arm[a] if not r.get("error")]
    succ = [r for r in valid if r["success"]]
    return (100*len(succ)/len(valid), mean([r["tokens_total"] for r in succ]) if succ else float('nan'))
for x, y in [("A_grep","B_lsp"), ("C_both","D_forced")]:
    sx, tx = arm_stats(x); sy, ty = arm_stats(y)
    print(f"  {LABEL[x]} vs {LABEL[y]}:")
    print(f"     success {sx:.0f}% -> {sy:.0f}%   |   T2S {tx:.0f} -> {ty:.0f}  ({100*(ty-tx)/tx:+.1f}% tokens)")

# Tool usage mix per arm (esp. how often the semantic tools are actually used)
print()
print("="*82)
print("TOOL-USE MIX per arm (total calls across all episodes)")
print("="*82)
from collections import Counter
for a in ARM_ORDER:
    mix = Counter()
    for r in by_arm[a]:
        mix.update(r.get("tool_calls", []))
    sem = mix.get("find_references",0)+mix.get("find_definition",0)+mix.get("document_symbols",0)
    lex = mix.get("search_text",0)
    print(f"  {LABEL[a]:14s} grep={lex:3d}  semantic(symbols/refs/defs)={sem:3d}  "
          f"[refs={mix.get('find_references',0)}, defs={mix.get('find_definition',0)}, "
          f"syms={mix.get('document_symbols',0)}]  reads={mix.get('read_file',0)}")
