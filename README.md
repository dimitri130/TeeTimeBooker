# Timmy's Tee Time Booker

## Purpose
Books tee times at a golf course by driving a Chrome browser through the EZLinks
Golf booking site. Supports single-attempt, scheduled polling (auto), and waiting-for-opening
modes, plus viewing and cancelling existing reservations. A simple desktop GUI
(built with customtkinter) wraps the whole flow — most users only ever touch the GUI.

## Quick start (build & run the app)

**Requirements:** Windows, [Python 3.9+](https://www.python.org/downloads/), and Google Chrome installed.

```bat
:: 1. Clone and enter the repo
git clone https://github.com/dimitri130/TeeTimeBooker.git
cd TeeTimeBooker

:: 2. Install dependencies
py -m pip install -r requirements.txt

:: 3. Build the app (one .exe + supporting files in dist\)
py -m PyInstaller TimmysTeeTimeBooker.spec --noconfirm
```

4. **Run it:** double-click **`dist\TimmysTeeTimeBooker\TimmysTeeTimeBooker.exe`**.

On first launch the app creates `config.json` and `.secret.key` next to the exe. Copy
`config.example.json` over the generated `config.json` if you would rather start from the
documented skeleton. Neither `config.json` nor `.secret.key` is tracked by git. Then, in the GUI:

1. Fill in your EZLinks credentials and booking preferences, then click **Save Config**.
2. Click **Calibrate Cloudflare** once (redo it whenever your monitor, DPI, or zoom changes).
3. Click **Validate** to confirm the config is ready to book.
4. Click **Run** to book. Leave **Testing Mode** on to dry-run the full flow without finalizing.

> Build once. After pulling new code, re-run step 3 to rebuild. You can also run the app
> straight from source without building: `py gui.py`.

### Optional: email notifications
Notifications are off unless you supply a Gmail sender through the environment. No credential is
stored in the repo or baked into the exe.

```bat
set TTB_SMTP_SENDER=you@gmail.com
set TTB_SMTP_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

`TTB_SMTP_APP_PASSWORD` must be a 16-character Gmail **App Password**, not the account password.
With either variable unset, the app runs normally and skips sending mail.

## What the name means
Named after the colleague it was originally built for. Targets the EZLinks Golf
platform; the course is set by `booking_url` in your config (see Configuration).

## How it fits in
- **Project type:** standalone Python CLI script. No services, no shared libraries, no upstream/downstream.
- **Upstream:** local `config.json` + `.secret.key`; the EZLinks Golf booking website.
- **Downstream:** a confirmed tee-time reservation on the website. Console output + `tee_time_booking.log`.
- **Talks to:** EZLinks Golf website (via Chrome/Selenium), optionally Gmail SMTP for notifications.

## Data flow
1. `prepare_config()` loads and validates `config.json`, encrypting any plaintext sensitive fields
   in place before returning the decrypted config to the caller.
2. `TeeTimeBooker` is instantiated with the config; a Chrome session is launched via
   `undetected_chromedriver`.
3. Cloudflare bot-detection is handled (calibrated mouse click + Bezier-curve movement simulation).
4. Login → set player count → set date → set time filter → set price filter → scrape available
   slots → select best match → select saved card (Work/Play) → confirm booking → email + PDF.
5. In auto mode, the browser is closed and relaunched on every retry, with the loop sleeping
   `check_interval_seconds` between attempts.

## Configuration
All config lives in `config.json` next to the script. On first run it is created from a default
skeleton; edit values then re-run to encrypt and validate.

| Field | Purpose |
|---|---|
| `booking_url` | EZLinks booking page URL. The default is a placeholder. Set it to your course's booking page. |
| `booking_date` | Target date (`YYYY-MM-DD`). Validated as not-in-the-past. |
| `username` / `password` | Site credentials. Stored as `ENC:…` after first run. |
| `num_players` | Number of players (1–4). |
| `desired_time` | Preferred tee time (`HH:MM` 24-hour). Slot selection minimises distance from this. |
| `earliest_acceptable_time` / `latest_acceptable_time` | Time window filter applied on the booking page. |
| `earlier_tolerance_minutes` / `later_tolerance_minutes` | Minutes earlier / later than `desired_time` tolerated when ranking slots (0–240). |
| `preference_if_equal` | Tiebreak when two slots are equidistant from desired time (`"earlier"` or `"later"`). |
| `additional_players` | Named players beyond the booker, in `"LastName, FirstName"` format. Must have exactly `num_players - 1` entries. |
| `testing` | If true, runs the full flow but stops before finalizing the reservation (no real booking, no confirmation email). |
| `capture_page_dumps` | If true, saves DOM snapshots to `dumps/` for offline debugging. |
| `reservations_cache_max_age_hours` | Age past which the cached reservations list is considered stale. |
| `payment.use_work_card` | Selects the Work saved card when true, Play when false (GUI **Mode** toggle). |
| `payment.work_card_last4` / `payment.default_card_last4` | Last-4 digits used to pick the matching saved card radio on checkout. |
| `auto_booking.enabled` | If true (and no CLI override), runs auto polling mode rather than a single attempt. |
| `auto_booking.release_time` | Tee-time release time (`HH:MM`); the run preps ahead of this and strikes at it. |
| `auto_booking.prep_lead_minutes` | Minutes before `release_time` to open the browser and clear Cloudflare (2–120). |
| `auto_booking.strike_max_minutes` / `auto_booking.strike_poll_seconds` | Duration and poll interval of the fast strike loop around release (1–240 / 1–60). |
| `auto_booking.widen_after_seconds` | Seconds into the strike before the time window is widened (0–3600). |
| `auto_booking.check_interval_seconds` | Sleep between fallback retries. |
| `auto_booking.max_attempts` | Give up after this many failed fallback attempts. |
| `auto_booking.open_days_before` | Days before the tee date that slots release (14 for this course). |
| `auto_booking.probe_attempts` | Pre-release probe attempts used to detect early opening. |
| `waiting_mode.enabled` | If true (and no CLI override / other mode), hunts for an opening on an already-released day. |
| `waiting_mode.reconnect_min_minutes` / `reconnect_max_minutes` / `reconnect_jitter_seconds` | Bounds and jitter for the sleep between reconnect cycles (max must be ≥ min). |
| `waiting_mode.in_session_poll_seconds` | Poll interval while a browser session is live (10–600). |
| `waiting_mode.session_max_minutes` | Cap on a single session's lifetime; `0` = uncapped (run until the site ends it). |
| `waiting_mode.auto_take` / `continuous_upgrade` / `cancel_existing_on_upgrade` | Take an opening automatically, keep upgrading toward `desired_time`, and cancel the prior booking on upgrade. |
| `waiting_mode.fee_period_hours` / `continue_into_fee_period` / `fee_period_auto_swap` | Cancellation-fee window handling near the tee date. |
| `cloudflare_checkbox` | Relative viewport coordinates of the Cloudflare checkbox. Must be calibrated per device (see Running). |
| `email_notifications` | Optional notifications to playing partners (`enabled`, `to_emails`, `failure_contacts`, `heartbeat_contacts`, `local_popup`). Sent via Gmail SMTP from the account in the `TTB_SMTP_*` environment variables (see Optional: email notifications). |

**Sensitive fields** (`username`, `password`) are encrypted
with Fernet on first run and stored as `ENC:…` strings. The key lives in `.secret.key` —
**do not commit it**.

**Other hardcoded values** (`TimmysTeeTimeBooker.py`):
- `DEFAULT_WAIT = 15`, `LOGIN_TIMEOUT = 10`, `CLOUDFLARE_TIMEOUT = 30` (lines 71–73)
- Price filter target: `"Full"` in `set_course_filter()` — edit there to change the pricing option.

## Running / debugging locally

The GUI (`py gui.py`, or the built exe) is the primary interface and the recommended way to run
everything below. The CLI is for advanced/headless use.

Install dependencies:
```
py -m pip install -r requirements.txt
```

CLI commands (all via `TimmysTeeTimeBooker.py`):

| Command | Effect |
|---|---|
| `py TimmysTeeTimeBooker.py` | Run based on config (`auto_booking.enabled` / `waiting_mode.enabled` decide mode) |
| `py TimmysTeeTimeBooker.py single` | Force a single booking attempt |
| `py TimmysTeeTimeBooker.py auto` | Force auto-monitoring mode |
| `py TimmysTeeTimeBooker.py waiting` | Hunt for an opening on an already-released day |
| `py TimmysTeeTimeBooker.py setup` | Bootstrap / encrypt / validate config |
| `py TimmysTeeTimeBooker.py calibrate` | Recalibrate the Cloudflare checkbox coordinates |
| `py TimmysTeeTimeBooker.py reservations` | Show current reservations (cached) |
| `py TimmysTeeTimeBooker.py reservations-refresh` | Force a live reservations refresh |
| `py TimmysTeeTimeBooker.py cancel <conf#>` | Cancel a reservation by confirmation number |
| `py TimmysTeeTimeBooker.py help` | Show usage |

**First time on a new machine:**
1. Run `py TimmysTeeTimeBooker.py setup` — creates `config.json` if missing, encrypts credentials,
   and prompts for Cloudflare calibration.
2. Edit `config.json` with your credentials and booking preferences (plaintext is fine —
   setup encrypts sensitive fields on next run).
3. Calibrate: `py TimmysTeeTimeBooker.py calibrate` — opens a browser to the booking URL, hover your
   mouse over the Cloudflare checkbox, then press ENTER. Calibration is **device and
   browser-zoom specific** — redo it if you change monitors, DPI, or zoom level.

## Key types

- `TeeTimeBooker` (`TimmysTeeTimeBooker.py`) — main class; orchestrates the full booking flow.
- `HumanMouse` (`TimmysTeeTimeBooker.py`) — simulates human-like mouse movement via Bezier curves;
  used for Cloudflare checkbox clicking to avoid bot detection.
- `prepare_config()` (`run_setup.py`) — config load / encrypt / validate pipeline; called on
  every run before the booker is instantiated.
- `validate_config()` (`run_setup.py`) — full field validation; raises `ConfigError` with all
  problems at once.
- `crypto_utils.py` — thin Fernet wrapper; `encrypt()`/`decrypt()` used by setup and the
  `_decrypt_recursive` walk in the main script.

## Gotchas & assumptions

- **No implicit Selenium waits.** Implicit waits make every `find_element` call that finds
  nothing block for the full timeout, stacking badly with explicit `WebDriverWait`. All
  timing-sensitive lookups use explicit waits instead (comment at `TimmysTeeTimeBooker.py:265`).
- **Fresh browser per auto-booking retry.** The earlier approach (single session, re-use after
  login) was replaced because session disconnects were hard to recover cleanly. Each retry
  opens and closes its own browser.
- **Cloudflare click search pattern.** After `CLOUDFLARE_SEARCH_AFTER` (3) direct clicks fail,
  the script tries an expanding grid of pixel offsets around the calibrated point. Handles
  slight calibration drift without requiring a full recalibration.
- **EZLinks Angular quirks.** Date input requires jQuery datepicker API + Angular `blur` trigger
  (not just setting `el.value`). The price-options filter `<li>` became a `<div>` in a 2026
  site update; the selector matches on `ng-if` attribute rather than tag name.
- **`additional_players` format is strict.** Must be `"LastName, FirstName"` and the list length
  must equal `num_players - 1`; validation raises `ConfigError` otherwise.
- **`booking_date` is validated as not-in-past** at config load time — re-running the script on
  the booking date itself is fine, but any prior date will fail at startup.

## Known gaps / future work

- **Email notifications are optional.** Sent via Gmail SMTP from the sender account given by
  the `TTB_SMTP_SENDER` / `TTB_SMTP_APP_PASSWORD` environment variables (read in
  `run_setup.py`). The password must be a Gmail **App Password** (the plain account password is
  rejected by Gmail SMTP). Nothing is stored in the repo or the exe. Recipients come from
  `email_notifications.to_emails` (the per-player email fields in the GUI). An email is sent
  **only after a real booking is confirmed** — never in test mode — and includes the date,
  selected tee time, and players, with the confirmation page attached as a PDF.
- **Saved-card selection is active.** The GUI **Mode** toggle picks which saved card to use
  (the `payment.work_card_last4` / `payment.default_card_last4` values from your config) by clicking the matching radio on the checkout page. Entering a
  *new* card is not supported: the old `enter_payment()` / `_fill_field_anywhere()` /
  `_set_expiration()` path was removed as dead code, so re-enabling new-card entry would mean
  re-implementing it and adding `payment.*` card fields.
- **Booking URL is a placeholder** in `DEFAULT_CONFIG` and the example
  config. Works for any EZLinks site by changing `booking_url`, but a different site may need
  selector/filter adjustments.

## Open questions / for domain owner
None outstanding.

## Related
- `filter_page_dump.html` — written to the repo root by the filter diagnostics routine when the
  price-options filter can't be located; useful for offline inspection after a failed run.
- EZLinks Golf platform: `https://www.ezlinksgolf.com`
