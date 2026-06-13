#!/usr/bin/env python3
"""
Reference-completeness task — the find_references home-turf experiment.

Motivation: localization tasks (where the issue names the symbol) favor grep. The task class
where semantic retrieval *should* win is reference-finding: "list EVERY call site of function F."
grep over-reports (comments, strings, attribute access on unrelated objects with the same name)
and can under-report; LSP find_references returns exactly the resolved call sites. Missing one
caller = wrong answer, so precision AND recall matter — this is where LSP earns its keep, if ever.

TASK: given a target function/method F (file + name), list all (file, line) call sites of F.
GROUND TRUTH: the resolved reference set. We build it two ways and intersect for safety:
  (1) AST oracle: walk every .py file, find Call nodes whose func resolves (by name) to F.
  (2) LSP oracle: pylsp textDocument/references on F's definition.
We use the LSP oracle as primary truth (semantic), and flag disagreements for manual review.
SUCCESS metric: F1 between the agent's reported call-site set and the ground-truth set, plus a
strict "exact set match" boolean. tokens-to-success measured as before.

This reuses harness.py's LSP client + tools. It runs against repos/requests at a PINNED commit
(HEAD of the cloned repo) so the symbol/reference set is stable — NO per-task checkout, so this
must still not run concurrently with harness.py's sweep (both share repos/requests working tree).
"""
import json, os, re, ast, sys, time, subprocess
import anthropic
import harness  # reuse LSP client, tools, model client

REPO = harness.REPO_DIR
CLIENT = harness.CLIENT
MODEL = harness.MODEL

# pyright is a far more accurate reference oracle than pylsp (jedi) on dynamic Python.
# We use it both as the ground-truth oracle AND as arm B's language server.
PYRIGHT_CMD = [os.path.join(harness.HARNESS_DIR, "node_modules", ".bin", "pyright-langserver"), "--stdio"]
LSP_INDEX_WARMUP = 4.0  # pyright needs a few seconds to analyze the project after initialize


def make_lsp():
    lsp = harness.LSP(PYRIGHT_CMD, REPO)
    lsp.initialize()
    time.sleep(LSP_INDEX_WARMUP)
    return lsp


# ---- AST oracle: find call sites of a bare function/method name -------------
def ast_call_sites(name):
    """Return set of (relpath, line) where `name(...)` is called. Name-based (approximate)."""
    sites = set()
    for root, _, files in os.walk(REPO):
        if "/.git" in root:
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, REPO)
            try:
                tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    f = node.func
                    called = None
                    if isinstance(f, ast.Name):
                        called = f.id
                    elif isinstance(f, ast.Attribute):
                        called = f.attr
                    if called == name:
                        sites.add((rel, node.lineno))
    return sites


# ---- LSP oracle -------------------------------------------------------------
def lsp_reference_sites(lsp, rel, line0, char):
    locs = lsp.references(rel, line0, char)
    sites = set()
    for L in locs:
        uri = L.get("uri", "")
        p = uri.replace("file://", "")
        r = os.path.relpath(p, REPO)
        ln = L["range"]["start"]["line"] + 1
        sites.add((r, ln))
    return sites


# ---- find a target function definition (file, name, line0, char) ------------
def find_def_position(rel, name):
    """Locate `def <name>` in rel; return (line0, char_of_name)."""
    path = os.path.join(REPO, rel)
    for i, line in enumerate(open(path, encoding="utf-8", errors="replace")):
        m = re.search(r'\bdef\s+(' + re.escape(name) + r')\b', line)
        if m:
            return i, m.start(1)
    return None


# ---- the agent task ---------------------------------------------------------
REF_SUBMIT = {
    "name": "submit_call_sites",
    "description": "Submit the complete list of call sites of the target function as 'path:line' strings.",
    "input_schema": {"type": "object", "properties": {
        "sites": {"type": "array", "items": {"type": "string"},
                  "description": "Every call site, e.g. 'requests/sessions.py:512'"}},
        "required": ["sites"]},
}
REF_SYS = """You are a code-navigation agent. You are given a target function in the `requests` library.
Your goal: find EVERY call site of that function across the codebase — all the places it is actually
called. Missing even one is a failure; including a non-call (a comment, a string, an unrelated method
with the same name) is also wrong. Use the provided tools, then call submit_call_sites with the
complete list of 'path:line' locations. Be thorough AND precise."""


def parse_site(s):
    s = s.strip().split()[0] if s.strip() else s
    m = re.match(r'(.+?):(\d+)', s.strip())
    if m:
        return (m.group(1), int(m.group(2)))
    return None


def run_ref_episode(target, arm_name, model=None, max_turns=25, verbose=False):
    model = model or MODEL
    arm = harness.ARMS[arm_name]
    # swap in the ref submit tool instead of the localization one
    tools = [t for t in arm["tools"]] + [REF_SUBMIT]
    sys_p = REF_SYS + (harness.SYS_FORCE if arm["force_semantic"] else "")
    lsp = None
    if any(t["name"] in ("find_references", "find_definition", "document_symbols") for t in tools):
        lsp = make_lsp()
    user = (f"Target function: `{target['name']}` defined in {target['file']} "
            f"(line {target['defline']}). Find every call site of it.")
    messages = [{"role": "user", "content": user}]
    tok_in = tok_out = 0
    tool_calls = []
    submitted = None
    t0 = time.monotonic()
    try:
        for turn in range(max_turns):
            resp = CLIENT.messages.create(model=model, max_tokens=1500, system=sys_p,
                                          tools=tools, messages=messages)
            tok_in += resp.usage.input_tokens
            tok_out += resp.usage.output_tokens
            messages.append({"role": "assistant", "content": resp.content})
            tres = []
            stop = False
            for b in resp.content:
                if b.type == "tool_use":
                    tool_calls.append(b.name)
                    if b.name == "submit_call_sites":
                        raw = b.input.get("sites", [])
                        if isinstance(raw, str):
                            raw = re.split(r"[\s,]+", raw.strip())
                        submitted = set(filter(None, (parse_site(x) for x in raw)))
                        tres.append({"type": "tool_result", "tool_use_id": b.id, "content": "Recorded."})
                        stop = True
                    else:
                        try:
                            out = harness.run_tool(b.name, b.input, lsp)
                        except Exception as e:
                            out = f"(tool error: {e})"
                        tres.append({"type": "tool_result", "tool_use_id": b.id, "content": out[:6000]})
            if stop:
                break
            if tres:
                messages.append({"role": "user", "content": tres})
            else:
                break
    finally:
        if lsp:
            lsp.shutdown()

    gt = target["truth"]
    got = submitted or set()
    tp = len(got & gt); fp = len(got - gt); fn = len(gt - got)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {
        "target": target["name"], "arm": arm_name, "model": model,
        "exact_match": got == gt, "f1": round(f1, 3), "precision": round(prec, 3),
        "recall": round(rec, 3), "n_truth": len(gt), "n_got": len(got),
        "tp": tp, "fp": fp, "fn": fn,
        "tokens_total": tok_in + tok_out, "n_tool_calls": len(tool_calls),
        "tool_calls": tool_calls, "turns": turn + 1, "seconds": round(time.monotonic() - t0, 1),
    }


# ---- build targets (pin to current repo HEAD) -------------------------------
# Chosen functions that have multiple internal call sites in requests.
# Chosen functions with multiple CROSS-FILE call sites in requests (pyright-stable, 3<=refs<=15).
# Ground truth = pyright's reference set (semantic gold standard). Arm A (grep) must reconstruct it.
TARGET_SPECS = [
    {"file": "src/requests/utils.py", "name": "to_key_val_list"},              # ~9 refs / 3 files
    {"file": "src/requests/cookies.py", "name": "cookiejar_from_dict"},        # ~11 refs / 4 files
    {"file": "src/requests/utils.py", "name": "get_auth_from_url"},            # ~7 refs / 3 files
    {"file": "src/requests/auth.py", "name": "_basic_auth_str"},               # ~6 refs / 3 files
    {"file": "src/requests/sessions.py", "name": "merge_setting"},             # ~8 refs / 1 file clean
]


def build_targets():
    # ground truth = pyright references (semantic gold standard). caller must guarantee no concurrent checkout.
    out = []
    lsp = make_lsp()
    try:
        for spec in TARGET_SPECS:
            pos = find_def_position(spec["file"], spec["name"])
            if not pos:
                print(f"  SKIP {spec['file']}::{spec['name']} (def not found)")
                continue
            line0, char = pos
            lsp_sites = lsp_reference_sites(lsp, spec["file"], line0, char)
            ast_sites = ast_call_sites(spec["name"])
            defsite = (spec["file"], line0 + 1)
            truth = {s for s in lsp_sites if s != defsite}   # pyright refs minus the def line
            nfiles = len({s[0] for s in truth})
            out.append({**spec, "defline": line0 + 1, "truth": truth,
                        "lsp_n": len(lsp_sites), "ast_n": len(ast_sites),
                        "ast_agree": len(truth & ast_sites), "n_files": nfiles})
            print(f"  {spec['name']:18s} truth={len(truth):2d} across {nfiles} files "
                  f"(pyright={len(lsp_sites)} ast={len(ast_sites)} ast_agree={len(truth & ast_sites)})")
    finally:
        lsp.shutdown()
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="A_grep,B_lsp,C_both,D_forced")
    ap.add_argument("--rollouts", type=int, default=2)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--out", default="ref_results.jsonl")
    ap.add_argument("--build-only", action="store_true", help="just build+print targets, don't run agent")
    args = ap.parse_args()

    print("Building reference targets (LSP + AST oracle) against repo HEAD...")
    targets = build_targets()
    print(f"\n{len(targets)} targets built.")
    if args.build_only:
        sys.exit(0)

    outpath = os.path.join(harness.HARNESS_DIR, "runs", args.out)
    arms = args.arms.split(",")
    n = 0
    with open(outpath, "w") as fout:
        for tgt in targets:
            for arm in arms:
                for r in range(args.rollouts):
                    try:
                        res = run_ref_episode(tgt, arm, model=args.model)
                    except Exception as e:
                        res = {"target": tgt["name"], "arm": arm, "model": args.model,
                               "error": f"{type(e).__name__}: {e}", "exact_match": False,
                               "f1": 0, "tokens_total": 0, "n_tool_calls": 0, "turns": 0}
                    fout.write(json.dumps(res) + "\n"); fout.flush(); n += 1
                    print(f"{res['target']:18s} {arm:9s} r{r} f1={res.get('f1')} "
                          f"exact={res.get('exact_match')} tok={res.get('tokens_total',0):6d} "
                          f"calls={res.get('n_tool_calls',0):2d}", flush=True)
    print(f"\nwrote {n} results to {outpath}", flush=True)
