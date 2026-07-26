"""Prebrief — context orchestration for AI agent fleets.

Capture agent activity into a local SQLite substrate; compose a per-agent
"moving target" injection payload: full fleet brief on first contact, then
delivery-ledger-deduped deltas, with task-aware inlining. Stdlib only; every
component fails open.
"""
from .store import Store, default_db_path
from .brief import full_brief, delta_brief
from .inject import compose
from .client import (register, heartbeat, tools, traverse, end,
                     open_plan, set_plan_status, make_decision,
                     supersede_decision, assert_claim)
from .projector import project_events, rebuild

__version__ = "0.1.0"

__all__ = [
    "Store", "default_db_path",
    "full_brief", "delta_brief",
    "compose",
    "register", "heartbeat", "tools", "traverse", "end",
    # log-first emitters (write events only; the projector derives the rows)
    "open_plan", "set_plan_status", "make_decision", "supersede_decision",
    "assert_claim",
    # the fold
    "project_events", "rebuild",
    "__version__",
]
