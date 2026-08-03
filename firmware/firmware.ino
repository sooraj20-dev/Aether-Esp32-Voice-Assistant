/*
 * firmware.ino — Aether AI Voice Assistant (Streaming WebSocket Mode)
 *
 * Flow:
 *   1. Boot → display "BOOTING..."
 *   2. WiFiManager: connect or open AETHER_SETUP portal.
 *   3. mDNS / fallback IP: discover FastAPI server (aether.local).
 *   4. Display "AETHER READY" and enter button-wait idle.
 *   5. Button short-press:
 *        a. Pre-session health check → re-discover server if needed.
 *        b. Open WebSocket, stream mic audio, receive TTS audio.
 *   6. GPIO14 held ≥ 5s → factory reset + reboot to setup portal.
 */

#include <WiFi.h>
#include <Wire.h>
#include <Preferences.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "driver/i2s.h"
#include "esp_heap_caps.h"
#include <math.h>

// ── Config & declarations ─────────────────────────────────────────────────
#include "Config.h"
#include "Declarations.h"

// ── Runtime server discovery globals (declared extern in Config.h) ────────
IPAddress discoveredServerIP(0, 0, 0, 0);
uint16_t  discoveredServerPort = DEFAULT_PORT;
bool      serverDiscovered     = false;
String    fallbackServerIP     = SERVER_HOST;  // ← Initialize from Config.h
String    assistantName        = "Aether";

// ── Global hardware globals (declared extern in Declarations.h) ───────────
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
AssistantState   currentState         = STATE_IDLE;
int              noiseFloor           = 50;
int16_t*         pcmBuffer            = nullptr;
size_t           recordedSamples      = 0;
int16_t          preBuffer[PREBUFFER_SAMPLES];
size_t           preBufferIndex       = 0;
String           currentReply         = "";
bool             responseActive       = false;
unsigned long    lastConversationTime = 0;
int              faceAudioLevel       = 0;
SemaphoreHandle_t oledMutex           = nullptr;

// Face animation state (also declared extern in Declarations.h)
FaceMode      currentFaceMode = FACE_IDLE;
unsigned long lastFaceFrame   = 0;
int           faceFrame       = 0;

void safeDisplay() {
  if (oledMutex != nullptr) {
    if (xSemaphoreTake(oledMutex, portMAX_DELAY) == pdTRUE) {
      display.display();
      xSemaphoreGive(oledMutex);
    }
  } else {
    display.display();
  }
}

// ── Module implementations ────────────────────────────────────────────────
#include "Face.h"
#include "WiFiHelper.h"
#include "AudioRecorder.h"
#include "AudioPlayback.h"
#include "ServerComm.h"

// ── State machine ─────────────────────────────────────────────────────────

void setState(AssistantState newState) {
  if (currentState == newState) {
    return;
  }
  const char* names[] = { "IDLE", "WAKE_LISTEN", "RECORDING", "PROCESSING", "SPEAKING", "SLEEP" };
  Serial.printf("[STATE] %s → %s\n", names[currentState], names[newState]);
  currentState = newState;

  switch (newState) {
    case STATE_IDLE:
    case STATE_WAKE_LISTEN:
      if (currentFaceMode != FACE_HAPPY && currentFaceMode != FACE_SAD) {
        setFace(FACE_IDLE);
      }
      break;
    case STATE_RECORDING:    setFace(FACE_RECORDING);  break;
    case STATE_PROCESSING:   setFace(FACE_PROCESSING); break;
    case STATE_SPEAKING:     setFace(FACE_SPEAKING);   break;
    case STATE_SLEEP:        setFace(FACE_SLEEP);      break;
  }
}

// ── Pre-session server resolution guard ───────────────────────────────────
// Call before every conversation turn.
// Returns false if the server is still unreachable after one re-discovery.

bool ensureServerReachable() {
  // ── Fast path: IP already known, just ping ────────────────────────────
  if (serverDiscovered && checkServerHealth()) {
    return true;
  }

  // ── Slow path: (re-)discover then ping ───────────────────────────────
  if (!serverDiscovered) {
    locateServer();
  }
  if (!serverDiscovered) {
    showOLED("SERVER ERROR", "Not reachable");
    playOfflineAlert();
    return false;
  }
  if (checkServerHealth()) {
    return true;
  }

  // Health check failed after discovery — try one more time
  Serial.println("[HEALTH] Ping failed — re-discovering server");
  serverDiscovered = false;
  locateServer();
  if (!serverDiscovered || !checkServerHealth()) {
    setFace(FACE_ERROR);
    showOLED("SERVER ERROR", "Offline");
    playOfflineAlert();
    return false;
  }
  return true;
}

// ── One conversation turn ──────────────────────────────────────────────────

void runConversationCycle() {
  // Set recording state immediately for instant visual feedback on OLED
  setState(STATE_RECORDING);

  // ── Pre-flight: confirm server is reachable ──
  if (!ensureServerReachable()) {
    Serial.println("[BTN] Server unreachable — cancelling turn");
    setState(STATE_WAKE_LISTEN);
    return;
  }

  responseActive = false;

  bool ok = streamConversationWebSocket();

  if (!ok) {
    setFace(FACE_ERROR);
    delay(900);
    responseActive = false;
    lastConversationTime = millis();
    setState(STATE_WAKE_LISTEN);
    return;
  }

  if (!wsGotAudio) {
    Serial.println("[BTN] No audio from server — returning to button wait");
    flushMicInput();
    responseActive = false;
    lastConversationTime = millis();
    setState(STATE_WAKE_LISTEN);
    return;
  }

  delay(POST_PLAYBACK_SETTLE_MS);
  flushMicInput();

  Serial.println("[BTN] Returning to button-wait mode");
  responseActive       = false;
  lastConversationTime = millis();
  setState(STATE_WAKE_LISTEN);
  showOLED(assistantName.c_str(), "Ready", "Press button");
}

void oledTaskCode(void* pvParameters) {
  while (true) {
    updateFace();
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

// ── Setup ─────────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  pinMode(BUTTON_PIN, INPUT_PULLUP);

  Wire.begin(26, 27);   // SDA=26, SCL=27
  Wire.setClock(400000);

  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("OLED failed");
    while (true) delay(1000);
  }
  display.clearDisplay();
  display.display();

  // Show a clean initial boot screen instead of multiple flashing success screens
  showOLED("AETHER AI", "Booting...", "Please wait");

  // ── CHECK: Factory reset flag (set in Config.h) ──
  #ifdef FORCE_FACTORY_RESET
  Serial.println("[SETUP] FORCE_FACTORY_RESET is enabled — triggering factory reset");
  showOLED("FACTORY RESET", "Clearing...", "Please wait");
  factoryReset();  // This never returns (reboots after clearing NVS)
  #endif

  // ── Step 1: Wi-Fi (Initialize WiFi first to prevent I2S/APLL clock/RF interference) ──
  connectWiFi();

  // Config time for India Standard Time (GMT+5:30 = 19800 seconds offset)
  configTime(19800, 0, "pool.ntp.org", "time.nist.gov");

  // ── Step 2: Audio Hardware & Calibration ──
  setupSpeaker();
  setupMic();
  allocateAudioBuffer();
  calibrateNoiseFloor();

  // ── Step 3: Server discovery ──
  locateServer();

  // ── Step 4: Show final result and enter idle ──
  if (!serverDiscovered) {
    showOLED("SERVER ERROR", "Not found");
    playOfflineAlert();
    delay(1200);
  }

  // Create OLED Mutex
  oledMutex = xSemaphoreCreateMutex();

  // Start background OLED task (priority 1 is low, so it yields to I2S/network)
  xTaskCreatePinnedToCore(
    oledTaskCode,     /* Task function. */
    "OLEDTask",       /* name of task. */
    4096,             /* Stack size of task */
    NULL,             /* parameter of the task */
    1,                /* priority of the task */
    NULL,             /* Task handle to keep track of created task */
    1                 /* pin task to core 1 */
  );

  lastConversationTime = millis();
  setState(STATE_WAKE_LISTEN);
}

// ── Main loop ──────────────────────────────────────────────────────────────

void loop() {
  ensureWiFi();

  // ── Inactivity Sleep Mode Trigger ──
  if (currentState == STATE_WAKE_LISTEN && (millis() - lastConversationTime >= SLEEP_TIMEOUT_MS)) {
    setState(STATE_SLEEP);
  }

  // ── Long-press / short-press button logic ─────────────────────────────
  static bool     buttonHeld       = false;
  static unsigned long buttonDownAt = 0;
  static bool     lastButtonState  = HIGH;

  bool currentButtonState = digitalRead(BUTTON_PIN);

  if (lastButtonState == HIGH && currentButtonState == LOW) {
    // Button just pressed down
    buttonHeld    = true;
    buttonDownAt  = millis();
    lastConversationTime = millis(); // Reset inactivity timer on button press

    // ── Instant audio feedback: Alexa-style chime on press-down ──────────
    // Play the wake chime immediately so the user knows the press registered,
    // before WebSocket connects. Only play in states where we will record.
    if (currentState == STATE_IDLE || currentState == STATE_WAKE_LISTEN || currentState == STATE_SLEEP) {
      playWakeSound();
      i2s_zero_dma_buffer(SPK_I2S_PORT); // flush DAC DMA so mic reads are clean
      flushMicInput();                   // discard mic samples captured during chime
    }
  }

  if (buttonHeld && currentButtonState == LOW) {
    unsigned long heldMs = millis() - buttonDownAt;
    if (heldMs >= LONG_PRESS_MS) {
      // ── Long press: factory reset ─────────────────────────────────────
      Serial.println("[BTN] LONG PRESS DETECTED — triggering factory reset!");
      buttonHeld = false;
      factoryReset();   // never returns (reboots)
    }
  }

  bool buttonReleased = (lastButtonState == LOW && currentButtonState == HIGH);
  lastButtonState = currentButtonState;

  if (buttonReleased && buttonHeld) {
    unsigned long heldMs = millis() - buttonDownAt;
    buttonHeld = false;

    if (heldMs < LONG_PRESS_MS) {
      if (currentState == STATE_IDLE || currentState == STATE_WAKE_LISTEN) {
        // ── Short press: conversation cycle ──────────────────────────────
        Serial.println("[BTN] Short press — starting conversation");
        setState(STATE_WAKE_LISTEN);  // ensure we are in WAKE_LISTEN before cycling
        runConversationCycle();
        return;
      } else if (currentState == STATE_SLEEP) {
        // ── Wake up from sleep and start conversation cycle immediately ──
        Serial.println("[BTN] Waking up from sleep, starting conversation");
        lastConversationTime = millis();
        setState(STATE_WAKE_LISTEN);
        runConversationCycle();
        return;
      }
    }
  }

  // ── State updates ─────────────────────────────────────────────────────
  switch (currentState) {
    case STATE_IDLE:
      setState(STATE_WAKE_LISTEN);
      break;

    default:
      break;
  }

  delay(0);   // cooperate with FreeRTOS scheduler without a fixed sleep
}
