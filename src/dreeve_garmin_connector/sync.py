"""One sync cycle: window, list, diff, cap, download, deliver, record.

The interesting part is not the happy path, it is what each kind of failure does. Garmin's errors
mean very different things — back off for hours, stop and ask a human, try again next cycle, never
try again — and treating them alike is how an account gets blocked or an activity gets lost. Each
one is handled exactly once, here.

The other rule worth stating: a cycle never downloads more than `MAX_DOWNLOADS_PER_CYCLE`. A first
run against five years of history is hundreds of files, and asking for all of them at once is the
single most reliable way to get rate-limited.
"""

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from dreeve_garmin_connector.archive import ActivityFile, InvalidArchive, extract_fit_files
from dreeve_garmin_connector.config import Config, FallbackFormat
from dreeve_garmin_connector.delivery import DeliveryOutcome, UndeliverableFile, WatchFolder
from dreeve_garmin_connector.garmin import (
    ActivityNotFound,
    AuthenticationFailed,
    ConnectionFailed,
    GarminClient,
    GarminError,
    NoActivityFile,
    RateLimited,
)
from dreeve_garmin_connector.ledger import ActivityStatus, Ledger, LedgerEntry
from dreeve_garmin_connector.weight import WeightSync
from dreeve_garmin_connector.window import Window, chunked, incremental_window, sync_start

logger = logging.getLogger(__name__)


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class CycleResult:
    listed: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    without_file: int = 0
    backlog: int = 0

    def __str__(self) -> str:
        return (
            f"listed {self.listed}, delivered {self.delivered}, no file {self.without_file}, "
            f"skipped {self.skipped}, failed {self.failed}, {self.backlog} waiting for a later cycle"
        )


class Sync:
    def __init__(
        self,
        config: Config,
        ledger: Ledger,
        client: GarminClient,
        watch_folder: WatchFolder,
        clock: Clock,
        should_stop: Callable[[], bool] = lambda: False,
        weight_sync: WeightSync | None = None,
    ) -> None:
        self._config = config
        self._ledger = ledger
        self._client = client
        self._watch_folder = watch_folder
        self._clock = clock
        self._should_stop = should_stop
        self._weight_sync = weight_sync

    @property
    def ledger(self) -> Ledger:
        """Read-only, for reporting. Everything that changes the ledger goes through this class."""
        return self._ledger

    def run_once(self) -> CycleResult:
        """Raises `RateLimited` and `AuthenticationFailed`; every other failure is absorbed per activity."""
        started_at = self._clock.now()
        since = self._ledger.resolve_since(
            sync_start(
                configured=self._config.since,
                already_resolved=self._ledger.resolved_since,
                now=started_at,
            )
        )

        window = incremental_window(
            since=since,
            last_synced_at=self._ledger.last_successful_sync,
            lookback_days=self._config.lookback_days,
            now=started_at,
        )
        if window is None:
            logger.info("SINCE (%s) has not arrived yet; nothing to list", since.isoformat())
            self._ledger.mark_successful_sync(started_at)
            self._flush()
            return CycleResult()

        try:
            listed = self._discover(window, since, started_at)
        except (RateLimited, AuthenticationFailed):
            self._flush()
            raise

        candidates = self._waiting_for_a_download()
        cap = self._config.max_downloads_per_cycle
        batch, backlog = candidates[:cap], candidates[cap:]

        if self._config.dry_run:
            for entry in batch:
                logger.info("Would download activity %s from %s", entry.activity_id, entry.start_time_gmt.date())
            logger.info("Dry run: nothing downloaded, nothing written")
            return CycleResult(listed=listed, backlog=len(candidates))

        if backlog:
            logger.info(
                "Downloading %d of %d waiting activities this cycle; the rest follow next time",
                len(batch),
                len(candidates),
            )

        counted: Counter[ActivityStatus] = Counter()
        try:
            self._download_batch(batch, started_at, counted)
        except (RateLimited, AuthenticationFailed):
            # The ledger still holds everything learned up to here; losing it would mean downloading it again.
            self._flush()
            raise

        weights_interrupted = self._weight_sync is not None and self._should_stop()
        if self._weight_sync is not None and not weights_interrupted:
            try:
                self._weight_sync.run_once(window)
            except GarminError:
                self._flush()
                raise

        if weights_interrupted:
            # Activities have their own ledger entries; weights only have this checkpoint.
            # Keep the previous window until weights have actually been processed.
            logger.info("Stopping before weight sync; keeping the previous successful sync checkpoint")
        else:
            self._ledger.mark_successful_sync(started_at)
        self._flush()

        return CycleResult(
            listed=listed,
            delivered=counted[ActivityStatus.DELIVERED],
            failed=counted[ActivityStatus.FAILED],
            skipped=counted[ActivityStatus.SKIPPED],
            without_file=counted[ActivityStatus.NO_FILE],
            backlog=len(backlog),
        )

    def _discover(self, window: Window, since: datetime, started_at: datetime) -> int:
        """Lists the window and registers anything new. Known-and-settled activities are never reconsidered."""
        pages = chunked(window)
        if len(pages) > 1:
            logger.info("Listing %s in %d pages", window, len(pages))

        activities = {}
        for index, page in enumerate(pages):
            if index:
                self._pause()
            for activity in self._client.list_activities(page, self._config.activity_types):
                activities[activity.activity_id] = activity

        for activity in activities.values():
            known = self._ledger.entry(activity.activity_id)
            if known is not None and known.status.is_terminal:
                continue

            self._ledger.mark_seen(
                activity.activity_id,
                activity.start_time_gmt,
                activity.activity_type,
                started_at,
            )
            if activity.start_time_gmt < since:
                # Listing works in whole days, so the first day can reach back past SINCE itself.
                self._ledger.mark_skipped(activity.activity_id, started_at)

        return len(activities)

    def _waiting_for_a_download(self) -> list[LedgerEntry]:
        """Everything still owed, listed this cycle or not.

        A backlog left by the per-cycle cap outlives the window it was found in: once the first sync
        succeeds the window shrinks to the lookback, so anything older could never be listed again.
        """
        waiting = [
            entry
            for entry in self._ledger.entries()
            if not entry.status.is_terminal and entry.attempts < self._config.max_attempts
        ]

        return sorted(waiting, key=lambda entry: entry.start_time_gmt)

    def _download_batch(self, batch: list[LedgerEntry], started_at: datetime, counted: Counter[ActivityStatus]) -> None:
        for index, entry in enumerate(batch):
            if self._should_stop():
                # Shutdown was asked for. The file in flight is finished, the rest stay pending.
                logger.info("Stopping after %d of %d activities; the rest stay recorded as waiting", index, len(batch))
                return
            if index:
                self._pause()
            counted[self._handle(entry, started_at)] += 1

    def _handle(self, entry: LedgerEntry, started_at: datetime) -> ActivityStatus:
        activity_id = entry.activity_id
        try:
            files = self._files_for(entry)
        except RateLimited:
            # Not this activity's problem, and retrying any of them now only makes it worse.
            raise
        except AuthenticationFailed:
            raise
        except ConnectionFailed as exception:
            logger.warning(
                "Could not reach Garmin for activity %s; trying again next cycle: %s", activity_id, exception
            )
            self._ledger.mark_failed(activity_id, str(exception), started_at)
            return ActivityStatus.FAILED
        except InvalidArchive as exception:
            logger.warning("Activity %s came back unreadable; trying again next cycle: %s", activity_id, exception)
            self._ledger.mark_failed(activity_id, str(exception), started_at)
            return ActivityStatus.FAILED
        # Deliberately broad: one bad activity must not take the rest of the cycle down with it.
        except Exception as exception:
            logger.exception("Unexpected failure on activity %s", activity_id)
            self._ledger.mark_failed(activity_id, str(exception), started_at)
            return ActivityStatus.FAILED

        if not files:
            logger.info("Activity %s has no file to download; recording it as such", activity_id)
            self._ledger.mark_no_file(activity_id, started_at)
            return ActivityStatus.NO_FILE

        try:
            delivered = [file.name for file in files if self._watch_folder.deliver(file) is not DeliveryOutcome.SKIPPED]
        except UndeliverableFile as exception:
            logger.warning("Activity %s could not be delivered; trying again next cycle: %s", activity_id, exception)
            self._ledger.mark_failed(activity_id, str(exception), started_at)
            return ActivityStatus.FAILED

        self._ledger.mark_delivered(activity_id, tuple(file.name for file in files), started_at)
        logger.info("Imported activity %s as %s", activity_id, ", ".join(delivered) or "a file that was already there")

        return ActivityStatus.DELIVERED

    def _files_for(self, entry: LedgerEntry) -> tuple[ActivityFile, ...]:
        try:
            files = extract_fit_files(self._client.download_original(entry.activity_id), entry.activity_id)
        except ActivityNotFound:
            # Deleted in Garmin since it was listed. No format will produce a file for it.
            return ()
        except NoActivityFile:
            files = ()

        if files:
            return files

        return self._fallback_for(entry)

    def _fallback_for(self, entry: LedgerEntry) -> tuple[ActivityFile, ...]:
        """Manually entered and some indoor activities have no FIT, but Dreeve imports TCX and GPX too."""
        fallback = self._config.fallback_format
        if fallback is FallbackFormat.NONE:
            return ()

        logger.info("No FIT for activity %s; asking for %s instead", entry.activity_id, fallback.value)
        self._pause()
        try:
            contents = self._client.download_fallback(entry.activity_id, fallback)
        except NoActivityFile:
            return ()

        return (ActivityFile(name=f"{entry.activity_id}.{fallback.value}", contents=contents),)

    def _pause(self) -> None:
        """Space out requests. Garmin is far more forgiving of a slow client than a busy one."""
        if self._config.download_delay_seconds > 0:
            self._clock.sleep(self._config.download_delay_seconds)

    def _flush(self) -> None:
        if self._config.dry_run:
            return

        self._ledger.flush()
