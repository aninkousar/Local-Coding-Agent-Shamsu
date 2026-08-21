from __future__ import annotations
import itertools
import queue
import threading

_event_queue: "queue.Queue" = queue.Queue()
_pending_events: dict[str, threading.Event] = {}
_pending_results: dict[str, str] = {}
_id_counter = itertools.count(1)
_lock = threading.Lock()


def push_event(event: dict) -> None:
    _event_queue.put(event)


def get_event_queue() -> "queue.Queue":
    return _event_queue


def new_request_id() -> str:
    with _lock:
        return f"req{next(_id_counter)}"


def wait_for_permission(request_id: str, timeout: float = 3600) -> str:
    """Blocks the calling (agent worker) thread until the frontend responds via
    resolve_permission(), or times out. A human may genuinely need real time to
    read a diff, so the timeout is generous (1 hour) - it exists only as a safety
    net against a request that never gets an answer (e.g. the window was closed).
    Times out to 'n' (deny), never to an implicit approval.
    """
    ev = threading.Event()
    _pending_events[request_id] = ev
    if not ev.wait(timeout=timeout):
        _pending_events.pop(request_id, None)
        return "n"
    return _pending_results.pop(request_id, "n")


def resolve_permission(request_id: str, decision: str) -> bool:
    ev = _pending_events.pop(request_id, None)
    if ev is None:
        return False
    _pending_results[request_id] = decision
    ev.set()
    return True
