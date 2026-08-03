"""
services/ai_brain.py — Ollama LLM client with in-memory conversation history,
Emotional Memory System, and Personality Evolution System.

Architecture additions (non-breaking)
--------------------------------------
ConversationContext gains:
  _personality_cache  — in-process copy of the global personality profile
  _emotion_cache      — last N emotions loaded from DB for this session

stream_ai() now:
  1. Extracts emotion from user text   (sync, < 1 ms)
  2. Stores emotion in DB if non-neutral
  3. Computes personality deltas        (sync, < 1 ms)
  4. Applies deltas → updates global profile + audit log
  5. Builds a dynamic system prompt with personality + emotion context
  6. Streams the Ollama response as before

All existing behaviour (streaming, history, extra context, voice formatting)
is preserved unchanged.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import AsyncGenerator

import httpx

import config
from services.memory_store import (
    add_emotion,
    add_message,
    get_personality,
    get_recent_emotions,
    get_recent_messages,
    get_user_profile,
    prune_messages,
    update_personality,
)
from services.emotion_engine import extract_emotion, summarise_emotion_context
from services.personality_engine import compute_personality_deltas, personality_to_prompt_block
from services.user_learner import extract_and_save_user_facts


# ─────────────────────────────────────────────────────────────────────────────
# Conversation context
# ─────────────────────────────────────────────────────────────────────────────

class ConversationContext:
    """Sliding-window conversation history persisted in SQLite.

    New fields
    ----------
    _personality_cache : dict  — global personality profile, refreshed each turn
    _emotion_cache     : list  — recent emotions for this session, refreshed each turn
    """

    def __init__(self, max_turns: int = 6, session_id: str = "default") -> None:
        self.max_turns  = max_turns
        self.session_id = session_id
        self.last_activity = time.time()

        # Warm caches on construction (lightweight DB reads)
        self._personality_cache: dict[str, float] = get_personality()
        self._emotion_cache: list[dict] = get_recent_emotions(session_id, limit=5)

    def clear_if_stale(self, timeout_s: float) -> None:
        # Keep persistent history; just update activity timestamp
        self.last_activity = time.time()

    def add_user(self, text: str) -> None:
        add_message("user", text, self.session_id)
        prune_messages(self.session_id, keep_limit=100)
        self.last_activity = time.time()

    def add_assistant(self, text: str) -> None:
        add_message("assistant", text, self.session_id)
        prune_messages(self.session_id, keep_limit=100)
        self.last_activity = time.time()

    def get_messages(self, system_prompt: str) -> list[dict[str, str]]:
        history = get_recent_messages(limit=self.max_turns * 2, session_id=self.session_id)
        return [{"role": "system", "content": system_prompt}, *history]

    # ── Cache refresh helpers ─────────────────────────────────────────────────

    def refresh_personality(self) -> None:
        """Reload the global personality profile from DB into the in-process cache."""
        self._personality_cache = get_personality()

    def refresh_emotions(self) -> None:
        """Reload recent emotions for this session from DB into the in-process cache."""
        self._emotion_cache = get_recent_emotions(self.session_id, limit=5)


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [LLM] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ─────────────────────────────────────────────────────────────────────────────

def _base_instructions() -> str:
    """Return the core AETHER persona instructions."""
    return (
        "You are AETHER, a warm, fast, Alexa-style smart speaker. "
        "Detect the user's language and reply in the same language: English, Malayalam, or Manglish. "
        "Answer the user's actual request immediately. "
        f"Use at most {config.MAX_REPLY_SENTENCES} sentences and around {config.MAX_REPLY_WORDS} words. "
        "Format strictly for spoken audio. No markdown, bullets, lists, URLs, or emoji. "
        "Expand abbreviations for speech: et cetera instead of etc., for example instead of e.g. "
        "Spell out numbers and symbols when clearer for speech."
    )


def _build_system_prompt(
    personality: dict[str, float],
    emotion_context: str,
    user_profile: list[dict] | None = None,
) -> str:
    """
    Assemble the full dynamic system prompt.

    Structure
    ---------
    [Base AETHER persona instructions]
    ---
    [Known user facts]       ← only when profile data exists (high/medium confidence)
    ---
    [Personality profile block]
    ---
    [Emotional context block] ← only when emotion data exists
    """
    parts: list[str] = [_base_instructions()]

    # ── User profile block ─────────────────────────────────────────────────
    # Only high/medium confidence facts are injected to keep the prompt lean.
    # Capped at 30 facts in the prompt even if 50 are stored in the DB.
    if user_profile:
        quality_facts = [
            f"- {item['fact']}"
            for item in user_profile
            if item.get("confidence") in ("high", "medium")
        ]
        if quality_facts:
            facts_block = "\n".join(quality_facts[:30])
            parts.append(
                "Known facts about this user — adapt your tone, depth, and examples accordingly:\n"
                + facts_block
            )

    # ── Personality block ───────────────────────────────────────────────
    personality_block = personality_to_prompt_block(personality)
    parts.append(personality_block)

    # ── Emotional context block (only when meaningful) ───────────────────
    if emotion_context:
        parts.append(
            "Emotional context — " + emotion_context + " "
            "Adapt your response tone empathetically to this emotional state."
        )

    return "\n\n".join(parts)


def _extra_context(user_text: str) -> str:
    """Inject simple local facts (time/date) without external APIs."""
    lower = user_text.lower()
    if re.search(r"\b(time|date|day)\b", lower):
        now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
        return f"Current local time: {now}."
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Voice text cleaner (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def _clean_voice_delta(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"[*#`_>~]+", "", text)
    text = re.sub(r"(?m)^\s*[-+]\s+", "", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", text)
    text = text.replace("etc.", "et cetera")
    text = text.replace("e.g.", "for example")
    text = text.replace("i.e.", "that is")
    text = text.replace("vs.", "versus")
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Emotion + personality processing (runs before Ollama call)
# ─────────────────────────────────────────────────────────────────────────────

def _process_user_turn(
    user_text: str,
    context: ConversationContext,
) -> tuple[str, str]:
    """
    Extract emotion and compute personality deltas from the user's utterance.

    Side effects
    ------------
    • Stores the detected emotion in SQLite (if non-neutral).
    • Applies personality deltas to the global profile in SQLite + audit log.
    • Refreshes both in-process caches on the context object.

    Returns
    -------
    (emotion, confidence) — the detected emotion for this turn.
    """
    # 1. Emotion extraction (< 1 ms)
    emotion, confidence = extract_emotion(user_text)
    _log(f"Emotion: {emotion} ({confidence})")

    if emotion != "neutral" and confidence != "low":
        add_emotion(
            session_id=context.session_id,
            emotion=emotion,
            confidence=confidence,
            source_text=user_text,
        )
        context.refresh_emotions()

    # 2. Personality delta computation (< 1 ms)
    deltas = compute_personality_deltas(user_text)
    if deltas:
        _log(f"Personality deltas: {deltas}")
        updated = update_personality(
            deltas=deltas,
            trigger_session_id=context.session_id,
            trigger_text=user_text,
        )
        context._personality_cache = updated
    else:
        context.refresh_personality()

    return emotion, confidence


# ─────────────────────────────────────────────────────────────────────────────
# Main streaming function
# ─────────────────────────────────────────────────────────────────────────────

async def stream_ai(
    user_text: str,
    context: ConversationContext | None = None,
) -> AsyncGenerator[str, None]:
    """Stream text tokens from local Ollama, with emotional and personality context."""

    # ── 1. Emotion + personality processing ──────────────────────────────────
    detected_emotion = "neutral"
    detected_confidence = "low"

    if context is not None:
        context.clear_if_stale(config.CONTEXT_TIMEOUT_S)
        detected_emotion, detected_confidence = _process_user_turn(user_text, context)
        context.add_user(user_text)

        # Build emotion context summary from the (now-refreshed) cache
        emotion_context = summarise_emotion_context(context._emotion_cache)

        # Read user profile fresh from DB each turn (no in-process cache)
        # This is a fast single SQLite read (~0.5 ms for 50 facts).
        user_profile = get_user_profile()
        system_prompt = _build_system_prompt(
            context._personality_cache, emotion_context, user_profile
        )

        messages = context.get_messages(system_prompt)
    else:
        # No context object — use defaults (personality/emotion still logged but
        # caches can't be refreshed without a context object)
        user_profile = get_user_profile()
        system_prompt = _build_system_prompt(
            get_personality(),
            "",
            user_profile,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_text},
        ]

    # ── 2. Time / date injection (unchanged) ─────────────────────────────────
    extra = _extra_context(user_text)
    if extra:
        messages.insert(1, {"role": "system", "content": extra})

    # ── 3. Log the assembled prompt (truncated) for debugging ────────────────
    _log(f"Ollama {config.OLLAMA_MODEL} | emotion={detected_emotion}")

    # ── 4. Ollama streaming call (unchanged) ─────────────────────────────────
    url = f"{config.OLLAMA_URL}/api/chat"
    payload = {
        "model":    config.OLLAMA_MODEL,
        "messages": messages,
        "stream":   True,
        "options":  {"temperature": 0.2, "num_predict": 80},   # 80 tokens >> MAX_REPLY_WORDS=28; avoids over-generation
        "keep_alive": -1,
    }

    timeout = httpx.Timeout(
        connect=5.0,
        read=config.LLM_STREAM_TIMEOUT,
        write=5.0,
        pool=5.0,
    )
    full_reply: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    raise RuntimeError(
                        f"Ollama {response.status_code}: "
                        f"{body[:240].decode(errors='ignore')}"
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = chunk.get("message") or {}
                    delta = _clean_voice_delta(message.get("content") or "")
                    if delta:
                        full_reply.append(delta)
                        yield delta
    except httpx.ConnectError:
        raise RuntimeError(
            f"Cannot reach Ollama at {config.OLLAMA_URL}. "
            f"Run: ollama pull {config.OLLAMA_MODEL}"
        ) from None

    if not full_reply:
        raise RuntimeError("Ollama returned no text")

    # ── 5. Store assistant reply ───────────────────────────────────────────────
    full_reply_text = "".join(full_reply).strip()
    if context is not None:
        context.add_assistant(full_reply_text)

    # ── 5b. Spawn background user-fact extraction ───────────────────────────
    # Fires AFTER TTS is already streaming to the ESP32 — zero latency impact.
    # The task runs concurrently and stores newly learned user facts to SQLite.
    # On the very next turn, get_user_profile() will return the updated profile.
    if context is not None and full_reply_text:
        asyncio.create_task(
            extract_and_save_user_facts(
                context.session_id,
                user_text,
                full_reply_text,
            )
        )

    # ── 6. Yield the detected emotion as a sentinel for pipeline.py ─────────
    # We cannot return a value from an async generator, so we communicate the
    # detected emotion via a side-channel attribute on the context object.
    if context is not None:
        context._last_emotion     = detected_emotion
        context._last_confidence  = detected_confidence
