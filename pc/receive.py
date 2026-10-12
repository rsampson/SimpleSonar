
import argparse
import serial
import struct
import csv
import os
import datetime
import signal
import time
from typing import NamedTuple, Optional

# Configuration
# SERIAL_PORT: set to a specific device path (e.g. '/dev/ttyUSB0') to force
# that port; leave as None to auto-detect by probing SERIAL_PORT_CANDIDATES
# (see find_sonar_port()) - the ESP32's USB-serial port has been observed to
# enumerate as either ttyUSB0 or ttyUSB1 depending on what else is plugged in.
SERIAL_PORT = None
SERIAL_PORT_CANDIDATES = ['/dev/ttyUSB0', '/dev/ttyUSB1']
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


class BufferedReader:
    """Buffers serial reads so parsing doesn't cost one syscall per byte.

    At 921600 baud a single byte takes ~10.85us to arrive, but each
    ser.read(1) call has Python/syscall overhead well above that - measured
    OS receive-buffer backlog of up to ~1952 bytes when reading byte-by-byte
    during a packet burst, which risks a kernel buffer overflow (silently
    dropped bytes) and permanently desyncing packet framing. Pulling
    whatever's currently available in one read() call per refill, and
    serving parse requests out of that in-memory buffer, keeps up with the
    incoming rate with much lower per-byte overhead.
    """

    def __init__(self, ser, chunk_size=4096):
        self._ser = ser
        self._chunk_size = chunk_size
        self._buf = bytearray()

    def _fill(self):
        # Read whatever's already buffered at the OS level in one call
        # (cheap, no blocking); if nothing was waiting, fall back to a
        # normal read() of up to chunk_size, which blocks up to ser.timeout
        # and may return b'' (checked by callers).
        to_read = self._ser.in_waiting or self._chunk_size
        chunk = self._ser.read(min(to_read, self._chunk_size))
        if chunk:
            self._buf += chunk
        return chunk

    def read_exact(self, n):
        """Return exactly n bytes, or fewer if the serial timeout elapses
        first (mirrors pyserial's own short-read-on-timeout behavior)."""
        while len(self._buf) < n:
            if not self._fill():
                break
        result = bytes(self._buf[:n])
        del self._buf[:n]
        return result

    def read_byte(self):
        """Return one byte, or b'' if the serial timeout elapses first."""
        if not self._buf:
            self._fill()
        if not self._buf:
            return b''
        b = bytes(self._buf[:1])
        del self._buf[:1]
        return b


class ParseResult(NamedTuple):
    kind: str  # 'packet' | 'debug_line' | 'warning' | 'idle'
    packet: Optional[dict] = None
    text: Optional[str] = None
    preceding_debug_line: Optional[str] = None
    timestamp: Optional[str] = None


def new_parse_state():
    return {'last_ping_id': None, 'debug_line': bytearray()}


def _pop_debug_line(state):
    """Drain state['debug_line'], returning decoded text or None if empty."""
    if not state['debug_line']:
        return None
    text = bytes(state['debug_line']).decode('utf-8', errors='replace').rstrip('\r')
    state['debug_line'].clear()
    return text if text else None


def parse_next_event(reader, state, now=datetime.datetime.now):
    """Consume bytes from `reader` (anything with .read_byte()/.read_exact(n),
    e.g. BufferedReader) to advance the packet-framing state machine by one
    step, and return a ParseResult. Mutates `state` in place. Performs no
    I/O of its own (no print(), no CSV writes) - the caller acts on the
    result. One call == one iteration of the read loop, so the mapping to
    the original inline loop body stays mechanical.

    The ESP32 sends binary data packets and plain-text debug lines on the
    same port. Peek one byte: 0xAA 0xBB marks the start of a binary packet;
    anything else is debug text, accumulated until '\\n'.
    """
    b = reader.read_byte()
    if not b:
        return ParseResult(kind='idle')  # read timeout, no byte available yet

    if b == b'\xAA':
        second = reader.read_byte()
        if second == b'\xBB':
            # Capture the host arrival time (subject to USB/serial latency -
            # use t_ping_us/t_sample_us from the header for the ESP32's own
            # timing instead). Captured here, before the header/payload reads
            # below (which can block up to the serial timeout), so a dropped
            # packet's warning timestamp reflects when the packet started
            # arriving, not when parsing gave up on it.
            debug_text = _pop_debug_line(state)
            timestamp = now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            raw_header = reader.read_exact(HEADER_SIZE)
            if len(raw_header) < HEADER_SIZE:
                return ParseResult(kind='warning', text='Header truncated. Dropping packet.',
                                    preceding_debug_line=debug_text, timestamp=timestamp)

            ping_id, t_ping_us, t_sample_us, num_samples, sample_dt_us = \
                struct.unpack(HEADER_FORMAT, raw_header)

            if num_samples != NUM_SAMPLES:
                # KNOWN QUIRK (preserved as-is, not fixed here): the actual
                # payload the firmware sent for this packet is num_samples*2
                # bytes, but we never read/discard it - we just abandon
                # parsing. Those bytes stay in the reader's buffer and get
                # reinterpreted as the start of the next sync-byte search.
                return ParseResult(
                    kind='warning',
                    text=f'unexpected num_samples={num_samples}, expected {NUM_SAMPLES}. Dropping packet.',
                    preceding_debug_line=debug_text, timestamp=timestamp)

            payload_bytes = num_samples * 2
            raw_data = reader.read_exact(payload_bytes)
            if len(raw_data) < payload_bytes:
                return ParseResult(kind='warning', text='Data truncated. Dropping packet.',
                                    preceding_debug_line=debug_text, timestamp=timestamp)

            integers = struct.unpack(f'<{num_samples}h', raw_data)

            ping_id_gap = None
            if state['last_ping_id'] is not None and ping_id != state['last_ping_id'] + 1:
                ping_id_gap = (state['last_ping_id'], ping_id)
            state['last_ping_id'] = ping_id

            return ParseResult(kind='packet', packet={
                'ping_id': ping_id,
                't_ping_us': t_ping_us,
                't_sample_us': t_sample_us,
                'num_samples': num_samples,
                'sample_dt_us': sample_dt_us,
                'values': integers,
                'ping_id_gap': ping_id_gap,
            }, preceding_debug_line=debug_text, timestamp=timestamp)
        else:
            # Lone 0xAA wasn't followed by 0xBB - treat both bytes as debug
            # text rather than dropping them.
            state['debug_line'] += b
            if second:
                if second == b'\n':
                    return ParseResult(kind='debug_line', text=_pop_debug_line(state))
                else:
                    state['debug_line'] += second
            return ParseResult(kind='idle')
    elif b == b'\n':
        text = _pop_debug_line(state)
        if text is None:
            return ParseResult(kind='idle')
        return ParseResult(kind='debug_line', text=text)
    else:
        state['debug_line'] += b
        return ParseResult(kind='idle')


def _drain_until_quiet(ser, quiet_s=0.3, max_wait_s=2.0):
    """Read and discard bytes until the port has been silent for `quiet_s`.

    STOP only clears the firmware's ping_enabled flag (see loop() in
    firmware/src/main.cpp), which is checked once per loop() iteration - a
    ping cycle already in progress (burst -> sample -> send_data()) finishes
    and transmits its packet regardless of when STOP arrives. A plain
    reset_input_buffer() right after STOP can therefore still leave the tail
    of that in-flight packet to arrive afterward with no sync bytes ahead of
    it (already consumed by the reset), which can collide with the next
    START's first packet and desync framing. This reduces how often that
    happens but hasn't been observed to eliminate it entirely (root cause
    not fully pinned down) - the main read loop's own resync-on-next-0xAA0xBB
    logic is what makes any remaining occurrence harmless: it's detected,
    logged as a ping_id-gap warning, and recovered from automatically rather
    than corrupting the CSV.
    """
    original_timeout = ser.timeout
    ser.timeout = quiet_s
    deadline = time.time() + max_wait_s
    try:
        while time.time() < deadline:
            if not ser.read(4096):
                return  # no bytes for a full quiet_s window - port is idle
    finally:
        ser.timeout = original_timeout
        ser.reset_input_buffer()


def _probe_port(port, timeout_s=3.0):
    """Try to identify the sonar on `port`; return an open, verified,
    idle (pinging stopped) Serial instance on success, or None.

    The probe deliberately does NOT close and let the caller reopen the
    port afterward: whether a second open actually re-toggles DTR/RTS (and
    so re-resets the ESP32) is unreliable across USB-serial adapters, and if
    it doesn't, the caller would attach to a board still mid-stream from the
    probe's own START, corrupting packet framing. Returning the same live
    connection the probe verified sidesteps that race entirely.

    Pinging is stopped again before returning (rather than leaving it
    running until the caller is ready) so nothing accumulates unread in the
    OS's serial receive buffer during the caller's own setup work (opening
    the output CSV, writing a header row, etc.) - that dead time was
    observed to let enough bytes pile up between the probe's START and the
    real read loop starting that the loop's first packet or two came in
    corrupted before it resynchronized.
    """
    try:
        ser = serial.Serial(port, baudrate=BAUD_RATE, timeout=timeout_s)
    except serial.SerialException:
        return None

    try:
        time.sleep(2)  # let the ESP32 finish rebooting before talking to it
        ser.reset_input_buffer()
        send_control_command(ser, START_COMMAND)

        # The firmware's response to START is the plain-text debug line
        # "START command received: pinging enabled\n" (see
        # handle_serial_command() in firmware/src/main.cpp), sent before any
        # binary packets. Read one byte at a time and stop the instant that
        # line's trailing '\n' arrives - reading in larger chunks (or past
        # the newline) risks grabbing the first bytes of the following
        # binary packet along with it, which would desync the main read
        # loop's framing before it even starts.
        deadline = time.time() + timeout_s
        buf = b''
        while time.time() < deadline:
            b = ser.read(1)
            if not b:
                continue
            buf += b
            if b == b'\n':
                if b'START command received' in buf:
                    send_control_command(ser, STOP_COMMAND)
                    _drain_until_quiet(ser)
                    return ser
                buf = b''  # unrelated line (e.g. boot banner); keep scanning
            if len(buf) > 256:
                buf = buf[-256:]  # cap growth if no newline ever arrives
    except serial.SerialException:
        pass

    ser.close()
    return None


def find_sonar_port():
    """Probe SERIAL_PORT_CANDIDATES and return (port, ser) for the one
    running sonar firmware, with `ser` already open, reset, and verified,
    but idle (pinging stopped again after the probe - see _probe_port()).
    The caller is responsible for sending START once it's ready to read.

    Only devices that actually exist are probed, so a missing port (e.g. the
    ESP32 enumerated as ttyUSB0 this time, not ttyUSB1) is skipped rather
    than raising a "no such file" error.
    """
    candidates = [p for p in SERIAL_PORT_CANDIDATES if os.path.exists(p)]
    if not candidates:
        raise RuntimeError(
            f"No candidate serial ports found (checked {SERIAL_PORT_CANDIDATES}). "
            "Is the ESP32 plugged in?"
        )

    for port in candidates:
        print(f"Probing {port} for sonar firmware...")
        ser = _probe_port(port)
        if ser is not None:
            print(f"Found sonar on {port}.")
            return port, ser

    raise RuntimeError(
        f"No sonar firmware responded on any candidate port ({candidates}). "
        "Is the ESP32 running receive-compatible firmware and plugged in?"
    )


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

    if SERIAL_PORT:
        # Explicit port: connect fresh, same as before auto-detect existed.
        print(f"Connecting to {SERIAL_PORT} at {BAUD_RATE} baud...")
        ser = serial.Serial(SERIAL_PORT, baudrate=BAUD_RATE, timeout=2) # 2s timeout gives leeway for 1s intervals

        # Opening the port toggles DTR/RTS, which resets the ESP32. Give it
        # time to finish rebooting and reach loop() before we talk to it,
        # otherwise the START command below is sent into the bootloader and lost.
        time.sleep(2)
        ser.reset_input_buffer()
    else:
        # Auto-detect: find_sonar_port() already opened, reset, and verified
        # the port, then stopped pinging again - reuse that same connection
        # rather than reopening (see _probe_port()) and send our own START
        # below, right before we're ready to read, same as the explicit-port
        # path above.
        port, ser = find_sonar_port()
        print(f"Connected to {port} at {BAUD_RATE} baud.")

    file_exists = os.path.isfile(OUTPUT_FILE) and not fresh
    write_mode = 'w' if fresh else 'a'

    try:
        # Using line-buffered output (buffering=1) so data writes to your disk instantly every second
        with open(OUTPUT_FILE, mode=write_mode, newline='', buffering=1) as f:
            writer = csv.writer(f)

            # Write a header row for a new file
            if not file_exists:
                header = ['Timestamp', 'PingID', 'T_ping_us', 'T_sample_us', 'NumSamples', 'SampleDt_us'] + \
                         [f"Int_{i}" for i in range(NUM_SAMPLES)]
                writer.writerow(header)

            # Sent here, immediately before the read loop, rather than
            # earlier during setup - any delay between START and the loop
            # actually draining the port lets the ESP32's packets pile up
            # unread, and once the OS receive buffer's limit is hit the
            # first packet or two comes in corrupted (the loop resyncs on
            # its own, but starting clean avoids that entirely).
            send_control_command(ser, START_COMMAND)
            print(f"Sent start command to ESP32: {START_COMMAND.decode().strip()}")
            print(f"Streaming data into {OUTPUT_FILE}. Press Ctrl+C to stop.\n")

            reader = BufferedReader(ser)
            parse_state = new_parse_state()

            try:
                while True:
                    result = parse_next_event(reader, parse_state)

                    if result.preceding_debug_line is not None:
                        print(f"[ESP32] {result.preceding_debug_line}")

                    if result.kind == 'idle':
                        continue

                    elif result.kind == 'debug_line':
                        print(f"[ESP32] {result.text}")

                    elif result.kind == 'warning':
                        print(f"[{result.timestamp}] Warning: {result.text}")

                    elif result.kind == 'packet':
                        p = result.packet
                        if p['ping_id_gap'] is not None:
                            last_id, this_id = p['ping_id_gap']
                            print(f"[{result.timestamp}] Warning: ping_id gap "
                                  f"({last_id} -> {this_id}), {this_id - last_id - 1} packet(s) missed.")

                        row_data = [result.timestamp, p['ping_id'], p['t_ping_us'], p['t_sample_us'],
                                    p['num_samples'], p['sample_dt_us']] + list(p['values'])
                        writer.writerow(row_data)

                        print(f"[{result.timestamp}] Logged packet {p['ping_id']} successfully "
                              f"({p['num_samples']} integers, dt={p['sample_dt_us']} us/sample).")

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
