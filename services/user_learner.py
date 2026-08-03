"""
services/user_learner.py — Background user-fact extraction from conversation turns.

How it fits into the pipeline
------------------------------
After each voice turn, pipeline.py has already streamed the TTS reply to the ESP32.
ai_brain.stream_ai() then spawns:

    asyncio.create_task(
        extract_and_save_user_facts(session_id, user_text, assistant_reply)
    )

This module handles the full extraction lifecycle:

  1. Skip trivial turns (< 5 user words — nothing to learn).
  2. Build a compact extraction prompt.
  3. Call the local Ollama model (same model already warm in RAM).
  4. Parse the JSON array from the response.
  5. Validate each fact (length, structure).
  6. Filter sensitive data  (phone numbers, emails, passwords, addresses, …).
  7. Normalise facts         (capitalise, ensure period, trim whitespace).
  8. Deduplicate             (word-overlap check against the existing DB profile).
  9. Persist via upsert_user_facts() with confidence labels.

Design goals
------------
• Zero latency impact — runs fully after TTS is already streaming.
• Never crashes the main voice flow — all exceptions are caught and logged.
• No external dependencies — only httpx (already in requirements.txt) + re + json.
• Uses the same Ollama model that is already warm, so no extra RAM.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

import httpx

import config
from services.memory_store import get_user_profile, upsert_user_facts


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [Learner] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Sensitive-data filter
# ─────────────────────────────────────────────────────────────────────────────

# Patterns that flag a fact as containing sensitive personal data.
# If any pattern matches, the entire fact is silently discarded.
_SENSITIVE_PATTERNS: list[re.Pattern] = [
    # Phone numbers (7+ consecutive digits, with optional separators)
    re.compile(r'\b\d[\d\s\-()]{6,}\d\b'),
    # Email addresses
    re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'),
    # Credential keywords — any fact mentioning passwords/tokens is filtered
    re.compile(
        r'\b(password|passwd|pwd|secret|token|api[_\s]?key|auth|credential|pin)\b',
        re.IGNORECASE,
    ),
    # Credit / debit card patterns (4 groups of 4 digits)
    re.compile(r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b'),
    # Physical home address signals
    re.compile(
        r'\b(home address|house number|street|postal code|zip code|'
        r'lives at|staying at|my address|flat no|apartment)\b',
        re.IGNORECASE,
    ),
    # National ID / Aadhaar-style 12-digit numbers
    re.compile(r'\b\d{12}\b'),
]


def _is_sensitive(fact: str) -> bool:
    """Return True if the fact string contains potentially sensitive data."""
    return any(p.search(fact) for p in _SENSITIVE_PATTERNS)


# ─────────────────────────────────────────────────────────────────────────────
# Fact normalisation
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_fact(raw: str) -> str:
    """
    Clean and standardise a raw fact string.

    Steps
    -----
    1. Strip surrounding whitespace.
    2. Collapse internal runs of whitespace to a single space.
    3. Capitalise the first character.
    4. Ensure the fact ends with a full stop.
    """
    fact = re.sub(r'\s+', ' ', raw.strip())
    if not fact:
        return ""
    # Capitalise first character without lowercasing the rest
    fact = fact[0].upper() + fact[1:]
    # Ensure trailing period
    if not fact[-1] in ".!?":
        fact += "."
    return fact


# ─────────────────────────────────────────────────────────────────────────────
# Semantic deduplication (word-overlap)
# ─────────────────────────────────────────────────────────────────────────────

# Common English stopwords to skip when computing word overlap, so that
# "Is a student" and "Is studying engineering" are not falsely merged.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "has", "have", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "can", "could", "to", "of", "in", "on",
    "at", "by", "for", "with", "about", "from", "and", "or", "but",
    "not", "that", "this", "it", "he", "she", "they", "we", "i",
    "my", "your", "his", "her", "our", "their", "user", "the",
})

_DUP_THRESHOLD = 0.65  # Jaccard-style overlap ratio above which a fact is a duplicate


def _content_words(sentence: str) -> set[str]:
    """Return non-stopword lowercase words from a sentence."""
    words = re.findall(r"[a-zA-Z']+", sentence.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _is_duplicate(new_fact: str, existing_facts: list[dict]) -> bool:
    """
    Return True if `new_fact` is semantically too similar to any existing fact.

    Uses a simplified Jaccard similarity on content words (stopwords excluded).
    Threshold: >= 65 % overlap → considered duplicate.
    """
    new_words = _content_words(new_fact)
    if not new_words:
        return False

    for item in existing_facts:
        ex_words = _content_words(item["fact"])
        if not ex_words:
            continue
        intersection = len(new_words & ex_words)
        union        = len(new_words | ex_words)
        if union > 0 and (intersection / union) >= _DUP_THRESHOLD:
            return True
    return False


def _filter_new_facts(
    candidates: list[dict],
    existing: list[dict],
) -> list[dict]:
    """
    From `candidates`, return only those that are not duplicates of
    anything already in `existing` or of each other.
    """
    accepted: list[dict] = []
    # Build a running pool that includes already-accepted candidates
    # so we don't save two very similar facts in the same batch.
    pool = list(existing)

    for item in candidates:
        if not _is_duplicate(item["fact"], pool):
            accepted.append(item)
            pool.append(item)

    return accepted


# ─────────────────────────────────────────────────────────────────────────────
# Extraction prompt
# ─────────────────────────────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """\
You are a memory extraction system for a voice assistant. Given ONE conversation \
turn, extract facts about THE USER ONLY.

Output ONLY a valid JSON array of objects. Each object MUST have exactly two keys:
  "fact"       : a short clear English sentence about the user (max 15 words)
  "confidence" : one of "high", "medium", or "low"

Confidence guide:
  high   — explicitly stated by the user ("I am a student")
  medium — strongly implied ("can you explain this simply" → user is a learner)
  low    — a weak inference

Rules:
  • Max 5 facts per turn. If nothing new learned, output: []
  • Facts about the USER only, not the assistant.
  • No sensitive data: no phone numbers, emails, passwords, home addresses.
  • Output ONLY the JSON array — no explanation, no markdown, no code block.

Example output:
[
  {{"fact": "Is learning Python programming.", "confidence": "high"}},
  {{"fact": "Works with ESP32 microcontrollers.", "confidence": "high"}},
  {{"fact": "Prefers concise, direct answers.", "confidence": "medium"}}
]

User said: "{user_text}"
Assistant replied: "{assistant_reply}"

Output:\
"""


# ─────────────────────────────────────────────────────────────────────────────
# Main background coroutine
# ─────────────────────────────────────────────────────────────────────────────

async def extract_and_save_user_facts(
    session_id: str,
    user_text: str,
    assistant_reply: str,
) -> None:
    """
    Background coroutine: extract user facts from one conversation turn,
    apply all safety and dedup filters, and persist to SQLite.

    This should always be called via asyncio.create_task() so it runs
    concurrently without blocking the voice pipeline.

    Failures are logged but never re-raised — the main voice flow must
    never be interrupted by a background learning failure.
    """
    # ── 1. Skip trivial turns ─────────────────────────────────────────────────
    user_words = user_text.strip().split()
    if len(user_words) < 5:
        return  # Nothing meaningful to extract from a one-liner

    # ── 2. Build extraction prompt ────────────────────────────────────────────
    prompt = _EXTRACTION_PROMPT.format(
        user_text     = user_text[:500],
        assistant_reply = assistant_reply[:300],
    )

    # ── 3. Call Ollama (non-streaming, compact response) ──────────────────────
    url = f"{config.OLLAMA_URL}/api/generate"
    payload = {
        "model":      config.OLLAMA_MODEL,
        "prompt":     prompt,
        "stream":     False,
        "options":    {"temperature": 0.1, "num_predict": 220},
        "keep_alive": -1,
    }
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            _log(f"Ollama returned HTTP {resp.status_code} — skipping fact extraction.")
            return
        raw_text: str = resp.json().get("response", "").strip()
    except asyncio.CancelledError:
        raise  # Let the cancellation propagate normally
    except Exception as exc:
        _log(f"Ollama request failed: {exc}")
        return

    # ── 4. Parse JSON array ───────────────────────────────────────────────────
    try:
        # Robustly find the JSON array even if the model wraps it in text/markdown
        match = re.search(r'\[.*?\]', raw_text, re.DOTALL)
        if not match:
            _log(f"No JSON array in response: {raw_text[:100]!r}")
            return
        facts_raw: list = json.loads(match.group(0))
        if not isinstance(facts_raw, list):
            _log("Parsed JSON is not a list — skipping.")
            return
    except json.JSONDecodeError as exc:
        _log(f"JSON parse error ({exc}) — raw: {raw_text[:100]!r}")
        return

    # ── 5. Validate + normalise + sensitive-data filter ───────────────────────
    validated: list[dict] = []
    for item in facts_raw:
        if not isinstance(item, dict):
            continue

        raw_fact   = item.get("fact", "")
        confidence = item.get("confidence", "medium")

        # Type and length guard
        if not isinstance(raw_fact, str) or not raw_fact.strip():
            continue
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"

        fact = _normalize_fact(raw_fact)
        if len(fact) < 10 or len(fact) > 150:
            continue  # Too short (likely noise) or suspiciously long

        # Sensitive-data gate
        if _is_sensitive(fact):
            _log(f"Sensitive fact suppressed: {fact[:50]!r}")
            continue

        validated.append({"fact": fact, "confidence": confidence})

    if not validated:
        _log("No valid facts extracted this turn.")
        return

    # ── 6. Deduplicate against the existing profile ───────────────────────────
    existing = get_user_profile()  # Always fresh read — no cache
    new_only  = _filter_new_facts(validated, existing)

    if not new_only:
        _log("All extracted facts were duplicates of existing profile — nothing saved.")
        return

    # ── 7. Persist ────────────────────────────────────────────────────────────
    source = f"{session_id}@{datetime.utcnow().strftime('%Y-%m-%dT%H:%M')}"
    upsert_user_facts(new_only, source)
    _log(
        f"Saved {len(new_only)} new fact(s): "
        + ", ".join(f"{f['fact']!r}({f['confidence']})" for f in new_only)
    )
