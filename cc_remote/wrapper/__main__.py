"""Entry point: `python -m cc_remote.wrapper`."""
from __future__ import annotations

import asyncio

from cc_remote.config import validate_wrapper_config, wrapper_config
from cc_remote.log import logger
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.transport import WrapperTransport
from cc_remote.wrapper.child_env import scrub_parent_control_secrets

log = logger("cc_remote.wrapper")


async def main() -> None:
    cfg = wrapper_config()
    validate_wrapper_config(cfg)
    # cfg now owns the one token transport needs. Remove control credentials from
    # the parent environment before any model/plugin/tool subprocess can inherit.
    scrub_parent_control_secrets()
    log.info("starting wrapper", relay=cfg.relay_url, cwd=cfg.cc_cwd,
             resume=bool(cfg.resume_session_id))
    transport = WrapperTransport(
        cfg.relay_url,
        cfg.wrapper_token,
        inbox_cap=cfg.transport_inbox_cap,
        send_cap=cfg.transport_send_cap,
        max_size=cfg.ws_max_size_bytes,
        inbox_bytes=cfg.transport_inbox_bytes,
        send_bytes=cfg.transport_send_bytes,
    )
    machine = WrapperMachine(cfg, transport)
    try:
        await machine.run()
    except KeyboardInterrupt:
        log.info("shutting down (keyboard interrupt)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
