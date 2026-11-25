"""Test doubles for the two things the sync cycle cannot own: Garmin, and the passage of time."""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from dreeve_garmin_connector.config import FallbackFormat
from dreeve_garmin_connector.garmin import Activity, NoActivityFile
from dreeve_garmin_connector.window import Window


class StubGarminApi:
    """Stands in for `garminconnect.Garmin` itself, to prove the boundary translates what it returns and raises."""

    def __init__(
        self,
        activities: Sequence[dict[str, Any]] = (),
        archive: bytes = b"",
        error: Exception | None = None,
    ) -> None:
        self._activities = list(activities)
        self._archive = archive
        self._error = error
        self.listings: list[tuple[str, str | None, str | None, str | None]] = []
        self.downloads: list[tuple[str, Any]] = []
        self.weight_requests: list[tuple[str, str]] = []

    def get_activities_by_date(
        self,
        startdate: str,
        enddate: str | None = None,
        activitytype: str | None = None,
        sortorder: str | None = None,
    ) -> list[dict[str, Any]]:
        self.listings.append((startdate, enddate, activitytype, sortorder))
        if self._error is not None:
            raise self._error

        return self._activities

    def download_activity(self, activity_id: str, dl_fmt: Any = None) -> bytes:
        self.downloads.append((activity_id, dl_fmt))
        if self._error is not None:
            raise self._error

        return self._archive

    def get_weigh_ins(self, startdate: str, enddate: str) -> dict[str, Any]:
        self.weight_requests.append((startdate, enddate))
        return {"dateWeightList": []}


class StubGarminSession(StubGarminApi):
    """A `garminconnect.Garmin` instance: the API surface plus the login that produces it."""

    def __init__(
        self,
        activities: Sequence[dict[str, Any]] = (),
        archive: bytes = b"",
        error: Exception | None = None,
        login_error: Exception | None = None,
        **constructed_with: Any,
    ) -> None:
        super().__init__(activities=activities, archive=archive, error=error)
        self.constructed_with = constructed_with
        self.login_error = login_error
        self.logins: list[str | None] = []

    def login(self, tokenstore: str | None = None) -> tuple[None, None]:
        self.logins.append(tokenstore)
        if self.login_error is not None:
            raise self.login_error

        return (None, None)


class StubSessionFactory:
    """Stands in for the `Garmin` class itself, so what is handed to the constructor can be inspected."""

    def __init__(self, login_error: Exception | None = None) -> None:
        self._login_error = login_error
        self.sessions: list[StubGarminSession] = []

    def __call__(self, **kwargs: Any) -> StubGarminSession:
        session = StubGarminSession(login_error=self._login_error, **kwargs)
        self.sessions.append(session)

        return session


class ScriptedFailures:
    """A failure that happens a fixed number of times, or forever."""

    def __init__(self, error: Exception, times: int | None) -> None:
        self._error = error
        self._remaining = times

    def next(self) -> Exception | None:
        if self._remaining is None:
            return self._error
        if self._remaining < 1:
            return None

        self._remaining -= 1

        return self._error


class FakeGarminClient:
    """Stands in for the whole boundary, so a cycle can be driven end to end without an account."""

    def __init__(
        self,
        activities: Sequence[Activity] = (),
        archives: Mapping[str, bytes] | None = None,
        fallbacks: Mapping[str, bytes] | None = None,
    ) -> None:
        self._activities = tuple(activities)
        self._archives = dict(archives or {})
        self._fallbacks = dict(fallbacks or {})
        self._listing_failures: ScriptedFailures | None = None
        self._download_failures: dict[str, ScriptedFailures] = {}
        self.listed: list[tuple[Window, tuple[str, ...]]] = []
        self.downloaded: list[str] = []
        self.fallbacks_downloaded: list[tuple[str, FallbackFormat]] = []
        # Lets a test act the moment a download happens, e.g. ask for a shutdown mid-batch.
        self.on_download: Callable[[str], None] = lambda _activity_id: None

    def fail_listing_with(self, error: Exception, times: int | None = None) -> None:
        self._listing_failures = ScriptedFailures(error, times)

    def fail_download_with(self, activity_id: str, error: Exception, times: int | None = None) -> None:
        self._download_failures[activity_id] = ScriptedFailures(error, times)

    def list_activities(self, window: Window, activity_types: tuple[str, ...] = ()) -> tuple[Activity, ...]:
        self.listed.append((window, activity_types))
        _raise_if_scripted(self._listing_failures)

        return tuple(
            activity
            for activity in self._activities
            if window.start <= activity.start_time_gmt.date() <= window.end
            and (not activity_types or activity.activity_type in activity_types)
        )

    def download_original(self, activity_id: str) -> bytes:
        self.downloaded.append(activity_id)
        self.on_download(activity_id)
        _raise_if_scripted(self._download_failures.get(activity_id))

        if activity_id not in self._archives:
            raise NoActivityFile(f"No archive for activity {activity_id}")

        return self._archives[activity_id]

    def download_fallback(self, activity_id: str, fallback: FallbackFormat) -> bytes:
        self.fallbacks_downloaded.append((activity_id, fallback))
        _raise_if_scripted(self._download_failures.get(activity_id))

        if activity_id not in self._fallbacks:
            raise NoActivityFile(f"No {fallback.value} for activity {activity_id}")

        return self._fallbacks[activity_id]


class FakeClock:
    """Time under the test's control: nothing in this project may sleep for real."""

    def __init__(self, now: datetime) -> None:
        self._now = now
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def _raise_if_scripted(failures: ScriptedFailures | None) -> None:
    error = failures.next() if failures is not None else None
    if error is not None:
        raise error
