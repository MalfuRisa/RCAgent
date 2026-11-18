"""Deprecated command name; delegates to the unified investigation runner."""

import asyncio
import sys
from local_testing.run_investigation import run

if __name__ == "__main__":
    sys.exit(
        0
        if asyncio.run(run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
        else 1
    )
