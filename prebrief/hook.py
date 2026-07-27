"""hook — single Claude Code hook entrypoint for Prebrief.

Wire in .claude/settings.json (use `prebrief enroll <project_dir>`):
SessionStart, UserPromptSubmit, PostToolUse, Stop all point here. Reads the
hook event JSON on stdin, dispatches to the prebrief client/injector, and
emits additionalContext JSON where the event allows it.

Fails open: any error -> empty output on stdout, a note on stderr, exit 0.
The agent always proceeds.
"""
import json
import os
import sys

# Bootstrap: make the package importable when invoked as a bare script path
# (hooks run `python .../prebrief/hook.py`, not `python -m prebrief.hook`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _db_path():
    """Resolve the store path: env PREBRIEF_DB or ~/.prebrief/prebrief.db."""
    p = os.environ.get("PREBRIEF_DB")
    if not p:
        p = os.path.join(os.path.expanduser("~"), ".prebrief", "prebrief.db")
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    return p


def _project(ev):
    """Tenant label for this hook event: the basename of the event's cwd.

    Each enrolled project is its own tenant, so one project's captured text can
    never be composed into another project's agent context. Unusable cwd ->
    'default'. Sanitized to a conservative charset (the label reaches SQL only
    as a bound parameter, but it is also rendered into agent context).
    """
    try:
        from .store import project_from_path
        cwd = str(ev.get("cwd") or ev.get("project_dir") or os.getcwd())
        return project_from_path(cwd)
    except Exception:
        return "default"


def _emit(event_name, context):
    """Print the hookSpecificOutput envelope Claude Code expects."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event_name,
        "additionalContext": context,
    }}))


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return  # unreadable event: contribute nothing, block nothing
    try:
        from prebrief.store import Store
        from prebrief import client, inject

        store = Store(_db_path())
        name = ev.get("hook_event_name", "")
        sess = str(ev.get("session_id", "unknown"))[:36]
        agent = "cc-" + sess[:8]
        proj = _project(ev)

        if name == "SessionStart":
            client.register(store, agent, sess, "builder",
                            str(ev.get("cwd", "")), project=proj)
            ctx = inject.compose(store, agent, sess, role="builder", task="",
                                 project=proj)
            if ctx:
                _emit("SessionStart", "[fleet-context]\n" + ctx)

        elif name == "UserPromptSubmit":
            task = str(ev.get("prompt", ""))[:200]
            ctx = inject.compose(store, agent, sess, role="builder", task=task,
                                 project=proj)
            if ctx and "unchanged" not in ctx:
                _emit("UserPromptSubmit", "[fleet-update]\n" + ctx)

        elif name == "PostToolUse":
            ti = ev.get("tool_input") or {}
            path = ti.get("file_path") or ti.get("path")
            resp = str(ev.get("tool_response", ""))[:200]
            inject.mark_used(store, agent, ti.get('file_path') or ti.get('path') or '', str(ti.get('command','')), ev.get('tool_name',''))
            client.tools(store, sess, [{
                "tool": ev.get("tool_name", "?"),
                "path": path,
                "is_error": "error" in resp.lower()[:80],
            }], project=proj)
            if path:
                client.heartbeat(store, agent, [str(path)], project=proj)

        elif name in ("Stop", "SessionEnd"):
            client.end(store, agent, sess, project=proj)

    except Exception as e:
        print(f"prebrief hook: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
