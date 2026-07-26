"""Delivery-correctness tests. R1 (SOL-REVIEW): a delta rendered at most N
events but advanced the recipient watermark to the global maximum, permanently
skipping the tail. These tests are the regression wall.

Run: python tests/test_delivery.py   (no pytest dependency required)
"""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from prebrief import Store, compose            # noqa: E402
from prebrief.brief import delta_brief          # noqa: E402

FAILED = []

def check(name, ok, note=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {note}")
    if not ok:
        FAILED.append(name)

def fresh():
    return Store(":memory:")

def burst(store, n, start=0):
    for i in range(start, start + n):
        store.event("observation", f"a{i}", "sess-burst", {"i": i})

def test_no_event_loss_on_burst():
    """Sol's reproduction: 20 events after first contact must ALL become
    deliverable across successive payloads."""
    s = fresh()
    compose(s, "agent1", "sess1")          # first contact
    burst(s, 20)
    seen, guard = set(), 0
    while guard < 12:
        guard += 1
        p = compose(s, "agent1", "sess1")
        for i in range(20):
            if f'"i": {i}' in p or f'"i":{i}' in p:
                seen.add(i)
        if "unchanged" in p:
            break
    check("burst of 20 fully delivered", len(seen) == 20,
          f"({len(seen)}/20 seen)")

def test_watermark_never_outruns_render():
    s = fresh()
    compose(s, "a2", "s2")
    burst(s, 30)
    compose(s, "a2", "s2")
    wm = s.sql("SELECT watermark FROM delivery WHERE agent_id='a2' AND item_ref='wm'")
    maxid = s.sql("SELECT max(id) FROM events")[0][0]
    shown = delta_brief(s, 0)[1]
    check("watermark <= last rendered event",
          bool(wm) and int(wm[0][0]) <= int(shown) < int(maxid),
          f"(wm={wm[0][0] if wm else '?'} rendered_to={shown} max={maxid})")

def test_pagination_signal_present():
    s = fresh()
    compose(s, "a3", "s3")
    burst(s, 25)
    p = compose(s, "a3", "s3")
    check("pending-events signal shown", "more events pending" in p, "")

def test_boundary_sizes():
    for n in (0, 1, 12, 13, 100):
        s = fresh()
        compose(s, f"b{n}", "sb")
        burst(s, n)
        seen, guard = set(), 0
        while guard < 30:
            guard += 1
            p = compose(s, f"b{n}", "sb")
            for i in range(n):
                if f'"i": {i}' in p or f'"i":{i}' in p:
                    seen.add(i)
            if "unchanged" in p:
                break
        check(f"boundary n={n}", len(seen) == n, f"({len(seen)}/{n})")

def test_idempotent_when_quiet():
    s = fresh()
    compose(s, "a4", "s4")
    p1 = compose(s, "a4", "s4")
    p2 = compose(s, "a4", "s4")
    check("quiet fleet -> unchanged payload", "unchanged" in p2 and len(p2) < 200,
          f"({len(p1)}/{len(p2)} chars)")

def test_first_contact_is_full():
    s = fresh()
    p = compose(s, "a5", "s5")
    check("first contact carries manual", "GO DEEPER" in p.upper() or "MANUAL" in p.upper(),
          f"({len(p)} chars)")

if __name__ == "__main__":
    print("delivery correctness:")
    test_no_event_loss_on_burst()
    test_watermark_never_outruns_render()
    test_pagination_signal_present()
    test_boundary_sizes()
    test_idempotent_when_quiet()
    test_first_contact_is_full()
    print(f"\n{'ALL PASS' if not FAILED else 'FAILURES: ' + ', '.join(FAILED)}")
    sys.exit(1 if FAILED else 0)
