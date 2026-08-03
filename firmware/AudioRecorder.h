#ifndef AUDIO_RECORDER_H
#define AUDIO_RECORDER_H

#include "Declarations.h"
#include "driver/i2s.h"
#include "esp_heap_caps.h"

void setupMic() {
  i2s_config_t i2s_config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 128,
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0
  };

  i2s_pin_config_t pin_config = {
    .mck_io_num = I2S_PIN_NO_CHANGE,
    .bck_io_num = I2S_SCK,
    .ws_io_num = I2S_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = I2S_SD
  };

  i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
  i2s_set_pin(I2S_PORT, &pin_config);
  i2s_zero_dma_buffer(I2S_PORT);

  Serial.println("[I2S] Microphone ready");
}

void allocateAudioBuffer() {
  size_t bytesNeeded = MAX_SAMPLES * sizeof(int16_t);

  Serial.print("[MEM] Need bytes: ");
  Serial.println(bytesNeeded);

  if (psramFound()) {
    Serial.println("[MEM] Using PSRAM");
    pcmBuffer = (int16_t*)ps_malloc(bytesNeeded);
  }

  if (!pcmBuffer) {
    Serial.println("[MEM] Using internal RAM");
    pcmBuffer = (int16_t*)heap_caps_malloc(bytesNeeded, MALLOC_CAP_8BIT);
  }

  if (!pcmBuffer) {
    Serial.println("[MEM] Allocation failed");
    showOLED("MEMORY ERROR");
    while (true) {
      delay(1000);
    }
  }

  memset(pcmBuffer, 0, bytesNeeded);

  Serial.print("[MEM] Free heap: ");
  Serial.println(ESP.getFreeHeap());
}

int16_t readMicSample() {
  int32_t sample32;
  size_t bytesRead;

  i2s_read(I2S_PORT, &sample32, sizeof(sample32), &bytesRead, portMAX_DELAY);

  // INMP441 outputs 24-bit audio left-justified in a 32-bit I2S frame.
  // The top 24 bits carry audio; bottom 8 bits are always zero.
  //
  // To extract a signed 16-bit sample we shift right by 14:
  //   >> 8  →  24-bit value (full resolution)
  //   >> 14 →  18-bit → top 16 bits (2 bits headroom to avoid clipping on loud peaks)
  //
  // Previous: (sample32 >> 13) * 2  ==  >> 12 total  →  4× too loud → noise floor ~10000
  // Correct:   sample32 >> 14                         →  noise floor should be 30–300
  int32_t sample16 = sample32 >> 14;

  if (sample16 >  32767) sample16 =  32767;
  if (sample16 < -32768) sample16 = -32768;

  return (int16_t)sample16;
}

void readMicSamples(int16_t* outBuffer, size_t numSamples) {
  if (numSamples == 0) return;
  // Read in batches of 32-bit values from I2S
  static int32_t rawBuffer[256];
  size_t toRead = numSamples > 256 ? 256 : numSamples;
  size_t bytesRead = 0;
  
  i2s_read(I2S_PORT, rawBuffer, toRead * sizeof(int32_t), &bytesRead, portMAX_DELAY);
  
  size_t samplesRead = bytesRead / sizeof(int32_t);
  for (size_t i = 0; i < samplesRead; i++) {
    int32_t sample16 = rawBuffer[i] >> 14;
    if (sample16 > 32767)  sample16 = 32767;
    if (sample16 < -32768) sample16 = -32768;
    outBuffer[i] = (int16_t)sample16;
  }
  
  // Fill remainder with zero if we read fewer samples than requested
  for (size_t i = samplesRead; i < numSamples; i++) {
    outBuffer[i] = 0;
  }
}

void pushPreBuffer(int16_t sample) {
  preBuffer[preBufferIndex++] = sample;

  if (preBufferIndex >= PREBUFFER_SAMPLES) {
    preBufferIndex = 0;
  }
}

void clearPreBuffer() {
  memset(preBuffer, 0, sizeof(preBuffer));
  preBufferIndex = 0;
}

void flushMicInput() {
  i2s_zero_dma_buffer(I2S_PORT);

  for (int i = 0; i < WAKE_FLUSH_SAMPLES; i++) {
    readMicSample();
  }

  clearPreBuffer();
}

void copyPreBufferToPcm() {
  size_t idx = preBufferIndex;

  for (size_t i = 0; i < PREBUFFER_SAMPLES; i++) {
    if (recordedSamples >= MAX_SAMPLES) {
      break;
    }

    pcmBuffer[recordedSamples++] = preBuffer[idx++];

    if (idx >= PREBUFFER_SAMPLES) {
      idx = 0;
    }
  }
}

void calibrateNoiseFloor() {
  long sum = 0;

  for (int i = 0; i < CALIBRATION_SAMPLES; i++) {
    int16_t sample16 = readMicSample();
    sum += abs(sample16);
    // No delay() here — i2s_read() already blocks in hardware (portMAX_DELAY),
    // so adding delay(1) per sample was wasting ~500 ms of boot time.
    // Yield to FreeRTOS scheduler every 50 samples to stay cooperative.
    if ((i & 63) == 0) taskYIELD();
  }

  noiseFloor = sum / CALIBRATION_SAMPLES;

  // Healthy INMP441 noise floor (after >>14 shift): 30–300.
  // Clamp floor to a minimum of 30 so the VAD threshold is never set to zero.
  if (noiseFloor < 30) {
    noiseFloor = 30;
  }
  // Safety cap: if floor is still unreasonably high after the fix, warn via serial.
  if (noiseFloor > 1000) {
    Serial.println("[CAL] WARNING: noise floor > 1000 — check mic wiring or I2S pin config");
  }

  Serial.print("[CAL] Noise floor: ");
  Serial.println(noiseFloor);
}

void recordAudio() {
  setState(STATE_RECORDING);

  recordedSamples = 0;

  int silenceThreshold = noiseFloor + SILENCE_THRESHOLD_OFFSET;

  unsigned long recordStart = millis();
  unsigned long lastVoice = millis();

  copyPreBufferToPcm();

  Serial.println("[REC] Prebuffer copied");

  while (true) {
    int16_t sample16 = readMicSample();
    int level = abs(sample16);

    if (recordedSamples < MAX_SAMPLES) {
      pcmBuffer[recordedSamples++] = sample16;
    }
    else {
      Serial.println("[REC] Buffer full");
      break;
    }

    if (level > silenceThreshold) {
      lastVoice = millis();
    }

    pushPreBuffer(sample16);
    updateFace();

    unsigned long duration = millis() - recordStart;
    unsigned long silence = millis() - lastVoice;

    if (duration > MIN_RECORD_TIME && silence > SILENCE_TIMEOUT) {
      Serial.println("[REC] Silence stop");
      break;
    }

    if (duration > MAX_RECORD_TIME) {
      Serial.println("[REC] Max duration reached");
      break;
    }
  }

  Serial.print("[REC] Samples: ");
  Serial.println(recordedSamples);

  Serial.print("[REC] Bytes: ");
  Serial.println(recordedSamples * sizeof(int16_t));

  setFace(FACE_PROCESSING);
}

#endif // AUDIO_RECORDER_H
