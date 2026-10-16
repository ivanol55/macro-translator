"""Start the app: python -m macro_translator (from the backend folder or with PYTHONPATH).

APP_HOST is 127.0.0.1 by default: only this machine can open the app.
Set APP_HOST=0.0.0.0 to open it from other devices on your network.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "macro_translator.main:app_factory",
        factory=True,
        host=os.environ.get("APP_HOST", "127.0.0.1"),
        port=int(os.environ.get("APP_PORT", "8000")),
        server_header=False,
        log_level="debug" if os.environ.get("DEBUG") == "1" else "info",
    )


if __name__ == "__main__":
    main()
