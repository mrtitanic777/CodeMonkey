"""Local models: what fits this computer, how to place it, and running it.

A model does not have to *fit* in fast memory; it has to be *placed*. A mixture-of-experts
model is large on disk and small per token: the shared layers go on the graphics card, the
experts stay in ordinary memory, and it runs at the speed of a much smaller model. A dense
model that does not fit the card whole is a different story: every layer is needed for every
token, and whatever is off the card is slow.

So this module looks at the machine (graphics memory, memory, free disk), holds a short list of
models with what is known about each, and says for each one, in plain words:

    whether it runs here and how (all on the card, card plus memory, or not at all, and why)
    the command that starts it, with the placement worked out for this machine
    what was reported about it and by whom, kept apart from what was measured here

It can also fetch a model file when you press Download (the file, where it comes from and its
size are shown first; an interrupted download carries on from where it stopped; the file is
checked against the publisher's checksum) and start or stop a `llama-server` you have
installed. Nothing here is downloaded or started on its own.

The ideas (look at the machine, recommend what fits and say why something does not, resumable
downloads, start/stop/status/logs, measurements named with their machine) follow the colibri
project's setup. The placement flags for mixture-of-experts models follow llama.cpp's
`--n-cpu-moe`. The code is written here.

Every number in the catalog is either copied from the publisher's page (and marked as theirs)
or measured on this machine. Estimates are called estimates.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

PORT = 8090            # the local model server (the app itself usually has 8080)
HUB = "https://huggingface.co"

CATALOG: List[Dict[str, Any]] = [
    {
        "id": "qwen38-35b-a3b-q4km", "name": "Qwen3.8-35B-A3B Distill", "quant": "Q4_K_M",
        "kind": "moe", "params_b": 35, "active_b": 3, "layers": 40,
        "file": "Qwen3.8-35B-A3B-Q4_K_M.gguf", "size_gb": 21.71, "bytes": 21713462944,
        "sha256": "196103269085bc54c9b8f49ed21e9f53e1b56b465e8b796c6d8e31e06f63cfa5",
        "repo": "empero-ai/Qwen3.8-35B-A3B-Distill-GGUF", "license": "Apache-2.0",
        "runner": "stock", "runner_note": "needs a recent llama.cpp (Qwen3.6 mixture-of-experts support)",
        "context": 262144, "thinking": True,
        "sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20},
        "issues_as_of": "2026-10-12",
        "issues": ["Every answer opens with a thinking block, so a finished answer takes longer than the tokens-a-second figure suggests."],
        "measured": "first run on this computer, 2026-10-12, while other work held 20 GB of memory and the processor: "
                    "loaded in 52 seconds, 9.2 GB of graphics memory, about 5 tokens a second, and free memory fell to "
                    "0.1 GB, so it was stopped. Not a fair speed figure: it needs about 13 GB of free memory",
        "match": ["qwen3.8-35b-a3b", "qwen3.6-35b-a3b"],
        "reported": "about 50 tokens a second on an RTX 3060 12 GB with 16 GB of memory (one user's post); "
                    "30 on an RTX 3070 8 GB for the base model (colibri's benchmark table)",
    },
    {
        "id": "qwen38-35b-a3b-iq4xs", "name": "Qwen3.8-35B-A3B Distill", "quant": "IQ4_XS",
        "kind": "moe", "params_b": 35, "active_b": 3, "layers": 40,
        "file": "Qwen3.8-35B-A3B-IQ4_XS.gguf", "size_gb": 19.63, "bytes": 19627788160,
        "sha256": "b645af45431ef9b41f43cae51c9323b2d0ca84f23031d483d2105c643ae58d65",
        "repo": "empero-ai/Qwen3.8-35B-A3B-Distill-GGUF", "license": "Apache-2.0",
        "runner": "stock", "runner_note": "needs a recent llama.cpp (Qwen3.6 mixture-of-experts support)",
        "context": 262144, "thinking": True,
        "sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20},
        "issues_as_of": "2026-10-12",
        "issues": ["Every answer opens with a thinking block, so a finished answer takes longer than the tokens-a-second figure suggests."],
        "match": [],
        "reported": "the publisher calls it near the Q4_K_M file's quality at about 2 GB less",
    },
    {
        "id": "bonsai2-27b-ptq1", "name": "Ternary Bonsai 2 27B", "quant": "PTQ1_0 (1.75 bits)",
        "kind": "dense", "params_b": 27, "active_b": 27, "layers": 64,
        "file": "Ternary-Bonsai-2-27B-PTQ1_0.gguf", "size_gb": 5.95, "bytes": 5946648928,
        "sha256": "53107f530aa52eb00912263ab1ee29bd199261c87cd7b4ad4ca1318c1fe33ee3",
        "repo": "prism-ml/Ternary-Bonsai-2-27B-gguf", "license": "Apache-2.0",
        "runner": "fork", "runner_note": "needs PrismML's build of llama.cpp (github.com/PrismML-Eng/llama.cpp); "
                                         "the ordinary llama.cpp cannot load this format",
        "context": 262144, "thinking": True,
        "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.05},
        "efforts": {"off": "none", "low": "medium", "medium": "medium", "high": "xhigh"},
        "budget_field": "thinking_budget_tokens", "budgets": {"low": 1024},
        "issues_as_of": "2026-09-23",
        "issues": [
            "It thinks before it answers, and the thinking counts against the output limit: with a small limit the answer comes back empty.",
            "It accepts the effort words low, medium and xhigh; \"high\" is answered with an error, and \"low\" thinks about as long as the default.",
            "Tool calls sometimes come out malformed or repeat themselves; the publisher lists this as open.",
            "It needs exactly one system message, at the start.",
        ],
        "advice": "The smaller packing: pick it where memory is tightest. The publisher measures the other packing as about twice as fast at reading prompts on every card, which matters most for an agent.",
        "match": ["bonsai-2-27b", "bonsai2-27b"],
        "reported": "98.2% of the full-precision model's score on the publisher's own 14 benchmarks, and about 47 "
                    "tokens a second on an Apple M5 Max (the publisher's figures)",
    },
    {
        "id": "bonsai2-27b-pq2", "name": "Ternary Bonsai 2 27B", "quant": "PQ2_0 (2.13 bits)",
        "kind": "dense", "params_b": 27, "active_b": 27, "layers": 64,
        "file": "Ternary-Bonsai-2-27B-PQ2_0.gguf", "size_gb": 7.21, "bytes": 7206168928,
        "sha256": "3907dc1658db1f78a9826bf8d5bcb8dc65db0d466388937af57f2294fae62ec1",
        "repo": "prism-ml/Ternary-Bonsai-2-27B-gguf", "license": "Apache-2.0",
        "runner": "fork", "runner_note": "needs PrismML's build of llama.cpp (github.com/PrismML-Eng/llama.cpp); "
                                         "the ordinary llama.cpp cannot load this format",
        "context": 262144, "thinking": True,
        "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.05},
        "efforts": {"off": "none", "low": "medium", "medium": "medium", "high": "xhigh"},
        "budget_field": "thinking_budget_tokens", "budgets": {"low": 1024},
        "issues_as_of": "2026-09-23",
        "issues": [
            "It thinks before it answers, and the thinking counts against the output limit: with a small limit the answer comes back empty.",
            "It accepts the effort words low, medium and xhigh; \"high\" is answered with an error, and \"low\" thinks about as long as the default.",
            "Tool calls sometimes come out malformed or repeat themselves; the publisher lists this as open.",
            "It needs exactly one system message, at the start.",
        ],
        "advice": "The packing to prefer for agent work when it fits: about twice as fast at reading prompts on every card the publisher measured, for 1.3 GB more.",
        "measured": "on this RTX 3060, 2026-10-12: loads in 10 seconds, 7.9 GB of graphics memory, under 1 GB of ordinary "
                    "memory, writes 21.6 tokens a second and reads 302. The check: tool calls 6 of 6, code 6 of 6, "
                    "candour 2 of 2. The agent trial: all 5 tasks done with the checks on and all 5 with them off, "
                    "no reply that claimed work that was not done, 23 tokens a second inside a turn; the round with "
                    "the checks took 533 seconds against 273 (one run each, so that difference is not yet a finding)",
        "match": [],
        "reported": "the same weights in a packing that is larger and cheaper to unpack (the publisher's description)",
    },
    {
        "id": "qwen25-coder-14b-q4km", "name": "Qwen2.5-Coder 14B", "quant": "Q4_K_M",
        "kind": "dense", "params_b": 14, "active_b": 14, "layers": 48,
        "file": "", "size_gb": 8.99, "bytes": 0, "sha256": "", "repo": "", "license": "Apache-2.0",
        "runner": "stock", "runner_note": "any llama.cpp or LM Studio",
        "context": 32768, "thinking": False, "sampling": {}, "match": ["qwen2.5-coder-14b"],
        "measured": "21 to 27 tokens a second on this RTX 3060; 20 of 24 held-out tasks and 1 of 6 hard "
                    "function-writing tasks (October 2026)",
    },
    {
        "id": "qwen25-coder-32b-q4km", "name": "Qwen2.5-Coder 32B", "quant": "Q4_K_M",
        "kind": "dense", "params_b": 32, "active_b": 32, "layers": 64,
        "file": "", "size_gb": 19.85, "bytes": 0, "sha256": "", "repo": "", "license": "Apache-2.0",
        "runner": "stock", "runner_note": "any llama.cpp or LM Studio",
        "context": 32768, "thinking": False, "sampling": {}, "match": ["qwen2.5-coder-32b"],
    },
]


def entry(model_id: str) -> Optional[Dict[str, Any]]:
    return next((m for m in CATALOG if m["id"] == model_id), None)


#: Models served by something else (not downloaded here) whose own words for effort are known.
#: Strata's Qwen3.8-Flash-Next thinks at its highest by default; it takes none, low, medium, high.
SERVED_PROFILES = [
    {"id": "qwen3.8-flash-next", "match": ["qwen3.8-flash-next", "flash-next"], "thinking": True, "context": 262144,
     "efforts": {"off": "none", "low": "low", "medium": "medium", "high": "high"}, "calm": "medium"},
]


def profile_for(model_name: str) -> Dict[str, Any]:
    """What is known about how to drive a model, found by its name. {} when nothing is known."""
    low = str(model_name or "").lower().replace("_", "-")
    for m in SERVED_PROFILES:
        if any(n in low for n in m["match"]):
            return {"sampling": {}, "thinking": True, "context": m["context"], "id": m["id"], "efforts": m["efforts"],
                    "budget_field": "", "calm": m["calm"], "budgets": {}}
    for m in CATALOG:
        names = list(m.get("match") or []) + [m["id"]] + ([m["file"].lower().replace("_", "-").rsplit(".", 1)[0]] if m.get("file") else [])
        if any(n and n in low for n in names):
            return {"sampling": dict(m.get("sampling") or {}), "thinking": bool(m.get("thinking")),
                    "context": m.get("context"), "id": m["id"], "efforts": m.get("efforts"),
                    "budget_field": m.get("budget_field", ""), "calm": m.get("calm", "medium"),
                    "budgets": m.get("budgets") or {}}
    return {}


#: How long a model may think, for each effort setting, where a budget can be sent.
THINKING_BUDGETS = {"low": 512, "medium": 2048, "high": 8192}
_GENERIC_EFFORTS = {"low": "low", "medium": "medium", "high": "high"}


def reasoning_params(profile: Dict[str, Any], effort: str) -> Dict[str, Any]:
    """Request fields that set how much a reasoning model thinks, for the session's effort setting.

    "auto" sends nothing: the model's own default applies. Each model takes its own words for
    effort (one accepts low, medium and xhigh and answers "high" with an error), so the words
    come from what is known about the model.
    """
    if not profile.get("thinking") or effort not in ("off", "low", "medium", "high"):
        return {}
    words = profile.get("efforts") or _GENERIC_EFFORTS
    out: Dict[str, Any] = {}
    if effort == "off" and not words.get("off"):
        effort = "low"   # a model with no word for none thinks as little as it can instead
    if effort == "off":
        # no thinking at all, for a model known to take a word for it; the budget field is
        # cleared (None is not sent) so an earlier budget does not travel with it
        out["reasoning_effort"] = words["off"]
        if profile.get("budget_field"):
            out[profile["budget_field"]] = None
        return out
    if words.get(effort):
        out["reasoning_effort"] = words[effort]
    if profile.get("budget_field") and effort in THINKING_BUDGETS:
        out[profile["budget_field"]] = (profile.get("budgets") or {}).get(effort, THINKING_BUDGETS[effort])
    return out


# ---------------------------------------------------------------- machine ---


def hardware(models_dir: str) -> Dict[str, Any]:
    from . import machine

    snap = machine.snapshot()
    gpu = snap.get("gpu") or {}
    try:
        os.makedirs(models_dir, exist_ok=True)
        disk_free = round(shutil.disk_usage(models_dir).free / 1024 ** 3, 1)
    except OSError:
        disk_free = None
    return {
        "gpu": gpu.get("name") or "", "vram_gb": gpu.get("total_gb"), "vram_used_gb": gpu.get("used_gb"),
        "ram_gb": (snap.get("memory") or {}).get("total_gb"), "ram_free_gb": (snap.get("memory") or {}).get("free_gb"),
        "cores": (snap.get("cpu") or {}).get("cores"), "disk_free_gb": disk_free,
    }


def fit(model: Dict[str, Any], hw: Dict[str, Any]) -> Dict[str, Any]:
    """How this model would run on this machine. Every figure in it is an estimate from sizes."""
    size = float(model["size_gb"])
    vram = float(hw.get("vram_gb") or 0)
    ram = float(hw.get("ram_gb") or 0)
    card = max(0.0, vram - 1.5)          # room for the context and the desktop
    spare_ram = max(0.0, ram - 4.0)      # room for Windows and the app
    out: Dict[str, Any] = {"verdict": "", "how": "", "why": [], "flags": [], "context": 32768}
    if not ram:
        out.update(verdict="unknown", how="This computer's memory could not be read, so nothing can be said.")
        return out
    if model["kind"] == "moe":
        if size + 1 > card + spare_ram:
            out.update(verdict="no", how="Does not run here.")
            out["why"].append(f"the whole {size:.1f} GB file must sit in graphics memory plus memory, and about "
                              f"{card + spare_ram:.0f} GB of that is free to use")
            return out
        if size <= card:
            out.update(verdict="gpu", how="Runs entirely on the graphics card.", flags=["-ngl", "99"])
            return out
        if vram < 4:
            out["ram_gb"] = round(size + 1.0, 1)
            out.update(verdict="cpu", how="Runs from memory on the processor. Only a few billion of its parameters "
                                          "are used per word, so this is usable, but slower than with a graphics card.",
                       flags=["-ngl", "0"])
            return out
        layers = int(model.get("layers") or 40)
        shared = size * 0.10                       # attention and shared layers: always on the card
        per_layer = size * 0.90 / layers           # the experts of one layer
        on_card = max(0, min(layers, int((card - shared) / per_layer)))
        cpu_layers = min(layers, layers - on_card + 2)   # two layers of margin
        out["ram_gb"] = round(size - (shared + per_layer * (layers - cpu_layers)) + 1.0, 1)
        out.update(
            verdict="split",
            how=(f"Runs with the shared layers and the experts of about {layers - cpu_layers} of {layers} layers on the "
                 f"graphics card and the rest in memory. Only about {model['active_b']} of its {model['params_b']} billion "
                 "parameters are used per word, so the part in memory costs little speed."),
            # weights kept off the card are loaded into memory outright: read through the file
            # mapping they are paged in and out, which llama.cpp itself warns is slow
            flags=["-ngl", "99", "--n-cpu-moe", str(cpu_layers), "--load-mode", "none"],
        )
        out["why"].append(f"needs about {out['ram_gb']:.0f} GB of free memory beside the card; this computer has {ram:.0f} GB in all")
        return out
    # a dense model: every layer is used for every word
    if size <= card:
        out.update(verdict="gpu", how="Runs entirely on the graphics card.", flags=["-ngl", "99"])
        if card - size < 1.0:
            out["why"].append("it fits with under 1 GB to spare, so keep the context small")
            out["context"] = 8192
        return out
    if size + 1 <= card + spare_ram and vram >= 4:
        layers = int(model.get("layers") or 32)
        on_card = max(1, int(layers * card / size))
        out["ram_gb"] = round(size * (1 - on_card / layers) + 1.0, 1)
        out.update(verdict="slow", how=(f"Loads, with about {on_card} of {layers} layers on the graphics card, but every "
                                        "layer is needed for every word, so the part in memory makes it slow."),
                   flags=["-ngl", str(on_card)])
        out["why"].append("measured here with a 14B model half off the card: 1 to 3 tokens a second")
        return out
    out.update(verdict="no", how="Does not run here.")
    out["why"].append(f"the {size:.1f} GB file needs more than the {card + spare_ram:.0f} GB this computer can give it")
    return out


def command(model: Dict[str, Any], placement: Dict[str, Any], path: str, binary: str = "llama-server",
            sleep_minutes: float = 0) -> List[str]:
    """The command that starts this model here, as a list of arguments."""
    args = [binary, "-m", path] + list(placement.get("flags") or [])
    if "--load-mode" not in args:
        # Measured here with a model wholly on the card: left to map the file, the server held 6.9 GB
        # of ordinary memory for nothing; loading it outright, 1 GB.
        args += ["--load-mode", "none"]
    # The server keeps finished conversations in memory to resume them, up to 8 GB unless told.
    # Measured: that took another 1.7 GB within four requests. One conversation needs little.
    args += ["--cache-ram", "1024"]
    # No compressed conversation cache (--cache-type-k/v q8_0). It came from a command posted for
    # another model; measured here, it makes Bonsai answer a long prompt with nothing but slashes,
    # where the plain cache answers correctly. Nothing is compressed unless it is measured to work.
    args += ["-c", str(placement.get("context") or 32768), "-fa", "on", "--jinja", "-np", "1"]
    flag = {"temperature": "--temp", "top_p": "--top-p", "top_k": "--top-k", "min_p": "--min-p"}
    for key, value in (model.get("sampling") or {}).items():
        if key in flag:
            args += [flag[key], str(value)]
    # replacing an old tool result in the middle of a conversation must not make the server read
    # everything after it again: let it shift what it already has
    args += ["--cache-reuse", "256", "-a", model["id"]]
    if sleep_minutes and sleep_minutes > 0:
        # free the graphics card and memory when nothing has asked for a while; it loads again on the next request
        args += ["--sleep-idle-seconds", str(int(sleep_minutes * 60))]
    args += ["--host", "127.0.0.1", "--port", str(PORT)]
    return args


_INI_KEYS = {"-ngl": "n-gpu-layers", "--n-cpu-moe": "n-cpu-moe", "-c": "c", "--temp": "temp", "--top-p": "top-p",
             "--top-k": "top-k", "--min-p": "min-p"}


def presets(rows: List[Dict[str, Any]], sleep_minutes: float = 0) -> str:
    """A llama-server presets file for several models: one section each, with its own placement.

    *rows* are {"model": catalog entry, "path": file, "placement": fit()}. With this file the
    server runs as a router: it keeps one model loaded and swaps to another when a request names it.
    """
    lines = ["version = 1", "", "; written by CodeMonkey: settings shared by every model", "[*]",
             "flash-attn = on", "jinja = true", "parallel = 1",
             "cache-reuse = 256", "load-mode = none", "cache-ram = 1024"]
    if sleep_minutes and sleep_minutes > 0:
        lines.append(f"sleep-idle-seconds = {int(sleep_minutes * 60)}")
    for row in rows:
        m, placement = row["model"], row["placement"]
        lines += ["", f"[{m['id']}]", f"model = {row['path']}", f"c = {placement.get('context') or 32768}"]
        flags = list(placement.get("flags") or [])
        for key, value in zip(flags[::2], flags[1::2]):
            if key in _INI_KEYS:
                lines.append(f"{_INI_KEYS[key]} = {value}")
            elif key == "--load-mode":
                pass   # the same for every model: set once above
        for key, value in (m.get("sampling") or {}).items():
            lines.append(f"{key.replace('_', '-').replace('temperature', 'temp')} = {value}")
    return "\n".join(lines) + "\n"


def router_command(preset_path: str, binary: str = "llama-server", at_once: int = 1) -> List[str]:
    """Start llama-server with no model of its own: it loads whichever one a request names."""
    return [binary, "--models-preset", preset_path, "--models-max", str(max(1, int(at_once))),
            "--host", "127.0.0.1", "--port", str(PORT)]


def shown(args: List[str]) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in args)


# ------------------------------------------------------------------ files ---


def find_files(models_dir: str) -> Dict[str, str]:
    """GGUF files already on this computer, by file name. Looks where they usually are."""
    home = os.path.expanduser("~")
    roots = [models_dir, os.path.join(home, ".lmstudio", "models"), os.path.join(home, ".cache", "lm-studio", "models"),
             os.path.join(home, "colibri-models"), os.path.join(home, "models")]
    for part in os.environ.get("PATH", "").split(os.pathsep):   # LM Studio may live under another account's folder
        if ".lmstudio" in part:
            roots.append(os.path.join(part[:part.index(".lmstudio")], ".lmstudio", "models"))
    cached = _FILES.get(models_dir)
    if cached and time.monotonic() - cached[0] < 8.0:
        return dict(cached[1])
    found: Dict[str, str] = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for folder, dirs, files in os.walk(root):
            if folder.count(os.sep) - base_depth >= 4:
                dirs[:] = []
            for name in files:
                if name.lower().endswith(".gguf") and name not in found:
                    found[name] = os.path.join(folder, name)
    _FILES[models_dir] = (time.monotonic(), dict(found))
    return found


_FILES: Dict[str, Any] = {}


def located(model: Dict[str, Any], files: Dict[str, str]) -> str:
    """Where this model's file is on this computer, or ""."""
    if model.get("file") and model["file"] in files:
        return files[model["file"]]
    if not model.get("file"):   # known by name only: any file that carries the name
        for name, path in files.items():
            low = name.lower().replace("_", "-")
            if any(tag in low for tag in model.get("match") or []) and "mmproj" not in low:
                return path
    return ""


def find_runner(setting: str = "", workspace: str = "", kind: str = "stock") -> str:
    """The llama-server program for a model, or "" when it is not installed.

    Two kinds are kept apart, because each can only run its own models: the ordinary llama.cpp
    ("stock", folders named llama-*) and PrismML's build ("fork", folders named prism-*), which
    the Bonsai files need. Looked for in CodeMonkey's own runners folder (newest first); for the
    ordinary kind also where you said it is and on the PATH.
    """
    prefix = "prism-" if kind == "fork" else "llama-"
    own: List[str] = []
    runners = os.path.join(workspace, "runners") if workspace else ""
    if runners and os.path.isdir(runners):
        for folder in sorted(os.listdir(runners), reverse=True):
            if folder.lower().startswith(prefix):
                for name in ("llama-server.exe", "llama-server"):
                    own.append(os.path.join(runners, folder, name))
    elsewhere = [setting, shutil.which("llama-server") or "", shutil.which("llama-server.exe") or ""] if kind != "fork" else []
    for candidate in own + elsewhere:
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


class Models:
    """One per app: the catalog against this machine, downloads, and the model server."""

    def __init__(self, app):
        self.app = app
        self.dir = os.path.join(app.workspace, "models")
        self._lock = threading.Lock()
        self.download: Dict[str, Any] = {}     # the one download in progress, or the last one's outcome
        self._cancel = threading.Event()
        self.process: Optional[subprocess.Popen] = None
        self.running: Dict[str, Any] = {}
        self.router = False   # the server was started for several models and swaps between them
        self.hub = HUB
        import atexit

        # A model server holds many gigabytes of memory and VRAM. Started from here, it must not
        # outlive this program: the desktop app ends the whole process group, but `python webui.py`
        # stopped with Ctrl-C left llama-server running with nothing talking to it.
        atexit.register(self.stop)

    def _sleep(self) -> float:
        try:
            return max(0.0, float(self.app.settings.get("model_sleep_minutes", 15)))
        except (TypeError, ValueError):
            return 15.0

    def _announce(self) -> None:
        try:
            self.app.bus.emit("models", {}, transient=True)
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------------- overview ---

    def overview(self) -> Dict[str, Any]:
        hw = hardware(self.dir)
        files = find_files(self.dir)
        setting = str(self.app.settings.get("llama_server_path") or "")
        runner = find_runner(setting, self.app.workspace)
        fork = find_runner(setting, self.app.workspace, "fork")
        rows = []
        for m in CATALOG:
            path = located(m, files)
            if path and not m["bytes"]:   # known by name only: its real size is the file's
                try:
                    m = dict(m, size_gb=round(os.path.getsize(path) / 1e9, 2))
                except OSError:
                    pass
            placement = fit(m, hw)
            part = os.path.join(self.dir, m["file"] + ".part") if m["file"] else ""
            row = {k: m.get(k) for k in ("id", "name", "quant", "kind", "params_b", "active_b", "size_gb", "repo",
                                         "license", "runner", "runner_note", "reported", "measured", "thinking", "file")}
            row.update(
                verdict=placement["verdict"], how=placement["how"], why=placement["why"],
                installed=bool(path), path=path,
                partial_gb=round(os.path.getsize(part) / 1e9, 2) if part and os.path.isfile(part) else 0,
                can_download=bool(m["file"] and m["repo"] and not path),
                enough_disk=hw["disk_free_gb"] is None or hw["disk_free_gb"] > m["size_gb"] + 5,
                source=f"{HUB}/{m['repo']}" if m["repo"] else "",
                command=shown(command(m, placement, path or os.path.join(self.dir, m["file"] or "model.gguf"),
                                      (fork if m.get("runner") == "fork" else runner) or "llama-server",
                                      self._sleep())) if placement["verdict"] not in ("no", "unknown") else "",
                runner_ready=bool(fork if m.get("runner") == "fork" else runner),
                issues=list(m.get("issues") or []), issues_as_of=m.get("issues_as_of", ""), advice=m.get("advice", ""),
                results=self.app.store.bench_results(m["id"]) if hasattr(self.app.store, "bench_results") else [],
            )
            rows.append(row)
        return {"hardware": hw, "models": rows, "runner": runner, "runner_found": bool(runner),
                "download": dict(self.download), "running": self.status(), "port": PORT, "folder": self.dir}

    # -------------------------------------------------------------- download ---

    def start_download(self, model_id: str) -> Dict[str, Any]:
        m = entry(model_id)
        if m is None or not m["file"] or not m["repo"]:
            raise ValueError("that model has no file to download")
        with self._lock:
            if self.download.get("state") == "downloading":
                raise RuntimeError("another download is already running")
            hw = hardware(self.dir)
            part = os.path.join(self.dir, m["file"] + ".part")
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            need = (m["bytes"] - have) / 1024 ** 3
            if hw["disk_free_gb"] is not None and hw["disk_free_gb"] < need + 2:
                raise RuntimeError(f"needs {need:.1f} GB free in {self.dir}; there is {hw['disk_free_gb']} GB")
            self._cancel.clear()
            self.download = {"id": model_id, "file": m["file"], "state": "downloading", "done": have,
                             "total": m["bytes"], "speed": 0.0, "error": "", "started": time.time()}
        threading.Thread(target=self._download, args=(m,), name="model-download", daemon=True).start()
        self._announce()
        return dict(self.download)

    def cancel_download(self) -> None:
        self._cancel.set()

    def _download(self, m: Dict[str, Any]) -> None:
        final = os.path.join(self.dir, m["file"])
        part = final + ".part"
        state = self.download
        try:
            os.makedirs(self.dir, exist_ok=True)
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            if have < m["bytes"]:
                url = f"{self.hub}/{m['repo']}/resolve/main/{m['file']}"
                req = urllib.request.Request(url, headers={"Range": f"bytes={have}-", "User-Agent": "CodeMonkey"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    if have and resp.status != 206:   # the server sent the whole file again: start over
                        have = 0
                    with open(part, "ab" if have else "wb") as out:
                        mark, mark_at = have, time.time()
                        while True:
                            if self._cancel.is_set():
                                state.update(state="paused", done=have)
                                self._announce()
                                return
                            chunk = resp.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                            have += len(chunk)
                            now = time.time()
                            if now - mark_at >= 1.0:
                                state.update(done=have, speed=(have - mark) / (now - mark_at))
                                mark, mark_at = have, now
                                self._announce()
            state.update(done=have, state="checking")
            self._announce()
            if m["bytes"] and have != m["bytes"]:
                raise RuntimeError(f"the file is {have} bytes but should be {m['bytes']}; press Download to carry on")
            if m["sha256"]:
                digest = hashlib.sha256()
                with open(part, "rb") as fh:
                    for block in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != m["sha256"]:
                    os.replace(part, part + ".bad")
                    raise RuntimeError("the file does not match the publisher's checksum, so it was set aside "
                                       "(.part.bad) and not installed")
            os.replace(part, final)
            _FILES.clear()
            state.update(state="done", path=final)
            self.app.store.add_activity(None, "model", f"Downloaded {m['file']}", f"{m['size_gb']} GB from {m['repo']}")
        except Exception as exc:  # noqa: BLE001 - reported on the page
            state.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            self._announce()

    # ----------------------------------------------------------------- server ---

    def status(self) -> Dict[str, Any]:
        proc = self.process
        if proc is None:
            why = getattr(self, "stopped_for", "")
            return {"state": "stopped", "why": why} if why else {"state": "stopped"}
        code = proc.poll()
        if code is not None:
            out = dict(self.running, state="exited", exit=code, log=self.log_tail(12))
            return out
        if self.router:
            return self._router_status(proc)
        ready = self.running.get("ready")
        asleep = False
        try:   # /health and /props do not wake a sleeping model or reset its idle timer
            if not ready:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=0.8) as resp:
                    ready = resp.status == 200
                self.running["ready"] = ready
            if ready:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/props", timeout=0.8) as resp:
                    asleep = bool(json.loads(resp.read().decode("utf-8", "replace")).get("is_sleeping"))
        except Exception:  # noqa: BLE001 - still loading, or an older server without /props
            pass
        return dict(self.running, state="asleep" if asleep else "ready" if ready else "loading", pid=proc.pid)

    def _router_status(self, proc: subprocess.Popen) -> Dict[str, Any]:
        """Which of the several models is loaded, loading, asleep or not loaded right now."""
        each: Dict[str, str] = {}
        ready = False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/models", timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            ready = True
            for item in data.get("data") or []:
                status = item.get("status") or {}
                each[str(item.get("id"))] = "failed" if status.get("failed") else str(status.get("value") or "unloaded")
        except Exception:  # noqa: BLE001 - still starting
            pass
        self.running["ready"] = ready
        loaded = [k for k, v in each.items() if v in ("loaded", "sleeping")]
        return dict(self.running, state="ready" if ready else "loading", pid=proc.pid, router=True, each=each,
                    loaded=loaded)

    def start_router(self) -> Dict[str, Any]:
        """Start one server for every installed model that the ordinary llama.cpp can run."""
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("a model is already running; stop it first")
        runner = find_runner(str(self.app.settings.get("llama_server_path") or ""), self.app.workspace)
        if not runner:
            raise RuntimeError("llama-server is not installed (or not on the PATH). Install llama.cpp, or set its "
                               "location in Settings, Models.")
        hw = hardware(self.dir)
        files = find_files(self.dir)
        rows, left_out = [], []
        for m in CATALOG:
            path = located(m, files)
            if not path:
                continue
            if m.get("runner") != "stock":
                left_out.append(f"{m['name']} {m['quant']} (needs its publisher's own llama.cpp)")
                continue
            sized = dict(m, size_gb=round(os.path.getsize(path) / 1e9, 2)) if not m["bytes"] else m
            placement = fit(sized, hw)
            if placement["verdict"] in ("no", "unknown"):
                left_out.append(f"{m['name']} {m['quant']} (does not run here)")
                continue
            rows.append({"model": m, "path": path, "placement": placement})
        if len(rows) < 2:
            raise RuntimeError("switching needs at least two installed models the ordinary llama.cpp can run; "
                               f"{len(rows)} found")
        os.makedirs(self.dir, exist_ok=True)
        preset_path = os.path.join(self.dir, "models.ini")
        with open(preset_path, "w", encoding="utf-8") as fh:
            fh.write(presets(rows, self._sleep()))
        at_once = int(self.app.settings.get("models_at_once") or 1)
        args = router_command(preset_path, runner, at_once)
        log = self._open_log("$ " + shown(args))
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        creationflags=flags)
        self.router = True
        ids = [r["model"]["id"] for r in rows]
        self.running = {"id": "", "name": f"{len(ids)} models, {at_once} loaded at a time", "command": shown(args),
                        "started": time.time(), "ready": False, "models": ids, "left_out": left_out}
        self.app.config.providers["local"] = {"base_url": f"http://127.0.0.1:{PORT}/v1", "api_key_env": "", "models": ids}
        self.app.store.add_activity(None, "model", f"Started {len(ids)} models to switch between", ", ".join(ids)[:300])
        self._announce()
        return self.status()

    def _router_call(self, action: str, model_id: str) -> None:
        if not self.router or self.process is None or self.process.poll() is not None:
            raise RuntimeError("the models are not running")
        if model_id not in (self.running.get("models") or []):
            raise ValueError("no such model")
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/models/{action}",
                                     data=json.dumps({"model": model_id}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"the model server did not accept that ({type(exc).__name__})") from exc
        self._announce()

    def load(self, model_id: str) -> None:
        self._router_call("load", model_id)

    def unload(self, model_id: str) -> None:
        self._router_call("unload", model_id)

    def log_tail(self, lines: int = 40) -> str:
        try:
            with open(os.path.join(self.dir, "server.log"), "r", encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:])
        except OSError:
            return ""

    def start(self, model_id: str) -> Dict[str, Any]:
        m = entry(model_id)
        if m is None:
            raise ValueError("no such model")
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("a model is already running; stop it first")
        runner = find_runner(str(self.app.settings.get("llama_server_path") or ""), self.app.workspace,
                             m.get("runner") or "stock")
        if not runner:
            if m.get("runner") == "fork":
                raise RuntimeError("this model needs PrismML's build of llama.cpp, which is not installed "
                                   "(it goes in the runners folder, in a folder named prism-...)")
            raise RuntimeError("llama-server is not installed (or not on the PATH). Install llama.cpp, or set its "
                               "location in Settings, Models.")
        path = located(m, find_files(self.dir))
        if not path:
            raise RuntimeError("the model file is not on this computer yet")
        hw = hardware(self.dir)
        placement = fit(m, hw)
        if placement["verdict"] in ("no", "unknown"):
            raise RuntimeError(placement["how"] + " " + "; ".join(placement["why"]))
        floor = float(self.app.settings.get("min_free_ram_gb") or 0)
        need = float(placement.get("ram_gb") or 0) + max(floor, 1.5)
        if hw["ram_free_gb"] is not None and hw["ram_free_gb"] < need:
            # found out the hard way: started with too little free, it loads, then the whole machine pages
            raise RuntimeError(
                f"this model needs about {placement.get('ram_gb') or 0:.0f} GB of free memory beside the graphics card, "
                f"with {max(floor, 1.5):.1f} GB left over for everything else, and {hw['ram_free_gb']} GB is free right "
                f"now. Close about {need - hw['ram_free_gb']:.0f} GB worth of other programs and try again.")
        args = command(m, placement, path, runner, self._sleep())
        log = self._open_log("$ " + shown(args))
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        creationflags=flags)
        self.router = False
        self.stopped_for = ""
        self.running = {"id": model_id, "name": f"{m['name']} {m['quant']}", "command": shown(args),
                        "started": time.time(), "ready": False, "context": placement["context"]}
        # the app can talk to it straight away: a provider named "local"
        self.app.config.providers["local"] = {"base_url": f"http://127.0.0.1:{PORT}/v1", "api_key_env": "",
                                              "models": [m["id"]]}
        self.app.store.add_activity(None, "model", f"Started {m['name']} {m['quant']}", shown(args)[:300])
        self._watch_memory()
        self._announce()
        return self.status()

    def _watch_memory(self) -> None:
        """Stop the model server if it leaves the computer without memory.

        A model that does not quite fit still loads; then everything else is pushed to disk and
        the machine crawls. Free memory under the floor on three looks in a row, and the server
        is stopped with the reason recorded.
        """
        proc = self.process

        def watch() -> None:
            from . import machine

            low = 0
            while self.process is proc and proc is not None and proc.poll() is None:
                time.sleep(4)
                free = machine.memory().get("free_gb")
                low = low + 1 if free is not None and free < 0.6 else 0
                if low >= 3 and self.process is proc:
                    self.stop()
                    self.running = {}
                    self.stopped_for = (f"It was stopped to protect this computer: free memory fell to {free} GB "
                                        "while it was loaded. Close other programs, or use a smaller file.")
                    try:
                        self.app.store.add_activity(None, "model", "Stopped the model: the computer ran out of memory",
                                                    self.stopped_for, "error")
                    except Exception:  # noqa: BLE001
                        pass
                    self._announce()
                    return

        threading.Thread(target=watch, name="model-memory", daemon=True).start()

    def _open_log(self, first_line: str):
        """The model server's log, kept open only while that server runs."""
        self._close_log()
        os.makedirs(self.dir, exist_ok=True)
        self._log = open(os.path.join(self.dir, "server.log"), "w", encoding="utf-8", errors="replace")
        self._log.write(first_line + "\n")
        self._log.flush()
        return self._log

    def _close_log(self) -> None:
        log, self._log = getattr(self, "_log", None), None
        if log is not None:
            try:
                log.close()
            except OSError:
                pass

    def stop(self) -> Dict[str, Any]:
        proc, self.process = self.process, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._close_log()
        self.running = {}
        self.router = False
        self._announce()
        return {"state": "stopped"}
