# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All commands use `uv` (Python package/runtime manager). No build step required.

```bash
# Discover iCloud calendar names (required before first use)
uv run calendar_digest.py --list-calendars
uv run calendar_digest.py --list-calendars -v   # with CalDAV/HTTP debug logs

# Generate preview HTML without sending email
uv run calendar_digest.py --preview             # writes preview.html

# Send the digest email
uv run calendar_digest.py

# Verbose mode (DEBUG logging)
uv run calendar_digest.py --verbose
uv run calendar_digest.py --preview --verbose
```

There are no tests or linting configurations in this project.

## Architecture

Single-file application (`calendar_digest.py`) with no internal modules. All logic lives in one script.

**Data flow:**
1. `run_digest()` orchestrates everything: fetch events -> geocode locations -> compute travel -> fetch weather -> build AI prompt -> generate briefing -> render HTML -> send/save
2. `fetch_icloud_events()` uses CalDAV to pull events from iCloud, parsed via `icalendar` into `DigestEvent` dataclasses
3. `geocode_locations()` calls Nominatim (OpenStreetMap) with 1-second rate limiting; results cached in `geocode_cache.json`
4. `compute_travel_segments()` uses haversine distance + configured speed to flag tight transitions between geolocated events
5. `fetch_weather()` hits Open-Meteo (no API key) for 7-day forecast
6. `generate_briefing()` sends a structured prompt to Claude (claude-sonnet-4-20250514) for a prose briefing
7. `render_html()` builds inline-CSS HTML email; `send_html_email()` sends via SMTP STARTTLS

**Week logic:** On Sundays, `week_bounds_utc()` returns the coming week (starts Monday). On the first Sunday of the month, a 30-day month-ahead section is also included.

**CalDAV import is deferred** (`_load_caldav()`) because caldav+lxml are slow to import (~15-45s cold). `--list-calendars` prints a warning before the import begins.

**HTTP/3 is explicitly disabled** on the CalDAV client (`_disable_http3_on_caldav_client()`) to avoid iCloud QUIC stalls.

## Configuration

`config.json` (gitignored) is required at the script's directory. Copy from `config.example.json`.

Key fields:
- `icloud.calendars`: map of exact iCloud display names -> short labels used in HTML/AI prompt
- `ai_summary_enabled`: set to `false` to skip AI briefing (no Anthropic key needed)
- `context`: free-text description of the user passed to the AI model
- `travel.enabled`: when `false`, skips Nominatim geocoding entirely
- `DISPLAY_TZ` is hardcoded to `Europe/London` in the script

## Systemd Scheduling

Timer files in `systemd/` target Sunday 18:00. Adjust `WorkingDirectory` and `ExecStart` paths before deploying to `~/.config/systemd/user/`.
