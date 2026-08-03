"""
app_fastapi.py — Offline ESP32 voice assistant server.

WebSocket /voice_stream: PCM in → STT → Ollama → Piper → PCM out.

mDNS advertisement: This server registers itself as aether.local on startup
so that the ESP32 can discover it automatically via mDNS without any
hardcoded IP addresses.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import sys
import time
from datetime import datetime

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect as StarletteWebSocketDisconnect

try:
    from zeroconf import IPVersion, ServiceInfo, Zeroconf
    _ZEROCONF_AVAILABLE = True
except Exception:  # ImportError or DLL load failures on restricted Windows
    _ZEROCONF_AVAILABLE = False

import config
from services.ai_brain import ConversationContext
from services.pipeline import (
    LatencyTracker,
    STATE_IDLE,
    STATE_LISTENING,
    run_turn,
    send_state,
)
from services.speech_to_text import load_whisper_model
from services.tts_service import load_piper_voice, _load_voice




# ── mDNS advertisement state ──────────────────────────────────────────────
_zeroconf: object | None = None
_mdns_service: object | None = None


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── mDNS / Zeroconf helpers ───────────────────────────────────────────────────

def _get_local_ip() -> str:
    """Return the machine's primary LAN IP (not loopback)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _register_mdns(port: int) -> None:
    """Advertise this server as aether.local (runs in a thread — blocking I/O)."""
    global _zeroconf, _mdns_service
    if not _ZEROCONF_AVAILABLE:
        _log("[mDNS] zeroconf unavailable — skipping (ESP32 will use fallback IP)")
        return
    try:
        local_ip = _get_local_ip()
        ip_bytes = socket.inet_aton(local_ip)

        _mdns_service = ServiceInfo(
            type_="_http._tcp.local.",
            name="aether._http._tcp.local.",
            addresses=[ip_bytes],
            port=port,
            properties={
                b"path":    b"/",
                b"ws":      b"/voice_stream",
                b"version": b"2.0",
            },
            server="aether.local.",
        )
        _zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        _zeroconf.register_service(_mdns_service)
        _log(f"[mDNS] Registered  aether.local → {local_ip}:{port}")
    except Exception as exc:
        _log(f"[mDNS] Registration failed: {exc}")
        _log("[mDNS] ESP32 will use its configured fallback IP")


def _unregister_mdns() -> None:
    """Clean up mDNS registration on shutdown."""
    global _zeroconf, _mdns_service
    try:
        if _zeroconf and _mdns_service:
            _zeroconf.unregister_service(_mdns_service)
            _zeroconf.close()
            _log("[mDNS] Service unregistered")
    except Exception as exc:
        _log(f"[mDNS] Unregister error: {exc}")
    finally:
        _zeroconf    = None
        _mdns_service = None


# ── Energy-based VAD (merged from vad.py, Silero removed to save RAM) ───────────

class StreamingVAD:
    """Detect speech start/end from streaming PCM frames."""

    def __init__(
        self,
        energy_threshold: float = 220.0,
        silence_timeout_ms: int = 700,
        min_speech_ms: int = 100,
        max_speech_ms: int = 8000,
    ) -> None:
        self.energy_threshold = energy_threshold
        self.silence_frames_needed = max(1, silence_timeout_ms // 20)
        self.min_speech_bytes = int(min_speech_ms / 1000 * 16000 * 2)
        self.max_speech_bytes = int(max_speech_ms / 1000 * 16000 * 2)

        self._speaking = False
        self._accumulated = 0
        self._silent_frames = 0
        self._warmup = 0
        self._warmup_needed = 4
        self._noise_floor = 0.0

    @staticmethod
    def _rms(pcm: bytes) -> float:
        n = len(pcm) // 2
        if n == 0:
            return 0.0
        samples = struct.unpack_from(f"<{n}h", pcm)
        return (sum(s * s for s in samples) / n) ** 0.5

    def feed(self, pcm_chunk: bytes) -> str:
        rms = self._rms(pcm_chunk)
        if not self._speaking:
            if self._noise_floor <= 0.0:
                self._noise_floor = rms
            else:
                self._noise_floor = (self._noise_floor * 0.96) + (rms * 0.04)

        adaptive_threshold = max(
            self.energy_threshold,
            self._noise_floor * getattr(config, "VAD_NOISE_MULTIPLIER", 2.4),
            self._noise_floor + getattr(config, "VAD_NOISE_MARGIN", 80.0),
        )
        is_speech = rms > adaptive_threshold

        if is_speech and not self._speaking:
            self._warmup += 1
            self._accumulated += len(pcm_chunk)
            if self._warmup >= self._warmup_needed:
                self._speaking = True
                self._silent_frames = 0
                return "speech_start"
            return "silence"

        if not is_speech and not self._speaking:
            self._warmup = 0
            self._accumulated = 0

        if self._speaking:
            self._accumulated += len(pcm_chunk)
            if is_speech:
                self._silent_frames = 0
            else:
                self._silent_frames += 1

            if self._accumulated >= self.max_speech_bytes:
                self._speaking = False
                return "speech_end"

            if self._silent_frames >= self.silence_frames_needed:
                if self._accumulated >= self.min_speech_bytes:
                    self._speaking = False
                    return "speech_end"
                self._speaking = False
                self._warmup = 0
                self._accumulated = 0
                return "silence"

            return "speech"

        return "silence"

    def reset(self) -> None:
        self._speaking = False
        self._accumulated = 0
        self._silent_frames = 0
        self._warmup = 0


# ── Lifespan (startup + shutdown) ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(application: FastAPI):
    # ── Startup ────────────────────────────────────────────────────────────
    _log("Aether offline voice server starting …")
    # mDNS: runs in thread because Zeroconf uses blocking socket I/O
    await asyncio.to_thread(_register_mdns, config.PORT)
    # Whisper
    model = await asyncio.to_thread(load_whisper_model)
    if model:
        _log(f"Whisper ready: {config.WHISPER_MODEL_SIZE}/{config.WHISPER_COMPUTE_TYPE}")
    else:
        _log("Whisper not available — run: pip install faster-whisper")

    # Piper voices — pre-warm both English and Malayalam so first use has no load delay
    try:
        await asyncio.to_thread(load_piper_voice)          # English (config.PIPER_MODEL)
        _log(f"English voice ready: {config.PIPER_MODEL.name}")
    except Exception as exc:
        _log(f"English voice failed to load: {exc}")
    try:
        await asyncio.to_thread(_load_voice, config.PIPER_MODEL_ML)  # Malayalam
        _log(f"Malayalam voice ready: {config.PIPER_MODEL_ML.name}")
    except Exception as exc:
        _log(f"Malayalam voice failed to load (non-fatal): {exc}")

    yield  # ── server is running ──

    # ── Shutdown ──────────────────────────────────────────────────────────
    await asyncio.to_thread(_unregister_mdns)


app = FastAPI(title="Aether Offline Voice Assistant", lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "mode": "offline",
        "ws_endpoint": "/voice_stream",
        "ollama_model": config.OLLAMA_MODEL,
        "whisper_model": config.WHISPER_MODEL_SIZE,
        "piper_voice_en": str(config.PIPER_MODEL),
        "piper_voice_ml": str(config.PIPER_MODEL_ML),
        "malayalam_threshold": config.MALAYALAM_SCRIPT_THRESHOLD,
        "input_sample_rate": config.INPUT_SAMPLE_RATE,
        "output_sample_rate": config.OUTPUT_SAMPLE_RATE,
    })


# ── WebSocket ────────────────────────────────────────────────────────────────

async def _ws_reader(
    ws: WebSocket,
    message_queue: asyncio.Queue,
    interrupt_event: asyncio.Event,
    connection_alive: list[bool],
    turn_task: list[asyncio.Task | None],
) -> None:
    try:
        while True:
            msg = await ws.receive()
            if "text" in msg:
                try:
                    evt = json.loads(msg["text"])
                    etype = evt.get("type")
                    if etype == "ping":
                        try:
                            await ws.send_text(json.dumps({"type": "pong"}))
                        except Exception:
                            pass
                        continue
                    if etype == "interrupt":
                        _log("Interrupt received")
                        interrupt_event.set()
                        task = turn_task[0]
                        if task and not task.done():
                            task.cancel()
                        await message_queue.put(msg)
                        continue
                except json.JSONDecodeError:
                    pass
            await message_queue.put(msg)
    except (WebSocketDisconnect, StarletteWebSocketDisconnect):
        connection_alive[0] = False
        task = turn_task[0]
        if task and not task.done():
            task.cancel()
        await message_queue.put({"type": "disconnect"})
    except RuntimeError as exc:
        # Silently handle "Cannot call receive once a disconnect message has been received"
        # This is a normal race condition when ESP32 closes the socket after 'done'.
        err_str = str(exc)
        if "disconnect" in err_str or "receive" in err_str:
            pass   # expected — not a real error
        else:
            _log(f"Reader runtime error: {exc}")
        connection_alive[0] = False
        task = turn_task[0]
        if task and not task.done():
            task.cancel()
        await message_queue.put({"type": "disconnect"})
    except Exception as exc:
        _log(f"Reader error: {exc}")
        connection_alive[0] = False
        task = turn_task[0]
        if task and not task.done():
            task.cancel()
        await message_queue.put({"type": "disconnect"})


@app.websocket("/voice_stream")
async def voice_stream(ws: WebSocket) -> None:
    await ws.accept()
    # Support session management: use session_id query parameter, or fall back to device IP (client host)
    session_id = ws.query_params.get("session_id")
    if not session_id:
        session_id = f"device_{ws.client.host}" if ws.client else "default"
    _log(f"ESP32 connected (session_id: {session_id})")

    latency = LatencyTracker()
    context = ConversationContext(config.CONVERSATION_HISTORY_TURNS, session_id=session_id)
    interrupt_event = asyncio.Event()
    connection_alive = [True]
    turn_task: list[asyncio.Task | None] = [None]
    audio_buffer: list[bytes] = []

    message_queue: asyncio.Queue = asyncio.Queue()
    reader_task = asyncio.create_task(
        _ws_reader(ws, message_queue, interrupt_event, connection_alive, turn_task)
    )

    vad = StreamingVAD(
        energy_threshold=config.VAD_ENERGY_THRESHOLD,
        silence_timeout_ms=config.VAD_SILENCE_TIMEOUT_MS,
        min_speech_ms=config.STREAM_MIN_AUDIO_MS,
        max_speech_ms=config.STREAM_MAX_RECORD_MS,
    )

    started_at = time.perf_counter()

    async def _receive_loop() -> bytes:
        nonlocal started_at
        vad.reset()
        audio_buffer.clear()
        speech_started = False
        bytes_received = False

        await send_state(ws, STATE_IDLE)

        while True:
            try:
                msg = await asyncio.wait_for(message_queue.get(), timeout=45.0)
            except asyncio.TimeoutError:
                connection_alive[0] = False
                break
            finally:
                try:
                    message_queue.task_done()
                except ValueError:
                    pass

            if msg.get("type") == "disconnect":
                connection_alive[0] = False
                break

            if "text" in msg:
                try:
                    evt = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")
                if etype == "interrupt":
                    interrupt_event.clear()
                    vad.reset()
                    audio_buffer.clear()
                    speech_started = False
                    await send_state(ws, STATE_IDLE)
                elif etype == "end":
                    if bytes_received:
                        # Always process audio when client signals end,
                        # even if server VAD never detected speech_start
                        # (handles quiet mics below VAD threshold).
                        break
                    continue

            elif "bytes" in msg:
                frame = msg["bytes"]
                if not frame:
                    continue
                bytes_received = True
                audio_buffer.append(frame)
                event = vad.feed(frame)

                if event == "speech_start" and not speech_started:
                    speech_started = True
                    started_at = time.perf_counter()
                    latency.reset()
                    latency.mark("AUDIO_START")
                    await send_state(ws, STATE_LISTENING)
                    try:
                        await ws.send_text(json.dumps({"type": "vad", "event": "speech_start"}))
                    except Exception:
                        pass

                if event == "speech_end":
                    latency.mark("VAD_END")
                    try:
                        await ws.send_text(json.dumps({"type": "vad", "event": "speech_end"}))
                    except Exception:
                        pass
                    break

        return b"".join(audio_buffer)

    try:
        while connection_alive[0]:
            try:
                pcm = await _receive_loop()
            except (WebSocketDisconnect, StarletteWebSocketDisconnect):
                break
            except asyncio.CancelledError:
                break

            if not connection_alive[0]:
                break
            if not pcm:
                await send_state(ws, STATE_IDLE)
                continue

            interrupt_event.clear()

            async def _do_turn() -> None:
                await run_turn(ws, latency, context, interrupt_event, pcm, started_at)

            task = asyncio.create_task(_do_turn())
            turn_task[0] = task
            try:
                await task
            except asyncio.CancelledError:
                _log("Turn cancelled (barge-in)")
            finally:
                turn_task[0] = None

    except (WebSocketDisconnect, StarletteWebSocketDisconnect):
        _log("ESP32 disconnected gracefully.")
    except Exception as err:
        _log(f"WebSocket error: {err}")
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(err)}))
        except Exception:
            pass
    finally:
        reader_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass
        _log("Connection closed — ready for reconnect.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app_fastapi:app",
        host=config.HOST,
        port=config.PORT,
        log_level="info",
        ws_ping_interval=30,
        ws_ping_timeout=120,
    )
