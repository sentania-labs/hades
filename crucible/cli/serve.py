"""`crucible serve --all|--api|--supervisor`. Refuses to serve when migrations are not at head."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

import uvicorn

from crucible.adapters.persistence.migrate import is_current
from crucible.cli.wiring import wire
from crucible.logs import configure_logging
from crucible.scheduler.loop import SupervisorLoop
from crucible.settings import load_settings

log = logging.getLogger("crucible.serve")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crucible")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the api and/or the supervisor")
    serve.add_argument("--all", action="store_true", help="api and supervisor in one process")
    serve.add_argument("--api", action="store_true")
    serve.add_argument("--supervisor", action="store_true")
    serve.add_argument("--reload", action="store_true", help="developer mode: reload on change")
    serve.add_argument("--config", default=None, help="TOML configuration file")
    return parser


async def _serve(*, api: bool, supervisor: bool, reload: bool, config: str | None) -> int:
    settings = load_settings(config)
    wiring = wire(settings, role="supervisor" if supervisor else "api")
    ok, detail = is_current(wiring.ctx.engine, settings.database.url)
    if not ok:
        log.error("refusing to serve: migrations not current", extra={"detail": detail})
        return 2
    tasks: list[asyncio.Task[None]] = []
    loop_obj: SupervisorLoop | None = None
    server: uvicorn.Server | None = None
    if supervisor:
        loop_obj = SupervisorLoop(
            wiring.supervisor(), tick_seconds=settings.supervisor.tick_seconds
        )
        tasks.append(asyncio.create_task(loop_obj.run(), name="supervisor"))
    if api:
        server = uvicorn.Server(
            uvicorn.Config(
                wiring.app(),
                host=settings.service.host,
                port=settings.service.port,
                log_config=None,
                access_log=True,
                reload=False,
            )
        )
        tasks.append(asyncio.create_task(server.serve(), name="api"))
    if reload:
        log.warning("--reload is accepted but not implemented in C1; running without reload")

    stop = asyncio.Event()

    def _request_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_stop)
    log.info("crucible serving", extra={"api": api, "supervisor": supervisor})
    await stop.wait()
    log.info("shutdown requested")
    if server is not None:
        server.should_exit = True
    if loop_obj is not None:
        loop_obj.request_stop()
    await asyncio.gather(*tasks, return_exceptions=True)
    return 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    configure_logging(settings.service.log_level)
    api = args.all or args.api
    supervisor = args.all or args.supervisor
    if not (api or supervisor):
        print("nothing to serve: pass --all, --api, or --supervisor", file=sys.stderr)
        sys.exit(2)
    sys.exit(
        asyncio.run(_serve(api=api, supervisor=supervisor, reload=args.reload, config=args.config))
    )


if __name__ == "__main__":
    main()
