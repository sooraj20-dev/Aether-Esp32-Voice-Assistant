"""
services/tts_service.py — Offline TTS with Piper.

Supports automatic language detection and per-language voice selection:
  • English  → en_US-hfc_female-medium.onnx   (config.PIPER_MODEL)
  • Malayalam → ml_IN-meera-medium.onnx        (config.PIPER_MODEL_ML)

Language detection uses Unicode block analysis (no external libraries needed):
  Malayalam script occupies U+0D00–U+0D7F.
  If that fraction of characters exceeds config.MALAYALAM_SCRIPT_THRESHOLD
  (default 0.15 = 15 %), the Malayalam voice is selected.

Both voices are cached in _voice_cache after first load so switching
languages mid-conversation has zero reload overhead.
"""

from __future__ import annotations

import asyncio
import re
import struct
from pathlib import Path
from typing import AsyncIterator

import config

# ── Voice model cache ──────────────────────────────────────────────────────────
# Keyed by the absolute model path string so each ONNX file is loaded once.
_voice_cache: dict[str, object] = {}


def _log(msg: str) -> None:
    print(f"[TTS] {msg}", flush=True)


# ── Language detection ─────────────────────────────────────────────────────────

# Malayalam Unicode block: U+0D00 – U+0D7F
_MALAYALAM_RANGE = range(0x0D00, 0x0D80)


def detect_language(text: str) -> str:
    """
    Detect whether *text* is predominantly Malayalam or English.

    Algorithm
    ---------
    1. Count total non-whitespace characters.
    2. Count characters whose code-point falls in the Malayalam Unicode block
       (U+0D00 – U+0D7F).
    3. If the Malayalam fraction > config.MALAYALAM_SCRIPT_THRESHOLD → "malayalam".
    4. Otherwise → "english".

    Returns
    -------
    "malayalam" | "english"
    """
    if not text:
        return "english"

    # Only consider non-whitespace characters for the ratio
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return "english"

    malayalam_count = sum(1 for c in chars if ord(c) in _MALAYALAM_RANGE)
    ratio = malayalam_count / len(chars)

    language = "malayalam" if ratio >= config.MALAYALAM_SCRIPT_THRESHOLD else "english"

    _log(
        f"Language detected: {'Malayalam' if language == 'malayalam' else 'English'} "
        f"(Malayalam chars: {malayalam_count}/{len(chars)} = {ratio:.1%})"
    )
    return language


def get_voice_model(text: str) -> Path:
    """
    Return the Piper ONNX model path appropriate for *text*.

    Decision flow
    -------------
    Assistant Response
           ↓
    Detect Language (Unicode block analysis)
           ↓
    English? ── Yes → config.PIPER_MODEL   (en_US-hfc_female-medium.onnx)
           │
           No
           ↓
    Malayalam → config.PIPER_MODEL_ML      (ml_IN-meera-medium.onnx)
    """
    language = detect_language(text)

    if language == "malayalam":
        model_path = config.PIPER_MODEL_ML
        _log(f"Voice selected: {model_path.name}")
    else:
        model_path = config.PIPER_MODEL
        _log(f"Voice selected: {model_path.name}")

    return model_path


# ── Voice loader (cached per model path) ──────────────────────────────────────

def _load_voice(model_path: Path):
    """
    Load a PiperVoice from *model_path*, using the in-process cache.

    The matching JSON config file is expected alongside the .onnx file
    (Piper's standard layout):
        en_US-hfc_female-medium.onnx
        en_US-hfc_female-medium.onnx.json   ← loaded automatically by Piper
        ml_IN-meera-medium.onnx
        ml_IN-meera-medium.onnx.json        ← loaded automatically by Piper
    """
    key = str(model_path.resolve())

    if key in _voice_cache:
        return _voice_cache[key]

    if not model_path.exists():
        raise FileNotFoundError(f"Piper model not found: {model_path}")

    # Verify the companion JSON config exists (Piper requires it)
    json_path = Path(str(model_path) + ".json")
    if not json_path.exists():
        raise FileNotFoundError(
            f"Piper model config not found: {json_path}\n"
            f"Download it alongside the .onnx file from "
            f"https://github.com/rhasspy/piper/blob/master/VOICES.md"
        )

    from piper.voice import PiperVoice

    voice = PiperVoice.load(str(model_path))
    _voice_cache[key] = voice

    _log(
        f"Loaded voice: {model_path.name} "
        f"@ {voice.config.sample_rate} Hz"
    )
    return voice


# ── Legacy single-model loader (kept for backward compatibility) ───────────────

def load_piper_voice():
    """Load (and cache) the default English voice. Used by startup checks."""
    return _load_voice(config.PIPER_MODEL)


# ── Audio helpers ──────────────────────────────────────────────────────────────

def _apply_gain_db(pcm: bytes, gain_db: float) -> bytes:
    if gain_db == 0.0:
        return pcm
    factor = 10.0 ** (gain_db / 20.0)
    n = len(pcm) // 2
    samples = struct.unpack_from(f"<{n}h", pcm)
    clipped = [max(-32768, min(32767, int(s * factor))) for s in samples]
    return struct.pack(f"<{n}h", *clipped)


def _apply_fade(pcm_bytes: bytes, fade_samples: int = 400) -> bytes:
    """Apply smooth fade-in and fade-out using numpy (no Python loops)."""
    import numpy as np
    n = len(pcm_bytes) // 2
    if n <= 0:
        return pcm_bytes

    samples = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)

    # Fade in
    fi = min(fade_samples, n)
    samples[:fi] *= np.linspace(0.0, 1.0, fi, dtype=np.float32)

    # Fade out
    fo = min(fade_samples, n)
    samples[n - fo:] *= np.linspace(1.0, 0.0, fo, dtype=np.float32)

    return np.clip(samples, -32768, 32767).astype("<i2").tobytes()


# ── Resampler cache ───────────────────────────────────────────────────────────
# resample_poly designs a polyphase FIR filter on every call.
# We cache the filter coefficients per (src, dst) pair so the expensive
# filter design only happens once — not on every TTS synthesis.
import numpy as _np
from math import gcd as _gcd

_RESAMPLE_CACHE: dict[tuple[int, int], tuple[int, int, _np.ndarray]] = {}

def _get_resample_params(src_rate: int, dst_rate: int):
    """Return (up, down, window) for resample_poly, cached per rate pair."""
    key = (src_rate, dst_rate)
    if key not in _RESAMPLE_CACHE:
        g   = _gcd(src_rate, dst_rate)
        up  = dst_rate // g
        down = src_rate // g
        # Kaiser window (beta=8) gives a very sharp anti-aliasing FIR.
        # This is the main fix for the ringing / noise heard on Malayalam output.
        window = ("kaiser", 8.0)
        _RESAMPLE_CACHE[key] = (up, down, window)
    return _RESAMPLE_CACHE[key]


def _resample_pcm(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit mono PCM with proper anti-aliasing (Kaiser window FIR)."""
    if src_rate == dst_rate or not pcm:
        return pcm
    import numpy as np
    from scipy.signal import resample_poly  # type: ignore

    up, down, window = _get_resample_params(src_rate, dst_rate)
    samples   = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    resampled = resample_poly(samples, up, down, window=window)
    return np.clip(resampled, -32768, 32767).astype("<i2").tobytes()


# ── Core synthesis ─────────────────────────────────────────────────────────────

def _synthesize_pcm(text: str) -> bytes:
    """
    Synthesize *text* to raw 16-bit mono PCM bytes.

    The correct Piper voice is chosen automatically based on the language
    detected in *text* (English or Malayalam).

    For Malayalam, synthesis noise_w is lowered (0.4 vs default 0.8) to
    reduce the prosody noise that is more audible after downsampling.
    """
    import io
    import wave

    # ── Select and load the correct voice for this text ──────────────────────
    model_path = get_voice_model(text)
    voice      = _load_voice(model_path)
    is_malayalam = (model_path == config.PIPER_MODEL_ML)

    # ── Synthesize to WAV in memory ───────────────────────────────────────────
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        if is_malayalam:
            # Lower noise_w reduces the stochastic prosody variation that
            # causes crackling / noise when downsampled from 22050 → 16000 Hz.
            try:
                from piper.voice import SynthesisConfig  # type: ignore
                cfg = SynthesisConfig(noise_w=0.4)
                voice.synthesize_wav(text, wav_file, synthesis_config=cfg)
            except (ImportError, TypeError):
                # Older piper versions don't expose SynthesisConfig — fall back
                voice.synthesize_wav(text, wav_file)
        else:
            voice.synthesize_wav(text, wav_file)

    buffer.seek(0)
    with wave.open(buffer, "rb") as wav_file:
        pcm = wav_file.readframes(wav_file.getnframes())

    # ── Pitch shift (cute voice / English only) ───────────────────────────────
    src_rate = voice.config.sample_rate
    if config.CUTE_VOICE and not is_malayalam:
        src_rate = int(src_rate * config.CUTE_PITCH_FACTOR)

    # ── Resample to ESP32 output rate (Kaiser-windowed FIR — low noise) ───────
    pcm = _resample_pcm(pcm, src_rate, config.OUTPUT_SAMPLE_RATE)

    # ── Volume gain ───────────────────────────────────────────────────────────
    pcm = _apply_gain_db(pcm, config.OUTPUT_GAIN_DB)

    # ── Fade in/out to prevent clicks ────────────────────────────────────────
    return _apply_fade(pcm)


# ── Sentence utilities ─────────────────────────────────────────────────────────

def is_sentence_boundary(text: str) -> bool:
    t = text.rstrip()
    if t.endswith((".", "!", "?", "…")):
        return True
    return len(t) >= 90 and t.endswith((",", ";", ":"))


def split_sentences(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", text.strip())
        if part.strip()
    ]


# ── Public streaming interface ─────────────────────────────────────────────────

async def stream_tts_pcm(text: str, chunk_ms: int = 40) -> AsyncIterator[bytes]:
    """
    Yield raw 16-bit PCM chunks for the ESP32 speaker.

    Automatically selects English or Malayalam Piper voice based on *text*.
    """
    text = text.strip()
    if not text:
        return

    try:
        pcm = await asyncio.to_thread(_synthesize_pcm, text)
    except Exception as exc:
        _log(f"Synthesis failed: {exc}")
        return

    if not pcm:
        _log("No audio produced")
        return

    frame_bytes = max(
        config.OUTPUT_SAMPLE_RATE
        * config.OUTPUT_SAMPLE_WIDTH
        * config.OUTPUT_CHANNELS
        * chunk_ms
        // 1000,
        320,
    )

    _log(f"Streaming {len(pcm)} bytes in {(len(pcm) + frame_bytes - 1) // frame_bytes} chunks")
    for offset in range(0, len(pcm), frame_bytes):
        frame = pcm[offset : offset + frame_bytes]
        if len(frame) < frame_bytes:
            frame = frame + b"\x00" * (frame_bytes - len(frame))
        yield frame
