"""Broker entrypoint.

Config comes from env only (see relay.load_broker_settings):
AAKAAR_BROKER_TOKEN (required), AAKAAR_BROKER_HOST, AAKAAR_BROKER_PORT,
AAKAAR_BROKER_MAX_SESSIONS, AAKAAR_BROKER_HANDSHAKE_TIMEOUT.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from aakaar_broker.relay import RendezvousBroker, load_broker_settings


async def _serve() -> None:
    broker = RendezvousBroker(load_broker_settings())
    await broker.start()
    try:
        await broker.serve_forever()
    finally:
        await broker.stop()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("AAKAAR_BROKER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    with contextlib.suppress(KeyboardInterrupt):  # pragma: no cover
        asyncio.run(_serve())


if __name__ == "__main__":  # pragma: no cover
    main()
