"""Write the rule catalogue for the webapp.

The front page lists what the analyzer checks, and the findings panel names each rule. Both
read webapp/src/lib/ruleCatalogue.json, generated from the worker's own catalogue so that the
list a user reads cannot drift from the checks that actually run -- tests/test_rules_config.py
fails if the file is stale.

    python scripts/export_rule_catalogue.py
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

from emi_worker.rules.settings import board_catalogue, catalogue  # noqa: E402

OUT = HERE.parents[2] / "webapp" / "src" / "lib" / "ruleCatalogue.json"
#: The board-wide settings, for the settings view in the app.
BOARD_OUT = OUT.with_name("boardSettings.json")


def render(data: list | None = None) -> str:
    return json.dumps(catalogue() if data is None else data, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    BOARD_OUT.write_text(render(board_catalogue()), encoding="utf-8")
    print(f"wrote {OUT} and {BOARD_OUT.name}")
