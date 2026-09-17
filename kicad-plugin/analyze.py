"""KiCad action: open the EMI Analyzer on the board in the editor."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from emi_analyzer.app import analyze  # noqa: E402

if __name__ == "__main__":
    sys.exit(analyze())
