#include <Arduino.h>
#include <esp_timer.h>
#include <esp_cpu.h>
#include <soc/gpio_reg.h>
#include <esp_adc/adc_continuous.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <WiFi.h>
#include <esp_http_server.h>
#include <lwip/sockets.h>
#include <errno.h>
#include "web_page.h"

#define BUFFER_SIZE 4096 // 4096 int16_t integers = 8192 bytes (8 KB), ~20 ms at 200 kHz
int16_t data_buffer[BUFFER_SIZE];

// Ping output and analog input pins
// Minimum time between the starts of successive bursts. Pinging back-to-back
// (the serial transfer alone takes ~70 ms) lets the room's reverberation
// from earlier pings pile up under the next one's echoes; a longer interval
// lets it decay first.
#define PING_INTERVAL_MS 300
// The transmit array (10 x 40T-12 in parallel, 12.5 nF) sits across the
// outputs of a DRV8871 H-bridge on 12 V, driven complementary for a 24 V
// peak-to-peak swing. IN1/IN2 = 1/0 drives forward, 0/1 reverse, and 1/1
// brakes (both low sides on, array shorted). Between bursts the pins hold
// brake: 0/0 would put the DRV8871 to sleep after 1 ms, and waking it costs
// ~55 us, the first two cycles of the next burst.
#define PING_PIN_A 25           // DRV8871 IN1
#define PING_PIN_B 26           // DRV8871 IN2
#define ANALOG_PIN 34           // single 40R-12 receive element via the preamp
#define PING_FREQUENCY 40000     // Hz, the 40T/40R-12 resonance (40.0 +/- 1.0 kHz)
// The 40T-12 is narrowband (~4 kHz bandwidth, Q~10), so it needs only
// ~Q/pi =~ 3-4 cycles to reach steady amplitude, and a matched filter can't
// meaningfully compress a longer single-tone burst (no bandwidth to exploit).
// Extra cycles past that lengthen the burst and push out the near-field dead
// zone, but add energy per ping. 10 cycles (250 us, ~4 cm) keeps margin
// above the ramp-up while staying short.
#define PING_CYCLES 10          // number of cycles to emit
// TONE runs on the LEDC (bursts are timed in software, see emit_burst()).
// Channels 0 and 1 share LEDC timer 0, so with B inverted the pair stays
// complementary.
#define PING_RESOLUTION 8       // bits (0-255)
#define PING_CHANNEL_A 0
#define PING_CHANNEL_B 1
#define PING_DUTY 128           // duty (0 - 2^PING_RESOLUTION-1)
// ADC configuration - set explicitly so sample rate/dynamic range are
// deterministic rather than relying on core defaults. Sampling uses the
// ESP-IDF ADC continuous (DMA) driver rather than analogRead(): a single
// one-shot analogRead() call costs several microseconds of driver overhead
// on top of the actual conversion, which caps analogRead()-in-a-loop at
// roughly 25-40 kHz and makes the per-sample spacing jittery (it's timed by
// software, not hardware).  The continuous driver clocks conversions from
// the APB/ADC hardware timer instead, so ADC_SAMPLE_FREQ_HZ is the true,
// uniform per-sample interval - which is what makes 200 kHz practical here.
#define ADC_RESOLUTION_BITS 12
#define ADC_ATTENUATION ADC_ATTEN_DB_12 // ~0-3.3V input range
// 200 kHz gives 5 samples per 40 kHz cycle, puts Nyquist well above the
// preamp's ~61 kHz poles, and keeps sample_dt_us an exact integer (5 us).
#define ADC_SAMPLE_FREQ_HZ 200000 // 200 kHz continuous-mode sample rate
// sample_dt_us (header) is whole microseconds; a rate that doesn't divide
// 1 MHz would silently skew every range axis.
static_assert(1000000 % ADC_SAMPLE_FREQ_HZ == 0, "ADC_SAMPLE_FREQ_HZ must give a whole-us sample period");
// FREQ <hz> accepts drive frequencies in this range (for frequency sweeps).
#define PING_FREQ_MIN_HZ 30000
#define PING_FREQ_MAX_HZ 50000
// CYCLES <n> accepts burst lengths in this range (long bursts measure the
// transducers' steady-state response).
#define PING_CYCLES_MIN 1
#define PING_CYCLES_MAX 200
// Bytes per ADC continuous-mode DMA frame (must be a multiple of
// SOC_ADC_DIGI_RESULT_BYTES, 4 on ESP32). Kept well under the ~4092 byte
// single-descriptor ceiling used elsewhere in the Arduino core's ADC
// wrapper; data_buffer is filled by accumulating multiple frames.
#define ADC_FRAME_SIZE_BYTES 512
// Serial baud rate. This single UART carries data packets, debug/status
// text, and START/STOP commands - see send_data()/process_commands() and
// the framing note above them. At 115200 baud, transmitting the header +
// 8KB payload would take ~700ms; at 921600 it takes ~90ms.
#define SERIAL_BAUD 921600

// At boot (and on WIFI ON) the ESP32 runs an open access point with:
// - a TCP bridge on BRIDGE_PORT: a client gets the same byte stream as the
//   UART (packets and text) and can send the same commands, so pyserial's
//   socket://192.168.4.1:3333 URL works wherever a serial port does;
// - a web server on port 80: GET / is the live echogram page (web_page.h),
//   GET /config the drive settings as JSON, and the /ws WebSocket sends each
//   ping as one binary message (the packet, byte for byte) and takes text
//   commands (START, STOP).
// Pinging starts at boot too, so on a battery the sonar runs with no PC.
#define WIFI_AP_SSID "SimpleSonar"
#define BRIDGE_PORT 3333
// Longest a bridge write may take before the client is dropped. The TCP send
// buffer (~5.7 KB) is smaller than a packet, so a healthy client needs a few
// ms per packet; one that has stopped reading (e.g. a phone app frozen in the
// background) would otherwise stall the ping loop for seconds per write.
#define BRIDGE_WRITE_TIMEOUT_US 250000
#define PING_AUTOSTART true
#define WEB_MAX_CLIENTS 4       // browser sessions (each uses an lwIP socket)
#define WEB_COMMAND_MAX 32      // longest WebSocket command accepted

// Packet header sent ahead of each data_buffer payload. Timestamps are
// esp_timer_get_time() microseconds (monotonic since boot, not wall-clock),
// so the PC must treat them as relative to the ESP32's own clock rather
// than trying to correlate them to host time.
struct __attribute__((packed)) PingHeader {
    uint8_t  sync[2];       // 0xAA 0xBB
    uint32_t ping_id;       // monotonic counter, increments every ping
    int64_t  t_ping_us;     // timestamp at burst start
    int64_t  t_sample_us;   // timestamp at adc_continuous_start() for this record
    uint16_t num_samples;   // BUFFER_SIZE, in case it ever changes
    uint32_t sample_dt_us;  // nominal 1e6 / ADC_SAMPLE_FREQ_HZ, hardware-clocked
};

// Web server state. WebSocket handlers run on the server's own task, so
// they queue commands for loop() (which owns `out` and the ping cycle) and
// pings are sent from that task via httpd_queue_work().
static httpd_handle_t web_server = NULL;
static QueueHandle_t web_command_queue = NULL;
static volatile bool ws_send_busy = false;  // a ping is still going out
static uint8_t ws_frame_buf[sizeof(PingHeader) + BUFFER_SIZE * sizeof(int16_t)];
static bool wifi_start();
static void wifi_stop();
static int ws_client_count();

uint32_t ping_id = 0;
bool ping_enabled = false;
bool tone_enabled = false; // continuous ping_frequency_hz on IN1/IN2, for scope debugging
// Drive frequency: PING_FREQUENCY at boot or after RESET, changed by FREQ <hz>.
// Bursts hit it exactly (emit_burst() times edges off the CPU clock); TONE's
// LEDC can be a few Hz off (its clock divider is quantized).
uint32_t ping_frequency_hz = PING_FREQUENCY;
// Burst length: PING_CYCLES at boot or after RESET, changed by CYCLES <n>.
uint16_t ping_cycles = PING_CYCLES;

// Both bridge inputs high: brake, the idle state between bursts.
static void bridge_brake() {
    pinMode(PING_PIN_A, OUTPUT);
    pinMode(PING_PIN_B, OUTPUT);
    digitalWrite(PING_PIN_A, HIGH);
    digitalWrite(PING_PIN_B, HIGH);
}

// Hands the pins to the LEDC for a continuous complementary square wave.
// Returns the LEDC's actual frequency (0 if it couldn't set it).
static uint32_t start_tone() {
    if (!ledcAttachChannel(PING_PIN_A, ping_frequency_hz, PING_RESOLUTION, PING_CHANNEL_A) ||
        !ledcAttachChannel(PING_PIN_B, ping_frequency_hz, PING_RESOLUTION, PING_CHANNEL_B)) {
        bridge_brake();
        return 0;
    }
    ledcOutputInvert(PING_PIN_B, true);
    ledcWrite(PING_PIN_A, PING_DUTY);
    ledcWrite(PING_PIN_B, PING_DUTY);
    return ledcReadFreq(PING_PIN_A);
}

// Takes the pins back from the LEDC and returns the bridge to brake.
static void stop_tone() {
    ledcOutputInvert(PING_PIN_B, false);
    ledcDetach(PING_PIN_A);
    ledcDetach(PING_PIN_B);
    bridge_brake();
}
String serial_command_buffer;
String bridge_command_buffer;

bool wifi_enabled = false;
WiFiServer bridge_server(BRIDGE_PORT);
WiFiClient bridge_client;

// Sends to the bridge client without blocking for long: WiFiClient::write()
// retries for up to 10 s, which a client that stops reading turns into a
// stalled ping loop. A client that can't take `size` bytes within
// BRIDGE_WRITE_TIMEOUT_US is dropped (its stream would be broken anyway).
static void bridge_write(const uint8_t *buf, size_t size) {
    const int fd = bridge_client.fd();
    const int64_t deadline = esp_timer_get_time() + BRIDGE_WRITE_TIMEOUT_US;
    size_t done = 0;
    while (done < size) {
        ssize_t n = send(fd, buf + done, size - done, MSG_DONTWAIT);
        if (n > 0) {
            done += n;
        } else if ((n < 0 && errno != EAGAIN && errno != EWOULDBLOCK) || esp_timer_get_time() > deadline) {
            break; // connection gone, or too slow
        } else {
            vTaskDelay(1);
        }
    }
    if (done < size) {
        bridge_client.stop();
    }
}

// All output - packets and text - goes through `out`: to the UART, and to
// the TCP bridge client while one is connected, as the same stream.
class TeeOut : public Print {
public:
    size_t write(uint8_t b) override { return write(&b, 1); }
    size_t write(const uint8_t *buf, size_t size) override {
        size_t n = Serial.write(buf, size);
        if (bridge_client && bridge_client.connected()) {
            bridge_write(buf, size);
        }
        return n; // the UART's count: it's the link the short-write check guards
    }
};
TeeOut out;

adc_continuous_handle_t adc_handle = NULL;
static SemaphoreHandle_t adc_frame_sem = NULL;
uint16_t adc_samples_collected = 0;
static adc_channel_t adc_target_channel;

static bool IRAM_ATTR adc_conv_done_cb(adc_continuous_handle_t handle, const adc_continuous_evt_data_t *edata, void *user_data) {
    BaseType_t mustYield = pdFALSE;
    // Counting (not binary) semaphore: on_conv_done can fire again before
    // capture_samples() drains the previous frame, and a binary semaphore
    // would silently drop those extra signals, desyncing "one take = one
    // available frame" and starving the read loop into a timeout.
    xSemaphoreGiveFromISR(adc_frame_sem, &mustYield);
    return mustYield == pdTRUE;
}

void init_adc_continuous() {
    // Max count is generous headroom (store buffer holds at most 4 frames'
    // worth, see max_store_buf_size below); never actually blocks giving.
    adc_frame_sem = xSemaphoreCreateCounting(64, 0);

    adc_continuous_handle_cfg_t handle_cfg = {
        .max_store_buf_size = ADC_FRAME_SIZE_BYTES * 4,
        .conv_frame_size = ADC_FRAME_SIZE_BYTES,
    };
    ESP_ERROR_CHECK(adc_continuous_new_handle(&handle_cfg, &adc_handle));

    adc_unit_t unit;
    ESP_ERROR_CHECK(adc_continuous_io_to_channel(ANALOG_PIN, &unit, &adc_target_channel));

    adc_digi_pattern_config_t pattern = {
        .atten = (uint8_t)ADC_ATTENUATION,
        .channel = (uint8_t)adc_target_channel,
        .unit = (uint8_t)unit,
        .bit_width = (uint8_t)ADC_RESOLUTION_BITS,
    };
    adc_continuous_config_t dig_cfg = {
        .pattern_num = 1,
        .adc_pattern = &pattern,
        .sample_freq_hz = ADC_SAMPLE_FREQ_HZ,
        .conv_mode = ADC_CONV_SINGLE_UNIT_1,
        .format = ADC_DIGI_OUTPUT_FORMAT_TYPE1,
    };
    ESP_ERROR_CHECK(adc_continuous_config(adc_handle, &dig_cfg));

    adc_continuous_evt_cbs_t cbs = {
        .on_conv_done = adc_conv_done_cb,
    };
    ESP_ERROR_CHECK(adc_continuous_register_event_callbacks(adc_handle, &cbs, NULL));
}

// Blocks until BUFFER_SIZE samples have been captured into data_buffer at
// ADC_SAMPLE_FREQ_HZ. Starts/stops the continuous driver around each burst
// (rather than free-running continuously) so pinging can be gated by
// ping_enabled without the ADC DMA pool filling up between pings.
void capture_samples() {
    adc_samples_collected = 0;
    while (xSemaphoreTake(adc_frame_sem, 0) == pdTRUE) {
        // drain any stale counts left over from a previous ping
    }

    ESP_ERROR_CHECK(adc_continuous_start(adc_handle));

    uint8_t frame[ADC_FRAME_SIZE_BYTES];
    while (adc_samples_collected < BUFFER_SIZE) {
        if (xSemaphoreTake(adc_frame_sem, pdMS_TO_TICKS(100)) != pdTRUE) {
            out.println("WARNING: ADC frame wait timed out; padding remainder with zeros");
            break; // timed out waiting on the ADC - avoid hanging loop() forever
        }

        uint32_t bytes_read = 0;
        esp_err_t err = adc_continuous_read(adc_handle, frame, sizeof(frame), &bytes_read, 0);
        if (err != ESP_OK) {
            out.printf("WARNING: adc_continuous_read failed: %d\n", (int)err);
            continue;
        }

        uint16_t collected = adc_samples_collected;
        for (uint32_t i = 0; i + SOC_ADC_DIGI_RESULT_BYTES <= bytes_read && collected < BUFFER_SIZE; i += SOC_ADC_DIGI_RESULT_BYTES) {
            adc_digi_output_data_t *p = (adc_digi_output_data_t *)&frame[i];
            // The DMA stream occasionally carries a stale/invalid record
            // (wrong channel tag) mixed in with valid conversions, most
            // often right after adc_continuous_start(). Skip anything not
            // tagged as our channel rather than treating it as a real
            // (and usually near-zero) sample.
            if (p->type1.channel != adc_target_channel) {
                continue;
            }
            data_buffer[collected++] = (int16_t)p->type1.data;
        }
        adc_samples_collected = collected;
    }

    ESP_ERROR_CHECK(adc_continuous_stop(adc_handle));

    uint16_t valid_samples = adc_samples_collected;

    // The original ESP32's ADC DMA delivers each pair of adjacent samples in
    // swapped order (2k+1 before 2k). Left alone, every sample sits up to one
    // sample period from its true time, alternating, which puts an image of
    // any input tone at fs/2 - f with amplitude tan(2*pi*f/fs) relative to
    // it (measured: exactly that, from 100 Hz to 15 kHz). Swap them back.
    for (uint16_t i = 0; i + 1 < valid_samples; i += 2) {
        int16_t t = data_buffer[i];
        data_buffer[i] = data_buffer[i + 1];
        data_buffer[i + 1] = t;
    }

    // At 100 kHz the ADC DMA emitted short runs of exact-zero samples at each
    // hardware burst restart, which repair_adc_glitches() interpolated over
    // (removed after commit 345fd9b). At 200 kHz none occur (0 in 96 pings
    // measured raw), and the repair would mask real clipping at 0 V, so
    // samples are sent as converted. Restore it if the rate goes back down.

    // Pad any shortfall (e.g. from the timeout above) with zeros rather than
    // sending stale data from a previous ping.
    for (uint16_t i = valid_samples; i < BUFFER_SIZE; i++) {
        data_buffer[i] = 0;
    }
}

void send_data(const PingHeader &header) {
    // Data packets and debug text share one stream (the UART, mirrored to
    // the TCP bridge). receive.py tells them apart by peeking at the first
    // byte: 0xAA (this header's sync byte) means a binary packet follows;
    // anything else is a plain text debug line ending in '\n'. Never print
    // to `out` while a packet is being written, or its bytes could land
    // mid-packet.

    // 1. Send Header (includes its own sync bytes)
    size_t hdr_written = out.write((const uint8_t*)&header, sizeof(header));

    // 2. Send Payload (Cast the integer array to a byte pointer)
    size_t payload_written = out.write((uint8_t*)data_buffer, sizeof(data_buffer));

    if (hdr_written != sizeof(header) || payload_written != sizeof(data_buffer)) {
        out.printf("WARNING: short write ping_id=%lu hdr=%u/%u payload=%u/%u\n",
                       (unsigned long)header.ping_id, (unsigned)hdr_written, (unsigned)sizeof(header),
                       (unsigned)payload_written, (unsigned)sizeof(data_buffer));
    }
}

void handle_serial_command(const String &command) {
    String normalized = command;
    normalized.trim();

    if (normalized.equalsIgnoreCase("START")) {
        if (tone_enabled) {
            tone_enabled = false;
            stop_tone();
        }
        ping_enabled = true;
        out.println("START command received: pinging enabled");
    } else if (normalized.equalsIgnoreCase("STOP")) {
        ping_enabled = false;
        if (tone_enabled) {
            tone_enabled = false;
            stop_tone();
        }
        out.println("STOP command received: pinging disabled");
    } else if (normalized.equalsIgnoreCase("TONE")) {
        // Leave the LEDC output running: a steady complementary square wave
        // on IN1/IN2 until STOP or START.
        ping_enabled = false;
        if (tone_enabled) {
            stop_tone();
        }
        uint32_t actual = start_tone();
        if (actual == 0) {
            tone_enabled = false;
            out.println("TONE failed: LEDC could not start");
            return;
        }
        tone_enabled = true;
        out.printf("TONE command received: continuous %lu Hz tone on IN1/IN2\n", (unsigned long)actual);
    } else if (normalized.length() > 5 && normalized.substring(0, 5).equalsIgnoreCase("FREQ ")) {
        long requested = normalized.substring(5).toInt();
        if (requested < PING_FREQ_MIN_HZ || requested > PING_FREQ_MAX_HZ) {
            out.printf("FREQ rejected: %ld Hz outside %d-%d Hz\n", requested, PING_FREQ_MIN_HZ, PING_FREQ_MAX_HZ);
            return;
        }
        // Bursts are timed off the CPU clock, so they get the requested
        // frequency exactly.
        ping_frequency_hz = (uint32_t)requested;
        if (tone_enabled) {
            // A running TONE picks up the new frequency.
            stop_tone();
            uint32_t tone_hz = start_tone();
            tone_enabled = tone_hz != 0;
            out.printf("FREQ set: requested %ld Hz, actual %lu Hz (TONE %lu Hz)\n", requested,
                       (unsigned long)ping_frequency_hz, (unsigned long)tone_hz);
        } else {
            out.printf("FREQ set: requested %ld Hz, actual %lu Hz\n", requested, (unsigned long)ping_frequency_hz);
        }
    } else if (normalized.length() > 7 && normalized.substring(0, 7).equalsIgnoreCase("CYCLES ")) {
        long requested = normalized.substring(7).toInt();
        if (requested < PING_CYCLES_MIN || requested > PING_CYCLES_MAX) {
            out.printf("CYCLES rejected: %ld outside %d-%d\n", requested, PING_CYCLES_MIN, PING_CYCLES_MAX);
            return;
        }
        ping_cycles = (uint16_t)requested;
        out.printf("CYCLES set: %u\n", (unsigned)ping_cycles);
    } else if (normalized.equalsIgnoreCase("RESET")) {
        // Back to the compiled-in drive settings. Opening the serial port
        // doesn't reliably reboot the ESP32, so FREQ/CYCLES from an earlier
        // session would otherwise carry into normal pinging. Pinging itself
        // is left as it was; a running TONE stops.
        if (tone_enabled) {
            tone_enabled = false;
            stop_tone();
        }
        ping_frequency_hz = PING_FREQUENCY;
        ping_cycles = PING_CYCLES;
        out.printf("RESET done: %lu Hz, %u cycles\n", (unsigned long)ping_frequency_hz, (unsigned)ping_cycles);
    } else if (normalized.equalsIgnoreCase("WIFI ON")) {
        if (wifi_start()) {
            out.printf("WIFI on: SSID %s (open), bridge %s:%d, web http://%s\n", WIFI_AP_SSID,
                       WiFi.softAPIP().toString().c_str(), BRIDGE_PORT, WiFi.softAPIP().toString().c_str());
        } else {
            out.println("WIFI failed: access point did not start");
        }
    } else if (normalized.equalsIgnoreCase("WIFI OFF")) {
        // Sent over the bridge or a WebSocket, this ends its own connection.
        wifi_stop();
        out.println("WIFI off");
    } else if (normalized.equalsIgnoreCase("WIFI STATUS")) {
        out.printf("WIFI status: %s, %d station(s), bridge client %s, %d web client(s)\n",
                   wifi_enabled ? "on" : "off", wifi_enabled ? WiFi.softAPgetStationNum() : 0,
                   (bridge_client && bridge_client.connected()) ? "connected" : "none", ws_client_count());
    }
}

// --- web server -------------------------------------------------------------

static esp_err_t page_handler(httpd_req_t *req) {
    httpd_resp_set_type(req, "text/html");
    return httpd_resp_send(req, WEB_PAGE, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t config_handler(httpd_req_t *req) {
    char json[160];
    snprintf(json, sizeof(json),
             "{\"freq_hz\":%lu,\"cycles\":%u,\"sample_rate_hz\":%d,\"interval_ms\":%d,\"samples\":%d}",
             (unsigned long)ping_frequency_hz, (unsigned)ping_cycles, ADC_SAMPLE_FREQ_HZ, PING_INTERVAL_MS,
             BUFFER_SIZE);
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_send(req, json, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t ws_handler(httpd_req_t *req) {
    if (req->method == HTTP_GET) {
        return ESP_OK; // handshake (done by the server); pings are pushed by ws_broadcast()
    }
    httpd_ws_frame_t frame = {};
    esp_err_t err = httpd_ws_recv_frame(req, &frame, 0); // length only
    if (err != ESP_OK || frame.len > WEB_COMMAND_MAX) {
        return ESP_FAIL; // closes the connection
    }
    char cmd[WEB_COMMAND_MAX + 1] = {};
    frame.payload = (uint8_t *)cmd;
    if (frame.len > 0 && (err = httpd_ws_recv_frame(req, &frame, frame.len)) != ESP_OK) {
        return err;
    }
    if (frame.type == HTTPD_WS_TYPE_TEXT && frame.len > 0) {
        xQueueSend(web_command_queue, cmd, 0); // dropped if loop() is that far behind
    }
    return ESP_OK;
}

static int ws_client_count() {
    if (web_server == NULL) {
        return 0;
    }
    size_t n = WEB_MAX_CLIENTS;
    int fds[WEB_MAX_CLIENTS];
    int count = 0;
    if (httpd_get_client_list(web_server, &n, fds) == ESP_OK) {
        for (size_t i = 0; i < n; i++) {
            count += httpd_ws_get_fd_info(web_server, fds[i]) == HTTPD_WS_CLIENT_WEBSOCKET;
        }
    }
    return count;
}

// Runs on the server's task: sends ws_frame_buf to every WebSocket client.
static void ws_send_work(void *arg) {
    size_t n = WEB_MAX_CLIENTS;
    int fds[WEB_MAX_CLIENTS];
    if (httpd_get_client_list(web_server, &n, fds) == ESP_OK) {
        httpd_ws_frame_t frame = {};
        frame.type = HTTPD_WS_TYPE_BINARY;
        frame.final = true;
        frame.payload = ws_frame_buf;
        frame.len = sizeof(ws_frame_buf);
        for (size_t i = 0; i < n; i++) {
            if (httpd_ws_get_fd_info(web_server, fds[i]) == HTTPD_WS_CLIENT_WEBSOCKET &&
                httpd_ws_send_frame_async(web_server, fds[i], &frame) != ESP_OK) {
                httpd_sess_trigger_close(web_server, fds[i]); // stalled past send_wait_timeout
            }
        }
    }
    ws_send_busy = false;
}

// Queues this ping for the browsers. If the previous one is still going out
// (a slow client), this ping is skipped for them rather than overwriting
// the buffer mid-send; the UART and bridge always get every ping.
static void ws_broadcast(const PingHeader &header) {
    if (web_server == NULL || ws_send_busy || ws_client_count() == 0) {
        return;
    }
    memcpy(ws_frame_buf, &header, sizeof(header));
    memcpy(ws_frame_buf + sizeof(header), data_buffer, sizeof(data_buffer));
    ws_send_busy = true;
    if (httpd_queue_work(web_server, ws_send_work, NULL) != ESP_OK) {
        ws_send_busy = false;
    }
}

static bool web_start() {
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.max_open_sockets = WEB_MAX_CLIENTS;
    cfg.send_wait_timeout = 1; // s: a stalled client is dropped, not waited on
    cfg.lru_purge_enable = true;
    if (httpd_start(&web_server, &cfg) != ESP_OK) {
        web_server = NULL;
        return false;
    }
    httpd_uri_t page = {};
    page.uri = "/";
    page.method = HTTP_GET;
    page.handler = page_handler;
    httpd_register_uri_handler(web_server, &page);
    httpd_uri_t config = {};
    config.uri = "/config";
    config.method = HTTP_GET;
    config.handler = config_handler;
    httpd_register_uri_handler(web_server, &config);
    httpd_uri_t ws = {};
    ws.uri = "/ws";
    ws.method = HTTP_GET;
    ws.handler = ws_handler;
    ws.is_websocket = true;
    httpd_register_uri_handler(web_server, &ws);
    return true;
}

static bool wifi_start() {
    if (wifi_enabled) {
        return true;
    }
    WiFi.mode(WIFI_AP);
    if (!WiFi.softAP(WIFI_AP_SSID)) { // no password: open network
        WiFi.mode(WIFI_OFF);
        return false;
    }
    bridge_server.begin();
    bridge_server.setNoDelay(true);
    if (!web_start()) {
        out.println("WARNING: web server did not start");
    }
    wifi_enabled = true;
    return true;
}

static void wifi_stop() {
    if (!wifi_enabled) {
        return;
    }
    if (web_server != NULL) {
        httpd_stop(web_server);
        web_server = NULL;
        ws_send_busy = false;
    }
    bridge_client.stop();
    bridge_server.end();
    WiFi.softAPdisconnect(true);
    WiFi.mode(WIFI_OFF);
    wifi_enabled = false;
}

// --- commands ------------------------------------------------------------------

static void read_commands(Stream &in, String &buffer) {
    while (in.available() > 0) {
        char c = in.read();
        if (c == '\n' || c == '\r') {
            if (buffer.length() > 0) {
                handle_serial_command(buffer);
                buffer = "";
            }
        } else {
            buffer += c;
            if (buffer.length() > 32) {
                buffer = "";
            }
        }
    }
}

void process_commands() {
    read_commands(Serial, serial_command_buffer);
    char cmd[WEB_COMMAND_MAX + 1];
    while (web_command_queue != NULL && xQueueReceive(web_command_queue, cmd, 0) == pdTRUE) {
        handle_serial_command(String(cmd));
    }
    if (!wifi_enabled) {
        return;
    }
    if (bridge_server.hasClient()) {
        // One bridge client at a time; a new connection replaces the old.
        bridge_client.stop();
        bridge_client = bridge_server.accept();
        bridge_client.setNoDelay(true);
        bridge_command_buffer = "";
    }
    if (bridge_client && bridge_client.connected()) {
        read_commands(bridge_client, bridge_command_buffer);
    }
}

// Emits the burst and returns esp_timer_get_time() at its true start.
//
// The bridge is driven from brake through ping_cycles x (forward, reverse)
// and back to brake, with each pin edge timed off the CPU cycle counter
// inside a critical section, from IRAM. That gives the exact cycle count
// at any FREQ, a burst start that is a fixed number of CPU cycles after the
// returned timestamp (so each echo's phase against the ADC window repeats
// from ping to ping, for coherent averaging), and a waveform that can later
// be extended, e.g. with anti-phase damping cycles. Interrupts on this core
// are off for the burst: 250 us at the default 10 cycles, 5 ms at 200.
//
// Half-period edges are spaced cpu_hz / (2 f) ticks apart, carrying the
// remainder Bresenham-style so the average frequency is exact (each edge is
// within one tick, ~4 ns). Each transition sets the rising pin before
// clearing the falling one, so the two writes' ~25 ns skew is a brief brake,
// far shorter than the DRV8871 can see (it needs 400-800 ns).
static portMUX_TYPE burst_mux = portMUX_INITIALIZER_UNLOCKED;

int64_t IRAM_ATTR emit_burst() {
    const uint32_t mask_a = 1UL << PING_PIN_A;
    const uint32_t mask_b = 1UL << PING_PIN_B;
    const uint32_t cpu_hz = getCpuFrequencyMhz() * 1000000UL;
    const uint32_t half_den = 2 * ping_frequency_hz;
    const uint32_t half_q = cpu_hz / half_den;
    const uint32_t half_r = cpu_hz % half_den;
    const uint32_t half_cycles = 2 * (uint32_t)ping_cycles;

    portENTER_CRITICAL(&burst_mux);
    const int64_t t_start = esp_timer_get_time();
    const uint32_t c_start = esp_cpu_get_cycle_count();
    uint32_t edge = 0; // ticks from c_start to the next edge
    uint32_t rem = 0;
    for (uint32_t h = 0; h <= half_cycles; h++) {
        while (esp_cpu_get_cycle_count() - c_start < edge) {
        }
        if (h == half_cycles) {
            REG_WRITE(GPIO_OUT_W1TS_REG, mask_a | mask_b); // brake
        } else if (h & 1) {
            REG_WRITE(GPIO_OUT_W1TS_REG, mask_b); // reverse: IN1=0, IN2=1
            REG_WRITE(GPIO_OUT_W1TC_REG, mask_a);
        } else {
            REG_WRITE(GPIO_OUT_W1TS_REG, mask_a); // forward: IN1=1, IN2=0
            REG_WRITE(GPIO_OUT_W1TC_REG, mask_b);
        }
        edge += half_q;
        rem += half_r;
        if (rem >= half_den) {
            rem -= half_den;
            edge++;
        }
    }
    portEXIT_CRITICAL(&burst_mux);

    return t_start;
}

void setup() {
    // Brake first: until these are outputs the DRV8871's pulldowns hold it
    // asleep with the array floating.
    bridge_brake();

    Serial.begin(SERIAL_BAUD);

    init_adc_continuous();

    web_command_queue = xQueueCreate(4, WEB_COMMAND_MAX + 1);
    if (!wifi_start()) {
        out.println("WARNING: access point did not start");
    }
    ping_enabled = PING_AUTOSTART;
}

void loop() {
    process_commands();

    if (!ping_enabled) {
        delay(10);
        return;
    }

    // Microseconds to wait after burst before sampling. If the transducer
    // has appreciable Q, mechanical ringdown after the drive signal stops
    // can last hundreds of us to a few ms and will show up as a spurious
    // near-range echo unless this is long enough - verify on a scope.
    const unsigned int AFTER_DELAY_US = 1000;

    const uint32_t loop_start_ms = millis();

    PingHeader header;
    header.sync[0] = 0xAA;
    header.sync[1] = 0xBB;
    header.ping_id = ping_id++;
    header.num_samples = BUFFER_SIZE;

    header.t_ping_us = emit_burst(); // timestamp of the burst's true start
    delayMicroseconds(AFTER_DELAY_US);

    header.t_sample_us = esp_timer_get_time();
    header.sample_dt_us = 1000000UL / ADC_SAMPLE_FREQ_HZ;
    capture_samples();

    send_data(header);
    ws_broadcast(header);

    // Pad out to PING_INTERVAL_MS, staying responsive to START/STOP.
    while (ping_enabled && (millis() - loop_start_ms) < PING_INTERVAL_MS) {
        process_commands();
        delay(1);
    }
}

