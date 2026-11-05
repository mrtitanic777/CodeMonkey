"""Portable browser driver (requires ``playwright``).

This is the clean-room, open-source replacement for a proprietary
browser-broker: it launches a real Chromium, keeps one persistent page per
named session, and exposes the actions an agent actually needs —
navigate, read, click, type, screenshot — as tools.

Setup::

    pip install playwright
    playwright install chromium

Design notes for porters:
  - One ``BrowserSession`` per agent run; sessions are cheap and isolated.
  - Every action returns a *string* for the model: never raw objects.
  - Text extraction strips scripts/styles and collapses whitespace, so the
    model gets readable content, not markup soup.
  - Screenshots are saved to disk and the *path* is returned; the caller
    decides how to show them to the model (most vision APIs take a path).
"""

from __future__ import annotations

import os
import threading
import time
from typing import List, Optional

from .registry import Tool

# Playwright takes seconds to import, so it is only checked for here and imported when a
# browser is actually started.
import importlib.util

_PLAYWRIGHT_AVAILABLE = importlib.util.find_spec("playwright") is not None


def _require_playwright() -> None:
    if not _PLAYWRIGHT_AVAILABLE:
        raise RuntimeError(
            "playwright is not installed. Run: pip install playwright && "
            "playwright install chromium"
        )


class BrowserSession:
    """A persistent Chromium page behind a thread-safe facade."""

    def __init__(self, headless: bool = True, timeout_ms: int = 30000,
                 shot_dir: str = "./workspace/shots"):
        _require_playwright()
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.shot_dir = os.path.abspath(shot_dir)
        os.makedirs(self.shot_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._page = None
        self._shots = 0

    # ---------------------------------------------------------- lifecycle ---

    def _ensure(self):
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        context = self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120 Safari/537.36"
            ),
        )
        self._page = context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    def close(self):
        with self._lock:
            try:
                if self._browser:
                    self._browser.close()
            finally:
                if self._pw:
                    self._pw.stop()
                self._page = None
                self._browser = None
                self._pw = None

    # ------------------------------------------------------------ actions ---

    def navigate(self, url: str) -> str:
        with self._lock:
            self._ensure()
            try:
                resp = self._page.goto(url, wait_until="domcontentloaded")
                status = resp.status if resp else "?"
                title = self._page.title()
                return f"[{status}] {title}\nURL: {self._page.url}"
            except Exception as exc:  # noqa: BLE001 - to the model
                return f"ERROR: navigation failed: {type(exc).__name__}: {exc}"

    def read(self, max_chars: int = 12000) -> str:
        """Return the page's readable text."""
        with self._lock:
            self._ensure()
            try:
                text = self._page.evaluate(
                    """() => {
                        const clone = document.body.cloneNode(true);
                        clone.querySelectorAll('script,style,noscript,svg')
                            .forEach(e => e.remove());
                        return (document.title + '\\n\\n' +
                            clone.innerText).replace(/\\s+/g, ' ');
                    }"""
                )
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: could not read page: {type(exc).__name__}: {exc}"
        text = (text or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated, {len(text)-max_chars} more]"
        return f"URL: {self._page.url}\n{text}" or "(empty page)"

    def click(self, selector: str) -> str:
        with self._lock:
            self._ensure()
            try:
                self._page.click(selector, timeout=self.timeout_ms)
                time.sleep(0.5)
                return f"Clicked {selector!r}. Now at: {self._page.url}"
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: click failed: {type(exc).__name__}: {exc}"

    def click_text(self, text: str) -> str:
        """Click the first element containing *text* (buttons, links)."""
        with self._lock:
            self._ensure()
            try:
                self._page.get_by_text(text, exact=False).first.click(
                    timeout=self.timeout_ms
                )
                time.sleep(0.5)
                return f"Clicked text {text!r}. Now at: {self._page.url}"
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: click_text failed: {type(exc).__name__}: {exc}"

    def fill(self, selector: str, text: str, submit: bool = False) -> str:
        with self._lock:
            self._ensure()
            try:
                self._page.fill(selector, text, timeout=self.timeout_ms)
                if submit:
                    self._page.press(selector, "Enter")
                    time.sleep(1.0)
                return f"Filled {selector!r}." + (
                    " Submitted." if submit else "")
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: fill failed: {type(exc).__name__}: {exc}"

    def screenshot(self, full_page: bool = False) -> str:
        with self._lock:
            self._ensure()
            self._shots += 1
            path = os.path.join(self.shot_dir, f"shot-{self._shots:03d}.png")
            try:
                self._page.screenshot(path=path, full_page=full_page)
                import images

                # the picture itself goes to a model that can see (images.py); the path to any other
                return f"Screenshot saved to {path}\n" + images.mark(path)
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: screenshot failed: {type(exc).__name__}: {exc}"

    def view(self, path: str) -> Optional[str]:
        """Save what the page shows now to *path* and return its address (None before any page).
        For the person watching (CodeMonkey's Browser pane), not for the model."""
        with self._lock:
            if self._page is None:
                return None
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                self._page.screenshot(path=path)
                return self._page.url
            except Exception:  # noqa: BLE001 - a missed picture is not an error
                return None

    def links(self, max_links: int = 40) -> str:
        """List clickable elements: text + selector hint."""
        with self._lock:
            self._ensure()
            try:
                items = self._page.evaluate(
                    """() => Array.from(
                        document.querySelectorAll('a,button,[role=button],input[type=submit]'))
                        .map(e => ({
                            text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 60),
                            tag: e.tagName.toLowerCase()
                        }))
                        .filter(x => x.text)"""
                )
            except Exception as exc:  # noqa: BLE001
                return f"ERROR: could not list links: {type(exc).__name__}: {exc}"
        lines = [
            f"- [{it['tag']}] {it['text']}" for it in items[:max_links]
        ]
        return "Clickable elements:\n" + "\n".join(lines) if lines else "(no links found)"


def browser_tools(session: Optional[BrowserSession] = None) -> List[Tool]:
    """Build the browser tool set around one shared session."""
    sess = session or BrowserSession()

    def tool(name, desc, props, required, func, approval=False):
        return Tool(name=name, description=desc,
                    parameters={"type": "object", "properties": props,
                                "required": required},
                    func=func, needs_approval=approval)

    return [
        tool("browser_navigate",
             "Navigate the browser to a URL. Returns HTTP status and page title.",
             {"url": {"type": "string"}}, ["url"],
             lambda url: sess.navigate(url)),
        tool("browser_read",
             "Read the current page as plain text (scripts stripped).",
             {"max_chars": {"type": "string"}}, [],
             lambda max_chars=12000: sess.read(int(max_chars))),
        tool("browser_links",
             "List clickable elements on the page (links, buttons).",
             {"max_links": {"type": "string"}}, [],
             lambda max_links=40: sess.links(int(max_links))),
        tool("browser_click",
             "Click an element by CSS selector.",
             {"selector": {"type": "string",
                           "description": "CSS selector, e.g. 'button.submit'"}},
             ["selector"],
             lambda selector: sess.click(selector)),
        tool("browser_click_text",
             "Click the first element containing the given text.",
             {"text": {"type": "string"}}, ["text"],
             lambda text: sess.click_text(text)),
        tool("browser_fill",
             "Fill a form field by CSS selector; optionally submit with Enter.",
             {"selector": {"type": "string"}, "text": {"type": "string"},
              "submit": {"type": "string",
                         "description": "'true' to press Enter after filling"}},
             ["selector", "text"],
             lambda selector, text, submit="false": sess.fill(
                 selector, text, str(submit).lower() == "true")),
        tool("browser_screenshot",
             "Screenshot the current viewport. Returns the saved file path.",
             {"full_page": {"type": "string"}}, [],
             lambda full_page="false": sess.screenshot(
                 str(full_page).lower() == "true"),
             approval=True),
    ]
