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

from emi_worker.rules.settings import catalogue  # noqa: E402

OUT = HERE.parents[2] / "webapp" / "src" / "lib" / "ruleCatalogue.json"


def render() -> str:
    return json.dumps(catalogue(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT}")
