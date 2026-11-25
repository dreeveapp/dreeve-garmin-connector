from datetime import UTC, datetime
from pathlib import Path

import pytest

from dreeve_garmin_connector.config import (
    Config,
    ConflictPolicy,
    FallbackFormat,
    InvalidConfiguration,
    LogFormat,
)

MINIMAL_ENV = {"GARMIN_EMAIL": "rider@example.com"}
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def test_optional_dreeve_weight_destination() -> None:
    config = Config.from_env({**MINIMAL_ENV, "DREEVE_URL": "http://dreeve:8080/", "DREEVE_API_KEY": "secret"})
    assert config.dreeve_url == "http://dreeve:8080"
    assert config.dreeve_api_key == "secret"
    assert Config.from_env(MINIMAL_ENV).dreeve_url is None


@pytest.mark.parametrize(
    "values",
    [
        {"DREEVE_URL": "http://dreeve:8080"},
        {"DREEVE_API_KEY": "secret"},
        {"DREEVE_URL": "file:///tmp/dreeve", "DREEVE_API_KEY": "secret"},
        {"DREEVE_URL": "https://user:pass@example.com", "DREEVE_API_KEY": "secret"},
    ],
)
def test_invalid_dreeve_destinations_are_rejected(values: dict[str, str]) -> None:
    with pytest.raises(InvalidConfiguration):
        Config.from_env({**MINIMAL_ENV, **values})


def test_it_applies_the_documented_defaults() -> None:
    config = Config.from_env(MINIMAL_ENV)

    assert config.garmin_email == "rider@example.com"
    assert config.garmin_password is None
    assert config.garmin_is_cn is False
    assert config.garmin_tokens == Path("/tokens")
    assert config.watch_dir == Path("/watch")
    assert config.state_dir == Path("/state")
    assert config.since is None
    assert config.poll_interval == 3600
    assert config.poll_jitter_pct == 10
    assert config.lookback_days == 7
    assert config.max_downloads_per_cycle == 25
    assert config.download_delay_seconds == 2.0
    assert config.activity_types == ()
    assert config.fallback_format is FallbackFormat.TCX
    assert config.on_conflict is ConflictPolicy.SKIP
    assert config.max_attempts == 5
    assert config.max_backoff_seconds == 21600
    assert config.allow_password_login is False
    assert config.http_addr == "0.0.0.0:8080"
    assert config.max_cycles == 0
    assert config.log_level == "INFO"
    assert config.log_format is LogFormat.TEXT
    assert config.dry_run is False


def test_it_reads_every_variable_from_the_environment() -> None:
    config = Config.from_env(
        {
            "GARMIN_EMAIL": "rider@example.com",
            "GARMIN_PASSWORD": "s3cret",
            "GARMIN_IS_CN": "true",
            "GARMINTOKENS": "/var/tokens",
            "WATCH_DIR": "/srv/watch",
            "STATE_DIR": "/srv/state",
            "SINCE": "2026-01-01",
            "POLL_INTERVAL": "900",
            "POLL_JITTER_PCT": "25",
            "LOOKBACK_DAYS": "14",
            "MAX_DOWNLOADS_PER_CYCLE": "5",
            "DOWNLOAD_DELAY_SECONDS": "0.5",
            "ACTIVITY_TYPES": "cycling,running",
            "FALLBACK_FORMAT": "gpx",
            "ON_CONFLICT": "overwrite",
            "MAX_ATTEMPTS": "3",
            "MAX_BACKOFF_SECONDS": "600",
            "ALLOW_PASSWORD_LOGIN": "true",
            "HTTP_ADDR": "127.0.0.1:9000",
            "MAX_CYCLES": "2",
            "LOG_LEVEL": "debug",
            "LOG_FORMAT": "json",
            "DRY_RUN": "yes",
        }
    )

    assert config.garmin_password == "s3cret"
    assert config.garmin_is_cn is True
    assert config.garmin_tokens == Path("/var/tokens")
    assert config.watch_dir == Path("/srv/watch")
    assert config.state_dir == Path("/srv/state")
    assert config.since is not None
    assert config.since.resolve(NOW) == datetime(2026, 1, 1, tzinfo=UTC)
    assert config.poll_interval == 900
    assert config.poll_jitter_pct == 25
    assert config.lookback_days == 14
    assert config.max_downloads_per_cycle == 5
    assert config.download_delay_seconds == 0.5
    assert config.activity_types == ("cycling", "running")
    assert config.fallback_format is FallbackFormat.GPX
    assert config.on_conflict is ConflictPolicy.OVERWRITE
    assert config.max_attempts == 3
    assert config.max_backoff_seconds == 600
    assert config.allow_password_login is True
    assert config.http_addr == "127.0.0.1:9000"
    assert config.max_cycles == 2
    assert config.log_level == "DEBUG"
    assert config.log_format is LogFormat.JSON
    assert config.dry_run is True


@pytest.mark.parametrize("key", ["GARMIN_EMAIL", "GARMIN_PASSWORD"])
def test_it_reads_credentials_from_a_secret_file(key: str, tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    # Trailing newline included on purpose: `echo secret > file` is how these get written.
    secret_file.write_text("from-a-file\n", encoding="utf-8")

    env = {**MINIMAL_ENV, f"{key}_FILE": str(secret_file)}
    env.pop(key, None)

    config = Config.from_env(env)

    assert {"GARMIN_EMAIL": config.garmin_email, "GARMIN_PASSWORD": config.garmin_password}[key] == "from-a-file"


def test_it_rejects_a_credential_given_both_inline_and_as_a_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("from-a-file", encoding="utf-8")

    with pytest.raises(InvalidConfiguration) as raised:
        Config.from_env({"GARMIN_EMAIL": "rider@example.com", "GARMIN_EMAIL_FILE": str(secret_file)})

    assert raised.value.errors == ("GARMIN_EMAIL and GARMIN_EMAIL_FILE are both set; use one or the other.",)


def test_it_rejects_a_secret_file_that_cannot_be_read(tmp_path: Path) -> None:
    missing = tmp_path / "nope"

    with pytest.raises(InvalidConfiguration) as raised:
        Config.from_env({"GARMIN_EMAIL_FILE": str(missing)})

    assert "GARMIN_EMAIL_FILE points at" in raised.value.errors[0]
    assert str(missing) in raised.value.errors[0]


def test_it_rejects_an_empty_secret_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text("   \n", encoding="utf-8")

    with pytest.raises(InvalidConfiguration) as raised:
        Config.from_env({"GARMIN_EMAIL_FILE": str(secret_file)})

    assert "which is empty" in raised.value.errors[0]


def test_it_requires_an_email_and_names_both_ways_to_provide_it() -> None:
    with pytest.raises(InvalidConfiguration) as raised:
        Config.from_env({})

    assert raised.value.errors == (
        "GARMIN_EMAIL is required. Set it, or point GARMIN_EMAIL_FILE at a file containing it.",
    )


def test_it_rejects_password_login_without_a_password() -> None:
    with pytest.raises(InvalidConfiguration) as raised:
        Config.from_env({**MINIMAL_ENV, "ALLOW_PASSWORD_LOGIN": "true"})

    assert raised.value.errors == (
        "ALLOW_PASSWORD_LOGIN is enabled but GARMIN_PASSWORD (or GARMIN_PASSWORD_FILE) is not set.",
    )


def test_it_reports_every_problem_at_once() -> None:
    with pytest.raises(InvalidConfiguration) as raised:
        Config.from_env({"POLL_INTERVAL": "nope", "FALLBACK_FORMAT": "fit", "HTTP_ADDR": "8080"})

    assert len(raised.value.errors) == 4
    assert "Invalid configuration:" in str(raised.value)


@pytest.mark.parametrize(
    ("key", "value", "expected_message"),
    [
        ("POLL_INTERVAL", "nope", "POLL_INTERVAL must be a whole number, got 'nope'."),
        ("POLL_INTERVAL", "0", "POLL_INTERVAL must be 1 or higher, got 0."),
        ("POLL_JITTER_PCT", "101", "POLL_JITTER_PCT must be 100 or lower, got 101."),
        ("LOOKBACK_DAYS", "-1", "LOOKBACK_DAYS must be 0 or higher, got -1."),
        ("MAX_DOWNLOADS_PER_CYCLE", "0", "MAX_DOWNLOADS_PER_CYCLE must be 1 or higher, got 0."),
        ("DOWNLOAD_DELAY_SECONDS", "-1", "DOWNLOAD_DELAY_SECONDS must be 0.0 or higher, got -1.0."),
        ("DOWNLOAD_DELAY_SECONDS", "soon", "DOWNLOAD_DELAY_SECONDS must be a number, got 'soon'."),
        ("MAX_ATTEMPTS", "0", "MAX_ATTEMPTS must be 1 or higher, got 0."),
        ("MAX_CYCLES", "-1", "MAX_CYCLES must be 0 or higher, got -1."),
        ("FALLBACK_FORMAT", "fit", "FALLBACK_FORMAT must be one of tcx|gpx|none, got 'fit'."),
        ("ON_CONFLICT", "merge", "ON_CONFLICT must be one of skip|overwrite, got 'merge'."),
        ("LOG_FORMAT", "xml", "LOG_FORMAT must be one of text|json, got 'xml'."),
        ("LOG_LEVEL", "chatty", "LOG_LEVEL must be one of debug|info|warning|error|critical, got 'chatty'."),
        ("DRY_RUN", "maybe", "DRY_RUN must be a boolean (true/false), got 'maybe'."),
        ("HTTP_ADDR", "8080", "HTTP_ADDR must be 'host:port' or 'off', got '8080'."),
        ("HTTP_ADDR", "0.0.0.0:nope", "HTTP_ADDR must carry a port between 1 and 65535, got '0.0.0.0:nope'."),
        ("HTTP_ADDR", "0.0.0.0:70000", "HTTP_ADDR must carry a port between 1 and 65535, got '0.0.0.0:70000'."),
        (
            "SINCE",
            "yesterdayish",
            "SINCE must be a date (2026-01-01), an ISO instant, a relative offset (-30d, 720h) "
            "or 'now', got 'yesterdayish'.",
        ),
    ],
)
def test_it_rejects_an_unusable_value_with_a_message_naming_the_variable(
    key: str, value: str, expected_message: str
) -> None:
    with pytest.raises(InvalidConfiguration) as raised:
        Config.from_env({**MINIMAL_ENV, key: value})

    assert raised.value.errors == (expected_message,)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("now", NOW),
        ("-30d", datetime(2026, 6, 25, 12, 0, tzinfo=UTC)),
        ("30d", datetime(2026, 6, 25, 12, 0, tzinfo=UTC)),
        ("720h", datetime(2026, 6, 25, 12, 0, tzinfo=UTC)),
        ("2026-01-01", datetime(2026, 1, 1, tzinfo=UTC)),
        ("2026-01-01T06:13:42+00:00", datetime(2026, 1, 1, 6, 13, 42, tzinfo=UTC)),
        ("2026-01-01T06:13:42Z", datetime(2026, 1, 1, 6, 13, 42, tzinfo=UTC)),
        ("2026-01-01T06:13:42", datetime(2026, 1, 1, 6, 13, 42, tzinfo=UTC)),
    ],
)
def test_it_understands_every_documented_since_format(value: str, expected: datetime) -> None:
    config = Config.from_env({**MINIMAL_ENV, "SINCE": value})

    assert config.since is not None
    assert config.since.resolve(NOW) == expected


def test_it_leaves_since_now_unresolved_so_a_restart_does_not_skip_activities() -> None:
    config = Config.from_env({**MINIMAL_ENV, "SINCE": "now"})
    later = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    assert config.since is not None
    assert config.since.resolve(NOW) != config.since.resolve(later)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("cycling,running", ("cycling", "running")),
        (" Cycling , RUNNING ", ("cycling", "running")),
        ("cycling,,", ("cycling",)),
        ("", ()),
    ],
)
def test_it_parses_the_activity_type_filter(value: str, expected: tuple[str, ...]) -> None:
    assert Config.from_env({**MINIMAL_ENV, "ACTIVITY_TYPES": value}).activity_types == expected


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("1", True), ("on", True), ("false", False)])
def test_it_accepts_the_usual_boolean_spellings(value: str, expected: bool) -> None:
    assert Config.from_env({**MINIMAL_ENV, "DRY_RUN": value}).dry_run is expected


@pytest.mark.parametrize("value", ["off", "OFF"])
def test_it_treats_http_addr_off_as_no_status_server(value: str) -> None:
    assert Config.from_env({**MINIMAL_ENV, "HTTP_ADDR": value}).http_addr is None
