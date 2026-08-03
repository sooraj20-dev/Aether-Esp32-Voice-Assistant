"""
services/pipeline.py — STT → LLM → TTS orchestration for one voice turn.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import config
from services.ai_brain import ConversationContext, stream_ai
from services.speech_to_text import (
    is_likely_hallucination,
    speech_ratio,
    transcribe_pcm_bytes_async,
    trim_silence_pcm,
)
from services.tts_service import is_sentence_boundary, stream_tts_pcm

# ESP32 face states — keep these strings unchanged
STATE_IDLE = "Idle"
STATE_LISTENING = "Listening"
STATE_TRANSCRIBING = "Transcribing"
STATE_THINKING = "Thinking"
STATE_SPEAKING = "Speaking"

_WAKE_WORDS = frozenset({
    "aether", "ether", "hey aether", "hello aether", "ok aether", "okay aether",
    "alpha", "alfa", "hello", "hey", "alexa", "hey alpha", "hello alpha",
    "ok alpha", "okay alpha",
})


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [Pipeline] {msg}", flush=True)


class LatencyTracker:
    """Lightweight per-turn timing (merged from latency.py)."""

    def __init__(self) -> None:
        self.checkpoints: list[tuple[str, float]] = []
        self.start_time = time.perf_counter()

    def mark(self, label: str) -> None:
        self.checkpoints.append((label, time.perf_counter()))

    def reset(self) -> None:
        self.checkpoints.clear()
        self.start_time = time.perf_counter()

    def report(self) -> None:
        if not self.checkpoints or not config.LATENCY_LOG:
            return
        lines = ["Latency report:"]
        prev = self.start_time
        for label, t in self.checkpoints:
            lines.append(f"  {label}: +{(t - prev) * 1000:.0f} ms")
            prev = t
        _log("\n".join(lines))


async def send_state(ws, state: str) -> None:
    try:
        await ws.send_text(json.dumps({"type": "state", "state": state}))
    except Exception:
        pass


async def pre_synthesize(segment: str, is_fallback: bool) -> tuple[list[bytes], bool, str]:
    """Pre-synthesizes text to PCM chunks in a background thread."""
    chunks = []
    try:
        async for chunk in stream_tts_pcm(segment, chunk_ms=config.TTS_CHUNK_MS):
            chunks.append(chunk)
    except Exception as e:
        _log(f"Pre-synthesis failed for '{segment[:20]}...': {e}")
    return chunks, is_fallback, segment


async def tts_worker(
    ws,
    tts_queue: asyncio.Queue,
    interrupt_event: asyncio.Event,
    latency: LatencyTracker,
) -> int:
    """Background task to pull pre-synthesized PCM tasks, await them, and stream PCM to ESP32."""
    chunk_index = 0
    first_audio = False
    last_sent_bytes = 0

    try:
        while True:
            task = await tts_queue.get()
            if task is None:
                tts_queue.task_done()
                break

            # Await the background synthesis task
            try:
                chunks, is_fallback, segment = await task
            except Exception as e:
                _log(f"Synthesis task error: {e}")
                tts_queue.task_done()
                continue

            if interrupt_event.is_set():
                tts_queue.task_done()
                continue

            await send_state(ws, STATE_SPEAKING)

            if is_fallback:
                last_sent_bytes = await _speak_text(ws, segment, interrupt_event)
            else:
                if chunks:
                    last_sent_bytes = await _send_pcm_chunks(
                        ws, chunks, chunk_index, interrupt_event, latency, first_audio
                    )
                    first_audio = True
                    chunk_index += 1

            tts_queue.task_done()

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        _log(f"TTS Worker error: {exc}")

    return last_sent_bytes


async def run_turn(
    ws,
    latency: LatencyTracker,
    context: ConversationContext,
    interrupt_event: asyncio.Event,
    pcm: bytes,
    started_at: float,
) -> None:
    """Execute one complete STT → LLM → TTS turn."""
    if not pcm:
        await send_state(ws, STATE_IDLE)
        return

    audio_ms = len(pcm) / (config.INPUT_SAMPLE_WIDTH * config.INPUT_CHANNELS) / config.INPUT_SAMPLE_RATE * 1000
    _log(f"Processing {audio_ms:.0f} ms audio")

    pcm = trim_silence_pcm(pcm)
    ratio = speech_ratio(pcm)
    trimmed_ms = len(pcm) / (config.INPUT_SAMPLE_WIDTH * config.INPUT_CHANNELS) / config.INPUT_SAMPLE_RATE * 1000
    _log(f"After trim: {trimmed_ms:.0f} ms, speech_ratio={ratio:.2f}")

    if ratio < config.STT_MIN_SPEECH_RATIO or trimmed_ms < config.STREAM_MIN_AUDIO_MS:
        await _send_done(ws, "", started_at, latency)
        await send_state(ws, STATE_IDLE)
        return

    # STT
    await send_state(ws, STATE_TRANSCRIBING)
    latency.mark("STT_START")
    try:
        await ws.send_text(json.dumps({"type": "status", "stage": "transcribing"}))
    except Exception:
        pass

    transcript = (await transcribe_pcm_bytes_async(pcm)).strip()
    if transcript and is_likely_hallucination(transcript, pcm, speech_ratio_value=ratio):
        transcript = ""

    norm = transcript.lower().strip().rstrip(".!?,")
    if norm in _WAKE_WORDS:
        latency.mark("TRANSCRIPT")
        try:
            await ws.send_text(json.dumps({"type": "transcript", "text": transcript, "latency_ms": 0}))
        except Exception:
            pass
        await send_state(ws, STATE_SPEAKING)
        last_bytes = await _speak_text(ws, "Yes? How can I help you?", interrupt_event)
        if last_bytes:
            await _drain(last_bytes)
        await _send_done(ws, "", started_at, latency)
        await send_state(ws, STATE_IDLE)
        return

    latency.mark("TRANSCRIPT")
    stt_ms = int((time.perf_counter() - started_at) * 1000)
    try:
        await ws.send_text(json.dumps({"type": "transcript", "text": transcript, "latency_ms": stt_ms}))
    except Exception:
        await send_state(ws, STATE_IDLE)
        return
    _log(f"Transcript ({stt_ms} ms): {transcript or '[silence]'}")

    if not transcript:
        await _send_done(ws, "", started_at, latency)
        await send_state(ws, STATE_IDLE)
        return

    # LLM + TTS
    await send_state(ws, STATE_THINKING)
    latency.mark("LLM_START")
    try:
        await ws.send_text(json.dumps({"type": "status", "stage": "thinking"}))
    except Exception:
        pass

    full_reply = ""
    pending = ""
    first_token = False
    fallback_spoken = False
    last_sent_bytes = 0

    def _should_flush(text: str) -> bool:
        stripped = text.strip()
        words = stripped.split()
        if stripped.endswith((".", "?", "!")):
            return True
        if len(words) >= config.TTS_FLUSH_WORDS and text.endswith("\n"):
            return True
        if len(words) < config.TTS_FLUSH_WORDS:
            return is_sentence_boundary(text)
        return False

    tts_queue = asyncio.Queue()
    worker_task = asyncio.create_task(tts_worker(ws, tts_queue, interrupt_event, latency))

    try:
        async for delta in stream_ai(transcript, context=context):
            if interrupt_event.is_set():
                _log("Barge-in during LLM")
                break

            if not first_token:
                latency.mark("FIRST_TOKEN")
                first_token = True

            full_reply += delta
            pending += delta
            await ws.send_text(json.dumps({"type": "ai_delta", "text": delta}))

            if _should_flush(pending):
                segment = pending.strip()
                pending = ""
                task = asyncio.create_task(pre_synthesize(segment, False))
                await tts_queue.put(task)

        if not interrupt_event.is_set() and pending.strip():
            task = asyncio.create_task(pre_synthesize(pending.strip(), False))
            await tts_queue.put(task)

    except asyncio.CancelledError:
        worker_task.cancel()
        raise
    except Exception as exc:
        _log(f"LLM error: {exc}")
        if not interrupt_event.is_set():
            fallback_spoken = True
            task = asyncio.create_task(pre_synthesize("Sorry, the AI service is not available right now.", True))
            await tts_queue.put(task)
        full_reply = ""

    # Graceful completion flow
    await tts_queue.put(None)
    try:
        last_sent_bytes = await worker_task
    except Exception as e:
        _log(f"Worker task error: {e}")

    if not interrupt_event.is_set() and not full_reply.strip() and not fallback_spoken:
        await send_state(ws, STATE_SPEAKING)
        last_sent_bytes = await _speak_text(
            ws, "Sorry, I could not get a reply. Please try again.", interrupt_event
        )

    if not interrupt_event.is_set() and last_sent_bytes > 0:
        await _drain(last_sent_bytes)

    if not interrupt_event.is_set():
        await _send_done(ws, full_reply.strip(), started_at, latency)

    await send_state(ws, STATE_IDLE)

    if not interrupt_event.is_set():
        if context is not None:
            last_em = getattr(context, "_last_emotion", "neutral")
            last_conf = getattr(context, "_last_confidence", "low")
            if last_em != "neutral":
                try:
                    await ws.send_text(json.dumps({
                        "type": "emotion",
                        "emotion": last_em,
                        "confidence": last_conf
                    }))
                    _log(f"Sent emotion to ESP32: {last_em} ({last_conf})")
                except Exception as e:
                    _log(f"Failed to send emotion frame: {e}")


async def _speak_segment(
    ws, text: str, index: int, interrupt_event: asyncio.Event,
    latency: LatencyTracker, first_audio: bool,
) -> int:
    chunks: list[bytes] = []
    try:
        async for chunk in stream_tts_pcm(text, chunk_ms=config.TTS_CHUNK_MS):
            chunks.append(chunk)
            if interrupt_event.is_set():
                return 0
    except Exception as exc:
        _log(f"TTS prefetch error: {exc}")
        return 0
    return await _send_pcm_chunks(ws, chunks, index, interrupt_event, latency, first_audio)


async def _speak_text(ws, text: str, interrupt_event: asyncio.Event) -> int:
    chunks: list[bytes] = []
    try:
        async for chunk in stream_tts_pcm(text, chunk_ms=config.TTS_CHUNK_MS):
            chunks.append(chunk)
    except Exception as exc:
        _log(f"TTS error: {exc}")
        return 0
    if interrupt_event.is_set():
        return 0
    return await _send_pcm_chunks(ws, chunks, 0, interrupt_event, None, False)


async def _send_pcm_chunks(
    ws, chunks: list[bytes], index: int,
    interrupt_event: asyncio.Event,
    latency: LatencyTracker | None,
    first_audio: bool,
) -> int:
    if not chunks or interrupt_event.is_set():
        return 0

    if latency and not first_audio:
        latency.mark("FIRST_AUDIO")

    total_bytes = sum(len(c) for c in chunks)
    chunk_duration_s = config.TTS_CHUNK_MS / 1000.0

    await ws.send_text(json.dumps({
        "type": "tts_start",
        "index": index,
        "sample_rate": config.OUTPUT_SAMPLE_RATE,
        "sample_width": config.OUTPUT_SAMPLE_WIDTH,
        "channels": config.OUTPUT_CHANNELS,
        "encoding": "pcm_s16le",
        "bytes": total_bytes,
        "chunk_ms": config.TTS_CHUNK_MS,
    }))

    send_start = asyncio.get_event_loop().time()
    for i, pcm_chunk in enumerate(chunks):
        if interrupt_event.is_set():
            break
        await ws.send_bytes(pcm_chunk)
        elapsed = asyncio.get_event_loop().time() - send_start
        target = (i + 1) * chunk_duration_s * 0.95   # was 0.85 — send closer to real-time
        gap = target - elapsed
        if gap > 0.002:
            await asyncio.sleep(gap)

    await ws.send_text(json.dumps({"type": "tts_end", "index": index}))
    return total_bytes


async def _send_done(ws, reply: str, started_at: float, latency: LatencyTracker) -> None:
    latency.mark("TURN_DONE")
    total_ms = int((time.perf_counter() - started_at) * 1000)
    await ws.send_text(json.dumps({
        "type": "done",
        "assistant_reply": reply,
        "total_latency_ms": total_ms,
    }))
    _log(f"Turn complete: {total_ms} ms")
    latency.report()


async def _drain(total_bytes: int) -> None:
    """Wait for ESP32 to finish playing the last audio chunk.
    Pacing is 0.95 (sends at 95% real-time), so only 5% of audio duration
    remains in the DMA ring when 'done' arrives. Base wait reduced to 120ms.
    """
    if total_bytes <= 0:
        return
    PACING = 0.95
    bps = config.OUTPUT_SAMPLE_RATE * config.OUTPUT_SAMPLE_WIDTH * config.OUTPUT_CHANNELS
    audio_s = total_bytes / bps
    drain_s = audio_s * (1.0 - PACING) + 0.12   # was 0.35 base — saves ~230ms per reply
    _log(f"Drain wait {drain_s * 1000:.0f} ms")
    await asyncio.sleep(drain_s)
