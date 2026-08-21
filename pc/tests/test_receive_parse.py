import datetime
import struct

import pytest

from receive import HEADER_FORMAT, NUM_SAMPLES, new_parse_state, parse_next_event


class FakeReader:
    """Minimal .read_byte()/.read_exact(n) double for parse_next_event
    tests, backed by a byte buffer. Serves bytes in order; once exhausted,
    read_byte() returns b'' and read_exact() returns a short/empty result,
    mirroring pyserial's read-timeout behavior."""

    def __init__(self, data=b''):
        self._buf = bytearray(data)

    def read_byte(self):
        if not self._buf:
            return b''
        b = bytes(self._buf[:1])
        del self._buf[:1]
        return b

    def read_exact(self, n):
        result = bytes(self._buf[:n])
        del self._buf[:n]
        return result


def make_packet_bytes(ping_id=1, t_ping_us=1000, t_sample_us=1100,
                       num_samples=NUM_SAMPLES, sample_dt_us=10, values=None):
    """Defaults to a valid, fully-parseable packet (num_samples ==
    NUM_SAMPLES) - tests that specifically want a num_samples mismatch
    pass a different value explicitly."""
    if values is None:
        values = [i % 100 for i in range(num_samples)]
    header = struct.pack(HEADER_FORMAT, ping_id, t_ping_us, t_sample_us, num_samples, sample_dt_us)
    payload = struct.pack(f'<{num_samples}h', *values)
    return b'\xAA\xBB' + header + payload


def test_successful_packet_parse():
    reader = FakeReader(make_packet_bytes(ping_id=5, num_samples=NUM_SAMPLES,
                                           values=list(range(NUM_SAMPLES))))
    state = new_parse_state()

    result = parse_next_event(reader, state)

    assert result.kind == 'packet'
    assert result.packet['ping_id'] == 5
    assert result.packet['values'] == tuple(range(NUM_SAMPLES))
    assert result.packet['ping_id_gap'] is None
    assert state['last_ping_id'] == 5
    assert result.preceding_debug_line is None


def test_ping_id_gap_detected():
    reader = FakeReader(make_packet_bytes(ping_id=8))
    state = new_parse_state()
    state['last_ping_id'] = 5

    result = parse_next_event(reader, state)

    assert result.packet['ping_id_gap'] == (5, 8)
    assert state['last_ping_id'] == 8


def test_no_gap_warning_on_first_packet_ever():
    reader = FakeReader(make_packet_bytes(ping_id=999))
    state = new_parse_state()

    result = parse_next_event(reader, state)

    assert result.packet['ping_id_gap'] is None


def test_consecutive_ping_ids_no_gap():
    reader = FakeReader(make_packet_bytes(ping_id=6))
    state = new_parse_state()
    state['last_ping_id'] = 5

    result = parse_next_event(reader, state)

    assert result.packet['ping_id_gap'] is None


def test_header_truncated():
    reader = FakeReader(b'\xAA\xBB' + b'\x01\x02\x03')
    state = new_parse_state()

    result = parse_next_event(reader, state)

    assert result.kind == 'warning'
    assert 'Header truncated' in result.text


def test_payload_truncated():
    header = struct.pack(HEADER_FORMAT, 1, 0, 0, NUM_SAMPLES, 10)
    reader = FakeReader(b'\xAA\xBB' + header + b'\x00\x01')
    state = new_parse_state()

    result = parse_next_event(reader, state)

    assert result.kind == 'warning'
    assert 'Data truncated' in result.text


def test_quirk_bad_num_samples_leaves_payload_unread():
    """Documents existing behavior, not a fix: when num_samples in the
    header doesn't match NUM_SAMPLES, the actual payload bytes the
    firmware sent are never read/discarded, so they corrupt framing for
    the next parse attempt."""
    bad_packet = (b'\xAA\xBB' + struct.pack(HEADER_FORMAT, 1, 0, 0, 4, 10)
                  + struct.pack('<4h', 1, 2, 3, 4))
    good_packet = make_packet_bytes(ping_id=2, num_samples=NUM_SAMPLES,
                                     values=list(range(NUM_SAMPLES)))
    reader = FakeReader(bad_packet + good_packet)
    state = new_parse_state()

    first = parse_next_event(reader, state)
    assert first.kind == 'warning'
    assert 'unexpected num_samples=4' in first.text

    second = parse_next_event(reader, state)
    assert second.kind != 'packet'


def test_lone_0xAA_no_second_byte_yet_timeout():
    reader = FakeReader(b'\xAA')
    state = new_parse_state()

    result = parse_next_event(reader, state)

    assert result.kind == 'idle'
    assert bytes(state['debug_line']) == b'\xAA'


def test_lone_0xAA_followed_by_non_BB_non_newline():
    reader = FakeReader(b'\xAAZ')
    state = new_parse_state()

    result = parse_next_event(reader, state)

    assert result.kind == 'idle'
    assert bytes(state['debug_line']) == b'\xAAZ'


def test_lone_0xAA_followed_by_newline_flushes():
    reader = FakeReader(b'\xAA\n')
    state = new_parse_state()

    result = parse_next_event(reader, state)

    assert result.kind == 'debug_line'
    assert result.text == bytes([0xAA]).decode('utf-8', errors='replace')
    assert bytes(state['debug_line']) == b''


def test_plain_bytes_accumulate_into_debug_line():
    reader = FakeReader(b'hello')
    state = new_parse_state()

    for _ in range(5):
        result = parse_next_event(reader, state)
        assert result.kind == 'idle'

    assert bytes(state['debug_line']) == b'hello'


def test_newline_flushes_accumulated_debug_line():
    reader = FakeReader(b'hello\n')
    state = new_parse_state()

    for _ in range(5):
        result = parse_next_event(reader, state)
        assert result.kind == 'idle'

    result = parse_next_event(reader, state)
    assert result.kind == 'debug_line'
    assert result.text == 'hello'
    assert bytes(state['debug_line']) == b''


def test_newline_with_empty_debug_line_is_idle():
    reader = FakeReader(b'\n')
    state = new_parse_state()

    result = parse_next_event(reader, state)

    assert result.kind == 'idle'


def test_read_timeout_returns_idle():
    reader = FakeReader(b'')
    state = new_parse_state()

    result = parse_next_event(reader, state)

    assert result.kind == 'idle'
    assert state == new_parse_state()


def test_preceding_debug_line_flushed_before_successful_packet():
    reader = FakeReader(b'garbage' + make_packet_bytes(ping_id=1, num_samples=NUM_SAMPLES,
                                                         values=list(range(NUM_SAMPLES))))
    state = new_parse_state()

    for _ in range(len('garbage')):
        result = parse_next_event(reader, state)
        assert result.kind == 'idle'

    result = parse_next_event(reader, state)

    assert result.kind == 'packet'
    assert result.preceding_debug_line == 'garbage'
    assert bytes(state['debug_line']) == b''


def test_preceding_debug_line_flushed_before_warning():
    reader = FakeReader(b'garbage' + b'\xAA\xBB' + b'\x01\x02\x03')
    state = new_parse_state()

    for _ in range(len('garbage')):
        result = parse_next_event(reader, state)
        assert result.kind == 'idle'

    result = parse_next_event(reader, state)

    assert result.kind == 'warning'
    assert result.preceding_debug_line == 'garbage'


def test_timestamp_uses_injected_now():
    reader = FakeReader(make_packet_bytes())
    state = new_parse_state()
    fixed_now = lambda: datetime.datetime(2026, 1, 1, 12, 0, 0, 123456)

    result = parse_next_event(reader, state, now=fixed_now)

    assert result.timestamp == '2026-01-01 12:00:00.123'


def test_multiple_packets_sequential_calls():
    reader = FakeReader(make_packet_bytes(ping_id=1) + make_packet_bytes(ping_id=2))
    state = new_parse_state()

    first = parse_next_event(reader, state)
    assert first.kind == 'packet'
    assert first.packet['ping_id'] == 1
    assert first.packet['ping_id_gap'] is None

    second = parse_next_event(reader, state)
    assert second.kind == 'packet'
    assert second.packet['ping_id'] == 2
    assert second.packet['ping_id_gap'] is None

    assert state['last_ping_id'] == 2
