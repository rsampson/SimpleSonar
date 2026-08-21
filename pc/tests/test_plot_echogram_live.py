import pytest

from plot_echogram_live import new_live_state, tail_new_packets

HEADER = "Timestamp,PingID,T_ping_us,T_sample_us,NumSamples,SampleDt_us,Int_0,Int_1\r\n"


def row(ping_id, t_ping_us=1000, values=(1, 2)):
    return f"2026-01-01 00:00:00.000,{ping_id},{t_ping_us},1100,2,10,{values[0]},{values[1]}\r\n"


def test_file_not_yet_existing(tmp_path):
    path = tmp_path / "sensor_stream.csv"
    state = new_live_state()

    packets = tail_new_packets(str(path), state)

    assert packets == []
    assert state['offset'] == 0


def test_header_only_file_no_data_yet(tmp_path):
    path = tmp_path / "sensor_stream.csv"
    path.write_text(HEADER)
    state = new_live_state()

    packets = tail_new_packets(str(path), state)

    assert packets == []
    assert state['header_seen'] is True
    assert state['offset'] == len(HEADER.encode())


def test_full_row_arrives_complete(tmp_path):
    path = tmp_path / "sensor_stream.csv"
    path.write_text(HEADER + row(1))
    state = new_live_state()

    packets = tail_new_packets(str(path), state)

    assert len(packets) == 1
    meta, values = packets[0]
    assert meta['PingID'] == 1
    assert values == [1, 2]


def test_partial_row_then_completed_across_two_polls(tmp_path):
    path = tmp_path / "sensor_stream.csv"
    path.write_text(HEADER + row(1))
    state = new_live_state()

    first = tail_new_packets(str(path), state)
    assert len(first) == 1
    assert first[0][0]['PingID'] == 1
    offset_after_first_row = state['offset']

    with open(path, 'a', newline='') as f:
        f.write("2026-01-01 00:00:01.000,2,3000,")  # no trailing newline yet

    second = tail_new_packets(str(path), state)
    assert second == []
    assert state['offset'] == offset_after_first_row  # nothing new consumed

    with open(path, 'a', newline='') as f:
        f.write("3100,2,10,5,6\r\n")

    third = tail_new_packets(str(path), state)
    assert len(third) == 1
    meta, values = third[0]
    assert meta['PingID'] == 2
    assert values == [5, 6]


def test_multiple_complete_rows_in_one_poll(tmp_path):
    path = tmp_path / "sensor_stream.csv"
    path.write_text(HEADER + row(1) + row(2) + row(3))
    state = new_live_state()

    packets = tail_new_packets(str(path), state)

    assert len(packets) == 3
    assert [p[0]['PingID'] for p in packets] == [1, 2, 3]


def test_fresh_truncation_mid_session_sets_just_reset(tmp_path):
    path = tmp_path / "sensor_stream.csv"
    path.write_text(HEADER + row(1) + row(2))
    state = new_live_state()
    tail_new_packets(str(path), state)  # consume both rows, advance offset

    path.write_text(HEADER + row(1))  # simulate --fresh: truncate + restart from PingID=1

    packets = tail_new_packets(str(path), state)

    assert state['just_reset'] is True
    assert len(packets) == 1
    assert packets[0][0]['PingID'] == 1


def test_mismatched_header_after_reset_raises(tmp_path):
    path = tmp_path / "sensor_stream.csv"
    path.write_text(HEADER + row(1))
    state = new_live_state()
    tail_new_packets(str(path), state)

    path.write_text("Timestamp,PingID,Value\r\n2026-01-01 00:00:00.000,1,5\r\n")

    with pytest.raises(ValueError):
        tail_new_packets(str(path), state)
