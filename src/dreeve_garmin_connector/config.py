"""Environment configuration.

Everything the connector can be told to do arrives as an environment variable, and every one of
them is validated here, at construction. A misconfigured container must fail on boot with a
readable message rather than halfway through its first sync cycle.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

SECONDS_PER_UNIT = {"h": 3600, "d": 86400}
RELATIVE_SINCE_PATTERN = re.compile(r"^-?(?P<amount>\d+)(?P<unit>[hd])$")
BOOLEANS = {"true": True, "1": True, "yes": True, "on": True, "false": False, "0": False, "no": False, "off": False}
HTTP_ADDR_DISABLED = "off"


class InvalidConfiguration(Exception):
    """Raised with every problem found, not just the first one: fixing env vars one boot at a time is misery."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("\n".join(["Invalid configuration:", *(f"  - {error}" for error in errors)]))


class FallbackFormat(StrEnum):
    TCX = "tcx"
    GPX = "gpx"
    NONE = "none"


class ConflictPolicy(StrEnum):
    SKIP = "skip"
    OVERWRITE = "overwrite"


class LogFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


@dataclass(frozen=True)
class Since:
    """The lower bound of the very first sync, either an absolute instant or an offset from "now".

    It stays unresolved on purpose: `SINCE=now` must resolve exactly once, on first boot, and then
    live in the ledger as `resolvedSince`. Resolving it here would silently skip every activity
    recorded while the container was down.
    """

    value: datetime | timedelta

    def resolve(self, now: datetime) -> datetime:
        if isinstance(self.value, timedelta):
            return now - self.value
        return self.value


@dataclass(frozen=True)
class Config:
    garmin_email: str
    garmin_password: str | None
    garmin_is_cn: bool
    garmin_tokens: Path
    watch_dir: Path
    state_dir: Path
    since: Since | None
    poll_interval: int
    poll_jitter_pct: int
    lookback_days: int
    max_downloads_per_cycle: int
    download_delay_seconds: float
    activity_types: tuple[str, ...]
    fallback_format: FallbackFormat
    on_conflict: ConflictPolicy
    max_attempts: int
    max_backoff_seconds: int
    allow_password_login: bool
    http_addr: str | None
    max_cycles: int
    log_level: str
    log_format: LogFormat
    dry_run: bool
    dreeve_url: str | None = None
    dreeve_api_key: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        reader = _EnvReader(env)

        dreeve_url = env.get("DREEVE_URL", "").strip().rstrip("/") or None
        dreeve_api_key = reader.optional_secret("DREEVE_API_KEY")
        if bool(dreeve_url) != bool(dreeve_api_key):
            reader.errors.append("DREEVE_URL and DREEVE_API_KEY (or DREEVE_API_KEY_FILE) must be configured together.")
        if dreeve_url:
            try:
                parsed = urlsplit(dreeve_url)
                if (
                    parsed.scheme not in ("http", "https")
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError
            except ValueError:
                reader.errors.append("DREEVE_URL must be an HTTP(S) URL without credentials, query or fragment.")

        password = reader.optional_secret("GARMIN_PASSWORD")
        allow_password_login = reader.flag("ALLOW_PASSWORD_LOGIN", default=False)
        if allow_password_login and password is None:
            reader.errors.append(
                "ALLOW_PASSWORD_LOGIN is enabled but GARMIN_PASSWORD (or GARMIN_PASSWORD_FILE) is not set."
            )

        config = cls(
            garmin_email=reader.required_secret("GARMIN_EMAIL"),
            garmin_password=password,
            garmin_is_cn=reader.flag("GARMIN_IS_CN", default=False),
            garmin_tokens=reader.path("GARMINTOKENS", default="/tokens"),
            watch_dir=reader.path("WATCH_DIR", default="/watch"),
            state_dir=reader.path("STATE_DIR", default="/state"),
            since=reader.since("SINCE"),
            poll_interval=reader.integer("POLL_INTERVAL", default=3600, minimum=1),
            poll_jitter_pct=reader.integer("POLL_JITTER_PCT", default=10, minimum=0, maximum=100),
            lookback_days=reader.integer("LOOKBACK_DAYS", default=7, minimum=0),
            max_downloads_per_cycle=reader.integer("MAX_DOWNLOADS_PER_CYCLE", default=25, minimum=1),
            download_delay_seconds=reader.number("DOWNLOAD_DELAY_SECONDS", default=2.0, minimum=0.0),
            activity_types=reader.csv("ACTIVITY_TYPES"),
            fallback_format=reader.choice("FALLBACK_FORMAT", FallbackFormat, default=FallbackFormat.TCX),
            on_conflict=reader.choice("ON_CONFLICT", ConflictPolicy, default=ConflictPolicy.SKIP),
            max_attempts=reader.integer("MAX_ATTEMPTS", default=5, minimum=1),
            max_backoff_seconds=reader.integer("MAX_BACKOFF_SECONDS", default=21600, minimum=0),
            allow_password_login=allow_password_login,
            http_addr=reader.http_addr("HTTP_ADDR", default="0.0.0.0:8080"),
            max_cycles=reader.integer("MAX_CYCLES", default=0, minimum=0),
            log_level=reader.log_level("LOG_LEVEL", default="info"),
            log_format=reader.choice("LOG_FORMAT", LogFormat, default=LogFormat.TEXT),
            dry_run=reader.flag("DRY_RUN", default=False),
            dreeve_url=dreeve_url,
            dreeve_api_key=dreeve_api_key,
        )

        if reader.errors:
            raise InvalidConfiguration(reader.errors)

        return config


class _EnvReader:
    """Reads and validates one variable at a time, collecting failures instead of raising on the first."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self.errors: list[str] = []

    def required_secret(self, key: str) -> str:
        return self._secret(key, required=True) or ""

    def optional_secret(self, key: str) -> str | None:
        return self._secret(key, required=False)

    def _secret(self, key: str, *, required: bool) -> str | None:
        """Supports the `_FILE` convention so credentials can come from a Docker secret."""
        file_key = f"{key}_FILE"
        inline = self._env.get(key, "").strip()
        secret_file = self._env.get(file_key, "").strip()

        if inline and secret_file:
            self.errors.append(f"{key} and {file_key} are both set; use one or the other.")
            return None

        if secret_file:
            try:
                # A file written by `echo` ends in a newline, which would end up in the login payload.
                contents = Path(secret_file).read_text(encoding="utf-8").strip()
            except OSError as exception:
                self.errors.append(f"{file_key} points at '{secret_file}', which could not be read: {exception}.")
                return None
            if not contents:
                self.errors.append(f"{file_key} points at '{secret_file}', which is empty.")
                return None
            return contents

        if not inline:
            if required:
                self.errors.append(f"{key} is required. Set it, or point {file_key} at a file containing it.")
            return None

        return inline

    def flag(self, key: str, *, default: bool) -> bool:
        raw = self._env.get(key, "").strip().lower()
        if not raw:
            return default
        if raw not in BOOLEANS:
            self.errors.append(f"{key} must be a boolean (true/false), got '{raw}'.")
            return default
        return BOOLEANS[raw]

    def integer(self, key: str, *, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
        raw = self._env.get(key, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{key} must be a whole number, got '{raw}'.")
            return default
        return self._within_bounds(key, value, minimum, maximum, default)

    def number(self, key: str, *, default: float, minimum: float | None = None) -> float:
        raw = self._env.get(key, "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            self.errors.append(f"{key} must be a number, got '{raw}'.")
            return default
        return self._within_bounds(key, value, minimum, None, default)

    def _within_bounds[T: (int, float)](
        self, key: str, value: T, minimum: T | None, maximum: T | None, default: T
    ) -> T:
        if minimum is not None and value < minimum:
            self.errors.append(f"{key} must be {minimum} or higher, got {value}.")
            return default
        if maximum is not None and value > maximum:
            self.errors.append(f"{key} must be {maximum} or lower, got {value}.")
            return default
        return value

    def choice[E: StrEnum](self, key: str, options: type[E], *, default: E) -> E:
        raw = self._env.get(key, "").strip().lower()
        if not raw:
            return default
        try:
            return options(raw)
        except ValueError:
            allowed = "|".join(option.value for option in options)
            self.errors.append(f"{key} must be one of {allowed}, got '{raw}'.")
            return default

    def csv(self, key: str) -> tuple[str, ...]:
        raw = self._env.get(key, "")
        return tuple(item.strip().lower() for item in raw.split(",") if item.strip())

    def path(self, key: str, *, default: str) -> Path:
        return Path(self._env.get(key, "").strip() or default)

    def log_level(self, key: str, *, default: str) -> str:
        raw = self._env.get(key, "").strip().upper()
        if not raw:
            return default.upper()
        if raw not in logging.getLevelNamesMapping():
            allowed = "|".join(name.lower() for name in ("debug", "info", "warning", "error", "critical"))
            self.errors.append(f"{key} must be one of {allowed}, got '{raw.lower()}'.")
            return default.upper()
        return raw

    def http_addr(self, key: str, *, default: str) -> str | None:
        raw = self._env.get(key, "").strip() or default
        if raw.lower() == HTTP_ADDR_DISABLED:
            return None

        host, separator, port = raw.rpartition(":")
        if not separator or not host:
            self.errors.append(f"{key} must be 'host:port' or '{HTTP_ADDR_DISABLED}', got '{raw}'.")
            return default
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            self.errors.append(f"{key} must carry a port between 1 and 65535, got '{raw}'.")
            return default

        return raw

    def since(self, key: str) -> Since | None:
        raw = self._env.get(key, "").strip()
        if not raw:
            return None

        if raw.lower() == "now":
            return Since(timedelta(0))

        relative = RELATIVE_SINCE_PATTERN.match(raw.lower())
        if relative:
            amount = int(relative.group("amount"))
            return Since(timedelta(seconds=amount * SECONDS_PER_UNIT[relative.group("unit")]))

        try:
            instant = datetime.fromisoformat(raw)
        except ValueError:
            self.errors.append(
                f"{key} must be a date (2026-01-01), an ISO instant, a relative offset (-30d, 720h) "
                f"or 'now', got '{raw}'."
            )
            return None

        # A bare date or a naive instant is read as UTC; Garmin timestamps are compared in UTC throughout.
        return Since(instant if instant.tzinfo else instant.replace(tzinfo=UTC))
