"""Event bus: thread-safe publish/subscribe, with replay for listeners that fell behind.

The agent worker emits events; each open page subscribes its own queue and drains it. Slow or
dead subscribers never block the worker: emit() never waits.

Every event is numbered. The bus keeps the recent ones, so a page whose connection dropped (a
phone that went to sleep, a network that blinked) can say "I have everything up to number N" and
be sent exactly what it missed. When it has been away for longer than the bus remembers, it is
told so and reloads the conversation instead. Either way no device is left showing a stale view.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple


class EventBus:
    """One bus per session (and one for the app as a whole)."""

    def __init__(self, keep: int = 600) -> None:
        self._subs: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._seq = 0
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=keep)

    @property
    def seq(self) -> int:
        """The number of the latest event."""
        with self._lock:
            return self._seq

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        return self.subscribe_from(None)[0]

    def subscribe_from(
        self, after: Optional[int]
    ) -> "Tuple[queue.Queue[Dict[str, Any]], Optional[List[Dict[str, Any]]]]":
        """Subscribe, and get what was missed since event number *after*.

        Returns (queue, missed). ``missed`` is the list of events after *after* (possibly empty),
        or None when that cannot be given: nothing was asked for, or it is too long ago.
        Subscribing and reading the backlog happen in one step, so nothing falls between them.
        """
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
            if after is None:
                return q, None
            if after >= self._seq:
                return q, []
            oldest = self._recent[0]["seq"] if self._recent else self._seq + 1
            if after + 1 < oldest:
                return q, None   # the gap is longer than what is kept
            return q, [e for e in self._recent if e["seq"] > after]

    def unsubscribe(self, q: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def emit(self, type: str, data: Dict[str, Any] | None = None, transient: bool = False) -> int:
        """Publish an event and return its number.

        A *transient* event is one that the next of its kind makes obsolete (a partly written
        answer, a progress tick). Only the latest in a run is kept for replay, so a long answer
        cannot push everything else out of the backlog.
        """
        with self._lock:
            self._seq += 1
            event = {"type": type, "data": data or {}, "seq": self._seq, "transient": transient}
            last = self._recent[-1] if self._recent else None
            if transient and last is not None and last["transient"] and last["type"] == type:
                self._recent[-1] = event
            else:
                self._recent.append(event)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:  # pragma: no cover - unbounded queues
                pass
        return event["seq"]
