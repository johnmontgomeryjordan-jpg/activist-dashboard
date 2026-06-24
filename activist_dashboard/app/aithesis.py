"""
AI thesis layer — re-voice the deterministic, fact-grounded pitch (pitch.py) into sharper
prose with Claude Haiku.

STRICTLY constrained: the model may only rephrase the numbers, names and labels we already
computed — it must never add a fact. The deterministic pitch remains the source of truth and
the fallback, and the (deterministic) evidence cards are always shown alongside, so the AI
layer is purely a readability polish, never a source of new claims.

Cached per company by a hash of the input facts, so we call the API once per fact-set
(pennies/month). If ANTHROPIC_API_KEY is unset or any call fails, revoice() returns None and
callers keep the templated pitch — the feature is entirely additive and degrades safely.
"""
import os
import json
import hashlib

import requests

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = (
    "You are a senior analyst at a shareholder-activism DEFENSE advisory firm. You turn an "
    "internal, fact-checked draft explaining why an activist investor might target a company "
    "into crisp, persuasive prose for a partner's pitch.\n"
    "ABSOLUTE RULES:\n"
    "- Use ONLY the facts, numbers, percentages, dollar figures, names and dates present in "
    "the draft. NEVER introduce any figure or claim not in the draft.\n"
    "- Do not exaggerate, soften, or add hype words ('massive', 'huge', 'incredible').\n"
    "- Be concrete and tight. Plain professional English.\n"
    "- Return STRICT JSON only, no preamble, no code fences."
)


def key():
    return os.getenv("ANTHROPIC_API_KEY", "")


def model():
    return os.getenv("AI_MODEL", DEFAULT_MODEL)


def facts_hash(name, pitch, facts):
    payload = {"n": name, "t": (pitch or {}).get("thesis"),
               "p": (pitch or {}).get("points"), "f": facts}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _prompt(name, pitch, facts):
    draft = {
        "company": name,
        "archetype": (pitch or {}).get("archetype"),
        "thesis_draft": (pitch or {}).get("thesis"),
        "talking_points_draft": (pitch or {}).get("points") or [],
        "supporting_facts": facts or [],
    }
    return ("Rewrite the draft below into sharper prose. Return JSON with EXACTLY this shape:\n"
            '{"thesis": "<2-3 sentence thesis>", "points": ["<one sentence>", ...]}\n'
            "Keep the same number of talking points as the draft. Each point is one sentence. "
            "Use only facts that appear in the draft.\n\nDRAFT:\n" + json.dumps(draft, indent=2))


def _extract_json(text):
    s = (text or "").strip()
    i, j = s.find("{"), s.rfind("}")
    return s[i:j + 1] if (i != -1 and j != -1 and j > i) else s


def revoice(name, pitch, facts, api_key=None, mdl=None, timeout=30):
    """Return {'thesis': str, 'points': [str]} re-voiced by Haiku, or None on any failure."""
    api_key = api_key or key()
    if not api_key or not pitch or not (pitch.get("thesis")):
        return None
    body = {
        "model": mdl or model(),
        "max_tokens": 700,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": _prompt(name, pitch, facts)}],
    }
    try:
        r = requests.post(API_URL, timeout=timeout, json=body, headers={
            "x-api-key": api_key, "anthropic-version": "2023-06-01",
            "content-type": "application/json"})
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        obj = json.loads(_extract_json(text))
    except (ValueError, KeyError, TypeError):
        return None
    thesis = (obj.get("thesis") or "").strip()
    points = [p.strip() for p in (obj.get("points") or [])
              if isinstance(p, str) and p.strip()]
    if not thesis:
        return None
    return {"thesis": thesis, "points": points}
