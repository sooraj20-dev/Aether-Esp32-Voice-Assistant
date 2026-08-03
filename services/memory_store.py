"""
services/memory_store.py — SQLite helper functions for persistent conversation memory,
emotional state tracking, and personality profile management.

Tables
------
messages              — per-session conversation history
preferences           — key-value user preferences
conversation_emotions — per-session emotional memory with decay support
personality_profile   — single global personality profile (session_id = 'global')
personality_events    — audit log of every personality delta applied
user_profile          — NEW: persistent flat list of learned user facts with
                         confidence scores; max 50 rows, pruned by confidence+age
"""

import sqlite3
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

# Database path
DB_PATH = Path("assistant_memory.db")

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Initialize the SQLite database and create all tables if they do not exist."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()

        # ── Existing tables (unchanged) ───────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT    NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS preferences (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)"
        )

        # ── NEW: Emotional memory ─────────────────────────────────────────────
        # Stored per session_id (per device). Emotion decay is computed in Python
        # using the timestamp, not stored explicitly, to keep queries lightweight.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_emotions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT     NOT NULL,
                timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
                emotion     TEXT     NOT NULL,
                confidence  TEXT     NOT NULL,
                source_text TEXT     NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_emotions_session_ts
                ON conversation_emotions(session_id, timestamp)
        """)

        # ── NEW: Global personality profile ──────────────────────────────────
        # One row with session_id = 'global'.  All devices share one personality.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS personality_profile (
                session_id          TEXT PRIMARY KEY,
                humor_level         REAL NOT NULL DEFAULT 20.0,
                curiosity_level     REAL NOT NULL DEFAULT 50.0,
                friendliness_level  REAL NOT NULL DEFAULT 60.0,
                technical_depth     REAL NOT NULL DEFAULT 70.0,
                encouragement_level REAL NOT NULL DEFAULT 60.0,
                updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ── NEW: Personality events audit log ─────────────────────────────────
        # Records every delta applied so the evolution can be inspected / replayed.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS personality_events (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp           DATETIME DEFAULT CURRENT_TIMESTAMP,
                session_id          TEXT     NOT NULL,
                trigger_text        TEXT     NOT NULL,
                trait               TEXT     NOT NULL,
                delta               REAL     NOT NULL,
                value_after         REAL     NOT NULL
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_personality_events_ts
                ON personality_events(timestamp)
        """)

        # ── NEW: User profile — learned facts about the user ──────────────────
        # One row per unique fact string.  Confidence is "high"/"medium"/"low".
        # Pruned to 50 rows: lowest-confidence + oldest removed first.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                fact       TEXT    NOT NULL UNIQUE,
                confidence TEXT    NOT NULL DEFAULT 'medium',
                source     TEXT    NOT NULL DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_profile_confidence
                ON user_profile(confidence, updated_at)
        """)

        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Existing helpers — messages (unchanged API)
# ─────────────────────────────────────────────────────────────────────────────

def add_message(role: str, content: str, session_id: str = "default") -> None:
    """Add a message (user or assistant) to the database for a given session."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content)
        )
        conn.commit()
    finally:
        conn.close()


def get_recent_messages(limit: int = 20, session_id: str = "default") -> list[dict[str, str]]:
    """Retrieve the most recent messages for a given session, ordered chronologically."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT role, content FROM (
                SELECT id, role, content FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (session_id, limit)
        )
        rows = cursor.fetchall()
        return [{"role": row[0], "content": row[1]} for row in rows]
    finally:
        conn.close()


def prune_messages(session_id: str = "default", keep_limit: int = 100) -> None:
    """Keep only the last `keep_limit` messages for a session to prevent DB bloat."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id FROM messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT 1 OFFSET ?
            """,
            (session_id, keep_limit - 1)
        )
        row = cursor.fetchone()
        if row:
            threshold_id = row[0]
            cursor.execute(
                "DELETE FROM messages WHERE session_id = ? AND id <= ?",
                (session_id, threshold_id)
            )
            conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Emotion helpers
# ─────────────────────────────────────────────────────────────────────────────

def add_emotion(
    session_id: str,
    emotion: str,
    confidence: str,
    source_text: str,
) -> None:
    """Store a detected emotion for the given session."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO conversation_emotions (session_id, emotion, confidence, source_text)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, emotion, confidence, source_text)
        )
        conn.commit()
    finally:
        conn.close()


def get_recent_emotions(
    session_id: str,
    limit: int = 10,
    days: int = 30,
) -> list[dict]:
    """
    Retrieve recent emotions for a session, ordered newest-first.

    Each returned dict has:
        emotion, confidence, source_text, timestamp, decay_weight

    Decay weights:
        0–1 day   → 1.00
        1–7 days  → 0.75
        7–30 days → 0.50
        >30 days  → 0.10  (filtered out by default via `days`)
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        cursor.execute(
            """
            SELECT emotion, confidence, source_text, timestamp
            FROM conversation_emotions
            WHERE session_id = ? AND timestamp >= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, cutoff, limit)
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    now = datetime.utcnow()
    result = []
    for emotion, confidence, source_text, ts_str in rows:
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            ts = now
        age_days = (now - ts).total_seconds() / 86400.0
        if age_days <= 1:
            weight = 1.00
        elif age_days <= 7:
            weight = 0.75
        else:
            weight = 0.50
        result.append({
            "emotion":     emotion,
            "confidence":  confidence,
            "source_text": source_text,
            "timestamp":   ts_str,
            "decay_weight": weight,
        })
    return result


# ─────────────────────────────────────────────────────────────────────────────
# NEW: Personality helpers
# ─────────────────────────────────────────────────────────────────────────────

_PERSONALITY_DEFAULTS: dict[str, float] = {
    "humor_level":         20.0,
    "curiosity_level":     50.0,
    "friendliness_level":  60.0,
    "technical_depth":     70.0,
    "encouragement_level": 60.0,
}

_GLOBAL_PERSONALITY_ID = "global"


def get_personality() -> dict[str, float]:
    """
    Return the global personality profile.
    Inserts default values on first call.
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT humor_level, curiosity_level, friendliness_level,
                   technical_depth, encouragement_level
            FROM personality_profile
            WHERE session_id = ?
            """,
            (_GLOBAL_PERSONALITY_ID,)
        )
        row = cursor.fetchone()
        if row is None:
            # First run — insert defaults
            cursor.execute(
                """
                INSERT INTO personality_profile
                    (session_id, humor_level, curiosity_level,
                     friendliness_level, technical_depth, encouragement_level)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _GLOBAL_PERSONALITY_ID,
                    _PERSONALITY_DEFAULTS["humor_level"],
                    _PERSONALITY_DEFAULTS["curiosity_level"],
                    _PERSONALITY_DEFAULTS["friendliness_level"],
                    _PERSONALITY_DEFAULTS["technical_depth"],
                    _PERSONALITY_DEFAULTS["encouragement_level"],
                )
            )
            conn.commit()
            return dict(_PERSONALITY_DEFAULTS)
        return {
            "humor_level":         row[0],
            "curiosity_level":     row[1],
            "friendliness_level":  row[2],
            "technical_depth":     row[3],
            "encouragement_level": row[4],
        }
    finally:
        conn.close()


def update_personality(
    deltas: dict[str, float],
    trigger_session_id: str,
    trigger_text: str,
) -> dict[str, float]:
    """
    Apply trait deltas to the global personality profile.

    Rules:
    - Each delta is clamped to ±1.0 before applying.
    - Each resulting trait value is clamped to [0, 100].
    - Every applied delta is written to personality_events for audit.

    Returns the updated profile dict.
    """
    if not deltas:
        return get_personality()

    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()

        # Fetch current values
        cursor.execute(
            """
            SELECT humor_level, curiosity_level, friendliness_level,
                   technical_depth, encouragement_level
            FROM personality_profile WHERE session_id = ?
            """,
            (_GLOBAL_PERSONALITY_ID,)
        )
        row = cursor.fetchone()
        if row is None:
            profile = dict(_PERSONALITY_DEFAULTS)
        else:
            profile = {
                "humor_level":         row[0],
                "curiosity_level":     row[1],
                "friendliness_level":  row[2],
                "technical_depth":     row[3],
                "encouragement_level": row[4],
            }

        # Apply clamped deltas
        now_iso = datetime.utcnow().isoformat()
        for trait, raw_delta in deltas.items():
            if trait not in profile:
                continue
            clamped_delta = max(-1.0, min(1.0, raw_delta))
            new_val = max(0.0, min(100.0, profile[trait] + clamped_delta))
            profile[trait] = new_val
            # Audit log
            cursor.execute(
                """
                INSERT INTO personality_events
                    (timestamp, session_id, trigger_text, trait, delta, value_after)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now_iso, trigger_session_id, trigger_text[:200], trait, clamped_delta, new_val)
            )

        # Persist updated profile
        cursor.execute(
            """
            UPDATE personality_profile
            SET humor_level = ?,
                curiosity_level = ?,
                friendliness_level = ?,
                technical_depth = ?,
                encouragement_level = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (
                profile["humor_level"],
                profile["curiosity_level"],
                profile["friendliness_level"],
                profile["technical_depth"],
                profile["encouragement_level"],
                now_iso,
                _GLOBAL_PERSONALITY_ID,
            )
        )
        conn.commit()
        return profile
    finally:
        conn.close()


def get_personality_events(limit: int = 50) -> list[dict]:
    """
    Return the most recent personality audit events (newest-first).
    Useful for debugging personality drift.
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT timestamp, session_id, trigger_text, trait, delta, value_after
            FROM personality_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        )
        rows = cursor.fetchall()
        return [
            {
                "timestamp":    r[0],
                "session_id":   r[1],
                "trigger_text": r[2],
                "trait":        r[3],
                "delta":        r[4],
                "value_after":  r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# NEW: User profile helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_user_profile() -> list[dict]:
    """
    Return all stored user facts, ordered by confidence (high first) then
    recency.  Each entry is a dict with keys: fact, confidence.

    Always reads directly from SQLite — no in-process cache — so callers
    always see the latest state written by background learning tasks.
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT fact, confidence
            FROM user_profile
            ORDER BY
                CASE confidence
                    WHEN 'high'   THEN 3
                    WHEN 'medium' THEN 2
                    ELSE 1
                END DESC,
                updated_at DESC
        """)
        rows = cursor.fetchall()
        return [{"fact": row[0], "confidence": row[1]} for row in rows]
    finally:
        conn.close()


def upsert_user_facts(facts: list[dict], source: str) -> None:
    """
    Insert new user facts or update confidence + source on exact-text conflict.

    Each item in `facts` must have:
        fact       : str  — the fact sentence
        confidence : str  — "high" | "medium" | "low"

    After inserting, the table is pruned to 50 rows: lowest-confidence,
    oldest rows are removed first to keep the profile tight and relevant.
    """
    if not facts:
        return

    init_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        now_iso = datetime.utcnow().isoformat()

        for item in facts:
            fact       = item.get("fact", "").strip()
            confidence = item.get("confidence", "medium")
            if not fact:
                continue
            if confidence not in ("high", "medium", "low"):
                confidence = "medium"
            # Upsert: on exact text match, refresh confidence + source + timestamp
            cursor.execute("""
                INSERT INTO user_profile (fact, confidence, source, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(fact) DO UPDATE SET
                    confidence = excluded.confidence,
                    source     = excluded.source,
                    updated_at = excluded.updated_at
            """, (fact, confidence, source, now_iso))

        # Prune to 50 facts — remove lowest confidence + oldest first
        cursor.execute("SELECT COUNT(*) FROM user_profile")
        total = cursor.fetchone()[0]
        if total > 50:
            to_delete = total - 50
            cursor.execute("""
                DELETE FROM user_profile
                WHERE id IN (
                    SELECT id FROM user_profile
                    ORDER BY
                        CASE confidence
                            WHEN 'high'   THEN 3
                            WHEN 'medium' THEN 2
                            ELSE 1
                        END ASC,
                        updated_at ASC
                    LIMIT ?
                )
            """, (to_delete,))

        conn.commit()
    finally:
        conn.close()
