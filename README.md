# Calendar digest

Weekly iCloud (CalDAV) digest with an optional Anthropic-powered briefing, Open-Meteo weather, haversine-based travel hints (with Nominatim geocoding), and SMTP email.

## Quick start

1. Install [uv](https://docs.astral.sh/uv/) if needed: `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Clone this repo and `cd` into it.
3. Gather the [credentials](#required-credentials) below, then `cp config.example.json config.json` and fill in every field that applies.
4. `chmod 600 config.json`
5. `uv run calendar_digest.py --list-calendars` to discover exact iCloud calendar names (case-sensitive) and update `icloud.calendars` keys to match.
6. `uv run calendar_digest.py --preview` and open `preview.html`.
7. `uv run calendar_digest.py` to send the email.

## Required credentials

Everything sensitive lives in `config.json` (gitignored). Restrict the file to your user (`chmod 600 config.json`).

### iCloud (CalDAV)

Used to read calendars. **No** full Apple ID password in config—only an **app-specific password**.

1. Enable **two-factor authentication** on your Apple ID if it is not already on.
2. Sign in at [Apple ID account management](https://appleid.apple.com/) (Account → Sign-In and Security → App-Specific Passwords).
3. Create an app-specific password (e.g. label it “Calendar digest”) and copy the `xxxx-xxxx-xxxx-xxxx` value.
4. In `config.json`, set:
   - **`icloud.username`**: your Apple ID email (e.g. `you@icloud.com` or `you@gmail.com` if that is your Apple ID).
   - **`icloud.app_password`**: the app-specific password (including hyphens).
   - **`icloud.url`**: leave as `https://caldav.icloud.com` unless you know you need something else.
5. Map **`icloud.calendars`**: keys must be the **exact** calendar names iCloud exposes. Run `uv run calendar_digest.py --list-calendars` after the above is set, then copy each name you want into the JSON keys. Values are short labels used in the email and AI prompt (e.g. `"Personal": "personal"`).

### SMTP (sending email)

The digest is sent over **SMTP with STARTTLS** (typically port **587**).

1. Choose a mailbox that is allowed to send mail (the example uses Gmail).
2. For **Gmail** (and many providers), you cannot use your normal login password for SMTP. Create an [App password](https://myaccount.google.com/apppasswords) (Google Account → Security → 2-Step Verification → App passwords) and use that as **`smtp.password`**. **`smtp.username`** is usually your full Gmail address.
3. Set **`email.from`** and **`email.to`** to the addresses you want on the message (often the same inbox). **`from`** must be an identity your SMTP account is permitted to send as.

If you use another provider, set **`smtp.host`**, **`smtp.port`**, **`smtp.username`**, and **`smtp.password`** to that provider’s documented values (still STARTTLS on 587 in most cases).

### Anthropic (optional AI briefing)

Used only when **`ai_summary_enabled`** is `true`.

1. Create an API key in the [Anthropic Console](https://console.anthropic.com/).
2. Set **`anthropic_api_key`** to that key (starts with `sk-ant-...`).
3. To run **without** the model (events and HTML only), set **`ai_summary_enabled`** to `false` and omit or ignore the key.

If the key is missing, invalid, or the API errors, the script still builds and sends the digest; the briefing block is simply omitted.

### Not API keys (no signup)

- **Weather** uses [Open-Meteo](https://open-meteo.com/) with no key. Coordinates and labels are configured under **`weather`**.
- **Geocoding** uses [Nominatim](https://nominatim.openstreetmap.org/) with no key. Event location strings are sent to OpenStreetMap when **`travel.enabled`** is `true`; disable travel if you do not want that.

## systemd (Sunday 18:00)

Copy `systemd/calendar-digest.service` and `systemd/calendar-digest.timer` into `~/.config/systemd/user/`, adjust `WorkingDirectory` and `ExecStart` paths, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now calendar-digest.timer
```

On a headless host, enable lingering so user timers run without a login session:

```bash
sudo loginctl enable-linger YOUR_USERNAME
```

## Files

| File | Purpose |
|------|---------|
| `calendar_digest.py` | Entry point and all application logic |
| `config.json` | Secrets and settings (gitignored) |
| `config.example.json` | Template |
| `geocode_cache.json` | Nominatim cache (gitignored) |
| `preview.html` | Output of `--preview` (gitignored) |

## Configuration

Beyond credentials, `config.example.json` documents **`context`** (free text for the model), **`weather`**, **`travel`** (average speed for crow-fly distance and buffer minutes), and toggles. Copy it to `config.json` and edit.

## Requirements

Python 3.11+ (managed by `uv`). Dependencies are listed in `pyproject.toml`.
