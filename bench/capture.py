"""Capture a fixed number of pings to a CSV in the sensor_stream.csv format.

Reuses pc/receive.py's port detection and packet parser; the serial port is
opened once and can be reused for many captures (START/STOP per capture).

    python bench/capture.py out.csv -n 60
"""

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pc'))
import receive  # noqa: E402
from receive import (BufferedReader, find_sonar_port, new_parse_state,  # noqa: E402
                     parse_next_event, send_control_command)

HEADER = ['Timestamp', 'PingID', 'T_ping_us', 'T_sample_us', 'NumSamples', 'SampleDt_us'] + \
         [f"Int_{i}" for i in range(receive.NUM_SAMPLES)]


def open_sonar():
    """Find the sonar and return its open, idle serial port."""
    _, ser = find_sonar_port()
    return ser


def capture(ser, n_pings, out_path, skip=2, timeout_s=None, log=print):
    """Record n_pings valid packets to out_path; returns the number written.

    The first `skip` packets after START are discarded: the first packet of
    a run is occasionally corrupted (see CLAUDE.md), and dropping a couple
    costs well under a second.
    """
    if timeout_s is None:
        timeout_s = 10 + n_pings * 1.0
    reader = BufferedReader(ser)
    state = new_parse_state()
    written = skipped = 0
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        ser.reset_input_buffer()
        # No settle sleep: the read loop must start draining before the
        # first 8 KB packet overflows the tty/USB-serial buffers.
        send_control_command(ser, receive.START_COMMAND, settle_s=0)
        deadline = time.monotonic() + timeout_s
        try:
            while written < n_pings:
                if time.monotonic() > deadline:
                    log(f"  timed out after {written} of {n_pings} pings")
                    break
                result = parse_next_event(reader, state)
                if result.kind == 'warning':
                    log(f"  warning: {result.text}")
                if result.kind != 'packet':
                    continue
                p = result.packet
                if skipped < skip:
                    skipped += 1
                    continue
                if any(v < 0 or v > 4095 for v in p['values']):
                    log(f"  dropped corrupted packet {p['ping_id']}")
                    continue
                writer.writerow([result.timestamp, p['ping_id'], p['t_ping_us'], p['t_sample_us'],
                                 p['num_samples'], p['sample_dt_us']] + list(p['values']))
                written += 1
        finally:
            send_control_command(ser, receive.STOP_COMMAND)
            time.sleep(0.4)            # let an in-flight packet finish
            ser.reset_input_buffer()
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('out_csv')
    ap.add_argument('-n', '--pings', type=int, default=60)
    args = ap.parse_args()
    ser = open_sonar()
    try:
        n = capture(ser, args.pings, args.out_csv)
        print(f"Wrote {n} pings to {args.out_csv}")
    finally:
        ser.close()


if __name__ == '__main__':
    main()
