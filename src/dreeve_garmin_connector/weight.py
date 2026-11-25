"""Optional weigh-in delivery to Dreeve, using the activity cycle and existing Garmin session."""

import json
import logging
import math
import urllib.error
import urllib.request
from datetime import date
from typing import Any, Protocol

from dreeve_garmin_connector.garmin import GarminApi, GarminError, translated_errors
from dreeve_garmin_connector.window import Window, chunked

logger = logging.getLogger(__name__)


class WeightSyncError(GarminError):
    """The optional weight delivery failed; settled activity downloads remain in the ledger."""


class WeightDestination(Protocol):
    def is_enabled(self) -> bool: ...

    def deliver(self, weights: list[dict[str, str | float]]) -> None: ...


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # Never forward the Dreeve bearer token to a redirect target.
        del req, fp, code, msg, headers, newurl
        raise WeightSyncError("Dreeve weight endpoint redirected; configure its final URL.")


class DreeveWeightClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self._url = base_url.rstrip("/") + "/api/v1/athlete/weights/garmin"
        self._api_key = api_key
        self._opener = urllib.request.build_opener(NoRedirects())

    def is_enabled(self) -> bool:
        response = self._request(None)
        if not isinstance(response.get("enabled"), bool):
            raise WeightSyncError("Dreeve weight endpoint returned an invalid sync setting.")
        return response["enabled"] is True

    def deliver(self, weights: list[dict[str, str | float]]) -> None:
        response = self._request({"weights": weights})
        if response.get("imported") != len(weights):
            raise WeightSyncError("Dreeve did not acknowledge all weigh-ins.")

    def _request(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST" if payload is not None else "GET",
        )
        try:
            with self._opener.open(request, timeout=15) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise WeightSyncError("Dreeve weight response was too large.")
            decoded: Any = json.loads(raw)
        except urllib.error.HTTPError as exception:
            raise WeightSyncError(f"Dreeve weight endpoint returned HTTP {exception.code}.") from exception
        except (urllib.error.URLError, OSError, ValueError) as exception:
            raise WeightSyncError("Could not read the Dreeve weight endpoint.") from exception
        if not isinstance(decoded, dict):
            raise WeightSyncError("Dreeve weight endpoint returned an invalid response.")
        return decoded


class WeightSync:
    def __init__(self, api: GarminApi, destination: WeightDestination) -> None:
        self._api = api
        self._destination = destination

    def run_once(self, window: Window) -> None:
        if not self._destination.is_enabled():
            return
        imported = 0
        for page in chunked(window):
            with translated_errors("reading Garmin weigh-ins"):
                response = self._api.get_weigh_ins(page.start.isoformat(), page.end.isoformat())
            weights = latest_daily_weights(response, page)
            if weights:
                self._destination.deliver(weights)
                imported += len(weights)
        logger.info("Weight sync finished: delivered %d daily weigh-ins", imported)


def latest_daily_weights(response: dict[str, Any], window: Window) -> list[dict[str, str | float]]:
    """Garmin returns grams and local calendar dates; the latest measurement wins within each day."""
    entries = response.get("dateWeightList")
    if entries is None:
        summaries = response.get("dailyWeightSummaries")
        if not isinstance(summaries, list):
            raise WeightSyncError("Garmin returned an unrecognized weigh-in response.")
        entries = []
        for summary in summaries:
            if not isinstance(summary, dict):
                raise WeightSyncError("Garmin returned an invalid daily weight summary.")
            measurements = summary.get("allWeightMetrics")
            if measurements is None:
                last = summary.get("latestWeight")
                measurements = [] if last is None else [last]
            if not isinstance(measurements, list):
                raise WeightSyncError("Garmin returned an invalid daily weigh-in list.")
            for measurement in measurements:
                if isinstance(measurement, dict):
                    entries.append({"calendarDate": summary.get("summaryDate"), **measurement})
    if not isinstance(entries, list):
        raise WeightSyncError("Garmin returned an invalid weigh-in list.")
    latest: dict[str, tuple[float, float]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            on = date.fromisoformat(entry["calendarDate"])
            grams = float(entry["weight"])
            timestamp = float(entry.get("date", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if not window.start <= on <= window.end or not math.isfinite(grams) or grams <= 0:
            continue
        if not math.isfinite(timestamp):
            continue
        key = on.isoformat()
        if key not in latest or timestamp >= latest[key][0]:
            latest[key] = (timestamp, round(grams / 1000, 3))
    return [{"on": on, "weightKg": value[1]} for on, value in sorted(latest.items())]
