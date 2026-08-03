/*
 * WiFiHelper.h — Dynamic Wi-Fi provisioning + mDNS server discovery
 *
 * Responsibilities:
 *   1. WiFiManager captive portal for first-boot credential setup.
 *   2. Persistent storage of all settings via ESP32 Preferences (NVS).
 *   3. mDNS lookup of aether.local to discover the FastAPI server IP.
 *   4. Fallback to a manually stored IP if mDNS fails.
 *   5. checkServerHealth() to verify server reachability before voice sessions.
 *   6. ensureWiFi() reconnection guard called on every loop iteration.
 *
 * Required Libraries:
 *   - WiFiManager by tzapu (install via Arduino Library Manager)
 *   - ESPmDNS (built into ESP32 Arduino core)
 *   - Preferences (built into ESP32 Arduino core)
 */

#ifndef WIFI_HELPER_H
#define WIFI_HELPER_H

#include "Declarations.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#define WM_NODEBUG        // Disables WiFiManager internal debug prints to save flash space
#include <WiFiManager.h>  // tzapu/WiFiManager — install via Library Manager
#include "nvs_flash.h"    // For nvs_flash_erase() — full NVS wipe on factory reset

// ─────────────────────────────────────────────────────────────────────────
// Preferences namespace
// ─────────────────────────────────────────────────────────────────────────
static Preferences _prefs;

// ─────────────────────────────────────────────────────────────────────────
// Preferences helpers
// ─────────────────────────────────────────────────────────────────────────

static void savePreferences(const String& name, const String& fbIP, uint16_t port) {
  _prefs.begin("aether-cfg", false);
  _prefs.putString("assistantName", name);
  _prefs.putString("fallbackIP",    fbIP);
  _prefs.putUShort("serverPort",    port);
  _prefs.end();
  Serial.printf("[PREFS] Saved — name=%s  fbIP=%s  port=%u\n",
                name.c_str(), fbIP.c_str(), port);
}

static void loadPreferences() {
  _prefs.begin("aether-cfg", true);

  // Only overwrite each field if NVS actually has a non-empty value saved.
  // If the user has never opened the portal, NVS returns "", which would
  // destroy the compile-time SERVER_HOST default set in firmware.ino.
  String storedName = _prefs.getString("assistantName", "");
  String storedIP   = _prefs.getString("fallbackIP",    "");
  uint16_t storedPort = _prefs.getUShort("serverPort",  0);

  if (storedName.length() > 0)  assistantName        = storedName;
  if (storedIP.length()   > 0)  fallbackServerIP     = storedIP;
  if (storedPort          > 0)  discoveredServerPort  = storedPort;

  _prefs.end();
  Serial.printf("[PREFS] Loaded — name=%s  fbIP=%s  port=%u\n",
                assistantName.c_str(), fallbackServerIP.c_str(), discoveredServerPort);
}


// ─────────────────────────────────────────────────────────────────────────
// Factory reset — clears WiFi credentials + all preferences
// Uses nvs_flash_erase() to wipe the ENTIRE NVS partition so that
// WiFiManager credentials, app settings, and fallback IP are all cleared.
// ─────────────────────────────────────────────────────────────────────────

void factoryReset() {
  showOLED("RESETTING...", "CLEARING NVS", "Please wait");
  Serial.println("[RESET] Factory reset triggered — wiping entire NVS partition");

  // Close any open Preferences handle first to avoid corruption
  _prefs.end();

  // Wipe the entire NVS flash partition (covers aether-cfg AND WiFiManager creds)
  esp_err_t err = nvs_flash_erase();
  if (err == ESP_OK) {
    Serial.println("[RESET] NVS erased successfully");
  } else {
    Serial.printf("[RESET] NVS erase error: %s — falling back to namespace clear\n", esp_err_to_name(err));
    // Fallback: clear just our namespace + WiFiManager namespace individually
    _prefs.begin("aether-cfg", false);
    _prefs.clear();
    _prefs.end();
    WiFiManager wm;
    wm.resetSettings();
  }

  showOLED("FACTORY RESET", "Done!", "Rebooting...");
  delay(1500);
  ESP.restart();
}

// ─────────────────────────────────────────────────────────────────────────
// WiFiManager provisioning with custom portal fields
// ─────────────────────────────────────────────────────────────────────────

void connectWiFi() {
#ifdef BYPASS_WIFIMANAGER
  loadPreferences();

  showOLED("CONNECTING...", ssid, "Please wait");
  Serial.printf("[WIFI] Bypassing WiFiManager. Connecting to SSID: %s\n", ssid);

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WIFI] Connected: %s\n", WiFi.localIP().toString().c_str());
    showOLED("CONNECTED", WiFi.localIP().toString().c_str(), "");
    delay(1500);
  } else {
    Serial.println("\n[WIFI] Connection failed!");
    showOLED("CONN FAILED", "Check Config.h", "Rebooting...");
    delay(3000);
    ESP.restart();
  }
#else
  // Load any previously saved app preferences first
  loadPreferences();

  WiFiManager wm;

  // ── Custom portal parameters ───────────────────────────────────────────
  char nameBuf[32];
  char fbIPBuf[20];
  char portBuf[8];
  strncpy(nameBuf, assistantName.c_str(),         sizeof(nameBuf) - 1);
  strncpy(fbIPBuf, fallbackServerIP.c_str(),      sizeof(fbIPBuf) - 1);
  snprintf(portBuf, sizeof(portBuf), "%u",        (unsigned)discoveredServerPort);

  WiFiManagerParameter paramName("aname",  "Assistant Name",    nameBuf, 30);
  WiFiManagerParameter paramFBIP("fbip",   "Fallback Server IP (optional)", fbIPBuf, 16);
  WiFiManagerParameter paramPort("port",   "Server Port",       portBuf,  6);

  wm.addParameter(&paramName);
  wm.addParameter(&paramFBIP);
  wm.addParameter(&paramPort);

  // ── Show SETUP MODE only when the portal actually opens ───────────────
  // Devices with stored credentials skip this callback entirely.
  wm.setAPCallback([](WiFiManager*) {
    showOLED("SETUP MODE", "Connect To:", WIFI_AP_NAME);
    Serial.println("[WIFI] Portal started — SSID: " WIFI_AP_NAME);
  });

  wm.setSaveConfigCallback([]() {
    showOLED("SAVING CONFIG", "Connecting...", "");
    Serial.println("[WIFI] Web portal config saved, attempting connection...");
  });

  // ── Portal behaviour ───────────────────────────────────────────────────
  wm.setConfigPortalTimeout(0);
  wm.setConnectTimeout(12); // Limit connection attempts to 12s so it doesn't hang indefinitely if network is unreachable
  // wm.setBreakAfterConfig(true); // Disable so that portal stays open on connection failure, allowing user to retry credentials
  wm.setAPStaticIPConfig(
    IPAddress(192, 168, 4, 1),
    IPAddress(192, 168, 4, 1),
    IPAddress(255, 255, 255, 0)
  );

  bool connected = wm.autoConnect(WIFI_AP_NAME, WIFI_AP_PASSWORD);

  if (connected) {
    Serial.printf("[WIFI] Connected: %s\n", WiFi.localIP().toString().c_str());

    String newName = String(paramName.getValue());
    String newFBIP = String(paramFBIP.getValue());
    uint16_t newPort = (uint16_t)atoi(paramPort.getValue());
    if (newPort == 0) newPort = DEFAULT_PORT;
    if (newName.length() == 0) newName = "Aether";

    savePreferences(newName, newFBIP, newPort);
    assistantName        = newName;
    fallbackServerIP     = newFBIP;
    discoveredServerPort = newPort;
    // No delay — proceed immediately to server discovery
  } else {
    showOLED("WIFI ERROR", "Check settings", "Rebooting...");
    Serial.println("[WIFI] Connection failed — rebooting");
    delay(2000);
    ESP.restart();
  }
#endif
}

// ─────────────────────────────────────────────────────────────────────────
// Ensure Wi-Fi is still connected (call in loop)
// ─────────────────────────────────────────────────────────────────────────

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  // Gate the reconnect attempts to avoid spamming WiFi.reconnect()
  static unsigned long lastReconnectAttempt = 0;
  unsigned long now = millis();
  if (now - lastReconnectAttempt < WIFI_CHECK_INTERVAL_MS) {
    return;
  }
  lastReconnectAttempt = now;

  Serial.println("[WIFI] Connection lost — attempting reconnect...");
  serverDiscovered = false;   // Force server re-discovery after reconnect

  WiFi.disconnect(false);
  WiFi.reconnect();

  // Wait briefly (up to 3 seconds) for a connection, yielding to FreeRTOS
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 3000) {
    delay(100);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WIFI] Reconnected: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\n[WIFI] Reconnect failed — will retry later. No reboot.");
  }
}

// ─────────────────────────────────────────────────────────────────────────
// mDNS lookup: resolve aether.local → IP
// Returns true and sets discoveredServerIP on success.
// ─────────────────────────────────────────────────────────────────────────

static bool mdnsLookup() {
  // Silent lookup — no showOLED here; caller shows final result.
  Serial.println("[mDNS] Starting lookup for aether.local");

  if (!MDNS.begin("esp32-aether")) {
    Serial.println("[mDNS] Failed to initialise mDNS responder");
    return false;
  }

  // Try service-based lookup first (_http._tcp)
  int n = MDNS.queryService(MDNS_SERVICE, MDNS_PROTOCOL);
  if (n > 0) {
    for (int i = 0; i < n; i++) {
      String hostname = MDNS.hostname(i);
      hostname.toLowerCase();
      if (hostname.startsWith(MDNS_HOST)) {
        IPAddress ip = MDNS.address(i);
        uint16_t  port = MDNS.port(i);
        discoveredServerIP   = ip;
        if (port > 0) discoveredServerPort = port;
        Serial.printf("[mDNS] Found via service: %s → %s:%u\n",
                      MDNS_HOST, ip.toString().c_str(), discoveredServerPort);
        MDNS.end();
        return true;
      }
    }
  }

  // Fallback: direct hostname resolution
  IPAddress resolvedIP;
  if (WiFi.hostByName((String(MDNS_HOST) + ".local").c_str(), resolvedIP) == 1) {
    discoveredServerIP = resolvedIP;
    Serial.printf("[mDNS] Resolved hostname: aether.local → %s\n",
                  resolvedIP.toString().c_str());
    MDNS.end();
    return true;
  }

  MDNS.end();
  Serial.println("[mDNS] Lookup failed");
  return false;
}

// ─────────────────────────────────────────────────────────────────────────
// Main server discovery — mDNS first, then fallback IP
// Sets serverDiscovered = true on success.
// ─────────────────────────────────────────────────────────────────────────

void locateServer() {
  serverDiscovered = false;

  // ── Attempt mDNS first (picks up server IP changes automatically) ─────
  // mDNS lookup is tried first so that if the server IP has changed,
  // the new IP is discovered rather than using a stale cached value.
  if (mdnsLookup()) {
    Serial.printf("[SERVER] Discovered via mDNS: %s:%u\n",
                  discoveredServerIP.toString().c_str(), discoveredServerPort);
    serverDiscovered = true;
    return;
  }

  // ── Fallback: use manually stored IP from portal / Config.h ──────────
  // Used when mDNS is unavailable (different subnet, mDNS blocked, etc.).
  // fallbackServerIP is populated from:
  //   (a) NVS Preferences set via the WiFiManager portal, OR
  //   (b) SERVER_HOST constant in Config.h (set in alpha.ino as the initial value).
  if (fallbackServerIP.length() > 0) {
    Serial.printf("[SERVER] mDNS failed — using fallback IP: %s:%u\n",
                  fallbackServerIP.c_str(), discoveredServerPort);
    discoveredServerIP.fromString(fallbackServerIP);
    serverDiscovered = true;
    return;
  }

  Serial.println("[SERVER] Discovery failed — set a fallback IP in the setup portal");
}

// ─────────────────────────────────────────────────────────────────────────
// HTTP health check against the currently discovered server
// Returns true if the server responds with HTTP 200.
// ─────────────────────────────────────────────────────────────────────────

bool checkServerHealth() {
  if (!serverDiscovered) return false;

  ensureWiFi();

  String url = "http://" + discoveredServerIP.toString()
               + ":" + String(discoveredServerPort) + "/health";

  HTTPClient http;
  http.begin(url);
  http.setTimeout(5000);

  int code = http.GET();
  http.end();

  Serial.printf("[HEALTH] %s → %d\n", url.c_str(), code);
  return (code == 200);
}

#endif // WIFI_HELPER_H
