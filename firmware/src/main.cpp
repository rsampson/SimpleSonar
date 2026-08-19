#include <Arduino.h>
#include <esp_timer.h>

#define BUFFER_SIZE 2048 // 2048 int16_t integers = 4096 bytes (4 KB)
int16_t data_buffer[BUFFER_SIZE];

// Define the custom pins you want to use for the second serial port
#define RXD2 16
#define TXD2 17
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
// deterministic rather than relying on core defaults.
#define ADC_RESOLUTION_BITS 12
#define ADC_ATTENUATION ADC_11db // ~0-3.3V input range
// Serial2 baud rate to the PC. At 115200 baud, transmitting the header +
// 4KB payload takes ~250-350ms, which dominates the ping-to-ping period.
#define SERIAL2_BAUD 921600

// Packet header sent ahead of each data_buffer payload. Timestamps are
// esp_timer_get_time() microseconds (monotonic since boot, not wall-clock),
// so the PC must treat them as relative to the ESP32's own clock rather
// than trying to correlate them to host time.
struct __attribute__((packed)) PingHeader {
    uint8_t  sync[2];       // 0xAA 0xBB
    uint32_t ping_id;       // monotonic counter, increments every ping
    int64_t  t_ping_us;     // timestamp at burst start
    int64_t  t_sample_us;   // timestamp at first analogRead() of this record
    uint16_t num_samples;   // BUFFER_SIZE, in case it ever changes
    uint32_t sample_dt_us;  // measured (t_sample_end - t_sample_us) / (num_samples - 1)
};

uint32_t ping_id = 0;
bool ping_enabled = false;
String serial_command_buffer;

void send_data(const PingHeader &header) {
    // 1. Send Header (includes its own sync bytes)
    Serial2.write((const uint8_t*)&header, sizeof(header));

    // 2. Send Payload (Cast the integer array to a byte pointer)
    Serial2.write((uint8_t*)data_buffer, sizeof(data_buffer));
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

void process_serial2_commands() {
    while (Serial2.available() > 0) {
        char c = Serial2.read();
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
    Serial.begin(115200);
     // Initialize the second serial port: Serial2.begin(baud, config, rxPin, txPin);
     Serial2.begin(SERIAL2_BAUD, SERIAL_8N1, RXD2, TXD2);

    // Configure LEDC for the differential ping pair: both pins free-run in
    // hardware at PING_FREQUENCY, with B's output inverted relative to A so
    // the pair is always electrically complementary while attached.
    ledcAttach(PING_PIN_A, PING_FREQUENCY, PING_RESOLUTION);
    ledcAttach(PING_PIN_B, PING_FREQUENCY, PING_RESOLUTION);
    ledcOutputInvert(PING_PIN_B, true);

    ledcWrite(PING_PIN_A, 0);
    ledcWrite(PING_PIN_B, 0);

    analogReadResolution(ADC_RESOLUTION_BITS);
    analogSetPinAttenuation(ANALOG_PIN, ADC_ATTENUATION);
}

void loop() {
    process_serial2_commands();

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
    for (int i = 0; i < BUFFER_SIZE; i++) {
        data_buffer[i] = (int16_t)analogRead(ANALOG_PIN);
    }
    int64_t t_sample_end_us = esp_timer_get_time();
    header.sample_dt_us = (uint32_t)((t_sample_end_us - header.t_sample_us) / (BUFFER_SIZE - 1));

    send_data(header);
}

