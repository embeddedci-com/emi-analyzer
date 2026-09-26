"""Write the decoupling view of a small synthetic board for the webapp's tests.

The browser rebuilds every curve from the branches the worker ships (lib/decoupling.ts), so
the two must agree on the gaps and on the ranking of the what-ifs. This is the board they are
compared on -- the two-layer one from tests/test_decoupling_view.py -- and
tests/test_decoupling_view.py fails if the file is stale.

    python scripts/gen_decoupling_fixture.py
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

OUT = HERE.parents[2] / "webapp" / "src" / "lib" / "decouplingFixture.json"


def render() -> str:
    from emi_worker.rules import decoupling_view as dv
    from tests.test_decoupling_view import _two_layer
    from tests.test_layout_checks import ctx_for

    return json.dumps(dv.build(ctx_for(_two_layer())), indent=1, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT}")
