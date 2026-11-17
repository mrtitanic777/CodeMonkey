"""What a model server on this computer is doing right now, for the working line and the model menu.

A server that answers `GET /metrics` with JSON in Strata's shape ({"live": {...}, "requests": [...],
"engine": {...}}) is asked; anything else (Ollama, LM Studio, a hosted service) gives nothing and
the page shows what it always did. Only addresses on this computer are asked, and an answer is kept
for a second, so several pages asking at once ask the server once.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

_CACHE: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}


def _metrics_url(base_url: str) -> str:
    parts = urllib.parse.urlsplit(base_url or "")
    if parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        return ""
    return f"{parts.scheme}://{parts.netloc}/metrics"


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) else None


def live(base_url: str) -> Optional[Dict[str, Any]]:
    """{state, phase, read, total, tok_s, last_tok_s, last_reused, vram_free_mib} or None."""
    url = _metrics_url(base_url)
    if not url:
        return None
    cached = _CACHE.get(url)
    if cached and time.monotonic() - cached[0] < 1.0:
        return cached[1]
    out: Optional[Dict[str, Any]] = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=1.5) as resp:
            data = json.loads(resp.read(512 * 1024))
        now = data.get("live") if isinstance(data, dict) else None
        if isinstance(now, dict) and "state" in now:
            last = (data.get("requests") or [{}])[-1] if isinstance(data.get("requests"), list) and data["requests"] else {}
            dur, outs = _num(last.get("duration_s")), _num(last.get("output_tokens"))
            out = {
                "state": str(now.get("state") or ""),          # idle, reading, generating, queued ...
                "phase": str(now.get("phase") or ""),
                "read": _num(now.get("prompt_read")), "total": _num(now.get("prompt_total") or now.get("prompt_tokens")),
                "generated": _num(now.get("generated")), "tok_s": _num(now.get("tok_s")),
                "last_tok_s": round(outs / dur, 1) if dur and outs else None,
                "last_reused": _num(last.get("reused")), "last_prompt": _num(last.get("prompt_tokens")),
                "vram_free_mib": _num((data.get("engine") or {}).get("vram_free_mib")),
            }
    except Exception:  # noqa: BLE001 - not a server that says this; the page shows what it always did
        out = None
    _CACHE[url] = (time.monotonic(), out)
    return out
