
import argparse
import serial
import struct
import csv
import os
import datetime
import signal
import time

# Configuration
SERIAL_PORT = '/dev/ttyUSB1'
BAUD_RATE = 921600           # must match Serial baud rate set in firmware (SERIAL_BAUD)
OUTPUT_FILE = 'sensor_stream.csv'
START_COMMAND = b'START\n'
STOP_COMMAND = b'STOP\n'

NUM_SAMPLES = 2048
PAYLOAD_BYTES = NUM_SAMPLES * 2  # int16_t per sample

# PingHeader layout (matches the packed struct in firmware src/main.cpp),
# little-endian, sync bytes excluded (already consumed to find frame start):
#   uint32_t ping_id
#   int64_t  t_ping_us     - esp_timer_get_time() at burst start
#   int64_t  t_sample_us   - esp_timer_get_time() at first analogRead
#   uint16_t num_samples
#   uint32_t sample_dt_us  - measured per-sample interval for this record
HEADER_FORMAT = '<IqqHI'
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)


def send_control_command(ser, command):
    ser.write(command)
    ser.flush()
    time.sleep(0.1)


def _install_interrupt_handler():
    # ser.read() spends nearly all its time inside a C-level blocking read,
    # returning to the interpreter only briefly between bytes at this data
    # rate. That starves the default SIGINT handler's deferred
    # KeyboardInterrupt (relying on try/except KeyboardInterrupt around the
    # read loop was observed to miss Ctrl+C for 10+ seconds), so raise it
    # explicitly from the handler instead.
    def handler(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def continuous_serial_to_csv(fresh=False):
    _install_interrupt_handler()

    print(f"Connecting to {SERIAL_PORT} at {BAUD_RATE} baud...")
    ser = serial.Serial(SERIAL_PORT, baudrate=BAUD_RATE, timeout=2) # 2s timeout gives leeway for 1s intervals

    # Opening the port toggles DTR/RTS, which resets the ESP32. Give it time
    # to finish rebooting and reach loop() before we talk to it, otherwise
    # the START command below is sent into the bootloader and lost.
    time.sleep(2)
    ser.reset_input_buffer()

    file_exists = os.path.isfile(OUTPUT_FILE) and not fresh
    write_mode = 'w' if fresh else 'a'

    try:
        send_control_command(ser, START_COMMAND)
        print(f"Sent start command to ESP32: {START_COMMAND.decode().strip()}")

        # Using line-buffered output (buffering=1) so data writes to your disk instantly every second
        with open(OUTPUT_FILE, mode=write_mode, newline='', buffering=1) as f:
            writer = csv.writer(f)

            # Write a header row for a new file
            if not file_exists:
                header = ['Timestamp', 'PingID', 'T_ping_us', 'T_sample_us', 'NumSamples', 'SampleDt_us'] + \
                         [f"Int_{i}" for i in range(NUM_SAMPLES)]
                writer.writerow(header)

            print(f"Streaming data into {OUTPUT_FILE}. Press Ctrl+C to stop.\n")

            last_ping_id = None
            debug_line = bytearray()

            def flush_debug_line():
                if debug_line:
                    text = debug_line.decode('utf-8', errors='replace').rstrip('\r')
                    if text:
                        print(f"[ESP32] {text}")
                    debug_line.clear()

            try:
                while True:
                    # The ESP32 sends binary data packets and plain-text debug
                    # lines on the same port. Peek one byte: 0xAA 0xBB marks the
                    # start of a binary packet; anything else is debug text,
                    # accumulated until '\n' and printed (not logged to CSV).
                    b = ser.read(1)
                    if not b:
                        continue  # read timeout, no byte available yet

                    if b == b'\xAA':
                        second = ser.read(1)
                        if second == b'\xBB':
                            flush_debug_line()

                            # Capture the host arrival time (subject to USB/serial
                            # latency - use T_ping_us/T_sample_us from the header
                            # for the ESP32's own timing instead).
                            timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

                            # 2. Read the rest of the header (sync bytes already consumed)
                            raw_header = ser.read(HEADER_SIZE)
                            if len(raw_header) < HEADER_SIZE:
                                print(f"[{timestamp}] Warning: Header truncated. Dropping packet.")
                                continue

                            ping_id, t_ping_us, t_sample_us, num_samples, sample_dt_us = \
                                struct.unpack(HEADER_FORMAT, raw_header)

                            if num_samples != NUM_SAMPLES:
                                print(f"[{timestamp}] Warning: unexpected num_samples={num_samples}, "
                                      f"expected {NUM_SAMPLES}. Dropping packet.")
                                continue

                            # 3. Read the payload
                            payload_bytes = num_samples * 2
                            raw_data = ser.read(payload_bytes)
                            if len(raw_data) < payload_bytes:
                                print(f"[{timestamp}] Warning: Data truncated. Dropping packet.")
                                continue

                            # 4. Unpack binary bytes to a tuple of integers
                            integers = struct.unpack(f'<{num_samples}h', raw_data)

                            # 5. Detect drops/reordering via ping_id
                            if last_ping_id is not None and ping_id != last_ping_id + 1:
                                print(f"[{timestamp}] Warning: ping_id gap "
                                      f"({last_ping_id} -> {ping_id}), {ping_id - last_ping_id - 1} packet(s) missed.")
                            last_ping_id = ping_id

                            # 6. Prepend timestamp/header fields and write row
                            row_data = [timestamp, ping_id, t_ping_us, t_sample_us, num_samples, sample_dt_us] + \
                                       list(integers)
                            writer.writerow(row_data)

                            print(f"[{timestamp}] Logged packet {ping_id} successfully "
                                  f"({num_samples} integers, dt={sample_dt_us} us/sample).")
                        else:
                            # Lone 0xAA wasn't followed by 0xBB - treat both
                            # bytes as debug text rather than dropping them.
                            debug_line += b
                            if second:
                                if second == b'\n':
                                    flush_debug_line()
                                else:
                                    debug_line += second
                    elif b == b'\n':
                        flush_debug_line()
                    else:
                        debug_line += b

            except KeyboardInterrupt:
                print("\nStreaming stopped by user.")
            finally:
                send_control_command(ser, STOP_COMMAND)
                print(f"Sent stop command to ESP32: {STOP_COMMAND.decode().strip()}")
    finally:
        ser.close()
        print("Serial port closed safely.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stream sonar ping packets from serial into a CSV log.")
    parser.add_argument(
        '--fresh', action='store_true',
        help=f"Erase {OUTPUT_FILE} and start a new file instead of appending to the existing one."
    )
    args = parser.parse_args()
    continuous_serial_to_csv(fresh=args.fresh)
