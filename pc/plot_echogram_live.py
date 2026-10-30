import argparse
import csv
import os
import numpy as np
import matplotlib.pyplot as plt

from plot_sensor_stream import META_COLUMNS, parse_row, range_axis_cm
from plot_matched_filter import make_receive_filter, matched_filter, US_TO_CM
from plot_echogram import MIN_DISPLAY_RANGE_CM, hilbert_envelope

INPUT_FILE = 'sensor_stream.csv'

# receive.py logs one ping every PING_INTERVAL_MS (300ms) in
# firmware/src/main.cpp. Polling well under that keeps pings from visibly
# batching up without any meaningful CPU cost.
POLL_INTERVAL_S = 0.05

# Fixed-width scrolling window, like a real fish-finder, rather than an
# ever-growing image - keeps redraw cost and memory bounded regardless of
# session length. ~60s of history at the 300ms ping interval.
WINDOW_PINGS = 200

# Display in dB below a reference that tracks the strongest recent echo.
# Kept separate from plot_echogram.py's constant of the same name/value since
# a user may reasonably want a different setting for live tuning vs. offline
# review.
DYNAMIC_RANGE_DB = 40

# The reference jumps up at once to a stronger ping (nothing new clips) and
# otherwise relaxes by this much per ping, so after a strong transient (a
# hand in the beam) the display recovers in ~(its excess dB / this) pings:
# 28 dB in ~28 pings (~8 s) at 1 dB. Higher recovers faster but lets the
# brightness pump with ordinary ping-to-ping variation.
REFERENCE_DECAY_DB_PER_PING = 1.0

CM_PER_FOOT = 30.48

# Ring cancellation. The shared transducer rings after every burst and fills
# the near range (to ~65 cm) with returns that are the same on every ping
# (timing scatter ~0.1 us). Each ping has a running mean of recent pings
# subtracted before the matched filter, over the near range only: full weight
# to RING_CANCEL_MAX_CM, a raised-cosine fade to zero over the next
# RING_CANCEL_TAPER_CM, so static targets beyond it stay visible. Anything
# that doesn't move inside it fades within ~RING_CANCEL_PINGS pings (~5 s).
# On 2026-10-29 captures this cleared the near range from ~30 cm instead of
# ~65 cm; 16 pings kept up with the drift after power-on, where 32-64 left a
# residue at 28-32 cm (bench/TEST_LOG.md).
RING_CANCEL_PINGS = 16
RING_CANCEL_MAX_CM = 90
RING_CANCEL_TAPER_CM = 20
MIN_DISPLAY_RANGE_RING_CANCEL_CM = 30
RING_RESET_GAP_US = 2_000_000   # a longer gap between pings restarts the estimate
# A ping whose near range differs from the estimate by more than
# RING_GATE_FACTOR x the usual amount (something moved in) is not folded into
# the estimate, or it would leave an inverted ghost when it moves away. After
# RING_GATE_MAX_SKIPS rejected pings in a row (~12 s) the new level is taken
# as the ring, so real drift can't lock the estimate. On the 2026-10-29
# captures the gate rejected 0-1 of 99 pings; with it, a hand echo that had
# left left no ghost (without: +24 dB over the far floor, fading over ~40 pings).
RING_GATE_FACTOR = 3.0
RING_GATE_MAX_SKIPS = 40


def new_live_state(ring_cancel=True):
    state = {
        'offset': 0,
        'header_seen': False,
        'just_reset': False,
        'ring_cancel': ring_cancel,
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
        'total_pings_seen': 0,
        'reference': 0.0,
        'ring_weights': None,
        'ring_estimate': None,
        'ring_count': 0,
        'ring_typical': None,
        'ring_skips': 0,
        'blank_samples_uncancelled': None,
        'last_t_ping_us': None,
    })


def update_reference(reference, ping_peak, decay_db=REFERENCE_DECAY_DB_PER_PING):
    """Fast-attack, slow-release display reference (see REFERENCE_DECAY_DB_PER_PING)."""
    return max(ping_peak, reference * 10 ** (-decay_db / 20))


def ring_cancel_weights(range_cm, max_cm=RING_CANCEL_MAX_CM, taper_cm=RING_CANCEL_TAPER_CM):
    """Weight of the ring estimate at each range: 1 to max_cm, a raised
    cosine down to 0 at max_cm + taper_cm, 0 beyond."""
    z = np.clip((np.asarray(range_cm, dtype=np.float64) - max_cm) / taper_cm, 0.0, 1.0)
    return 0.5 * (1 + np.cos(np.pi * z))


def update_ring_estimate(estimate, values, count, n_pings=RING_CANCEL_PINGS):
    """Fold ping number `count` (0-based) into the running ring estimate:
    the plain mean of the pings so far until there are n_pings of them, then
    an exponential average with that time constant."""
    values = np.asarray(values, dtype=np.float64)
    if estimate is None:
        return values.copy()
    return estimate + (values - estimate) / min(count + 1, n_pings)


def ring_residual_rms(values, estimate, weights):
    """Weighted RMS of (ping - estimate) over the cancelled range, with the
    DC difference taken out."""
    d = np.asarray(values, dtype=np.float64) - estimate
    d = d - d.mean()
    return float(np.sqrt((weights * d * d).sum() / weights.sum()))


def ring_gate(stat, typical, skips, n_pings=RING_CANCEL_PINGS, max_skips=RING_GATE_MAX_SKIPS):
    """Decide whether a ping with residual `stat` goes into the estimate.
    Returns (accept, typical, skips): `typical` is the running mean residual of
    accepted pings and `skips` the rejected pings in a row."""
    if typical is None:
        return True, stat, 0
    if stat <= RING_GATE_FACTOR * typical:
        return True, typical + (stat - typical) / n_pings, 0
    if skips >= max_skips:
        return True, stat, 0
    return False, typical, skips + 1


def cancel_ring(values, estimate, weights):
    """Subtract the ring estimate where `weights` says. Its mean is taken
    out first, so the fade doesn't put a step of the ADC's DC level into the
    data."""
    values = np.asarray(values, dtype=np.float64)
    return values - weights * (estimate - estimate.mean())


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

    return [parse_row(row) for row in csv.reader(lines)]


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
            live_state['range_cm'] = range_axis_cm(meta, n_samples)
            template = make_receive_filter(sample_dt_us)
            live_state['template'] = template
            min_cm = MIN_DISPLAY_RANGE_RING_CANCEL_CM if live_state['ring_cancel'] else MIN_DISPLAY_RANGE_CM
            blank_cm = max(len(template) * sample_dt_us * US_TO_CM, min_cm)
            live_state['ring_weights'] = ring_cancel_weights(live_state['range_cm'])
            live_state['blank_cm'] = blank_cm
            live_state['blank_samples'] = int(np.count_nonzero(live_state['range_cm'] < blank_cm))
            plain_cm = max(len(template) * sample_dt_us * US_TO_CM, MIN_DISPLAY_RANGE_CM)
            live_state['blank_samples_uncancelled'] = int(np.count_nonzero(live_state['range_cm'] < plain_cm))

        # A pause (receive.py restarted, STOP/START) lets the ring drift, so
        # start the estimate afresh.
        last_t = live_state['last_t_ping_us']
        if last_t is not None and meta['T_ping_us'] - last_t > RING_RESET_GAP_US:
            live_state['ring_estimate'] = None
            live_state['ring_count'] = 0
            live_state['ring_typical'] = None
            live_state['ring_skips'] = 0
        live_state['last_t_ping_us'] = meta['T_ping_us']

        raw = values
        blank_samples = live_state['blank_samples']
        if live_state['ring_cancel']:
            if live_state['ring_estimate'] is not None:
                values = cancel_ring(raw, live_state['ring_estimate'], live_state['ring_weights'])
            else:
                # Nothing to cancel with yet: don't show the uncancelled ring below 40 cm.
                blank_samples = live_state['blank_samples_uncancelled']
        # The estimate is updated with cancellation off too, like the web page's.
        accept = True
        if live_state['ring_estimate'] is not None:
            stat = ring_residual_rms(raw, live_state['ring_estimate'], live_state['ring_weights'])
            accept, live_state['ring_typical'], live_state['ring_skips'] = ring_gate(
                stat, live_state['ring_typical'], live_state['ring_skips'])
        if accept:
            live_state['ring_estimate'] = update_ring_estimate(
                live_state['ring_estimate'], raw, live_state['ring_count'])
            live_state['ring_count'] += 1

        mf = matched_filter(values, live_state['template'])
        env = hilbert_envelope(mf)
        env[:blank_samples] = np.nan
        env[len(env) - len(live_state['template']) + 1:] = np.nan  # the matched filter ran out of samples

        live_state['columns'].append(env)
        live_state['ping_times'].append(meta['T_ping_us'] / 1e6)
        live_state['ping_ids'].append(meta['PingID'])
        live_state['reference'] = update_reference(live_state['reference'], np.nanmax(env))
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
    cbar.set_label("Echo strength (dB re: recent strongest return)")

    fig.tight_layout()
    fig.show()
    return fig, ax, mesh


def redraw(fig, ax, mesh, live_state):
    image = make_window_image(live_state)
    peak = live_state['reference'] if live_state['reference'] > 0 else 1.0
    with np.errstate(divide='ignore', invalid='ignore'):
        db = 20 * np.log10(image / peak)
    mesh.set_array(db.ravel())

    latest_id = live_state['ping_ids'][-1] if live_state['ping_ids'] else '-'
    ring = (f"ring cancel to {RING_CANCEL_MAX_CM} cm" if live_state['ring_cancel'] else "ring cancel off")
    title = (f"Live echogram — {live_state['total_pings_seen']} pings so far, "
             f"latest ping_id={latest_id}, {DYNAMIC_RANGE_DB} dB dynamic range, {ring}")
    ax.set_title(title)
    fig.canvas.draw_idle()


def main():
    ap = argparse.ArgumentParser(description="Live echogram of sensor_stream.csv, run alongside receive.py.")
    ap.add_argument('--no-ring-cancel', action='store_true',
                    help="show the raw near range (don't subtract the running mean of recent pings)")
    args = ap.parse_args()
    print(f"Waiting for {INPUT_FILE}... (start receive.py in another terminal)")
    live_state = new_live_state(ring_cancel=not args.no_ring_cancel)
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
