#include <Arduino.h>
#include <esp_timer.h>
#include <esp_adc/adc_continuous.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#define BUFFER_SIZE 2048 // 2048 int16_t integers = 4096 bytes (4 KB)
int16_t data_buffer[BUFFER_SIZE];

// Ping output and analog input pins
#define PING_PIN_A 25
#define PING_PIN_B 26
#define ANALOG_PIN 34
// PWM (LEDC) configuration for ESP32
#define PING_FREQUENCY 3300      // Hz
#define PING_CYCLES 10          // number of cycles to emit
#define PING_RESOLUTION 8       // bits (0-255)
#define PING_DUTY 128           // duty (0 - 2^PING_RESOLUTION-1)
// ADC configuration - set explicitly so sample rate/dynamic range are
// deterministic rather than relying on core defaults. Sampling uses the
// ESP-IDF ADC continuous (DMA) driver rather than analogRead(): a single
// one-shot analogRead() call costs several microseconds of driver overhead
// on top of the actual conversion, which caps analogRead()-in-a-loop at
// roughly 25-40 kHz and makes the per-sample spacing jittery (it's timed by
// software, not hardware). The continuous driver clocks conversions from
// the APB/ADC hardware timer instead, so ADC_SAMPLE_FREQ_HZ is the true,
// uniform per-sample interval - which is what makes 100 kHz practical here.
#define ADC_RESOLUTION_BITS 12
#define ADC_ATTENUATION ADC_ATTEN_DB_12 // ~0-3.3V input range
#define ADC_SAMPLE_FREQ_HZ 100000 // 100 kHz continuous-mode sample rate
// Bytes per ADC continuous-mode DMA frame (must be a multiple of
// SOC_ADC_DIGI_RESULT_BYTES, 4 on ESP32). Kept well under the ~4092 byte
// single-descriptor ceiling used elsewhere in the Arduino core's ADC
// wrapper; data_buffer is filled by accumulating multiple frames.
#define ADC_FRAME_SIZE_BYTES 512
// Serial baud rate. This single UART carries data packets, debug/status
// text, and START/STOP commands - see send_data()/process_commands() and
// the framing note above them. At 115200 baud, transmitting the header +
// 4KB payload takes ~250-350ms, which dominates the ping-to-ping period.
#define SERIAL_BAUD 921600

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

uint32_t ping_id = 0;
bool ping_enabled = false;
String serial_command_buffer;

adc_continuous_handle_t adc_handle = NULL;
static SemaphoreHandle_t adc_frame_sem = NULL;
volatile uint16_t adc_samples_collected = 0;
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

// The original ESP32's ADC digital/DMA controller re-triggers conversions in
// fixed-size hardware bursts (it can't free-run continuously like later
// chips), and each burst restart can emit a handful of stale/zero readings
// before real conversions resume. At ADC_SAMPLE_FREQ_HZ these show up as
// short runs of exact-zero samples (empirically a few samples long,
// recurring every few dozen samples) - a driver artifact, not real signal:
// a live analog waveform essentially never lands on exactly zero twice in a
// row. Linearly interpolate across each run using its real neighbors rather
// than sending the driver's zeros as if they were measurements.
#define ADC_GLITCH_MAX_RUN 16 // generous upper bound on a burst-restart run
void repair_adc_glitches(uint16_t count) {
    uint16_t i = 0;
    while (i < count) {
        if (data_buffer[i] != 0) {
            i++;
            continue;
        }
        uint16_t run_start = i;
        uint16_t run_end = i; // exclusive
        while (run_end < count && data_buffer[run_end] == 0 && (run_end - run_start) < ADC_GLITCH_MAX_RUN) {
            run_end++;
        }

        int16_t before = (run_start > 0) ? data_buffer[run_start - 1] : 0;
        int16_t after = (run_end < count) ? data_buffer[run_end] : 0;
        bool have_before = run_start > 0;
        bool have_after = run_end < count;

        if (!have_before && !have_after) {
            // Whole buffer is zero - nothing sensible to interpolate from.
        } else if (!have_before) {
            for (uint16_t j = run_start; j < run_end; j++) {
                data_buffer[j] = after;
            }
        } else if (!have_after) {
            for (uint16_t j = run_start; j < run_end; j++) {
                data_buffer[j] = before;
            }
        } else {
            uint16_t span = run_end - run_start + 1; // steps from before to after
            for (uint16_t j = run_start; j < run_end; j++) {
                uint16_t step = j - run_start + 1;
                data_buffer[j] = (int16_t)(before + ((int32_t)(after - before) * step) / span);
            }
        }

        i = run_end;
    }
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
            Serial.println("WARNING: ADC frame wait timed out; padding remainder with zeros");
            break; // timed out waiting on the ADC - avoid hanging loop() forever
        }

        uint32_t bytes_read = 0;
        esp_err_t err = adc_continuous_read(adc_handle, frame, sizeof(frame), &bytes_read, 0);
        if (err != ESP_OK) {
            Serial.printf("WARNING: adc_continuous_read failed: %d\n", (int)err);
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
    repair_adc_glitches(valid_samples);

    // Pad any shortfall (e.g. from the timeout above) with zeros rather than
    // sending stale data from a previous ping.
    for (uint16_t i = valid_samples; i < BUFFER_SIZE; i++) {
        data_buffer[i] = 0;
    }
}

void send_data(const PingHeader &header) {
    // Data packets and debug text share one Serial port. receive.py tells
    // them apart by peeking at the first byte: 0xAA (this header's sync
    // byte) means a binary packet follows; anything else is a plain text
    // debug line ending in '\n'. Never call Serial.print()/println() while
    // a packet is being written, or its bytes could land mid-packet.

    // 1. Send Header (includes its own sync bytes)
    size_t hdr_written = Serial.write((const uint8_t*)&header, sizeof(header));

    // 2. Send Payload (Cast the integer array to a byte pointer)
    size_t payload_written = Serial.write((uint8_t*)data_buffer, sizeof(data_buffer));

    if (hdr_written != sizeof(header) || payload_written != sizeof(data_buffer)) {
        Serial.printf("WARNING: short write ping_id=%lu hdr=%u/%u payload=%u/%u\n",
                       (unsigned long)header.ping_id, (unsigned)hdr_written, (unsigned)sizeof(header),
                       (unsigned)payload_written, (unsigned)sizeof(data_buffer));
    }
}

void handle_serial_command(const String &command) {
    String normalized = command;
    normalized.trim();

    if (normalized.equalsIgnoreCase("START")) {
        ping_enabled = true;
        Serial.println("START command received: pinging enabled");
    } else if (normalized.equalsIgnoreCase("STOP")) {
        ping_enabled = false;
        Serial.println("STOP command received: pinging disabled");
    }
}

void process_commands() {
    while (Serial.available() > 0) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            if (serial_command_buffer.length() > 0) {
                handle_serial_command(serial_command_buffer);
                serial_command_buffer = "";
            }
        } else {
            serial_command_buffer += c;
            if (serial_command_buffer.length() > 32) {
                serial_command_buffer = "";
            }
        }
    }
}

void emit_differential_burst() {
    // A and B free-run as hardware square waves at PING_FREQUENCY (configured
    // in setup(), with B's output hardware-inverted relative to A), so the
    // differential pair is generated entirely by the LEDC peripheral - no
    // per-half-cycle software toggling needed. Un-gate both channels for
    // exactly PING_CYCLES worth of time, then gate them back to idle.
    const uint32_t burstUs = (1000000ULL * PING_CYCLES) / PING_FREQUENCY;

    ledcWrite(PING_PIN_A, PING_DUTY);
    ledcWrite(PING_PIN_B, PING_DUTY);

    delayMicroseconds(burstUs);

    // End with both outputs quiet.
    ledcWrite(PING_PIN_A, 0);
    ledcWrite(PING_PIN_B, 0);
}

void setup() {
    Serial.begin(SERIAL_BAUD);

    // Configure LEDC for the differential ping pair: both pins free-run in
    // hardware at PING_FREQUENCY, with B's output inverted relative to A so
    // the pair is always electrically complementary while attached.
    ledcAttach(PING_PIN_A, PING_FREQUENCY, PING_RESOLUTION);
    ledcAttach(PING_PIN_B, PING_FREQUENCY, PING_RESOLUTION);
    ledcOutputInvert(PING_PIN_B, true);

    ledcWrite(PING_PIN_A, 0);
    ledcWrite(PING_PIN_B, 0);

    init_adc_continuous();
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

    PingHeader header;
    header.sync[0] = 0xAA;
    header.sync[1] = 0xBB;
    header.ping_id = ping_id++;
    header.num_samples = BUFFER_SIZE;

    header.t_ping_us = esp_timer_get_time();
    emit_differential_burst();
    delayMicroseconds(AFTER_DELAY_US);

    header.t_sample_us = esp_timer_get_time();
    header.sample_dt_us = 1000000UL / ADC_SAMPLE_FREQ_HZ;
    capture_samples();

    send_data(header);
}

