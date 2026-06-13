# Does a Language Server Save Tokens for Coding Agents?

A measurement study of **LSP (semantic retrieval) vs. `grep` (lexical retrieval)** for LLM coding
agents, framed as an information-retrieval-under-a-token-budget problem. We ask a question that is
near-universally *asserted* but almost never *measured*: **does giving an agent a language server
actually save tokens?**

**Short answer (from this pilot): in general, no — and it's conditional.**

| Setting | Result |
|---|---|
| **Localization** (issue names the symbol) | LSP **costs** tokens, not saves: +6% (Opus 4.8), +118% (Sonnet 4.6) at equal success. |
| **Reference-completeness** (find all callers) | LSP buys **precision** (1.00 vs 0.76, zero false sites) but **not** token savings (+19% premium), and can't lift the ~0.66 recall ceiling. |
| **Model capability** | LSP **saves** tokens only for the weakest model (Haiku 4.5, −26%) — a crutch against grep noise. |
| **Revealed preference** | Given both tools free, **every** model defaults to `grep` (semantic-tool use 0% / 4% / 6%), forgoing the LSP even where it helps. |

The engineering takeaway is not "add an LSP" but **(a)** an adaptive router keyed on task class, model
capability, and lexical noise, and **(b)** training the *when-to-go-semantic* competence into the
policy — because handing the model the tool is demonstrably insufficient.

## The experiment

A minimal agent harness drives a Claude model through a tool-use loop on tasks built from
[SWE-bench-Lite](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite) `requests` instances.
**The only variable across arms is the retrieval tool surface**; the model, tasks, prompt, and loop are
held fixed. Tokens are counted from the API's reported usage.

**Arms:** `A` grep-only · `B` LSP-only · `C` both (agent chooses) · `D` forced-semantic-first.

**Primary metric — tokens-to-success (T2S):** total context tokens consumed to reach a *verified*
result. Token savings only count at **iso-accuracy**.

**Two task classes:**
- **Localization** — name the file(s) to change; verified against the gold patch (no Docker needed).
- **Reference-completeness** — list *every* call site of a function; scored by F1 against the
  reference set, with [`pyright`](https://github.com/microsoft/pyright) as the semantic ground-truth
  oracle (we switched from `pylsp`/jedi after finding it too incomplete — itself a finding about LSP
  quality variance).

## Repository layout

```
harness/
  harness.py        # agent loop + tool surfaces (grep / LSP via stdio JSON-RPC) + localization verifier
  ref_task.py       # reference-completeness experiment (find_references home turf), pyright oracle
  analyze4.py       # 4-arm analysis for localization runs
  analyze.py        # earlier A/B analysis
  make_figs.py      # generates the paper figures from runs/*.jsonl
  smoke_api.py      # smoke test: drive the model through the API
  smoke_lsp.py      # smoke test: drive a language server over stdio JSON-RPC
  runs/             # raw experiment results (JSONL) + run logs
  figs/             # generated figures (PDF)
paper/
  lsp-token-efficiency-paper.md    # full write-up (methodology + results)
  lsp-token-efficiency-paper.tex   # LaTeX version (compiles with `tectonic` or pdflatex)
  lsp-token-efficiency-paper.pdf   # compiled, 8 pages, 4 figures
  figs/                            # figures used by the .tex
```

## Raw data

`harness/runs/` holds the JSONL results, one object per agent episode:
- `v1_all_arms.jsonl` — localization, 4 arms × 6 tasks × 3 rollouts, Opus 4.8 (the clean run).
- `sweep_haiku.jsonl`, `sweep_sonnet.jsonl` — same, on Haiku 4.5 and Sonnet 4.6 (model sweep).
- `ref_opus.jsonl` — reference-completeness, 5 cross-file functions × 4 arms × 3 rollouts, Opus 4.8.
- `ab_results.jsonl` — an earlier A-vs-B localization run (superseded by `v1_all_arms.jsonl`).

Each localization row: `instance, arm, model, success, gold, submitted, tokens_total, n_tool_calls,
tool_calls, turns`. Each reference row adds `f1, precision, recall, exact_match, tp/fp/fn`.

## Reproducing

Requires Python 3.11+, Node 18+, and access to the Claude API (the harness reads `ANTHROPIC_AUTH_TOKEN`
/ `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL`).

```bash
pip install anthropic datasets matplotlib python-lsp-server
npm install typescript-language-server typescript pyright   # language servers

# clone the target repo the harness navigates (kept out of git on purpose)
mkdir -p harness/repos && git clone https://github.com/psf/requests harness/repos/requests

# localization, all 4 arms (per-instance git checkout of the SWE-bench base_commit)
python harness/harness.py --arms A_grep,B_lsp,C_both,D_forced --limit 6 --rollouts 3 \
    --model claude-opus-4-8 --out v1_all_arms.jsonl

# model sweep (run sequentially; both share the same repo checkout)
python harness/harness.py --arms A_grep,B_lsp,C_both,D_forced --limit 6 --rollouts 3 \
    --model claude-haiku-4-5 --out sweep_haiku.jsonl

# reference-completeness (pyright oracle)
python harness/ref_task.py --arms A_grep,B_lsp,C_both,D_forced --rollouts 3 \
    --model claude-opus-4-8 --out ref_opus.jsonl

python harness/analyze4.py harness/runs/v1_all_arms.jsonl   # analysis tables
python harness/make_figs.py                                 # regenerate figures
```

> Note: `harness/repos/` (the cloned `requests`) and `node_modules/` are intentionally gitignored.
> Any run that uses the repo does a `git checkout`, so do **not** run two experiments against the same
> checkout concurrently.

## Caveats

This is a **pilot**: one repository (`requests`, which the models have likely seen), small N, two task
classes (not the full edit/refactor space), Python with `pylsp`/`pyright` (the originally-motivating
**TypeScript** stratum is untested), one harness. The *directions* are robust; *effect sizes* are
pilot-scale. See the paper's Limitations section.

## The paper

`paper/lsp-token-efficiency-paper.pdf` (or `.md` / `.tex`) has the full methodology, the three-failure-
mode mapping (install / interface / churn), the mechanism analysis (including *why* the LSP costs more
on reference tasks — `find_references` returns locations only, forcing follow-up reads, while `grep`
returns the line inline), all results with figures, and honest limitations.
