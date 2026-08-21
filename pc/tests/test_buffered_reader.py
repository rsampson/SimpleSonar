from receive import BufferedReader


class FakeSerial:
    """Minimal double for the .in_waiting/.read(n) subset of pyserial's
    Serial that BufferedReader._fill() uses. Bytes are delivered from a
    queue of 'arrival chunks' so tests can control exactly what's
    available on a given _fill() call."""

    def __init__(self, chunks, expect_read_n=None):
        self._chunks = list(chunks)
        self._expect_read_n = expect_read_n

    @property
    def in_waiting(self):
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, n):
        if self._expect_read_n is not None:
            assert n == self._expect_read_n, f"expected read({self._expect_read_n}), got read({n})"
        if not self._chunks:
            return b''
        return self._chunks.pop(0)[:n]


def test_read_exact_single_fill_satisfies_request():
    fake = FakeSerial(chunks=[b'0123456789'])
    reader = BufferedReader(fake, chunk_size=4096)

    result = reader.read_exact(5)

    assert result == b'01234'
    assert bytes(reader._buf) == b'56789'


def test_read_exact_spans_multiple_fills():
    fake = FakeSerial(chunks=[b'ab', b'cd', b'ef'])
    reader = BufferedReader(fake, chunk_size=4096)

    result = reader.read_exact(5)

    assert result == b'abcde'
    assert bytes(reader._buf) == b'f'


def test_read_exact_short_on_timeout():
    fake = FakeSerial(chunks=[b'ab'])
    reader = BufferedReader(fake, chunk_size=4096)

    result = reader.read_exact(5)

    assert result == b'ab'


def test_read_byte_single_byte_reads_amortized():
    fake = FakeSerial(chunks=[b'xyz'])
    reader = BufferedReader(fake, chunk_size=4096)

    assert reader.read_byte() == b'x'
    assert reader.read_byte() == b'y'
    assert reader.read_byte() == b'z'
    # All three bytes came from one _fill() call - nothing left queued.
    assert fake._chunks == []


def test_read_byte_timeout_returns_empty():
    fake = FakeSerial(chunks=[])
    reader = BufferedReader(fake, chunk_size=4096)

    assert reader.read_byte() == b''


def test_fill_respects_in_waiting_over_chunk_size():
    fake = FakeSerial(chunks=[b'xy'], expect_read_n=2)
    reader = BufferedReader(fake, chunk_size=4096)

    assert reader.read_byte() == b'x'
