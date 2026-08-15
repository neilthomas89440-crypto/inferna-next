"""Worker entry point: `python -m inferna_worker`."""

from __future__ import annotations

import asyncio

from inferna_worker.main import main

if __name__ == "__main__":
    asyncio.run(main())
