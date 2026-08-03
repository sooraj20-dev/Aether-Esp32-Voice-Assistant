/*
 * Declarations.h — Global extern declarations for the Aether ESP32 firmware.
 *
 * Changes from previous version:
 *   • Removed sendToServer() — HTTP upload no longer used.
 *   • Removed playAudioFromURL() — audio arrives via WebSocket binary frames.
 *   • Removed recordAudio() — recording is handled inline in streamConversationWebSocket().
 */

#ifndef DECLARATIONS_H
#define DECLARATIONS_H

#include <Arduino.h>
#include "Config.h"
#include <Adafruit_SSD1306.h>

#include <freertos/semphr.h>

// Forward-declare WakeWordDetector so ServerComm.h can compile
class WakeWordDetector;

// ──────────────────────────────────────────────────────────────────────────
// Global variables (defined in firmware.ino)
// ──────────────────────────────────────────────────────────────────────────
extern Adafruit_SSD1306   display;
extern AssistantState     currentState;
extern int                noiseFloor;
extern int16_t*           pcmBuffer;
extern size_t             recordedSamples;
extern int16_t            preBuffer[PREBUFFER_SAMPLES];
extern size_t             preBufferIndex;
extern String             currentReply;
extern bool               responseActive;
extern unsigned long      lastConversationTime;
extern int                faceAudioLevel;
extern SemaphoreHandle_t  oledMutex;

// Face animation state
extern FaceMode       currentFaceMode;
extern unsigned long  lastFaceFrame;
extern int            faceFrame;

// ── Server discovery globals (defined in alpha.ino) ──────────────────────
#include <WiFi.h>
extern IPAddress discoveredServerIP;
extern uint16_t  discoveredServerPort;
extern bool      serverDiscovered;
extern String    fallbackServerIP;
extern String    assistantName;

// ──────────────────────────────────────────────────────────────────────────
// Function forward declarations
// ──────────────────────────────────────────────────────────────────────────

// Face / OLED
void setFace(FaceMode mode);
void updateFace();
void renderFace(bool force);
void showOLED(const char* title, const char* line1 = "", const char* line2 = "");
void displayResponse(const char* text);
void safeDisplay();
void applyEmotionToFace(const char* emotion);

// State machine
void setState(AssistantState newState);

// WiFi / Provisioning
void connectWiFi();
void ensureWiFi();
void factoryReset();
void locateServer();
bool checkServerHealth();
bool ensureServerReachable();

// Server
bool streamConversationWebSocket();

// Microphone / audio
void setupMic();
void allocateAudioBuffer();
int16_t readMicSample();
void readMicSamples(int16_t* outBuffer, size_t numSamples);
void pushPreBuffer(int16_t sample);
void clearPreBuffer();
void flushMicInput();
void copyPreBufferToPcm();
void calibrateNoiseFloor();

// Speaker
void setupSpeaker();
void playPcm16Chunk(uint8_t* data, size_t length);
void playWakeSound();
void playStartChime();
void playOfflineAlert();

// Wake word
void runConversationCycle();
bool listenForWakeWord();
size_t captureWakeCandidate();
bool sendWakeCandidateToServer(String& recognizedText, String& wakeReply, String& wakeAudioUrl);


#endif // DECLARATIONS_H
