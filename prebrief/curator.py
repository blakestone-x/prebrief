"""prebrief.curator — optional local-model compression of oversized payloads.

Asks a local Ollama instance to rank and compress an injection payload for a
specific task. Purely optional: no Ollama (or any error at all) degrades to
deterministic truncation. The only non-stdlib touch in the package is urllib
against localhost.
"""
import json
import urllib.request

TIMEOUT_S = 20
MODEL = "qwen3:14b"


def curate(task, text, budget_chars, url="http://127.0.0.1:11434"):
    """Compress `text` to fit `budget_chars`, keeping what matters most for
    `task`. Deterministic truncation on any failure."""
    if not text:
        return ""
    if len(text) <= budget_chars:
        return text
    try:
        prompt = (
            "You compress fleet-status briefings for an AI coding agent. "
            f"The agent's current task: {str(task)[:200]}\n"
            f"Rewrite the briefing below to under {int(budget_chars)} characters. "
            "Keep every decision, build reference (build:N, dN), and risk line "
            "relevant to the task; drop repetition and low-value lines; keep "
            "the section headers. Output ONLY the compressed briefing.\n\n"
            + text[:12000])
        body = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0},
        }).encode("utf-8")
        req = urllib.request.Request(
            url.rstrip("/") + "/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        out = str(data.get("message", {}).get("content", "")).strip()
        if out:
            return out[:budget_chars]
    except Exception:
        pass
    return text[:budget_chars]
