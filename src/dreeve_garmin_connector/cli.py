"""Command line entry points."""

import argparse
import json
import logging
import os
import signal
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial

from dreeve_garmin_connector.auth import Authenticator
from dreeve_garmin_connector.config import Config, InvalidConfiguration
from dreeve_garmin_connector.delivery import WatchFolder, WatchFolderUnusable
from dreeve_garmin_connector.garmin import GarminConnectClient, GarminError
from dreeve_garmin_connector.ledger import LEDGER_FILENAME, CorruptLedger, Ledger
from dreeve_garmin_connector.logging_ import Secrets, configure_logging
from dreeve_garmin_connector.loop import SyncLoop, heartbeat_path
from dreeve_garmin_connector.status import UNHEALTHY_AFTER_CYCLES, Status, StatusServer
from dreeve_garmin_connector.sync import Sync, SystemClock
from dreeve_garmin_connector.weight import DreeveWeightClient, WeightSync

EXIT_OK = 0
EXIT_FAILED = 1

logger = logging.getLogger(__name__)


class MfaPromptUnavailable(Exception):
    """Garmin asked for a code and there is nobody at the keyboard to type it."""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dreeve-garmin-connector",
        description="Syncs Garmin Connect activities into Dreeve's watch folder.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Log in to Garmin once, interactively, and store the session.")
    sync_command = commands.add_parser("sync-once", help="Run a single sync cycle and exit.")
    sync_command.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be downloaded, download nothing and leave the ledger untouched.",
    )
    commands.add_parser("run", help="Run cycles on POLL_INTERVAL until stopped. This is what the container does.")
    commands.add_parser("healthcheck", help="Exit 0 while the connector is healthy. Used by Docker's HEALTHCHECK.")
    commands.add_parser("status", help="Print what the running connector is doing.")

    arguments = parser.parse_args(argv)

    try:
        config = Config.from_env(os.environ)
    except InvalidConfiguration as exception:
        # Logging is configured from the very config that just failed, so this one goes straight to stderr.
        print(exception, file=sys.stderr)
        return EXIT_FAILED

    if getattr(arguments, "dry_run", False):
        config = replace(config, dry_run=True)

    secrets = Secrets([config.garmin_email, config.garmin_password, config.dreeve_api_key])
    configure_logging(config.log_level, config.log_format, secrets)

    # argparse has already rejected anything that is not a registered command.
    commands_by_name = {
        "login": login,
        "sync-once": sync_once,
        "run": run,
        "healthcheck": healthcheck,
        "status": status,
    }

    return commands_by_name[arguments.command](config, secrets)


def login(config: Config, secrets: Secrets) -> int:
    try:
        Authenticator(config, secrets).log_in(prompt_mfa=ask_for_mfa_code)
    except (InvalidConfiguration, GarminError, MfaPromptUnavailable) as exception:
        logger.error("Login failed. %s", exception)
        return EXIT_FAILED

    logger.info(
        "Logged in. The session is stored in %s and will be resumed from there; "
        "keep that volume and you should not have to do this again.",
        config.garmin_tokens,
    )

    return EXIT_OK


def sync_once(config: Config, secrets: Secrets) -> int:
    try:
        result = build_sync(config, secrets).run_once()
    except (InvalidConfiguration, GarminError, CorruptLedger, WatchFolderUnusable) as exception:
        logger.error("Sync failed. %s", exception)
        return EXIT_FAILED

    logger.info("Cycle finished: %s", result)

    return EXIT_OK


def run(config: Config, secrets: Secrets) -> int:
    clock = SystemClock()

    try:
        # Fail fast on the two things no amount of waiting will fix. A missing Garmin session is
        # deliberately not one of them: the container stays up, reports unhealthy and says what to run.
        WatchFolder(config.watch_dir, config.on_conflict).prepare()
        Ledger.load(config.state_dir / LEDGER_FILENAME)
    except (WatchFolderUnusable, CorruptLedger) as exception:
        logger.error("Cannot start. %s", exception)
        return EXIT_FAILED

    status = Status(config.poll_interval, clock.now())
    server = StatusServer(config.http_addr, status, clock) if config.http_addr else None
    if server is not None:
        server.start()

    loop = SyncLoop(config, partial(build_sync, config, secrets), status, clock)
    for received in (signal.SIGTERM, signal.SIGINT):
        signal.signal(received, loop.request_stop)

    logger.info("Watching Garmin every %d seconds; delivering into %s", config.poll_interval, config.watch_dir)
    try:
        loop.run()
    finally:
        if server is not None:
            server.stop()

    return EXIT_OK


def healthcheck(config: Config, secrets: Secrets) -> int:  # noqa: ARG001 - every command takes the same arguments
    if config.http_addr is None:
        return _heartbeat_is_recent(config)

    try:
        # urlopen raises for anything that is not a 2xx, so reaching this line means healthy.
        with urllib.request.urlopen(_local_url(config.http_addr, "/healthz"), timeout=5):
            return EXIT_OK
    except (urllib.error.URLError, OSError) as exception:
        print(f"Not healthy: {exception}", file=sys.stderr)
        return EXIT_FAILED


def status(config: Config, secrets: Secrets) -> int:  # noqa: ARG001 - every command takes the same arguments
    if config.http_addr is None:
        print("HTTP_ADDR is off, so there is no status endpoint to ask.", file=sys.stderr)
        return EXIT_FAILED

    try:
        with urllib.request.urlopen(_local_url(config.http_addr, "/status"), timeout=5) as response:
            print(json.dumps(json.loads(response.read()), indent=2))
    except (urllib.error.URLError, OSError) as exception:
        print(f"Could not reach the status endpoint: {exception}", file=sys.stderr)
        return EXIT_FAILED

    return EXIT_OK


def _local_url(http_addr: str, path: str) -> str:
    host, _, port = http_addr.rpartition(":")
    # 0.0.0.0 is an address to listen on, not one to connect to.
    return f"http://{'127.0.0.1' if host in ('0.0.0.0', '::', '') else host}:{port}{path}"


def _heartbeat_is_recent(config: Config) -> int:
    heartbeat = heartbeat_path(config.state_dir)
    try:
        touched_at = datetime.fromtimestamp(heartbeat.stat().st_mtime, UTC)
    except OSError:
        print(f"No heartbeat at {heartbeat} yet.", file=sys.stderr)
        return EXIT_FAILED

    stale_after = timedelta(seconds=config.poll_interval * UNHEALTHY_AFTER_CYCLES)
    if datetime.now(UTC) - touched_at > stale_after:
        print(f"The last successful cycle was at {touched_at.isoformat()}.", file=sys.stderr)
        return EXIT_FAILED

    return EXIT_OK


def build_sync(config: Config, secrets: Secrets, should_stop: Callable[[], bool] = lambda: False) -> Sync:
    watch_folder = WatchFolder(config.watch_dir, config.on_conflict)
    watch_folder.prepare()

    session = Authenticator(config, secrets).resume()
    weight_sync = None
    if config.dreeve_url is not None and config.dreeve_api_key is not None:
        weight_sync = WeightSync(session, DreeveWeightClient(config.dreeve_url, config.dreeve_api_key))

    return Sync(
        config=config,
        ledger=Ledger.load(config.state_dir / LEDGER_FILENAME),
        client=GarminConnectClient(session),
        watch_folder=watch_folder,
        clock=SystemClock(),
        weight_sync=weight_sync,
        should_stop=should_stop,
    )


def ask_for_mfa_code() -> str:
    if not sys.stdin.isatty():
        raise MfaPromptUnavailable(
            "Garmin asked for a multi-factor code, but there is no terminal to ask on. "
            "Run this with `docker compose run --rm garmin-connector login`, which attaches one."
        )

    return input("Garmin sent a multi-factor code. Enter it: ").strip()
