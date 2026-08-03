"""
services/emotion_engine.py — Lightweight keyword-based emotion extraction.

Design goals
------------
• Zero external dependencies (no NLTK, no transformers).
• Synchronous — no async/await — so it adds < 1 ms to each turn.
• Returns a (emotion, confidence) tuple ready to store in SQLite and
  inject into the Ollama system prompt.

Emotions supported
------------------
happy | excited | calm | neutral | anxious | stressed |
frustrated | sad | confused | motivated

Priority order (highest → lowest)
----------------------------------
anxious > stressed > frustrated > sad > confused >
excited > happy > motivated > calm > neutral

Confidence scoring
------------------
  2+ keyword hits  → "high"
  1 keyword hit    → "medium"
  0 keyword hits   → "low"  (caller should store as neutral, skip storage)
"""

from __future__ import annotations

import re

# ─────────────────────────────────────────────────────────────────────────────
# Keyword clusters (ordered by specificity / emotional salience)
# ─────────────────────────────────────────────────────────────────────────────

# Each entry: (emotion_label, [keyword_or_phrase, ...])
# Phrases are matched as whole words or sub-phrases (case-insensitive).

_EMOTION_KEYWORDS: list[tuple[str, list[str]]] = [
    ("anxious", [
        "worried", "worrying", "worry", "nervous", "anxious", "anxiety",
        "scared", "afraid", "fear", "fearful", "panic", "panicking",
        "dread", "dreading", "uneasy", "tense", "terrified", "apprehensive",
        "what if", "can't stop thinking", "overthinking",
    ]),
    ("stressed", [
        "stressed", "stress", "overwhelmed", "overwhelm", "pressure",
        "deadline", "deadlines", "too much", "so much to do", "burnt out",
        "burnout", "exhausted", "overloaded", "can't cope", "swamped",
        "hectic", "no time",
    ]),
    ("frustrated", [
        "frustrated", "frustrating", "annoyed", "annoying", "irritated",
        "irritating", "angry", "anger", "mad", "furious", "upset",
        "hate this", "hate it", "ugh", "this is stupid", "doesn't work",
        "not working", "keeps failing", "keeps breaking",
    ]),
    ("sad", [
        "sad", "unhappy", "depressed", "depression", "miserable",
        "crying", "cried", "tears", "heartbroken", "terrible", "awful",
        "failed", "failure", "lost", "gave up", "hopeless", "alone",
        "lonely", "miss", "missing", "grief", "mourning",
    ]),
    ("confused", [
        "confused", "confusing", "don't understand", "dont understand",
        "not sure", "unclear", "lost", "what do you mean", "huh",
        "i'm confused", "im confused", "makes no sense", "doesn't make sense",
        "what is", "how does", "can you explain", "please explain",
        "i don't get it", "i dont get it",
    ]),
    ("excited", [
        "excited", "exciting", "can't wait", "cant wait", "so pumped",
        "pumped", "awesome", "incredible", "amazing", "wow", "omg",
        "oh my god", "thrilled", "stoked", "hyped", "love this",
        "this is great", "so cool", "brilliant",
    ]),
    ("happy", [
        "happy", "happiness", "glad", "great", "wonderful", "fantastic",
        "joy", "joyful", "delighted", "pleased", "excellent", "perfect",
        "love", "loving", "thankful", "grateful", "good news", "good day",
        "had a great", "feeling good", "so good",
    ]),
    ("motivated", [
        "motivated", "motivation", "determined", "determination",
        "ready", "let's go", "lets go", "focused", "can do this",
        "will do", "pumped up", "going to", "gonna do", "starting",
        "new plan", "fresh start", "try again", "keep going", "won't give up",
        "wont give up", "keep trying",
    ]),
    ("calm", [
        "calm", "calming", "relaxed", "relaxing", "peaceful", "peace",
        "okay", "fine", "alright", "comfortable", "content", "settled",
        "no worries", "all good", "chill", "chilling", "at ease",
    ]),
    # neutral is the catch-all — no keywords needed
]

# Pre-compile per-emotion pattern sets for speed
_COMPILED: list[tuple[str, list[re.Pattern]]] = []
for _emotion, _keywords in _EMOTION_KEYWORDS:
    _patterns = [
        re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
        for kw in _keywords
    ]
    _COMPILED.append((_emotion, _patterns))


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def extract_emotion(text: str) -> tuple[str, str]:
    """
    Detect the dominant emotion in `text`.

    Returns
    -------
    (emotion, confidence)
        emotion    : one of the 10 emotion labels or "neutral"
        confidence : "high" | "medium" | "low"

    The caller should skip DB storage when confidence == "low" (neutral).
    """
    if not text or not text.strip():
        return "neutral", "low"

    for emotion, patterns in _COMPILED:
        hits = sum(1 for p in patterns if p.search(text))
        if hits >= 2:
            return emotion, "high"
        if hits == 1:
            return emotion, "medium"

    return "neutral", "low"


def emotion_decay_weight(age_days: float) -> float:
    """
    Return a 0–1 importance weight for an emotion given its age in days.

    Decay schedule (as specified):
        0–1 day   → 1.00  (100 %)
        1–7 days  → 0.75  (≈ 50 % towards the 1-week target)
        7–30 days → 0.50
        >30 days  → 0.10
    """
    if age_days <= 1:
        return 1.00
    if age_days <= 7:
        return 0.75
    if age_days <= 30:
        return 0.50
    return 0.10


def summarise_emotion_context(recent_emotions: list[dict]) -> str:
    """
    Produce a concise natural-language summary of recent emotional state
    for injection into the Ollama system prompt.

    `recent_emotions` is the list returned by memory_store.get_recent_emotions().
    Returns an empty string if no meaningful emotions are present.
    """
    if not recent_emotions:
        return ""

    # Weighted vote across recent emotions
    scores: dict[str, float] = {}
    for entry in recent_emotions:
        em = entry["emotion"]
        if em == "neutral":
            continue
        conf = entry["confidence"]
        weight = entry.get("decay_weight", 1.0)
        conf_mult = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(conf, 0.5)
        scores[em] = scores.get(em, 0.0) + weight * conf_mult

    if not scores:
        return ""

    dominant = max(scores, key=lambda k: scores[k])
    # Find most recent example text for the dominant emotion
    example = next(
        (e["source_text"] for e in recent_emotions if e["emotion"] == dominant),
        "",
    )
    example_snippet = (example[:80] + "…") if len(example) > 80 else example

    lines = [f"Recent emotional state: {dominant}"]
    if example_snippet:
        lines.append(f'Context: "{example_snippet}"')

    # Mention secondary emotion if strong enough
    secondary_scores = {k: v for k, v in scores.items() if k != dominant}
    if secondary_scores:
        secondary = max(secondary_scores, key=lambda k: secondary_scores[k])
        if secondary_scores[secondary] >= scores[dominant] * 0.6:
            lines.append(f"Secondary emotion: {secondary}")

    return "\n".join(lines)
