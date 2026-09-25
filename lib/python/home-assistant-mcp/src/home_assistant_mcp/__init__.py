from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv


def main() -> None:
    from .config import PROJECT_ROOT

    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("LOG_LEVEL", "INFO").upper())
    from .server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
