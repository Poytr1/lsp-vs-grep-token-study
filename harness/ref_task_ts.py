#!/usr/bin/env python3
"""
TypeScript reference-completeness experiment (option 3).
Same design as ref_task.py but on a real TS repo (ts-pattern) with
typescript-language-server (tsserver) as the ground-truth oracle AND arm-B server.

TS is statically typed, so reference resolution is cleaner than Python/jedi — this
tests whether the LSP's precision/token advantage on reference tasks is larger here.

Reuses ref_task.py's episode loop + tools. We repoint harness.REPO_DIR at the TS repo
and swap in a TS language server, so grep/read/LSP tools all operate on the TS tree.
Run only when nothing else uses the TS repo (no concurrent checkout needed — pinned HEAD).
"""
import os, re, time, json, sys, argparse
import harness
import ref_task as rt

TS_REPO = os.path.join(harness.HARNESS_DIR, "repos", "hono")
TSLS_CMD = [os.path.join(harness.HARNESS_DIR, "node_modules", ".bin", "typescript-language-server"), "--stdio"]
TS_WARMUP = 12.0

# --- repoint the shared harness/ref_task config at the TS repo + TS server ---
harness.REPO_DIR = TS_REPO
rt.REPO = TS_REPO


class TSLSP(harness.LSP):
    """typescript-language-server speaks the same LSP; only languageId differs on didOpen."""
    def ensure_open(self, rel):
        if rel in self._opened:
            return
        path = os.path.join(self.root, rel)
        text = open(path, encoding="utf-8", errors="replace").read()
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": self.uri(rel), "languageId": "typescript", "version": 1, "text": text}})
        self._opened.add(rel)
        time.sleep(0.4)


def ts_make_lsp():
    lsp = TSLSP(TSLS_CMD, TS_REPO)
    lsp.initialize()
    time.sleep(TS_WARMUP)
    # Pre-warm: tsserver returns partial (definition-file-only) references for the FIRST symbol
    # queried until its cross-file references are fully computed. Issue throwaway queries on the
    # real targets first so the subsequent real query for each is warm and complete.
    for spec in rt.TARGET_SPECS:
        pos = ts_find_def_position(spec["file"], spec["name"])
        if pos:
            try:
                lsp.references(spec["file"], pos[0], pos[1])
            except Exception:
                pass
    time.sleep(1.0)
    return lsp

# make ref_task use the TS language server everywhere it builds one
rt.make_lsp = ts_make_lsp

# TS def-position finder: `export function NAME` or `export const NAME =` or `const NAME =`
def ts_find_def_position(rel, name):
    path = os.path.join(TS_REPO, rel)
    for i, line in enumerate(open(path, encoding="utf-8", errors="replace")):
        m = re.search(r'\b(?:export\s+)?(?:function\s+|const\s+|let\s+)(' + re.escape(name) + r')\b', line)
        if m:
            return i, m.start(1)
    return None
rt.find_def_position = ts_find_def_position

# grep tool: include *.ts instead of *.py (override the lexical search)
def ts_search_text(query, max_results=40):
    import subprocess
    try:
        out = subprocess.run(["grep", "-rn", "--include=*.ts", query, TS_REPO],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception as e:
        return f"(search error: {e})"
    lines = out.splitlines()
    rels = [L.replace(TS_REPO + "/", "", 1) for L in lines[:max_results]]
    extra = "" if len(lines) <= max_results else f"\n...(+{len(lines)-max_results} more)"
    return ("\n".join(rels) if rels else "(no matches)") + extra
harness.tool_search_text = ts_search_text

# TS targets: hono (web framework, NOISIER naming than remeda — common words collide in grep).
# Stable cross-file functions (verified via double-query + pre-warm). Mix favors noisy short names
# (html, stream) to stress-test whether grep's precision drops here vs remeda's distinctive names.
rt.TARGET_SPECS = [
    {"file": "src/helper/html/index.ts", "name": "html"},               # ~6 refs / 3 files — very common word
    {"file": "src/helper/streaming/stream.ts", "name": "stream"},       # ~9 refs / 4 files — collides w/ streamSSE/Text
    {"file": "src/utils/encode.ts", "name": "decodeBase64"},            # ~9 refs / 5 files
    {"file": "src/helper/adapter/index.ts", "name": "getRuntimeKey"},   # ~11 refs / 6 files — widest spread
    {"file": "src/utils/html.ts", "name": "escapeToBuffer"},            # ~14 refs / 4 files
    {"file": "src/utils/accept.ts", "name": "parseAccept"},             # ~6 refs / 3 files
]

# AST oracle doesn't apply to TS; stub it so build_targets' sanity field is harmless
rt.ast_call_sites = lambda name: set()

# system prompt: tell the agent it's TypeScript / ts-pattern
rt.REF_SYS = rt.REF_SYS.replace("the `requests` library", "the `hono` TypeScript web framework")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="A_grep,B_lsp,C_both,D_forced")
    ap.add_argument("--rollouts", type=int, default=3)
    ap.add_argument("--model", default=harness.MODEL)
    ap.add_argument("--out", default="ref_ts_opus.jsonl")
    ap.add_argument("--build-only", action="store_true")
    args = ap.parse_args()

    print(f"TS repo: {TS_REPO}")
    print("Building TS reference targets (tsserver oracle)...")
    targets = rt.build_targets()
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
                        res = rt.run_ref_episode(tgt, arm, model=args.model)
                    except Exception as e:
                        res = {"target": tgt["name"], "arm": arm, "model": args.model,
                               "error": f"{type(e).__name__}: {e}", "exact_match": False,
                               "f1": 0, "tokens_total": 0, "n_tool_calls": 0, "turns": 0}
                    res["lang"] = "typescript"
                    fout.write(json.dumps(res) + "\n"); fout.flush(); n += 1
                    print(f"{res['target']:18s} {arm:9s} r{r} f1={res.get('f1')} "
                          f"exact={res.get('exact_match')} tok={res.get('tokens_total',0):6d} "
                          f"calls={res.get('n_tool_calls',0):2d}", flush=True)
    print(f"\nwrote {n} results to {outpath}", flush=True)
