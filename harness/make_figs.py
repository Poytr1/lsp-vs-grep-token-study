#!/usr/bin/env python3
"""Generate paper figures from experiment data. Saves vector PDFs into figs/."""
import json, os
from collections import defaultdict, Counter
from statistics import mean
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
FIGS = os.path.join(HERE, "figs")
os.makedirs(FIGS, exist_ok=True)
plt.rcParams.update({"font.size": 11, "axes.grid": True,
                     "grid.alpha": 0.3, "figure.dpi": 120})

def load(p):
    path = os.path.join(HERE, "runs", p)
    return [json.loads(l) for l in open(path) if not json.loads(l).get("error")]

ARMS = ["A_grep", "B_lsp", "C_both", "D_forced"]
ARM_LBL = {"A_grep": "A\ngrep", "B_lsp": "B\nLSP", "C_both": "C\nboth", "D_forced": "D\nforced"}
COL = {"A_grep": "#4C72B0", "B_lsp": "#C44E52", "C_both": "#55A868", "D_forced": "#8172B3"}

def t2s(rows, arm):
    rs = [r for r in rows if r["arm"] == arm and r.get("success")]
    return mean([r["tokens_total"] for r in rs]) if rs else 0

def semfrac(rows, arm):
    rs = [r for r in rows if r["arm"] == arm]
    mix = Counter()
    for r in rs: mix.update(r.get("tool_calls", []))
    g = mix.get("search_text", 0)
    s = mix.get("document_symbols", 0)+mix.get("find_references", 0)+mix.get("find_definition", 0)
    return 100*s/(s+g) if (s+g) else 0

# ---------- Figure 1: localization T2S by arm (Opus) ----------
loc = load("v1_all_arms.jsonl")
fig, ax = plt.subplots(figsize=(5.4, 3.3))
vals = [t2s(loc, a) for a in ARMS]
bars = ax.bar([ARM_LBL[a] for a in ARMS], vals, color=[COL[a] for a in ARMS])
ax.axhline(vals[0], ls="--", color="gray", lw=1, alpha=0.7)
ax.set_ylabel("tokens-to-success")
ax.set_ylim(0, max(vals)*1.18)
ax.set_title("Localization (Opus 4.8): LSP costs more, not less", fontsize=10.5)
for b, v in zip(bars, vals):
    ax.text(b.get_x()+b.get_width()/2, v+8, f"{v:.0f}", ha="center", fontsize=9)
ax.text(0.02, 0.97, "free-choice arm C uses LSP 0% of the time",
        transform=ax.transAxes, fontsize=8, style="italic", va="top", color="#555")
fig.tight_layout(); fig.savefig(os.path.join(FIGS, "fig1_localization.pdf")); plt.close(fig)

# ---------- Figure 2: three-model LSP-vs-grep ratio + free-choice sem use ----------
models = [("Haiku 4.5", "sweep_haiku.jsonl"), ("Sonnet 4.6", "sweep_sonnet.jsonl"), ("Opus 4.8", "v1_all_arms.jsonl")]
ratios, semC = [], []
labels = []
for name, f in models:
    rows = load(f)
    a = t2s(rows, "A_grep"); b = t2s(rows, "B_lsp")
    ratios.append(100*(b-a)/a if a else 0)
    semC.append(semfrac(rows, "C_both"))
    labels.append(name)
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(8.2, 3.3))
cols = ["#C44E52" if r > 0 else "#55A868" for r in ratios]
b1 = ax1.bar(labels, ratios, color=cols)
ax1.axhline(0, color="black", lw=0.8)
ax1.set_ylabel("LSP token cost vs grep (%)")
ax1.set_title("LSP token premium by model")
for b, v in zip(b1, ratios):
    ax1.text(b.get_x()+b.get_width()/2, v+(4 if v >= 0 else -10), f"{v:+.0f}%", ha="center", fontsize=9)
ax1.text(0.5, 0.02, "below 0 = LSP saves tokens", transform=ax1.transAxes, ha="center",
         fontsize=8, style="italic", color="#555")
b2 = ax2.bar(labels, semC, color="#4C72B0")
ax2.set_ylabel("semantic-tool use in free-choice arm C (%)")
ax2.set_title("Do models use the LSP when free?")
ax2.set_ylim(0, max(10, max(semC)+3))
for b, v in zip(b2, semC):
    ax2.text(b.get_x()+b.get_width()/2, v+0.2, f"{v:.0f}%", ha="center", fontsize=9)
fig.tight_layout(); fig.savefig(os.path.join(FIGS, "fig2_models.pdf")); plt.close(fig)

# ---------- Figure 3: reference-completeness precision/recall/F1 ----------
ref = load("ref_opus.jsonl")
def metric(arm, key):
    rs = [r for r in ref if r["arm"] == arm]
    return mean([r[key] for r in rs]) if rs else 0
import numpy as np
x = np.arange(len(ARMS)); w = 0.25
fig, ax = plt.subplots(figsize=(6.2, 3.4))
prec = [metric(a, "precision") for a in ARMS]
rec = [metric(a, "recall") for a in ARMS]
f1 = [metric(a, "f1") for a in ARMS]
ax.bar(x-w, prec, w, label="precision", color="#C44E52")
ax.bar(x, rec, w, label="recall", color="#DD8452")
ax.bar(x+w, f1, w, label="F1", color="#4C72B0")
ax.set_xticks(x); ax.set_xticklabels([a.replace("_", "\n") for a in ARMS])
ax.set_ylabel("score"); ax.set_ylim(0, 1.08)
ax.set_title("Reference-completeness: LSP buys precision, not recall")
ax.legend(fontsize=8, loc="upper right", ncol=3)
ax.annotate("B=1.00\n(zero false +)", xy=(1-w, 1.0), xytext=(1-w, 0.55),
            fontsize=7.5, ha="center", color="#C44E52",
            arrowprops=dict(arrowstyle="->", color="#C44E52", lw=0.8))
ax.text(0.5, 0.02, "recall ~0.66 for ALL arms — a thoroughness ceiling no tool fixes",
        transform=ax.transAxes, ha="center", fontsize=7.5, style="italic", color="#555")
fig.tight_layout(); fig.savefig(os.path.join(FIGS, "fig3_reference.pdf")); plt.close(fig)

# ---------- Figure 4: why LSP costs more — calls + read_file counts ----------
def avg_calls(rows, arm):
    rs=[r for r in rows if r["arm"]==arm]; return mean([r["n_tool_calls"] for r in rs]) if rs else 0
def reads(rows, arm):
    rs=[r for r in rows if r["arm"]==arm]; m=Counter()
    for r in rs: m.update(r.get("tool_calls",[]))
    return m.get("read_file",0)
fig, (axa, axb) = plt.subplots(1, 2, figsize=(8.0, 3.2))
ac = [avg_calls(ref, "A_grep"), avg_calls(ref, "B_lsp")]
axa.bar(["A grep", "B LSP"], ac, color=["#4C72B0", "#C44E52"])
axa.set_ylabel("avg tool calls / episode")
axa.set_title("LSP needs more turns")
for i, v in enumerate(ac): axa.text(i, v+0.05, f"{v:.1f}", ha="center", fontsize=9)
rc = [reads(ref, "A_grep"), reads(ref, "B_lsp")]
axb.bar(["A grep", "B LSP"], rc, color=["#4C72B0", "#C44E52"])
axb.set_ylabel("total read_file calls (15 episodes)")
axb.set_title("LSP reads more files to verify refs")
for i, v in enumerate(rc): axb.text(i, v+0.5, f"{v}", ha="center", fontsize=9)
fig.suptitle("Why LSP costs +19% tokens on reference tasks: location-only results force follow-up reads",
             fontsize=9, y=1.02)
fig.tight_layout(); fig.savefig(os.path.join(FIGS, "fig4_why.pdf"), bbox_inches="tight"); plt.close(fig)

print("Wrote figures:")
for f in sorted(os.listdir(FIGS)):
    print("  figs/"+f)
