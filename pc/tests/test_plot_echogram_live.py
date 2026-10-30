import numpy as np
import pytest

from plot_echogram_live import (new_live_state, process_new_packets, ring_cancel_weights, tail_new_packets,
                                update_reference, update_ring_estimate)
from plot_sensor_stream import range_axis_cm

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


def test_reference_jumps_up_to_a_stronger_ping():
    assert update_reference(100.0, 2500.0) == 2500.0


def test_reference_relaxes_1_db_per_ping_but_not_below_current_ping():
    assert update_reference(1000.0, 10.0, decay_db=1.0) == pytest.approx(1000.0 * 10 ** (-1 / 20))
    assert update_reference(1000.0, 990.0, decay_db=1.0) == 990.0


def test_reference_recovers_from_a_28_db_transient_in_about_28_pings():
    typical, ref = 100.0, 100.0 * 10 ** (28 / 20)   # right after the transient
    pings = 0
    while ref > typical:
        ref = update_reference(ref, typical, decay_db=1.0)
        pings += 1
    assert 27 <= pings <= 29


def test_ring_estimate_is_the_plain_mean_then_an_exponential_average():
    est = None
    for k, v in enumerate([10.0, 20.0, 30.0]):
        est = update_ring_estimate(est, np.full(3, v), k, n_pings=4)
    np.testing.assert_allclose(est, 20.0)          # mean of the first three
    est = update_ring_estimate(est, np.full(3, 60.0), 3, n_pings=4)
    np.testing.assert_allclose(est, 30.0)          # mean of four
    est = update_ring_estimate(est, np.full(3, 70.0), 4, n_pings=4)
    np.testing.assert_allclose(est, 30.0 + 40.0 / 4)  # then 1/4 per ping


def test_ring_cancel_weights_fade_out_beyond_the_near_range():
    w = ring_cancel_weights([0, 90, 100, 110, 200], max_cm=90, taper_cm=20)
    np.testing.assert_allclose(w, [1, 1, 0.5, 0, 0], atol=1e-12)


def synthetic_pings(n_pings, mover_at_last_cm=None, seed=0):
    """Pings at the real timing: a static 40 kHz ring decaying over the near
    range, a static target at 150 cm, noise, and optionally something new at
    mover_at_last_cm in the last ping."""
    rng = np.random.default_rng(seed)
    meta = {'Timestamp': '', 'PingID': 0, 'T_ping_us': 0, 'T_sample_us': 1500,
            'NumSamples': 4096, 'SampleDt_us': 5}
    r = range_axis_cm(meta, 4096)
    t = np.arange(4096) * 5e-6

    def burst(cm, amp):
        return amp * np.exp(-((r - cm) / 6) ** 2) * np.sin(2 * np.pi * 40e3 * t + cm)

    ring = 2000 * np.exp(-(r - 26) / 8) * np.sin(2 * np.pi * 40e3 * t + 1)
    packets = []
    for k in range(n_pings):
        v = 1750 + ring + burst(150, 300) + rng.normal(0, 3, 4096)
        if mover_at_last_cm is not None and k == n_pings - 1:
            v = v + burst(mover_at_last_cm, 300)
        packets.append((dict(meta, PingID=k, T_ping_us=k * 300_000, T_sample_us=k * 300_000 + 1500), v))
    return packets, r


def last_column(packets, ring_cancel):
    state = new_live_state(ring_cancel=ring_cancel)
    process_new_packets(packets, state)
    return state['columns'][-1]


def peak_db(env, r, lo, hi):
    return 20 * np.log10(np.nanmax(env[(r >= lo) & (r < hi)]))


def test_ring_cancel_clears_the_near_range_and_keeps_far_and_new_targets():
    packets, r = synthetic_pings(20, mover_at_last_cm=55)
    off, on = last_column(packets, False), last_column(packets, True)
    clean, _ = synthetic_pings(20)
    on_without_mover = last_column(clean, True)

    # The static ring is gone from the near range...
    assert peak_db(on_without_mover, r, 40, 45) < peak_db(off, r, 40, 45) - 30
    # ...the far static target is untouched...
    assert abs(peak_db(on, r, 140, 160) - peak_db(off, r, 140, 160)) < 0.1
    # ...and something new in the near range shows up at full strength.
    assert peak_db(on, r, 50, 60) > peak_db(on_without_mover, r, 50, 60) + 20
    # With cancellation the display starts at 30 cm instead of 40.
    assert np.isnan(off[r < 40]).all() and not np.isnan(on[(r >= 31) & (r < 40)]).any()


def test_first_ping_is_not_shown_uncancelled_below_40_cm():
    packets, r = synthetic_pings(3)
    state = new_live_state(ring_cancel=True)
    process_new_packets(packets, state)
    assert np.isnan(state['columns'][0][r < 40]).all()                  # no estimate yet
    assert not np.isnan(state['columns'][1][(r >= 31) & (r < 40)]).any()  # cancelled from the second


def test_a_pause_restarts_the_ring_estimate():
    packets, _ = synthetic_pings(6)
    packets[4][0]['T_ping_us'] += 10_000_000   # 10 s gap before ping 4
    packets[5][0]['T_ping_us'] += 10_000_000
    state = new_live_state(ring_cancel=True)
    process_new_packets(packets[:4], state)
    assert state['ring_count'] == 4
    process_new_packets(packets[4:], state)
    assert state['ring_count'] == 2
