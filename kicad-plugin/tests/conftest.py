import sys
from pathlib import Path

# The plugin is loaded by KiCad from its own folder, not installed. The tests import it the
# same way the entry script does.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
