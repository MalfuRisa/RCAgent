"""Backward-compatible entry point for the real Gateway + mock backend run."""

import asyncio

from .run_mcp_mock_pipeline import main


if __name__ == "__main__":
    asyncio.run(main())
