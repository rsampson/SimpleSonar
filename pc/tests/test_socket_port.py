"""SocketPort (socket:// for the firmware's Wi-Fi bridge) against a local TCP server."""

import socket
import struct
import threading
import time

from receive import (HEADER_FORMAT, NUM_SAMPLES, BufferedReader, new_parse_state, open_port,
                     parse_next_event)


def packet(ping_id):
    header = b'\xAA\xBB' + struct.pack(HEADER_FORMAT, ping_id, 1000, 2240, NUM_SAMPLES, 5)
    return header + struct.pack(f'<{NUM_SAMPLES}h', *range(NUM_SAMPLES))


def serve_once(data):
    """Listen on a free local port; send `data` once the client writes a byte
    (pyserial's open() discards input that arrives before it returns), then
    hold the connection until the client closes it."""
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        conn.settimeout(5)
        conn.recv(1)
        conn.sendall(data)
        try:
            conn.recv(1)        # b'' once the client closes
        except OSError:
            pass
        conn.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def connect(port):
    ser = open_port(f'socket://127.0.0.1:{port}')
    ser.write(b'g')             # tell the server to send
    return ser


def test_in_waiting_reports_the_real_byte_count():
    port = serve_once(b'x' * 5000)
    ser = connect(port)
    try:
        deadline = time.time() + 2
        while ser.in_waiting < 5000 and time.time() < deadline:
            time.sleep(0.01)
        assert ser.in_waiting == 5000      # the stock backend would say 1
    finally:
        ser.close()


def test_packets_parse_over_the_socket():
    port = serve_once(b'START command received\n' + packet(7) + packet(8))
    ser = connect(port)
    try:
        reader, state = BufferedReader(ser), new_parse_state()
        ids = []
        deadline = time.time() + 3
        while len(ids) < 2 and time.time() < deadline:
            r = parse_next_event(reader, state)
            if r.kind == 'packet':
                ids.append(r.packet['ping_id'])
                assert list(r.packet['values'][:3]) == [0, 1, 2]
        assert ids == [7, 8]
    finally:
        ser.close()
