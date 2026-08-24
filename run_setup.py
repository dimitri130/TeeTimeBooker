"""
Configuration bootstrap and validation for TeeTimeBooker.

On first run (no config.json), writes a default skeleton and exits with a
message. Otherwise, encrypts any plaintext sensitive fields in place and
validates the result. Raises ConfigError on any failure.

Usage:
    python run_setup.py                       # validate + encrypt
    python run_setup.py decrypt FIELD_PATH    # show plaintext of a field

Field paths use dotted notation: e.g., 'username', 'password'.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from crypto_utils import encrypt, decrypt, load_key

# -----------------------------------------------------------------------------
# Paths and hardcoded settings
# -----------------------------------------------------------------------------
SCRIPT_DIR = (
    Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
)
CONFIG_FILE = SCRIPT_DIR / "config.json"

# -----------------------------------------------------------------------------
# Email (Gmail SMTP) settings — sender account from the environment
# -----------------------------------------------------------------------------
# The app password is read from the TTB_SMTP_APP_PASSWORD environment variable
# so it is never committed or embedded in the distributed exe. Email
# notifications are skipped when it is unset.
#
# WARNING (functionality): Gmail no longer accepts a regular account password
# over SMTP (disabled May 2022). SENDER_PASSWORD MUST be a 16-character Gmail
# *App Password* (enable 2-Step Verification on the account, then create one
# under Google Account > Security > App passwords). The plain account password
# will fail with an authentication error.
SENDER_EMAIL = os.environ.get("TTB_SMTP_SENDER", "")
SENDER_PASSWORD = os.environ.get("TTB_SMTP_APP_PASSWORD", "")
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# Default failure-alert recipients. Empty by default; set
# email_notifications.failure_contacts in config.json to receive alerts.
DEFAULT_FAILURE_CONTACTS: list[str] = []

# Operational alerts (prep status, heartbeats, warnings, crash reports) go
# only here; failure_contacts is reserved for booking outcomes.
HEARTBEAT_CONTACTS: list[str] = []

# Sensitive fields (dotted paths). Encrypted before validation passes.
SENSITIVE_FIELDS = (
    "username",
    "password",
    # "payment.card_number",
    # "payment.expiration",
)

# Always-required, non-empty fields.
REQUIRED_NONEMPTY = (
    "username",
    "password",
    # "payment.name_on_card",
    # "payment.card_number",
    # "payment.expiration",
)

# Default config skeleton written when no config.json exists.
DEFAULT_CONFIG = {
    "booking_url": "https://YOUR-COURSE.ezlinksgolf.com/index.html#/search",
    "booking_date": datetime.now().strftime("%Y-%m-%d"),
    "username": "",
    "password": "",
    "num_players": 1,
    "desired_time": "09:00",
    "earliest_acceptable_time": "08:00",
    "latest_acceptable_time": "10:00",
    "preference_if_equal": "earlier",
    "testing": True,
    "additional_players": [],
    "payment": {
        "use_work_card": False,
        "work_card_last4": "",
        "default_card_last4": "",
    },
    "email_notifications": {
        "enabled": False,
        "to_emails": [],
        # Editable like heartbeat_contacts: prefilled with the defaults so the
        # GUI shows them; empty -> falls back to DEFAULT_FAILURE_CONTACTS.
        "failure_contacts": list(DEFAULT_FAILURE_CONTACTS),
        # Used as-is at send time; empty -> falls back to HEARTBEAT_CONTACTS
        "heartbeat_contacts": list(HEARTBEAT_CONTACTS),
        # Off by default: a modal stealing focus mid-run can interfere with
        # the bot's own Cloudflare clicks, and the output panel already
        # surfaces every alert. Opt-in via the Notifications sub-tab.
        "local_popup": False,
    },
    "auto_booking": {
        "enabled": False,
        "release_time": "06:00",
        "prep_lead_minutes": 5,
        "strike_max_minutes": 15,
        "strike_poll_seconds": 2,
        "widen_after_seconds": 90,
        "check_interval_seconds": 15,
        "max_attempts": 20,
        "open_days_before": 14,
        "probe_attempts": 2
    },
    "waiting_mode": {
        "enabled": False,
        "reconnect_min_minutes": 60,
        "reconnect_max_minutes": 180,
        "reconnect_jitter_seconds": 300,
        "in_session_poll_seconds": 30,
        "session_max_minutes": 0,
        "auto_take": True,
        "continuous_upgrade": True,
        "cancel_existing_on_upgrade": False,
        "fee_period_hours": 48,
        "continue_into_fee_period": False,
        "fee_period_auto_swap": False
    },
    "earlier_tolerance_minutes": 30,
    "later_tolerance_minutes": 30,
    "capture_page_dumps": False,
    "reservations_cache_max_age_hours": 12,
    "cloudflare_checkbox": {
        "relative_x": 0.3328125,
        "relative_y": 0.2577266922094508,
        "calibrated": True
    }
}


class ConfigError(Exception):
    """Raised when config is missing, malformed, or invalid."""


class CalibrationRequired(ConfigError):
    """Raised when config is otherwise valid but calibration is not done."""


# -----------------------------------------------------------------------------
# Validators — each returns an error string or None
# -----------------------------------------------------------------------------
def _err_url(v):
    if not isinstance(v, str) or not v.startswith(("http://", "https://")):
        return "must start with http:// or https://"
    return None


def _err_date_format(v):
    """YYYY-MM-DD format only (no past check)."""
    if not isinstance(v, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        return "must be in format YYYY-MM-DD"
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError as e:
        return f"invalid date: {e}"
    return None


def _err_date(v):
    """YYYY-MM-DD only; must not be in the past."""
    if e := _err_date_format(v):
        return e
    if datetime.strptime(v, "%Y-%m-%d").date() < datetime.now().date():
        return f"date {v} is in the past"
    return None


def _err_time(v):
    """HH:MM 24-hour only."""
    if not isinstance(v, str) or not re.match(r"^\d{2}:\d{2}$", v):
        return "must be in format HH:MM (24-hour)"
    h, m = int(v[:2]), int(v[3:])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return f"invalid time {v}"
    return None


def _err_time_for_date(v, booking_date):
    """Time format check + can't be in the past if booking is today."""
    if e := _err_time(v):
        return e
    try:
        bd = datetime.strptime(booking_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None  # date error reported separately
    if bd == datetime.now().date():
        h, m = int(v[:2]), int(v[3:])
        now = datetime.now()
        if (h, m) <= (now.hour, now.minute):
            return f"time {v} has already passed today"
    return None


def _err_email(v):
    if not isinstance(v, str) or not re.match(
        r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", v
    ):
        return "invalid email format"
    return None


def _err_int_range(v, lo, hi):
    if not isinstance(v, int) or isinstance(v, bool):
        return f"must be an integer in [{lo}, {hi}]"
    if not (lo <= v <= hi):
        return f"must be in [{lo}, {hi}], got {v}"
    return None


def _err_card_number(v):
    """Plaintext card number check. Run BEFORE encryption."""
    if not isinstance(v, str):
        return "must be a string"
    cleaned = re.sub(r"[\s\-]", "", v)
    if not cleaned.isdigit():
        return "must contain only digits (spaces/hyphens allowed)"
    if not (13 <= len(cleaned) <= 19):
        return f"must be 13-19 digits, got {len(cleaned)}"
    return None


def _err_expiration(v):
    """Plaintext expiration check. Run BEFORE encryption.
    Accepts MM/YY or MM/YYYY; must not be expired.
    """
    if not isinstance(v, str):
        return "must be a string"
    m = re.match(r"^(\d{1,2})/(\d{2}|\d{4})$", v.strip())
    if not m:
        return "must be in format MM/YY or MM/YYYY"
    month, year = int(m.group(1)), int(m.group(2))
    if year < 100:
        year += 2000
    if not (1 <= month <= 12):
        return f"invalid month {month}"
    now = datetime.now()
    if year < now.year or (year == now.year and month < now.month):
        return f"card expired ({month:02d}/{year})"
    return None


# -----------------------------------------------------------------------------
# Nested-field helpers
# -----------------------------------------------------------------------------
def _get_nested(d, path):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_nested(d, path, value):
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


# -----------------------------------------------------------------------------
# Encryption pass
# -----------------------------------------------------------------------------
def _ensure_encrypted(config):
    """Encrypt any sensitive field that's non-empty plaintext.
    Returns True if anything was changed (caller should rewrite the file).
    """
    changed = False
    for path in SENSITIVE_FIELDS:
        val = _get_nested(config, path)
        if isinstance(val, str) and val and not val.startswith("ENC:"):
            _set_nested(config, path, encrypt(val))
            changed = True
    return changed


def _validate_plaintext_payment(config, errors):
    """Check payment-field formats BEFORE encryption.
    Once a field is encrypted we can't re-validate format, so do it here.
    """
    pay = config.get("payment", {})
    card = pay.get("card_number", "")
    exp = pay.get("expiration", "")
    if isinstance(card, str) and card and not card.startswith("ENC:"):
        if e := _err_card_number(card):
            errors.append(f"payment.card_number: {e}")
    if isinstance(exp, str) and exp and not exp.startswith("ENC:"):
        if e := _err_expiration(exp):
            errors.append(f"payment.expiration: {e}")


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------
def _strip_empty_strings(d):
    """Copy of a config dict with empty-string leaves removed, so the lenient
    (require_complete=False) pass treats a cleared GUI field as absent rather
    than malformed. Does not mutate the caller's dict."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _strip_empty_strings(v)
        elif v == "":
            continue
        else:
            out[k] = v
    return out


def validate_config(config, require_complete=True, require_booking=None):
    """Validate the (encrypted) config. Raises ConfigError with all problems.

    require_complete=True (CLI/run path) also enforces required-field
    completeness and calibration. False (GUI save path) checks only the
    format/type/range of values that are present and non-empty, so a
    partially filled config can still be saved.

    require_booking gates the booking-specific completeness checks (booking
    date/time, player counts and names, notification recipients). It defaults
    to require_complete, so a normal run enforces them. The reservations
    features (refresh, cancel) pass require_booking=False with
    require_complete=True: they need valid sign-in (username, password,
    booking_url, Cloudflare calibration) but nothing about a future booking,
    so a stale booking_date never blocks them.
    """
    if require_booking is None:
        require_booking = require_complete
    errors = []

    if not require_complete:
        config = _strip_empty_strings(config)

    # Required non-empty; sensitive ones must be encrypted.
    for path in REQUIRED_NONEMPTY:
        val = _get_nested(config, path)
        if not val:
            if require_complete:
                errors.append(f"{path}: required, currently empty")
        elif (
            path in SENSITIVE_FIELDS
            and isinstance(val, str)
            and not val.startswith("ENC:")
        ):
            errors.append(f"{path}: sensitive field not encrypted (re-run setup)")

    if require_complete or "booking_url" in config:
        if e := _err_url(config.get("booking_url", "")):
            errors.append(f"booking_url: {e}")

    # A present-but-past date/time is stale, not malformed: it only blocks a
    # run (require_complete), never a save.
    booking_date = config.get("booking_date", "")
    if require_booking:
        if e := _err_date(booking_date):
            errors.append(f"booking_date: {e}")
    elif booking_date:
        if e := _err_date_format(booking_date):
            errors.append(f"booking_date: {e}")

    num_players = config.get("num_players")
    if require_booking or num_players is not None:
        if e := _err_int_range(num_players, 1, 4):
            errors.append(f"num_players: {e}")

    if require_booking:
        if e := _err_time_for_date(config.get("desired_time", ""), booking_date):
            errors.append(f"desired_time: {e}")
    elif config.get("desired_time"):
        if e := _err_time(config.get("desired_time", "")):
            errors.append(f"desired_time: {e}")

    pref = config.get("preference_if_equal", "")
    if (require_booking or pref) and pref not in ("earlier", "later"):
        errors.append("preference_if_equal: must be 'earlier' or 'later'")

    # additional_players: list of "LastName, FirstName" strings.
    # Length must equal num_players - 1 (current player + others).
    # The lenient pass skips the length coupling and empty slots; only a
    # malformed non-empty name blocks a save.
    addl = config.get("additional_players", [])
    if not require_booking:
        if not isinstance(addl, list):
            errors.append("additional_players: must be a list")
        else:
            for i, p in enumerate(addl):
                if not isinstance(p, str) or not p.strip():
                    continue
                parts = [s.strip() for s in p.split(",")]
                if len(parts) != 2 or not all(parts):
                    errors.append(
                        f"additional_players[{i}]: expected 'LastName, FirstName' format, got {p!r}"
                    )
    elif isinstance(num_players, int) and not isinstance(num_players, bool) and num_players >= 1:
        expected = num_players - 1
        if not isinstance(addl, list):
            errors.append("additional_players: must be a list")
        elif len(addl) != expected:
            errors.append(
                f"additional_players: expected {expected} entries "
                f"(num_players={num_players}), got {len(addl)}"
            )
        else:
            for i, p in enumerate(addl):
                if not isinstance(p, str) or not p.strip():
                    errors.append(f"additional_players[{i}]: must be a non-empty string")
                    continue
                parts = [s.strip() for s in p.split(",")]
                if len(parts) != 2 or not all(parts):
                    errors.append(
                        f"additional_players[{i}]: expected 'LastName, FirstName' format, got {p!r}"
                    )

    # email_notifications (only checked if enabled)
    enotif = config.get("email_notifications", {})
    if enotif.get("enabled"):
        to_emails = enotif.get("to_emails", [])
        if not isinstance(to_emails, list) or not to_emails:
            if require_booking:
                errors.append("email_notifications.to_emails: required when enabled")
            elif not isinstance(to_emails, list) and to_emails:
                errors.append("email_notifications.to_emails: must be a list")
        else:
            for i, em in enumerate(to_emails):
                if e := _err_email(em or ""):
                    errors.append(f"email_notifications.to_emails[{i}]: {e}")

    # failure_contacts checked whenever present (failure alerts fire regardless of
    # the success-notification 'enabled' flag). May include SMS-gateway addresses.
    for i, em in enumerate(enotif.get("failure_contacts", []) or []):
        if e := _err_email(em or ""):
            errors.append(f"email_notifications.failure_contacts[{i}]: {e}")

    # heartbeat_contacts: the heartbeat recipient list, used as-is at send
    # time (empty falls back to HEARTBEAT_CONTACTS). Checked whenever present.
    hb = enotif.get("heartbeat_contacts", [])
    if hb is not None and not isinstance(hb, list):
        errors.append("email_notifications.heartbeat_contacts: must be a list")
    else:
        for i, em in enumerate(hb or []):
            if e := _err_email(em or ""):
                errors.append(f"email_notifications.heartbeat_contacts[{i}]: {e}")
    if "local_popup" in enotif and not isinstance(enotif.get("local_popup"), bool):
        errors.append("email_notifications.local_popup: must be a boolean")

    # auto_booking (only checked if enabled). New keys are optional — code
    # falls back to sane defaults — but must be valid when present, so a typo
    # can't silently misconfigure release night.
    auto = config.get("auto_booking", {})
    if auto.get("enabled"):
        if e := _err_time(auto.get("release_time", "06:00")):
            errors.append(f"auto_booking.release_time: {e}")
        if e := _err_int_range(auto.get("prep_lead_minutes", 15), 2, 120):
            errors.append(f"auto_booking.prep_lead_minutes: {e}")
        if e := _err_int_range(auto.get("strike_max_minutes", 30), 1, 240):
            errors.append(f"auto_booking.strike_max_minutes: {e}")
        if e := _err_int_range(auto.get("strike_poll_seconds", 2), 1, 60):
            errors.append(f"auto_booking.strike_poll_seconds: {e}")
        if e := _err_int_range(auto.get("widen_after_seconds", 120), 0, 3600):
            errors.append(f"auto_booking.widen_after_seconds: {e}")
        if e := _err_int_range(auto.get("check_interval_seconds"), 1, 3600):
            errors.append(f"auto_booking.check_interval_seconds: {e}")
        if e := _err_int_range(auto.get("max_attempts"), 1, 100000):
            errors.append(f"auto_booking.max_attempts: {e}")
        if e := _err_int_range(auto.get("open_days_before", 14), 1, 60):
            errors.append(f"auto_booking.open_days_before: {e}")
        if e := _err_int_range(auto.get("probe_attempts", 2), 1, 100):
            errors.append(f"auto_booking.probe_attempts: {e}")

    # waiting_mode: checked whenever the block is PRESENT, not gated on
    # enabled, because the GUI's Run Mode combobox launches the waiting verb
    # regardless of that flag, so bad values must never hide behind
    # enabled=false. enabled itself is only the default-run flag. Keys are
    # optional with defaults, but must be valid when present so a typo
    # can't silently misconfigure a multi-hour run.
    wm = config.get("waiting_mode", {})
    if wm:
        if "enabled" in wm and not isinstance(wm.get("enabled"), bool):
            errors.append("waiting_mode.enabled: must be a boolean")
        recon_min = wm.get("reconnect_min_minutes", 60)
        recon_max = wm.get("reconnect_max_minutes", 180)
        if e := _err_int_range(recon_min, 5, 1440):
            errors.append(f"waiting_mode.reconnect_min_minutes: {e}")
        if e := _err_int_range(recon_max, 5, 1440):
            errors.append(f"waiting_mode.reconnect_max_minutes: {e}")
        elif (isinstance(recon_min, int) and not isinstance(recon_min, bool)
                and recon_max < recon_min):
            errors.append("waiting_mode.reconnect_max_minutes: must be >= "
                          "reconnect_min_minutes")
        if e := _err_int_range(wm.get("reconnect_jitter_seconds", 300), 0, 3600):
            errors.append(f"waiting_mode.reconnect_jitter_seconds: {e}")
        if e := _err_int_range(wm.get("in_session_poll_seconds", 30), 10, 600):
            errors.append(f"waiting_mode.in_session_poll_seconds: {e}")
        # 0 = uncapped: the poll loop runs until the site itself ends the
        # session, so its real lifetime is measured instead of assumed.
        if e := _err_int_range(wm.get("session_max_minutes", 0), 0, 60):
            errors.append(f"waiting_mode.session_max_minutes: {e}")
        if e := _err_int_range(wm.get("fee_period_hours", 48), 1, 168):
            errors.append(f"waiting_mode.fee_period_hours: {e}")
        for key in ("auto_take", "continuous_upgrade",
                    "cancel_existing_on_upgrade", "continue_into_fee_period",
                    "fee_period_auto_swap"):
            if key in wm and not isinstance(wm.get(key), bool):
                errors.append(f"waiting_mode.{key}: must be a boolean")

    for key in ("earlier_tolerance_minutes", "later_tolerance_minutes"):
        if key in config:
            if e := _err_int_range(config.get(key), 0, 240):
                errors.append(f"{key}: {e}")

    cf = config.get("cloudflare_checkbox", {})
    # Only range-check coordinates if user claims they're calibrated; otherwise
    # the values are placeholders and CalibrationRequired below will fire.
    if cf.get("calibrated"):
        for key in ("relative_x", "relative_y"):
            v = cf.get(key)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0.0 <= v <= 1.0):
                errors.append(f"cloudflare_checkbox.{key}: must be a number in [0.0, 1.0]")

    if errors:
        raise ConfigError("Config validation failed:\n  - " + "\n  - ".join(errors))

    # Calibration is a run-completeness concern, not a save blocker.
    if require_complete and not cf.get("calibrated"):
        raise CalibrationRequired(
            "Cloudflare checkbox not calibrated. "
            "Run: python run_setup.py calibrate"
        )


# -----------------------------------------------------------------------------
# Orchestrator
# -----------------------------------------------------------------------------
def prepare_config(require_booking=True):
    """Ensure config exists, encrypt sensitive fields, validate.
    Returns the (encrypted) config dict on success.
    Raises ConfigError on any failure.

    require_booking=False skips the booking-specific completeness checks so
    the reservations verbs validate only sign-in essentials (see
    validate_config).
    """
    load_key()  # creates .secret.key if missing

    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=4))
        raise ConfigError(
            f"No config found. Default written to {CONFIG_FILE}.\n"
            "Edit the file (especially payment, credentials, and dates), then "
            "re-run: python run_setup.py"
        )

    try:
        config = json.loads(CONFIG_FILE.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"config.json is not valid JSON: {e}")

    # Format-check payment fields BEFORE encryption (can't check after).
    pre_errors = []
    _validate_plaintext_payment(config, pre_errors)
    if pre_errors:
        raise ConfigError(
            "Config validation failed (pre-encryption):\n  - "
            + "\n  - ".join(pre_errors)
        )

    if _ensure_encrypted(config):
        CONFIG_FILE.write_text(json.dumps(config, indent=4))

    validate_config(config, require_booking=require_booking)
    return config


# -----------------------------------------------------------------------------
# Verification helper
# -----------------------------------------------------------------------------
def decrypt_field(field_path):
    """Return the plaintext value of a (possibly encrypted) field, or None.
    Useful for checking what's stored without decrypting the whole config.
    """
    if not CONFIG_FILE.exists():
        return None
    config = json.loads(CONFIG_FILE.read_text())
    val = _get_nested(config, field_path)
    if isinstance(val, str) and val.startswith("ENC:"):
        return decrypt(val)
    return val


# -----------------------------------------------------------------------------
# Cloudflare checkbox calibration
# -----------------------------------------------------------------------------
def _detect_chrome_major():
    """Read the installed Chrome major version from the Windows registry, or
    None. Mirrors the bot's detection so calibration uses the same driver."""
    import subprocess
    keys = [
        r"HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon",
        r"HKEY_LOCAL_MACHINE\SOFTWARE\Google\Chrome\BLBeacon",
        r"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon",
    ]
    for key in keys:
        try:
            out = subprocess.check_output(
                ["reg", "query", key, "/v", "version"],
                stderr=subprocess.DEVNULL, text=True,
            )
            m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
            if m:
                return int(m.group(1))
        except Exception:
            continue
    return None


def _clear_uc_cache():
    """Wipe UC's patched-driver cache so it re-downloads on next launch."""
    import os
    import shutil
    cache = Path(os.environ.get("APPDATA", "")) / "undetected_chromedriver"
    if cache.exists():
        try:
            shutil.rmtree(cache)
        except OSError:
            pass


def init_uc_driver(log_fn=None):
    """Launch undetected_chromedriver matched to the installed Chrome, with a
    fallback ladder so Chrome/driver updates are handled gracefully:

        1. version_main = the detected Chrome major version
        2. on a version / 'session not created' error: clear UC's cache and
           retry the detected version (handles a stale cached driver after a
           Chrome auto-update)
        3. still failing: clear cache and let uc auto-resolve (version_main=None)

    A non-version error is raised immediately. Detection runs every call, so
    nothing is pinned to a fixed version — it always tracks whatever Chrome is
    installed. Returns the driver; the caller manages window state. Pass
    log_fn (e.g. print or log.info) to surface progress messages.
    """
    import undetected_chromedriver as uc

    def say(msg):
        if log_fn:
            log_fn(msg)

    chrome_major = _detect_chrome_major()
    if chrome_major:
        say(f"Detected Chrome major version: {chrome_major}")

    # Later rungs clear the cache first; final rung lets uc auto-detect.
    versions = [chrome_major, chrome_major, None] if chrome_major else [None, None]

    last_err = None
    for idx, version in enumerate(versions):
        if idx > 0:
            _clear_uc_cache()
            say("Driver/Chrome version mismatch; cleared UC cache, retrying...")
        try:
            return uc.Chrome(options=uc.ChromeOptions(), version_main=version)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "version" in msg or "session not created" in msg:
                continue
            raise
    raise last_err


def _wait_for_capture_keypress():
    """Block until the user presses ENTER/space, WITHOUT moving the mouse.

    Fallback for the windowed (console=False) frozen build, where sys.stdin is
    lost and input() raises RuntimeError. A tiny always-on-top window takes the
    keyboard focus while the mouse stays hovering over the checkbox, so
    pyautogui.position() still reads the intended cursor location.
    """
    import tkinter as tk

    root = tk.Tk()
    root.title("Calibrate Cloudflare")
    root.attributes("-topmost", True)
    tk.Label(
        root,
        text=("Hover the mouse over the Cloudflare checkbox,\n"
              "then press ENTER (do not click or move the mouse)."),
        padx=20, pady=20, justify="center",
    ).pack()
    root.bind("<Return>", lambda e: root.destroy())
    root.bind("<space>", lambda e: root.destroy())
    root.update_idletasks()
    root.lift()
    root.focus_force()
    root.mainloop()


def calibrate_cloudflare():
    """Launch a browser, capture mouse position over the Cloudflare checkbox,
    and save the relative coordinates to config.json.

    Reads/writes config.json directly, leaving any ENC: fields untouched
    (they're handled as opaque strings by json.load/dump).
    """
    if not CONFIG_FILE.exists():
        raise ConfigError(f"No config at {CONFIG_FILE}. Run setup first.")

    config = json.loads(CONFIG_FILE.read_text())
    booking_url = config.get("booking_url", "")
    if not booking_url:
        raise ConfigError("booking_url is not set in config.")

    try:
        import pyautogui
        import undetected_chromedriver as uc
    except ImportError as e:
        raise ConfigError(
            f"Calibration requires pyautogui and undetected_chromedriver: {e}\n"
            "Install with: pip install undetected-chromedriver pyautogui"
        )

    print(f"Launching browser to: {booking_url}")
    print("When the Cloudflare checkbox is visible, hover your mouse over it,")
    print("then return to this terminal and press ENTER to capture the position.")
    print("(Press Ctrl+C to cancel.)\n")

    # Match the bot's run-time browser (undetected_chromedriver, maximized) so
    # the captured window geometry — toolbar/infobar height and viewport size —
    # matches what the clicker reconstructs at run time. A plain-Chrome
    # calibration has different chrome height and shifts the click vertically.
    driver = init_uc_driver(log_fn=print)

    try:
        driver.maximize_window()
        driver.get(booking_url)
        # A real terminal (dev CLI run) still uses input(); the windowed frozen
        # build has no stdin, so fall back to a keypress-capturing window.
        if sys.stdin is not None and sys.stdin.isatty():
            input("Press ENTER when mouse is hovering over the checkbox...")
        else:
            _wait_for_capture_keypress()

        mouse_x, mouse_y = pyautogui.position()
        info = driver.execute_script("""
            return {
                x: window.screenX || window.screenLeft || 0,
                y: window.screenY || window.screenTop || 0,
                outerH: window.outerHeight,
                innerH: window.innerHeight,
                innerW: window.innerWidth,
                sw: screen.width, sh: screen.height,
                dpr: window.devicePixelRatio || 1
            };
        """)
    finally:
        driver.quit()

    # This MUST be the exact inverse of
    # TimmysTeeTimeBooker._get_scaled_coordinates, or a point captured at the
    # checkbox center reconstructs off-center. The forward transform is
    #   x = scale_x * (screenX + rel_x * innerW)
    #   y = scale_y * (screenY + (outerH - innerH) + rel_y * innerH)
    # so the scale is the physical-desktop / CSS-screen ratio (equals dpr when
    # Chrome is DPI-aware), and screenX/screenY are CSS px that are NOT divided
    # by the scale. The earlier formula divided them and used dpr directly,
    # drifting the click down by screenY*(scale-1) whenever the window was not
    # flush to a screen edge (the calibrated-fallback misalignment).
    scale_x = scale_y = info["dpr"]
    try:
        size = pyautogui.size()
        if info.get("sw") and info.get("sh"):
            scale_x = size.width / info["sw"]
            scale_y = size.height / info["sh"]
    except Exception:
        pass
    rel_x = (mouse_x / scale_x - info["x"]) / info["innerW"]
    rel_y = (mouse_y / scale_y - info["y"]
             - (info["outerH"] - info["innerH"])) / info["innerH"]

    if not (0.0 <= rel_x <= 1.0 and 0.0 <= rel_y <= 1.0):
        raise ConfigError(
            f"Calculated coordinates ({rel_x:.4f}, {rel_y:.4f}) are out of [0, 1]. "
            "The mouse may have been outside the browser viewport. Try again."
        )

    print(f"\nCaptured: relative_x={rel_x:.4f}, relative_y={rel_y:.4f}")
    # As with the hover prompt above, the windowed frozen build has no stdin;
    # fall back to a dialog. The mouse position is already captured, so a
    # clickable dialog is fine here.
    if sys.stdin is not None and sys.stdin.isatty():
        confirm = input("Save these to config? [y/N]: ").strip().lower()
        save = confirm == "y"
    else:
        import tkinter as tk
        import tkinter.messagebox as _mb
        _root = tk.Tk()
        _root.withdraw()
        _root.attributes("-topmost", True)
        save = _mb.askyesno(
            "Calibrate Cloudflare",
            f"Captured relative_x={rel_x:.4f}, relative_y={rel_y:.4f}.\n\n"
            "Save these to config?",
            parent=_root,
        )
        _root.destroy()
    if not save:
        print("Not saved.")
        return

    config.setdefault("cloudflare_checkbox", {})
    config["cloudflare_checkbox"]["relative_x"] = rel_x
    config["cloudflare_checkbox"]["relative_y"] = rel_y
    config["cloudflare_checkbox"]["calibrated"] = True
    CONFIG_FILE.write_text(json.dumps(config, indent=4))
    print(f"Saved to {CONFIG_FILE}")


# -----------------------------------------------------------------------------
# Orchestrator: full interactive setup flow
# -----------------------------------------------------------------------------
def run_setup():
    """Full setup: bootstrap, encrypt, validate, prompt for calibration if needed.
    Returns True on success, False if the user declined calibration.
    Raises ConfigError on validation failures other than calibration.
    """
    try:
        prepare_config()
    except CalibrationRequired:
        print("Cloudflare checkbox not yet calibrated.")
        ans = input("Run calibration now? [Y/n]: ").strip().lower()
        if ans == "n":
            print(
                "Skipped. Run 'python run_setup.py calibrate' before booking.",
                file=sys.stderr,
            )
            return False
        calibrate_cloudflare()
        # Re-validate to confirm everything's good post-calibration
        prepare_config()

    print(f"Config OK: {CONFIG_FILE}")
    return True


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if cmd == "decrypt":
        if len(sys.argv) < 3:
            print("Usage: python run_setup.py decrypt FIELD_PATH", file=sys.stderr)
            sys.exit(2)
        result = decrypt_field(sys.argv[2])
        print(result if result is not None else "(no value)")
        return

    if cmd == "calibrate":
        try:
            calibrate_cloudflare()
        except ConfigError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        return

    try:
        run_setup()
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()