#!/usr/bin/env python3
"""LSP smoke test: drive typescript-language-server over stdio JSON-RPC.
Proves the arm-B keystone: initialize -> didOpen -> references -> resolved locations.
"""
import json, os, subprocess, threading, time, sys

LSP_BIN = os.path.join(os.path.dirname(__file__), "node_modules", ".bin", "typescript-language-server")

class LSP:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.p = subprocess.Popen(
            [LSP_BIN, "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._id = 0
        self._resp = {}
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        buf = b""
        f = self.p.stdout
        while True:
            # read headers
            header = b""
            while b"\r\n\r\n" not in header:
                ch = f.read(1)
                if not ch:
                    return
                header += ch
            # parse content-length
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

    def _send(self, method, params, is_notification=False):
        with self._lock:
            self._id += 1
            mid = self._id
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if not is_notification:
            msg["id"] = mid
        data = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        self.p.stdin.write(header + data)
        self.p.stdin.flush()
        return mid

    def request(self, method, params, timeout=20):
        mid = self._send(method, params)
        deadline = time.monotonic() + timeout
        with self._cv:
            while mid not in self._resp:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{method} timed out")
                self._cv.wait(remaining)
            return self._resp.pop(mid)

    def notify(self, method, params):
        self._send(method, params, is_notification=True)

    def uri(self, relpath):
        return "file://" + os.path.join(self.root, relpath)

    def initialize(self):
        r = self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": "file://" + self.root,
            "capabilities": {},
        })
        self.notify("initialized", {})
        return r

    def did_open(self, relpath):
        path = os.path.join(self.root, relpath)
        with open(path) as fh:
            text = fh.read()
        self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": self.uri(relpath),
                "languageId": "typescript",
                "version": 1,
                "text": text,
            }
        })
        return text

    def references(self, relpath, line, char):
        return self.request("textDocument/references", {
            "textDocument": {"uri": self.uri(relpath)},
            "position": {"line": line, "character": char},
            "context": {"includeDeclaration": True},
        })

    def shutdown(self):
        try:
            self.request("shutdown", None, timeout=5)
            self.notify("exit", None)
        except Exception:
            pass
        self.p.terminate()


# ---- build a tiny throwaway TS project ----
SCRATCH = os.path.join(os.path.dirname(__file__), "_lsp_scratch")
os.makedirs(SCRATCH, exist_ok=True)
with open(os.path.join(SCRATCH, "tsconfig.json"), "w") as f:
    json.dump({"compilerOptions": {"strict": True, "module": "commonjs", "target": "es2020"},
               "include": ["*.ts"]}, f)
with open(os.path.join(SCRATCH, "math.ts"), "w") as f:
    f.write("export function addNumbers(a: number, b: number): number {\n  return a + b;\n}\n")
with open(os.path.join(SCRATCH, "app.ts"), "w") as f:
    f.write(
        "import { addNumbers } from './math';\n"
        "// addNumbers in a comment should NOT count as a real reference\n"
        "const s = 'addNumbers in a string literal';\n"
        "console.log(addNumbers(1, 2));\n"
        "console.log(addNumbers(3, 4));\n"
    )

if __name__ == "__main__":
    lsp = LSP(SCRATCH)
    try:
        init = lsp.initialize()
        print("initialize ok:", "result" in init)
        lsp.did_open("math.ts")
        lsp.did_open("app.ts")
        time.sleep(2.0)  # let the server index
        # addNumbers is defined at math.ts line 0, char 16 ("function addNumbers")
        refs = lsp.references("math.ts", 0, 16)
        locs = refs.get("result") or []
        print(f"references found: {len(locs)}")
        for L in locs:
            uri = L["uri"].split("/")[-1]
            ln = L["range"]["start"]["line"]
            print(f"  {uri}:{ln}")
        # The KEY semantic point: comment + string mention are NOT in references,
        # but a naive grep for 'addNumbers' would return 5 hits (def + 2 calls + comment + string).
        print("EXPECT: 3 real refs (def + 2 calls); grep would over-report 5.")
    finally:
        lsp.shutdown()
