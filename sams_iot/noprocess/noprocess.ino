// ================================================================
// S.A.M.S. - ESP32-C3 SuperMini (Audio Capture Only - No Processing)
// ================================================================

#include <Arduino.h>
#include <ArduinoJson.h>
#include <driver/i2s.h>
#include <WiFi.h> 
#include <WiFiClientSecure.h>
#include "esp_wifi.h" 
#include "esp_bt.h" 

#include "secrets.h"   // WiFi + backend + Supabase credentials (gitignored)

// ── WiFi Configuration ──────────────────────────────────────────────────
const char* wifi_ssid = WIFI_SSID;
const char* wifi_password = WIFI_PASSWORD;
WiFiClient espClient;
bool ledwifi_state = false; 

// ── Backend server (FastAPI) ─────────────────────────────────────────
#define HEARTBEAT_ENDPOINT "/api/devices/heartbeat"
#define HEARTBEAT_INTERVAL_MS   60000UL   // 60s 

// ── Supabase Storage (HTTPS) ──────────────────────────────────────────────
// SUPABASE_HOST / SUPABASE_KEY come from secrets.h
#define SUPABASE_BUCKET  "audio-clips"

// ── Device identity ─────────────────────────────────────────────────────────
#define DEVICE_ID        "esp32-001"
#define LOCATION_ID      "loc-toilet-a"

// ── HTTP upload settings ─────────────────────────────────────────────────────
#define HTTP_TIMEOUT_MS  60000
#define BOUNDARY         "----SAMSBoundary7MA4YWxkTrZu0gW"
#define TZ_OFFSET_SEC    (8 * 3600)

// ── WAV Header ──────────────────────────────────────────────────────────────
#define WAV_HEADER_SIZE  44

// Pin Configuration
#define SOUND_SENSOR_PIN 1
#define SOUND_THRESHOLD 3500
#define ALERT_LED_PIN 20
#define I2S_WS_PIN   5
#define I2S_SCK_PIN  4
#define I2S_SD_PIN   6

// Audio Settings
#define SAMPLE_RATE 16000
#define RECORD_SECONDS 8
#define CHUNK_SAMPLES 256

// ════════════════════════════════════════════════════════════════════════════
// Globals
// ════════════════════════════════════════════════════════════════════════════
unsigned long lastHeartbeatTime = 0;

unsigned long lastTriggerTime = 0;
const unsigned long triggerCooldownMs = 40000;  // for 8s clips
// const unsigned long triggerCooldownMs = 15000;  // for 8s clips
bool isLedAlertActive = false;

// Small stack buffers only — no large heap allocation needed
int32_t  rawSamples[CHUNK_SAMPLES];   // I2S raw 32-bit samples  (~1KB)
int16_t  pcmChunk[CHUNK_SAMPLES];     // converted 16-bit samples (~512B)

// ════════════════════════════════════════════════════════════════════════════
// UUID Generator (matches your bucket format)
// ════════════════════════════════════════════════════════════════════════════
void generateUUID(char* out, size_t len) {
    if (len < 37) return; // Need at least 37 chars for UUID (36 + null)
    
    uint32_t r1 = esp_random();
    uint32_t r2 = esp_random();
    uint32_t r3 = esp_random();
    uint32_t r4 = esp_random();
    
    // Format: 8-4-4-4-12 (standard UUID format)
    // Example: 1ea33c87-35db-40a3-903e-d1e512fe5c4a
    snprintf(out, len, "%08x-%04x-%04x-%04x-%08x%04x",
             r1, 
             (r2 >> 16) & 0xFFFF,
             r2 & 0xFFFF,
             (r3 >> 16) & 0xFFFF,
             r3 & 0xFFFFFFFF,
             r4 & 0xFFFF);
}

// ════════════════════════════════════════════════════════════════════════════
// WAV Header Builder
// ════════════════════════════════════════════════════════════════════════════
void buildWavHeader(uint8_t* header, uint32_t numSamples) {
    uint32_t dataBytes   = numSamples * 2;
    uint32_t fileSize    = dataBytes + WAV_HEADER_SIZE - 8;
    uint32_t sampleRate  = SAMPLE_RATE;
    uint32_t byteRate    = sampleRate * 2;
    uint16_t blockAlign  = 2;
    uint16_t bitsPerSamp = 16;
    uint16_t numChannels = 1;
    uint16_t audioFmt    = 1;
    uint32_t fmtSize     = 16;

    memcpy(header,      "RIFF", 4);
    memcpy(header + 4,  &fileSize,    4);
    memcpy(header + 8,  "WAVE", 4);
    memcpy(header + 12, "fmt ", 4);
    memcpy(header + 16, &fmtSize,     4);
    memcpy(header + 20, &audioFmt,    2);
    memcpy(header + 22, &numChannels, 2);
    memcpy(header + 24, &sampleRate,  4);
    memcpy(header + 28, &byteRate,    4);
    memcpy(header + 32, &blockAlign,  2);
    memcpy(header + 34, &bitsPerSamp, 2);
    memcpy(header + 36, "data", 4);
    memcpy(header + 40, &dataBytes,   4);
}

// ── Function Declarations ──────────────────────────────────────────────────
void setupI2S();
void setupWiFi();
void setupNTP();
void initiateHeartbeat();
void getISO8601Timestamp(char* buf, size_t len);
void getCompactTimestamp(char* buf, size_t len);
bool streamRecordToSupabase(const char* filename, int soundLevel, const char* timestamp);
bool sendHeartbeat(uint16_t timeoutMs = 1500);
void triggerSoundDetectedAlert();
void triggerSolidLedAlert();

// ════════════════════════════════════════════════════════════════════════════
// Setup
// ════════════════════════════════════════════════════════════════════════════
void setup() {
    esp_bt_controller_disable();

    Serial.begin(115200);
    delay(1000);
    
    Serial.println();
    Serial.println(F("======================================"));
    Serial.println(F("   S.A.M.S. - ESP32-C3 SUPERMINI      "));
    Serial.println(F("======================================"));

    WiFi.disconnect(true);
    delay(1000);
    WiFi.mode(WIFI_STA);
    delay(1000);

    esp_wifi_set_max_tx_power(WIFI_POWER_8_5dBm);  // Reduce from 19.5dBm to 8.5dBm 
    // esp_wifi_set_max_tx_power(WIFI_POWER_10dBm);


    setupWiFi();
    setupNTP();
    setupI2S();
    initiateHeartbeat(); 

    pinMode(SOUND_SENSOR_PIN, INPUT);
    pinMode(ALERT_LED_PIN, OUTPUT);
    digitalWrite(ALERT_LED_PIN, LOW);

    // // Allocate the audio buffer once at startup
    // // 8s * 16000 = 128,000 samples * 2 bytes = 256 KB

    Serial.printf("Free heap: %u bytes\n", ESP.getFreeHeap());
    Serial.println(F("✅ System ready!"));
    Serial.println(F("📡 Waiting for sound trigger..."));
}

// ════════════════════════════════════════════════════════════════════════════
// Loop
// ════════════════════════════════════════════════════════════════════════════
void loop() {
    // ── WiFi status LED ──────────────────────────────────────────────────
    if (WiFi.status() != WL_CONNECTED) {
        digitalWrite(ALERT_LED_PIN, HIGH); delay(80);
        digitalWrite(ALERT_LED_PIN, LOW);  delay(80);
        digitalWrite(ALERT_LED_PIN, HIGH); delay(80);
        digitalWrite(ALERT_LED_PIN, LOW);
    } else {
        if (!isLedAlertActive) {
            digitalWrite(ALERT_LED_PIN, LOW);
        }
    }

    int soundLevel = analogRead(SOUND_SENSOR_PIN);

    static int counter = 0;
    if (++counter >= 20) {
        counter = 0;
        Serial.print(F("Sound level: "));
        Serial.println(soundLevel);
    }

    bool thresholdExceeded = soundLevel >= SOUND_THRESHOLD;
    bool cooldownPassed = millis() - lastTriggerTime >= triggerCooldownMs;

    if (thresholdExceeded && cooldownPassed) {
        lastTriggerTime = millis();

        Serial.println();
        Serial.print(F("🔊 Sound level "));
        Serial.print(soundLevel);
        Serial.print(F(" exceeded threshold ("));
        Serial.print(SOUND_THRESHOLD);
        Serial.println(F(")"));

        triggerSoundDetectedAlert();

        // ── Get timestamp before recording (still used inside filename below) ──
        char timestamp[30];
        getISO8601Timestamp(timestamp, sizeof(timestamp));

        // ── NEW: filename IS the metadata — no separate .json upload needed ────
        // Format: {device_id}_{location_id}_{sound_level}_{compact_timestamp}_{short_id}.wav
        // Example: esp32-001_loc-toilet-a_3421_20260909T153045_a1b2.wav
        char compactTs[20];
        getCompactTimestamp(compactTs, sizeof(compactTs));

        uint16_t shortId = (uint16_t)(esp_random() & 0xFFFF); 

        char filename[128];
        snprintf(filename, sizeof(filename), "%s_%s_%d_%s_%04x.wav",
                 DEVICE_ID, LOCATION_ID, soundLevel, compactTs, shortId);

        Serial.println(F("🎙️ Recording + streaming 8s to Supabase..."));

        bool ok = streamRecordToSupabase(filename, soundLevel, timestamp);

        if (ok) {
            Serial.println(F("✅ Upload complete!"));

            // Begin cooldown only after the upload has finished
            lastTriggerTime = millis();

            Serial.println(F("⏳ 40-second cooldown started before accepting another sound event..."));
            Serial.println(F("   New sound events will be ignored during cooldown."));
        } else {
            Serial.println(F("❌ Upload failed!"));
        }

        Serial.println();
    }

    // ── heartbeat, checked LAST so it can never delay scream detection ──
    if (WiFi.status() == WL_CONNECTED &&
        millis() - lastHeartbeatTime >= HEARTBEAT_INTERVAL_MS) {
        lastHeartbeatTime = millis();
        sendHeartbeat(1500);
    }

    delay(50);
}

// ════════════════════════════════════════════════════════════════════════════
// Stream-record: record I2S audio and simultaneously stream it to Supabase
// over HTTPS using chunked transfer encoding.
// No large buffer needed — only CHUNK_SAMPLES * 4 bytes at a time in RAM.
// ════════════════════════════════════════════════════════════════════════════
bool streamRecordToSupabase(const char* filename, int soundLevel,
                            const char* timestamp) {

    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(HTTP_TIMEOUT_MS / 1000);

    if (!client.connect(SUPABASE_HOST, 443)) {
        Serial.println(F("[Supabase] HTTPS connect failed"));
        return false;
    }

    uint32_t totalSamples  = (uint32_t)SAMPLE_RATE * RECORD_SECONDS;
    uint32_t pcmBytes      = totalSamples * sizeof(int16_t);
    uint32_t totalWavBytes = WAV_HEADER_SIZE + pcmBytes;

    // Build Supabase REST path
    String path = "/storage/v1/object/";
    path += SUPABASE_BUCKET;
    path += "/";
    path += filename;

    Serial.printf("[Supabase] PUT %s (%u bytes)\n", path.c_str(), totalWavBytes);

    // ── Send HTTP headers ─────────────────────────────────────────────────
    client.print("PUT ");
    client.print(path);
    client.println(" HTTP/1.1");
    client.print("Host: ");
    client.println(SUPABASE_HOST);
    client.print("Authorization: Bearer ");
    client.println(SUPABASE_KEY);
    client.println("Content-Type: audio/wav");
    client.print("Content-Length: ");
    client.println(totalWavBytes);
    client.println("x-upsert: true");       // prevents hang on duplicate filename
    client.println("Connection: close");
    client.println("Accept: application/json");
    client.println();                       // end of headers

    // ── Send WAV header first ─────────────────────────────────────────────
    uint8_t wavHeader[WAV_HEADER_SIZE];
    buildWavHeader(wavHeader, totalSamples);
    client.write(wavHeader, WAV_HEADER_SIZE);

    // ── Record I2S and stream PCM chunks directly ─────────────────────────
    uint32_t samplesStreamed = 0;

    while (samplesStreamed < totalSamples) {
        size_t   bytesRead = 0;
        uint32_t toRead    = min((uint32_t)CHUNK_SAMPLES,
                                 totalSamples - samplesStreamed);

        esp_err_t result = i2s_read(
            I2S_NUM_0,
            rawSamples,
            toRead * sizeof(int32_t),
            &bytesRead,
            portMAX_DELAY
        );

        if (result != ESP_OK || bytesRead == 0) {
            Serial.println(F("[I2S] Read error during streaming"));
            client.stop();
            return false;
        }

        int n = bytesRead / sizeof(int32_t);

        // Convert 32-bit I2S samples → 16-bit PCM
        for (int i = 0; i < n; i++) {
            pcmChunk[i] = (int16_t)(rawSamples[i] >> 16);
        }

        // Stream directly to Supabase
        client.write((uint8_t*)pcmChunk, n * sizeof(int16_t));
        samplesStreamed += n;

        yield();  // keep WiFi stack alive during long transfer
    }

    Serial.printf("[Supabase] Streamed %u samples (%.1fs)\n",
                  samplesStreamed, (float)samplesStreamed / SAMPLE_RATE);

    // ── Read response ─────────────────────────────────────────────────────
    unsigned long deadline = millis() + 15000;
    while (!client.available() && client.connected() && millis() < deadline) {
        delay(10);
    }

    String statusLine = "";
    bool gotStatus = false;

    while (client.connected() && millis() < deadline) {
        if (client.available()) {
            delay(100);
            String line = client.readStringUntil('\n');
            line.trim();

            if (!gotStatus && line.indexOf("HTTP") >= 0) {
                statusLine = line;
                gotStatus  = true;
                Serial.print(F("[Supabase] "));
                Serial.println(statusLine);
                continue;
            }
            if (line.length() == 0) break;
        }
    }
    client.stop();
    delay(500);

    if (!gotStatus) {
        Serial.println(F("[Supabase] No response received"));
        return false;
    }

    bool success = statusLine.indexOf("200") > 0 ||
                   statusLine.indexOf("201") > 0;

    if (success) {
        Serial.println(F("✅ Upload complete — backend will process asynchronously"));
        triggerSolidLedAlert();
    } else {
        Serial.print(F("[Supabase] Upload failed: "));
        Serial.println(statusLine);
        // optionally: a distinct failure blink pattern here
    }

    return success;
}

// ════════════════════════════════════════════════════════════════════════════
// FR29 — device heartbeat: proves this device is alive even when it hasn't
// triggered a recording recently. Talks to YOUR FastAPI backend directly
// (plain HTTP, local network) — NOT Supabase.
// ════════════════════════════════════════════════════════════════════════════
bool sendHeartbeat(uint16_t timeoutMs) {
    WiFiClient client;
    client.setTimeout(timeoutMs);   // short — must never meaningfully stall the loop

    if (!client.connect(SERVER_IP, SERVER_PORT)) {
        Serial.println(F("[Heartbeat] Backend connect failed — will retry next interval"));
        return false;
    }

    String body = "device_id=";
    body += DEVICE_ID;

    client.print("POST ");
    client.print(HEARTBEAT_ENDPOINT);
    client.println(" HTTP/1.1");
    client.print("Host: ");
    client.print(SERVER_IP);
    client.print(":");
    client.println(SERVER_PORT);
    client.println("Content-Type: application/x-www-form-urlencoded");
    client.print("Content-Length: ");
    client.println(body.length());
    client.print("X-API-Key: ");
    client.println(DEVICE_API_KEY);   // define this in secrets.h
    client.println("Connection: close");
    client.println();
    client.print(body);
    client.flush();

    unsigned long deadline = millis() + timeoutMs;
    bool gotStatus = false;
    String statusLine = "";

    while (millis() < deadline && (client.connected() || client.available())) {
        if (client.available()) {
            String line = client.readStringUntil('\n');
            line.trim();
            if (!gotStatus && line.startsWith("HTTP/")) {
                statusLine = line;
                gotStatus = true;
                break; // Exit immediately once status line is received
            }
        }
        delay(10);
    }
    client.stop();

    bool ok = gotStatus && (statusLine.indexOf("200") >= 0);

    Serial.print(F("[Heartbeat] Status: "));
    if (gotStatus) {
        Serial.println(statusLine); // Will print exact code (e.g. 401, 404, 422, 429)
    } else {
        Serial.println(F("NO RESPONSE (Timeout)"));
    }
    return ok;
}

bool uploadMetadataToSupabase(const char* uuid, int soundLevel, const char* timestamp) {
    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(HTTP_TIMEOUT_MS / 1000);

    if (!client.connect(SUPABASE_HOST, 443)) {
        Serial.println(F("[Supabase] Metadata upload: HTTPS connect failed"));
        return false;
    }

    // Build JSON metadata
    String jsonBody = "{";
    jsonBody += "\"device_id\":\"" + String(DEVICE_ID) + "\",";
    jsonBody += "\"location_id\":\"" + String(LOCATION_ID) + "\",";
    jsonBody += "\"timestamp\":\"" + String(timestamp) + "\",";
    jsonBody += "\"sound_level\":" + String(soundLevel);
    jsonBody += "}";

    // Build path: /storage/v1/object/audio-clips/{uuid}.json
    String path = "/storage/v1/object/";
    path += SUPABASE_BUCKET;
    path += "/";
    path += String(uuid);
    path += ".json";

    Serial.printf("[Supabase] PUT metadata %s (%u bytes)\n", path.c_str(), jsonBody.length());

    // ── Send HTTP headers ─────────────────────────────────────────────────
    client.print("PUT ");
    client.print(path);
    client.println(" HTTP/1.1");
    client.print("Host: ");
    client.println(SUPABASE_HOST);
    client.print("Authorization: Bearer ");
    client.println(SUPABASE_KEY);
    client.println("Content-Type: application/json");
    client.print("Content-Length: ");
    client.println(jsonBody.length());
    client.println("x-upsert: true");
    client.println("Connection: close");
    client.println("Accept: application/json");
    client.println();
    client.print(jsonBody);

    // ── Read response ─────────────────────────────────────────────────────
    unsigned long deadline = millis() + 15000;
    while (!client.available() && client.connected() && millis() < deadline) {
        delay(10);
    }

    String statusLine = "";
    bool gotStatus = false;

    while (client.connected() && millis() < deadline) {
        if (client.available()) {
            delay(100);
            String line = client.readStringUntil('\n');
            line.trim();

            if (!gotStatus && line.indexOf("HTTP") >= 0) {
                statusLine = line;
                gotStatus = true;
                Serial.print(F("[Supabase] Metadata "));
                Serial.println(statusLine);
                continue;
            }
            if (line.length() == 0) break;
        }
    }
    client.stop();
    delay(500);

    if (!gotStatus) {
        Serial.println(F("[Supabase] Metadata upload: No response"));
        return false;
    }

    bool success = statusLine.indexOf("200") > 0 || statusLine.indexOf("201") > 0;
    if (success) {
        Serial.println(F("✅ Metadata upload complete"));
    } else {
        Serial.print(F("[Supabase] Metadata upload failed: "));
        Serial.println(statusLine);
    }

    return success;
}

// ════════════════════════════════════════════════════════════════════════════
// WiFi Setup
// ════════════════════════════════════════════════════════════════════════════
void setupWiFi() {
    delay(10);
    WiFi.begin(wifi_ssid, wifi_password);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 40) {
        Serial.print(".");
        delay(500);
        attempts++;
    }
    Serial.println();

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println(F("✅ WiFi connected!"));
        Serial.print("   IP: ");
        Serial.println(WiFi.localIP());
        Serial.print(F("   RSSI: "));
        Serial.print(WiFi.RSSI());
        Serial.println(F(" dBm"));
    } else {
        Serial.println(F("❌ WiFi connection FAILED!"));
    }
}

// ════════════════════════════════════════════════════════════════════════════
// NTP Setup
// ════════════════════════════════════════════════════════════════════════════
void setupNTP() {
    configTime(TZ_OFFSET_SEC, 0, "pool.ntp.org", "time.google.com");
    Serial.print("[NTP] Syncing time");
    struct tm timeinfo;
    uint8_t retries = 0;
    while (!getLocalTime(&timeinfo) && retries < 15) {
        delay(500);
        Serial.print(".");
        retries++;
    }
    if (retries < 15) {
        char buf[30];
        strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &timeinfo);
        Serial.print(" OK → ");
        Serial.println(buf);
    } else {
        Serial.println(F(" FAILED (using fallback)"));
    }
}

void initiateHeartbeat() {
    Serial.println(F("📡 Registering boot heartbeat with backend..."));
    int attempts = 0;
    while (!sendHeartbeat(5000)) {
        attempts++;
        Serial.printf("⚠️ [Boot] Heartbeat failed (Attempt %d). Retrying ...\n", attempts);
        delay(500);
    }
    Serial.println(F("✅ Boot heartbeat confirmed by backend!"));
}

void getISO8601Timestamp(char* buf, size_t len) {
    struct tm timeinfo;
    if (getLocalTime(&timeinfo)) {
        strftime(buf, len, "%Y-%m-%dT%H:%M:%S", &timeinfo);
    } else {
        snprintf(buf, len, "2026-06-23T00:00:00");
    }
}

// ════════════════════════════════════════════════════════════════════════════
// filename-safe timestamp (no colons), used for metadata-in-filename
// ════════════════════════════════════════════════════════════════════════════
void getCompactTimestamp(char* buf, size_t len) {
    struct tm timeinfo;
    if (getLocalTime(&timeinfo)) {
        strftime(buf, len, "%Y%m%dT%H%M%S", &timeinfo);
    } else {
        snprintf(buf, len, "20260909000000");
    }
}


// ════════════════════════════════════════════════════════════════════════════
// I2S Setup (ESP32-C3 SuperMini)
// ════════════════════════════════════════════════════════════════════════════
void setupI2S() {
    Serial.println(F("Initializing I2S..."));

    i2s_config_t i2s_config = {
        .mode              = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate       = SAMPLE_RATE,
        .bits_per_sample   = I2S_BITS_PER_SAMPLE_32BIT,
        .channel_format    = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags  = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count     = 4,
        .dma_buf_len       = 256,
        .use_apll          = false,
        .tx_desc_auto_clear = false,
        .fixed_mclk        = 0
    };

    i2s_pin_config_t pin_config = {
        .bck_io_num   = I2S_SCK_PIN,
        .ws_io_num    = I2S_WS_PIN,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num  = I2S_SD_PIN
    };

    esp_err_t err = i2s_driver_install(I2S_NUM_0, &i2s_config, 0, NULL);
    if (err != ESP_OK) {
        Serial.printf("I2S install failed: %d\n", err);
        return;
    }

    err = i2s_set_pin(I2S_NUM_0, &pin_config);
    if (err != ESP_OK) {
        Serial.printf("I2S pin set failed: %d\n", err);
        return;
    }

    i2s_zero_dma_buffer(I2S_NUM_0);
    Serial.println(F("I2S ready."));
}

// ════════════════════════════════════════════════════════════════════════════
// LED Alerts
// ════════════════════════════════════════════════════════════════════════════
void triggerSoundDetectedAlert() {
    Serial.println(F("🔊 Sound detected - LED blinking"));
    for (int i = 0; i < 2; i++) {
        digitalWrite(ALERT_LED_PIN, HIGH);
        delay(150);
        digitalWrite(ALERT_LED_PIN, LOW);
        delay(150);
    }
}

void triggerSolidLedAlert() {
    isLedAlertActive = true;
    Serial.println(F("🔴 LED alert ON for 3 seconds"));
    digitalWrite(ALERT_LED_PIN, HIGH);
    delay(3000);
    digitalWrite(ALERT_LED_PIN, LOW);
    isLedAlertActive = false;
}
