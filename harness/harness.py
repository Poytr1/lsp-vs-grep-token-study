#!/usr/bin/env python3
"""
LSP-vs-grep agent harness — v0.
Measures tokens-to-success for a localization task under different retrieval tool surfaces.

A localization task: given a SWE-bench problem_statement, the agent must name the file(s)
that must change. Ground truth = files touched by the gold patch. Token cost is the total
context tokens consumed until the agent submits a correct localization.

Arms (tool surface is the ONLY variable):
  A  grep-only      : search_text + read_file + submit
  B  lsp-only       : find_references + find_definition + document_symbols + read_file + submit
  C  both           : union of A and B (agent chooses)
  D  forced-semantic: same as C but system prompt mandates a semantic attempt before grep
  E  repo-map       : (added later) static signature map up front + grep

This module is intentionally a single file for v0 clarity.
"""
import json, os, re, subprocess, threading, time, sys, argparse
import anthropic

HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(HARNESS_DIR, "repos", "requests")
PYLSP_CMD = [sys.executable, "-m", "pylsp"]

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4.8")
CLIENT = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_AUTH_TOKEN"),
    base_url=os.environ.get("ANTHROPIC_BASE_URL"),
)

# ----------------------------------------------------------------------------
# LSP client over stdio JSON-RPC (server-agnostic; here driving pylsp)
# ----------------------------------------------------------------------------
class LSP:
    def __init__(self, cmd, root):
        self.root = os.path.abspath(root)
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, bufsize=0)
        self._id = 0
        self._resp = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._opened = set()

    def _read_loop(self):
        f = self.p.stdout
        while True:
            header = b""
            while b"\r\n\r\n" not in header:
                ch = f.read(1)
                if not ch:
                    return
                header += ch
            length = 0
            for line in header.decode("ascii", "replace").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":")[1].strip())
            body = b""
            while len(body) < length:
                chunk = f.read(length - len(body))
                if not chunk:
                    return
                body += chunk
            try:
                msg = json.loads(body.decode("utf-8"))
            except Exception:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._cv:
                    self._resp[msg["id"]] = msg
                    self._cv.notify_all()

    def _send(self, method, params, notify=False):
        with self._lock:
            self._id += 1
            mid = self._id
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            msg["id"] = mid
        data = json.dumps(msg).encode("utf-8")
        self.p.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data)
        self.p.stdin.flush()
        return mid

    def request(self, method, params, timeout=25):
        mid = self._send(method, params)
        deadline = time.monotonic() + timeout
        with self._cv:
            while mid not in self._resp:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError(method)
                self._cv.wait(rem)
            return self._resp.pop(mid)

    def notify(self, method, params):
        self._send(method, params, notify=True)

    def uri(self, rel):
        return "file://" + os.path.join(self.root, rel)

    def initialize(self):
        self.request("initialize", {"processId": os.getpid(),
                                     "rootUri": "file://" + self.root,
                                     "capabilities": {}})
        self.notify("initialized", {})

    def ensure_open(self, rel):
        if rel in self._opened:
            return
        path = os.path.join(self.root, rel)
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": self.uri(rel), "languageId": "python", "version": 1, "text": text}})
        self._opened.add(rel)
        time.sleep(0.4)

    def references(self, rel, line, char):
        self.ensure_open(rel)
        r = self.request("textDocument/references", {
            "textDocument": {"uri": self.uri(rel)},
            "position": {"line": line, "character": char},
            "context": {"includeDeclaration": True}})
        return r.get("result") or []

    def definition(self, rel, line, char):
        self.ensure_open(rel)
        r = self.request("textDocument/definition", {
            "textDocument": {"uri": self.uri(rel)},
            "position": {"line": line, "character": char}})
        return r.get("result") or []

    def document_symbols(self, rel):
        self.ensure_open(rel)
        r = self.request("textDocument/documentSymbol", {
            "textDocument": {"uri": self.uri(rel)}})
        return r.get("result") or []

    def shutdown(self):
        try:
            self.request("shutdown", None, timeout=5)
            self.notify("exit", None)
        except Exception:
            pass
        self.p.terminate()


def loc_to_str(loc):
    """Normalize an LSP Location/range to 'relpath:line'."""
    uri = loc.get("uri") or loc.get("targetUri", "")
    rng = loc.get("range") or loc.get("targetRange", {})
    rel = uri.replace("file://", "")
    rel = os.path.relpath(rel, os.path.abspath(REPO_DIR)) if rel else "?"
    ln = rng.get("start", {}).get("line", -1) + 1
    return f"{rel}:{ln}"


# ----------------------------------------------------------------------------
# Plain tools (grep / read) — operate on the working tree
# ----------------------------------------------------------------------------
def tool_search_text(query, max_results=40):
    """ripgrep-style lexical search over .py files (the grep arm's only navigation)."""
    try:
        out = subprocess.run(
            ["grep", "-rn", "--include=*.py", query, REPO_DIR],
            capture_output=True, text=True, timeout=20).stdout
    except Exception as e:
        return f"(search error: {e})"
    lines = out.splitlines()
    rels = []
    for L in lines[:max_results]:
        # strip the repo dir prefix to keep paths short (token-honest)
        rels.append(L.replace(REPO_DIR + "/", "", 1))
    extra = "" if len(lines) <= max_results else f"\n...(+{len(lines)-max_results} more matches)"
    return ("\n".join(rels) if rels else "(no matches)") + extra


def tool_read_file(relpath, start=1, end=None):
    path = os.path.join(REPO_DIR, relpath)
    if not os.path.isfile(path):
        return f"(no such file: {relpath})"
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    end = end or min(len(lines), start + 200)
    chunk = lines[start - 1:end]
    return f"# {relpath} (lines {start}-{start+len(chunk)-1} of {len(lines)})\n" + "".join(chunk)


# ----------------------------------------------------------------------------
# Tool schemas per arm
# ----------------------------------------------------------------------------
SUBMIT_TOOL = {
    "name": "submit_localization",
    "description": "Submit your final answer: the repository-relative path(s) of the file(s) that must be modified to fix the issue.",
    "input_schema": {"type": "object", "properties": {
        "files": {"type": "array", "items": {"type": "string"},
                  "description": "Repo-relative file paths, e.g. 'requests/sessions.py'"},
        "reason": {"type": "string", "description": "One sentence: why these files."}},
        "required": ["files"]},
}
GREP_TOOLS = [
    {"name": "search_text",
     "description": "Lexical text search (grep) across all .py files. Returns 'path:line: matched line'. Matches everything textually, including comments and strings.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"}}, "required": ["query"]}},
    {"name": "read_file",
     "description": "Read a file (optionally a line range).",
     "input_schema": {"type": "object", "properties": {
         "relpath": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
         "required": ["relpath"]}},
]
LSP_TOOLS = [
    {"name": "document_symbols",
     "description": "List the symbols (classes, functions, methods) defined in a file, with their line numbers. Semantic — only real definitions.",
     "input_schema": {"type": "object", "properties": {"relpath": {"type": "string"}},
                      "required": ["relpath"]}},
    {"name": "find_references",
     "description": "Find all semantic references to the symbol at a given file:line:character. Resolves real usages only — excludes comments, strings, and unrelated identifiers.",
     "input_schema": {"type": "object", "properties": {
         "relpath": {"type": "string"}, "line": {"type": "integer", "description": "1-based line"},
         "character": {"type": "integer", "description": "0-based column on that line"}},
         "required": ["relpath", "line", "character"]}},
    {"name": "find_definition",
     "description": "Jump to the definition of the symbol at file:line:character.",
     "input_schema": {"type": "object", "properties": {
         "relpath": {"type": "string"}, "line": {"type": "integer"}, "character": {"type": "integer"}},
         "required": ["relpath", "line", "character"]}},
    {"name": "read_file",
     "description": "Read a file (optionally a line range).",
     "input_schema": {"type": "object", "properties": {
         "relpath": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
         "required": ["relpath"]}},
]

ARMS = {
    "A_grep":   {"tools": GREP_TOOLS, "force_semantic": False},
    "B_lsp":    {"tools": LSP_TOOLS,  "force_semantic": False},
    "C_both":   {"tools": GREP_TOOLS + LSP_TOOLS[:-1], "force_semantic": False},
    "D_forced": {"tools": GREP_TOOLS + LSP_TOOLS[:-1], "force_semantic": True},
}

SYS_BASE = """You are a code-navigation agent. You are given a bug report for the `requests` Python library.
Your ONLY goal is to identify which source file(s) must be modified to fix it — this is a LOCALIZATION task.
Do NOT write the fix. Explore the repository with the provided tools, then call submit_localization with the file path(s).
Be efficient: use the fewest tool calls needed to be confident. Repo-relative paths look like 'requests/sessions.py'."""
SYS_FORCE = "\nIMPORTANT: Prefer semantic navigation (document_symbols / find_references / find_definition) BEFORE falling back to text search."


def run_tool(name, inp, lsp):
    if name == "search_text":
        return tool_search_text(inp["query"])
    if name == "read_file":
        return tool_read_file(inp["relpath"], inp.get("start", 1), inp.get("end"))
    if name == "document_symbols":
        syms = lsp.document_symbols(inp["relpath"])
        return json.dumps(_flatten_symbols(syms), indent=0)[:4000] or "(none)"
    if name == "find_references":
        locs = lsp.references(inp["relpath"], inp["line"] - 1, inp["character"])
        return "\n".join(loc_to_str(L) for L in locs) or "(no references)"
    if name == "find_definition":
        locs = lsp.definition(inp["relpath"], inp["line"] - 1, inp["character"])
        return "\n".join(loc_to_str(L) for L in locs) or "(no definition)"
    return f"(unknown tool {name})"


def _normalize_files(val):
    """The model sometimes passes 'files' as a bare string instead of a list, or as a
    delimiter-separated string. Normalize to a clean list of repo-relative paths.
    Without this, set('a/b.py') iterates into single characters and corrupts the verifier."""
    if val is None:
        return []
    if isinstance(val, str):
        parts = re.split(r"[\s,]+", val.strip())
        return [p for p in parts if p]
    out = []
    for item in val:
        if isinstance(item, str):
            parts = re.split(r"[\s,]+", item.strip())
            out.extend(p for p in parts if p)
    return out


def _flatten_symbols(syms, depth=0):
    out = []
    for s in syms:
        name = s.get("name")
        kind = s.get("kind")
        rng = s.get("range") or s.get("location", {}).get("range", {})
        ln = rng.get("start", {}).get("line", -1) + 1
        out.append({"name": name, "kind": kind, "line": ln})
        for c in s.get("children", []) or []:
            out.extend(_flatten_symbols([c], depth + 1))
    return out


def run_episode(instance, arm_name, max_turns=20, verbose=False, model=None):
    """One rollout. Returns dict with tokens, success, tool calls."""
    model = model or MODEL
    arm = ARMS[arm_name]
    tools = arm["tools"] + [SUBMIT_TOOL]
    sys_prompt = SYS_BASE + (SYS_FORCE if arm["force_semantic"] else "")
    lsp = None
    if any(t["name"] in ("find_references", "find_definition", "document_symbols") for t in tools):
        lsp = LSP(PYLSP_CMD, REPO_DIR)
        lsp.initialize()

    user = f"Bug report:\n\n{instance['problem_statement'][:4000]}\n\nIdentify the file(s) to modify."
    messages = [{"role": "user", "content": user}]
    tok_in = tok_out = 0
    tool_calls = []
    submitted = None
    t0 = time.monotonic()
    try:
        for turn in range(max_turns):
            resp = CLIENT.messages.create(model=model, max_tokens=1024,
                                          system=sys_prompt, tools=tools, messages=messages)
            tok_in += resp.usage.input_tokens
            tok_out += resp.usage.output_tokens
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            stop = False
            for block in resp.content:
                if block.type == "tool_use":
                    tool_calls.append(block.name)
                    if block.name == "submit_localization":
                        submitted = _normalize_files(block.input.get("files", []))
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                             "content": "Recorded. Done."})
                        stop = True
                    else:
                        try:
                            out = run_tool(block.name, block.input, lsp)
                        except Exception as e:
                            out = f"(tool error: {type(e).__name__}: {e})"
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                             "content": out[:6000]})
                        if verbose:
                            print(f"    [{arm_name}] {block.name}({block.input}) -> {out[:80]!r}")
            if stop:
                break
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                break  # model produced no tool call and didn't submit
    finally:
        if lsp:
            lsp.shutdown()

    # verify localization against gold-patch files
    gold = set(re.findall(r'^\+\+\+ b/(.+)$', instance["patch"], re.M))
    got = set(submitted or [])
    # success = the gold file is among submitted (exact path match)
    success = bool(gold & got)
    return {
        "instance": instance["instance_id"], "arm": arm_name, "model": model,
        "success": success, "gold": sorted(gold), "submitted": sorted(got),
        "tokens_in": tok_in, "tokens_out": tok_out, "tokens_total": tok_in + tok_out,
        "tool_calls": tool_calls, "n_tool_calls": len(tool_calls),
        "turns": turn + 1, "seconds": round(time.monotonic() - t0, 1),
    }


def checkout(base_commit):
    subprocess.run(["git", "-C", REPO_DIR, "checkout", "--quiet", base_commit],
                   check=True, capture_output=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="A_grep,B_lsp")
    ap.add_argument("--rollouts", type=int, default=1)
    ap.add_argument("--limit", type=int, default=2, help="number of instances")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--out", default="v0_results.jsonl", help="output filename under runs/")
    ap.add_argument("--model", default=MODEL, help="model id (e.g. claude-haiku-4-5)")
    args = ap.parse_args()

    from datasets import load_dataset
    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    insts = [x for x in ds if x["repo"] == "psf/requests"][:args.limit]
    arms = args.arms.split(",")
    outpath = os.path.join(HARNESS_DIR, "runs", args.out)
    results = []
    # incremental write: each episode flushed immediately so a crash preserves progress
    with open(outpath, "w") as fout:
        for inst in insts:
            checkout(inst["base_commit"])
            for arm in arms:
                for r in range(args.rollouts):
                    try:
                        res = run_episode(inst, arm, verbose=args.verbose, model=args.model)
                    except Exception as e:
                        res = {"instance": inst["instance_id"], "arm": arm, "rollout": r,
                               "error": f"{type(e).__name__}: {e}", "success": False,
                               "tokens_total": 0, "n_tool_calls": 0, "turns": 0}
                    results.append(res)
                    fout.write(json.dumps(res) + "\n")
                    fout.flush()
                    err = res.get("error", "")
                    print(f"{res['instance']:28s} {arm:9s} r{r} "
                          f"success={res.get('success')!s:5s} tok={res.get('tokens_total',0):6d} "
                          f"calls={res.get('n_tool_calls',0):2d} turns={res.get('turns',0):2d} "
                          f"{'ERR='+err if err else 'got='+str(res.get('submitted'))}",
                          flush=True)
    checkout("main")
    print(f"\nwrote {len(results)} results to {outpath}", flush=True)
