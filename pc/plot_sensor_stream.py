import csv
import sys
import numpy as np
import matplotlib.pyplot as plt

INPUT_FILE = 'sensor_stream.csv'

# Speed of sound in air at sea level, 75 F (23.9 C): c = 331.3 + 0.606 * T_celsius
SPEED_OF_SOUND_M_S = 331.3 + 0.606 * 23.9
US_TO_CM = (SPEED_OF_SOUND_M_S * 100 / 1e6) / 2  # one-way range per microsecond of time-of-flight


def range_axis_cm(meta, n_samples):
    """One-way range (cm) of each sample, measured from the start of the burst.

    Sampling doesn't begin until `T_sample_us - T_ping_us` after the burst
    starts (burst duration + settle delay + overheads, ~1.24 ms / ~21 cm in
    the current firmware), so sample i is `offset + i * dt` after the ping,
    not `i * dt`.
    """
    start_us = meta['T_sample_us'] - meta['T_ping_us']
    return (start_us + np.arange(n_samples) * meta['SampleDt_us']) * US_TO_CM


META_COLUMNS = ['Timestamp', 'PingID', 'T_ping_us', 'T_sample_us', 'NumSamples', 'SampleDt_us']


def parse_row(row):
    """(meta, values) for one sensor_stream.csv data row."""
    meta = dict(zip(META_COLUMNS, row[:len(META_COLUMNS)]))
    for key in ('PingID', 'T_ping_us', 'T_sample_us', 'NumSamples', 'SampleDt_us'):
        meta[key] = int(meta[key])
    values = [int(v) for v in row[len(META_COLUMNS):]]
    return meta, values


def load_packets(path):
    with open(path, newline='') as f:
        reader = csv.reader(f)
        header = next(reader)
        if header[:len(META_COLUMNS)] != META_COLUMNS:
            raise ValueError(
                f"{path} does not match the expected header format {META_COLUMNS}; "
                "re-record with the current receive.py."
            )
        return [parse_row(row) for row in reader]


def main():
    packets = load_packets(INPUT_FILE)
    if not packets:
        print(f"No packets found in {INPUT_FILE}.")
        sys.exit(1)

    state = {'idx': 0}
    n_samples = len(packets[0][1])

    fig, ax = plt.subplots(figsize=(10, 5))
    meta, values = packets[0]
    range_cm = range_axis_cm(meta, n_samples)
    line, = ax.plot(range_cm, values)
    ax.set_xlim(range_cm[0], range_cm[-1])
    ax.set_ylim(0, 4096)  # full 12-bit ADC range
    ax.set_xlabel("Range (cm)")
    ax.set_ylabel("Amplitude")

    def render():
        idx = state['idx']
        meta, values = packets[idx]
        range_cm = range_axis_cm(meta, len(values))
        line.set_xdata(range_cm)
        line.set_ydata(values)
        ax.set_xlim(range_cm[0], range_cm[-1])

        title = f"Packet {idx + 1} / {len(packets)} — {meta['Timestamp']}   ping_id={meta['PingID']}"
        if idx > 0:
            prev_meta, _ = packets[idx - 1]
            dt_ping_ms = (meta['T_ping_us'] - prev_meta['T_ping_us']) / 1000.0
            title += f"   Δt_ping={dt_ping_ms:.1f} ms"
        title += f"   sample_dt={meta['SampleDt_us']} us   (←/→ to navigate)"
        ax.set_title(title)
        fig.canvas.draw_idle()

    def on_key(event):
        if event.key == 'right':
            state['idx'] = min(state['idx'] + 1, len(packets) - 1)
            render()
        elif event.key == 'left':
            state['idx'] = max(state['idx'] - 1, 0)
            render()

    fig.canvas.mpl_connect('key_press_event', on_key)
    render()
    fig.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
