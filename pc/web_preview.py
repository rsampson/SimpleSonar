"""Preview the ESP32's live echogram page on a PC, fed from a recording.

    python web_preview.py                      # sensor_stream.csv, http://localhost:8000
    python web_preview.py --csv OTHER.csv --port 8080

Serves the page in firmware/src/web_page.h (the text between its R"HTML( and
)HTML" delimiters) with the firmware's /config and /ws endpoints. /ws sends
each recorded row as a binary ping packet, built exactly as the firmware
sends it, every PING_INTERVAL_S, looping at the end of the file; START and
STOP from the page pause and resume the replay. For developing the page
without joining the sonar's Wi-Fi. Standard library only.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import socketserver
import struct
import threading
import time

from plot_matched_filter import PING_CYCLES, PING_FREQUENCY_HZ
from plot_sensor_stream import load_packets
from receive import HEADER_FORMAT

PAGE_H = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'firmware', 'src', 'web_page.h')
PING_INTERVAL_S = 0.3          # PING_INTERVAL_MS in the firmware
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def load_page(path=PAGE_H):
    """The HTML inside web_page.h's raw string literal."""
    text = open(path, encoding='utf-8').read()
    opener = 'WEB_PAGE[] = R"HTML('   # the comments above it mention the delimiter too
    start = text.index(opener) + len(opener)
    return text[start:text.index(')HTML"', start)]


def packet_bytes(meta, values):
    """A recorded row as the firmware's packet: sync, header, int16 samples."""
    header = struct.pack(HEADER_FORMAT, meta['PingID'], meta['T_ping_us'], meta['T_sample_us'],
                         meta['NumSamples'], meta['SampleDt_us'])
    return b'\xAA\xBB' + header + struct.pack(f'<{len(values)}h', *values)


def ws_frame(payload, opcode):
    """One unmasked, final server-to-client WebSocket frame."""
    n = len(payload)
    if n < 126:
        head = struct.pack('!BB', 0x80 | opcode, n)
    elif n < 1 << 16:
        head = struct.pack('!BBH', 0x80 | opcode, 126, n)
    else:
        head = struct.pack('!BBQ', 0x80 | opcode, 127, n)
    return head + payload


def ws_read_frame(rfile):
    """(opcode, payload) of one masked client frame, or (None, b'') at EOF."""
    head = rfile.read(2)
    if len(head) < 2:
        return None, b''
    opcode, n = head[0] & 0x0F, head[1] & 0x7F
    if n == 126:
        n = struct.unpack('!H', rfile.read(2))[0]
    elif n == 127:
        n = struct.unpack('!Q', rfile.read(8))[0]
    mask = rfile.read(4) if head[1] & 0x80 else b'\0\0\0\0'
    data = rfile.read(n)
    return opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


class Handler(http.server.BaseHTTPRequestHandler):
    packets = []        # set in main()
    config = {}

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == '/':
            self._send(200, 'text/html', load_page().encode())   # reread: edits show on reload
        elif self.path == '/config':
            self._send(200, 'application/json', json.dumps(self.config).encode())
        elif self.path == '/ws' and self.headers.get('Upgrade', '').lower() == 'websocket':
            self._websocket()
        else:
            self._send(404, 'text/plain', b'not found')

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _websocket(self):
        key = self.headers['Sec-WebSocket-Key']
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.send_header('Sec-WebSocket-Accept', accept)
        self.end_headers()
        running = threading.Event()
        running.set()
        closed = threading.Event()

        def reader():
            while not closed.is_set():
                opcode, data = ws_read_frame(self.rfile)
                if opcode is None or opcode == 0x8:
                    closed.set()
                elif opcode == 0x1:
                    cmd = data.decode(errors='replace').strip().upper()
                    print(f"page sent {cmd}")
                    if cmd == 'START':
                        running.set()
                    elif cmd == 'STOP':
                        running.clear()

        threading.Thread(target=reader, daemon=True).start()
        i = 0
        try:
            while not closed.is_set():
                if running.is_set():
                    self.wfile.write(ws_frame(self.packets[i % len(self.packets)], 0x2))
                    self.wfile.flush()
                    i += 1
                time.sleep(PING_INTERVAL_S)
        except (BrokenPipeError, ConnectionResetError):
            pass
        closed.set()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--csv', default='sensor_stream.csv')
    ap.add_argument('--port', type=int, default=8000)
    args = ap.parse_args()
    rows = load_packets(args.csv)
    if not rows:
        raise SystemExit(f"No packets in {args.csv}.")
    Handler.packets = [packet_bytes(m, v) for m, v in rows]
    meta = rows[0][0]
    Handler.config = {'freq_hz': PING_FREQUENCY_HZ, 'cycles': PING_CYCLES,
                      'sample_rate_hz': round(1e6 / meta['SampleDt_us']),
                      'interval_ms': round(PING_INTERVAL_S * 1000), 'samples': meta['NumSamples']}
    load_page()   # fail now, not on the first request, if the header can't be read
    print(f"Replaying {len(rows)} pings from {args.csv} at http://localhost:{args.port}  (Ctrl+C to stop)")
    with Server(('', args.port), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
