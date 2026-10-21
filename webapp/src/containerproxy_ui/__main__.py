"""Entry point: run the admin UI with uvicorn."""

from __future__ import annotations

import os

import uvicorn

from .app import create_app


def main() -> None:
    app = create_app()
    uvicorn.run(
        app,
        host=os.environ.get("UI_LISTEN", "0.0.0.0"),
        port=int(os.environ.get("UI_PORT", "8080")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
