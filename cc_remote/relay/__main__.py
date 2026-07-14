"""Entry point: `python -m cc_remote.relay`."""
from __future__ import annotations

import uvicorn

from cc_remote.config import relay_config, validate_relay_config
from cc_remote.log import logger
from cc_remote.relay.log_safety import uvicorn_log_config
from cc_remote.relay.server import create_app

log = logger("cc_remote.relay")


def main() -> None:
    cfg = relay_config()
    validate_relay_config(cfg)
    app = create_app(cfg)
    log.info("starting relay", host=cfg.host, port=cfg.port)
    # Uvicorn's access log includes the full WebSocket target (including query
    # strings). Keep it disabled even though browser credentials are cookie-only.
    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        log_config=uvicorn_log_config(),
        access_log=False,
        ws_max_size=cfg.ws_max_size_bytes,
        ws_max_queue=2,
    )


if __name__ == "__main__":
    main()
