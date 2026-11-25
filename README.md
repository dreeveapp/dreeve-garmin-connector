<p align="center">
  <img src="https://raw.githubusercontent.com/dreeveapp/dreeve-garmin-connector/refs/heads/master/assets/logo.svg" width="250" alt="Logo" >
</p>

<h1 align="center">Dreeve Garmin Connector</h1>

<p align="center">
<a href="https://raw.githubusercontent.com/dreeveapp/dreeve-garmin-connector/refs/heads/master/LICENSE"><img src="https://img.shields.io/github/license/dreeveapp/dreeve-garmin-connector?color=428f7e&logo=open%20source%20initiative&logoColor=white" alt="License"></a>
<a href="https://github.com/dreeveapp/dreeve-garmin-connector/pkgs/container/dreeve-garmin-connector"><img src="https://img.shields.io/github/v/tag/dreeveapp/dreeve-garmin-connector?sort=semver&logo=docker&logoColor=white&label=image" alt="Image version"></a>
<a href="https://docs.dreeve.app"><img src="https://img.shields.io/badge/docs-docs.dreeve.app-428f7e?logo=readthedocs&logoColor=white" alt="Documentation"></a>
<a href="https://discord.gg/p4zpZyCHNc"><img src="https://img.shields.io/badge/Dreeve-%235865F4?logo=discord&logoColor=%23ffffff&label=%20&labelColor=585858" alt="Discord server"></a>
</p>

---

<h4 align="center">Pulls your activities out of Garmin Connect and drops them into <a href="https://dreeve.app">Dreeve</a>'s watch folder.</h4>

<p align="center">
  <a href="https://docs.dreeve.app">Docs</a> •
  <a href="https://github.com/dreeveapp/dreeve">Dreeve</a> •
  <a href="https://discord.gg/p4zpZyCHNc">Discord</a>
</p>

## 📖 Documentation

Head over to the [documentation](https://docs.dreeve.app) to get started with the installation and
configuration of this connector.



## Optional weight synchronization

Set `DREEVE_URL` and `DREEVE_API_KEY` (or `DREEVE_API_KEY_FILE`), then enable
**Settings → Weight history → Sync weight from Garmin Connect** in Dreeve.
The URL must be reachable from this container, for example `http://app:8080`
when Dreeve shares its Docker network. Use a Dreeve API key, not an MCP key.
This requires a Dreeve version offering `/api/v1/athlete/weights/garmin`.

Weights run in the same activity cycle, using the same polling interval, Garmin
session, first-run date and lookback window. No new activities are needed to sync
new weights. The latest measurement for each Garmin calendar day is sent in kg.
Weight requests include one adjacent calendar day on each side of the activity
window to cover local Garmin dates in every timezone, even with `LOOKBACK_DAYS=0`.
Dreeve retains manual entries and gives them priority over Garmin for the same day.
Turning the switch off stops weight requests and preserves imported history.

Leaving these variables unset preserves activity-only behavior. Dry runs never
write weights. A failed weight delivery, or a shutdown before weights are processed,
leaves the sync window unadvanced so the
measurements can be retried; completed activity downloads remain recorded.
