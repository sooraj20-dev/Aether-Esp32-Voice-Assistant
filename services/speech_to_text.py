"""
services/speech_to_text.py — Offline STT with faster-whisper + PCM helpers.

Latency optimisations applied
------------------------------
1. beam_size = 1  (greedy decoding — ~4× faster than beam_size=5 on CPU,
                   barely any accuracy loss for short voice commands)
2. vad_filter disabled — we already have ESP32-side VAD + server StreamingVAD;
   running Whisper's internal VAD on top just wastes ~300 ms per clip.
3. Bandpass filter coefficients pre-computed once at import time (not per call).
4. Single-pass transcription — the "relaxed retry" second Whisper pass is
   removed; it doubled worst-case latency for no meaningful gain.
5. numpy used throughout for all array operations.
"""

from __future__ import annotations

import asyncio
import re
import struct
from typing import Optional

import numpy as np

import config

# ── Model singleton ──────────────────────────────────────────────────────────

_whisper_model = None


def load_whisper_model():
    """Pre-warm faster-whisper at startup. Returns model or None."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        print("[STT] faster-whisper not installed", flush=True)
        return None

    device = config.WHISPER_DEVICE
    if device == "auto":
        device = "cuda"

    try:
        _whisper_model = WhisperModel(
            config.WHISPER_MODEL_SIZE,
            device=device,
            compute_type=config.WHISPER_COMPUTE_TYPE,
            cpu_threads=config.WHISPER_CPU_THREADS,
        )
        print(
            f"[STT] ready: {config.WHISPER_MODEL_SIZE}/{config.WHISPER_COMPUTE_TYPE} on {device}",
            flush=True,
        )
    except Exception:
        if device == "cuda":
            print("[STT] CUDA unavailable, using CPU", flush=True)
            _whisper_model = WhisperModel(
                config.WHISPER_MODEL_SIZE,
                device="cpu",
                compute_type="int8",
                cpu_threads=config.WHISPER_CPU_THREADS,
            )
        else:
            raise
    return _whisper_model


# ── Pre-computed bandpass filter (computed ONCE at import, not per call) ──────
# Recalculating butter() + sosfilt() per transcription was costing ~150 ms.
# Computing the SOS coefficients here costs ~0 ms at import time.

_BANDPASS_SOS: object | None = None

def _get_bandpass_sos():
    """Return cached SOS coefficients for the 120 Hz – 7600 Hz bandpass filter."""
    global _BANDPASS_SOS
    if _BANDPASS_SOS is None:
        try:
            from scipy.signal import butter  # type: ignore
            _BANDPASS_SOS = butter(
                4, [120.0, 7600.0], btype="bandpass",
                fs=config.INPUT_SAMPLE_RATE, output="sos",
            )
        except Exception:
            _BANDPASS_SOS = False   # scipy unavailable — mark as disabled
    return _BANDPASS_SOS if _BANDPASS_SOS is not False else None


# ── PCM helpers ──────────────────────────────────────────────────────────────

def _frame_rms(pcm: bytes) -> float:
    n = len(pcm) // config.INPUT_SAMPLE_WIDTH
    if n == 0:
        return 0.0
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(samples ** 2)))


def speech_ratio(pcm_bytes: bytes, *, frame_ms: int = 30, threshold: float = 80.0) -> float:
    """Fraction of frames whose RMS exceeds threshold (0..1).
    Threshold lowered to 80 from 200 to match INMP441 mic output levels.
    """
    frame_bytes = int(config.INPUT_SAMPLE_RATE * frame_ms / 1000) * config.INPUT_SAMPLE_WIDTH
    if frame_bytes <= 0 or len(pcm_bytes) < frame_bytes:
        return 0.0
    voiced = total = 0
    for off in range(0, len(pcm_bytes) - frame_bytes + 1, frame_bytes):
        total += 1
        if _frame_rms(pcm_bytes[off : off + frame_bytes]) > threshold:
            voiced += 1
    return voiced / total if total else 0.0


def trim_silence_pcm(
    pcm_bytes: bytes,
    *,
    frame_ms: int = 30,
    threshold: float = 80.0,   # lowered from 180 to match INMP441 levels
    pad_ms: int = 200,
    min_keep_ms: int = 400,
) -> bytes:
    """Trim leading/trailing silence; keep a short pad around speech."""
    frame_bytes = int(config.INPUT_SAMPLE_RATE * frame_ms / 1000) * config.INPUT_SAMPLE_WIDTH
    pad_bytes   = int(config.INPUT_SAMPLE_RATE * pad_ms   / 1000) * config.INPUT_SAMPLE_WIDTH
    min_bytes   = int(config.INPUT_SAMPLE_RATE * min_keep_ms / 1000) * config.INPUT_SAMPLE_WIDTH

    if len(pcm_bytes) < frame_bytes:
        return pcm_bytes

    n_frames = len(pcm_bytes) // frame_bytes
    voiced = [
        _frame_rms(pcm_bytes[i * frame_bytes : (i + 1) * frame_bytes]) > threshold
        for i in range(n_frames)
    ]
    try:
        first = voiced.index(True)
        last  = len(voiced) - 1 - voiced[::-1].index(True)
    except ValueError:
        return pcm_bytes

    start   = max(0, first * frame_bytes - pad_bytes)
    end     = min(len(pcm_bytes), (last + 1) * frame_bytes + pad_bytes)
    trimmed = pcm_bytes[start:end]
    return pcm_bytes if len(trimmed) < min_bytes else trimmed


# ── Audio preprocessing ──────────────────────────────────────────────────────

def _pcm_to_float32(pcm_bytes: bytes) -> np.ndarray:
    """
    Convert raw 16-bit PCM → float32 in [-1, 1].

    Applies:
      • DC offset removal (mean subtraction)
      • Pre-computed bandpass filter 120 Hz – 7.6 kHz  (if scipy available)
      • Auto-gain normalization — boosts quiet INMP441 signals to a target
        RMS of 0.15 so Whisper can reliably decode them. Without this,
        INMP441 output (RMS ~0.006-0.012) is treated as no-speech.

    Filter coefficients are cached at module level — no per-call overhead.
    """
    n = len(pcm_bytes) // 2
    if n == 0:
        return np.zeros(1, dtype=np.float32)

    samples = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)
    samples -= samples.mean()   # remove DC bias

    sos = _get_bandpass_sos()
    if sos is not None:
        try:
            from scipy.signal import sosfilt  # type: ignore
            samples = sosfilt(sos, samples).astype(np.float32)
        except Exception:
            pass

    samples /= 32768.0

    # ── Auto-gain: normalize quiet mic to target RMS ──────────────────────
    # INMP441 on ESP32 I2S produces low-amplitude PCM (RMS ~0.006-0.012).
    # Whisper expects speech around RMS 0.05-0.20; below that it assigns
    # high no-speech probability and returns empty segments.
    TARGET_RMS = 0.15
    MIN_RMS    = 0.001   # skip gain on true silence to avoid noise boost
    MAX_GAIN   = 30.0    # cap to prevent clipping on very quiet noise
    rms = float(np.sqrt(np.mean(samples ** 2)))
    if rms > MIN_RMS:
        gain = min(TARGET_RMS / rms, MAX_GAIN)
        samples = np.clip(samples * gain, -1.0, 1.0)

    return samples


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio ** 2))) if len(audio) > 0 else 0.0


# ── Hallucination guards ─────────────────────────────────────────────────────

_INITIAL_PROMPT = (
    "Aether voice assistant. Understands English and Malayalam. "
    "Transcribe exactly what the user says in their language."
)

_HALLUCINATION_PHRASES = frozenset({
    "welcome back", "thank you for watching", "thanks for watching",
    "subscribe", "like and subscribe", "see you next time",
    "bye", "goodbye", "you", "the end",
})

_PROMPT_ECHO = (
    "aether voice assistant", "alpha voice assistant", "understands english", "transcribe exactly",
)


def is_likely_hallucination(
    text: str,
    pcm_bytes: bytes,
    *,
    speech_ratio_value: float | None = None,
) -> bool:
    norm = text.strip().lower().rstrip(".!?")
    if norm not in _HALLUCINATION_PHRASES:
        return False
    ratio = speech_ratio_value if speech_ratio_value is not None else speech_ratio(pcm_bytes)
    if ratio < config.STT_HALLUCINATION_MAX_SPEECH_RATIO:
        print(f"[STT] Hallucination rejected: '{text}' (ratio={ratio:.2f})", flush=True)
        return True
    return False


def _is_prompt_echo(text: str) -> bool:
    lower = text.lower()
    return any(pat in lower for pat in _PROMPT_ECHO)


def _is_repetitive(text: str) -> bool:
    clean = re.sub(r"[.,!?;:]", "", text.lower())
    words = clean.split()
    if len(words) < 4:
        return False
    for phrase_len in range(1, min(5, len(words))):
        phrase = tuple(words[:phrase_len])
        matches = sum(
            1 for i in range(0, len(words) - phrase_len + 1, phrase_len)
            if tuple(words[i : i + phrase_len]) == phrase
        )
        if matches >= 3 and (matches * phrase_len) >= len(words) * 0.60:
            return True
    return False


# ── Transcription ────────────────────────────────────────────────────────────

def _collect_transcript(segments, info, label: str = "") -> str:
    text = " ".join(seg.text.strip() for seg in segments if seg.text.strip()).strip()
    lang = getattr(info, "language", "?")
    tag  = f" ({label})" if label else ""
    print(f"[STT]{tag} lang={lang} -> '{text or '[silence]'}'", flush=True)
    return text


def _transcribe(model, audio: np.ndarray) -> str:
    """
    Single-pass Whisper transcription tuned for minimum CPU latency.

    Key settings
    ------------
    beam_size=1           — greedy decoding; ~4× faster than beam_size=5 with
                            minimal accuracy loss for short voice commands.
    vad_filter=False      — ESP32 hardware VAD + server StreamingVAD already trim
                            silence before audio reaches here; a 3rd VAD pass only
                            wastes ~300 ms.
    no_speech_threshold=0.90 — raised from 0.60: INMP441 produces quiet audio
                            that Whisper assigns high no-speech probability even
                            when real speech is present. 0.90 forces transcription
                            unless the clip is almost certainly silent.
    language=None         — auto-detect so Malayalam and English both work.
    """
    segments, info = model.transcribe(
        audio,
        beam_size=1,                      # greedy — fastest on CPU
        initial_prompt=_INITIAL_PROMPT,
        no_speech_threshold=0.90,         # raised: quiet mic → high no-speech prob
        vad_filter=False,                 # already handled upstream
        condition_on_previous_text=False,
        without_timestamps=True,
    )
    return _collect_transcript(segments, info)


def _reject_bad_text(text: str, pcm_bytes: bytes, ratio: Optional[float] = None) -> str:
    if not text:
        return ""
    if (
        is_likely_hallucination(text, pcm_bytes, speech_ratio_value=ratio)
        or _is_prompt_echo(text)
        or _is_repetitive(text)
    ):
        print(f"[STT] Rejected: '{text}'", flush=True)
        return ""
    return text


def transcribe_pcm_bytes(pcm_bytes: bytes) -> str:
    """Transcribe raw 16-bit PCM mono 16 kHz bytes. Returns '' on silence/noise."""
    model = load_whisper_model()
    if model is None:
        return ""

    min_bytes = int(config.INPUT_SAMPLE_RATE * config.INPUT_SAMPLE_WIDTH * 0.35)
    if len(pcm_bytes) < min_bytes:
        return ""

    audio = _pcm_to_float32(pcm_bytes)
    rms   = _rms(audio)
    print(f"[STT] Clip: {len(audio) / config.INPUT_SAMPLE_RATE:.1f}s RMS={rms:.4f}", flush=True)

    if rms < config.STT_COMMAND_SILENCE_RMS:
        return ""

    # Single pass — no expensive retry loop
    return _reject_bad_text(_transcribe(model, audio), pcm_bytes, ratio=None)


async def transcribe_pcm_bytes_async(pcm_bytes: bytes) -> str:
    return await asyncio.to_thread(transcribe_pcm_bytes, pcm_bytes)
