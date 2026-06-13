#!/usr/bin/env python3
"""Analyze v0 results: success rate + tokens-to-success per arm, per instance."""
import json
from collections import defaultdict
from statistics import mean, median

rows = [json.loads(l) for l in open("runs/v0_results.jsonl")]

def fmt(xs): return f"{mean(xs):6.0f}" if xs else "   n/a"

print("="*78)
print("HEADLINE — per arm (across all 36 episodes)")
print("="*78)
by_arm = defaultdict(list)
for r in rows: by_arm[r["arm"]].append(r)
print(f"{'arm':10s} {'n':>3s} {'succ%':>6s} {'T2S(succ)':>10s} {'tok(all)':>9s} {'calls':>6s} {'turns':>6s}")
for arm in sorted(by_arm):
    rs = by_arm[arm]
    succ = [r for r in rs if r["success"]]
    sr = 100*len(succ)/len(rs)
    t2s = mean([r["tokens_total"] for r in succ]) if succ else float('nan')
    tall = mean([r["tokens_total"] for r in rs])
    calls = mean([r["n_tool_calls"] for r in rs])
    turns = mean([r["turns"] for r in rs])
    print(f"{arm:10s} {len(rs):3d} {sr:5.0f}% {t2s:10.0f} {tall:9.0f} {calls:6.1f} {turns:6.1f}")

print()
print("="*78)
print("PER-INSTANCE — mean tokens (A vs B), success counts /3")
print("="*78)
by_inst = defaultdict(dict)
for r in rows:
    by_inst[r["instance"]].setdefault(r["arm"], []).append(r)
print(f"{'instance':22s} {'A succ':>6s} {'A tok':>7s} | {'B succ':>6s} {'B tok':>7s} | {'Δtok(B-A)':>9s}")
for inst in sorted(by_inst):
    a = by_inst[inst].get("A_grep", [])
    b = by_inst[inst].get("B_lsp", [])
    a_sr = sum(r["success"] for r in a); b_sr = sum(r["success"] for r in b)
    a_tok = mean([r["tokens_total"] for r in a]); b_tok = mean([r["tokens_total"] for r in b])
    print(f"{inst:22s} {a_sr:4d}/3 {a_tok:7.0f} | {b_sr:4d}/3 {b_tok:7.0f} | {b_tok-a_tok:+9.0f}")

print()
print("="*78)
print("SUCCESS-ONLY tokens-to-success (iso-accuracy view; only instances both arms solved)")
print("="*78)
both_solved = []
for inst in sorted(by_inst):
    a = [r for r in by_inst[inst].get("A_grep", []) if r["success"]]
    b = [r for r in by_inst[inst].get("B_lsp", []) if r["success"]]
    if a and b:
        both_solved.append((inst, mean([r["tokens_total"] for r in a]), mean([r["tokens_total"] for r in b])))
if both_solved:
    aA = mean([x[1] for x in both_solved]); aB = mean([x[2] for x in both_solved])
    print(f"instances solved by BOTH: {len(both_solved)}")
    print(f"  mean T2S  A_grep = {aA:.0f}   B_lsp = {aB:.0f}   delta = {aB-aA:+.0f} ({100*(aB-aA)/aA:+.1f}%)")
else:
    print("  (no instance solved by both arms)")
