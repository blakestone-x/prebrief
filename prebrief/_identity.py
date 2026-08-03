"""Canonical, dependency-free tenant identity.

The private Postgres harness mirrors this pure function and verifies AST
equivalence plus cross-platform behavior. Database and filesystem access stay
in adapters so identity tests never need production credentials.
"""

DEFAULT = "default"


def _clean(label):
    """Conservative charset: this string is rendered into agent context."""
    return "".join(c for c in str(label) if c.isalnum() or c in "-_.")[:64]


def project_from_path(path, explicit=None, env=None, marker_reader=None):
    """Host-independent, stable tenant label.

    explicit        a caller-supplied label always wins
    env             mapping to read SG_PROJECT / PREBRIEF_PROJECT from
    marker_reader   callable(dir) -> label or None, for `.prebrief-project`

    Both separators are treated as separators on EVERY platform; drive letters
    are dropped. A basename is not an identity (two repos named `web` collide,
    a rename re-tenants history), so an explicit label or marker file wins.
    """
    try:
        if explicit:
            return _clean(explicit) or DEFAULT
        env = {} if env is None else env
        override = env.get("SG_PROJECT") or env.get("PREBRIEF_PROJECT")
        if override:
            return _clean(override) or DEFAULT
        raw = str(path or "")
        if raw and marker_reader is not None:
            try:
                label = marker_reader(raw)
                if label:
                    return _clean(label) or DEFAULT
            except Exception:
                pass
        # host-independent: BOTH separators, always. Never os.path.
        parts = [p for p in raw.replace("\\", "/").split("/") if p]
        # A git worktree at <project>/.claude/worktrees/<slug> is the SAME
        # tenant as the project it was cut from: the branch slug is ephemeral,
        # the project is not. Without this, every worktree became its own
        # tenant, so a worktree agent read an empty brief and the fleet's
        # history fragmented one directory at a time.
        # Scan ASCENDING and stop at the FIRST marker: a worktree cut from a
        # worktree must resolve to the original project, not to the inner slug.
        # Scanning backwards returned that slug and reintroduced exactly the
        # per-worktree fragmentation this loop exists to prevent.
        for i in range(1, len(parts)):
            if parts[i - 1].lower() == ".claude" and parts[i].lower() == "worktrees":
                parts = parts[: i - 1]
                break
        if parts and len(parts[-1]) == 2 and parts[-1].endswith(":"):
            parts.pop()
        base = parts[-1] if parts else ""
        if len(base) == 2 and base.endswith(":"):
            base = ""
        return _clean(base) or DEFAULT
    except Exception:
        return DEFAULT
