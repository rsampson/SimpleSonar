import csv
import sys
import numpy as np
import matplotlib.pyplot as plt

from plot_sensor_stream import load_packets, META_COLUMNS

INPUT_FILE = 'sensor_stream.csv'

# Must match the chirp configuration in firmware/src/main.cpp
# (PING_CHIRP_START_HZ / PING_CHIRP_END_HZ / PING_CHIRP_STEPS /
# PING_CHIRP_CYCLES_PER_STEP).
PING_CHIRP_START_HZ = 2640
PING_CHIRP_END_HZ = 3960
PING_CHIRP_STEPS = 5
PING_CHIRP_CYCLES_PER_STEP = 4
PING_CYCLES = PING_CHIRP_STEPS * PING_CHIRP_CYCLES_PER_STEP

# Speed of sound in air at sea level, 75 F (23.9 C): c = 331.3 + 0.606 * T_celsius
SPEED_OF_SOUND_M_S = 331.3 + 0.606 * 23.9
US_TO_CM = (SPEED_OF_SOUND_M_S * 100 / 1e6) / 2  # one-way range per microsecond of time-of-flight


def make_ping_template(sample_dt_us):
    """Reconstruct the transmitted burst as sampled at the receiver's rate.

    The firmware drives PING_PIN_A/B as a free-running LEDC square wave
    (~50% duty), stepping PING_CHIRP_STEPS discrete frequencies from
    PING_CHIRP_START_HZ to PING_CHIRP_END_HZ and holding each for
    PING_CHIRP_CYCLES_PER_STEP cycles (see emit_differential_burst() in
    firmware/src/main.cpp) - a stepped chirp rather than one fixed tone, so
    the matched filter's autocorrelation peak comes out sharper than a
    single-tone pulse of the same length. ledcChangeFrequency() reconfigures
    the LEDC timer for each step (not a phase-continuous glide), so each
    step is reconstructed starting at phase 0, same as the hardware.
    That gated, stepped square wave, not an idealized chirp sinusoid, is the
    actual matched-filter reference: it is what left the transducer
    electrically before the channel and transducer bandlimited it on the
    way back.
    """
    dt_s = sample_dt_us * 1e-6
    freqs = [
        PING_CHIRP_START_HZ + (PING_CHIRP_END_HZ - PING_CHIRP_START_HZ) * step // (PING_CHIRP_STEPS - 1)
        for step in range(PING_CHIRP_STEPS)
    ]

    step_segments = []
    for freq in freqs:
        step_duration_s = PING_CHIRP_CYCLES_PER_STEP / freq
        n_step = max(int(round(step_duration_s / dt_s)), 1)
        t = np.arange(n_step) * dt_s
        segment = np.sign(np.sin(2 * np.pi * freq * t))
        segment[segment == 0] = 1.0
        step_segments.append(segment)

    template = np.concatenate(step_segments)
    # Zero-mean so correlation isn't biased by the reference's DC content.
    return template - template.mean()


def matched_filter(values, template):
    """Cross-correlate the recorded signal against the ping template.

    This is convolution with the time-reversed template
    (correlate(x, h) == convolve(x, h[::-1])), which is the standard sonar
    "pulse compression" matched filter: it maximizes SNR for a known
    transmitted waveform in additive noise and collapses each echo of the
    burst into a single sharp peak at the round-trip delay, rather than
    smearing it the way a literal convolution against the pulse would.
    """
    x = np.asarray(values, dtype=np.float64)
    x = x - x.mean()
    n, L = len(x), len(template)
    # 'full' output index k holds lag (k - (L-1)); we want lag=0 (template
    # aligned to start at x[0]) to land at output index 0, so the peak's
    # index in the trimmed result matches the sample index in x where the
    # echo begins - keeping this on the same range axis as the raw plot.
    result = np.correlate(x, template, mode='full')
    return result[L - 1:L - 1 + n]


def load_first_sample_dt(path):
    with open(path, newline='') as f:
        reader = csv.reader(f)
        header = next(reader)
        row = next(reader)
        meta = dict(zip(META_COLUMNS, row[:len(META_COLUMNS)]))
        return int(meta['SampleDt_us'])


def main():
    packets = load_packets(INPUT_FILE)
    if not packets:
        print(f"No packets found in {INPUT_FILE}.")
        sys.exit(1)

    state = {'idx': 0}

    fig, (ax_raw, ax_mf) = plt.subplots(2, 1, figsize=(10, 8), sharex=False)

    meta0, values0 = packets[0]
    template = make_ping_template(meta0['SampleDt_us'])
    burst_range_cm = len(template) * meta0['SampleDt_us'] * US_TO_CM
    print(f"Ping template: {len(template)} samples covering the "
          f"{PING_CYCLES}-cycle chirp ({PING_CHIRP_START_HZ}-{PING_CHIRP_END_HZ} Hz, "
          f"{PING_CHIRP_STEPS} steps) "
          f"(~{burst_range_cm:.1f} cm of range resolution loss near t=0).")

    line_raw, = ax_raw.plot([], [])
    ax_raw.set_ylabel("Amplitude (ADC counts)")
    ax_raw.set_title("Raw received signal")

    line_mf, = ax_mf.plot([], [])
    ax_mf.set_xlabel("Range (cm)")
    ax_mf.set_ylabel("Matched-filter response")
    ax_mf.set_title("Cross-correlation with transmitted ping (pulse compression)")

    def render():
        idx = state['idx']
        meta, values = packets[idx]
        sample_dt_us = meta['SampleDt_us']
        tmpl = make_ping_template(sample_dt_us)

        n = len(values)
        range_cm = np.arange(n) * sample_dt_us * US_TO_CM

        line_raw.set_data(range_cm, values)
        ax_raw.set_xlim(0, range_cm[-1])
        ax_raw.relim()
        ax_raw.autoscale_view(scalex=False, scaley=True)

        mf = matched_filter(values, tmpl)
        line_mf.set_data(range_cm, mf)
        ax_mf.set_xlim(0, range_cm[-1])
        ax_mf.relim()
        ax_mf.autoscale_view(scalex=False, scaley=True)

        title = f"Packet {idx + 1} / {len(packets)} — {meta['Timestamp']}   ping_id={meta['PingID']}"
        if idx > 0:
            prev_meta, _ = packets[idx - 1]
            dt_ping_ms = (meta['T_ping_us'] - prev_meta['T_ping_us']) / 1000.0
            title += f"   Δt_ping={dt_ping_ms:.1f} ms"
        title += f"   sample_dt={sample_dt_us} us   (←/→ to navigate)"
        fig.suptitle(title)
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
