"""Entry point: ``python -m emi_worker``."""

from __future__ import annotations

import logging
import os
import sys

from .config import Config, detect_capabilities
from .runner import Worker


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("EMI_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO, which drowns the worker's own output.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = Config.from_env()
    caps = detect_capabilities(cfg)
    Worker(cfg, caps).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
