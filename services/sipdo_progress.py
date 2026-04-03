"""In-memory side-channel for SIPDO optimisation progress.

LangGraph only persists node state *after* a node returns, so the UI cannot
poll GraphState for live updates while sipdo_optimize_node (which runs for
minutes) is still executing.  This module provides a simple dict-based store
that the progress callback writes to in real time and the API serves to the UI.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

_lock = threading.Lock()
_store: dict[str, dict] = {}


def update_progress(session_id: str, message: str) -> None:
    """Append a progress message for *session_id*."""
    with _lock:
        entry = _store.setdefault(session_id, {"messages": [], "done": False})
        entry["messages"].append(message)


def mark_done(session_id: str) -> None:
    """Mark optimisation as finished for *session_id*."""
    with _lock:
        if session_id in _store:
            _store[session_id]["done"] = True


def get_progress(session_id: str) -> dict:
    """Return the current progress for *session_id*."""
    with _lock:
        entry = _store.get(session_id)
        if entry is None:
            return {"messages": [], "done": False}
        return {"messages": list(entry["messages"]), "done": entry["done"]}


def clear(session_id: str) -> None:
    """Remove progress data for *session_id* (optional cleanup)."""
    with _lock:
        _store.pop(session_id, None)
