"""
config.py — Offline voice assistant settings.

Override any value via environment variables or a .env file.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Server ───────────────────────────────────────────────────────────────────

HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "5000"))

# ── Audio format (must match ESP32 I2S config) ───────────────────────────────

INPUT_SAMPLE_RATE: int = int(os.getenv("INPUT_SAMPLE_RATE", "16000"))
INPUT_SAMPLE_WIDTH: int = 2  # 16-bit mono
INPUT_CHANNELS: int = 1

OUTPUT_SAMPLE_RATE: int = int(os.getenv("OUTPUT_SAMPLE_RATE", "16000"))
OUTPUT_SAMPLE_WIDTH: int = 2
OUTPUT_CHANNELS: int = 1
OUTPUT_GAIN_DB: float = float(os.getenv("OUTPUT_GAIN_DB", "0"))

# ── Streaming / VAD ──────────────────────────────────────────────────────────

STREAM_MIN_AUDIO_MS: int = int(os.getenv("STREAM_MIN_AUDIO_MS", "150"))
VAD_SILENCE_TIMEOUT_MS: int = int(os.getenv("VAD_SILENCE_TIMEOUT_MS", "1500"))  # 1.5s silence = clear sentence end
STREAM_MAX_RECORD_MS: int = int(os.getenv("STREAM_MAX_RECORD_MS", "10000"))     # 10 second max
VAD_ENERGY_THRESHOLD: float = float(os.getenv("VAD_ENERGY_THRESHOLD", "80"))   # lowered: INMP441 is low-amplitude
VAD_NOISE_MULTIPLIER: float = float(os.getenv("VAD_NOISE_MULTIPLIER", "1.8"))   # less aggressive noise gate
VAD_NOISE_MARGIN: float = float(os.getenv("VAD_NOISE_MARGIN", "40"))            # lower margin for quiet mics

# ── Speech-to-Text (faster-whisper, local) ───────────────────────────────────

# small    = multilingual, good Malayalam + English accuracy  ← recommended for bilingual
# base     = multilingual, ~3× faster but poor Malayalam recognition (too few parameters)
# small.en = English-only, fastest but cannot hear Malayalam at all
WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL_SIZE", "tiny")   # was small.en — ~4x faster on CPU
WHISPER_DEVICE: str = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE: str = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_CPU_THREADS: int = int(os.getenv("WHISPER_CPU_THREADS", "4"))

STT_COMMAND_SILENCE_RMS: float = float(os.getenv("STT_COMMAND_SILENCE_RMS", "0.004"))  # lowered: INMP441 RMS ~0.006-0.012
STT_MIN_SPEECH_RATIO: float = float(os.getenv("STT_MIN_SPEECH_RATIO", "0.03"))         # lowered: quiet mic gives low ratio
STT_HALLUCINATION_MAX_SPEECH_RATIO: float = float(
    os.getenv("STT_HALLUCINATION_MAX_SPEECH_RATIO", "0.45")
)

# ── LLM (Ollama, local) ──────────────────────────────────────────────────────

OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
# qwen2.5:3b fits 8 GB RAM; use qwen2.5:7b only if you have headroom
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
MAX_REPLY_WORDS: int = int(os.getenv("MAX_REPLY_WORDS", "28"))
MAX_REPLY_SENTENCES: int = int(os.getenv("MAX_REPLY_SENTENCES", "2"))
LLM_STREAM_TIMEOUT: float = float(os.getenv("LLM_STREAM_TIMEOUT", "15"))  # reduced from 30 — fail fast if Ollama stalls

# ── TTS (Piper, local) ───────────────────────────────────────────────────────

# Download from: https://github.com/rhasspy/piper/blob/master/VOICES.md
# English voice (default)
PIPER_MODEL: Path = Path(
    os.getenv("PIPER_MODEL", "models/piper/en_US-hfc_female-medium.onnx")
)
# Malayalam voice — used automatically when Malayalam script is detected in the reply
PIPER_MODEL_ML: Path = Path(
    os.getenv("PIPER_MODEL_ML", "models/piper/ml_IN-meera-medium.onnx")
)
# Fraction of Malayalam Unicode characters required to switch to ML voice (0.0–1.0).
# Default 0.15 means: if >15% of the text is Malayalam script → use Malayalam TTS.
MALAYALAM_SCRIPT_THRESHOLD: float = float(
    os.getenv("MALAYALAM_SCRIPT_THRESHOLD", "0.15")
)
PIPER_LENGTH_SCALE: float = float(os.getenv("PIPER_LENGTH_SCALE", "1.05"))

TTS_CHUNK_MS: int = int(os.getenv("TTS_CHUNK_MS", "40"))        # was 60 — finer chunks start playback sooner
TTS_FLUSH_WORDS: int = int(os.getenv("TTS_FLUSH_WORDS", "5"))   # was 12 — flush after 5 words for lower TTFA

# ── Conversation memory (in-RAM sliding window) ──────────────────────────────

CONVERSATION_HISTORY_TURNS: int = int(os.getenv("CONVERSATION_HISTORY_TURNS", "6"))
CONTEXT_TIMEOUT_S: int = int(os.getenv("CONTEXT_TIMEOUT_S", "120"))

# ── Debug ────────────────────────────────────────────────────────────────────

LATENCY_LOG: bool = os.getenv("LATENCY_LOG", "true").lower() == "true"

# ── Cute Voice ────────────────────────────────────────────────────────────────

CUTE_VOICE: bool = os.getenv("CUTE_VOICE", "false").lower() == "true"
CUTE_PITCH_FACTOR: float = float(os.getenv("CUTE_PITCH_FACTOR", "1.08"))
