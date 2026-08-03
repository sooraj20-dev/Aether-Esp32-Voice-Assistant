/*
 * Face.h — High-fidelity OLED face renderer for AI Voice Assistant (ESP32 + SSD1306 128×64)
 *
 * Design merged from StackChanFace.cpp/.h into a single header-only implementation.
 * Requires only: Declarations.h (provides `display` global, FaceMode enum).
 *
 * Emotions supported (matched to AI assistant states):
 *   FACE_IDLE        — neutral resting face with breathing bob
 *   FACE_LISTENING   — attentive scan + expanding sound-wave arcs
 *   FACE_RECORDING   — same as listening, mouth pulses with level
 *   FACE_PROCESSING  — thinking look-away + floating "..."
 *   FACE_SPEAKING    — organic jaw flap + speed shimmer lines
 *   FACE_HAPPY       — big crescent smile + sparkle cross
 *   FACE_ERROR       — angry X eyes + frown + broken wifi icon
 *
 * Layout (128×64):
 *   Eyes:       y ≈ 24   (with breathing ±1–2 px)
 *   Mouth:      y ≈ 44
 *   Status bar: y  54-63  (divider line + centred label)
 */

#ifndef FACE_H
#define FACE_H

#include "Declarations.h"

// ─────────────────────────────────────────────────────────────────────────────
// Layout constants
// ─────────────────────────────────────────────────────────────────────────────
#define EYE_L_X     38
#define EYE_R_X     90
#define EYE_Y       24
#define EYE_RADIUS   8      // Base radius for both eyes

#define MOUTH_CX    64
#define MOUTH_Y     44

#define DIV_Y       53      // Horizontal divider line y
#define STATUS_Y    56      // Status text baseline y

// ─────────────────────────────────────────────────────────────────────────────
// Global face state variables are declared extern in Declarations.h and defined in firmware.ino


// Persistent animation state (no class — all static/global)
static float  s_breathPhase     = 0.0f;
static float  s_mouthWeight     = 0.0f;   // 0.0 closed → 1.0 max open
static bool   s_isBlinking      = false;
static float  s_blinkWeight     = 1.0f;   // 1.0 fully open → 0.0 fully closed
static unsigned long s_lastBlink = 0;
static unsigned long s_blinkInterval = 4000;
static unsigned long s_blinkStartMs  = 0;
static float  s_listenScanX     = 0.0f;   // Smooth eye-scan for listening
static float  s_speakWavePhase  = 0.0f;   // Phase for shimmer / jaw wave

// ─────────────────────────────────────────────────────────────────────────────
// Low-level drawing helpers
// ─────────────────────────────────────────────────────────────────────────────

static void drawRealisticEye(int cx, int cy, int r, float eyeOpen) {
  if (eyeOpen <= 0.0f) {
    display.drawFastHLine(cx - r, cy, r * 2 + 1, SSD1306_WHITE);
    return;
  }
  display.fillCircle(cx, cy, r, SSD1306_WHITE);
  if (eyeOpen < 1.0f) {
    int lidH = (int)((1.0f - eyeOpen) * r);
    if (lidH > 0) {
      display.fillRect(cx - r - 1, cy - r - 1, r * 2 + 3, lidH + 1, SSD1306_BLACK);
      display.fillRect(cx - r - 1, cy + r - lidH, r * 2 + 3, lidH + 1, SSD1306_BLACK);
      // Optional: Draw thin eyelid borders to look more realistic
      display.drawFastHLine(cx - r, cy - r + lidH, r * 2 + 1, SSD1306_WHITE);
      display.drawFastHLine(cx - r, cy + r - lidH, r * 2 + 1, SSD1306_WHITE);
    }
  }
}

static void drawEyebrow(int cx, int cy) {
  display.drawFastHLine(cx - 5, cy, 11, SSD1306_WHITE);
  display.drawPixel(cx - 6, cy + 1, SSD1306_WHITE);
  display.drawPixel(cx + 6, cy + 1, SSD1306_WHITE);
}

static void drawSmoothSmile(int cx, int cy, int w, int h) {
  int hw = w / 2;
  int x0 = cx - hw;
  int y0 = cy;
  int x1 = cx - hw / 2;
  int y1 = cy + h;
  int x2 = cx + hw / 2;
  int y2 = cy + h;
  int x3 = cx + hw;
  int y3 = cy;
  
  display.drawLine(x0, y0, x1, y1, SSD1306_WHITE);
  display.drawLine(x1, y1, x2, y2, SSD1306_WHITE);
  display.drawLine(x2, y2, x3, y3, SSD1306_WHITE);
  
  display.drawLine(x0, y0 + 1, x1, y1 + 1, SSD1306_WHITE);
  display.drawLine(x1, y1 + 1, x2, y2 + 1, SSD1306_WHITE);
  display.drawLine(x2, y2 + 1, x3, y3 + 1, SSD1306_WHITE);
}

/*
 * drawFilledEye — Draw a filled round eye with optional top-lid cut for blink.
 *   cx, cy    : centre pixel
 *   r         : radius
 *   blinkLid  : 0.0 = fully open, 1.0 = fully closed (lid sweeps from top)
 */
static void drawFilledEye(int cx, int cy, int r, float blinkLid = 0.0f) {
  display.fillCircle(cx, cy, r, SSD1306_WHITE);
  if (blinkLid > 0.02f) {
    int lidH = (int)(blinkLid * (r * 2 + 2));
    display.fillRect(cx - r - 1, cy - r - 1, r * 2 + 3, lidH + 1, SSD1306_BLACK);
    // Restore thin eyelid line so the eye reads cleanly
    if (blinkLid < 0.95f)
      display.drawFastHLine(cx - r, cy - r - 1 + lidH, r * 2, SSD1306_WHITE);
  }
}

/*
 * drawHappyEye — Crescent arch facing down (^ shape) — used for Happy/Smile.
 */
static void drawHappyEye(int cx, int cy, int r) {
  for (int dx = -1; dx <= 1; dx++) {
    display.drawCircleHelper(cx + dx, cy + 3, r, 0x01 | 0x02, SSD1306_WHITE);
  }
}

/*
 * drawAngryEye — Filled circle with diagonal black cut on the inner-top corner.
 *   is_left: which eye (cut flips)
 */
static void drawAngryEye(int cx, int cy, int r, bool is_left) {
  display.fillCircle(cx, cy, r, SSD1306_WHITE);
  if (is_left) {
    display.fillTriangle(
      cx - r - 1, cy - r - 1,
      cx + r + 1, cy - r - 1,
      cx + r + 1, cy - 2,
      SSD1306_BLACK);
  } else {
    display.fillTriangle(
      cx - r - 1, cy - r - 1,
      cx + r + 1, cy - r - 1,
      cx - r - 1, cy - 2,
      SSD1306_BLACK);
  }
}

/*
 * drawSadEye — Filled circle with diagonal black cut on the outer-top corner.
 */
static void drawSadEye(int cx, int cy, int r, bool is_left) {
  display.fillCircle(cx, cy, r, SSD1306_WHITE);
  if (is_left) {
    display.fillTriangle(
      cx - r - 1, cy - r - 1,
      cx + r + 1, cy - r - 1,
      cx - r - 1, cy - 2,
      SSD1306_BLACK);
  } else {
    display.fillTriangle(
      cx - r - 1, cy - r - 1,
      cx + r + 1, cy - r - 1,
      cx + r + 1, cy - 2,
      SSD1306_BLACK);
  }
}

/*
 * drawPixelSmile — Staircase pixel-art smile arc.
 *   cx, cy : reference centre
 *   w      : half-width
 *   lift   : depth of arc (px the centre dips below ends)
 */
static void drawPixelSmile(int cx, int cy, int w, int lift) {
  display.fillRect(cx - w,         cy - lift + 2, 6, 3, SSD1306_WHITE);
  display.fillRect(cx - w + 7,     cy - lift + 4, 5, 3, SSD1306_WHITE);
  display.fillRect(cx - 5,         cy - lift + 5, 10, 3, SSD1306_WHITE);
  display.fillRect(cx + 6,         cy - lift + 4, 5, 3, SSD1306_WHITE);
  display.fillRect(cx + w - 5,     cy - lift + 2, 6, 3, SSD1306_WHITE);
}

/*
 * drawOpenMouth — Rounded open mouth, height driven by `openH` (0–max).
 *   A thin black erase strip on top gives a natural lower-lip crescent shape.
 */
static void drawOpenMouth(int cx, int cy, int w, int openH) {
  if (openH < 2) openH = 2;
  display.fillRoundRect(cx - w / 2, cy - openH / 2, w, openH, openH / 3, SSD1306_WHITE);
  // Erase inner top to create crescent / jaw open illusion
  display.fillRect(cx - w / 2 - 1, cy - openH / 2 - 1, w + 2, openH / 3, SSD1306_BLACK);
}

/*
 * drawSpeakMouth — Highly organic animated jaw for speaking.
 *   weight : 0.0 closed → 1.0 fully open
 *   phase  : continuously incrementing phase for natural micro-vibration
 *
 *  Inner technique:
 *    - Outer arch (upper lip) stays fixed as a filled semi-circle top
 *    - Lower jaw drops and closes driven by weight + tiny sine ripple
 *    - Inner cavity drawn in black to look like an actual open mouth
 *    - Teeth line added when mouth is moderately open
 */
static void drawSpeakMouth(int cx, int cy, float weight, float phase) {
  const int W  = 22;   // half-width of mouth
  const int MH = 14;   // max height when fully open

  // Micro-jitter: adds <1 px natural movement even between major phoneme changes
  float jitter = sin(phase * 7.3f) * 0.08f + sin(phase * 13.1f) * 0.05f;
  float w      = weight + jitter;
  if (w < 0.0f) w = 0.0f;
  if (w > 1.0f) w = 1.0f;

  int h = (int)(w * MH);
  if (h < 1) h = 1;

  // Draw outer white lip shape (rounded rect)
  display.fillRoundRect(cx - W, cy - h / 2, W * 2, h + 3, 4, SSD1306_WHITE);

  if (h >= 3) {
    // Black inner cavity — creates the open-mouth look
    int innerH = h - 2;
    if (innerH > 1) {
      display.fillRoundRect(cx - W + 3, cy - innerH / 2 + 2, (W - 3) * 2, innerH, 3, SSD1306_BLACK);
    }

    // Teeth line — thin white strip just inside top lip, visible when mouth opens enough
    if (h >= 5) {
      display.fillRect(cx - W + 4, cy - h / 2 + 2, (W - 4) * 2, 2, SSD1306_WHITE);
    }
  }
}

/*
 * drawFlatMouth — Simple flat resting mouth line.
 */
static void drawFlatMouth(int cx, int cy) {
  display.fillRect(cx - 8, cy, 16, 2, SSD1306_WHITE);
  display.drawPixel(cx - 9, cy + 1, SSD1306_WHITE);
  display.drawPixel(cx + 9, cy + 1, SSD1306_WHITE);
}

/*
 * drawFrownMouth — Inverted staircase pixel arc (sad frown).
 */
static void drawFrownMouth(int cx, int cy) {
  display.fillRect(cx - 10,    cy + 4, 6, 2, SSD1306_WHITE);
  display.fillRect(cx - 4,     cy + 2, 5, 2, SSD1306_WHITE);
  display.fillRect(cx + 1,     cy + 1, 7, 2, SSD1306_WHITE);
  display.fillRect(cx + 8,     cy + 2, 5, 2, SSD1306_WHITE);
  display.fillRect(cx + 13,    cy + 4, 6, 2, SSD1306_WHITE);
}

/*
 * drawSoundWaves — Concentric outward arcs simulating microphone listening.
 *   side: -1 = left edge, +1 = right edge
 *   rings: how many rings to draw (1–3)
 */
static void drawSoundWaves(int side, int rings) {
  int ox = (side == -1) ? 10 : 118;
  int oy = 30;
  // quadrant masks: left side = Q1|Q2 (0x01|0x02), right side = Q3|Q4 (0x04|0x08)
  uint8_t mask = (side == -1) ? (0x01 | 0x02) : (0x04 | 0x08);
  if (rings >= 1) display.drawCircleHelper(ox, oy, 5,  mask, SSD1306_WHITE);
  if (rings >= 2) display.drawCircleHelper(ox, oy, 9,  mask, SSD1306_WHITE);
  if (rings >= 3) display.drawCircleHelper(ox, oy, 13, mask, SSD1306_WHITE);
}

/*
 * drawShimmerLines — Speed / talking energy lines beside the mouth.
 */
static void drawShimmerLines(int cx, int cy, float phase) {
  int y0 = cy - 5 + (int)(sin(phase) * 1.5f);
  int y1 = cy     + (int)(sin(phase + 1.1f) * 1.5f);
  int y2 = cy + 5 + (int)(sin(phase + 2.2f) * 1.5f);

  // Right side shimmer
  display.fillRect(cx + 26, y0, 9, 2, SSD1306_WHITE);
  display.fillRect(cx + 28, y1, 7, 2, SSD1306_WHITE);
  display.fillRect(cx + 26, y2, 9, 2, SSD1306_WHITE);

  // Left side shimmer (mirrored)
  display.fillRect(cx - 35, y0, 9, 2, SSD1306_WHITE);
  display.fillRect(cx - 35, y1, 7, 2, SSD1306_WHITE);
  display.fillRect(cx - 35, y2, 9, 2, SSD1306_WHITE);
}

// ─────────────────────────────────────────────────────────────────────────────
// Status bar
// ─────────────────────────────────────────────────────────────────────────────

static const char* faceStatusText() {
  switch (currentFaceMode) {
    case FACE_LISTENING:  return "Listening...";
    case FACE_RECORDING:  return "Recording...";
    case FACE_PROCESSING: return "Thinking...";
    case FACE_SPEAKING:   return "Speaking...";
    case FACE_HAPPY:      return "Happy!";
    case FACE_SAD:        return "Sad...";
    case FACE_ERROR:      return "Error!";
    case FACE_IDLE:
    default:              return "Ready";
  }
}

static void drawStatusBar() {
  display.drawFastHLine(0, DIV_Y, 128, SSD1306_WHITE);
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  const char* text = faceStatusText();
  int16_t x1, y1;
  uint16_t tw, th;
  display.getTextBounds(text, 0, STATUS_Y, &x1, &y1, &tw, &th);
  display.setCursor((128 - tw) / 2, STATUS_Y);
  display.print(text);
}

// ─────────────────────────────────────────────────────────────────────────────
// Global animation tick (call once per renderFace call)
// ─────────────────────────────────────────────────────────────────────────────

static void tickAnimations(float dt) {
  unsigned long now = millis();

  // ── Breathing ──────────────────────────────────────────────────────────────
  s_breathPhase += dt * 1.8f;
  if (s_breathPhase > 6.2832f) s_breathPhase -= 6.2832f;

  // ── Speak wave phase ───────────────────────────────────────────────────────
  s_speakWavePhase += dt * 14.0f;
  if (s_speakWavePhase > 62.832f) s_speakWavePhase -= 62.832f;

  // ── Blink state machine ────────────────────────────────────────────────────
  if (!s_isBlinking) {
    if (now - s_lastBlink > s_blinkInterval) {
      s_isBlinking   = true;
      s_blinkStartMs = now;
    }
  } else {
    unsigned long elapsed = now - s_blinkStartMs;
    if (elapsed < 70) {
      s_blinkWeight = 1.0f - (elapsed / 70.0f);       // closing
    } else if (elapsed < 140) {
      s_blinkWeight = (elapsed - 70) / 70.0f;         // opening
    } else {
      s_isBlinking      = false;
      s_blinkWeight     = 1.0f;
      s_lastBlink       = now;
      // Randomise next blink interval between 2.5s and 6s
      s_blinkInterval = 2500 + (unsigned long)(random(0, 3500));
    }
  }

  // ── Listening smooth eye scan ──────────────────────────────────────────────
  // Softly oscillates eyes left / right
  float scanTarget = sin(now * 0.0008f) * 5.0f;
  s_listenScanX += (scanTarget - s_listenScanX) * 0.08f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Mouth weight driving (call inside renderFace)
// ─────────────────────────────────────────────────────────────────────────────

static void driveMouthWeight(FaceMode mode, float dt) {
  float target = 0.0f;
  if (mode == FACE_SPEAKING) {
    // Synthesise a phoneme-like LFO: fast + slow modulation layers
    unsigned long now = millis();
    float fast  = sin(now * 0.022f);          // ~14 Hz jaw
    float slow  = sin(now * 0.0055f);         // ~3.5 Hz syllable envelope
    float combo = (fast * 0.55f + slow * 0.45f) * 0.5f + 0.5f;
    // Add a brief silence pocket every ~1.8s to sound like natural breath pause
    float breath = sin(now * 0.00055f);
    if (breath < -0.65f) combo *= 0.2f;       // near-silence pocket
    target = combo;
  } else if (mode == FACE_RECORDING || mode == FACE_LISTENING) {
    target = 0.15f + sin(millis() * 0.003f) * 0.05f; // tiny mouth bobble
  } else if (mode == FACE_HAPPY) {
    target = 1.0f;
  } else if (mode == FACE_ERROR || mode == FACE_SAD) {
    target = 0.0f;
  }

  // Smooth approach — fast close, slightly slower open
  float rate = (target > s_mouthWeight) ? 8.0f : 12.0f;
  s_mouthWeight += (target - s_mouthWeight) * dt * rate;
  if (s_mouthWeight < 0.0f) s_mouthWeight = 0.0f;
  if (s_mouthWeight > 1.0f) s_mouthWeight = 1.0f;
}

// ─────────────────────────────────────────────────────────────────────────────
// Individual face drawers
// ─────────────────────────────────────────────────────────────────────────────

// ── IDLE ────────────────────────────────────────────────────────────────────
static void drawFaceIdle() {
    int bob = (int)(sin(s_breathPhase) * 2.0f);

    // Blink amount: 1.0 = open, 0.0 = fully closed
    float eyeOpen = 1.0f - s_blinkWeight;

    // Ease the blink so it feels natural
    eyeOpen = eyeOpen * eyeOpen * (3.0f - 2.0f * eyeOpen);

    // Draw eyes with eyelid effect
    drawRealisticEye(EYE_L_X, EYE_Y + bob, EYE_RADIUS, eyeOpen);
    drawRealisticEye(EYE_R_X, EYE_Y + bob, EYE_RADIUS, eyeOpen);

    // Optional eyebrows
    int browOffset = (int)(s_blinkWeight * 2);
    drawEyebrow(EYE_L_X, EYE_Y - 12 + bob + browOffset);
    drawEyebrow(EYE_R_X, EYE_Y - 12 + bob + browOffset);

    // Subtle smile breathing animation
    int smileLift = (int)(sin(s_breathPhase) * 1.0f);
    drawSmoothSmile(MOUTH_CX, MOUTH_Y + bob - smileLift, 16, 4);
}

// ── HAPPY ────────────────────────────────────────────────────────────────────
static void drawFaceHappy() {
  int bob = (int)(sin(s_breathPhase) * 1.5f);

  drawHappyEye(EYE_L_X, EYE_Y + bob, EYE_RADIUS);
  drawHappyEye(EYE_R_X, EYE_Y + bob, EYE_RADIUS);

  // Smiley mouth
  int mouthH = 3 + (int)(s_mouthWeight * 6);
  drawOpenMouth(MOUTH_CX, MOUTH_Y + bob, 16, mouthH);
}

// ── SAD ──────────────────────────────────────────────────────────────────────
static void drawFaceSad() {
  int bob = (int)(sin(s_breathPhase) * 1.5f);

  drawSadEye(EYE_L_X, EYE_Y + bob, EYE_RADIUS, true);
  drawSadEye(EYE_R_X, EYE_Y + bob, EYE_RADIUS, false);

  // Frown mouth
  drawFrownMouth(MOUTH_CX, MOUTH_Y + bob);
}

// ── LISTENING ────────────────────────────────────────────────────────────────
static void drawFaceListening() {
  int bob = (int)(sin(s_breathPhase) * 1.2f);
  int scan = (int)s_listenScanX;

  // Eyes with soft horizontal scan offset
  drawFilledEye(EYE_L_X + scan, EYE_Y + bob, EYE_RADIUS, 1.0f - s_blinkWeight);
  drawFilledEye(EYE_R_X + scan, EYE_Y + bob, EYE_RADIUS, 1.0f - s_blinkWeight);

  // Attentive slightly open mouth
  int mouthH = 4 + (int)(s_mouthWeight * 6);
  display.fillRoundRect(MOUTH_CX - 12, MOUTH_Y + bob - mouthH / 2,
                        24, mouthH + 2, 3, SSD1306_WHITE);
  // Top erase for crescent look
  display.fillRect(MOUTH_CX - 13, MOUTH_Y + bob - mouthH / 2 - 1,
                   26, (mouthH + 2) / 3, SSD1306_BLACK);

  // Sound wave ripples — animated rings
  unsigned long now = millis();
  int rings = (now / 300) % 4; // 0,1,2,3 rings cycling
  if (rings > 0) drawSoundWaves(-1, rings);
  if (rings > 0) drawSoundWaves( 1, rings);
}


// ── THINKING / PROCESSING ────────────────────────────────────────────────────
static void drawFaceProcessing() {
  int bob = (int)(sin(s_breathPhase) * 1.0f);

  // Eyes look up-left (thinking gaze)
  int gazeX = -4, gazeY = -3;
  drawFilledEye(EYE_L_X + gazeX, EYE_Y + gazeY + bob, EYE_RADIUS,     1.0f - s_blinkWeight);
  drawFilledEye(EYE_R_X + gazeX, EYE_Y + gazeY + bob, EYE_RADIUS - 1, 1.0f - s_blinkWeight);

  // Flat smirk mouth
  drawFlatMouth(MOUTH_CX, MOUTH_Y + bob);

  // Floating animated "..." dots at top
  unsigned long now = millis();
  int dotPhase = (now / 350) % 4;  // 0,1,2,3 — light up dots one by one
  display.setTextSize(2);
  display.setTextColor(SSD1306_WHITE);
  for (int d = 0; d < 3; d++) {
    int dotX = 46 + d * 16;
    int dotY = 2 + (int)(sin(now * 0.003f + d * 1.1f) * 2.0f);
    if (d < dotPhase) {
      display.setCursor(dotX, dotY);
      display.print(".");
    }
  }
  display.setTextSize(1);
}

// ── SPEAKING ─────────────────────────────────────────────────────────────────
static void drawFaceSpeaking() {
  int bob = (int)(sin(s_breathPhase) * 1.0f);

  drawFilledEye(EYE_L_X, EYE_Y + bob, EYE_RADIUS, 1.0f - s_blinkWeight);
  drawFilledEye(EYE_R_X, EYE_Y + bob, EYE_RADIUS, 1.0f - s_blinkWeight);

  // Organic speaking jaw
  drawSpeakMouth(MOUTH_CX, MOUTH_Y + bob, s_mouthWeight, s_speakWavePhase);

  // Shimmer energy lines
  drawShimmerLines(MOUTH_CX, MOUTH_Y + bob, s_speakWavePhase);
}


// ── ERROR ────────────────────────────────────────────────────────────────────
static void drawFaceError() {
  int shake = (faceFrame % 3 == 0) ? random(-2, 3) : 0;

  drawAngryEye(EYE_L_X + shake, EYE_Y, EYE_RADIUS, true);
  drawAngryEye(EYE_R_X - shake, EYE_Y, EYE_RADIUS, false);

  // Frown mouth
  drawFrownMouth(MOUTH_CX, MOUTH_Y);

  // Broken WiFi icon (top centre)
  display.drawCircle(64, 10, 9,  SSD1306_WHITE);
  display.drawCircle(64, 10, 5,  SSD1306_WHITE);
  display.drawFastVLine(64, 6,   8, SSD1306_BLACK); // Erase bottom
  // Cross through icon
  display.drawLine(56, 2, 72, 18, SSD1306_WHITE);
  display.drawLine(72, 2, 56, 18, SSD1306_WHITE);
}

// ── SLEEP ────────────────────────────────────────────────────────────────────
static void drawDigitalClock() {
  time_t nowTime;
  time(&nowTime);
  struct tm timeinfo;
  memset(&timeinfo, 0, sizeof(struct tm));

  char timeStr[32] = "00:00:00";
  char dateStr[64] = "Syncing time...";
  
  if (localtime_r(&nowTime, &timeinfo) != nullptr && timeinfo.tm_year > (1970 - 1900)) {
    if ((millis() / 500) % 2 == 0) {
      strftime(timeStr, sizeof(timeStr), "%H:%M:%S", &timeinfo);
    } else {
      strftime(timeStr, sizeof(timeStr), "%H %M %S", &timeinfo);
    }
    strftime(dateStr, sizeof(dateStr), "%b %d, %Y", &timeinfo);
  } else {
    unsigned long secs = millis() / 1000;
    int h = (secs / 3600) % 24;
    int m = (secs / 60) % 60;
    int s = secs % 60;
    if ((millis() / 500) % 2 == 0) {
      snprintf(timeStr, sizeof(timeStr), "%02d:%02d:%02d", h, m, s);
    } else {
      snprintf(timeStr, sizeof(timeStr), "%02d %02d %02d", h, m, s);
    }
  }
  
  display.setTextSize(2);
  display.setTextColor(SSD1306_WHITE);
  int16_t x1 = 0, y1 = 0;
  uint16_t tw = 0, th = 0;
  display.getTextBounds(timeStr, 0, 0, &x1, &y1, &tw, &th);
  display.setCursor((128 - (int)tw) / 2, 2);
  display.print(timeStr);
  
  display.setTextSize(1);
  display.getTextBounds(dateStr, 0, 0, &x1, &y1, &tw, &th);
  display.setCursor((128 - (int)tw) / 2, 20);
  display.print(dateStr);
}

static void drawFaceSleep() {
  drawDigitalClock();
  
  int bob = (int)(sin(s_breathPhase) * 0.8f);
  int eyeY = 40 + bob;
  int mouthY = 46 + bob;
  
  display.drawFastHLine(EYE_L_X - 4, eyeY, 9, SSD1306_WHITE);
  display.drawFastHLine(EYE_R_X - 4, eyeY, 9, SSD1306_WHITE);
  
  display.drawCircle(MOUTH_CX, mouthY, 2, SSD1306_WHITE);
  
  unsigned long now = millis();
  for (int i = 0; i < 3; i++) {
    float phase = (float)((now + i * 800) % 2400) / 2400.0f;
    int zX = EYE_R_X + 12 + (int)(sin(phase * 6.28f) * 3.0f);
    int zY = mouthY - 4 - (int)(phase * 20.0f);
    int zSize = 1;
    if (phase > 0.4f) zSize = 2;
    if (zY > 28) {
      display.setTextSize(zSize);
      display.setTextColor(SSD1306_WHITE);
      display.setCursor(zX, zY);
      display.print(phase > 0.6f ? "Z" : "z");
    }
  }
  display.setTextSize(1);
}

// ─────────────────────────────────────────────────────────────────────────────
// Core render / set / update — PUBLIC API
// ─────────────────────────────────────────────────────────────────────────────

// Dirty flag — set true whenever we draw a new frame; cleared after display.display()
static bool s_displayDirty = false;

void renderFace(bool force) {
  unsigned long now = millis();

  // ── Frame-rate cap ───────────────────────────────────────────────────────────
  // Throttled to 10 FPS (OLED_FRAME_MS = 100 ms) normally.
  unsigned int frameIntervalMs = OLED_FRAME_MS;
  if (currentFaceMode == FACE_SPEAKING || currentFaceMode == FACE_RECORDING) {
    frameIntervalMs = OLED_AUDIO_FRAME_MS;
  }

  if (!force && (now - lastFaceFrame) < frameIntervalMs) return;

  // Compute delta-time (clamped so a long first frame doesn't snap animations)
  float dt = (now - lastFaceFrame) / 1000.0f;
  if (dt <= 0.0f || dt > 0.25f) dt = (float)frameIntervalMs / 1000.0f;

  lastFaceFrame = now;
  if (++faceFrame > 10000) faceFrame = 0;

  // Update animation state
  tickAnimations(dt);
  driveMouthWeight(currentFaceMode, dt);

  display.clearDisplay();

  switch (currentFaceMode) {
    case FACE_IDLE:       drawFaceIdle();       break;
    case FACE_LISTENING:
    case FACE_RECORDING:  drawFaceListening();  break;
    case FACE_PROCESSING: drawFaceProcessing(); break;
    case FACE_SPEAKING:   drawFaceSpeaking();   break;
    case FACE_HAPPY:      drawFaceHappy();      break;
    case FACE_SAD:        drawFaceSad();        break;
    case FACE_ERROR:      drawFaceError();      break;
    case FACE_SLEEP:      drawFaceSleep();      break;
  }

  if (currentFaceMode != FACE_SLEEP) {
    drawStatusBar();
  }

  // ── Dirty flag ──
  s_displayDirty = true;
  safeDisplay();
  s_displayDirty = false;
}

void setFace(FaceMode mode) {
  if (currentFaceMode == mode) return;
  currentFaceMode = mode;
  faceFrame       = 0;
  lastFaceFrame   = 0;
  s_mouthWeight   = 0.0f;   // reset so new emotion opens from silence
  renderFace(true);          // force immediate redraw on mode change
}

void updateFace() {
  renderFace(false);
}

// ─────────────────────────────────────────────────────────────────────────────
// OLED utility helpers — startup / info screens
// ─────────────────────────────────────────────────────────────────────────────

// showOLED — const char* overload avoids heap String allocation on every call.
// The String overload below is kept for backward compatibility with callers
// that already pass String arguments.
void showOLED(const char* title, const char* line1, const char* line2) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);  display.print(title);
  display.drawFastHLine(0, 12, 128, SSD1306_WHITE);
  display.setCursor(0, 18); display.print(line1);
  display.setCursor(0, 34); display.print(line2);
  safeDisplay();
}

void displayResponse(const char* text) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);  display.print("Aether says:");
  display.drawFastHLine(0, 12, 128, SSD1306_WHITE);
  int y = 16;
  const int maxChars = 21;
  int len = strlen(text);
  char lineBuf[maxChars + 1];
  for (int i = 0; i < len; i += maxChars) {
    if (y > 56) break;
    int n = len - i;
    if (n > maxChars) n = maxChars;
    strncpy(lineBuf, text + i, n);
    lineBuf[n] = '\0';
    display.setCursor(0, y);
    display.print(lineBuf);
    y += 10;
  }
  safeDisplay();
}

void applyEmotionToFace(const char* emotion) {
  if (!emotion || strlen(emotion) == 0) return;
  Serial.printf("[Emotion] Applying emotion to face: %s\n", emotion);
  if (strcmp(emotion, "happy") == 0 || strcmp(emotion, "excited") == 0 || strcmp(emotion, "motivated") == 0) {
    setFace(FACE_HAPPY);
  } else if (strcmp(emotion, "confused") == 0) {
    setFace(FACE_PROCESSING);
  } else if (strcmp(emotion, "anxious") == 0 || strcmp(emotion, "frustrated") == 0 || strcmp(emotion, "stressed") == 0) {
    setFace(FACE_ERROR);
  } else if (strcmp(emotion, "sad") == 0) {
    setFace(FACE_SAD);
  } else if (strcmp(emotion, "calm") == 0 || strcmp(emotion, "neutral") == 0) {
    setFace(FACE_IDLE);
  }
}

#endif // FACE_H
