#!/usr/bin/env python3
"""
LSP-vs-grep agent harness — EDIT task with REAL native test execution.

Unlike localization (name the file) and reference-completeness (list call sites),
an EDIT task makes the agent WRITE a patch; correctness is defined by EXECUTION:
the target regression test must flip red->green and the rest of the suite must
stay green. No static metric can substitute for running the suite.

Why native (not SWE-bench Docker): on this arm64 host the SWE-bench x86 images
hang under emulation (and plain `docker pull` stalls). Instead we build a local,
SWE-bench-style task set from real bugfix commits in modern `requests` (runs on
Python 3.11 in a venv) — see edit_tasks.py. Same red->green contract, no Docker.

Protocol (per episode), faithful to SWE-bench:
  1. checkout the task's base_commit (bug present, fix absent, regression test
     NOT yet added — the agent works only from the problem_statement).
  2. agent explores with the arm's retrieval tools and edits the working tree
     via edit_file, then calls submit. Its `git diff` is the candidate patch.
  3. verify: clean tree -> base -> apply the agent patch -> apply the test_patch
     -> run the target tests. Resolved iff they pass. (Run verify_edit.py.)

Variable across arms is the retrieval tool surface, identical to harness.py:
  A_grep / B_lsp / C_both / D_forced.

Failure-mode classification (test of the recall->edit-failure prediction):
  missed_site   = agent's patch touched a STRICT SUBSET of the gold edit files
  over_edit     = touched files the gold patch did not
  wrong_rewrite = right file set, tests still fail
This measures whether the ~0.66 reference-completeness recall ceiling propagates
into edit failures and whether the LSP arm changes it.
"""
import json, os, re, subprocess, time, argparse, sys

from harness import (
    LSP, PYLSP_CMD, CLIENT, MODEL,
    GREP_TOOLS, LSP_TOOLS, run_tool, _flatten_symbols, loc_to_str,
)

HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
# Modern requests lives under src/requests; the harness navigates the whole repo.
REPO_DIR = os.path.join(HARNESS_DIR, "repos", "requests")
TASKS_PATH = os.path.join(HARNESS_DIR, "edit", "tasks.jsonl")

# LSP backend. pylsp (jedi) is semantically INCOMPLETE on dynamic Python — it
# returns only ~1/3 of real cross-file references on `requests`, which sabotages
# reference-heavy edits regardless of how the output is formatted. pyright is far
# more complete (matches grep's site set ~14/15). For the reference-heavy edit /
# rename tasks we default to pyright so the comparison isolates location-vs-text,
# not weak-vs-strong backend. Select with --lsp {pyright,pylsp}.
PYRIGHT_CMD = [os.path.join(HARNESS_DIR, "node_modules", ".bin", "pyright-langserver"), "--stdio"]
LSP_BACKEND = {
    "pyright": (PYRIGHT_CMD, 4.0),
    "pylsp":   (PYLSP_CMD, 2.0),
}
LSP_CMD, LSP_WARMUP = LSP_BACKEND["pyright"]   # default; overridden by --lsp

# harness.py's tools default to REPO_DIR / *.py; for the edit task we want the
# agent to search src/ and tests live, so we monkeypatch its REPO_DIR target and
# broaden the grep glob to .py everywhere under the repo.
import harness as _h
_h.REPO_DIR = REPO_DIR


def tool_edit_file(relpath, old, new):
    path = os.path.join(REPO_DIR, relpath)
    if not os.path.isfile(path):
        return f"(edit error: no such file {relpath})"
    src = open(path, encoding="utf-8", errors="replace").read()
    n = src.count(old)
    if n == 0:
        return ("(edit error: old_str not found verbatim. Re-read the file and copy the exact "
                "text including indentation.)")
    if n > 1:
        return (f"(edit error: old_str matches {n} places; include more surrounding context "
                "to make it unique.)")
    open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))
    return f"OK: applied 1 replacement in {relpath}."


EDIT_TOOL = {
    "name": "edit_file",
    "description": ("Replace an exact substring in a file in place. `old_str` must appear VERBATIM "
                    "and exactly once (include enough surrounding lines to be unique). Use this to "
                    "write your fix; call it as many times as needed."),
    "input_schema": {"type": "object", "properties": {
        "relpath": {"type": "string", "description": "Repo-relative path, e.g. 'src/requests/utils.py'"},
        "old_str": {"type": "string"}, "new_str": {"type": "string"}},
        "required": ["relpath", "old_str", "new_str"]},
}
SUBMIT_TOOL = {
    "name": "submit",
    "description": "Call when your edits are complete and the bug is fixed.",
    "input_schema": {"type": "object", "properties": {
        "summary": {"type": "string"}}, "required": []},
}

# ---------------------------------------------------------------------------
# Arm F — the "text-based LSP" remedy (per @pchsu's hypothesis).
# The stock LSP tool returns LOCATIONS ONLY (file:line:col); the agent must then
# read_file each site to see the code. grep, by contrast, returns the matched
# LINE TEXT inline. We wrap the SAME location-based `references` call and enrich
# every hit with its source line plus a few lines of context, returned grep-style
# ("relpath:line: <code>"). No change to the LSP protocol — pure harness wrap.
# This isolates whether the LSP's token penalty / recall ceiling is caused by the
# missing inline text, not by semantic retrieval itself.
# ---------------------------------------------------------------------------
def _line_with_context(rel, line0, ctx=2):
    """Return 'rel:line: text' for the hit line plus +/-ctx lines, like grep -C."""
    p = os.path.join(REPO_DIR, rel)
    if not os.path.isfile(p):
        return f"{rel}:{line0+1}: (unreadable)"
    lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
    lo = max(0, line0 - ctx)
    hi = min(len(lines), line0 + ctx + 1)
    out = []
    for i in range(lo, hi):
        marker = ">" if i == line0 else " "
        out.append(f"{rel}:{i+1}:{marker} {lines[i]}")
    return "\n".join(out)


def tool_find_references_with_context(lsp, rel, line, char, ctx=2):
    """Location-based references, enriched with inline source text (grep-like)."""
    locs = lsp.references(rel, line - 1, char)
    if not locs:
        return "(no references)"
    blocks = []
    for L in locs:
        uri = L.get("uri") or L.get("targetUri", "")
        rng = L.get("range") or L.get("targetRange", {})
        lrel = uri.replace("file://", "")
        lrel = os.path.relpath(lrel, os.path.abspath(REPO_DIR)) if lrel else "?"
        l0 = rng.get("start", {}).get("line", 0)
        blocks.append(_line_with_context(lrel, l0, ctx))
    return f"{len(locs)} reference(s):\n" + "\n--\n".join(blocks)


REF_CTX_TOOL = {
    "name": "find_references_with_context",
    "description": ("Find all semantic references to the symbol at file:line:character, returning each "
                    "reference's location AND the surrounding source lines inline (like grep -C), so you "
                    "do not need a separate read_file to see each call site. Resolves real usages only — "
                    "excludes comments, strings, and unrelated identifiers."),
    "input_schema": {"type": "object", "properties": {
        "relpath": {"type": "string"}, "line": {"type": "integer", "description": "1-based line"},
        "character": {"type": "integer", "description": "0-based column on that line"}},
        "required": ["relpath", "line", "character"]},
}
# Arm F's tool surface: the wrapped references tool + the rest of the LSP toolset
# (document_symbols, find_definition, read_file) but NOT the bare find_references —
# F replaces location-only references with the context-enriched version.
F_TOOLS = [REF_CTX_TOOL] + [t for t in LSP_TOOLS if t["name"] != "find_references"]

ARMS = {
    "A_grep":   {"tools": GREP_TOOLS,                  "force_semantic": False},
    "B_lsp":    {"tools": LSP_TOOLS,                   "force_semantic": False},
    "C_both":   {"tools": GREP_TOOLS + LSP_TOOLS[:-1], "force_semantic": False},
    "D_forced": {"tools": GREP_TOOLS + LSP_TOOLS[:-1], "force_semantic": True},
    "F_lsp_ctx": {"tools": F_TOOLS,                    "force_semantic": False},
}

SYS_BASE = """You are a senior engineer fixing a bug in the `requests` Python library (source under src/requests/).
You are given a bug report. EDIT the source so the bug is fixed: explore with the provided tools, then
use edit_file to make the change(s), and call submit when done.

A correct fix often requires editing EVERY relevant site, not just one — if the change affects a
function's callers or sibling code paths, update all of them. Read code before editing it and copy exact
text into edit_file. Do NOT edit test files. Paths look like 'src/requests/utils.py'."""
SYS_FORCE = ("\nIMPORTANT: Prefer semantic navigation (document_symbols / find_references / "
             "find_definition) BEFORE text search, especially to find every site that must change.")


def git(*args):
    return subprocess.run(["git", "-C", REPO_DIR, *args], capture_output=True, text=True)


def reset_to(base_commit):
    git("checkout", "-f", base_commit)
    git("clean", "-fdqx")


def warmup_references(lsp):
    """Open every src/requests/*.py file so the language server analyzes the whole
    project. pyright resolves references LAZILY per-file: a cold `find_references`
    query (especially at a symbol's definition) only sees usages in files it has
    already analyzed, so it under-reports until the referencing files are opened.
    Opening all of them up front makes find_references complete (matches grep)."""
    src_root = os.path.join(REPO_DIR, "src", "requests")
    if not os.path.isdir(src_root):
        return
    opened = 0
    for root, _, files in os.walk(src_root):
        for f in files:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, f), REPO_DIR)
                try:
                    lsp.ensure_open(rel)
                    opened += 1
                except Exception:
                    pass
    if opened:
        time.sleep(3.0)   # let the server finish analyzing the freshly-opened files


def run_tool_edit(name, inp, lsp):
    if name == "edit_file":
        return tool_edit_file(inp["relpath"], inp["old_str"], inp["new_str"])
    if name == "find_references_with_context":
        return tool_find_references_with_context(lsp, inp["relpath"], inp["line"], inp["character"])
    return run_tool(name, inp, lsp)


def run_episode(instance, arm_name, max_turns=40, verbose=False, model=None):
    model = model or MODEL
    arm = ARMS[arm_name]
    tools = arm["tools"] + [EDIT_TOOL, SUBMIT_TOOL]
    sys_prompt = SYS_BASE + (SYS_FORCE if arm["force_semantic"] else "")

    reset_to(instance["base_commit"])  # bug present, regression test absent

    lsp = None
    if any(t["name"] in ("find_references", "find_definition", "document_symbols",
                         "find_references_with_context") for t in tools):
        lsp = LSP(LSP_CMD, REPO_DIR)
        lsp.initialize()
        time.sleep(LSP_WARMUP)
        warmup_references(lsp)   # CRITICAL: pyright resolves refs lazily per-file; open the
                                 # whole project so find_references sees cross-file usages.

    user = (f"Bug report:\n\n{instance['problem_statement'][:5000]}\n\n"
            "Fix the bug by editing the source under src/requests/, then call submit.")
    messages = [{"role": "user", "content": user}]
    tok_in = tok_out = 0
    tool_calls = []
    n_edits = 0
    submitted = False
    t0 = time.monotonic()
    try:
        for turn in range(max_turns):
            resp = CLIENT.messages.create(model=model, max_tokens=2048,
                                          system=sys_prompt, tools=tools, messages=messages)
            tok_in += resp.usage.input_tokens
            tok_out += resp.usage.output_tokens
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            stop = False
            for block in resp.content:
                if block.type == "tool_use":
                    tool_calls.append(block.name)
                    if block.name == "submit":
                        submitted = True
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                             "content": "Recorded. Done."})
                        stop = True
                    else:
                        try:
                            out = run_tool_edit(block.name, block.input, lsp)
                        except Exception as e:
                            out = f"(tool error: {type(e).__name__}: {e})"
                        if block.name == "edit_file" and out.startswith("OK"):
                            n_edits += 1
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                             "content": out[:6000]})
                        if verbose:
                            print(f"    [{arm_name}] {block.name}({str(block.input)[:55]}) -> {out[:65]!r}")
            if stop:
                break
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                break
    finally:
        if lsp:
            lsp.shutdown()

    diff = git("diff").stdout
    got_files = sorted(set(re.findall(r'^\+\+\+ b/(.+)$', diff, re.M)))
    rec = {
        "instance": instance["instance_id"], "arm": arm_name, "model": model,
        "submitted": submitted, "n_edits": n_edits,
        "gold_files": instance["gold_files"], "edited_files": got_files,
        "patch": diff,
        "tokens_in": tok_in, "tokens_out": tok_out, "tokens_total": tok_in + tok_out,
        "tool_calls": tool_calls, "n_tool_calls": len(tool_calls),
        "turns": turn + 1, "seconds": round(time.monotonic() - t0, 1),
    }
    reset_to("main")
    return rec


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="A_grep,B_lsp,C_both,D_forced")
    ap.add_argument("--rollouts", type=int, default=2)
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--out", default="edit_opus.jsonl")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--tasks", default=TASKS_PATH,
                    help="path to a tasks .jsonl (default single-file edit tasks; "
                         "pass edit/rename_tasks.jsonl for multi-file rename tasks)")
    ap.add_argument("--lsp", default="pyright", choices=["pyright", "pylsp"],
                    help="LSP backend for semantic arms (default pyright; pylsp is weaker)")
    args = ap.parse_args()

    LSP_CMD, LSP_WARMUP = LSP_BACKEND[args.lsp]
    print(f"[edit_harness] LSP backend = {args.lsp}", flush=True)

    tasks_path = args.tasks if os.path.isabs(args.tasks) else os.path.join(HARNESS_DIR, args.tasks)
    tasks = [json.loads(l) for l in open(tasks_path)][:args.limit]
    arms = args.arms.split(",")
    outpath = os.path.join(HARNESS_DIR, "runs", args.out)
    n = 0
    with open(outpath, "w") as fout:
        for inst in tasks:
            for arm in arms:
                for r in range(args.rollouts):
                    try:
                        res = run_episode(inst, arm, verbose=args.verbose, model=args.model)
                    except Exception as e:
                        res = {"instance": inst["instance_id"], "arm": arm, "rollout": r,
                               "error": f"{type(e).__name__}: {e}", "submitted": False,
                               "patch": "", "tokens_total": 0, "n_tool_calls": 0, "turns": 0}
                    res["rollout"] = r
                    fout.write(json.dumps(res) + "\n")
                    fout.flush()
                    n += 1
                    err = res.get("error", "")
                    print(f"{res['instance']:20s} {arm:9s} r{r} edits={res.get('n_edits',0)} "
                          f"files={res.get('edited_files')} tok={res.get('tokens_total',0):6d} "
                          f"turns={res.get('turns',0):2d} {'ERR='+err if err else ''}", flush=True)
    print(f"\nwrote {n} episodes to {outpath} (patches only; run verify_edit.py for pass@1)", flush=True)
