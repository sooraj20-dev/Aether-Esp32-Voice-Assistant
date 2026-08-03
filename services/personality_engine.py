"""
services/personality_engine.py — Keyword-driven personality trait delta computation.

Design goals
------------
• Zero external dependencies — pure Python keyword matching.
• Synchronous and fast (< 1 ms per call).
• Returns small fractional deltas so personality shifts are gradual.
• Each trait is capped at ±1.0 per conversation turn before being
  written to the DB by memory_store.update_personality().

Personality traits (0–100 scale)
---------------------------------
  humor_level         — sense of humour / playfulness
  curiosity_level     — desire to explain and explore topics
  friendliness_level  — warmth and personal engagement
  technical_depth     — inclination toward technical detail
  encouragement_level — motivational / supportive tone

Signal → trait mapping
-----------------------
  Signal cluster                      → trait            delta
  ---------------------------------------------------------------
  Programming / electronics / IoT kw  → technical_depth  +0.5
  Joke / funny / humour kw            → humor_level       +0.5
  Personal question / greeting kw     → friendliness      +0.5
  "Explain" / "tell me about" kw      → curiosity         +0.5
  Failure / exam / goal / try kw      → encouragement     +0.5

Multiple signals can fire in one turn, but each trait delta is
independently clamped to [-1, +1] before being applied.
"""

from __future__ import annotations

import re

# ─────────────────────────────────────────────────────────────────────────────
# Signal clusters
# ─────────────────────────────────────────────────────────────────────────────

_SIGNALS: list[tuple[str, float, list[str]]] = [
    # (trait_name, delta_per_signal, [keywords / phrases])
    (
        "technical_depth", 0.5,
        [
            "code", "coding", "program", "programming", "algorithm", "function",
            "variable", "class", "object", "api", "library", "framework",
            "esp32", "esp8266", "arduino", "raspberry pi", "microcontroller",
            "iot", "internet of things", "sensor", "gpio", "i2c", "spi",
            "wifi", "bluetooth", "mqtt", "firmware", "embedded",
            "python", "javascript", "c++", "rust", "java", "typescript",
            "database", "sql", "server", "cloud", "docker", "linux",
            "circuit", "pcb", "resistor", "capacitor", "transistor",
            "voltage", "current", "ohm", "watt", "ampere",
            "machine learning", "neural network", "ai model", "dataset",
            "debug", "debugging", "error", "exception", "compile", "build",
        ],
    ),
    (
        "humor_level", 0.5,
        [
            "joke", "jokes", "funny", "fun", "lol", "lmao", "haha", "hehe",
            "laugh", "laughing", "hilarious", "humour", "humor", "witty",
            "wit", "pun", "sarcastic", "sarcasm", "roast", "meme",
            "just kidding", "jk", "kidding", "playful", "silly",
            "tell me a joke", "make me laugh", "cheer me up",
        ],
    ),
    (
        "friendliness_level", 0.5,
        [
            "how are you", "how are u", "are you okay", "are you fine",
            "how do you feel", "what do you think", "do you like",
            "do you enjoy", "what's your favourite", "your opinion",
            "your thoughts", "your name", "who are you", "about yourself",
            "tell me about you", "personal", "friend", "buddy", "pal",
            "miss you", "glad you're here", "love talking to you",
            "i feel", "i'm feeling", "im feeling", "my day", "my life",
            "my family", "my friend", "my problem",
        ],
    ),
    (
        "curiosity_level", 0.5,
        [
            "explain", "explanation", "tell me about", "tell me more",
            "what is", "what are", "what does", "how does", "how do",
            "why does", "why is", "why are", "how does it work",
            "how it works", "what happens", "curious", "i wonder",
            "interesting", "teach me", "can you teach", "learn",
            "learning", "understand", "help me understand",
            "in detail", "more detail", "elaborate", "describe",
        ],
    ),
    (
        "encouragement_level", 0.5,
        [
            "failed", "failure", "fail", "failing", "gave up", "give up",
            "i lost", "i didn't win", "didn't make it", "couldn't do it",
            "exam", "test", "result", "results", "score", "grade",
            "interview", "rejected", "rejection", "not selected",
            "goal", "goals", "dream", "aspiration", "ambition",
            "try again", "another attempt", "second chance",
            "keep going", "won't give up", "wont give up",
            "struggle", "struggling", "hard time", "difficult",
            "challenge", "challenging", "setback", "obstacle",
            "motivate", "motivation", "inspire", "inspiration",
        ],
    ),
]

# Pre-compile patterns for speed
_COMPILED_SIGNALS: list[tuple[str, float, list[re.Pattern]]] = []
for _trait, _delta, _keywords in _SIGNALS:
    _patterns = [
        re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
        for kw in _keywords
    ]
    _COMPILED_SIGNALS.append((_trait, _delta, _patterns))


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def compute_personality_deltas(text: str) -> dict[str, float]:
    """
    Analyse `text` and return a dict of trait → delta to apply.

    Rules
    -----
    • A signal fires if at least one keyword matches.
    • Each fired signal contributes its base delta to that trait.
    • Multiple signals for the same trait accumulate, but the final
      per-trait delta is clamped to [-1.0, +1.0].
    • Traits with no signal are omitted from the result.

    The caller (ai_brain) passes the result directly to
    memory_store.update_personality().
    """
    if not text or not text.strip():
        return {}

    raw: dict[str, float] = {}

    for trait, delta_per_signal, patterns in _COMPILED_SIGNALS:
        hits = sum(1 for p in patterns if p.search(text))
        if hits > 0:
            # Each extra hit beyond the first adds half the base delta
            total = delta_per_signal + (hits - 1) * (delta_per_signal * 0.5)
            raw[trait] = raw.get(trait, 0.0) + total

    # Clamp each trait to ±1.0
    return {trait: max(-1.0, min(1.0, val)) for trait, val in raw.items()}


def personality_to_prompt_block(profile: dict[str, float]) -> str:
    """
    Convert a personality profile dict into a concise prompt block
    for injection into the Ollama system prompt.

    Example output
    --------------
    Personality — Friendliness: 72 | Humor: 35 | Technical Depth: 88 |
    Encouragement: 74 | Curiosity: 61

    Let these levels subtly shape your tone and phrasing:
    • High friendliness  → warm, personal, empathetic
    • High humor         → light wit, occasional wordplay (keep it tasteful)
    • High technical     → precise terminology, depth when relevant
    • High encouragement → uplifting, solution-oriented
    • High curiosity     → inquisitive follow-ups, love of explanation
    """
    h  = round(profile.get("humor_level",         20.0))
    cu = round(profile.get("curiosity_level",      50.0))
    fr = round(profile.get("friendliness_level",   60.0))
    td = round(profile.get("technical_depth",      70.0))
    en = round(profile.get("encouragement_level",  60.0))

    return (
        f"Personality — Friendliness: {fr} | Humor: {h} | "
        f"Technical Depth: {td} | Encouragement: {en} | Curiosity: {cu}. "
        "Let these levels subtly shape your tone: "
        "high friendliness means be warm and personal; "
        "high humor means add light wit when appropriate; "
        "high technical depth means use precise detail; "
        "high encouragement means be uplifting and solution-focused; "
        "high curiosity means offer to explain further."
    )
