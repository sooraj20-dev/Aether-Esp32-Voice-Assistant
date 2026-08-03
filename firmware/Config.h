/*
 * Config.h — Aether ESP32 Voice Assistant Configuration
 *
 * Performance tuning:
 *   • OLED_FRAME_MS / OLED_AUDIO_FRAME_MS — FPS caps for OLED renderer.
 *   • DMA buffers increased in AudioPlayback.h for smoother audio.
 *   • WiFiManager AP name / password defined here (not inline in headers).
 */

#ifndef CONFIG_H
#define CONFIG_H

#include <Arduino.h>

// ──────────────────────────────────────────────────────────────────────────
// FACTORY RESET FLAG
// ──────────────────────────────────────────────────────────────────────────
// Set this to true to CLEAR all saved WiFi + server settings from flash memory.
// After uploading with this set to true, the ESP32 will erase NVS and reboot.
// Then set it back to false and re-upload for normal operation.
// #define FORCE_FACTORY_RESET true   // ← NVS wiped. Keep this commented out for normal operation.

// ──────────────────────────────────────────────────────────────────────────
// Wi-Fi Connection Mode
// ──────────────────────────────────────────────────────────────────────────
// Uncomment the line below to bypass WiFiManager and use hardcoded credentials
// (Useful if WiFiManager fails to scan or connect, or for quick setup).
// When active, you must configure 'ssid', 'password', and 'SERVER_HOST' below.
// #define BYPASS_WIFIMANAGER

// ──────────────────────────────────────────────────────────────────────────
// WiFiManager provisioning portal (used only if BYPASS_WIFIMANAGER is commented out)
// ──────────────────────────────────────────────────────────────────────────
#define WIFI_AP_NAME     "AetherSetup"  // SSID the portal broadcasts
#define WIFI_AP_PASSWORD "aether1234"   // Portal password (min 8 chars)

// ──────────────────────────────────────────────────────────────────────────
// mDNS server discovery
// ──────────────────────────────────────────────────────────────────────────
#define MDNS_HOST      "aether"         // Looks for aether.local (matches Python server)
#define MDNS_SERVICE   "_http"
#define MDNS_PROTOCOL  "_tcp"
#define DEFAULT_PORT   5000

// ──────────────────────────────────────────────────────────────────────────
// Button timing
// ──────────────────────────────────────────────────────────────────────────
#define LONG_PRESS_MS  5000             // Hold ≥5 s → factory reset

// ──────────────────────────────────────────────────────────────────────────
// OLED frame-rate caps (milliseconds between redraws)
//   Lower = smoother animation but more SPI bus time stolen from audio DMA.
//   10 FPS (100 ms) is a good balance for animated faces.
//   During audio/speaking: OLED completely paused (handled by flag in Face.h)
//   to eliminate SPI bus contention with I2S DMA — root cause of voice breaking.
// ──────────────────────────────────────────────────────────────────────────
#define OLED_FRAME_MS        100        // 10 FPS — normal states
#define OLED_AUDIO_FRAME_MS  500        // 2 FPS — SPEAKING / RECORDING (minimal SPI during audio)

// How often to run the WiFi reconnect guard in the main loop.
#define WIFI_CHECK_INTERVAL_MS 5000     // ms

// ──────────────────────────────────────────────────────────────────────────
// Sleep / inactivity timeout
// ──────────────────────────────────────────────────────────────────────────
#define SLEEP_TIMEOUT_MS     10000      // 10 s of inactivity → sleep mode
#define SLEEP_CLOCK_UPDATE_MS 1000      // update digital clock every 1 s

// ──────────────────────────────────────────────────────────────────────────
// OLED
// ──────────────────────────────────────────────────────────────────────────
#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT 64
#define OLED_RESET    -1

// ──────────────────────────────────────────────────────────────────────────
// WiFi & Server
// ──────────────────────────────────────────────────────────────────────────
const char* const ssid        = "Host";
const char* const password    = "12345678";
const char* const SERVER_HOST = "";          // Leave blank — mDNS (aether.local) will discover the IP automatically
const uint16_t    SERVER_PORT = 5000;
const char* const HEALTH_URL  = "";          // Not used directly; health check uses discoveredServerIP
const char* const VOICE_WS_PATH = "/voice_stream";

// ──────────────────────────────────────────────────────────────────────────
// Button
// ──────────────────────────────────────────────────────────────────────────
#define BUTTON_PIN 14

// ──────────────────────────────────────────────────────────────────────────
// I2S Microphone (INMP441)
// ──────────────────────────────────────────────────────────────────────────
#define I2S_WS   32
#define I2S_SD   34
#define I2S_SCK  33
#define I2S_PORT I2S_NUM_1

// ──────────────────────────────────────────────────────────────────────────
// I2S Speaker (MAX98357A external amplifier)
//   DIN  → GPIO 25  (I2S data)
//   BCLK → GPIO 4   (bit clock)
//   LRC  → GPIO 2   (word select / frame sync)
//   GAIN → unconnected (default +9 dB, LEFT channel)
//   SD   → 3.3 V (always-on)
// ──────────────────────────────────────────────────────────────────────────
#define SPK_I2S_PORT         I2S_NUM_0
#define SPK_DIN_PIN          25      // I2S data  → MAX98357A DIN
#define SPK_BCLK_PIN         4       // bit clock → MAX98357A BCLK
#define SPK_LRC_PIN          2       // word sel  → MAX98357A LRC
#define PLAYBACK_VOLUME_PERCENT 85   // software pre-scale before amp

// ──────────────────────────────────────────────────────────────────────────
// Audio format (must match server config.py)
// ──────────────────────────────────────────────────────────────────────────
#define SAMPLE_RATE 16000
#define CHANNELS    1

// WebSocket streaming chunk size
#define STREAM_CHUNK_MS      20
#define STREAM_CHUNK_SAMPLES ((SAMPLE_RATE * STREAM_CHUNK_MS) / 1000)  // 320 samples

// ──────────────────────────────────────────────────────────────────────────
// Pre-buffer (audio captured just before wake detection fires)
// ──────────────────────────────────────────────────────────────────────────
#define PREBUFFER_SAMPLES 4800   // 300 ms at 16 kHz — captures query start after wake word
#define SEND_WAKE_PREBUFFER_TO_SERVER true

// ──────────────────────────────────────────────────────────────────────────
// Local energy-based silence fallback
// Server VAD is the PRIMARY stop signal (sends {"type":"vad","event":"speech_end"}).
// These values are intentionally generous so the local VAD only fires as a
// safety fallback — NOT as the normal recording terminator.
// ──────────────────────────────────────────────────────────────────────────
#define MIN_RECORD_TIME          800    // ms — minimum before silence stop is checked
#define MAX_RECORD_TIME          10000  // ms — 10 second hard limit
#define SILENCE_TIMEOUT          2500   // ms of silence before stopping — waits for real sentence end
#define SILENCE_THRESHOLD_OFFSET 60     // added to noise floor

// ──────────────────────────────────────────────────────────────────────────
// Wake-word energy detector
// ──────────────────────────────────────────────────────────────────────────
#define RMS_WINDOW             32
#define WAKE_SENSITIVITY       350    // Stage 1 energy threshold above noise floor
                                      // (lowered from 600: INMP441 gives modest amplitude)
#define WAKE_MIN_RMS_OFFSET    80
#define WAKE_CONFIRMATIONS     6      // ~12 ms of sustained energy required
                                      // (6 × 32-sample RMS windows = ~12 ms at 16 kHz)
#define WAKE_RESET_TIMEOUT     500
#define WAKE_RECHECK_COOLDOWN  400
#define WAKE_FLUSH_SAMPLES     1600
#define WAKE_TO_COMMAND_DELAY  250
#define WAKE_CANDIDATE_MS      1500
#define WAKE_CANDIDATE_SAMPLES ((SAMPLE_RATE * WAKE_CANDIDATE_MS) / 1000)
#define MAX_SAMPLES            WAKE_CANDIDATE_SAMPLES
// Peak-to-RMS ratio guard: reject clicks/knocks whose peak >> average energy
#define WAKE_PEAK_RATIO        8

// ──────────────────────────────────────────────────────────────────────────
// Noise floor calibration
// ──────────────────────────────────────────────────────────────────────────
#define CALIBRATION_SAMPLES 500

// ──────────────────────────────────────────────────────────────────────────
// Conversation cooldown (prevent immediate re-trigger)
// ──────────────────────────────────────────────────────────────────────────
#define CONVERSATION_COOLDOWN 100

// After TTS playback — time before follow-up or wake-word listen
// Raised to 900ms: gives speaker time to drain and prevents mic echo from triggering
#define POST_PLAYBACK_SETTLE_MS  400    // ms — was 900, less dead wait after reply
// How long to wait for a follow-up utterance (no wake word needed)
#define FOLLOW_UP_WINDOW_MS      400    // ms — was 700

// ──────────────────────────────────────────────────────────────────────────
// State machine
// ──────────────────────────────────────────────────────────────────────────
enum AssistantState {
  STATE_IDLE,
  STATE_WAKE_LISTEN,
  STATE_RECORDING,
  STATE_PROCESSING,
  STATE_SPEAKING,
  STATE_SLEEP
};

// ──────────────────────────────────────────────────────────────────────────
// OLED face modes
// ──────────────────────────────────────────────────────────────────────────
enum FaceMode {
  FACE_IDLE,
  FACE_LISTENING,
  FACE_RECORDING,
  FACE_PROCESSING,
  FACE_SPEAKING,
  FACE_HAPPY,
  FACE_ERROR,
  FACE_SLEEP,
  FACE_SAD
};

// ──────────────────────────────────────────────────────────────────────────

#endif // CONFIG_H
