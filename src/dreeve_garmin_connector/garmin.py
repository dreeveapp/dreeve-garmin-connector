"""The one place that talks to Garmin.

This is an unofficial API on top of a library that has already had to rewrite its authentication
more than once. Everything it can do is funnelled through the narrow protocol below, so when Garmin
changes something the damage is contained to this file — and so the sync cycle can be tested in
full without an account.

The library's exceptions are translated here too. They are not interchangeable: one of them means
back off for hours, another means stop and ask a human, a third means try again in an hour. Losing
that distinction is how an account gets blocked.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectInvalidFileFormatError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from dreeve_garmin_connector.config import FallbackFormat
from dreeve_garmin_connector.window import Window

GARMIN_DATE_FORMAT = "%Y-%m-%d"
GARMIN_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
# Oldest first, so a backlog drains in chronological order and the per-cycle cap always takes the
# activities that have been waiting longest.
OLDEST_FIRST = "asc"

logger = logging.getLogger(__name__)


class GarminError(Exception):
    """Base for everything this boundary raises, so nothing from the library leaks past it."""


class RateLimited(GarminError):
    """Back off. Not for this one activity — for the whole cycle."""


class AuthenticationFailed(GarminError):
    """Stop. Retrying against an endpoint that just rejected us is what gets an account blocked."""


class ConnectionFailed(GarminError):
    """Transient. The same request is worth making again next cycle."""


class NoActivityFile(GarminError):
    """There is nothing to download, and there never will be. Not a failure."""


class ActivityNotFound(NoActivityFile):
    """The activity was deleted between listing it and fetching it, so no format will produce a file."""


class UnexpectedResponse(GarminError):
    """Garmin answered with something we do not recognise — worth failing loudly over, not guessing at."""


@dataclass(frozen=True)
class Activity:
    activity_id: str
    start_time_gmt: datetime
    activity_type: str
    name: str


class GarminApi(Protocol):
    """The sliver of `garminconnect.Garmin` this connector actually depends on.

    The library ships no type information, so without this everything past the import is `Any`.
    Spelling it out keeps the dependency small enough to read, and breaks loudly when it moves.
    """

    def get_activities_by_date(
        self,
        startdate: str,
        enddate: str | None = ...,
        activitytype: str | None = ...,
        sortorder: str | None = ...,
    ) -> list[dict[str, Any]]: ...

    def download_activity(self, activity_id: str, dl_fmt: Any = ...) -> bytes: ...

    def get_weigh_ins(self, startdate: str, enddate: str) -> dict[str, Any]: ...


@runtime_checkable
class GarminClient(Protocol):
    def list_activities(self, window: Window, activity_types: tuple[str, ...] = ()) -> tuple[Activity, ...]: ...

    def download_original(self, activity_id: str) -> bytes: ...

    def download_fallback(self, activity_id: str, fallback: FallbackFormat) -> bytes: ...


class GarminConnectClient:
    """The only implementation, and the only code in this project that touches `garminconnect`."""

    def __init__(self, api: GarminApi) -> None:
        self._api = api

    def list_activities(self, window: Window, activity_types: tuple[str, ...] = ()) -> tuple[Activity, ...]:
        # Garmin filters by one type at a time. Several are asked for in full and filtered here, so
        # ACTIVITY_TYPES behaves the same either way.
        requested_type = activity_types[0] if len(activity_types) == 1 else None

        with translated_errors(f"listing activities for {window}"):
            payloads = self._api.get_activities_by_date(
                window.start.strftime(GARMIN_DATE_FORMAT),
                window.end.strftime(GARMIN_DATE_FORMAT),
                requested_type,
                OLDEST_FIRST,
            )

        activities = tuple(_activity_from(payload) for payload in payloads)
        if not activity_types:
            return activities

        return tuple(activity for activity in activities if activity.activity_type in activity_types)

    def download_original(self, activity_id: str) -> bytes:
        with translated_errors(f"downloading activity {activity_id}"):
            return self._api.download_activity(activity_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)

    def download_fallback(self, activity_id: str, fallback: FallbackFormat) -> bytes:
        if fallback is FallbackFormat.NONE:
            raise ValueError("FALLBACK_FORMAT is 'none'; there is no fallback to download.")

        download_format = {
            FallbackFormat.TCX: Garmin.ActivityDownloadFormat.TCX,
            FallbackFormat.GPX: Garmin.ActivityDownloadFormat.GPX,
        }[fallback]

        with translated_errors(f"downloading activity {activity_id} as {fallback.value}"):
            return self._api.download_activity(activity_id, dl_fmt=download_format)


@contextmanager
def translated_errors(what: str) -> Iterator[None]:
    try:
        yield
    except GarminConnectTooManyRequestsError as exception:
        raise RateLimited(f"Garmin rate-limited {what}: {exception}") from exception
    except GarminConnectAuthenticationError as exception:
        raise AuthenticationFailed(f"Garmin rejected the session while {what}: {exception}") from exception
    # Before the connection clause on purpose: the library derives "not found" from "connection
    # failed", so the wider clause would otherwise turn a deleted activity into an endless retry.
    except GarminConnectNotFoundError as exception:
        raise ActivityNotFound(f"Garmin no longer knows about this activity while {what}: {exception}") from exception
    except GarminConnectConnectionError as exception:
        raise ConnectionFailed(f"Could not reach Garmin while {what}: {exception}") from exception
    except GarminConnectInvalidFileFormatError as exception:
        raise NoActivityFile(f"Garmin has no file for this activity while {what}: {exception}") from exception


def _activity_from(payload: Any) -> Activity:
    try:
        return Activity(
            activity_id=str(payload["activityId"]),
            start_time_gmt=_timestamp(payload["startTimeGMT"]),
            activity_type=str(payload["activityType"]["typeKey"]),
            name=str(payload.get("activityName") or ""),
        )
    except (KeyError, TypeError, ValueError, IndexError) as exception:
        raise UnexpectedResponse(
            f"Garmin returned an activity this connector cannot read ({exception}). "
            f"This usually means the API changed; check for a newer connector."
        ) from exception


def _timestamp(raw: Any) -> datetime:
    """Garmin sends GMT timestamps without saying so: '2026-07-25 06:13:42'."""
    if not isinstance(raw, str):
        raise TypeError(f"expected a timestamp, got {type(raw).__name__}")

    try:
        naive = datetime.strptime(raw, GARMIN_TIMESTAMP_FORMAT)  # noqa: DTZ007 - the format carries no zone
    except ValueError:
        naive = datetime.fromisoformat(raw)

    return naive.replace(tzinfo=UTC) if naive.tzinfo is None else naive.astimezone(UTC)
