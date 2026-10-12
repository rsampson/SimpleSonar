import numpy as np
import pytest

from plot_sensor_stream import US_TO_CM, load_packets, range_axis_cm


def test_load_packets_valid_file(tmp_path):
    csv_path = tmp_path / "sensor_stream.csv"
    csv_path.write_text(
        "Timestamp,PingID,T_ping_us,T_sample_us,NumSamples,SampleDt_us,Int_0,Int_1\r\n"
        "2026-01-01 00:00:00.000,1,1000,1100,2,10,100,-50\r\n"
        "2026-01-01 00:00:00.100,2,2000,2100,2,10,200,-60\r\n"
    )

    packets = load_packets(str(csv_path))

    assert len(packets) == 2
    assert packets[0][0] == {
        'Timestamp': '2026-01-01 00:00:00.000',
        'PingID': 1,
        'T_ping_us': 1000,
        'T_sample_us': 1100,
        'NumSamples': 2,
        'SampleDt_us': 10,
    }
    assert packets[0][1] == [100, -50]
    assert packets[1][0]['PingID'] == 2


def test_load_packets_mismatched_header_raises(tmp_path):
    csv_path = tmp_path / "sensor_stream.csv"
    csv_path.write_text("Timestamp,PingID,Value\r\n2026-01-01 00:00:00.000,1,5\r\n")

    with pytest.raises(ValueError, match="does not match the expected header format"):
        load_packets(str(csv_path))


def test_load_packets_empty_file_raises_stopiteration(tmp_path):
    csv_path = tmp_path / "sensor_stream.csv"
    csv_path.write_text("")

    with pytest.raises(StopIteration):
        load_packets(str(csv_path))


def test_load_packets_header_only_returns_empty_list(tmp_path):
    csv_path = tmp_path / "sensor_stream.csv"
    csv_path.write_text("Timestamp,PingID,T_ping_us,T_sample_us,NumSamples,SampleDt_us\r\n")

    packets = load_packets(str(csv_path))

    assert packets == []


def test_range_axis_cm_starts_at_capture_offset():
    meta = {'T_ping_us': 1000, 'T_sample_us': 9200, 'SampleDt_us': 10}

    range_cm = range_axis_cm(meta, 4)

    # sample 0 is 8200 us after the ping started, not at 0 cm
    assert range_cm[0] == pytest.approx(8200 * US_TO_CM)
    np.testing.assert_allclose(np.diff(range_cm), 10 * US_TO_CM)
    assert len(range_cm) == 4
