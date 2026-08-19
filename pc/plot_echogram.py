import sys
import numpy as np
import matplotlib.pyplot as plt

from plot_sensor_stream import load_packets
from plot_matched_filter import make_ping_template, matched_filter, US_TO_CM

INPUT_FILE = 'sensor_stream.csv'

# Below this range the transmitted burst is still ringing in the receiver
# (see make_ping_template's docstring) - the matched filter response there
# is dominated by the pulse's own autocorrelation sidelobes, not real
# echoes, so it's blanked out rather than shown as if it were a return.
MIN_DISPLAY_RANGE_CM = None  # set to a number to override the auto estimate

# Display in dB below the strongest echo in the whole recording. Larger
# values reveal more of the weak-echo/noise floor; smaller values show only
# strong returns.
DYNAMIC_RANGE_DB = 40

CM_PER_FOOT = 30.48


def hilbert_envelope(x):
    """Amplitude envelope of a real signal via the analytic signal.

    The matched-filter output oscillates at the ping's carrier frequency,
    so plotting it directly as color would just show the carrier's fringes
    rather than echo strength. The envelope (the magnitude of the analytic
    signal, i.e. |x + j*Hilbert(x)|) is the smooth quantity a real sonar
    display colorizes. This reimplements scipy.signal.hilbert's FFT method
    directly so the project doesn't need scipy as a dependency for one
    transform.
    """
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    spectrum = np.fft.fft(x)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1.0
        h[1:n // 2] = 2.0
    else:
        h[0] = 1.0
        h[1:(n + 1) // 2] = 2.0
    analytic = np.fft.ifft(spectrum * h)
    return np.abs(analytic)


def build_echogram(packets):
    """Return (image [range x ping], range_cm, ping_times) for imshow."""
    n_samples = len(packets[0][1])
    sample_dt_us = packets[0][0]['SampleDt_us']
    range_cm = np.arange(n_samples) * sample_dt_us * US_TO_CM

    template = make_ping_template(sample_dt_us)
    burst_range_cm = len(template) * sample_dt_us * US_TO_CM
    blank_cm = MIN_DISPLAY_RANGE_CM if MIN_DISPLAY_RANGE_CM is not None else burst_range_cm
    blank_samples = int(np.ceil(blank_cm / (sample_dt_us * US_TO_CM))) if sample_dt_us > 0 else 0

    columns = []
    ping_times = []
    for meta, values in packets:
        tmpl = make_ping_template(meta['SampleDt_us'])
        mf = matched_filter(values, tmpl)
        env = hilbert_envelope(mf)
        env[:blank_samples] = np.nan
        columns.append(env)
        ping_times.append(meta['T_ping_us'] / 1e6)  # seconds, ESP32 boot-relative

    image = np.column_stack(columns)  # shape: (n_samples, n_pings)
    return image, range_cm, np.array(ping_times), blank_cm


def to_db(image):
    finite = image[np.isfinite(image)]
    peak = finite.max() if finite.size else 1.0
    peak = peak if peak > 0 else 1.0
    with np.errstate(divide='ignore', invalid='ignore'):
        db = 20 * np.log10(image / peak)
    return db, peak


def main():
    packets = load_packets(INPUT_FILE)
    if not packets:
        print(f"No packets found in {INPUT_FILE}.")
        sys.exit(1)

    image, range_cm, ping_times_s, blank_cm = build_echogram(packets)
    db, peak = to_db(image)

    print(f"Loaded {len(packets)} pings. Blanking first {blank_cm:.1f} cm "
          f"(transmit burst ringdown). Peak matched-filter envelope: {peak:.1f}")

    t_rel = ping_times_s - ping_times_s[0]

    fig, ax = plt.subplots(figsize=(12, 6))
    mesh = ax.pcolormesh(
        t_rel, range_cm, db,
        shading='nearest',
        cmap='viridis',
        vmin=-DYNAMIC_RANGE_DB, vmax=0,
    )
    ax.invert_yaxis()  # fish-finder convention: shallow/near range at top
    ax.set_xlabel("Time since first ping (s)")
    ax.set_ylabel("Range (cm)")
    ax.set_title(f"Echogram — {len(packets)} pings, {DYNAMIC_RANGE_DB} dB dynamic range")

    ax_ft = ax.secondary_yaxis('right', functions=(lambda cm: cm / CM_PER_FOOT, lambda ft: ft * CM_PER_FOOT))
    ax_ft.set_ylabel("Range (ft)")

    cbar = fig.colorbar(mesh, ax=ax)
    cbar.set_label("Echo strength (dB re: strongest return)")

    fig.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
