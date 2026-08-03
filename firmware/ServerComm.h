/*
 * ServerComm.h — Full-duplex WebSocket streaming for Aether ESP32 Voice Assistant
 *
 * Protocol:
 *   ESP32  ──► binary PCM chunks (16-bit, 16kHz, mono) continuously
 *   Server ──► JSON control messages  (text frames)
 *   Server ──► PCM audio chunks       (binary frames) for playback
 *   ESP32  ──► {"type":"interrupt"}   (optional: when user speaks over reply)
 *
 * Server address is resolved dynamically via mDNS / fallback IP stored in
 * WiFiHelper.h — no hardcoded IP addresses.
 * Auto-recovery: on any connection failure serverDiscovered is cleared,
 * triggering a fresh mDNS lookup on the next conversation cycle.
 */

#ifndef SERVER_COMM_H
#define SERVER_COMM_H

#include "Declarations.h"
#include <ArduinoJson.h>
#include <WebSocketsClient.h>

// ─────────────────────────────────────────────────────────────────────────
// State shared between the event callback and streamConversationWebSocket()
// ─────────────────────────────────────────────────────────────────────────

static WebSocketsClient voiceSocket;

static volatile bool wsConnected      = false;
static volatile bool wsDone           = false;
static volatile bool wsError          = false;
static volatile bool wsSpeaking       = false;   // server is currently sending audio
static volatile bool wsGotAudio       = false;   // server sent at least one tts_start with bytes
static volatile bool wsServerSpeechEnd = false;  // server VAD: stop mic stream

static char wsFullReply[512];

// ─────────────────────────────────────────────────────────────────────────
// WebSocket event callback
// ─────────────────────────────────────────────────────────────────────────

void voiceWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {

    // ── connection events ────────────────────────────────────────────────
    case WStype_CONNECTED:
      wsConnected = true;
      Serial.println("[WS] Connected");
      break;

    case WStype_DISCONNECTED:
      wsConnected = false;
      Serial.println("[WS] Disconnected");
      break;

    case WStype_ERROR:
      wsError = true;
      Serial.println("[WS] Error");
      break;

    // ── incoming binary frame → play it immediately ──────────────────────
    case WStype_BIN:
      if (length > 0) {
        playPcm16Chunk(payload, length);
      }
      break;

    // ── incoming JSON text frame ─────────────────────────────────────────
    case WStype_TEXT: {
      StaticJsonDocument<512> doc;
      DeserializationError err = deserializeJson(doc, payload, length);
      if (err) break;

      const char* t     = doc["type"]  | "";
      const char* state = doc["state"] | "";

      // State updates → update face animation and LED ring/bulb
      if (strcmp(t, "state") == 0) {
        Serial.printf("[WS] Server state: %s\n", state);
        
        if (strcmp(state, "Idle") == 0) {
          if (currentState == STATE_WAKE_LISTEN || currentState == STATE_IDLE) {
            setFace(FACE_IDLE);
          }
        } else if (strcmp(state, "Wake") == 0 || strcmp(state, "Listening") == 0) {
          if (currentState == STATE_WAKE_LISTEN || currentState == STATE_IDLE) {
            setFace(FACE_LISTENING);
          }
        } else if (strcmp(state, "Transcribing") == 0 || strcmp(state, "Thinking") == 0) {
          setFace(FACE_PROCESSING);
        } else if (strcmp(state, "Speaking") == 0) {
          setFace(FACE_SPEAKING);
        } else if (strcmp(state, "Error") == 0 || strcmp(state, "Offline") == 0) {
          setFace(FACE_ERROR);
        }
      }

      // VAD events
      else if (strcmp(t, "vad") == 0) {
        const char* ev = doc["event"] | "";
        if (strcmp(ev, "speech_start") == 0) {
          Serial.println("[VAD] Speech start detected by server");
        } else if (strcmp(ev, "speech_end") == 0) {
          Serial.println("[VAD] Speech end — stopping mic stream");
          wsServerSpeechEnd = true;
          setState(STATE_PROCESSING);
        }
      }

      // Transcript confirmation
      else if (strcmp(t, "transcript") == 0) {
        Serial.print("[WS] Transcript: ");
        Serial.println((const char*)doc["text"]);
      }

      // Server still working (STT / LLM) — keep connection alive
      else if (strcmp(t, "status") == 0) {
        const char* stage = doc["stage"] | "";
        Serial.printf("[WS] Server status: %s\n", stage);
      }

      // Accumulate LLM reply text for OLED display
      else if (strcmp(t, "ai_delta") == 0) {
        const char* tok = doc["text"] | "";
        strncat(wsFullReply, tok, sizeof(wsFullReply) - strlen(wsFullReply) - 1);
      }

      // TTS audio chunk header — playback happens in WStype_BIN above
      else if (strcmp(t, "tts_start") == 0) {
        int bytes = doc["bytes"] | 0;
        if (bytes > 0) wsGotAudio = true;  // real audio incoming (not empty reply)
        wsSpeaking = true;
        setState(STATE_SPEAKING);
        spkResetDebugCounters();
        int idx = doc["index"] | 0;
        Serial.printf("[WS] TTS start chunk %d  expecting %d bytes\n", idx, bytes);
      }

      else if (strcmp(t, "tts_end") == 0) {
        Serial.printf("[WS] TTS end chunk %d\n", (int)(doc["index"] | 0));
      }

      // Full turn completed
      else if (strcmp(t, "done") == 0) {
        const char* reply = doc["assistant_reply"] | "";
        if (strlen(reply) > 0) {
          currentReply = String(reply);
        } else {
          currentReply = String(wsFullReply);
        }
        int latencyMs = doc["total_latency_ms"] | 0;
        Serial.printf("[WS] Done — latency %d ms\n", latencyMs);
        wsSpeaking = false;
        wsDone     = true;
      }

      // Emotion frame from server
      else if (strcmp(t, "emotion") == 0) {
        const char* em = doc["emotion"] | "";
        applyEmotionToFace(em);
      }

      // Server-side error
      else if (strcmp(t, "error") == 0) {
        Serial.print("[WS] Server error: ");
        Serial.println((const char*)doc["message"]);
        wsError = true;
      }

      // Heartbeat
      else if (strcmp(t, "pong") == 0) {
        // No action needed
      }

      break;
    }

    default:
      break;
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Wait for WebSocket connection (non-blocking poll)
// ─────────────────────────────────────────────────────────────────────────

static bool waitForWsConnect(unsigned long timeoutMs) {
  unsigned long start = millis();
  while (!wsConnected && millis() - start < timeoutMs) {
    for (int i = 0; i < 16; i++) {
      pushPreBuffer(readMicSample());
    }
    voiceSocket.loop();
    delay(0);
  }
  return wsConnected;
}

// ─────────────────────────────────────────────────────────────────────────
// Helper: send interrupt to server
// ─────────────────────────────────────────────────────────────────────────

static void sendInterrupt() {
  voiceSocket.sendTXT("{\"type\":\"interrupt\"}");
  Serial.println("[WS] Interrupt sent");
}

// ─────────────────────────────────────────────────────────────────────────
// playWakeAck — HTTP GET /wake_ack → play pre-generated "Tell me, boss!"
// Called immediately on wake detection, BEFORE opening the recording WS.
// This gives instant spoken feedback so the user knows to speak.
// ─────────────────────────────────────────────────────────────────────────

#if 0
void playWakeAck() {
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT + "/wake_ack";
  Serial.printf("[ACK] Fetching wake ack: %s\n", url.c_str());

  HTTPClient http;
  http.begin(url);
  http.setTimeout(3000);  // 3s timeout — cached on server so should be fast

  int code = http.GET();
  if (code != 200) {
    Serial.printf("[ACK] HTTP error %d — skipping ack\n", code);
    http.end();
    return;
  }

  int totalLen = http.getSize();  // -1 if unknown
  WiFiClient* stream = http.getStreamPtr();

  Serial.printf("[ACK] Receiving %d bytes of wake ack PCM\n", totalLen);
  spkResetDebugCounters();

  static uint8_t ackBuf[1280];  // 40ms chunks
  int received = 0;

  while (http.connected() && (totalLen < 0 || received < totalLen)) {
    int avail = stream->available();
    if (avail == 0) {
      delay(1);
      continue;
    }
    int toRead = min(avail, (int)sizeof(ackBuf));
    int n = stream->readBytes(ackBuf, toRead);
    if (n > 0) {
      playPcm16Chunk(ackBuf, n);
      received += n;
    }
  }

  http.end();
  Serial.printf("[ACK] Wake ack played (%d bytes)\n", received);

  // Let speaker echo die; flush mic DMA so command capture is clean
  i2s_zero_dma_buffer(SPK_I2S_PORT);
  flushMicInput();
  delay(120);
}
#endif

// ─────────────────────────────────────────────────────────────────────────
// Main conversation function
// ─────────────────────────────────────────────────────────────────────────

bool streamConversationWebSocket() {
  ensureWiFi();

  // Reset state
  wsConnected       = false;
  wsDone            = false;
  wsError           = false;
  wsSpeaking        = false;
  wsGotAudio        = false;
  wsServerSpeechEnd = false;
  wsFullReply[0]    = '\0';   // clear static buffer without heap alloc

  // ── Dynamic server address ────────────────────────────────────────────
  // discoveredServerIP and discoveredServerPort are resolved by WiFiHelper
  // via mDNS (aether.local) or the user-configured fallback IP.
  if (!serverDiscovered) {
    Serial.println("[WS] Server not discovered — aborting turn");
    return false;
  }

  char serverIPStr[20];
  discoveredServerIP.toString().toCharArray(serverIPStr, sizeof(serverIPStr));

  Serial.printf("[WS] Connecting to %s:%u%s\n",
                serverIPStr, discoveredServerPort, VOICE_WS_PATH);

  // Connect
  voiceSocket.begin(serverIPStr, discoveredServerPort, VOICE_WS_PATH);
  voiceSocket.onEvent(voiceWsEvent);
  voiceSocket.setReconnectInterval(0);   // no auto-reconnect during a turn

  if (!waitForWsConnect(6000)) {
    Serial.println("[WS] Connect timeout — clearing server discovery");
    voiceSocket.disconnect();
    serverDiscovered = false;   // ← auto-recovery: force re-discovery next cycle
    return false;
  }

  // ── Mic streaming phase ──────────────────────────────────────────────

  setState(STATE_RECORDING);
  Serial.println("[WS] WebSocket connected — starting mic stream");

  // Start command streaming immediately after the WebSocket is ready.
  voiceSocket.sendTXT("{\"type\":\"ping\"}");
  voiceSocket.loop();

  if (SEND_WAKE_PREBUFFER_TO_SERVER) {
    static int16_t warmup[PREBUFFER_SAMPLES];
    size_t idx = preBufferIndex;
    for (size_t i = 0; i < PREBUFFER_SAMPLES; i++) {
      warmup[i] = preBuffer[idx++];
      if (idx >= PREBUFFER_SAMPLES) idx = 0;
    }
    voiceSocket.sendBIN((uint8_t*)warmup, PREBUFFER_SAMPLES * sizeof(int16_t));
    voiceSocket.loop();
  }

  // Live mic stream
  {
    unsigned long recordStart    = millis();
    unsigned long lastVoiceTime  = millis();
    int silenceThreshold         = noiseFloor + SILENCE_THRESHOLD_OFFSET;
    int16_t chunk[STREAM_CHUNK_SAMPLES];
    size_t  chunkSamples = 0;

    while (true) {
      // Server VAD ended speech, turn finished, or error
      if (wsDone || wsError || wsServerSpeechEnd) break;

      // Read one I2S sample
      int16_t sample = readMicSample();
      int     level  = abs(sample);

      faceAudioLevel = (faceAudioLevel * 7 + level) / 8;

      if (level > silenceThreshold) {
        lastVoiceTime = millis();
      }

      pushPreBuffer(sample);
      chunk[chunkSamples++] = sample;

      if (chunkSamples >= STREAM_CHUNK_SAMPLES) {
        // Only send audio if the server is NOT speaking (prevent echo feedback)
        if (!wsSpeaking) {
          voiceSocket.sendBIN((uint8_t*)chunk, chunkSamples * sizeof(int16_t));
        }
        chunkSamples = 0;
        voiceSocket.loop();   // pump incoming audio + control frames
      }

      unsigned long elapsed = millis() - recordStart;
      unsigned long silence = millis() - lastVoiceTime;

      // Local fallback VAD: stop if silence exceeds threshold
      if (elapsed > MIN_RECORD_TIME && silence > SILENCE_TIMEOUT) {
        Serial.println("[WS] Local VAD silence stop");
        break;
      }

      if (elapsed > MAX_RECORD_TIME) {
        Serial.println("[WS] Max record time reached");
        break;
      }
    }

    // Flush last partial chunk (only if not speaking)
    if (chunkSamples > 0 && !wsSpeaking) {
      voiceSocket.sendBIN((uint8_t*)chunk, chunkSamples * sizeof(int16_t));
    }
  }

  faceAudioLevel = 0;
  setState(STATE_PROCESSING);

  // End signal only if server did not already stop us via speech_end
  if (!wsServerSpeechEnd) {
    voiceSocket.sendTXT("{\"type\":\"end\"}");
  }

  // ── Wait for server to finish speaking ───────────────────────────────

  {
    unsigned long waitStart = millis();
    const unsigned long WAIT_TIMEOUT = 90000;  // 90 s — STT+LLM on CPU can exceed 45 s
    int bargeInSustained = 0;
    const size_t CHUNK_SIZE = 256;
    int16_t micChunk[CHUNK_SIZE];

    while (!wsDone && !wsError && millis() - waitStart < WAIT_TIMEOUT) {
      voiceSocket.loop();   // pumps incoming TTS PCM binary frames

      // Check button press to interrupt speaker immediately
      if (digitalRead(BUTTON_PIN) == LOW) {
        Serial.println("[BTN] Button press interrupt during playback");
        sendInterrupt();
        break;
      }

      // Read a block of microphone samples (blocks for ~16ms in hardware)
      readMicSamples(micChunk, CHUNK_SIZE);

      // Process samples
      int64_t sumAbs = 0;
      for (size_t i = 0; i < CHUNK_SIZE; i++) {
        int16_t sample = micChunk[i];
        pushPreBuffer(sample);
        sumAbs += abs(sample);
      }
      int level = sumAbs / CHUNK_SIZE;

      if (wsSpeaking) {
        const int BARGE_IN_THRESHOLD = noiseFloor + SILENCE_THRESHOLD_OFFSET + 300;
        const int MIN_BARGE_IN_SAMPLES = 800; // ~50ms of sustained speech

        if (level > BARGE_IN_THRESHOLD) {
          bargeInSustained += CHUNK_SIZE;
          if (bargeInSustained >= MIN_BARGE_IN_SAMPLES) {
            Serial.println("[BARGE] User voice barge-in detected! Interrupting speaker...");
            sendInterrupt();
            break;
          }
        } else {
          if (bargeInSustained > 0) {
            bargeInSustained = max(0, bargeInSustained - (int)CHUNK_SIZE);
          }
        }
      } else {
        bargeInSustained = 0;
      }
    }
  }

  faceAudioLevel = 0;

  // Allow the I2S DMA to fully drain before zeroing.
  // The server sends at 85% real-time, so a small amount of audio may still
  // be queued in the DMA ring buffer when "done" arrives. 300ms is enough
  // for the 12×512-sample DMA ring to fully play out at 16 kHz.
  if (wsDone) {
    delay(300);
  }

  // Fill DMA with midpoint voltage (1.65 V) rather than 0 V.
  // i2s_zero_dma_buffer() would cause a loud thump when the DAC
  // jumps from audio midpoint to 0 V. silenceSpeaker() avoids this.
  silenceSpeaker();

  bool success = wsDone && !wsError;
  voiceSocket.disconnect();

  if (success) {
    Serial.print("[WS] Reply: ");
    Serial.println(currentReply);
  } else {
    Serial.println("[WS] Conversation failed or timed out");
    // Auto-recovery: clear discovery so next cycle re-resolves the server.
    // This handles server restarts where the IP may have changed.
    serverDiscovered = false;
  }

  return success;
}

#endif // SERVER_COMM_H
