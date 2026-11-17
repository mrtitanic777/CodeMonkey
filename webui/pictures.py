"""Pictures from the web, saved into the project so a reply can show them.

A reply shows only pictures that are files in the project (never a web address: a picture's
address can carry text out to any server, which is how a page could make the model leak what it
has read). So a picture from the web is saved first, with the network permission like the other
web tools, and the reply then points at the saved file.

    save_picture(project_dir, "https://example.com/cat.jpg")          -> pictures/cat.jpg
    save_picture(project_dir, "https://en.wikipedia.org/wiki/Cat")    -> the page's main picture
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import struct
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

MAX_BYTES = 15 * 1024 * 1024
FOLDER = "pictures"
TIMEOUT = 30
_UA = "Mozilla/5.0 (CodeMonkey; +https://github.com/mrtitanic777/CodeMonkey)"

#: what a file starts with -> (kind, extension). Pictures a browser shows and that cannot run code
#: (SVG can, so it is not one of them).
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "PNG", ".png"), (b"\xff\xd8\xff", "JPEG", ".jpg"), (b"GIF87a", "GIF", ".gif"),
          (b"GIF89a", "GIF", ".gif"), (b"BM", "BMP", ".bmp"))
EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".webp": "image/webp", ".bmp": "image/bmp"}


def kind_of(data: bytes) -> Optional[Tuple[str, str]]:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP", ".webp"
    for magic, kind, ext in _MAGIC:
        if data.startswith(magic):
            return kind, ext
    return None


def size_of(data: bytes) -> Optional[Tuple[int, int]]:
    """Width and height, read from the file's header; None when it cannot be told cheaply."""
    try:
        if data.startswith(b"\x89PNG"):
            return struct.unpack(">II", data[16:24])
        if data[:3] == b"GIF":
            return struct.unpack("<HH", data[6:10])
        if data.startswith(b"BM"):
            w, h = struct.unpack("<ii", data[18:26])
            return w, abs(h)
        if data[:4] == b"RIFF" and data[12:16] == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if data.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + length
    except (struct.error, IndexError):
        return None
    return None


def _get(url: str, referer: str = "", limit: int = MAX_BYTES) -> Tuple[bytes, str, str]:
    headers = {"User-Agent": _UA, "Accept": "image/*,text/html;q=0.8,*/*;q=0.5"}
    if referer:
        headers["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as resp:
        data = resp.read(limit + 1)
        return data, resp.headers.get("Content-Type", ""), resp.geturl()


def pictures_on_page(page: str, base: str) -> List[Tuple[str, str]]:
    """The pictures a page names, main one first: its share picture (og:image, twitter:image),
    then its <img> tags, as (address, description). Icons, logos and tracking dots are left out."""
    found: List[Tuple[str, str]] = []

    def add(src: str, alt: str) -> None:
        src = html.unescape(src.strip())
        if not src or src.startswith("data:"):
            return
        url = urllib.parse.urljoin(base, src)
        low = url.lower().split("?")[0]
        if low.endswith(".svg") or re.search(r"(icon|logo|sprite|pixel|badge|avatar|spacer|blank)", low):
            return
        if url not in [u for u, _ in found]:
            found.append((url, html.unescape(alt.strip())[:120]))

    for m in re.finditer(r"<meta\b[^>]*>", page, re.I):
        tag = m.group(0)
        if re.search(r"(property|name)\s*=\s*[\"'](og:image|twitter:image)(:src)?[\"']", tag, re.I):
            content = re.search(r"content\s*=\s*[\"']([^\"']+)", tag, re.I)
            if content:
                add(content.group(1), "")
    for m in re.finditer(r"<img\b[^>]*>", page, re.I):
        tag = m.group(0)
        src = re.search(r"\bsrc\s*=\s*[\"']([^\"']+)", tag, re.I)
        alt = re.search(r"\balt\s*=\s*[\"']([^\"']*)", tag, re.I)
        width = re.search(r"\bwidth\s*=\s*[\"']?(\d+)", tag, re.I)
        if src and not (width and int(width.group(1)) < 48):
            add(src.group(1), alt.group(1) if alt else "")
    return found


def _target(project_dir: str, path: str, url: str, ext: str) -> Tuple[str, str]:
    """Where to save: *path* if given (inside the project), else pictures/<name from the address>.
    An existing file is never overwritten: a number is added instead."""
    root = os.path.realpath(project_dir)
    if path:
        rel = path.replace("\\", "/").lstrip("/")
        stem, given = os.path.splitext(rel)
        rel = stem + (given.lower() if given.lower() in EXTENSIONS and EXTENSIONS[given.lower()] ==
                      EXTENSIONS[ext] else ext)
    else:
        name = os.path.splitext(os.path.basename(urllib.parse.urlsplit(url).path))[0]
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", urllib.parse.unquote(name)).strip("-")[:60]
        rel = f"{FOLDER}/{name or hashlib.sha1(url.encode()).hexdigest()[:10]}{ext}"
    full = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, full]) != root:
        raise ValueError("the picture must be saved inside the project")
    stem, ext2 = os.path.splitext(full)
    n = 2
    while os.path.exists(full):
        full = f"{stem}-{n}{ext2}"
        n += 1
    return full, os.path.relpath(full, root).replace(os.sep, "/")


def save_picture(project_dir: str, url: str, path: str = "") -> str:
    """Save the picture at *url* (or a page's main picture) into the project; a line for the model."""
    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return "ERROR: give an http(s) address of a picture, or of a page that has one"
    try:
        data, ctype, final = _get(url)
    except Exception as exc:  # noqa: BLE001 - reported to the model
        return f"ERROR: could not fetch {url}: {type(exc).__name__}: {exc}"
    others: List[Tuple[str, str]] = []
    source, page = final, ""
    if kind_of(data) is None and ("html" in ctype.lower() or data.lstrip()[:1] == b"<"):
        page = final
        candidates = pictures_on_page(data[:3_000_000].decode("utf-8", "replace"), final)
        if not candidates:
            return f"ERROR: {url} is a page with no pictures in it that could be saved"
        for i, (src, _) in enumerate(candidates[:6]):
            try:
                picture, _, got = _get(src, referer=final)
            except Exception:  # noqa: BLE001 - try the next one
                continue
            if kind_of(picture):
                data, source, others = picture, got, candidates[i + 1:i + 9]
                break
        else:
            return "ERROR: none of the pictures on that page could be fetched as a PNG, JPEG, GIF, WebP or BMP"
    if len(data) > MAX_BYTES:
        return f"ERROR: the picture is bigger than {MAX_BYTES // (1024 * 1024)} MB"
    kind = kind_of(data)
    if kind is None:
        return f"ERROR: {source} is not a PNG, JPEG, GIF, WebP or BMP picture ({ctype or 'unknown type'})"
    try:
        full, rel = _target(project_dir, path, source, kind[1])
    except ValueError as exc:
        return f"ERROR: {exc}"
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)
    dims = size_of(data)
    about = f"{kind[0]}, " + (f"{dims[0]}×{dims[1]}, " if dims else "") + f"{max(1, len(data) // 1024)} KB"
    lines = [f"Saved {rel} ({about}) from {source}" + (f" (the main picture on {page})" if page else "") + ".",
             f"To show it, put ![a short description]({rel}) on its own line in your reply, and say where it came "
             "from: pictures on the web usually belong to someone."]
    if others:
        lines.append("Other pictures on that page (save one by its address):")
        lines += [f"- {u}" + (f" ({alt})" if alt else "") for u, alt in others]
    return "\n".join(lines)
