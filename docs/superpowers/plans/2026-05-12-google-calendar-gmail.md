# Google Calendar + Gmail API Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Google Calendar as a second event source and Gmail API as an optional email sender, both via a service account with domain-wide delegation.

**Architecture:** All changes are in the single file `calendar_digest.py`. Google functions mirror the iCloud pattern: lazy imports, separate fetch/list functions, same `DigestEvent` output type. `run_digest()` concatenates both sources. `send_html_email()` dispatches to SMTP or Gmail API based on config.

**Tech Stack:** `google-api-python-client>=2.0`, `google-auth>=2.0`, existing `requests`, `MIMEMultipart`/`MIMEText` from stdlib

> **Note:** This project has no test suite. Verification steps use `uv run calendar_digest.py --list-google-calendars` and `--preview` instead of pytest.

---

## File map

| File | Change |
|------|--------|
| `pyproject.toml` | Add two new dependencies |
| `calendar_digest.py` | Add ~120 lines: lazy import, constants, 6 new functions, modify 3 existing |
| `config.example.json` | Add `google` block |
| `README.md` | Add Google Calendar + Gmail setup section |

---

### Task 1: Add dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the two new packages**

Edit `pyproject.toml` — replace the `dependencies` list:

```toml
dependencies = [
    "caldav>=1.3",
    "icalendar>=5.0",
    "anthropic>=0.40",
    "requests>=2.28",
    "google-api-python-client>=2.0",
    "google-auth>=2.0",
]
```

- [ ] **Step 2: Sync and verify packages resolve**

```bash
uv sync
```

Expected: lock file updates, no resolver errors.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add google-api-python-client and google-auth dependencies"
```

---

### Task 2: Add Google lazy-load infrastructure and constants

**Files:**
- Modify: `calendar_digest.py`

- [ ] **Step 1: Add global bundle variable and `_load_google()` after `_load_caldav()`**

After the `_caldav_bundle` global and `_load_caldav()` function (after line ~51), add:

```python
_google_bundle: tuple[Any, Any] | None = None


def _load_google() -> tuple[Any, Any]:
    global _google_bundle
    if _google_bundle is None:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
        _google_bundle = (build, service_account)
    return _google_bundle
```

- [ ] **Step 2: Add scope constants**

After `NOMINATIM_UA` (line ~35), add:

```python
_GOOGLE_CAL_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
_GOOGLE_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
```

- [ ] **Step 3: Add `_google_calendar_service()` and `_google_gmail_service()`**

After `_load_google()`, add both helper functions:

```python
def _google_calendar_service(cfg: dict[str, Any]) -> Any:
    build, service_account = _load_google()
    g = cfg["google"]
    sa_path = Path(g["service_account_file"])
    if not sa_path.is_absolute():
        sa_path = SCRIPT_DIR / sa_path
    creds = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=[_GOOGLE_CAL_SCOPE]
    ).with_subject(g["impersonate"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _google_gmail_service(cfg: dict[str, Any]) -> Any:
    build, service_account = _load_google()
    g = cfg["google"]
    sa_path = Path(g["service_account_file"])
    if not sa_path.is_absolute():
        sa_path = SCRIPT_DIR / sa_path
    creds = service_account.Credentials.from_service_account_file(
        str(sa_path), scopes=[_GOOGLE_GMAIL_SCOPE]
    ).with_subject(g["impersonate"])
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
```

- [ ] **Step 4: Verify script still parses**

```bash
uv run python -c "import calendar_digest"
```

Expected: no output, no errors.

- [ ] **Step 5: Commit**

```bash
git add calendar_digest.py
git commit -m "feat: add Google lazy-load infrastructure and credential helpers"
```

---

### Task 3: Add `_parse_google_event()` and `fetch_google_events()`

**Files:**
- Modify: `calendar_digest.py`

- [ ] **Step 1: Add `_parse_google_event()` after `parse_vevent()`**

After `parse_vevent()` (after line ~284), add:

```python
def _parse_google_event(item: dict[str, Any], calendar_label: str) -> DigestEvent | None:
    try:
        title = item.get("summary", "(no title)")
        location = item.get("location", "")
        start_info = item.get("start", {})
        end_info = item.get("end", {})
        if "date" in start_info:
            start_d = date.fromisoformat(start_info["date"])
            start = datetime.combine(start_d, dt_time.min, tzinfo=timezone.utc)
            end_d = date.fromisoformat(end_info.get("date", start_info["date"]))
            end = datetime.combine(end_d, dt_time.min, tzinfo=timezone.utc) - timedelta(seconds=1)
            all_day = True
        else:
            start = datetime.fromisoformat(start_info["dateTime"]).astimezone(timezone.utc)
            end = datetime.fromisoformat(
                end_info.get("dateTime", start_info["dateTime"])
            ).astimezone(timezone.utc)
            all_day = False
        return DigestEvent(
            title=title,
            start=start,
            end=end,
            location=location,
            calendar_label=calendar_label,
            all_day=all_day,
            lat=None,
            lon=None,
        )
    except Exception as e:  # noqa: BLE001
        LOG.warning("Skipping Google event parse failure: %s", e)
        return None
```

- [ ] **Step 2: Add `fetch_google_events()` after `fetch_icloud_events()`**

After `fetch_icloud_events()` (after line ~324), add:

```python
def fetch_google_events(cfg: dict[str, Any], start: datetime, end: datetime) -> list[DigestEvent]:
    if not cfg.get("google"):
        return []
    try:
        svc = _google_calendar_service(cfg)
        cal_id_map: dict[str, str] = {}
        page_token: str | None = None
        while True:
            resp = svc.calendarList().list(pageToken=page_token).execute()
            for item in resp.get("items", []):
                cal_id_map[item["summary"]] = item["id"]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        time_min = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        events: list[DigestEvent] = []

        for name, label in cfg["google"]["calendars"].items():
            cal_id = cal_id_map.get(name)
            if not cal_id:
                LOG.warning(
                    "Google calendar not found: %r. Available: %s",
                    name,
                    sorted(cal_id_map),
                )
                continue
            page_token = None
            while True:
                resp = svc.events().list(
                    calendarId=cal_id,
                    singleEvents=True,
                    orderBy="startTime",
                    timeMin=time_min,
                    timeMax=time_max,
                    pageToken=page_token,
                ).execute()
                for item in resp.get("items", []):
                    de = _parse_google_event(item, label)
                    if de and de.start < end and de.end > start:
                        events.append(de)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        return events
    except Exception as e:  # noqa: BLE001
        LOG.error("Google Calendar fetch failed: %s", e)
        return []
```

- [ ] **Step 3: Verify script still parses**

```bash
uv run python -c "import calendar_digest"
```

Expected: no output, no errors.

- [ ] **Step 4: Commit**

```bash
git add calendar_digest.py
git commit -m "feat: add Google Calendar event fetching"
```

---

### Task 4: Add `list_google_calendars()` and `--list-google-calendars` CLI flag

**Files:**
- Modify: `calendar_digest.py`

- [ ] **Step 1: Add `list_google_calendars()` after `list_icloud_calendars()`**

After `list_icloud_calendars()` (after line ~382), add:

```python
def list_google_calendars(cfg: dict[str, Any]) -> None:
    try:
        svc = _google_calendar_service(cfg)
        names: list[str] = []
        page_token: str | None = None
        while True:
            resp = svc.calendarList().list(pageToken=page_token).execute()
            for item in resp.get("items", []):
                names.append(item["summary"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        for n in sorted(set(names)):
            print(n)
    except Exception as e:  # noqa: BLE001
        LOG.error("Failed to list Google calendars: %s", e)
        raise SystemExit(1) from e
```

- [ ] **Step 2: Add the CLI flag in `main()`**

In `main()`, after the `--list-calendars` argument, add:

```python
p.add_argument("--list-google-calendars", action="store_true", help="Print Google calendar names")
```

- [ ] **Step 3: Handle the flag in `main()`**

In `main()`, after the `if args.list_calendars:` block, add:

```python
    if args.list_google_calendars:
        list_google_calendars(cfg)
        return
```

- [ ] **Step 4: Verify flag appears in help**

```bash
uv run calendar_digest.py --help
```

Expected output includes: `--list-google-calendars  Print Google calendar names`

- [ ] **Step 5: Commit**

```bash
git add calendar_digest.py
git commit -m "feat: add list_google_calendars and --list-google-calendars flag"
```

---

### Task 5: Merge Google events in `run_digest()`

**Files:**
- Modify: `calendar_digest.py`

- [ ] **Step 1: Replace the two `fetch_icloud_events` calls in `run_digest()`**

Find these lines in `run_digest()`:

```python
    week_events = fetch_icloud_events(cfg, week_start, week_end)
    month_events: list[DigestEvent] = []
    if first_sun:
        month_events = fetch_icloud_events(cfg, week_end, week_end + timedelta(days=30))
```

Replace with:

```python
    week_events = (
        fetch_icloud_events(cfg, week_start, week_end)
        + fetch_google_events(cfg, week_start, week_end)
    )
    week_events.sort(key=lambda e: (e.start, e.end, e.title))
    month_events: list[DigestEvent] = []
    if first_sun:
        month_events = (
            fetch_icloud_events(cfg, week_end, week_end + timedelta(days=30))
            + fetch_google_events(cfg, week_end, week_end + timedelta(days=30))
        )
        month_events.sort(key=lambda e: (e.start, e.end, e.title))
```

- [ ] **Step 2: Verify preview runs without error (no Google config)**

```bash
uv run calendar_digest.py --preview
```

Expected: writes `preview.html`, no errors. Google fetch returns `[]` silently since `google` key is absent from `config.json`.

- [ ] **Step 3: Commit**

```bash
git add calendar_digest.py
git commit -m "feat: merge Google Calendar events into digest"
```

---

### Task 6: Add Gmail API sending

**Files:**
- Modify: `calendar_digest.py`

- [ ] **Step 1: Extract existing SMTP logic into `_send_smtp()`**

Find `send_html_email()`:

```python
def send_html_email(cfg: dict[str, Any], subject: str, html: str) -> None:
    smtp_cfg = cfg["smtp"]
    em = cfg["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = em["from"]
    msg["To"] = em["to"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg["port"])) as s:
        s.starttls()
        s.login(smtp_cfg["username"], smtp_cfg["password"])
        s.sendmail(em["from"], [em["to"]], msg.as_string())
```

Replace entirely with:

```python
def send_html_email(cfg: dict[str, Any], subject: str, html: str) -> None:
    if cfg.get("google"):
        _send_gmail_api(cfg, subject, html)
    else:
        _send_smtp(cfg, subject, html)


def _send_smtp(cfg: dict[str, Any], subject: str, html: str) -> None:
    smtp_cfg = cfg["smtp"]
    em = cfg["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = em["from"]
    msg["To"] = em["to"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg["port"])) as s:
        s.starttls()
        s.login(smtp_cfg["username"], smtp_cfg["password"])
        s.sendmail(em["from"], [em["to"]], msg.as_string())


def _send_gmail_api(cfg: dict[str, Any], subject: str, html: str) -> None:
    import base64
    em = cfg["email"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = em["from"]
    msg["To"] = em["to"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc = _google_gmail_service(cfg)
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()
```

- [ ] **Step 2: Verify script still imports cleanly**

```bash
uv run python -c "import calendar_digest"
```

Expected: no output, no errors.

- [ ] **Step 3: Verify preview still works (SMTP path unchanged)**

```bash
uv run calendar_digest.py --preview
```

Expected: writes `preview.html`, no errors.

- [ ] **Step 4: Commit**

```bash
git add calendar_digest.py
git commit -m "feat: add Gmail API sending via service account"
```

---

### Task 7: Update config.example.json and README

**Files:**
- Modify: `config.example.json`
- Modify: `README.md`

- [ ] **Step 1: Add `google` block to `config.example.json`**

Add the following after the `"icloud"` block (before `"smtp"`):

```json
  "google": {
    "service_account_file": "google-service-account.json",
    "impersonate": "you@yourworkspace.com",
    "calendars": {
      "Work": "work"
    }
  },
```

The `"smtp"` block is now optional when `"google"` is present. No change needed to the `"smtp"` block in the example file itself — the README covers this.

- [ ] **Step 2: Add Google Calendar + Gmail setup section to `README.md`**

After the `### SMTP (sending email)` section and before `### Anthropic (optional AI briefing)`, add:

````markdown
### Google Calendar + Gmail (optional, Google Workspace)

Replaces SMTP sending and adds Google Calendar as a second event source. Both use a single **service account with domain-wide delegation (DWD)** — no browser flow, no token expiry.

#### 1. Create a Google Cloud project and enable APIs

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and create a new project (or reuse one).
2. Enable the **Google Calendar API**: APIs & Services → Library → search "Google Calendar API" → Enable.
3. Enable the **Gmail API**: same path → search "Gmail API" → Enable.

#### 2. Create a service account and download the key

1. APIs & Services → Credentials → Create Credentials → Service account.
2. Give it any name (e.g. `calendar-digest`). No special roles needed. Click Done.
3. Click the service account → Keys tab → Add Key → Create new key → JSON → Create.
4. Save the downloaded `.json` file into this repository directory (it is gitignored via `*.json` — verify with `git status`). Name it `google-service-account.json` or update `service_account_file` in `config.json`.

#### 3. Grant domain-wide delegation in Workspace Admin

1. Open the service account in Google Cloud Console and copy its **client ID** (a long number under "OAuth 2 Client ID").
2. Sign into [admin.google.com](https://admin.google.com/) as a Workspace admin.
3. Go to **Security → Access and data control → API controls → Manage Domain Wide Delegation**.
4. Click **Add new** and enter:
   - **Client ID**: the number from step 1
   - **OAuth scopes**: `https://www.googleapis.com/auth/calendar.readonly,https://www.googleapis.com/auth/gmail.send`
5. Click Authorise.

#### 4. Configure `config.json`

Add the `google` block:

```json
"google": {
  "service_account_file": "google-service-account.json",
  "impersonate": "you@yourworkspace.com",
  "calendars": {
    "Work": "work"
  }
}
```

- **`service_account_file`**: path relative to the script directory, or absolute.
- **`impersonate`**: your Workspace email address — the service account acts as you.
- **`calendars`**: populated in the next step.

Once `google` is present, the `smtp` block is optional — Gmail API is used for sending instead.

#### 5. Discover your Google calendar names

```bash
uv run calendar_digest.py --list-google-calendars
```

Copy the names you want into `config.google.calendars` (keys are exact display names, values are short labels used in the email and AI prompt).

#### 6. Test

```bash
uv run calendar_digest.py --preview
```

Open `preview.html` to confirm Google Calendar events appear alongside iCloud events.
````

- [ ] **Step 3: Verify the script help and preview still work**

```bash
uv run calendar_digest.py --help
uv run calendar_digest.py --preview
```

Expected: help shows all flags, preview writes without error.

- [ ] **Step 4: Commit**

```bash
git add config.example.json README.md
git commit -m "docs: add Google Calendar and Gmail API setup instructions"
```
