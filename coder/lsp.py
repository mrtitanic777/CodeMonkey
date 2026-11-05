"""Code intelligence: what the compiler and type checker say, while the agent works.

A language server (pyright, typescript-language-server, gopls, rust-analyzer, clangd...) knows
the project the way an editor does: errors and warnings, where a name is defined, every place
it is used, its type. This module talks to one over stdio (LSP: JSON-RPC with Content-Length
headers), starting it the first time a file of its language is checked and keeping it for the
session. Tools:

    diagnostics(path)                       errors and warnings in a file
    lsp_definition(path, line, symbol)      where the name on that line is defined
    lsp_references(path, line, symbol)      every place it is used
    lsp_hover(path, line, symbol)           its type and documentation
    lsp_symbols(path)                       the file's outline: classes, functions, ...

Lines are numbered from 1 as read shows them; ``symbol`` is the name on that line (models count
columns badly, so the column is found from the name). After the agent edits a file, its errors
are added to the result, so a broken change is seen at once rather than at the next test run.

With no language server installed for a language, diagnostics still checks what it can: Python
by compiling it and running pyflakes when it is installed, JavaScript with ``node --check``,
JSON by parsing it. Servers can be set per language in config.yaml (coder.lsp).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

#: language -> (file extensions, candidate commands, the first one found is used)
SERVERS: Dict[str, Tuple[Tuple[str, ...], List[List[str]]]] = {
    "python": ((".py", ".pyi"), [["pyright-langserver", "--stdio"], ["basedpyright-langserver", "--stdio"], ["pylsp"]]),
    "typescript": ((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"), [["typescript-language-server", "--stdio"]]),
    "go": ((".go",), [["gopls"]]),
    "rust": ((".rs",), [["rust-analyzer"]]),
    "c": ((".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh"), [["clangd"]]),
    "csharp": ((".cs",), [["csharp-ls"]]),
    "java": ((".java",), [["jdtls"]]),
    "lua": ((".lua",), [["lua-language-server"]]),
}
_LANG_ID = {".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "typescriptreact", ".js": "javascript",
            ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript", ".go": "go", ".rs": "rust",
            ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
            ".cs": "csharp", ".java": "java", ".lua": "lua"}
_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
_SYMBOL_KIND = {1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method", 7: "property",
                8: "field", 9: "constructor", 10: "enum", 11: "interface", 12: "function", 13: "variable",
                14: "constant", 22: "enum member", 23: "struct", 26: "type parameter"}
INSTALL_HINT = {
    "python": "npm install -g pyright", "typescript": "npm install -g typescript typescript-language-server",
    "go": "go install golang.org/x/tools/gopls@latest", "rust": "rustup component add rust-analyzer",
    "c": "install clangd (LLVM)", "csharp": "dotnet tool install -g csharp-ls", "java": "install Eclipse jdtls",
    "lua": "install lua-language-server",
}


def language_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    for lang, (exts, _cmds) in SERVERS.items():
        if ext in exts:
            return lang
    return ""


def uri(path: str) -> str:
    return "file:" + urllib.request.pathname2url(os.path.abspath(path))


def path_of(file_uri: str) -> str:
    return os.path.normpath(urllib.request.url2pathname(urllib.parse.urlparse(file_uri).path))


class LSPError(RuntimeError):
    pass


class _Server:
    """One running language server for one project folder."""

    def __init__(self, argv: List[str], root: str, language: str) -> None:
        self.argv, self.root, self.language = argv, root, language
        self.diagnostics: Dict[str, Tuple[int, List[Dict[str, Any]]]] = {}   # uri -> (version, items)
        self.versions: Dict[str, int] = {}
        self.texts: Dict[str, str] = {}
        self.changed = threading.Condition()
        self._ids = 0
        self._pending: Dict[int, "queue.Queue"] = {}
        self._write = threading.Lock()
        self.log: List[str] = []
        exe = shutil.which(argv[0]) or argv[0]
        try:
            self.proc = subprocess.Popen([exe] + argv[1:], cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise LSPError(f"could not start {argv[0]}: {exc}") from exc
        threading.Thread(target=self._read, daemon=True, name=f"lsp-{language}").start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self.request("initialize", {
            "processId": os.getpid(), "rootUri": uri(root), "rootPath": root,
            "workspaceFolders": [{"uri": uri(root), "name": os.path.basename(root) or root}],
            "capabilities": {
                "textDocument": {"publishDiagnostics": {"relatedInformation": False, "versionSupport": True},
                                 "synchronization": {"didSave": True},
                                 "hover": {"contentFormat": ["plaintext", "markdown"]},
                                 "definition": {}, "references": {},
                                 "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}},
                "workspace": {"configuration": True, "workspaceFolders": True},
            },
        }, timeout=60)
        self.notify("initialized", {})

    # ------------------------------------------------------------ wire ---

    def _send(self, message: Dict[str, Any]) -> None:
        body = json.dumps(message).encode("utf-8")
        with self._write:
            try:
                self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise LSPError(f"{self.argv[0]} is not running: {exc}") from exc

    def _read(self) -> None:
        out = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = out.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                if length is None:
                    continue
                self._dispatch(json.loads(out.read(length)))
        except (OSError, ValueError):
            return
        finally:
            with self.changed:
                self.changed.notify_all()

    def _read_err(self) -> None:
        try:
            for line in self.proc.stderr:
                self.log = (self.log + [line.decode("utf-8", "replace").rstrip()])[-40:]
        except (OSError, ValueError):
            pass

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        if "method" in msg and "id" in msg:            # the server asks us something
            method = msg["method"]
            if method == "workspace/configuration":
                result: Any = [{} for _ in (msg.get("params") or {}).get("items") or [None]]
            elif method == "workspace/workspaceFolders":
                result = [{"uri": uri(self.root), "name": os.path.basename(self.root)}]
            else:                                        # registerCapability, workDoneProgress/create, ...
                result = None
            try:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
            except LSPError:
                pass
            return
        if msg.get("method") == "textDocument/publishDiagnostics":
            params = msg.get("params") or {}
            key = params.get("uri", "")
            with self.changed:
                self.diagnostics[key] = (int(params.get("version") or self.versions.get(key, 0)),
                                         params.get("diagnostics") or [])
                self.changed.notify_all()
            return
        q = self._pending.get(msg.get("id"))
        if q is not None:
            q.put(msg)

    def request(self, method: str, params: Dict[str, Any], timeout: float = 20) -> Any:
        self._ids += 1
        msg_id = self._ids
        q: "queue.Queue" = queue.Queue()
        self._pending[msg_id] = q
        try:
            self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
            try:
                reply = q.get(timeout=timeout)
            except queue.Empty:
                raise LSPError(f"{self.argv[0]} gave no answer to {method} in {timeout:.0f}s") from None
        finally:
            self._pending.pop(msg_id, None)
        if "error" in reply:
            raise LSPError(f"{method}: {(reply['error'] or {}).get('message')}")
        return reply.get("result")

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ------------------------------------------------------------ files ---

    def sync(self, path: str) -> str:
        """Tell the server the file's current text. Returns its uri."""
        key = uri(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            raise LSPError(f"cannot read {path}: {exc}") from exc
        if key not in self.versions:
            self.versions[key] = 1
            self.notify("textDocument/didOpen", {"textDocument": {
                "uri": key, "languageId": _LANG_ID.get(os.path.splitext(path)[1].lower(), self.language),
                "version": 1, "text": text}})
        elif self.texts.get(key) != text:
            self.versions[key] += 1
            self.notify("textDocument/didChange", {"textDocument": {"uri": key, "version": self.versions[key]},
                                                   "contentChanges": [{"text": text}]})
            self.notify("textDocument/didSave", {"textDocument": {"uri": key}})
        self.texts[key] = text
        return key

    def diagnose(self, path: str, wait: float = 8.0) -> List[Dict[str, Any]]:
        key = self.sync(path)
        version = self.versions[key]
        deadline = time.monotonic() + wait
        with self.changed:
            while True:
                got = self.diagnostics.get(key)
                if got is not None and got[0] >= version:
                    return got[1]
                left = deadline - time.monotonic()
                if left <= 0 or self.proc.poll() is not None:
                    return got[1] if got else []
                self.changed.wait(min(left, 0.5))

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.request("shutdown", {}, timeout=3)
                self.notify("exit", {})
            except LSPError:
                pass
        try:
            self.proc.wait(timeout=3)          # it holds the project folder open until it is gone
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass


class LSP:
    """The language servers of one project folder, started when first needed."""

    def __init__(self, project_dir: str, overrides: Optional[Dict[str, Any]] = None) -> None:
        self.root = os.path.abspath(project_dir)
        self.overrides = overrides or {}
        self.servers: Dict[str, Optional[_Server]] = {}
        self.errors: Dict[str, str] = {}
        self._lock = threading.Lock()

    def command(self, language: str) -> Optional[List[str]]:
        own = self.overrides.get(language)
        if isinstance(own, list) and own:
            return [str(x) for x in own]
        if isinstance(own, str) and own.strip():
            return own.split()
        for argv in SERVERS.get(language, ((), []))[1]:
            if shutil.which(argv[0]):
                return argv
        return None

    def server(self, path: str) -> Optional[_Server]:
        language = language_of(path)
        if not language:
            return None
        with self._lock:
            if language in self.servers:
                srv = self.servers[language]
                if srv is None or srv.proc.poll() is None:
                    return srv
            argv = self.command(language)
            srv = None
            if argv:
                try:
                    srv = _Server(argv, self.root, language)
                except LSPError as exc:
                    self.errors[language] = str(exc)
            self.servers[language] = srv
            return srv

    def status(self) -> List[Dict[str, Any]]:
        out = []
        for language in SERVERS:
            argv = self.command(language)
            srv = self.servers.get(language)
            out.append({"language": language, "server": " ".join(argv) if argv else "",
                        "running": bool(srv and srv.proc.poll() is None), "error": self.errors.get(language, ""),
                        "install": INSTALL_HINT.get(language, "")})
        return out

    def close(self) -> None:
        for srv in self.servers.values():
            if srv is not None:
                srv.close()

    # ------------------------------------------------------------ tools ---

    def _path(self, path: str) -> str:
        full = os.path.abspath(os.path.join(self.root, path))
        if not (full == self.root or full.startswith(self.root + os.sep)):
            raise LSPError(f"{path!r} is outside the project")
        if not os.path.isfile(full):
            raise LSPError(f"file not found: {path!r}")
        return full

    def diagnostics(self, path: str, errors_only: bool = False) -> str:
        full = self._path(path)
        srv = self.server(full)
        if srv is None:
            items = fallback_check(full)
            source = "built-in check" if items is not None else ""
            if items is None:
                lang = language_of(full)
                return (f"No checker for {os.path.basename(full)}" +
                        (f": install a {lang} language server ({INSTALL_HINT.get(lang)})." if lang else "."))
        else:
            items = [{"line": d["range"]["start"]["line"] + 1, "col": d["range"]["start"]["character"] + 1,
                      "severity": _SEVERITY.get(d.get("severity", 1), "error"), "message": d.get("message", ""),
                      "code": d.get("code", ""), "source": d.get("source", srv.argv[0])} for d in srv.diagnose(full)]
            source = srv.argv[0]
        if errors_only:
            items = [i for i in items if i["severity"] == "error"]
        rel = os.path.relpath(full, self.root).replace("\\", "/")
        if not items:
            return f"{rel}: no {'errors' if errors_only else 'problems'} ({source})."
        lines = [f"{rel}: {len(items)} {'error' if errors_only else 'problem'}{'s' if len(items) != 1 else ''} ({source})"]
        for i in sorted(items, key=lambda x: (x["severity"] != "error", x["line"]))[:30]:
            code = f" [{i['code']}]" if i.get("code") else ""
            lines.append(f"  {rel}:{i['line']}:{i['col']} {i['severity']}{code}: {i['message'].splitlines()[0][:300]}")
        if len(items) > 30:
            lines.append(f"  ... and {len(items) - 30} more")
        return "\n".join(lines)

    def _position(self, full: str, line: Any, symbol: str) -> Dict[str, int]:
        try:
            number = int(line)
        except (TypeError, ValueError):
            raise LSPError("line must be a line number (from 1)") from None
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            rows = fh.read().splitlines()
        if not 1 <= number <= len(rows):
            raise LSPError(f"line {number} is outside the file ({len(rows)} lines)")
        text = rows[number - 1]
        col = text.find(symbol) if symbol else len(text) - len(text.lstrip())
        if symbol and col < 0:
            raise LSPError(f"{symbol!r} is not on line {number}: {text.strip()[:120]!r}")
        return {"line": number - 1, "character": col}

    def _where(self, location: Dict[str, Any]) -> str:
        target = location.get("uri") or location.get("targetUri", "")
        rng = location.get("range") or location.get("targetSelectionRange") or {}
        start = rng.get("start") or {}
        path = path_of(target)
        rel = os.path.relpath(path, self.root).replace("\\", "/") if path.startswith(self.root) else path
        line = start.get("line", 0) + 1
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                code = fh.read().splitlines()[line - 1].strip()
        except (OSError, IndexError):
            code = ""
        return f"{rel}:{line}" + (f"  {code[:160]}" if code else "")

    def _ask(self, method: str, path: str, line: Any, symbol: str, extra: Optional[Dict[str, Any]] = None) -> Any:
        full = self._path(path)
        srv = self.server(full)
        if srv is None:
            lang = language_of(full)
            raise LSPError(f"no language server for {os.path.basename(full)}" +
                           (f": install one ({INSTALL_HINT.get(lang)}), or use code_callers / grep" if lang else ""))
        srv.sync(full)
        params = {"textDocument": {"uri": uri(full)}, "position": self._position(full, line, symbol)}
        params.update(extra or {})
        return srv.request(method, params)

    def definition(self, path: str, line: Any, symbol: str = "") -> str:
        full = self._path(path)
        if self.server(full) is None and language_of(full) == "python":
            return self._py_definition(full, line, symbol)
        result = self._ask("textDocument/definition", path, line, symbol)
        found = result if isinstance(result, list) else [result] if result else []
        return "\n".join(self._where(x) for x in found[:20]) or f"No definition found for {symbol or 'that'}."

    def _py_definition(self, full: str, line: Any, symbol: str) -> str:
        """Where a Python name is defined, found by parsing (no type information): this file first,
        then the project. Imports are not followed, so a name defined twice lists both."""
        import re

        if not symbol:
            pos = self._position(full, line, "")
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read().splitlines()[pos["line"]]
            words = re.findall(r"[A-Za-z_]\w*", text)
            symbol = words[0] if words else ""
            if not symbol:
                raise LSPError("give the symbol: the name on that line to look up")
        else:
            self._position(full, line, symbol)
        name = symbol.split(".")[-1]
        found: List[str] = []

        def look(file: str) -> None:
            for _, kind, defined, n in _py_outline(file):
                if defined == name and len(found) < 20:
                    rel = os.path.relpath(file, self.root).replace("\\", "/")
                    found.append(f"{rel}:{n}  {kind} {defined}")
        look(full)
        if not found:
            skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "references", "dist", "build"}
            seen = 0
            for top, dirs, files in os.walk(self.root):
                dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
                for f in files:
                    if f.endswith((".py", ".pyi")) and os.path.join(top, f) != full:
                        seen += 1
                        look(os.path.join(top, f))
                if seen > 4000 or len(found) >= 20:
                    break
        note = "\n(found by parsing; install pyright for type-aware answers: npm install -g pyright)"
        return ("\n".join(found) + note) if found else f"No definition of {name} found in the project{note}"

    def references(self, path: str, line: Any, symbol: str = "") -> str:
        result = self._ask("textDocument/references", path, line, symbol, {"context": {"includeDeclaration": True}}) or []
        rows = [self._where(x) for x in result[:80]]
        more = f"\n... and {len(result) - 80} more" if len(result) > 80 else ""
        return (f"{len(result)} reference(s) to {symbol or 'that'}:\n" + "\n".join(rows) + more) if rows else \
            f"No references found for {symbol or 'that'}."

    def hover(self, path: str, line: Any, symbol: str = "") -> str:
        result = self._ask("textDocument/hover", path, line, symbol) or {}
        contents = result.get("contents") if isinstance(result, dict) else None
        if isinstance(contents, dict):
            text = contents.get("value", "")
        elif isinstance(contents, list):
            text = "\n".join(c.get("value", "") if isinstance(c, dict) else str(c) for c in contents)
        else:
            text = str(contents or "")
        return text.strip()[:4000] or f"Nothing known about {symbol or 'that'}."

    def symbols(self, path: str) -> str:
        full = self._path(path)
        srv = self.server(full)
        if srv is None and language_of(full) == "python":
            return "\n".join(f"{'  ' * d}{kind} {name}  (line {n})" for d, kind, name, n in _py_outline(full)[:300]) \
                or "No symbols."
        if srv is None:
            raise LSPError(f"no language server for {os.path.basename(full)}; read the file instead")
        srv.sync(full)
        result = srv.request("textDocument/documentSymbol", {"textDocument": {"uri": uri(full)}}) or []
        rows: List[str] = []

        def walk(items: List[Dict[str, Any]], depth: int) -> None:
            for s in items:
                rng = (s.get("selectionRange") or s.get("range") or (s.get("location") or {}).get("range") or {})
                line = (rng.get("start") or {}).get("line", 0) + 1
                rows.append(f"{'  ' * depth}{_SYMBOL_KIND.get(s.get('kind'), 'symbol')} {s.get('name')}  (line {line})")
                walk(s.get("children") or [], depth + 1)
        walk(result, 0)
        return "\n".join(rows[:300]) or "No symbols."


def _py_outline(path: str) -> List[Tuple[int, str, str, int]]:
    """(depth, kind, name, line) for the classes, functions and module-level names in a Python file."""
    import ast

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return []
    rows: List[Tuple[int, str, str, int]] = []

    def walk(body: List[Any], depth: int, in_class: bool) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                rows.append((depth, "class", node.name, node.lineno))
                walk(node.body, depth + 1, True)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rows.append((depth, "method" if in_class else "function", node.name, node.lineno))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and depth == 0:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        rows.append((0, "variable", t.id, node.lineno))
    walk(tree.body, 0, False)
    return rows


def fallback_check(path: str) -> Optional[List[Dict[str, Any]]]:
    """Problems found without a language server, or None when there is no check for this kind of file."""
    ext = os.path.splitext(path)[1].lower()
    items: List[Dict[str, Any]] = []
    if ext == ".py":
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            compile(source, path, "exec")
        except SyntaxError as exc:
            return [{"line": exc.lineno or 1, "col": exc.offset or 1, "severity": "error", "message": exc.msg,
                     "code": "syntax", "source": "python"}]
        try:
            proc = subprocess.run([sys.executable, "-m", "pyflakes", path], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            return items
        if "No module named pyflakes" in proc.stderr:
            return items
        for line in proc.stdout.splitlines():
            # path:line:col: message   (the path may hold a drive letter's colon)
            rest = line[len(path):] if line.startswith(path) else line.split(":", 1)[-1]
            parts = rest.lstrip(":").split(":", 2)
            if len(parts) == 3 and parts[0].isdigit():
                message = parts[2].strip()
                severity = "error" if "undefined name" in message or "invalid syntax" in message else "warning"
                items.append({"line": int(parts[0]), "col": int(parts[1]) if parts[1].isdigit() else 1,
                              "severity": severity, "message": message, "code": "", "source": "pyflakes"})
        return items
    if ext in (".js", ".mjs", ".cjs") and shutil.which("node"):
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            first = [ln for ln in proc.stderr.splitlines() if ln.strip()]
            line = 1
            if first and ":" in first[0]:
                tail = first[0].rsplit(":", 1)[-1]
                line = int(tail) if tail.isdigit() else 1
            msg = next((ln for ln in first if "Error" in ln), first[-1] if first else "syntax error")
            items.append({"line": line, "col": 1, "severity": "error", "message": msg, "code": "", "source": "node"})
        return items
    if ext == ".json":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                json.load(fh)
        except ValueError as exc:
            return [{"line": getattr(exc, "lineno", 1), "col": getattr(exc, "colno", 1), "severity": "error",
                     "message": str(exc), "code": "", "source": "json"}]
        except OSError:
            pass
        return items
    return None


def lsp_tools(lsp: LSP) -> List[Any]:
    from tools.registry import Tool

    def wrap(fn):
        def run(**kwargs):
            try:
                return fn(**kwargs)
            except LSPError as exc:
                return f"ERROR: {exc}"
        return run

    prop = lambda d: {"type": "string", "description": d}  # noqa: E731
    where = {"path": prop("Project-relative file."), "line": prop("Line number, from 1 (as read shows)."),
             "symbol": prop("The name on that line to ask about.")}
    return [
        Tool(name="diagnostics", description=("Errors and warnings in a file, from its language server (type errors, "
             "undefined names, bad imports) or a built-in check. Use it after a change instead of guessing."),
             parameters={"type": "object", "properties": {"path": prop("Project-relative file.")}, "required": ["path"]},
             func=wrap(lambda path: lsp.diagnostics(path))),
        Tool(name="lsp_definition", description="Where a name is defined: give the file, the line it is on, and the name.",
             parameters={"type": "object", "properties": where, "required": ["path", "line", "symbol"]},
             func=wrap(lambda path, line, symbol="": lsp.definition(path, line, symbol))),
        Tool(name="lsp_references", description="Every place a name is used, across the project.",
             parameters={"type": "object", "properties": where, "required": ["path", "line", "symbol"]},
             func=wrap(lambda path, line, symbol="": lsp.references(path, line, symbol))),
        Tool(name="lsp_hover", description="A name's type, signature and documentation.",
             parameters={"type": "object", "properties": where, "required": ["path", "line", "symbol"]},
             func=wrap(lambda path, line, symbol="": lsp.hover(path, line, symbol))),
        Tool(name="lsp_symbols", description="A file's outline: its classes, functions and other symbols, with lines.",
             parameters={"type": "object", "properties": {"path": prop("Project-relative file.")}, "required": ["path"]},
             func=wrap(lambda path: lsp.symbols(path))),
    ]
