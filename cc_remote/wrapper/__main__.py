"""Entry point: `python -m cc_remote.wrapper`."""
from __future__ import annotations

import asyncio

from cc_remote.config import validate_wrapper_config, wrapper_config
from cc_remote.log import logger
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.transport import WrapperTransport
from cc_remote.wrapper.child_env import scrub_parent_control_secrets
from cc_remote.viewer import registry_path
from cc_remote.viewer_home import HomePages
from cc_remote.wrapper.viewer_transport import ViewerTransport

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
    viewers = asyncio.create_task(ViewerTransport(
        cfg.relay_url, cfg.wrapper_token, cfg.machine_id, registry_path(),
        session_pages=machine.viewer_pages,
        home_pages=HomePages(cfg.state_dir / "viewer-home-pages.json")).run())
    try:
        # The installer verifies Work schemas independently of optional Codex
        # readiness. A slow daemon probe must not look like a failed migration.
        await machine.initialize_work()
        # The official Codex TUI can share a thread only when its durable
        # app-server daemon already owns the control plane.  Prepare it before
        # Relay startup so a terminal opened after this service cannot win a
        # private-writer race and force Web into a read-only mirror.
        await machine.prepare_codex_daemons()
        await machine.run()
    except KeyboardInterrupt:
        log.info("shutting down (keyboard interrupt)")
    finally:
        viewers.cancel()
        await asyncio.gather(viewers, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
