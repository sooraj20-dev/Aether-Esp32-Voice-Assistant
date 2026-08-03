/*
 * AudioPlayback.h — ESP32 → MAX98357A I2S Amplifier Speaker Output
 *
 * Hardware wiring:
 *   MAX98357A VIN   → 5 V
 *   MAX98357A GND   → GND
 *   MAX98357A DIN   → GPIO 25  (I2S data)
 *   MAX98357A BCLK  → GPIO 4   (bit clock)
 *   MAX98357A LRC   → GPIO 2   (word select / frame sync)
 *   MAX98357A GAIN  → unconnected  (+9 dB, LEFT channel output)
 *   MAX98357A SD    → 3.3 V        (always-on, no shutdown)
 *   Speaker SPK+    → MAX98357A OUT+
 *   Speaker SPK-    → MAX98357A OUT-
 *
 * How it works:
 *   The MAX98357A is a Class-D I2S amplifier with an integrated DAC.
 *   It accepts standard Philips I2S signed 16-bit PCM directly.
 *   No DC-bias offset needed — the chip manages its own analog midpoint.
 *
 *   GAIN pin unconnected → amp outputs LEFT channel at +9 dB gain.
 *
 *   ESP32 I2S DMA buffer layout for I2S_CHANNEL_FMT_RIGHT_LEFT:
 *     [RIGHT, LEFT, RIGHT, LEFT, ...]
 *     RIGHT = even index, LEFT = odd index
 *   → Audio placed at BOTH indices so the amp always gets signal,
 *     regardless of which channel it selects.
 *
 * DSP pipeline per sample:
 *   1. VOLUME SCALE  — PLAYBACK_VOLUME_PERCENT (soft pre-scale)
 *   2. CLAMP         — keep within ±32767
 *   3. STEREO DUP    — write same signed value to R (even) and L (odd)
 */

#ifndef AUDIO_PLAYBACK_H
#define AUDIO_PLAYBACK_H

#include "Declarations.h"
#include "driver/i2s.h"

// ── I2S format compatibility macro ───────────────────────────────────────────
// ESP32 Arduino core 1.x / ESP-IDF 3.x  → I2S_COMM_FORMAT_I2S
// ESP32 Arduino core 2.x / ESP-IDF 4.x+ → I2S_COMM_FORMAT_STAND_I2S
#ifndef I2S_COMM_FORMAT_STAND_I2S
  #if defined(I2S_COMM_FORMAT_I2S)
    #define I2S_COMM_FORMAT_STAND_I2S I2S_COMM_FORMAT_I2S
  #else
    #define I2S_COMM_FORMAT_STAND_I2S ((i2s_comm_format_t)0x01)
  #endif
#endif

// ─────────────────────────────────────────────────────────────────────────────
// setupSpeaker — install I2S driver for MAX98357A on SPK_I2S_PORT (I2S_NUM_0)
// ─────────────────────────────────────────────────────────────────────────────

void setupSpeaker() {
  i2s_config_t cfg = {
    // External I2S master TX — drives MAX98357A amp directly.
    // No I2S_MODE_DAC_BUILT_IN: external chip handles the DAC.
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate          = SAMPLE_RATE,
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT,
    // RIGHT_LEFT stereo: DMA = [RIGHT(even), LEFT(odd)]
    // MAX98357A (GAIN open) reads LEFT → audio at odd index
    .channel_format       = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,   // Philips I2S standard
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = 12,
    .dma_buf_len          = 512,
    .use_apll             = true,      // APLL for accurate 16 kHz clock
    .tx_desc_auto_clear   = true,      // output silence on DMA underrun (no clicks)
    .fixed_mclk           = 0         // auto-compute APLL frequency
  };

  esp_err_t err = i2s_driver_install(SPK_I2S_PORT, &cfg, 0, NULL);

  if (err != ESP_OK) {
    // Fallback: retry without APLL
    Serial.printf("[SPK] APLL install failed (%d), retrying without APLL\n", (int)err);
    cfg.use_apll = false;
    err = i2s_driver_install(SPK_I2S_PORT, &cfg, 0, NULL);
  }

  if (err != ESP_OK) {
    Serial.printf("[SPK] FATAL: i2s_driver_install failed: %d\n", (int)err);
    return;
  }

  // Assign physical GPIO pins
  i2s_pin_config_t pins = {
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(4, 4, 0)
    .mck_io_num   = I2S_PIN_NO_CHANGE,   // MAX98357A does not need MCLK
#endif
    .bck_io_num   = SPK_BCLK_PIN,        // GPIO 4  → BCLK
    .ws_io_num    = SPK_LRC_PIN,          // GPIO 2  → LRC / WS
    .data_out_num = SPK_DIN_PIN,          // GPIO 25 → DIN
    .data_in_num  = I2S_PIN_NO_CHANGE
  };

  err = i2s_set_pin(SPK_I2S_PORT, &pins);
  if (err != ESP_OK) {
    Serial.printf("[SPK] FATAL: i2s_set_pin failed: %d\n", (int)err);
    return;
  }

  // Clear DMA — avoids any pop on first playback
  i2s_zero_dma_buffer(SPK_I2S_PORT);

  Serial.printf("[SPK] MAX98357A ready | DIN=GPIO%d  BCLK=GPIO%d  LRC=GPIO%d"
                " | port=%d  %dHz  apll=%s\n",
                SPK_DIN_PIN, SPK_BCLK_PIN, SPK_LRC_PIN,
                (int)SPK_I2S_PORT, (int)cfg.sample_rate,
                cfg.use_apll ? "on" : "off");
}

// ─────────────────────────────────────────────────────────────────────────────
// Debug counters
// ─────────────────────────────────────────────────────────────────────────────

static uint32_t _spkFrameCount = 0;
static uint32_t _spkBytesTotal = 0;

void spkResetDebugCounters() {
  _spkFrameCount = 0;
  _spkBytesTotal = 0;
}

// ─────────────────────────────────────────────────────────────────────────────
// playPcm16Chunk
//
// Accepts signed 16-bit mono PCM from the server.
// Writes interleaved stereo to I2S DMA for the MAX98357A:
//
//   DMA index:  [0=RIGHT] [1=LEFT] [2=RIGHT] [3=LEFT] ...
//   Audio goes to BOTH so the amp gets signal on LEFT (GAIN=open)
//
// ─────────────────────────────────────────────────────────────────────────────

void playPcm16Chunk(uint8_t* data, size_t length) {
  if (!data || length < 2) return;
  if (length & 1) length--;   // ensure complete 16-bit samples

  _spkFrameCount++;
  _spkBytesTotal += length;

  if (_spkFrameCount == 1) {
    Serial.printf("[SPK] First audio frame: %u bytes\n", (unsigned)length);
  }

  size_t monoSamples = length / 2;

  // Stereo interleaved buffer: [R, L] pairs × up to 1024 samples
  static int16_t stereoBuffer[2048];

  if (monoSamples > 1024) {
    monoSamples = 1024;   // clamp — buffer holds max 1024 stereo pairs
  }

  int64_t sumAbs = 0;

  for (size_t i = 0; i < monoSamples; i++) {
    // Little-endian 16-bit signed load
    int16_t sample = (int16_t)((uint16_t)data[i * 2] | ((uint16_t)data[i * 2 + 1] << 8));

    // 1. Volume scale (32-bit to prevent overflow)
    int32_t s = ((int32_t)sample * PLAYBACK_VOLUME_PERCENT) / 100;

    // 2. Clamp to ±32767
    if (s >  32767) s =  32767;
    if (s < -32768) s = -32768;

    int16_t out = (int16_t)s;

    // 3. Write to both stereo channels
    //    RIGHT = even index, LEFT = odd index (ESP32 I2S_CHANNEL_FMT_RIGHT_LEFT)
    //    MAX98357A (GAIN open) reads LEFT → odd index carries the audio
    stereoBuffer[i * 2]     = out;   // RIGHT (even)
    stereoBuffer[i * 2 + 1] = out;   // LEFT  (odd) ← MAX98357A reads this

    sumAbs += (s < 0 ? -s : s);
  }

  // Smoothed amplitude for face animation
  static int smoothedLevel = 0;
  int level      = (monoSamples > 0) ? (int)(sumAbs / monoSamples) : 0;
  smoothedLevel  = (smoothedLevel * 7 + level) / 8;
  faceAudioLevel = smoothedLevel;

  // Write to I2S DMA — stereoBytes = monoSamples × 2 ch × 2 bytes
  size_t    stereoBytes = monoSamples * 4;
  size_t    written     = 0;
  esp_err_t err = i2s_write(SPK_I2S_PORT, stereoBuffer, stereoBytes,
                             &written, pdMS_TO_TICKS(200));
  if (err != ESP_OK) {
    Serial.printf("[SPK] i2s_write error: %d\n", (int)err);
  } else if (written < stereoBytes) {
    Serial.printf("[SPK] Partial write: %u/%u bytes\n",
                  (unsigned)written, (unsigned)stereoBytes);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// silenceSpeaker — zero the DMA ring buffer (clean digital silence)
// MAX98357A handles signed zero cleanly — no DC-bias midpoint needed.
// ─────────────────────────────────────────────────────────────────────────────

void silenceSpeaker() {
  i2s_zero_dma_buffer(SPK_I2S_PORT);
}

// ─────────────────────────────────────────────────────────────────────────────
// playWakeSound — ascending two-tone "ding" (Alexa-style acknowledgment)
// ─────────────────────────────────────────────────────────────────────────────

void playWakeSound() {
  int16_t toneBuf[256];

  // Tone 1: 600 Hz for ~96 ms
  for (int chunk = 0; chunk < 6; chunk++) {
    for (int i = 0; i < 256; i++) {
      float t = (float)(chunk * 256 + i) / SAMPLE_RATE;
      toneBuf[i] = (int16_t)(11000.0f * sinf(2.0f * PI * 600.0f * t));
    }
    playPcm16Chunk((uint8_t*)toneBuf, sizeof(toneBuf));
  }

  // Tone 2: 900 Hz for ~96 ms (higher pitch)
  for (int chunk = 0; chunk < 6; chunk++) {
    for (int i = 0; i < 256; i++) {
      float t = (float)(chunk * 256 + i) / SAMPLE_RATE;
      toneBuf[i] = (int16_t)(11000.0f * sinf(2.0f * PI * 900.0f * t));
    }
    playPcm16Chunk((uint8_t*)toneBuf, sizeof(toneBuf));
  }

  silenceSpeaker();
}

// ─────────────────────────────────────────────────────────────────────────────
// playOfflineAlert — descending two-tone alert (server unreachable)
// ─────────────────────────────────────────────────────────────────────────────

void playOfflineAlert() {
  int16_t toneBuf[256];

  // Tone 1: 440 Hz for ~192 ms
  for (int chunk = 0; chunk < 12; chunk++) {
    for (int i = 0; i < 256; i++) {
      float t = (float)(chunk * 256 + i) / SAMPLE_RATE;
      toneBuf[i] = (int16_t)(12000.0f * sinf(2.0f * PI * 440.0f * t));
    }
    playPcm16Chunk((uint8_t*)toneBuf, sizeof(toneBuf));
  }

  // Brief gap ~48 ms
  memset(toneBuf, 0, sizeof(toneBuf));
  for (int chunk = 0; chunk < 3; chunk++) {
    playPcm16Chunk((uint8_t*)toneBuf, sizeof(toneBuf));
  }

  // Tone 2: 330 Hz for ~192 ms (lower pitch)
  for (int chunk = 0; chunk < 12; chunk++) {
    for (int i = 0; i < 256; i++) {
      float t = (float)(chunk * 256 + i) / SAMPLE_RATE;
      toneBuf[i] = (int16_t)(10000.0f * sinf(2.0f * PI * 330.0f * t));
    }
    playPcm16Chunk((uint8_t*)toneBuf, sizeof(toneBuf));
  }

  silenceSpeaker();
}

#endif // AUDIO_PLAYBACK_H
