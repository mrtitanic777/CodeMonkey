"""Event bus: thread-safe publish/subscribe, with replay for listeners that fell behind.

The agent worker emits events; each open page subscribes its own queue and drains it. Slow or
dead subscribers never block the worker: emit() never waits.

Every event is numbered. The bus keeps the recent ones, so a page whose connection dropped (a
phone that went to sleep, a network that blinked) can say "I have everything up to number N" and
be sent exactly what it missed. When it has been away for longer than the bus remembers, it is
told so and reloads the conversation instead. Either way no device is left showing a stale view.
"""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple


def sse_frame(type: str, data: Any, seq: Optional[int] = None) -> bytes:
    """The bytes one event travels in, as every page receives it.

    Worked out once here rather than once per page: an event goes to every open stream, and
    serialising it again for each of them is the same work repeated. Values that JSON cannot
    write are turned into words instead of losing the stream over them.
    """
    head = f"id: {seq}\n" if seq is not None else ""
    body = json.dumps(data, separators=(",", ":"), default=str)
    return f"{head}event: {type}\ndata: {body}\n\n".encode("utf-8")


def frame_of(event: Dict[str, Any]) -> bytes:
    """An event's bytes, made the first time a page needs them and kept on the event.

    The event a subscriber gets is the one every other subscriber gets, so the first sender pays
    and the rest read what it left. An event nobody is watching is never encoded at all.
    """
    frame = event.get("frame")
    if frame is None:
        frame = sse_frame(event["type"], event["data"], event["seq"])
        event["frame"] = frame
    return frame


class _Page(queue.Queue):
    """One page's waiting room for events, with room for a limited number.

    A stream that stops reading - a phone whose connection still looks alive after it slept -
    would otherwise pile events up here for the life of the session. When the room fills the page
    is marked as behind, and the stream tells it to reload what it missed rather than quietly
    dropping events it will never hear.
    """

    def __init__(self, maxsize: int = 1000) -> None:
        super().__init__(maxsize)
        self.behind = False


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

    @property
    def subscribers(self) -> int:
        """How many pages are listening right now. 0 means nobody is watching this session."""
        with self._lock:
            return len(self._subs)

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
        q = _Page()
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

    def emit(self, type: str, data: Dict[str, Any] | None = None, transient: bool = False,
             backlog: Optional[Dict[str, Any]] = None) -> int:
        """Publish an event and return its number.

        A *transient* event is one that the next of its kind makes obsolete (a partly written
        answer, a progress tick). Only the latest in a run is kept for replay, so a long answer
        cannot push everything else out of the backlog.

        *backlog* is what a page that reconnects should be given, when that is not what the pages
        watching now are being sent: a partly written answer goes out as its newest words, but a
        page arriving later needs the whole of it, not the last piece.
        """
        with self._lock:
            self._seq += 1
            event = {"type": type, "data": data or {}, "seq": self._seq, "transient": transient}
            # what the backlog keeps: the same event, or its fuller version if one was given
            kept = dict(event, data=backlog) if backlog is not None else event
            last = self._recent[-1] if self._recent else None
            if transient and last is not None and last["transient"] and last["type"] == type:
                self._recent[-1] = kept
            else:
                self._recent.append(kept)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # this page has stopped reading; say so once, and its stream will tell it to
                # reload rather than show a conversation with holes in it
                q.behind = True
        return event["seq"]
