"""Run the sonar pipeline end-to-end: record, then review.

Runs receive.py to stream serial data to sensor_stream.csv (stop it with
Ctrl+C), then plot_echogram.py, then plot_matched_filter.py, each waiting
for the previous one to exit before starting the next.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable

STAGES = ['receive.py', 'plot_echogram.py', 'plot_matched_filter.py']


def run_stage(script):
    print(f"\n=== Running {script} (Ctrl+C or close its window to continue) ===")
    # Ctrl+C hits the whole foreground process group, so this process gets
    # the KeyboardInterrupt too, right as the child (e.g. receive.py) is
    # catching its own and exiting cleanly. That's the expected way to end
    # a stage, so swallow it here instead of letting it kill the pipeline.
    try:
        result = subprocess.run([PYTHON, script], cwd=SCRIPT_DIR)
        returncode = result.returncode
    except KeyboardInterrupt:
        returncode = -2

    # -2 (SIGINT) is the expected way to end a stage, so don't treat it as fatal.
    if returncode not in (0, -2):
        print(f"{script} exited with code {returncode}; stopping pipeline.")
        sys.exit(returncode)


def main():
    for script in STAGES:
        run_stage(script)
    print("\nPipeline complete.")


if __name__ == "__main__":
    main()
