# Google Calendar + Gmail API Integration

**Date:** 2026-05-12  
**Status:** Approved

## Overview

Add Google Calendar as a second event source alongside iCloud CalDAV, and replace optional SMTP email sending with the Gmail API — both using a single Google service account with domain-wide delegation (DWD). All new Google integration is optional: if the `google` config block is absent, the script behaves exactly as before.

## Authentication

A Google Cloud service account with domain-wide delegation impersonates the Workspace user's email. No browser flow, no token refresh — only the service account JSON key file is needed on the server.

**DWD scopes required:**
- `https://www.googleapis.com/auth/calendar.readonly`
- `https://www.googleapis.com/auth/gmail.send`

## Config Schema

New optional `google` block in `config.json`:

```json
"google": {
  "service_account_file": "google-service-account.json",
  "impersonate": "you@yourworkspace.com",
  "calendars": {
    "Work": "work",
    "Shared": "shared"
  }
}
```

- `service_account_file`: path relative to the script directory, or absolute
- `impersonate`: Workspace user email the service account acts as
- `calendars`: display name → short label mapping, same pattern as `icloud.calendars`

The existing `smtp` block becomes optional when `google` is present — Gmail API is used for sending instead. `email.from` and `email.to` remain required.

## New Functions

### `_load_google() -> tuple`
Deferred import of `googleapiclient.discovery` and `google.oauth2.service_account`, matching the `_load_caldav()` lazy-load pattern to avoid slow cold-start imports.

### `_google_calendar_service(cfg) -> Resource`
Loads the service account JSON key, creates delegated credentials scoped to `calendar.readonly` impersonating `cfg["google"]["impersonate"]`, and returns a built `calendar` API service.

### `_google_gmail_service(cfg) -> Resource`
Same credential setup, scoped to `gmail.send`, returns a built `gmail` API service.

### `fetch_google_events(cfg, start, end) -> list[DigestEvent]`
- Skips silently if `google` block absent from config
- First calls `calendarList().list()` (paginated) to build a `summary → calendarId` map; Google's `events().list()` requires a calendar ID, not a display name
- Iterates `cfg["google"]["calendars"]`; warns and skips any configured name not found in the map
- Calls `events().list(calendarId=..., singleEvents=True, timeMin=..., timeMax=..., orderBy="startTime")` with RFC3339 UTC timestamps
- Handles pagination via `nextPageToken`
- Converts each event: `start.date` → all-day `DigestEvent`; `start.dateTime` → timed event
- On any error: logs and returns empty list (digest continues with iCloud events)

### `list_google_calendars(cfg) -> None`
- Calls `calendarList().list()` (paginated)
- Prints all calendar `summary` fields sorted alphabetically
- Same output format as `list_icloud_calendars`

## Changes to Existing Functions

### `run_digest()`
```python
week_events = fetch_icloud_events(...) + fetch_google_events(...)
month_events = fetch_icloud_events(...) + fetch_google_events(...)  # on first Sunday
week_events.sort(key=lambda e: (e.start, e.end, e.title))
month_events.sort(...)
```
The combined list flows into geocoding, travel, weather, AI, and HTML unchanged.

### `send_html_email()`
Detects `cfg.get("google")` — if present, sends via Gmail API (base64url-encodes the MIME message, posts to `users.messages.send` impersonating `email.from`). If absent, falls back to existing SMTP path unchanged.

### `main()`
- Adds `--list-google-calendars` flag
- Google fetch is skipped automatically when `google` block is absent — no flag needed to disable

## Dependencies

Added to `pyproject.toml`:
```
google-api-python-client>=2.0
google-auth>=2.0
```

## README: Google Calendar Setup

New section covering:
1. Create a Google Cloud project; enable the Google Calendar API and Gmail API
2. Create a service account; download the JSON key file to the script directory
3. In Workspace Admin → Security → API Controls → Domain-wide Delegation: add the service account's client ID with scopes `calendar.readonly` and `gmail.send`
4. Add the `google` block to `config.json`; optionally remove the `smtp` block
5. Run `uv run calendar_digest.py --list-google-calendars` to verify auth and discover calendar names
6. Add chosen calendars to `config.google.calendars`

## What Does Not Change

- iCloud CalDAV fetch is unaffected
- `DigestEvent` dataclass is unaffected
- All downstream processing (geocoding, travel, weather, AI briefing, HTML render) is unaffected
- Existing configs without a `google` block continue to work identically
