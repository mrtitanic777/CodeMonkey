"""Failover: one model not answering does not end the turn.

A local model server gets restarted, runs out of memory or is still loading; a hosted one rate
limits. The loop used to give up on the first failed request. Now each request is one
operation with a budget:

    a failure that may pass (the server is not there yet, a timeout, 429, 5xx) is tried again
        after a short wait, twice, honouring a "retry after" the server gives;
    a failure that will not pass on its own (bad key, no such model) is not retried;
    when the budget is spent, the next model in your fallback list takes the request, in the
        same turn, and you are told;
    a model that has just failed is left alone for half a minute, so helpers and background
        work do not all queue up against a dead server;
    a request the model refuses as too long is not a failure of the server at all: it is
        handed back so the context can be cut down and sent again (see workingset.py).

The shape (one logical operation, classified failures, an attempt budget, ordered fallback
candidates, a recovery period) follows the provider execution layer of free-claude-code. The
code is written here.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

RETRIES = 2
BACKOFF = (0.6, 2.5)
REST_SECONDS = 30.0

from agent import _TOO_LONG as _OVERFLOW   # one pattern for "the request is too long", shared with the client

_TRANSIENT = re.compile(r"URLError|ConnectionRefused|ConnectionReset|RemoteDisconnected|timed out|TimeoutError|"
                        r"HTTP Error (429|5\d\d)|Connection aborted|IncompleteRead|loading model|Service Unavailable", re.I)
_CONFIG = re.compile(r"HTTP Error (401|403|404)", re.I)
_RETRY_AFTER = re.compile(r"retry[- ]after[^0-9]{0,12}(\d+(?:\.\d+)?)", re.I)


def classify(error: BaseException) -> str:
    """"overflow", "transient", "config" (try another model, do not retry this one) or "final"."""
    text = f"{type(error).__name__}: {error}"
    if _OVERFLOW.search(text):
        return "overflow"
    if _CONFIG.search(text):
        return "config"
    if _TRANSIENT.search(text):
        return "transient"
    return "final"


class ContextOverflow(RuntimeError):
    """The model refused the request as too long. The caller should cut the context and send again."""


class Breaker:
    """Which models failed a moment ago. Shared by everything in the app."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until: Dict[str, float] = {}

    def resting(self, key: str) -> bool:
        with self._lock:
            return self._until.get(key, 0.0) > time.monotonic()

    def failed(self, key: str, why: str) -> None:
        with self._lock:
            self._until[key] = time.monotonic() + REST_SECONDS

    def worked(self, key: str) -> None:
        with self._lock:
            self._until.pop(key, None)


class ResilientClient:
    """Stands where a model client stands; tries the candidates in order."""

    def __init__(self, primary: Any, primary_key: str,
                 fallbacks: Optional[List[Tuple[str, Callable[[], Any]]]] = None,
                 breaker: Optional[Breaker] = None, notify: Optional[Callable[[str], None]] = None,
                 sleep: Callable[[float], None] = time.sleep, should_stop: Optional[Callable[[], bool]] = None):
        self.__dict__.update(
            _candidates=[(primary_key, lambda: primary)] + list(fallbacks or []),
            _built={0: primary}, _current=0, _breaker=breaker or Breaker(), _notify=notify or (lambda text: None),
            _sleep=sleep, _stop=should_stop or (lambda: False), switched_to=None, attempts=0,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._built[self._current], name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__dict__:
            self.__dict__[name] = value
        else:
            setattr(self._built[self._current], name, value)

    def _client(self, index: int) -> Any:
        if index not in self._built:
            made = self._candidates[index][1]()
            first = self._built[0]
            for hook in ("on_delta", "should_stop"):   # the page keeps showing the answer as it is written
                if hasattr(first, hook) and hasattr(made, hook):
                    setattr(made, hook, getattr(first, hook))
            self._built[index] = made
        return self._built[index]

    def hand_over_when(self, key: str, factory: Callable[[], Any], should: Callable[[], bool], why: str) -> None:
        """Arrange for another model to take over the rest of the turn once *should* says so."""
        self.__dict__["_second"] = (key, factory, should, why)

    def chat(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        second = self.__dict__.get("_second")
        if second and second[2]():
            key, factory, _should, why = second
            self.__dict__["_second"] = None   # once a turn
            if self._candidates[self._current][0] != key and not self._breaker.resting(key):
                self._candidates.insert(self._current + 1, (key, factory))
                # what was built for later places moves along with them
                self.__dict__["_built"] = {(i if i <= self._current else i + 1): c for i, c in self._built.items()}
                try:
                    self._client(self._current + 1)
                    self._current += 1
                    self.switched_to = key
                    self._notify(f"{why} {key} takes over for the rest of this turn.")
                except Exception as exc:  # noqa: BLE001 - it cannot be built: carry on as before
                    self._candidates.pop(self._current + 1)
                    self._notify(f"{key} could not take over ({type(exc).__name__}); carrying on with the same model.")
        last: Optional[BaseException] = None
        order = list(range(self._current, len(self._candidates)))
        for position, index in enumerate(order):
            key = self._candidates[index][0]
            is_last = position == len(order) - 1
            if self._breaker.resting(key) and not is_last:
                continue   # it failed a moment ago and there is another to ask
            try:
                client = self._client(index)
            except Exception as exc:  # noqa: BLE001 - a fallback that cannot even be built
                last = exc
                continue
            for attempt in range(RETRIES + 1):
                self.attempts += 1
                try:
                    reply = client.chat(messages, tools)
                except RuntimeError as exc:
                    kind = classify(exc)
                    if kind == "overflow":
                        raise ContextOverflow(str(exc)) from exc
                    if kind == "final":
                        raise
                    last = exc
                    if kind == "config" or attempt == RETRIES or self._stop():
                        break
                    wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                    m = _RETRY_AFTER.search(str(exc))
                    if m:
                        wait = min(20.0, max(wait, float(m.group(1))))
                    self._sleep(wait)
                    continue
                self._breaker.worked(key)
                if index != self._current:
                    self._current = index
                    self.switched_to = key
                return reply
            self._breaker.failed(key, str(last))
            if not is_last and not self._stop():
                nxt = self._candidates[order[position + 1]][0]
                self._notify(f"{key} did not answer ({_short(last)}). Trying {nxt} for this request.")
        raise last if isinstance(last, RuntimeError) else RuntimeError(f"Model request failed ({last})")


def _short(error: Optional[BaseException]) -> str:
    text = str(error or "no answer")
    m = re.search(r"\(([^()]{0,120})", text)
    return (m.group(1) if m else text)[:120]
