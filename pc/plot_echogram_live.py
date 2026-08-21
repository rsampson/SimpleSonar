import csv
import os
import numpy as np
import matplotlib.pyplot as plt

from plot_sensor_stream import META_COLUMNS
from plot_matched_filter import make_ping_template, matched_filter, US_TO_CM
from plot_echogram import hilbert_envelope

INPUT_FILE = 'sensor_stream.csv'

# receive.py logs one ping roughly every 68ms (measured against a live
# recording - 2048 samples / 100kHz ADC + burst + settle delay). Polling
# well under that keeps pings from visibly batching up without any
# meaningful CPU cost; revisit if firmware/src/main.cpp's timing changes.
POLL_INTERVAL_S = 0.05

# Fixed-width scrolling window, like a real fish-finder, rather than an
# ever-growing image - keeps redraw cost and memory bounded regardless of
# session length. ~13.6s of history at the measured cadence above.
WINDOW_PINGS = 200

# Display in dB below the strongest echo seen so far. Kept separate from
# plot_echogram.py's constant of the same name/value since a user may
# reasonably want a different setting for live tuning vs. offline review.
DYNAMIC_RANGE_DB = 40

CM_PER_FOOT = 30.48


def new_live_state():
    state = {
        'offset': 0,
        'header_seen': False,
        'just_reset': False,
    }
    reset_echogram_state(state)
    return state


def reset_echogram_state(state):
    state.update({
        'n_samples': None,
        'sample_dt_us': None,
        'range_cm': None,
        'template': None,
        'blank_samples': None,
        'blank_cm': None,
        'columns': [],
        'ping_times': [],
        'ping_ids': [],
        'running_peak': 0.0,
        'total_pings_seen': 0,
    })


def tail_new_packets(path, state):
    """Read and parse any complete new rows appended to `path` since the
    last call. Returns a list of (meta, values) tuples, possibly empty.

    Tolerates the file not existing yet (receive.py hasn't started) and a
    row being read mid-write (stashes the incomplete tail for next time).
    A drop in file size means receive.py --fresh truncated and restarted
    the file; that's signalled via state['just_reset'] for the caller to
    also clear the derived echogram state (this function only owns the
    file-tailing fields).
    """
    try:
        size = os.stat(path).st_size
    except FileNotFoundError:
        return []

    if state['offset'] > 0 and size < state['offset']:
        state['offset'] = 0
        state['header_seen'] = False
        state['just_reset'] = True

    with open(path, newline='') as f:
        f.seek(state['offset'])
        new_data = f.read()

    # The offset only ever advances up to the last newline actually seen,
    # so an unconsumed partial line at the tail is simply re-read (from
    # its unchanged offset) alongside whatever's new next time - no need
    # to separately buffer it in state between calls.
    split_at = new_data.rfind('\n')
    if split_at == -1:
        return []  # no complete line available yet this poll

    state['offset'] += len(new_data[:split_at + 1].encode())
    lines = [line for line in new_data[:split_at + 1].split('\n') if line]
    if not lines:
        return []

    if not state['header_seen']:
        header = next(csv.reader(lines[:1]))
        if header[:len(META_COLUMNS)] != META_COLUMNS:
            raise ValueError(
                f"{path} does not match the expected header format {META_COLUMNS}; "
                "re-record with the current receive.py."
            )
        state['header_seen'] = True
        lines = lines[1:]

    packets = []
    for row in csv.reader(lines):
        meta = dict(zip(META_COLUMNS, row[:len(META_COLUMNS)]))
        for key in ('PingID', 'T_ping_us', 'T_sample_us', 'NumSamples', 'SampleDt_us'):
            meta[key] = int(meta[key])
        values = [int(v) for v in row[len(META_COLUMNS):]]
        packets.append((meta, values))
    return packets


def process_new_packets(new_packets, live_state):
    """Run matched filter + envelope on each newly tailed packet and
    append the resulting column to live_state's growing buffers, trimmed
    to the last WINDOW_PINGS entries. Never reprocesses prior packets.
    """
    for meta, values in new_packets:
        if live_state['template'] is None:
            sample_dt_us = meta['SampleDt_us']
            n_samples = meta['NumSamples']
            live_state['n_samples'] = n_samples
            live_state['sample_dt_us'] = sample_dt_us
            live_state['range_cm'] = np.arange(n_samples) * sample_dt_us * US_TO_CM
            template = make_ping_template(sample_dt_us)
            live_state['template'] = template
            blank_cm = len(template) * sample_dt_us * US_TO_CM
            live_state['blank_cm'] = blank_cm
            live_state['blank_samples'] = int(np.ceil(blank_cm / (sample_dt_us * US_TO_CM))) if sample_dt_us > 0 else 0

        tmpl = make_ping_template(meta['SampleDt_us'])
        mf = matched_filter(values, tmpl)
        env = hilbert_envelope(mf)
        env[:live_state['blank_samples']] = np.nan

        live_state['columns'].append(env)
        live_state['ping_times'].append(meta['T_ping_us'] / 1e6)
        live_state['ping_ids'].append(meta['PingID'])
        live_state['running_peak'] = max(live_state['running_peak'], np.nanmax(env))
        live_state['total_pings_seen'] += 1

        if len(live_state['columns']) > WINDOW_PINGS:
            del live_state['columns'][:-WINDOW_PINGS]
            del live_state['ping_times'][:-WINDOW_PINGS]
            del live_state['ping_ids'][:-WINDOW_PINGS]


def make_window_image(live_state):
    """Dense (n_samples, WINDOW_PINGS) array for the current scrolling
    window, oldest-to-newest left-to-right, NaN-padded on the left until
    the window has WINDOW_PINGS worth of real data.
    """
    columns = live_state['columns']
    pad = WINDOW_PINGS - len(columns)
    image = np.column_stack(columns) if columns else np.zeros((live_state['n_samples'], 0))
    if pad > 0:
        filler = np.full((live_state['n_samples'], pad), np.nan)
        image = np.column_stack([filler, image])
    return image


def init_plot(live_state):
    fig, ax = plt.subplots(figsize=(12, 6))
    x = np.arange(WINDOW_PINGS + 1)
    y = np.concatenate([live_state['range_cm'], [live_state['range_cm'][-1] + live_state['sample_dt_us'] * US_TO_CM]])
    initial = np.full((live_state['n_samples'], WINDOW_PINGS), np.nan)
    mesh = ax.pcolormesh(
        x, y, initial,
        shading='flat',
        cmap='viridis',
        vmin=-DYNAMIC_RANGE_DB, vmax=0,
    )
    ax.invert_yaxis()
    ax.set_xlabel(f"Ping (most recent {WINDOW_PINGS} → right)")
    ax.set_ylabel("Range (cm)")

    ax_ft = ax.secondary_yaxis('right', functions=(lambda cm: cm / CM_PER_FOOT, lambda ft: ft * CM_PER_FOOT))
    ax_ft.set_ylabel("Range (ft)")

    cbar = fig.colorbar(mesh, ax=ax)
    cbar.set_label("Echo strength (dB re: strongest return so far)")

    fig.tight_layout()
    fig.show()
    return fig, ax, mesh


def redraw(fig, ax, mesh, live_state):
    image = make_window_image(live_state)
    peak = live_state['running_peak'] if live_state['running_peak'] > 0 else 1.0
    with np.errstate(divide='ignore', invalid='ignore'):
        db = 20 * np.log10(image / peak)
    mesh.set_array(db.ravel())

    latest_id = live_state['ping_ids'][-1] if live_state['ping_ids'] else '-'
    title = (f"Live echogram — {live_state['total_pings_seen']} pings so far, "
             f"latest ping_id={latest_id}, {DYNAMIC_RANGE_DB} dB dynamic range")
    ax.set_title(title)
    fig.canvas.draw_idle()


def main():
    print(f"Waiting for {INPUT_FILE}... (start receive.py in another terminal)")
    live_state = new_live_state()
    fig = ax = mesh = None
    printed_first = False

    try:
        while True:
            new_packets = tail_new_packets(INPUT_FILE, live_state)

            if live_state.pop('just_reset', False):
                if fig is not None:
                    plt.close(fig)
                fig = ax = mesh = None
                printed_first = False
                reset_echogram_state(live_state)
                print(f"{INPUT_FILE} was truncated (receive.py --fresh?) — resetting live view.")

            if new_packets:
                process_new_packets(new_packets, live_state)
                if fig is None:
                    fig, ax, mesh = init_plot(live_state)
                if not printed_first:
                    print(f"First packet received ({live_state['n_samples']} samples/ping, "
                          f"{live_state['sample_dt_us']} us/sample). Plotting live.")
                    printed_first = True
                redraw(fig, ax, mesh, live_state)

            if fig is not None and not plt.fignum_exists(fig.number):
                break

            plt.pause(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
