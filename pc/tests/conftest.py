import sys
from pathlib import Path

PC_DIR = Path(__file__).resolve().parent.parent
if str(PC_DIR) not in sys.path:
    sys.path.insert(0, str(PC_DIR))
