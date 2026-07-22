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
# Bump when _SYSTEM / _prompt changes so cached re-voicings are invalidated and every name
# re-voices under the new rules (the hash keys on draft content, not the prompt, so without
# this a prompt change would only reach names whose facts also happened to change).
_PROMPT_VERSION = 3

_SYSTEM = (
    "You are a senior analyst at a shareholder-activism DEFENSE advisory firm. You turn an "
    "internal, fact-checked draft explaining why an activist investor might target a company "
    "into crisp, persuasive prose for a partner's pitch.\n"
    "ABSOLUTE RULES:\n"
    "- Use ONLY the facts, numbers, percentages, dollar figures, names and dates present in "
    "the draft. NEVER introduce any figure or claim not in the draft.\n"
    "- Do not exaggerate, soften, or add hype words ('massive', 'huge', 'incredible').\n"
    "- Preserve the EXACT financial term attached to every figure; never rename one metric as "
    "another. In particular: a return the draft calls a 'price return' is NOT a 'total return'; "
    "a 'peer cutoff' or 'bottom-quartile' threshold is NOT a 'median' or 'average'; keep terms "
    "like 'operating margin', 'return on assets', 'debt-to-assets' verbatim. If unsure what a "
    "figure is, use the draft's own wording rather than substituting a different term.\n"
    "- Do NOT characterize the company or its STOCK as 'underperforming', 'lagging', "
    "'declining', 'falling', or 'struggling' unless the draft EXPLICITLY contains a negative "
    "stock-return or return-versus-index fact. A cheap valuation, weak revenue growth, a "
    "goodwill mark, or a governance flag is NOT stock underperformance — a company can be cheap "
    "or slow-growing while its stock has RISEN sharply. If the draft has no return-lag fact, do "
    "not comment on stock or share-price performance at all.\n"
    "- Describe an executive or leadership departure neutrally (e.g. 'a recent leadership "
    "change' or 'a recent C-suite transition'); never call it a 'vacuum', a 'void', or "
    "something to 'exploit'.\n"
    "- Be concrete and tight. Plain professional English.\n"
    "- Return STRICT JSON only, no preamble, no code fences."
)


def key():
    return os.getenv("ANTHROPIC_API_KEY", "")


def model():
    return os.getenv("AI_MODEL", DEFAULT_MODEL)


def facts_hash(name, pitch, facts):
    payload = {"v": _PROMPT_VERSION, "n": name, "t": (pitch or {}).get("thesis"),
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


# --- One-line gloss for the biweekly report's headlines / filings --------------------------------
# A short, plain-English "why this matters" beneath each headline and filing in the report. Same
# discipline as revoice(): use ONLY the given text, invent nothing. Cheap (a handful of Haiku calls
# per report build) and cached per (kind, text) for the process, so previews don't re-bill.
_SUMMARY_SYSTEM = (
    "You are a senior analyst at a shareholder-activism DEFENSE advisory firm. In ONE short "
    "sentence, explain why an item (a news headline or an SEC filing) matters for spotting or "
    "defending against shareholder activism.\n"
    "ABSOLUTE RULES:\n"
    "- Use ONLY what the given text states. NEVER invent a number, name, date, or event not in it.\n"
    "- No hype words, no exaggeration. Plain professional English.\n"
    "- Do not call a stock 'underperforming'/'lagging' unless the text itself says so.\n"
    "- Return ONLY the one sentence — no preamble, no quotation marks, no label."
)
_summary_cache = {}


def summarize_line(text, kind="headline", context=None, api_key=None, mdl=None, timeout=20):
    """One-sentence analyst gloss of a report headline/filing, or None on any failure/no key.
    Grounded strictly in `text` (+ the factual `context` line the caller supplies: company,
    signal type, whether it's a covered name). Context is what stops five routine "Results"
    8-Ks all collapsing to the same boilerplate sentence."""
    text = (text or "").strip()
    if not text:
        return None
    api_key = api_key or key()
    if not api_key:
        return None
    ck = (kind, text, context or "")
    if ck in _summary_cache:
        return _summary_cache[ck]
    ctx = f"\nCONTEXT (factual, use it): {context}" if context else ""
    prompt = (f"In ONE sentence (max ~22 words), explain to an activist-defense analyst why this "
              f"{kind} matters, using ONLY the {kind} text and the context below, inventing nothing. "
              f"Be specific to THIS company — do not write a generic sentence that would fit any "
              f"company.\n\n{kind.upper()}: {text}{ctx}")
    body = {
        "model": mdl or model(),
        "max_tokens": 90,
        "system": _SUMMARY_SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        r = requests.post(API_URL, timeout=timeout, json=body, headers={
            "x-api-key": api_key, "anthropic-version": "2023-06-01",
            "content-type": "application/json"})
        if r.status_code != 200:
            return None
        data = r.json()
        out = "".join(b.get("text", "") for b in data.get("content", [])
                      if b.get("type") == "text").strip()
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    out = out.strip().strip('"').replace("\n", " ").strip()
    out = out or None
    _summary_cache[ck] = out
    return out
