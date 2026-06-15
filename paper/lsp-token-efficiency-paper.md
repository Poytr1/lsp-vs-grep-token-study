# Does a Language Server Save Tokens for Coding Agents? A Measurement Methodology

**A design and methodology paper**

*Author persona: Ilya Sutskever (agent). Prepared for @pchsu.*
*Date: 2026-06-13. Primary language under study: TypeScript (`tsserver` / `typescript-language-server`).*

---

## Abstract

Coding agents spend most of their context budget on *retrieval*: deciding which tokens of a
repository must enter the model's context so that it can build a faithful internal model of the
task. Two retrieval regimes dominate in practice. **Lexical retrieval** (`grep`, `ripgrep`, `find`)
is universal, instant, and zero-setup, but noisy: it cannot distinguish a definition from a call
from a mention in a comment. **Semantic retrieval** via the Language Server Protocol (LSP) —
`textDocument/references`, `definition`, `hover`, `documentSymbol` — is precise and typed, but
requires a running, indexed server and pays a per-symbol round-trip cost. The widely repeated claim
is that semantic retrieval is "more token-efficient." We surveyed the tools and literature that make
this claim and found a striking gap: **the claim is asserted almost everywhere and measured almost
nowhere.** No public source isolates and reports the LSP-vs-lexical token delta for an agent at
equal task-success rate. This paper does four things. (1) It formalizes the question as an
information-retrieval problem with a single primary metric, **tokens-to-success**. (2) It specifies
a five-arm ablation that isolates the contribution of semantic retrieval from confounds. (3) It maps
three concrete, pre-stated failure modes — installation cost, interface gaps, and index churn —
onto directly measurable variables, so that a negative result is as informative as a positive one.
(4) It reports a **pilot implementation** (Python / `requests`, Claude Opus 4.8 / Sonnet 4.6 /
Haiku 4.5) that turns the prior into first numbers. The pilot finds the answer is conditional and,
in the common case, **negative**: on symbol-named *localization* the LSP *costs* tokens (+6% on Opus,
+118% on Sonnet) and the agent ignores it entirely when free; on *reference-completeness* the LSP buys
**precision** (1.00 vs 0.76, zero false call sites) but not token savings (a ~19% premium) and cannot
raise the recall ceiling set by agent thoroughness; and it nets a token **saving only for the weakest
model** (Haiku, −26%), as a crutch against lexical noise. The most robust finding is that the agent's
tool choice is **task-dependent**: it defaults to `grep` on localization (semantic-tool use 0–6%) but
reaches for the LSP about half the time on reference tasks (45–57%), unprompted — the grep preference
is not a fixed bias but a learned, task-shaped policy. On *editing* tasks scored by real test execution
the picture is starker still: grep solves multi-file renames perfectly while a location-only LSP fails
three-quarters of them by missing a call site, and even a complete, index-warmed, text-enriched LSP
(returning each reference's line inline, as production LSP-MCP servers do) recovers most of that gap —
cutting follow-up file-reads ~5× — but cannot close it, because a rename must touch comments and strings
that semantic references structurally exclude. This argues not for "LSP-always" but for an
*adaptive router* keyed on task class, model capability, and lexical noise — and, since that routing
competence is demonstrably already present in latent form, for reinforcing it into the policy rather
than bolting on the tool.

---

## 1. The fundamental question

If you think about it deeply, an agent navigating a codebase is not "using tools" in any essential
sense — it is performing **retrieval under a token budget**. The model has a finite context window.
The task ("rename this symbol and fix all callers", "why does this request 500", "add a field to
this type") is solvable only if the *right* tokens enter that window and the *wrong* tokens stay
out. Every tool call is a retrieval decision with a price, denominated in tokens.

From this lens, the two regimes are two points on a precision/recall/cost surface:

- **Lexical retrieval** (`grep`) has high recall and low precision. It finds every textual match,
  including matches in comments, strings, unrelated identifiers, vendored code, and tests. The agent
  pays to read noise, and frequently pays again to read surrounding lines (`-A/-B/-C`) to decide
  whether a match is real. Zero setup; works in any repo, any language, instantly.
- **Semantic retrieval** (LSP) has high precision and (for the queries it supports) high recall.
  "Find references" returns *exactly* the resolved references — not text that looks like the symbol.
  The cost moves elsewhere: a server must be installed and running, the project must be indexed, and
  each query is a JSON-RPC round-trip whose latency grows with repository size.

So the question "can an LSP save tokens?" has a precise form:

> **At equal task-success rate, how many fewer tokens does semantic retrieval place into the agent's
> context than lexical retrieval, and under what conditions does that delta become negative?**

The phrase *at equal task-success rate* is not decoration. A method that "saves tokens" by failing
the task earlier has saved nothing. Token savings are only meaningful **conditioned on iso-accuracy**.

---

## 2. Background and related work

### 2.1 Provenance note

Live web *search* tooling was degraded during this study (it returned model-generated synthesis
rather than ranked results). Two retrieval paths *did* return verifiable primary sources, and every
claim below is grounded in one of them:

- **(F) Fetch-verified this session** — retrieved directly and quoted: the OSS tools below, and four
  arXiv papers retrieved via the arXiv API (`https://export.arxiv.org/api/query`, full abstracts
  captured).
- **(K) Established background** from the author's training knowledge, flagged explicitly where used
  and *not* relied upon for any novel quantitative claim.

This separation is deliberate: the paper's central claim is a claim about *absence of measurement*,
so it must not itself rest on unverifiable measurement.

### 2.2 Tools that already expose semantic navigation to agents (F)

- **Serena** (`github.com/oraios/serena`). An abstraction layer over LSP exposing `find_symbol`,
  `find_referencing_symbols`, a symbol overview, and `find_implementations`, plus *symbolic editing*
  (`replace_symbol_body`, `insert_after/before_symbol`) across 40+ languages. It states that
  symbolic editing is "less error-prone and **much more token-efficient** than typical alternatives"
  and that semantic retrieval lets an agent explore "without reading entire files." This is an
  explicit token-efficiency claim — **with no accompanying benchmark numbers.**
- **mcp-language-server** (`github.com/isaacphi/mcp-language-server`). An MCP server wrapping a real
  language server, exposing `definition`, `references`, `diagnostics`, `hover`, `rename_symbol`, and
  `edit_file`. It describes its edit tool as "more reliable and **context-economical**" than
  search/replace. The efficiency claim is **scoped to editing; there is no general token comparison.**
- **Aider repo-map** (`aider.chat/docs/repomap.html`, `/2023/10/22/repomap.html`). A *static* cousin
  of LSP: tree-sitter extracts signatures (not bodies), and a graph/PageRank-style ranking keeps the
  most-referenced symbols within a token budget (`--map-tokens`, default ~1k tokens). The stated
  motivation is exactly our thesis — "sending whole files is a bulky way to send code context,
  wasting the precious context window" — but the writeup reports **no before/after token or accuracy
  numbers.**

### 2.3 Recent literature (F — arXiv, full abstracts captured)

- **TypeScript Repository Indexing for Code Agent Retrieval** (arXiv:2604.18413, 2026-04). The most
  directly relevant work, and in our chosen language. It builds a function-level semantic index
  (UniAST) and observes that *"LSP-based resolution requires a JSON-RPC call for each symbol lookup,
  [so] these per-symbol calls become a bottleneck on large TypeScript repositories,"* motivating a
  parser built directly on the TypeScript Compiler API, evaluated on projects up to 1.2M LOC.
  **Crucially, this measures the efficiency of *index construction*, not an agent's tokens-to-success
  with versus without semantic navigation.** It is strong evidence for the *cost side* of LSP
  (per-symbol latency) and for preferring batched/compiler-API indexing over naive per-call LSP — but
  it is not the agent-side token measurement we seek.
- **Reinforcement Learning from Compiler and Language Server Feedback** (arXiv:2510.22907, 2025-10).
  Argues that compilers, type checkers, and language servers "already compute the missing supervision
  signal ... but expose it through interfaces designed for human-driven IDEs rather than learning
  loops," and turns LSP signal (diagnostics, symbol resolution, references, refactoring
  preconditions) into a shaped process reward via a CLI-first orchestration layer. This is the
  strongest articulation of the *interface-friction* hypothesis (our H-interface), and points at the
  end-state: make the signal native to the policy rather than an external call.
- **CORE-Bench: Code Retrieval in the Era of Agentic Coding** (arXiv:2606.11864, 2026-06). A
  benchmark for *requirement-driven repository search* — code understanding, issue-to-edit
  localization, and broader-context retrieval — with 180K+ queries built on SWE-bench-series
  instances, reporting "a sharp drop from traditional code search to code retrieval in agentic coding
  settings." This is a candidate evaluation substrate for our reference-heavy tasks and confirms that
  snippet-matching is not the same problem as agentic repository navigation.
- **Code as Agent Harness** (arXiv:2605.18747, 2026-05, survey). Frames code and tooling as the
  agent's operational substrate and explicitly lists "evaluation beyond final task success" as an
  open challenge — direct support for using tokens-to-success rather than pass@1 alone.

Secondary, relevance-ranked but not fully read here (F, titles/short abstracts): Prometheus
(arXiv:2507.19942, long-horizon codebase navigation), RepoMaster (arXiv:2505.21577, autonomous repo
exploration), SWE-Adept (arXiv:2603.01327, deep codebase analysis), EVOR (arXiv:2402.12317, evolving
retrieval for code generation), DeepCodeSeek (arXiv:2509.25716, real-time API retrieval), TeaRAG
(arXiv:2511.05385, token-efficient agentic RAG), RepoAudit (arXiv:2501.18160).

Established background (K): SWE-bench (Jimenez et al., 2023; ~2294 task instances, 500 in the
Verified split) and the SWE-agent agent-computer-interface line of work (Yang et al., 2024) provide
the verified-task harness our protocol reuses; AutoCodeRover and Moatless are agent baselines that
combine lexical and structural navigation.

### 2.4 The gap

Across tools and papers, the pattern is consistent: **the token-efficiency of semantic retrieval is
asserted, not measured.** The closest quantitative work (arXiv:2604.18413) measures index-build
efficiency, not agent tokens-to-success. We found no public source that isolates the LSP-vs-lexical
token delta for an agent at iso-accuracy and reports it. That gap is the justification for this study:
**the deliverable the field needs here is a measurement, not another assertion.**

---

## 3. Method

### 3.1 Primary metric: tokens-to-success

Let a *task* be a repository state plus a verifiable goal (tests pass, or the patch matches a known-
correct edit). For agent configuration $c$ on task $t$, run $R$ independent rollouts. Define:

$$
\text{success}(c,t) = \frac{1}{R}\sum_{r=1}^{R} \mathbb{1}[\text{rollout } r \text{ verified correct}]
$$

$$
\text{T2S}(c,t) = \frac{\sum_{r:\,\text{verified}} \text{tokens}(c,t,r)}{\#\{r : \text{verified}\}}
$$

where $\text{tokens}(c,t,r)$ is the **total context tokens consumed** by rollout $r$ — prompt +
tool-result + generated tokens summed over every turn (this is what the user actually pays for).
T2S is the mean token cost of a *successful* solve; it is undefined when success is zero, which is
itself a reportable outcome. The headline comparison is always a pair: **(success rate, T2S)**. We
never report a token number without its success rate beside it.

Secondary metrics, all logged per rollout:

- **tool-call count**, split by tool (`grep` vs `references` vs file-read vs edit).
- **read volume**: bytes/tokens pulled into context by file reads, and the fraction later unused.
- **turns to first correct edit** and **total wall-clock**.
- **grep false-positive rate**: of lexical matches the agent retrieved, the fraction that are *not*
  semantically relevant (matches in comments, strings, unrelated identifiers). This is estimated
  against an LSP/compiler oracle on the same query and is the theoretical headroom for semantic
  retrieval — the noise that precision could, in principle, remove.

### 3.2 The ablation: five arms

All arms share **the same model, the same tasks, the same harness, and the same prompt scaffold.**
The *only* variable is the retrieval tool surface. This is what isolates the effect.

| Arm | Tool surface | Purpose |
|-----|--------------|---------|
| **A — Lexical only** | `grep`/`ripgrep`/`find` + file read + edit. No LSP. | Baseline; status quo for most agents. |
| **B — Semantic only** | LSP (`references`, `definition`, `hover`, `documentSymbol`) + file read + edit. No `grep`. | Upper bound of semantic retrieval in isolation; exposes where semantic *cannot* answer. |
| **C — Both, agent chooses** | A ∪ B; the policy decides. | Realistic mixed setting; reveals the agent's *revealed preference* between tools. |
| **D — Both, semantic-first routing** | A ∪ B, but prompt/tool-routing nudges a semantic attempt before falling back to lexical. | Tests whether the *gap is capability or habit* — i.e., does forcing semantic-first help, given C's free choice? |
| **E — Static repo-map** | Aider-style precomputed signature map within a token budget, + `grep` + read + edit. No live LSP. | Tests whether a cheap *static* index captures most of the benefit without a running server. |

The A–B–C triangle answers "does semantic retrieval help when available, and does the agent use it
when free to?" D vs C isolates **policy habit from tool capability** (see §5). E vs B isolates **how
much of the benefit needs a *live* server** versus a one-time static extraction — directly relevant
to deployment cost.

### 3.3 Tasks

We select tasks where reference-finding is on the critical path, because that is where semantic
retrieval has theoretical leverage. Categories:

1. **Change-a-signature, fix-all-callers** — add/remove/retype a parameter; success = type-checks +
   tests pass. The canonical "find references" task.
2. **"Who calls X / who implements I"** — localize all call sites or implementors; success = matches
   an oracle set.
3. **Cross-file refactor** — extract/move a symbol and update imports and usages.
4. **Dead-code identification** — find truly unreferenced exports (a lexical search over-reports;
   strings/dynamic dispatch under-report).
5. **Issue-to-edit localization** — given a bug report, find the file/function to change
   (SWE-bench-series / CORE-Bench style).

Tasks are drawn from or modeled on **SWE-bench-series and CORE-Bench** instances so that success is
objectively checkable by execution or against curated relevance labels, not by judgment.

**Stratification.** Results are reported *per stratum*, never only pooled:

- **Language strength**: strong-LSP (TypeScript — our primary; Python as comparison) vs
  weaker/heterogeneous-LSP settings. We expect the effect to be strongly language-dependent.
- **Repository size**: small / medium / large (large repos amplify both lexical noise *and* LSP
  per-symbol round-trip cost — opposing forces whose balance is empirical).
- **Task category** as above.

### 3.4 Controls and confounds

- **Fixed model and decoding.** Same checkpoint, same temperature, same max-context. The model is
  held constant so the measured delta is attributable to retrieval, not capability drift.
- **Identical scaffold.** Tool *descriptions* are matched in length and specificity across arms; an
  under-described LSP tool would otherwise be under-used for reasons unrelated to its utility (this
  is itself a measurable affordance effect — see §5 — but it must not silently contaminate A vs B).
- **Warm vs cold index.** LSP arms are measured both warm (index built before the timer starts) and
  cold (index build counted), and reported separately. Conflating them is the most common way to
  flatter or punish LSP unfairly.
- **Determinism and replay.** Every rollout logs the full tool-call transcript with per-call token
  cost, enabling exact attribution (§3.5) and re-analysis without re-running. (The RLCSF/Lanser
  line, arXiv:2510.22907, makes the same point about replayable LSP sessions.)
- **Sample size.** $R$ rollouts per (arm, task) with bootstrap confidence intervals on both success
  rate and T2S; we report intervals, not point estimates, and we **log any truncation or cap** (e.g.
  a turn limit that aborts a rollout) rather than silently dropping it.

### 3.5 Attribution: *where* do savings (or losses) come from?

A single T2S delta is not an explanation. From the per-call logs we decompose any difference into
three additive sources:

1. **Fewer reads** — semantic retrieval points directly at the right file/function, so the agent
   opens fewer files. (Measured: read count and read-token volume.)
2. **Less noise per read** — when it does read, it reads relevant code, not lexical false positives.
   (Measured: grep false-positive rate × tokens spent on false positives.)
3. **Fewer turns** — precise results converge in fewer agent steps. (Measured: turns-to-success.)

And we account for the **LSP cost side** symmetrically: per-symbol round-trip latency and the tokens
spent on verbose LSP payloads (ranges, URIs, kinds). The TS-indexing result (arXiv:2604.18413) tells
us this cost is real and size-dependent; our protocol *measures* it rather than assuming it away.

---

## 4. The three failure modes, made measurable

@pchsu pre-stated three hypotheses for *why* an LSP might fail to help in practice. Each is mapped to
a concrete measurement and a candidate remedy that the protocol can evaluate.

### H-install — "the LSP is too hard to install/initialize"
- **Measure**: per-language setup success rate and cold-start cost (time + tokens) to a queryable
  index, across the target languages and repo sizes. Failures to initialize are recorded as such.
- **Remedy arm**: *index-on-build* — produce the semantic index as a by-product of the project's
  existing build/typecheck (for TypeScript, via the Compiler API rather than a live `tsserver`
  session, following arXiv:2604.18413), so the agent inherits a warm index with no separate install
  step. Compare its warm-start cost and success to the live-LSP arm.

### H-interface — "the LSP API is too limited; key paths aren't available"
- **Measure**: log every **LSP-insufficiency event** — a point where the agent issued a semantic
  query, got an inadequate answer, and fell back to `grep`. Categorize the fallbacks (dynamic
  dispatch, string/registry indirection, config/build-time wiring, framework magic). The *taxonomy of
  fallbacks* is a primary research output, not a footnote.
- **Remedy direction**: each recurring fallback category is a candidate **API extension** (e.g. a
  "conceptual usages" query spanning dynamic dispatch and string keys). The data tells us which
  extensions would actually retire fallbacks. This connects to arXiv:2510.22907's thesis that the
  IDE-shaped interface, not the underlying signal, is the bottleneck.

### H-churn — "edits invalidate the index; re-indexing is costly"
- **Measure**: re-index latency after an edit, and **"tokens wasted re-reading after edit"** — context
  the agent re-pulls because stale results forced a re-check. Tracked across the edit-heavy task
  categories (refactor, change-signature).
- **Remedy arm**: *partial-index + grep hybrid* — incrementally re-index only the touched
  module/dependency cone and fall back to lexical search for the rest until the index catches up.
  Compare its post-edit token cost and correctness to full re-index and to lexical-only.

A clean negative on any remedy is a real finding. If "index-on-build" does not beat live-LSP, or if
the partial-index hybrid does not beat lexical-only after edits, that tells @pchsu where *not* to
invest engineering — which is exactly the value of running the ablation.

---

## 5. Why agents prefer `grep` — mechanism hypotheses

A recurring observation (and @pchsu's own) is that, given the free choice in arm C, agents reach for
`grep` and rarely issue LSP queries. *Why* matters, because the remedy differs by cause. Ranked by my
prior:

1. **Training prior (strongest).** Pretraining and RLHF corpora are saturated with humans running
   `grep`/`find` in terminals and almost devoid of programmatic LSP JSON-RPC. The policy has a high
   prior on the high-frequency tool. If this dominates, the fix is *training* (demonstrations / RL
   that reward semantic-first navigation), not prompting — which is the arXiv:2510.22907 direction
   and, in my view, the correct end-state: the competence should live in the weights, not in a
   fragile wrapper.
2. **Tool affordance.** Harnesses expose `Bash(grep)` universally and with terse, familiar
   descriptions; LSP tools, when present, often carry narrower descriptions and higher perceived
   activation energy. Testable by varying tool descriptions (held matched in the main ablation, varied
   deliberately in a side experiment).
3. **Universality.** `grep` works in any repo and language with zero setup; LSP needs the right
   server up and indexed (this is H-install showing up as a *policy* preference, not just a cost).
4. **Latency/feedback.** `grep` returns instantly; a cold or churning index stalls (H-churn at the
   moment of the edit). Agents may have learned to avoid tools with variable latency.
5. **Interface gaps.** No single LSP call answers "where is this *conceptually* used" across dynamic
   dispatch, string keys, and configuration; the agent learns that `grep` is the more general
   instrument (H-interface).

**The C-vs-D contrast is the decisive experiment for this section.** If forcing semantic-first (D)
improves T2S over free choice (C), the agent was *under-using* a capability it had — a habit problem,
addressable by training or routing. If D fails to beat C, semantic retrieval genuinely was not the
better instrument for those tasks — a capability/coverage problem (H-interface), addressable only by
extending what semantic retrieval can answer.

---

## 6. Predictions (stated priors)

I commit to these before any data, so the result can falsify them:

1. **Reference-heavy tasks on strong-LSP languages (TypeScript, Python): semantic retrieval yields a
   real T2S reduction at iso-accuracy.** Mechanism: lexical false-positive rate is high on these
   tasks, and precision removes that noise from context. Expected effect: material, not marginal.
2. **Simple lexical lookups and weak/heterogeneous-LSP settings: the delta is negligible or
   negative.** When the answer is a literal string or the server can't resolve the construct, LSP's
   setup and per-call cost is pure overhead.
3. **In arm C, agents under-use LSP relative to its measured benefit in B/D** — i.e., there is a
   habit gap, dominated by the training prior. Forcing semantic-first (D) recovers part of B's
   advantage.
4. **Large repositories shift the balance toward semantic retrieval on the *noise* axis but against
   it on the *latency* axis** (per arXiv:2604.18413's per-symbol bottleneck); the net is empirical
   and likely favors *batched/static* indexing (arm E) over naive live per-call LSP at scale.

If these hold, the engineering conclusion is **not** "give every agent an LSP." It is **build an
adaptive router** — lexical by default, semantic when the task is reference-heavy and the language/
repo supports it — and, ultimately, *train* that routing competence into the policy so it is native
rather than scaffolded.

---

## 6.5. Results (pilot implementation)

We implemented the harness and ran a pilot on **Python / `requests`** with two retrieval servers
(`python-lsp-server` and, for the reference experiment, `pyright`), driving Claude models through a
tool-use agent loop that logs token usage per turn. The pilot is small (one repository) and is meant
to validate the methodology and surface first signals, not to settle the question at scale; §7 states
the caveats. All raw results are in the artifact bundle (`lsp-harness/runs/`).

### 6.5.1 Localization tasks — *grep wins on tokens*

Six SWE-bench-Lite `requests` issues, task = name the file(s) to change, verified against the gold
patch. Four arms × 3 rollouts, Opus 4.8. Tokens-to-success (T2S) among successful rollouts:

| Arm | success | T2S | free-choice semantic-tool use |
|-----|--------:|----:|------------------------------:|
| A grep-only | 100% | 920 | 53 grep / 0 semantic |
| B lsp-only | 100% | **971 (+6%)** | — |
| C grep+lsp (free) | 100% | 919 | 50 grep / **0 semantic** |
| D forced-semantic | 89% | 945 | 51 grep / 4 semantic |

On localization where the issue text typically *names* the symbol, **semantic retrieval costs more
tokens, not fewer** (B vs A, +6% at identical success). Decisively, in the free-choice arm C the
agent used the LSP **zero times** — C collapses onto A. Forcing semantic-first (D) *reduced* success
(100%→89%) and the agent still resisted (4 semantic vs 51 grep calls). **Prediction 2 confirmed;
the localization half of Prediction 1 refuted** (for symbol-named localization, semantic retrieval is
a tax). **Prediction 3 confirmed and then some** — the habit gap is so strong that forcing backfires.

### 6.5.2 Model sweep — *LSP helps weak models, taxes strong ones*

Same localization tasks, three models, 72 episodes each:

| Model | grep T2S | lsp T2S | lsp vs grep | free-choice semantic use |
|-------|---------:|--------:|:-----------:|-------------------------:|
| Opus 4.8 | 920 | 971 | **+6%** | 0% |
| Sonnet 4.6 | 606 | 1319 | **+118%** | 4% |
| Haiku 4.5 | 11,911 | 8,799 | **−26%** | 6% |

The capability dependence is the headline. The **weakest** model (Haiku — which flails with grep
noise, ~12k tokens and 13+ turns per task) is the *only* one that **saves** tokens with the LSP
(−26%); both capable models pay a premium (Opus +6%, Sonnet +118%). **Semantic retrieval is a crutch
for weak models and a tax for strong ones.** And the grep preference is **universal**: free-choice
semantic-tool use is 0% / 4% / 6% across the three — even Haiku, which would save 26%, uses the LSP
only 6% of the time when free. Giving the tool is not enough; no model reaches for it.

### 6.5.3 Reference-completeness — *LSP buys precision, not tokens, and cannot fix recall*

Task = list **every** call site of a target function; scored by F1 against `pyright`'s reference set
(the semantic gold standard; we switched the oracle from `pylsp` to `pyright` after finding jedi's
reference resolution too incomplete to serve as ground truth — itself a finding about LSP quality
variance). Five cross-file `requests` functions × 4 arms × 3 rollouts, Opus 4.8:

| Arm | mean F1 | precision | recall | tokens |
|-----|--------:|----------:|-------:|-------:|
| A grep-only | 0.706 | 0.76 | 0.67 | 1136 |
| B lsp-only | **0.778** | **1.00** | 0.66 | 1347 (+19%) |
| C grep+lsp (free) | 0.706 | 0.76 | 0.67 | 1354 |
| D forced-semantic | 0.710 | 0.77 | 0.67 | 1499 |

Here — on `find_references`' home turf — **the LSP wins on accuracy** (F1 0.778 vs 0.706), the
*opposite* of localization: **the task class decides whether semantic retrieval helps.** The
decomposition is the real result:

- The entire gain is **precision**: B reports **zero false call sites** (precision 1.00) vs grep's
  0.76 (~24% of grep's hits are false positives — comments, strings, unrelated same-named symbols).
  This is the noise-filtering thesis made quantitative.
- **Recall is identical (~0.66) across all four arms.** Neither tool helps the agent find *more* true
  sites; the missing third is an **agent-thoroughness** problem (it does not exhaustively chase every
  reference), not a retrieval problem. The LSP cannot fix recall because that bottleneck is the
  agent's *strategy*, not retrieval precision.
- The precision gain costs **~19% more tokens**, and per-target it appears on **every cross-file
  function** (+0.06 to +0.13 F1) but **vanishes on the one single-file target** (both arms F1 = 1.00).
- Free-choice arm C reverts to grep's profile (precision 0.76) — the agent **forgoes the precision
  gain** even where it exists (but see §6.5.3.1: on reference tasks it uses the LSP far more than on
  localization).

So the reference half of **Prediction 1 is confirmed but reframed**: semantic retrieval's benefit on
reference-heavy tasks is real but is **precision/correctness, not token reduction** — and it is gated
by agent thoroughness on the recall side. Prediction 4 (repo-scale / static-index, arm E) remains
untested in this pilot.

### 6.5.3.1 Across models, and task-dependent tool choice

We ran the reference experiment on all three models (60 episodes each):

| Model | A grep F1 | B lsp F1 | ΔF1 | B vs A tokens |
|-------|----------:|---------:|----:|--------------:|
| Opus 4.8 | 0.706 | 0.778 | +0.072 | +19% |
| Sonnet 4.6 | 0.706 | 0.789 | +0.083 | +12% |
| Haiku 4.5 | 0.706 | 0.719 | +0.013 | **−7%** |

First, the accuracy benefit is **model-independent**: the LSP lifts F1 for all three, with precision
→ 1.00 for the two capable models and 0.93 for Haiku. Second, the *token* premium is milder than
localization — it shrinks sharply relative to the localization tax (Sonnet +118% on localization →
+12% here; Haiku −26% → −7%) but stays slightly positive for the strong models. Unlike localization,
here the premium **buys real F1**; a clean token saving appears only for the weakest model (Haiku, −7%).

The most important refinement: **tool choice is task-dependent.** The localization data suggested a
*universal* grep preference (free-choice semantic-tool use 0% / 4% / 6%). The reference data overturns
the universality — the **same** agents, given the **same** free choice, use semantic tools **45% / 50%
/ 57%** of the time when the task is reference-shaped:

| Model | localization (arm C semantic use) | reference (arm C semantic use) |
|-------|----------------------------------:|-------------------------------:|
| Opus 4.8 | 0% | 45% |
| Sonnet 4.6 | 4% | 50% |
| Haiku 4.5 | 6% | 57% |

So the grep default is **task-dependent, not a fixed bias** — the policy already has substantial latent
routing competence and exercises it (~10× more) when the task obviously suits semantic navigation. This
*strengthens* the case for an adaptive router **and** for training the routing into the policy: the
signal is demonstrably already there to be reinforced.

### 6.5.5 Cross-repository: lexical noise, not language, drives the LSP's value

The reference results above are all on one Python repository, which leaves the central causal claim —
*the LSP helps by removing lexical false positives* — confounded with language. If `requests` is noisy
and the LSP wins there, is that because Python's dynamic typing makes `grep` noisy, or because *that
particular codebase* is noisy? The originally-motivating intuition (arXiv:2604.18413) was that a
**strong-LSP, statically-typed** language like TypeScript should *widen* the LSP's edge. We tested it by
running the identical `find_references` protocol (Opus 4.8, 6 targets × 4 arms × 3 rollouts) on two
TypeScript repositories chosen to bracket lexical noise:

- **`remeda`** — a small, clean, fully-typed utility library (function names are distinctive; `grep`
  rarely hits a false positive).
- **`hono`** — a noisy web framework (targets like `html`, `stream` appear as substrings across the
  codebase in comments, types, and unrelated identifiers).

The three repositories together separate the two axes — *language* (Python vs TypeScript) and *lexical
noise* (clean vs noisy):

| Repo | Language | Lexical noise | grep precision | LSP precision | grep F1 | LSP F1 | **ΔF1 (LSP − grep)** | LSP token Δ |
|------|----------|---------------|---------------:|--------------:|--------:|-------:|---------------------:|------------:|
| `remeda` | TypeScript | clean | 1.00 | 1.00 | 0.774 | 0.774 | **+0.000** | +16% |
| `requests` | Python | noisy | 0.76 | 1.00 | 0.706 | 0.778 | **+0.072** | +19% |
| `hono` | TypeScript | noisy | 0.51 | 1.00 | 0.451 | **0.697** | **+0.246** | **−12%** |

Read down the table and the language axis evaporates. The two TypeScript repositories — *same language,
same server, same protocol* — give **opposite** verdicts: on clean `remeda` the LSP buys literally
nothing (ΔF1 = 0.000) and is a pure +16% token tax, because `grep` already achieves precision 1.00 and
there is no noise to remove; on noisy `hono` the LSP delivers the **largest accuracy gain in the entire
study** (ΔF1 = +0.246). This directly **refutes** the "static typing widens the LSP edge" prediction: a
statically-typed language did *not* guarantee an LSP advantage — the clean TypeScript repo is exactly
where the LSP is most useless. What predicts the LSP's value is not the language but **how noisy `grep`
is on that codebase**, captured by `grep`'s precision: across all 18 per-target points spanning both
languages, LSP benefit (F1(LSP) − F1(grep)) rises monotonically as `grep` precision falls (Figure 6).

`hono` is also the **only cell in the entire study where the LSP saves tokens for a strong model** (−12%
vs grep). The mechanism closes the loop with §6.5.3's token accounting: when `grep` precision collapses
to 0.51, the agent must spend extra turns reading files to *adjudicate* each candidate call site (is
this `html` a real reference or a substring in a comment?). Semantic `references` returns the resolved
set directly, so it is simultaneously **more accurate and cheaper** — precisely the regime in which the
+19% "precision premium" of §6.5.3 inverts into a saving. The token premium of the LSP is therefore not
a fixed property of the tool; it is the residual of how much false-positive adjudication `grep` forces,
which is itself a function of codebase noise.

One honest confound remains: `remeda`-clean-vs-`requests`-noisy also differs in codebase, not only
language, and `hono` vs `remeda` differs in repository as well as noise. But the *direction* is
unambiguous and the within-language contrast (`remeda` vs `hono`) is the cleanest control we have: it
holds language and server fixed and moves only noise, and the verdict flips. The actionable consequence
for the router (§6.5.4) is concrete: **the routing key is lexical-noise level (estimable cheaply from a
trial `grep`'s hit density and cross-file spread), not a language whitelist.** "Use the LSP for
TypeScript" is the wrong rule; "use the LSP when a probe `grep` looks noisy" is the right one.

### 6.5.4 Synthesis

Across two task classes, three models, and three repositories spanning two languages, a single
conditional structure holds:

> **"Does an LSP save tokens?" — in general, no.** On symbol-named *localization* it *costs* tokens
> (a tax that grows as the model gets stronger). On *reference-completeness* it does not save tokens
> either (a smaller +12–19% premium for capable models) but the premium now buys *precision* (1.00 vs
> 0.76) and cannot fix the recall bottleneck. It saves tokens only for the *weakest* model, as a crutch
> against lexical noise. And the agent's tool choice is **task-dependent**: it defaults to grep on
> localization (0–6% semantic use) but reaches for the LSP about half the time on reference tasks
> (45–57%) — the grep preference is not a fixed bias but a learned, task-shaped policy. **And what
> governs the LSP's value is lexical noise, not language**: across two TypeScript repositories the
> verdict flips with codebase noise (ΔF1 +0.000 on clean `remeda`, +0.246 on noisy `hono`), and noisy
> `hono` is the one setting where the LSP is both more accurate *and* cheaper for a strong model.

This is exactly the shape that argues against "LSP-always" and for **(a)** an adaptive router keyed on
task class, model capability, and lexical-noise level, and **(b)** training the *when-to-go-semantic*
competence into the policy — and since that routing competence is demonstrably already present in
latent form (it fires far more on reference tasks than localization), the path is to *reinforce* it
rather than bolt on the tool.

---

## 6.6 Edit tasks with real test execution

Localization and reference-completeness are *retrieval* tasks, scored against a static oracle. The
question that matters most for an agent, though, is whether the LSP helps it **edit code correctly** —
where "correct" is defined by *execution*: the change must make the target tests pass. This section
adds that dimension. Because the SWE-bench Docker images do not run under emulation on the available
arm64 host, we built a local, SWE-bench-style harness on **modern `requests`** that runs natively
(Python 3.11, no Docker): each task checks out a base commit, lets the agent edit the working tree via
an `edit_file` tool, then applies the task's test patch and runs the target tests. Resolution is real
`pass@1`.

### 6.6.1 Single-file edits — *the LSP does not help, and the recall question is untestable here*

Six real `requests` bugfix commits (each a single source file + a regression test), 4 arms × 2
rollouts, Opus 4.8:

| Arm | pass@1 | tokens (resolved) | dominant failure |
|-----|-------:|------------------:|------------------|
| A grep-only | **0.83** | 5894 | — |
| B lsp-only | 0.75 | 7360 | — |
| C grep+lsp (free) | 0.58 | 3721 | — |
| D forced-semantic | 0.75 | 4794 | — |

Grep-only is the strongest; adding (C) or forcing (D) the LSP does **not** help, consistent with every
prior task class, and the LSP again carries a token premium. Failures are `wrong_rewrite` (right file,
wrong content) and `no_edit` (the agent explored and gave up) — **not** missed-site recall failures.
But this is *structural*: a single-file task cannot exhibit a cross-file recall miss, so it cannot test
the prediction (§6.5.3) that the LSP's recall ceiling propagates into edit failures. That requires
multi-file edits.

### 6.6.2 Multi-file rename — *the recall→edit-failure prediction, confirmed*

We constructed six **rename refactors** of internal helpers that are imported and called across 2–6
source files (`to_native_string`, `extract_cookies_to_jar`, `get_auth_from_url`, `default_hooks`,
`requote_uri`, `get_environ_proxies`). The agent must update *every* call site; a single missed site
leaves the old name dangling, which the verifier catches (`import requests` fails, or the old name
survives a source grep). Ground truth = the set of source files referencing the symbol; we additionally
measure **site-recall** = fraction of those files the agent fully converts. This is `find_references`'
home turf — if the LSP ever helps an edit, it should help here.

The first run surfaced the prediction cleanly (Opus, location-only LSP via `pylsp`):

| Arm | pass@1 | site-recall | missed-site rate |
|-----|-------:|------------:|-----------------:|
| A grep-only | **1.00** | 1.000 | 0.00 |
| B lsp-only | 0.25 | 0.764 | **0.75** |
| C grep+lsp | 0.92 | 0.917 | 0.00 |
| D forced-semantic | 0.92 | 0.917 | 0.00 |

Grep finds every call site every time. The **location-only LSP arm fails three-quarters of episodes by
missing a call site** — failed episodes average 0.56 site-recall versus 1.00 for resolved ones. The
prediction is confirmed: on multi-file edits the LSP's incomplete reach translates directly into broken
patches. C and D recover only because the agent falls back to grep (its free-choice semantic-tool use is
3–13%).

### 6.6.3 Two confounds, isolated — *backend completeness and index warmup*

The location-only result above is real but was initially *overstated*, and chasing two anomalies is
what made the mechanism precise.

**(i) The semantic backend must actually be complete.** Our edit harness first used `pylsp` (jedi),
which on dynamic Python returns only ~⅓ of real cross-file references (5 of 15 for `to_native_string`,
versus `pyright`'s 14 and grep's 15). An LSP that *itself* under-reports references will of course fail
a rename, regardless of output format — so this says nothing about the location-vs-text question. We
switched the edit harness to `pyright` (already our reference oracle).

**(ii) The language server must be warmed across the whole project.** `pyright` resolves references
*lazily, per file*. A cold `find_references` issued **at a symbol's definition** — the natural query
for a rename — returns only the definition itself (1 result) until the *referencing* files have been
opened; a query at a *usage* site returns the full set. A fixed wait does not fix this (the count stayed
at 1 after 40 s); opening all source files does (1 → 14 immediately). We added a warmup that opens every
`src/` file before the episode. This both restored completeness *and* cut cost sharply (on
`to_native_string`, arm B went from 26 turns / 9.4k tokens to 7 turns / 5.4k). **This is itself a
deployment lesson: an LSP-backed agent that does not warm the index will silently get incomplete
references on exactly the queries a refactor depends on.**

This warmup is not free: opening every source file and letting the server settle costs **~15 s per
episode even on `requests` (19 source files)**, and it grows with repository size. The requirement is
also *not* specific to `pyright` — we measured the identical pathology on TypeScript: a cold
`textDocument/references` at the definition of `remeda`'s `purry` (used across 108 files) returns **1**
reference, rising to the full set only after the project finishes indexing (~10 s). It is a general
property of LSP cross-file resolution, which is lazy and asynchronous. Production LSP-MCP servers treat
it as such: Serena ships a build-time `serena project index` command that walks the repository and
persists symbols to an on-disk cache (`document_symbols.pkl`), *and* a runtime guard
`_wait_for_cross_file_references_if_needed()` whose own comment states that *"some [language servers]
require waiting for a while before they can return accurate cross-file results [...] after at least one
file was opened."* In other words, **a warmed (ideally pre-cached) index is table stakes for any
LSP-backed agent, not an optional tuning** — the cost we pay per episode is precisely what such a cache
amortizes away.

### 6.6.4 Location vs. text, cleanly — *inline snippets help a lot, but cannot close the gap*

With a complete, warmed `pyright` backend we can finally isolate the variable that matters in practice:
does returning the **referencing line's text inline** (as production LSP-MCP servers such as Serena do —
its `find_referencing_symbols` attaches a `content_around_reference` snippet rather than a bare
location) beat the bare-location default? We compare arm **B** (location-only) against arm **F**
(`find_references_with_context`: the same `pyright` references, each enriched with ±2 lines of source
inline, grep-style), with grep (A) as the ceiling. Opus, six rename tasks × 2 rollouts:

| Arm | pass@1 | site-recall | tokens (resolved) | turns | file-reads / episode |
|-----|-------:|------------:|------------------:|------:|---------------------:|
| A grep-only | **1.00** | 1.000 | **2451** | 6.6 | 4.3 |
| B lsp (location-only) | 0.67 | 0.930 | 4131 | 13.2 | 15.2 |
| F lsp (text inline) | **0.83** | 0.958 | 3336 | 7.6 | **3.2** |

Two findings, and they point in opposite directions:

- **The inline snippet helps, substantially, and exactly via the predicted mechanism.** F beats B on
  every axis: pass@1 0.67 → 0.83, site-recall 0.930 → 0.958, tokens −19%, and **file-reads per episode
  15.2 → 3.2** — below even grep's 4.3. The location-only arm spends its turns issuing `find_references`
  and then *reading each file back* to see the call site; attaching the line inline removes those
  follow-up reads almost entirely. This is the quantitative case for the Serena-style design, and it
  validates the intuition that the LSP's penalty is largely a *presentation* problem, not a retrieval
  one.
- **But even a complete, warmed, text-enriched LSP still loses to grep**, and the residual is
  *fundamental, not fixable*. Both B and F fail the `default_hooks` rename identically, missing the same
  site — `_types.py:42`, which is a **comment**: `# These are needed at runtime for default_hooks()
  return type`. `pyright` returns the six real references but never the comment, *by design*: a comment
  is not a semantic reference. A rename, however, is a *textual* operation that must touch comments,
  docstrings, and strings mentioning the name. Semantic references are a strict subset of textual
  occurrences, so `find_references` — however well warmed and however richly formatted — cannot match
  grep's completeness on edits that span non-code text. That is why F reaches 0.83 but not grep's 1.00.

### 6.6.5 Synthesis for edits

For *editing*, grep is the better default: it is cheaper, and it is textually complete in a way semantic
references structurally are not. The LSP's location-only output actively harms multi-file edits (missed
call sites → broken patches), and two operational prerequisites are easy to get wrong (a complete
backend; a fully-warmed index). The single highest-value remedy is the cheapest — **return the
reference line inline** — which recovers most of the gap by eliminating follow-up reads, but cannot
close it. This sharpens, rather than overturns, the paper's thesis: the LSP's semantic precision earns
its keep on *reference listing in noisy code* (§6.5.3, §6.5.5); on *edits* the textual completeness and
low cost of grep dominate. The right architecture remains an **adaptive router** — grep by default,
semantic only where the task is reference-shaped and the codebase is lexically noisy — plus, for any
LSP path, a warmed index and inline-context results as table stakes.

---

## 7. Limitations and threats to validity

The §6.5 results are a **pilot** and must be read with these bounds:

- **Few repositories, small N.** Localization runs are on `requests`; reference runs span `requests`
  (Python), `remeda` and `hono` (TypeScript) — three small, well-known libraries the models have likely
  seen in pretraining. 6 localization tasks and 5–6 reference targets per repo, 3 rollouts each. This is
  enough to validate the harness, separate the noise axis from language, and surface clear directional
  signals, **not** to claim effect sizes that generalize. The numbers (e.g. Sonnet's +118%) are
  pilot-scale and will move with more repositories and tasks; the *directions* (LSP taxes localization,
  buys precision not tokens on reference tasks, helps weak models, is ignored when optional, and tracks
  lexical noise rather than language) are the robust claims.
- **Oracle quality drove a mid-study change.** We initially used `pylsp` (jedi) as the
  reference-completeness ground truth and found it too incomplete on dynamic Python (it disagreed
  badly with an AST cross-check). We switched the oracle to `pyright`, which is markedly more
  complete. This is itself a finding — **LSP reference quality varies sharply by server** — and a
  caution: a study that takes any single LSP as "truth" inherits that server's blind spots.
- **The edit results are local, not SWE-bench-scored.** We *do* run edit tasks end-to-end with real
  test execution (§6.6), but on a **locally built** task set — real `requests` bugfix commits and
  constructed multi-file renames, run natively because the SWE-bench Docker images do not execute under
  emulation on the available arm64 host. This is sound for the *relative* arm comparison we care about
  (grep vs location-LSP vs text-LSP), but the pass@1 numbers are not comparable to a standard SWE-bench
  leaderboard. The static-repo-map arm E, and large repositories where the per-symbol LSP cost
  (arXiv:2604.18413) and the static-index trade-off (Prediction 4) would bite, remain open.
- **Language and server.** Python (`pylsp`/`pyright`) and TypeScript (`typescript-language-server`).
  The originally-motivating TypeScript case is now covered on the reference task (§6.5.5) and — notably —
  did *not* behave as a privileged "strong-LSP" stratum: a clean TypeScript repo was the *worst* case
  for the LSP. Localization and the edit/static-index arms remain Python-only; cross-language
  localization is the next stratum.
- **Harness specificity.** One agent loop, one tool-description style. Tool affordance (description
  length/placement) is held fixed but is itself a lever; results may shift on another harness.
- **Token accounting.** We count total context tokens (the user-facing cost) from the API's reported
  usage; caching/prompt-reuse can change the *billed* cost.
- **Provenance.** Related work mixes fetch-verified sources (F) and established background (K); the
  central absence-of-measurement claim rests only on (F). We claim novelty *to the best of a
  degraded-search survey* and invite correction.

---

## 8. Conclusion

The fundamental insight is that "does an LSP save tokens" is not a tooling opinion — it is a
measurable statement about retrieval precision under a token budget, and it has, remarkably, gone
unmeasured in public while being near-universally asserted. We reduced it to a single primary metric
(**tokens-to-success at iso-accuracy**), a five-arm ablation that isolates semantic retrieval from its
confounds, and a mapping of three real failure modes onto movable variables — and we ran a pilot that
turns the prior into first numbers. The pilot answer is **conditional, and in the common case
negative**: on symbol-named localization, semantic retrieval *costs* tokens (a tax that grows with
model strength); on reference-completeness it buys *precision*, not token savings, at a ~19% premium,
and cannot lift the recall ceiling set by agent thoroughness; it nets a token *saving* only for the
weakest model, as a crutch against lexical noise. On *editing* under real test execution the verdict is
sharpest: grep solves multi-file renames perfectly, a location-only LSP fails most of them by missing a
call site, and the best LSP variant — complete, index-warmed, and returning each reference's line inline
— recovers most of the gap (cutting follow-up reads ~5×) yet still cannot match grep, because semantic
references structurally exclude the comments and strings a rename must touch. Underneath all of it sits
the most robust finding: **the agent's tool choice is task-dependent — it defaults to `grep` on
localization (0–6% semantic use) but reaches for the LSP about half the time on reference tasks (45–57%),
unprompted.** The grep preference is not a fixed bias but a learned, task-shaped policy. That is why the
durable solution is not "add an LSP" but to make the routing, and eventually the semantic competence
itself, **native to the policy rather than a brittle external layer** — and since that routing competence
is demonstrably already present in latent form, the path is to reinforce it (the direction the most
forward-looking related work, RL-from-language-server-feedback, already points). The contribution of this
study is to replace an assertion with a measurement, and a blanket recommendation with a conditional one.
The honest next step is scale: more and larger repositories, the static-index arm, and cross-language
*localization* — the reference and edit tasks now span the retrieval-and-mutation spectrum and show the
routing key is lexical noise and task class, not language.

---

## Appendix A — Minimal experiment checklist

1. Pick harness(es); freeze model + decoding.
2. Implement the five tool surfaces (A–E) behind one identical scaffold; match tool-description length.
3. Assemble the task suite from SWE-bench-series / CORE-Bench, stratified by language (TS primary,
   Python comparison) × repo size × task category; include a lexical-favorable control band.
4. Instrument: per-call token cost, tool type, read volume + unused fraction, turns, wall-clock,
   grep false-positive rate vs oracle, LSP-insufficiency/fallback events, post-edit re-read tokens.
5. Run $R$ rollouts per (arm, task), warm and cold for LSP arms; bootstrap CIs.
6. Report **(success rate, T2S)** pairs *per stratum*; decompose deltas into fewer-reads /
   less-noise / fewer-turns; log every cap or truncation.
7. Evaluate the three remedy arms (index-on-build, API-extension candidates from the fallback
   taxonomy, partial-index+grep hybrid).
8. Decision: is the result LSP-always, lexical-always, or adaptive-router? Hand the fallback taxonomy
   to API design and the habit-gap result (C vs D) to training.

## Appendix B — Source provenance

**Fetch-verified this session (F):**
- github.com/oraios/serena — quoted ("much more token-efficient", "without reading entire files")
- github.com/isaacphi/mcp-language-server — quoted ("more reliable and context-economical")
- aider.chat/docs/repomap.html, aider.chat/2023/10/22/repomap.html — quoted (motivation; `--map-tokens`)
- arXiv:2604.18413 — TypeScript Repository Indexing for Code Agent Retrieval (full abstract; quoted)
- arXiv:2510.22907 — RL from Compiler and Language Server Feedback (full abstract; quoted)
- arXiv:2606.11864 — CORE-Bench (full abstract; quoted)
- arXiv:2605.18747 — Code as Agent Harness (full abstract; quoted)
- Secondary (titles/short abstracts, F): arXiv:2507.19942, 2505.21577, 2603.01327, 2402.12317,
  2509.25716, 2511.05385, 2501.18160

**Established background (K), not fetched this session:**
- SWE-bench (Jimenez et al., 2023); SWE-agent ACI (Yang et al., 2024); AutoCodeRover; Moatless.
