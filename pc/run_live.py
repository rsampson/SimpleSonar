#!/usr/bin/env python3
"""Run receive.py and plot_echogram_live.py concurrently.

Unlike run_pipeline.py's sequential stages, these two are meant to run at
the same time: receive.py --fresh streams into a new sensor_stream.csv
while plot_echogram_live.py tails it live. Stop either one (Ctrl+C, or
close the plot window) and this brings the other down too.
"""

import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable


def main():
    print("=== Starting receive.py and plot_echogram_live.py "
          "(Ctrl+C or close the plot window to stop) ===")

    receiver = subprocess.Popen([PYTHON, 'receive.py', '--fresh'], cwd=SCRIPT_DIR)
    plotter = subprocess.Popen([PYTHON, 'plot_echogram_live.py'], cwd=SCRIPT_DIR)

    try:
        while receiver.poll() is None and plotter.poll() is None:
            time.sleep(0.2)
    except KeyboardInterrupt:
        # Ctrl+C hits the whole foreground process group, so receiver
        # already got its own SIGINT and is running its handler, which
        # sends STOP to the ESP32 before closing the serial port (see
        # _install_interrupt_handler()/continuous_serial_to_csv() in
        # receive.py). Sending it SIGTERM right now could cut that
        # cleanup short and leave the ESP32 pinging - so give it a real
        # chance to exit on its own first, and only force it after a
        # timeout in case that handler is ever wedged.
        try:
            receiver.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    # Whichever process is still running (the other one exited on its own,
    # e.g. the plot window was closed, or receive.py was Ctrl+C'd directly)
    # gets the same treatment Ctrl+C would give it, then we wait for both.
    for proc in (receiver, plotter):
        if proc.poll() is None:
            proc.terminate()
    for proc in (receiver, plotter):
        proc.wait()

    print("\nStopped.")


if __name__ == "__main__":
    main()
