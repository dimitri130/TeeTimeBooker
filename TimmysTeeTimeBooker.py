"""
Automated Tee Time Booking Script for EZLinks Golf courses

Usage:
  py TimmysTeeTimeBooker.py           Run based on config (auto or single)
  py TimmysTeeTimeBooker.py single    Force single booking attempt
  py TimmysTeeTimeBooker.py auto      Force auto-monitoring mode
  py TimmysTeeTimeBooker.py waiting   Hunt for an opening on an already-released day
  py TimmysTeeTimeBooker.py setup     Run encryption setup
  py TimmysTeeTimeBooker.py calibrate Run Cloudflare calibration
  py TimmysTeeTimeBooker.py reservations  Show current reservations (cached)
  py TimmysTeeTimeBooker.py reservations-refresh  Force a live reservations refresh
  py TimmysTeeTimeBooker.py cancel <conf#>  Cancel a reservation by confirmation number
  py TimmysTeeTimeBooker.py help      Show help

Dependencies:
  py -m pip install undetected-chromedriver selenium cryptography pyautogui
"""

from __future__ import annotations

import atexit
import base64
import json
import logging
import logging.handlers
import math
import signal
import os
import random
import re
import smtplib
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
    InvalidSessionIdException,
    WebDriverException,
)

from crypto_utils import decrypt
from run_setup import (
    prepare_config,
    run_setup,
    calibrate_cloudflare,
    init_uc_driver,
    ConfigError,
    CalibrationRequired,
    SENDER_EMAIL,
    SENDER_PASSWORD,
    SMTP_SERVER,
    SMTP_PORT,
    DEFAULT_FAILURE_CONTACTS,
    HEARTBEAT_CONTACTS,
)

# =============================================================================
# CONFIGURATION & LOGGING
# =============================================================================
BASE_DIR = (
    Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
)
LOG_FILE = BASE_DIR / "tee_time_booking.log"

# Cloudflare challenge container IDs (used to detect the interstitial).
CLOUDFLARE_IDS = ["challenge-form", "challenge-running", "cf-wrapper"]

# Default timeouts (seconds).
DEFAULT_WAIT = 15
# Must exceed one worst-case _submit_login_form pass (up to 3x 15s inner
# waits) plus one recovery re-navigation, or the retry loop is fiction.
LOGIN_TIMEOUT = 60
CLOUDFLARE_TIMEOUT = 30

# Cloudflare click cadence and recovery search.
# First-click delay is generous on purpose: clicking before the widget has
# fully laid out lands on a half-rendered box (or the pre-render spinner), and
# an early miss then triggers the nudge-search below, which can walk the cursor
# off a checkbox it was only barely on. Waiting for a stable layout first
# avoids both.
CLOUDFLARE_FIRST_CLICK_DELAY = 4.0  # let the challenge widget finish rendering
CLOUDFLARE_CLICK_INTERVAL = 3.0  # seconds between click attempts
CLOUDFLARE_SEARCH_AFTER = 5      # failed clicks before nudging around the point
# Small px nudges (expanding) around the calibrated point, tried in order once
# the direct clicks fail. Handles a checkbox that sits just beside the cursor.
CLOUDFLARE_SEARCH_OFFSETS = [
    (-6, 0), (6, 0), (0, -6), (0, 6),
    (-6, -6), (6, -6), (-6, 6), (6, 6),
    (-12, 0), (12, 0), (0, -12), (0, 12),
]
CLOUDFLARE_CLICK_JITTER = 5       # px of slight random variation per click target

# Strike-phase defaults (all overridable via auto_booking config keys).
RELEASE_TIME_DEFAULT = "06:00"     # when the course releases tee times
# Keep the lead SHORT: the site expires idle logins ~15 minutes in, so a
# session warmed too early is dead by release. 5 min is measured-safe.
PREP_LEAD_MINUTES = 5              # start warming the session this long before release
STRIKE_MAX_MINUTES = 15            # keep striking this long past release before Tier-3 fallback
STRIKE_POLL_SECONDS = 2            # in-page re-search cadence during the strike
WIDEN_AFTER_SECONDS = 90           # widen the time window by the tolerances after this long

# Waiting-mode defaults (all overridable via waiting_mode config keys).
WAIT_RECONNECT_MIN_MINUTES = 60    # slow jittered reconnect cadence (fresh login)
WAIT_RECONNECT_MAX_MINUTES = 180
WAIT_RECONNECT_JITTER_SECONDS = 300
WAIT_POLL_SECONDS = 30             # cheap in-session re-search cadence
WAIT_FEE_PERIOD_HOURS = 48         # hunt deadline = tee time minus this
WAIT_PROXIMITY_MINUTES = 60        # site rejects a 2nd same-day booking this close


class AlreadyBookedError(Exception):
    """The site rejected the booking because a reservation already exists for
    this day. Terminal success-equivalent: verify and report, don't retry."""


class PageChangedError(Exception):
    """The page structure no longer matches what the automation expects.
    Retrying cannot help; alert a human immediately."""


class SessionDeadError(Exception):
    """The browser session is gone; only a full restart (Tier 3) can help."""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# =============================================================================
# DRIVER CLEANUP SAFETY NET
# =============================================================================
# Every Chrome we launch is registered here so an interrupt (Ctrl-C, SIGTERM,
# normal exit) tears all of them down instead of orphaning windows. undetected-
# chromedriver sometimes detaches Chrome from the process tree, so a parent
# kill alone can leave windows behind; quitting each driver closes its windows.
_LIVE_DRIVERS: set = set()


def _quit_all_drivers():
    for d in list(_LIVE_DRIVERS):
        try:
            d.quit()
        except Exception:
            pass
        _LIVE_DRIVERS.discard(d)


atexit.register(_quit_all_drivers)


def _signal_cleanup(signum, _frame):
    log.info(f"Signal {signum} received; closing browsers and exiting.")
    _quit_all_drivers()
    raise SystemExit(1)


for _sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(_sig, _signal_cleanup)
    except (ValueError, OSError):
        pass  # not on the main thread / unsupported platform


# =============================================================================
# SINGLE-INSTANCE LOCK
# =============================================================================
# Two concurrent bookers collide patching the shared chromedriver exe and
# fight over the one system mouse cursor during Cloudflare mitigation (both
# confirmed in the 2026-07-11 failure). A PID lockfile makes the booking modes
# mutually exclusive; a lock left behind by a force-killed process is detected
# as stale (its PID is no longer a live booker) and taken over.
LOCK_FILE = BASE_DIR / "ttb.lock"


def _pid_is_booker(pid: int) -> bool:
    """True when `pid` is a live process that looks like a booker run
    (python or the frozen TimmysTeeTimeBooker exe)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout.lower()
    except Exception:
        return False
    return f'"{pid}"' in out and (
        "python" in out or "timmysteetimebooker" in out)


def _pid_start_time(pid: int) -> float | None:
    """Process creation time as a Unix timestamp (Windows), or None."""
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(
                    handle, *(ctypes.byref(t) for t in times)):
                return None
            filetime = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            # FILETIME is 100ns ticks since 1601-01-01 UTC.
            return filetime / 10_000_000 - 11_644_473_600
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _other_instance_pid() -> int | None:
    """PID of a live booker (not this process) holding the lock, or None."""
    try:
        if LOCK_FILE.exists():
            holder = int(LOCK_FILE.read_text(encoding="utf-8").strip() or 0)
            if holder and holder != os.getpid() and _pid_is_booker(holder):
                # PID-reuse guard: if the process now at this PID started
                # AFTER the lock was written, it is not the writer — Windows
                # recycled the number onto an innocent python/GUI process.
                # (2s slack covers filesystem/clock granularity.)
                started = _pid_start_time(holder)
                locked = LOCK_FILE.stat().st_mtime
                if started is not None and started > locked + 2:
                    log.warning(
                        f"Lock PID {holder} was recycled (process started "
                        "after the lock was written); treating lock as stale.")
                    return None
                return holder
    except Exception as e:
        log.warning(f"Lock check failed ({e}); treating lock as absent.")
    return None


def _acquire_single_instance_lock() -> bool:
    """Take the instance lock. Returns False (after alerting) when another
    live booker already holds it."""
    holder = _other_instance_pid()
    if holder:
        try:
            locked_at = datetime.fromtimestamp(LOCK_FILE.stat().st_mtime)
        except OSError:
            locked_at = datetime.now()
        msg = (
            f"Another TeeTimeBooker instance is already running "
            f"(PID {holder}, lock written "
            f"{locked_at:%Y-%m-%d %H:%M:%S}). This launch "
            f"(PID {os.getpid()}) is exiting so the two can't fight "
            f"over the driver and the mouse. If the holder is a stray "
            f"you meant to replace, kill it first:\n"
            f"  taskkill /F /T /PID {holder}\nthen relaunch."
        )
        log.error(msg)
        send_mail_raw(
            "TeeTimeBooker BLOCKED: duplicate instance", msg,
            heartbeat_recipients())
        return False
    try:
        if LOCK_FILE.exists():
            log.warning("Stale lock found (holder is not a live booker); "
                        "taking over.")
    except OSError:
        pass
    try:
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as e:
        log.warning(f"Could not write lock file: {e}")
    return True


def _release_single_instance_lock() -> None:
    """Remove the lock, but only if THIS process owns it."""
    try:
        if (LOCK_FILE.exists()
                and LOCK_FILE.read_text(encoding="utf-8").strip()
                == str(os.getpid())):
            LOCK_FILE.unlink()
    except Exception:
        pass


# atexit runs LIFO. _quit_all_drivers was registered first (above), so the
# lock would release while this instance's chromedriver may still be alive,
# letting a relauncher patch the shared driver exe under it. Re-register the
# driver teardown so it runs BEFORE the lock release.
atexit.register(_release_single_instance_lock)
atexit.unregister(_quit_all_drivers)
atexit.register(_quit_all_drivers)


# =============================================================================
# SESSION / DESKTOP ATTACHMENT
# =============================================================================
def _ensure_desktop_attached() -> None:
    """If this process lives in a DISCONNECTED RDP session, reattach the
    session to the console (tscon /dest:console) so the desktop is rendered
    at native geometry and physical clicks land. Strictly a no-op while a
    user is actively connected — it can never kick a live RDP viewer."""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        wtsapi = ctypes.windll.wtsapi32
        session_id = wintypes.DWORD()
        if not kernel32.ProcessIdToSessionId(
                kernel32.GetCurrentProcessId(), ctypes.byref(session_id)):
            return
        buf = ctypes.c_void_p()
        size = wintypes.DWORD()
        WTS_CONNECT_STATE = 8   # WTSConnectState
        if not wtsapi.WTSQuerySessionInformationW(
                None, session_id, WTS_CONNECT_STATE,
                ctypes.byref(buf), ctypes.byref(size)):
            return
        state = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
        wtsapi.WTSFreeMemory(buf)
        WTS_DISCONNECTED = 4    # WTSDisconnected
        if state == WTS_DISCONNECTED:
            log.warning(
                "Session is disconnected (no rendered desktop for clicks); "
                "reattaching it to the console via tscon.")
            result = subprocess.run(
                ["tscon", str(session_id.value), "/dest:console"],
                capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                log.error(
                    f"tscon reattach failed (rc={result.returncode}): "
                    f"{(result.stderr or result.stdout).strip()} — physical "
                    "clicks may not land until someone connects.")
            else:
                time.sleep(2.0)  # let the console session finish rendering
    except Exception as e:
        log.debug(f"Desktop-attachment check failed: {e}")


# =============================================================================
# ALERTING (module-level: must work even before config loads)
# =============================================================================
# Last successfully loaded config, so module-level alert helpers can honor
# config settings best-effort. None until load_config() succeeds; crash
# paths that fire before then fall back to the hardcoded defaults.
_LOADED_CONFIG: dict | None = None


def heartbeat_recipients(config: dict | None = None) -> list:
    """Effective heartbeat recipient list: email_notifications
    .heartbeat_contacts from the config, used as-is (order-preserving,
    deduplicated) so an edited-out address really stops receiving. An empty
    or missing list falls back to the hardcoded HEARTBEAT_CONTACTS so
    heartbeats can never be silently disabled by accident, as do crash
    paths that fire before any config is loaded."""
    cfg = config if config is not None else _LOADED_CONFIG
    listed = []
    if isinstance(cfg, dict):
        listed = (cfg.get("email_notifications") or {}).get(
            "heartbeat_contacts") or []
    out = []
    for em in listed:
        if isinstance(em, str) and em.strip() and em.strip() not in out:
            out.append(em.strip())
    if not out:
        if isinstance(cfg, dict):
            log.info("No heartbeat_contacts configured; falling back to the "
                     "built-in defaults.")
        return list(HEARTBEAT_CONTACTS)
    return out


def _surface_alert_locally(subject: str, body: str) -> None:
    """Mirror an outgoing alert email locally: the full text at INFO (so it
    lands in the GUI output panel) and a best-effort dismissible popup via
    msg.exe, toggleable with email_notifications.local_popup."""
    log.info(f"ALERT EMAIL: {subject}\n{body}")
    popup = True
    if isinstance(_LOADED_CONFIG, dict):
        popup = (_LOADED_CONFIG.get("email_notifications") or {}).get(
            "local_popup", True)
    if not popup:
        return
    text = f"{subject}\n{body[:200]}"
    try:
        subprocess.run(["msg", os.environ.get("USERNAME", "*"), text],
                       capture_output=True, timeout=10)
    except Exception as e:
        log.debug(f"Local alert popup failed: {e}")


def send_mail_raw(subject: str, body: str, to_emails: list, pdf: str = None) -> bool:
    """Send one plaintext email (optional PDF attachment) via Gmail SMTP.

    Module-level and config-independent (uses the hardcoded SENDER_* constants)
    so crash/heartbeat alerts can fire even when config loading itself failed.
    SENDER_PASSWORD must be a Gmail App Password. Returns True on success.
    """
    if not to_emails:
        log.warning("No recipients configured; skipping email.")
        return False
    # Surface locally even when SMTP then fails: the local copy is the
    # backstop for exactly that case.
    _surface_alert_locally(subject, body)
    try:
        msg = MIMEMultipart()
        msg["From"] = SENDER_EMAIL
        msg["To"] = ", ".join(to_emails)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        if pdf and os.path.exists(pdf):
            with open(pdf, "rb") as f:
                att = MIMEApplication(f.read(), _subtype="pdf")
                att.add_header("Content-Disposition", "attachment",
                               filename=os.path.basename(pdf))
                msg.attach(att)

        log.info(f"Connecting to {SMTP_SERVER}:{SMTP_PORT}...")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SENDER_EMAIL, SENDER_PASSWORD)
            s.send_message(msg)
        log.info("Email sent.")
        return True
    except Exception as e:
        log.error(f"Email failed: {e}")
        return False


def send_crash_alert(stage: str, exc: BaseException) -> None:
    """Last-resort alert for failures outside the booker (config load, unhandled
    exceptions). Goes to the heartbeat contact only."""
    body = (
        f"TeeTimeBooker crashed during: {stage}\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        f"{traceback.format_exc()}"
    )
    send_mail_raw(f"TeeTimeBooker CRASHED ({stage})", body,
                  heartbeat_recipients())


# =============================================================================
# CONFIG LOADING
# =============================================================================
def load_config(require_booking: bool = True) -> dict:
    """Validate (via run_setup) and decrypt config for use by the booker.

    require_booking=False validates only sign-in essentials, so the
    reservations verbs run even when booking_date is stale."""
    global _LOADED_CONFIG
    config = _decrypt_recursive(prepare_config(require_booking=require_booking))
    # decrypt() returns None when an ENC: value can't be decrypted (missing or
    # mismatched .secret.key, e.g. a config.json moved to another machine).
    # Fail here with a clear message instead of driving a browser to a login
    # form with no credentials.
    for field in ("username", "password"):
        if not config.get(field):
            raise SystemExit(
                f"Config error: '{field}' is missing or cannot be decrypted "
                "(wrong/missing .secret.key for this config.json). "
                "Re-enter credentials via setup on this machine."
            )
    _LOADED_CONFIG = config
    return config


def _decrypt_recursive(obj):
    """Walk a structure and decrypt any ENC: strings."""
    if isinstance(obj, dict):
        return {k: _decrypt_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decrypt_recursive(item) for item in obj]
    if isinstance(obj, str) and obj.startswith("ENC:"):
        return decrypt(obj)
    return obj


# =============================================================================
# HUMAN-LIKE MOUSE MOVEMENT
# =============================================================================
class HumanMouse:
    """Simulates human-like mouse movements using Bezier curves."""

    def __init__(self):
        try:
            import pyautogui
        except ImportError:
            log.warning("pyautogui not installed - mouse simulation disabled")
            self.pyautogui = None
            self.available = False
            return

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.01
        self.pyautogui = pyautogui
        self.available = True
        log.info("PyAutoGUI initialized for human-like mouse simulation")

    def _bezier_curve(self, start: tuple, end: tuple, control_points: int = 2) -> list:
        """Generate points along a Bezier curve from start to end."""
        controls = [
            (
                (start[0] + end[0]) / 2 + random.randint(-80, 80),
                (start[1] + end[1]) / 2 + random.randint(-40, 40),
            )
            for _ in range(control_points)
        ]
        distance = math.hypot(end[0] - start[0], end[1] - start[1])
        steps = max(25, min(60, int(distance / 8)))

        points = [start]
        for i in range(1, steps + 1):
            t = i / steps
            if control_points == 1:
                p0, p1, p2 = start, controls[0], end
                x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
                y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
            else:
                p0, p1, p2, p3 = start, controls[0], controls[1], end
                x = ((1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0]
                     + 3 * (1 - t) * t ** 2 * p2[0] + t ** 3 * p3[0])
                y = ((1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1]
                     + 3 * (1 - t) * t ** 2 * p2[1] + t ** 3 * p3[1])
            points.append((int(x + random.uniform(-1, 1)), int(y + random.uniform(-1, 1))))
        points.append(end)
        return points

    def move_to(self, x: int, y: int, duration: float = None):
        if not self.available:
            return
        try:
            current = self.pyautogui.position()
            if duration is None:
                dist = math.hypot(x - current[0], y - current[1])
                duration = 0.3 + (dist / 900) + random.uniform(0.08, 0.18)

            points = self._bezier_curve(current, (x, y))
            step_duration = duration / len(points)

            for px, py in points:
                self.pyautogui.moveTo(px, py, duration=max(0.01, step_duration))
                time.sleep(random.uniform(0.002, 0.008))
        except Exception as e:
            log.debug(f"Mouse move failed: {e}")

    def click(self, x: int = None, y: int = None) -> bool:
        if not self.available:
            return False
        try:
            if x is not None and y is not None:
                self.move_to(x, y)
            time.sleep(random.uniform(0.05, 0.15))
            self.pyautogui.mouseDown()
            time.sleep(random.uniform(0.05, 0.12))
            self.pyautogui.mouseUp()
            time.sleep(random.uniform(0.1, 0.2))
            return True
        except Exception as e:
            log.debug(f"Mouse click failed: {e}")
            return False



# =============================================================================
# MAIN BOOKING CLASS
# =============================================================================
class TeeTimeBooker:
    def __init__(self, config: dict):
        self.config = config
        self.driver = None
        self.booking_details = {}
        self.human_mouse = HumanMouse()
        # When true, runs the full flow but stops before the final
        # "Finish Reservation" click so no real booking is made.
        self.testing = bool(config.get("testing", False))
        # Step mode: pause after each stage and wait for the controller (GUI)
        # to send a line on stdin before continuing. Set by the GUI via env var.
        self.step_mode = bool(os.environ.get("TTB_STEP_MODE"))
        # Capture a rendered-DOM dump at each flow stage (config-controlled;
        # turn on for rehearsals so they double as selector-capture sessions).
        self.capture_dumps = bool(config.get("capture_page_dumps"))
        # Local-clock minus server-clock offset (seconds); see _measure_clock_offset.
        self.clock_offset = 0.0
        # One-shot flags so repeated strike attempts don't re-send the same alert.
        self._alerted_page_changed = False
        self._alerted_date_not_open = False

    # -------------------------------------------------------------------------
    # DRIVER LIFECYCLE
    # -------------------------------------------------------------------------
    def setup_driver(self):
        # A disconnected RDP session has no rendered desktop for the
        # Cloudflare clicks; reattach to console first (no-op when someone
        # is actively connected).
        _ensure_desktop_attached()
        # Shared init: detects the installed Chrome version, retries with a
        # cache clear, then falls back to uc auto-detect. Same path calibration
        # uses, so both stay in lockstep across Chrome/driver updates.
        self.driver = init_uc_driver(log_fn=log.info)
        _LIVE_DRIVERS.add(self.driver)
        self.driver.maximize_window()
        # No implicit wait: it makes every find_element(s) call that matches
        # nothing block for the full duration (so empty-result polls like the
        # Cloudflare/login checks stall 15s per call) and it stacks
        # unpredictably with explicit WebDriverWaits. Timing-sensitive
        # lookups use explicit waits instead.
        log.info("WebDriver initialized")

    def close_driver(self):
        if self.driver:
            _LIVE_DRIVERS.discard(self.driver)
            try:
                self.driver.quit()
            except Exception as e:
                log.debug(f"Driver quit failed: {e}")
            else:
                # uc's Chrome.__del__ calls self.quit() again at GC time,
                # which on an already-quit instance spews "OSError:
                # [WinError 6] The handle is invalid" console noise. Stub
                # quit on THIS instance only (instance attributes shadow the
                # method for the __del__-internal call) so the re-quit is a
                # no-op; the class and other instances are untouched.
                try:
                    self.driver.quit = lambda: None
                except Exception:
                    pass
            self.driver = None

    # -------------------------------------------------------------------------
    # ALERTS, DUMPS & CLOCK
    # -------------------------------------------------------------------------
    def send_alert(self, subject: str, body: str, critical: bool = False,
                   pdf: str = None) -> bool:
        """Operational alert. critical=True goes to the failure contacts
        (booking outcomes); otherwise only the heartbeat contact (prep status,
        warnings, page-change alarms)."""
        if critical:
            cfg = self.config.get("email_notifications", {})
            to = cfg.get("failure_contacts") or DEFAULT_FAILURE_CONTACTS
        else:
            to = heartbeat_recipients(self.config)
        return send_mail_raw(subject, body, to, pdf=pdf)

    def _dump_stage(self, stage: str) -> None:
        """Save the rendered DOM for selector pinning / postmortems."""
        if not self.capture_dumps or not self.driver:
            return
        try:
            dump_dir = BASE_DIR / "dumps"
            dump_dir.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = dump_dir / f"{ts}_{stage}.html"
            html = self.driver.execute_script(
                "return document.documentElement.outerHTML;"
            )
            path.write_text(html or "", encoding="utf-8")
            log.info(f"Stage dump saved: {path.name}")
        except Exception as e:
            log.debug(f"Stage dump '{stage}' failed: {e}")

    def _measure_clock_offset(self) -> None:
        """Estimate local-minus-server clock offset from the site's HTTP Date
        header. Works even on a Cloudflare 403: the challenge response still
        carries a Date header. RTT-compensated to the request midpoint."""
        try:
            url = self.config.get("booking_url", "").split("#")[0]
            req = urllib.request.Request(url, method="HEAD")
            t0 = time.time()
            try:
                resp = urllib.request.urlopen(req, timeout=10)
                date_hdr = resp.headers.get("Date")
            except urllib.error.HTTPError as e:
                date_hdr = e.headers.get("Date")  # 403 challenge still has Date
            t1 = time.time()
            if not date_hdr:
                log.warning("No Date header from server; assuming clock is right.")
                return
            server = parsedate_to_datetime(date_hdr).timestamp()
            midpoint = (t0 + t1) / 2
            # Date header has 1s granularity; only trust offsets beyond that.
            offset = midpoint - server
            self.clock_offset = offset if abs(offset) > 1.5 else 0.0
            log.info(
                f"Clock check: local-server offset {offset:+.1f}s "
                f"(applied: {self.clock_offset:+.1f}s, RTT {t1 - t0:.2f}s)"
            )
            if abs(offset) > 30:
                self.send_alert(
                    "TeeTimeBooker WARNING: VM clock skew",
                    f"Local clock differs from the site's server clock by "
                    f"{offset:+.1f}s. Strike timing is being corrected, but "
                    f"fix the VM's NTP sync.",
                )
        except Exception as e:
            log.warning(f"Clock offset check failed ({e}); assuming clock is right.")

    def _now(self) -> datetime:
        """Server-corrected local time."""
        return datetime.now() - timedelta(seconds=self.clock_offset)

    # -------------------------------------------------------------------------
    # WINDOW / COORDINATE UTILS
    # -------------------------------------------------------------------------
    def _bring_to_front(self):
        try:
            self.driver.execute_script("window.focus();")
        except Exception:
            pass

    def _get_scaled_coordinates(self, rel_x: float, rel_y: float):
        """Calculate DPI-aware screen coordinates from a position relative to
        the browser viewport. Used by the Cloudflare clicker."""
        metrics = self.driver.execute_script("""
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
        # Browser-to-input scale. devicePixelRatio is NOT sufficient: in a
        # detached/console session Windows DPI-virtualizes Chrome, which then
        # reports logical pixels (dpr=1, e.g. screen 1024x768) while the real
        # input desktop is physical (e.g. 1536x1152) — every dpr-based click
        # lands short by the hidden factor (the 2026-07-12 unattended
        # Cloudflare failure). The true scale is the ratio of the input
        # desktop to the browser's reported screen; it equals dpr whenever
        # Chrome is DPI-aware, and captures the virtualization when not.
        scale_x = scale_y = metrics["dpr"]
        if self.human_mouse.available and metrics.get("sw"):
            try:
                size = self.human_mouse.pyautogui.size()
                scale_x = size.width / metrics["sw"]
                scale_y = size.height / metrics["sh"]
            except Exception:
                pass
        ui_height = (metrics["outerH"] - metrics["innerH"]) * scale_y
        view_x = (rel_x * metrics["innerW"]) * scale_x
        view_y = (rel_y * metrics["innerH"]) * scale_y
        return (
            int(metrics["x"] * scale_x + view_x),
            int(metrics["y"] * scale_y + ui_height + view_y),
            scale_x,
        )

    # -------------------------------------------------------------------------
    # CLOUDFLARE HANDLING
    # -------------------------------------------------------------------------
    def _is_cloudflare_blocking(self) -> bool:
        if "just a moment" in self.driver.title.lower():
            return True
        css = ", ".join(f"#{cf_id}" for cf_id in CLOUDFLARE_IDS)
        for el in self.driver.find_elements(By.CSS_SELECTOR, css):
            if el.is_displayed():
                return True
        return False

    def handle_cloudflare(self, timeout: int = CLOUDFLARE_TIMEOUT) -> bool:
        if not self._is_cloudflare_blocking():
            return True

        log.warning("Cloudflare challenge detected. Mitigating...")
        # The session may have been disconnected AFTER the browser launched
        # (RDP closed mid-run); reattach here too, or the clicks below land
        # on an unrendered desktop. No-op while someone is connected.
        _ensure_desktop_attached()
        self._dump_cf_fingerprint()
        self._dump_cf_diagnostics("start")
        self._bring_to_front()
        start = time.time()
        attempts = 0
        last_click = 0.0

        while time.time() - start < timeout:
            if not self._is_cloudflare_blocking():
                log.info("Cloudflare cleared.")
                time.sleep(1.5)
                return True

            now = time.time()
            if now - start < CLOUDFLARE_FIRST_CLICK_DELAY:
                time.sleep(0.1)
                continue
            if now - last_click >= CLOUDFLARE_CLICK_INTERVAL:
                # Each click carries its own slight location variation, and the
                # human-like move to that target happens just before the click.
                self._click_cloudflare(attempt=attempts)
                attempts += 1
                last_click = now

            time.sleep(0.5)

        log.error("Cloudflare mitigation timed out.")
        self._dump_cf_diagnostics("timeout")
        return False

    def _dump_cf_fingerprint(self) -> None:
        """Capture the exact browser environment Cloudflare fingerprints, so a
        connected-vs-detached diff names what actually changes when RDP closes.
        The 2026-07-12 controlled run showed Cloudflare passing PASSIVELY when
        connected (spinner only, no checkbox) but escalating to an INTERACTIVE
        checkbox when detached — i.e. the click was never the real problem, the
        environment change on detach is. The prime suspects are software
        rendering (no GPU on a headless console -> WebGL SwiftShader, a classic
        bot signal), the resolution/DPI drop, and lost window focus/visibility;
        this logs all of them plus a timer-throttle probe."""
        try:
            fp = self.driver.execute_script("""
                var gl = null, vendor = 'n/a', renderer = 'n/a';
                try {
                    var c = document.createElement('canvas');
                    gl = c.getContext('webgl') || c.getContext('experimental-webgl');
                    if (gl) {
                        var dbg = gl.getExtension('WEBGL_debug_renderer_info');
                        if (dbg) {
                            vendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL);
                            renderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
                        }
                    }
                } catch (e) { renderer = 'webgl-err:' + e; }
                return {
                    webdriver: navigator.webdriver,
                    ua: navigator.userAgent,
                    platform: navigator.platform,
                    langs: (navigator.languages || []).join(','),
                    cores: navigator.hardwareConcurrency,
                    mem: navigator.deviceMemory,
                    dpr: window.devicePixelRatio,
                    screen: screen.width + 'x' + screen.height,
                    avail: screen.availWidth + 'x' + screen.availHeight,
                    colorDepth: screen.colorDepth,
                    inner: window.innerWidth + 'x' + window.innerHeight,
                    outer: window.outerWidth + 'x' + window.outerHeight,
                    hasFocus: document.hasFocus(),
                    visibility: document.visibilityState,
                    hidden: document.hidden,
                    webglVendor: vendor,
                    webglRenderer: renderer,
                    maxTouch: navigator.maxTouchPoints,
                    chromeObj: (typeof window.chrome !== 'undefined'),
                    plugins: navigator.plugins ? navigator.plugins.length : -1
                };
            """)
            log.info(f"CF-FINGERPRINT: {fp}")
            if isinstance(fp, dict):
                r = str(fp.get("webglRenderer", "")).lower()
                if "swiftshader" in r or "software" in r or "llvmpipe" in r:
                    log.warning("CF-FINGERPRINT: WebGL is SOFTWARE-rendered "
                                f"({fp.get('webglRenderer')}) — a strong bot "
                                "signal; likely why the challenge escalates "
                                "when detached.")
                if fp.get("hasFocus") is False or fp.get("hidden") is True:
                    log.warning("CF-FINGERPRINT: window is unfocused/hidden — "
                                "can break Cloudflare's passive verification.")
            # Timer-throttle probe: a detached/background session may throttle
            # timers, which stalls the challenge's own JS.
            drift = self.driver.execute_async_script("""
                var cb = arguments[0], t0 = performance.now();
                setTimeout(function () { cb(performance.now() - t0); }, 100);
            """)
            log.info(f"CF-FINGERPRINT: 100ms timer actually took {drift:.0f}ms")
        except Exception as e:
            log.warning(f"CF-FINGERPRINT capture failed: {e}")

    def _cf_challenge_state(self) -> str:
        """'interactive' if the Verify-you-are-human checkbox/iframe is present,
        'passive' if only the spinner is up, 'unknown' otherwise. Makes the
        passive-vs-interactive distinction explicit in the log."""
        try:
            return self.driver.execute_script("""
                var ifr = document.querySelector(
                    'iframe[src*="challenges.cloudflare.com"]');
                if (ifr) {
                    var r = ifr.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) return 'interactive';
                }
                var cbs = document.querySelectorAll('input[type=checkbox]');
                for (var i = 0; i < cbs.length; i++) {
                    if (cbs[i].getBoundingClientRect().width > 0)
                        return 'interactive';
                }
                return 'passive';
            """) or "unknown"
        except Exception:
            return "unknown"

    def _dump_cf_diagnostics(self, tag: str) -> None:
        """Log desktop/window/input state and save a REAL desktop screenshot.
        Purpose: discriminate the unattended-failure modes — a session that
        isn't rendered (screenshot black/fails, mouse moves don't land) vs a
        resolution/layout shift (screenshot fine but the click target is off
        the checkbox). Only runs when a Cloudflare challenge is in play."""
        try:
            desktop = (self.human_mouse.pyautogui.size()
                       if self.human_mouse.available else None)
            mouse = (self.human_mouse.pyautogui.position()
                     if self.human_mouse.available else None)
            metrics = self.driver.execute_script("""
                return {x: window.screenX||0, y: window.screenY||0,
                        outerW: window.outerWidth, outerH: window.outerHeight,
                        innerW: window.innerWidth, innerH: window.innerHeight,
                        dpr: window.devicePixelRatio||1,
                        sw: screen.width, sh: screen.height};
            """)
            point = self._cloudflare_point()
            state = self._cf_challenge_state()
            widget = self._cloudflare_widget_rect()
            log.info(
                f"CF-DIAG[{tag}]: state={state}, desktop={desktop}, "
                f"mouse_at={mouse}, "
                f"screen={metrics['sw']}x{metrics['sh']}, "
                f"window={metrics['outerW']}x{metrics['outerH']} at "
                f"({metrics['x']},{metrics['y']}), "
                f"inner={metrics['innerW']}x{metrics['innerH']}, "
                f"dpr={metrics['dpr']}, click_target={point}, "
                f"widget_rect={widget}")
        except Exception as e:
            log.warning(f"CF-DIAG[{tag}]: metrics failed: {e}")
        try:
            if self.human_mouse.available:
                shot = BASE_DIR / f"cf_diag_{tag}_{datetime.now():%H%M%S}.png"
                img = self.human_mouse.pyautogui.screenshot()
                # Draw a crosshair at the computed click target so aim-vs-
                # checkbox is visible in the image without interpreting coords.
                try:
                    from PIL import ImageDraw
                    pt = self._cloudflare_point()
                    if pt:
                        d = ImageDraw.Draw(img)
                        cx, cy = pt
                        d.line([(cx - 20, cy), (cx + 20, cy)], fill=(255, 0, 0), width=2)
                        d.line([(cx, cy - 20), (cx, cy + 20)], fill=(255, 0, 0), width=2)
                        d.ellipse([cx - 10, cy - 10, cx + 10, cy + 10],
                                  outline=(255, 0, 0), width=2)
                except Exception as e:
                    log.debug(f"crosshair draw failed: {e}")
                img.save(str(shot))
                lo, hi = img.convert("L").getextrema()
                if hi == 0:
                    log.warning(f"CF-DIAG[{tag}]: screenshot is PURE BLACK — "
                                "the session has no rendered desktop.")
                log.info(f"CF-DIAG[{tag}]: desktop screenshot saved "
                         f"{shot.name} (luma range {lo}-{hi})")
        except Exception as e:
            log.warning(f"CF-DIAG[{tag}]: screenshot FAILED ({e}) — the "
                        "session likely has no usable desktop.")

    def _cloudflare_widget_rect(self):
        """Viewport rect of the live challenge widget (iframe or its ~300x65
        wrapper) plus inner dims, or None when it hasn't rendered yet."""
        try:
            return self.driver.execute_script("""
                function find() {
                    var el = document.querySelector(
                        'iframe[src*="challenges.cloudflare.com"]');
                    if (el) return el;
                    var divs = document.getElementsByTagName('div');
                    for (var i = 0; i < divs.length; i++) {
                        var r = divs[i].getBoundingClientRect();
                        if (r.width > 250 && r.width < 340 &&
                            r.height > 50 && r.height < 90) return divs[i];
                    }
                    return null;
                }
                var el = find();
                if (!el) return null;
                var r = el.getBoundingClientRect();
                if (!r.width || !r.height) return null;
                return {left: r.left, top: r.top, w: r.width, h: r.height,
                        innerW: window.innerWidth, innerH: window.innerHeight};
            """)
        except Exception:
            return None

    def _cloudflare_point(self):
        """Screen coords of the Cloudflare checkbox.

        DOM-anchored first: the widget's actual on-page position, so window
        size and layout can't invalidate the target (a fixed calibrated point
        only holds at the window width it was calibrated in — the 2026-07-12
        detached failure). The checkbox sits ~28px from the widget's left
        edge, vertically centered. Falls back to the calibrated point while
        the widget hasn't rendered (spinner phase) or if the DOM probe fails."""
        rect = self._cloudflare_widget_rect()
        if rect:
            try:
                rel_x = (rect["left"] + 28) / rect["innerW"]
                rel_y = (rect["top"] + rect["h"] / 2) / rect["innerH"]
                x, y, _ = self._get_scaled_coordinates(rel_x, rel_y)
                return x, y
            except Exception:
                pass
        try:
            cfg = self.config.get("cloudflare_checkbox", {})
            rx = cfg.get("relative_x", 0.3328125)
            ry = cfg.get("relative_y", 0.2577266922094508)
            x, y, _ = self._get_scaled_coordinates(rx, ry)
            return x, y
        except Exception:
            return None

    def _click_cloudflare(self, attempt: int = 0):
        """Click the calibrated Cloudflare checkbox.

        Each attempt clicks at the calibrated point plus a slight random
        variation (so the cursor lands a little differently every time rather
        than always hitting the exact same pixel). After CLOUDFLARE_SEARCH_AFTER
        failed attempts an expanding nudge is also added to catch a checkbox
        that sits just beside the cursor when calibration is slightly off."""
        try:
            point = self._cloudflare_point()
            if point is None:
                return
            x, y = point

            if attempt >= CLOUDFLARE_SEARCH_AFTER:
                idx = (attempt - CLOUDFLARE_SEARCH_AFTER) % len(CLOUDFLARE_SEARCH_OFFSETS)
                ox, oy = CLOUDFLARE_SEARCH_OFFSETS[idx]
                x += ox
                y += oy

            # Slight random variation applied to the click target itself.
            x += random.randint(-CLOUDFLARE_CLICK_JITTER, CLOUDFLARE_CLICK_JITTER)
            y += random.randint(-CLOUDFLARE_CLICK_JITTER, CLOUDFLARE_CLICK_JITTER)

            if self.human_mouse.available:
                self.human_mouse.click(x, y)
            else:
                import pyautogui
                pyautogui.click(x, y)
        except Exception as e:
            log.debug(f"Cloudflare click failed: {e}")

    # -------------------------------------------------------------------------
    # LOGIN
    # -------------------------------------------------------------------------
    def _is_logged_in(self) -> bool:
        try:
            profile_trigger = self.driver.find_elements(
                By.XPATH,
                "//div[contains(@data-ng-if, 'isCurrentUser')]//a[contains(@href, 'javascript')]",
            )
            if any(p.is_displayed() for p in profile_trigger):
                return True
            if self.driver.find_elements(By.CSS_SELECTOR, "a[ui-sref='reservations']"):
                return True
        except Exception:
            pass
        return False

    def _wait_for_booking_page(self) -> bool:
        try:
            WebDriverWait(self.driver, DEFAULT_WAIT).until(
                EC.presence_of_element_located((By.ID, "players-button"))
            )
            return True
        except TimeoutException:
            log.error("Booking page did not finish loading.")
            return False

    def _looks_blank(self) -> bool:
        """True when the page is the known stranded blank/white screen after a
        login submit: almost no body text, no login form, not a Cloudflare
        interstitial. A refresh reliably re-renders it (matches the manual fix)."""
        try:
            if self._is_cloudflare_blocking():
                return False
            txt = self.driver.execute_script(
                "return (document.body && document.body.innerText || '').trim();"
            ) or ""
            if len(txt) < 40 and not self.driver.find_elements(By.NAME, "login"):
                return True
        except Exception:
            pass
        return False

    def _await_login_settled(self, timeout: int = 20) -> bool:
        """After submitting credentials, wait for the logged-in state. The site
        intermittently strands a blank page even though login succeeded; a
        refresh fixes it, so detect that state quickly and reload rather than
        burning the whole timeout waiting for a page that will never render."""
        deadline = time.time() + timeout
        refreshes = 0
        warned = False
        while time.time() < deadline:
            if self._is_logged_in():
                return self._wait_for_booking_page()
            if self._looks_blank() and refreshes < 4:
                if not warned:
                    log.warning("Post-login page is blank; reloading to "
                                "recover (known render glitch).")
                    warned = True
                # Alternate refresh() (matches the manual fix) with a clean GET
                # of the app URL. The GET can't trigger a form-resubmission
                # prompt and reloads the SPA with the (already-set) session
                # cookie; refresh() re-renders the stranded route in place.
                try:
                    if refreshes % 2 == 0:
                        self.driver.refresh()
                    else:
                        self.driver.get(self.config.get("booking_url"))
                except Exception as e:
                    log.debug(f"Reload failed ({e}).")
                refreshes += 1
                self.handle_cloudflare()
                time.sleep(1.0)
                continue
            time.sleep(0.4)
        log.error("Login failed: profile or booking page did not appear after submit.")
        return False

    def _submit_login_form(self) -> bool:
        """Fill and submit the login form. Returns True on apparent success."""
        wait = WebDriverWait(self.driver, DEFAULT_WAIT)
        try:
            user_field = wait.until(EC.element_to_be_clickable((By.NAME, "login")))
            self.driver.execute_script("arguments[0].click();", user_field)
            user_field.clear()
            user_field.send_keys(self.config["username"])

            pass_field = self.driver.find_element(By.NAME, "password")
            self.driver.execute_script("arguments[0].click();", pass_field)
            pass_field.clear()
            pass_field.send_keys(self.config["password"])

            submit_btn = self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            self.driver.execute_script("arguments[0].click();", submit_btn)

            return self._await_login_settled()
        except TimeoutException:
            log.error("Login failed: could not fill/submit the login form.")
            return False
        except Exception as e:
            log.error(f"Login process error: {e}")
            return False

    def login(self) -> bool:
        log.info("Starting login phase...")
        self.driver.get(self.config.get("booking_url"))

        if not self.handle_cloudflare():
            return False

        wait = WebDriverWait(self.driver, DEFAULT_WAIT)
        start_time = time.time()

        stuck_refreshes = 0
        while time.time() - start_time < LOGIN_TIMEOUT:
            if self._is_logged_in():
                log.info("Already logged in.")
                return self._wait_for_booking_page()

            try:
                trigger = wait.until(EC.presence_of_element_located((
                    By.XPATH,
                    "//a[contains(@href, 'login')] | "
                    "//button[contains(translate(., 'S', 's'), 'sign in')] | "
                    "//input[@name='login']",
                )))
                if trigger.tag_name.lower() in ("a", "button"):
                    self.driver.execute_script("arguments[0].click();", trigger)
            except TimeoutException:
                log.warning("Could not find 'Sign In' trigger. Trying form directly.")

            if self._submit_login_form():
                log.info("Login successful.")
                return True

            # Stuck after Cloudflare with no fillable login form. This is the
            # intermittent post-Cloudflare blank/unrendered index page. There
            # is no reliable DOM signal for it, so rather than detect it we
            # just optimistically hard-refresh (F5) and retry: refresh()
            # re-renders the SPA in place (what the manual fix does), where a
            # fresh GET can reload into the same blank state. Bounded by
            # LOGIN_TIMEOUT; refresh() before the login POST carries no form
            # data, so it never triggers a resubmission prompt.
            stuck_refreshes += 1
            log.warning(f"Login form not ready (post-Cloudflare blank?); "
                        f"refreshing and retrying (attempt {stuck_refreshes}).")
            try:
                self.driver.refresh()
                self.handle_cloudflare()
            except Exception as e:
                log.debug(f"Login refresh retry failed ({e}).")
            time.sleep(1.5)

        log.error(f"Login phase timed out after {LOGIN_TIMEOUT} seconds.")
        return False

    # -------------------------------------------------------------------------
    # SIDEBAR FILTER HELPER
    # -------------------------------------------------------------------------
    @staticmethod
    def _xpath_str(s: str) -> str:
        """Quote a string safely for XPath (handles embedded quotes)."""
        if "'" not in s:
            return f"'{s}'"
        if '"' not in s:
            return f'"{s}"'
        parts = s.split("'")
        return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"

    def _read_filter_state(self, li) -> dict:
        """Snapshot {label_text: is_checked} for a filter <li>."""
        current = {}
        for inp in li.find_elements(By.CSS_SELECTOR, "input[type='checkbox']"):
            try:
                # Label is a sibling within the same row, not linked by 'for'.
                label = inp.find_element(
                    By.XPATH, "./ancestor::div[1]/following-sibling::div//label"
                )
                text = label.text.strip()
            except NoSuchElementException:
                continue
            if text:
                current[text] = inp.is_selected()
        return current

    def _expand_filter(self, li):
        """Expand the filter checklist if collapsed.

        Some EZBook themes wrap rows in .checklist; others don't. We only
        bother expanding if there's a collapsible wrapper.
        """
        try:
            wrapper = li.find_element(By.CSS_SELECTOR, ".checklist")
        except NoSuchElementException:
            return  # No wrapper — filter rows are always rendered.
        if wrapper.is_displayed():
            return
        try:
            header = li.find_element(By.CSS_SELECTOR, ".filter-header button")
            self.driver.execute_script("arguments[0].click();", header)
            WebDriverWait(self.driver, 5).until(lambda d: wrapper.is_displayed())
        except NoSuchElementException:
            pass

    def _find_checkbox_for_label(self, li, label_text: str):
        """Locate the checkbox input paired with a given label text.

        Walks: label -> its row container -> sibling div -> input. Required
        because IDs on this page aren't unique (Angular template reuses
        id='priceOption' across rows).
        """
        label = li.find_element(
            By.XPATH,
            f".//label[normalize-space(text())={self._xpath_str(label_text)}]",
        )
        return label.find_element(
            By.XPATH, "./ancestor::div[1]/preceding-sibling::div//input[@type='checkbox']"
        )

    def _toggle_filter_label(self, li, label_text: str) -> bool:
        """Click the checkbox associated with a given label_text."""
        try:
            inp = self._find_checkbox_for_label(li, label_text)
            self.driver.execute_script("arguments[0].click();", inp)
            log.info(f"  Toggled: {label_text}")
            time.sleep(1)  # let Angular's $apply settle
            return True
        except StaleElementReferenceException:
            raise  # Sidebar re-rendered; let _set_filter_options retry.
        except Exception as e:
            log.error(f"Failed to toggle '{label_text}': {type(e).__name__}: {e}")
            return False

    def _verify_filter_state(self, li, current_keys, desired_set) -> bool:
        """Confirm every checkbox matches its desired state."""
        for label_text in current_keys:
            try:
                inp = self._find_checkbox_for_label(li, label_text)
                actual = inp.is_selected()
                expected = label_text in desired_set
                if actual != expected:
                    log.error(
                        f"Filter verification failed: '{label_text}' "
                        f"expected={expected}, actual={actual}"
                    )
                    return False
            except StaleElementReferenceException:
                pass  # Re-render lost the reference; non-fatal.
            except NoSuchElementException:
                pass  # Label disappeared from DOM; non-fatal.
        return True

    def _log_filter_diagnostics(self):
        """Explain why the price-options filter <li> never appeared.

        Distinguishes 'Angular removed it via ng-if' (template comment still
        in source, scope flag false — i.e. no price options for this search)
        from 'the page markup changed' (template string gone entirely).
        """
        try:
            diag = self.driver.execute_script("""
                var out = {};
                out.templateInSource =
                    document.documentElement.outerHTML.indexOf('showPriceOptionsFilter') !== -1;
                out.sidebarNgIfs = Array.prototype.slice.call(
                    document.querySelectorAll('li')
                ).reduce(function (acc, li) {
                    Array.prototype.slice.call(li.attributes).forEach(function (a) {
                        if (a.name.indexOf('ng-if') !== -1) {
                            acc.push(a.name + "='" + a.value + "'");
                        }
                    });
                    return acc;
                }, []);
                try {
                    var anchor = document.querySelector('div[range-slider]');
                    var scope = angular.element(anchor).scope();
                    var ec = scope.ec || scope.$parent.ec;
                    out.showPriceOptionsFilter = ec.showPriceOptionsFilter;
                    var results = ec.teeTimes || ec.teeTimeResults || ec.searchResults;
                    out.resultCount = results ? results.length : 'unknown';
                } catch (e) {
                    out.scopeError = String(e);
                }
                return out;
            """)
            log.error(f"Filter diagnostics: {diag}")

            # Log the markup surrounding each 'showPriceOptionsFilter'
            # occurrence so we can see what element actually carries it now.
            snippets = self.driver.execute_script("""
                var html = document.documentElement.outerHTML;
                var out = [];
                var idx = html.indexOf('showPriceOptionsFilter');
                while (idx !== -1 && out.length < 5) {
                    out.push(html.substring(Math.max(0, idx - 300), idx + 300));
                    idx = html.indexOf('showPriceOptionsFilter', idx + 1);
                }
                return out;
            """)
            for i, snip in enumerate(snippets):
                log.error(f"Source context {i}: ...{snip}...")

            # Full page dump for offline inspection.
            dump = Path("filter_page_dump.html")
            dump.write_text(self.driver.page_source, encoding="utf-8")
            log.error(f"Full page HTML dumped to {dump.resolve()}")
        except Exception as e:
            log.error(f"Filter diagnostics failed: {type(e).__name__}: {e}")

    def _wait_for_results_idle(self, timeout: int = DEFAULT_WAIT):
        """Block until the search-results controller finishes loading.

        Date/time-filter changes trigger a results reload that re-renders
        the whole filter sidebar (ng-if/ng-repeat), invalidating any element
        references grabbed mid-rebuild. ec.isLoading is the page's own
        signal (checkboxes are ng-disabled on it).
        """
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script("""
                    try {
                        var anchor = document.querySelector('div[range-slider]');
                        var scope = angular.element(anchor).scope();
                        var ec = scope.ec || scope.$parent.ec;
                        return !ec.isLoading;
                    } catch (e) {
                        return true;  // Can't tell — don't block forever.
                    }
                """)
            )
        except TimeoutException:
            log.warning("Search results still loading; proceeding anyway.")

    def _set_filter_options(self, filter_li_selector: str, desired_labels: list) -> bool:
        """Set a sidebar filter so that exactly `desired_labels` are checked.

        Diffs current vs desired and only clicks what needs to change. Uses JS
        clicks to bypass overlay-intercept issues from the busy spinner.
        Retries if the sidebar re-renders mid-operation (stale references).
        """
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                wait = WebDriverWait(self.driver, DEFAULT_WAIT)
                self._wait_for_results_idle()
                try:
                    li = wait.until(EC.presence_of_element_located(
                        (By.CSS_SELECTOR, filter_li_selector)
                    ))
                except TimeoutException:
                    log.error(f"Filter '{filter_li_selector}' never appeared in the DOM.")
                    self._log_filter_diagnostics()
                    return False

                self._expand_filter(li)
                # Rows inside the container are template-rendered after the
                # container itself exists; wait for the checkboxes explicitly.
                try:
                    wait.until(lambda d: li.find_elements(
                        By.CSS_SELECTOR, "input[type='checkbox']"
                    ))
                except TimeoutException:
                    pass  # Fall through; _read_filter_state reports empty below.
                current = self._read_filter_state(li)

                if not current:
                    log.error(f"No checkboxes found in filter '{filter_li_selector}'.")
                    return False

                desired_set = set(desired_labels)
                available = set(current.keys())
                missing = desired_set - available
                if missing:
                    log.error(
                        f"Filter labels not on page: {sorted(missing)}. "
                        f"Available: {sorted(available)}"
                    )
                    return False

                to_toggle = [
                    label for label, is_checked in current.items()
                    if (label in desired_set) != is_checked
                ]

                if not to_toggle:
                    log.info(f"Filter already at desired state: {sorted(desired_set)}")
                    return True

                for label_text in to_toggle:
                    if not self._toggle_filter_label(li, label_text):
                        return False

                time.sleep(0.5)
                if not self._verify_filter_state(li, current.keys(), desired_set):
                    return False

                log.info(f"Filter set to: {sorted(desired_set)}")
                return True

            except StaleElementReferenceException:
                log.warning(
                    f"Filter sidebar re-rendered mid-operation "
                    f"(attempt {attempt}/{attempts}); re-locating..."
                )
                time.sleep(1)
            except Exception as e:
                log.error(f"Filter set failed: {type(e).__name__}: {e}")
                return False

        log.error("Filter sidebar kept re-rendering; giving up.")
        return False

    def set_course_filter(self) -> bool:
        """Configure the Price Options sidebar filter.

        EDIT HERE if the price options for this site need to change.
        Current targets: 'Full'.
        """
        log.info("Setting Price Options filter (Full)...")
        # Tag-agnostic: the site changed this container from <li> to <div>
        # in a 2026 update; match on the ng-if attribute only.
        return self._set_filter_options(
            filter_li_selector="[data-ng-if*='showPriceOptionsFilter']",
            desired_labels=["Full"],
        )

    # -------------------------------------------------------------------------
    # PLAYER COUNT & DATE
    # -------------------------------------------------------------------------
    def set_player_count(self, count: int) -> bool:
        try:
            log.info(f"Setting player count to {count}")
            wait = WebDriverWait(self.driver, DEFAULT_WAIT)
            btn = wait.until(EC.element_to_be_clickable((By.ID, "players-button")))

            current = btn.find_element(By.CSS_SELECTOR, ".dropdown-selected-item span").text.strip()
            if current == str(count):
                return True

            btn.click()
            opt = wait.until(EC.element_to_be_clickable((
                By.XPATH,
                f"//ul[contains(@class, 'dropdown-menu')]//a[contains(text(), '{count}')]",
            )))
            opt.click()
            time.sleep(0.5)
            return True
        except Exception as e:
            log.error(f"Failed to set players: {e}")
            return False

    def select_date(self, target_date: datetime,
                    wait_secs: int = DEFAULT_WAIT) -> bool:
        try:
            d_str = target_date.strftime("%m/%d/%Y")
            # wait_secs: the strike loop passes a short wait so a lost page
            # (picker gone) is detected in seconds, not DEFAULT_WAIT.
            WebDriverWait(self.driver, wait_secs).until(
                EC.presence_of_element_located((By.ID, "pickerDate"))
            )

            result = self.driver.execute_script("""
                var el = document.getElementById('pickerDate');
                var dateStr = arguments[0];

                // 1. If jQuery UI datepicker is attached, use its API.
                //    This syncs the picker's internal state.
                if (window.jQuery && window.jQuery(el).datepicker) {
                    try {
                        window.jQuery(el).datepicker('setDate', dateStr);
                    } catch (e) { /* fall through to manual set */ }
                }

                // 2. Set the value directly as a backstop.
                el.value = dateStr;

                // 3. Update Angular's model. This page uses
                //    ng-model-options="{ updateOn: 'blur' }", so 'input'
                //    and 'change' events are ignored — only blur counts.
                if (window.angular) {
                    var $el = window.angular.element(el);
                    $el.triggerHandler('input');   // pristine -> dirty
                    $el.triggerHandler('change');  // belt and suspenders
                    $el.triggerHandler('blur');    // <-- the one that matters
                    var scope = $el.scope() || $el.isolateScope();
                    if (scope && scope.$apply) {
                        scope.$apply();
                    }
                }

                return el.value;
            """, d_str)

            if result != d_str:
                # The picker rejected/clamped the value (e.g. the date is not
                # inside the site's bookable horizon yet). Booking would land
                # on whatever date the grid actually shows — hard-fail instead.
                log.error(
                    f"Date NOT accepted: wanted {d_str}, input reads {result!r}. "
                    "Refusing to proceed on the wrong date."
                )
                return False
            log.info(f"Date set to {d_str} (input now reads: {result})")
            return True
        except Exception as e:
            log.error(f"Failed to set date: {e}")
            return False


    def set_time_filter(self, start_time: str = None, end_time: str = None) -> bool:
        try:
            def to_mins(t): return (int(t.split(':')[0]) * 60 + int(t.split(':')[1])) if t else None
            s_m, e_m = to_mins(start_time), to_mins(end_time)

            slider = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[range-slider]")))
            
            self.driver.execute_script("""
                var scope = angular.element(arguments[0]).scope();
                var ec = scope.ec || scope.$parent.ec;
                if (arguments[1] !== null) ec.timeRange.userMinOne = arguments[1];
                if (arguments[2] !== null) ec.timeRange.userMaxOne = arguments[2];
                ec.onTeeTimeFilterHandleUp();
                scope.$apply();
            """, slider, s_m, e_m)
            
            log.info(f"Time filter: {start_time}-{end_time}")
            return True
        except Exception as e:
            log.error(f"Failed to set time filter: {e}")
            return False

    # -------------------------------------------------------------------------
    # TEE TIME SELECTION
    # -------------------------------------------------------------------------
    def _scrape_tee_times(self) -> list:
        """Scrape available tee time buttons from the grid."""
        slots = []
        no_times_xpath = (
            "//div[contains(., 'No tee times') or contains(., 'No results')]"
        )
        btn_xpath = (
            "//button[contains(translate(., 'VB', 'vb'), 'view') "
            "or contains(translate(., 'VB', 'vb'), 'book')]"
        )
        try:
            # Wait for the grid to settle: either slot buttons or the
            # "no times" message must appear before we trust the scrape.
            try:
                WebDriverWait(self.driver, DEFAULT_WAIT).until(
                    lambda d: d.find_elements(By.XPATH, btn_xpath)
                    or d.find_elements(By.XPATH, no_times_xpath)
                )
            except TimeoutException:
                log.warning("Results grid never settled; scraping anyway.")

            no_times = self.driver.find_elements(By.XPATH, no_times_xpath)
            if no_times and no_times[0].is_displayed():
                return []

            # Distinguish "sold out / not open" from "the markup changed":
            # if there is no grid container, no slot button, AND no no-times
            # message while the search page itself is loaded, retrying can
            # never help — alarm immediately instead of reporting "no times".
            structure = self.driver.execute_script("""
                return {
                    grid: !!document.querySelector('ul.tee-time-block'),
                    picker: !!document.getElementById('pickerDate'),
                };
            """)
            if structure["picker"] and not structure["grid"]:
                self._alert_page_changed(
                    "results grid (ul.tee-time-block) missing from search page"
                )
                raise PageChangedError("results grid container not found")

            # Pull every row's data in ONE round-trip. Each tee time is a
            # <li> in ul.tee-time-block holding a time span, a players span,
            # and the pod button. Reading them per-element over Selenium was
            # slow on a large grid; this collects the whole list in one call
            # and returns the (visible) button handles for clicking.
            rows = self.driver.execute_script("""
                return Array.from(
                    document.querySelectorAll('ul.tee-time-block > li')
                ).map(function (li) {
                    var btn = li.querySelector("button[ng-click*='onPodButtonClick']")
                              || li.querySelector('button');
                    var t = li.querySelector('span.time');
                    var p = li.querySelector('span.players');
                    return {
                        label: btn ? (btn.getAttribute('aria-label') || '') : '',
                        time: t ? t.textContent.trim() : '',
                        players: p ? p.textContent.trim() : '',
                        el: btn
                    };
                }).filter(function (r) { return r.el && r.el.offsetParent !== null; });
            """) or []

            for row in rows:
                # Time from the button's aria-label (carries AM/PM), e.g.
                # "View ... 5:40 PM tee time"; fall back to the time span.
                src = row.get("label") or row.get("time") or ""
                m = re.search(r'(\d{1,2}:\d{2})\s*([AP]M)', src, re.IGNORECASE)
                if not m:
                    continue
                t_str = f"{m.group(1)} {m.group(2).upper()}"
                try:
                    slot_time = datetime.strptime(t_str, "%I:%M %p")
                except ValueError:
                    continue

                # Max player capacity from the row's players text ("1–4 Players").
                nums = [int(n) for n in re.findall(r'\d+', row.get("players") or "")]
                p_count = max(nums) if nums else 4

                slots.append({
                    "time": slot_time,
                    "time_str": t_str,
                    "players": p_count,
                    "element": row.get("el"),
                })

        except PageChangedError:
            raise
        except Exception as e:
            log.warning(f"Scrape error: {e}")
        return slots

    def _alert_page_changed(self, what: str) -> None:
        """Loud one-shot alarm for a structural page change + page dump."""
        log.error(f"PAGE-STRUCTURE-CHANGED: {what}")
        try:
            dump = BASE_DIR / "structure_change_dump.html"
            dump.write_text(self.driver.page_source, encoding="utf-8")
            log.error(f"Page dumped to {dump}")
        except Exception:
            pass
        if not self._alerted_page_changed:
            self._alerted_page_changed = True
            self.send_alert(
                "TeeTimeBooker ALARM: page structure changed",
                f"The site's page structure no longer matches the automation: "
                f"{what}. Retries cannot fix this — manual booking may be "
                f"needed. Page HTML dumped next to the log.",
            )

    def _rank_slot(self, slot, target: datetime, tie_pref: str) -> float:
        """Distance score for a slot — lower is better."""
        diff = abs((slot["time"] - target).total_seconds())
        if tie_pref == "earlier" and slot["time"] > target:
            diff += 0.1
        elif tie_pref == "later" and slot["time"] < target:
            diff += 0.1
        return diff

    def _evaluate_tee_times(self, widen_tolerance: bool = False) -> tuple:
        """Read-only evaluator: scrape the grid and rank the in-window,
        capacity-matching slots best-first (closest to desired_time, tiebreak
        preference applied). No clicking, no booking. Returns
        (candidates, slots) where candidates is the sorted (score, slot) list
        and slots is the raw scrape. Shared by select_best_tee_time (which
        books the winner) and the waiting-mode tick (which must detect and
        rank openings without booking them)."""
        target = datetime.strptime(self.config.get("desired_time", "09:00"), "%H:%M")
        req_players = int(self.config.get("num_players", 1))
        tie_pref = self.config.get("preference_if_equal", "earlier")

        # Hard bound on the acceptable window: the site-side slider
        # filter can silently fail to apply, so never trust the grid to
        # only contain in-window times.
        def parse_bound(key):
            v = self.config.get(key)
            return datetime.strptime(v, "%H:%M") if v else None
        earliest = parse_bound("earliest_acceptable_time")
        latest = parse_bound("latest_acceptable_time")
        if widen_tolerance:
            if earliest is not None:
                earliest -= timedelta(minutes=int(
                    self.config.get("earlier_tolerance_minutes", 30)))
            if latest is not None:
                latest += timedelta(minutes=int(
                    self.config.get("later_tolerance_minutes", 30)))
            log.info("Time window WIDENED by the configured tolerances.")

        slots = self._scrape_tee_times()
        candidates = [
            (self._rank_slot(s, target, tie_pref), s)
            for s in slots
            if s["players"] >= req_players
            and (earliest is None or s["time"] >= earliest)
            and (latest is None or s["time"] <= latest)
        ]

        if not candidates:
            if slots:
                log.info(f"{len(slots)} slot(s) shown, none inside the "
                         "acceptable window with enough capacity.")
            return [], slots

        candidates.sort(key=lambda x: x[0])
        return candidates, slots

    def select_best_tee_time(self, widen_tolerance: bool = False) -> bool:
        """Pick the slot closest to desired_time, with tiebreak preference.

        Iterates the ranked candidate list: if the best slot's popup fails
        (sniped by another user, unexpected dialog), dismiss and try the next
        candidate in the SAME session instead of failing the whole attempt.
        widen_tolerance expands the window by the configured tolerances —
        the strike loop sets it once the strict window has come up empty
        past the release moment."""
        # Tells the strike loop whether a False was "nothing to book" (keep
        # polling) or "had candidates but couldn't book one" (page suspect).
        self._sel_had_candidates = False
        try:
            req_players = int(self.config.get("num_players", 1))

            candidates, slots = self._evaluate_tee_times(widen_tolerance)

            if not candidates:
                log.info("No matching times found.")
                return False

            self._sel_had_candidates = True
            ranked_times = [c[1]["time_str"] for c in candidates]

            for i, time_str in enumerate(ranked_times, start=1):
                # Re-scrape each try: the previous popup/dismiss may have
                # re-rendered the grid, invalidating held element handles.
                if i > 1:
                    self._dismiss_dialogs()
                    self._wait_for_results_idle(timeout=5)
                fresh = self._scrape_tee_times() if i > 1 else slots
                slot = next((s for s in fresh if s["time_str"] == time_str), None)
                if slot is None:
                    log.info(f"Candidate {time_str} gone from grid; next.")
                    continue

                log.info(f"Selected: {time_str} (candidate {i}/{len(ranked_times)})")
                self.booking_details["time"] = time_str
                try:
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});"
                        "arguments[0].click();",  # JS click: overlays can't intercept it
                        slot["element"],
                    )
                except StaleElementReferenceException:
                    log.warning(f"Candidate {time_str} went stale; next.")
                    continue

                if self._handle_popup_confirmation(req_players):
                    return True
                log.warning(f"Candidate {time_str} failed at the popup; "
                            "trying the next slot in-session.")

            return False

        except (AlreadyBookedError, PageChangedError):
            raise
        except Exception as e:
            log.error(f"Selection failed: {e}")
            return False

    def _dismiss_dialogs(self) -> None:
        """Close any open modal so the grid is clickable again.

        This site's pricing modal closes via a .close-reveal-modal button bound
        to $dismiss() (NOT the generic .close / data-dismiss). Click that first
        so Angular tears the modal down cleanly, then hard-remove any leftover
        uib-modal-window + backdrop so a lingering hidden modal can't poison the
        next candidate's selectors or intercept grid clicks."""
        try:
            self.driver.execute_script("""
                document.querySelectorAll(
                    '.close-reveal-modal, [data-ng-click="$dismiss()"],' +
                    ' div.modal button.close, div.modal [data-dismiss="modal"],' +
                    ' div.modal button[data-ng-click*="cancel"]'
                ).forEach(function (b) { try { b.click(); } catch (e) {} });
            """)
            time.sleep(0.2)
            # Hard teardown of anything the click left behind.
            self.driver.execute_script("""
                document.querySelectorAll(
                    'div[uib-modal-window], .modal-backdrop, .reveal-modal-bg'
                ).forEach(function (e) {
                    if (e.parentNode) { e.parentNode.removeChild(e); }
                });
                document.body.classList.remove('modal-open');
            """)
        except Exception as e:
            log.debug(f"Dialog dismissal incomplete: {e}")

    def _click_when_clickable(self, locator, attempts: int = 2) -> None:
        """Wait for an element to be clickable and click it, retrying on a
        stale reference (the modal re-renders between steps). Silently gives up
        after `attempts`, matching the prior best-effort behavior."""
        wait = WebDriverWait(self.driver, DEFAULT_WAIT)
        for _ in range(attempts):
            try:
                el = wait.until(EC.presence_of_element_located(locator))
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", el)
                try:
                    el.click()
                except ElementClickInterceptedException:
                    # Payment page overlays (cg-busy spinner, injected browser-
                    # extension widgets) sit over buttons; a JS click fires the
                    # handler regardless of what's painted on top.
                    self.driver.execute_script("arguments[0].click();", el)
                return
            except StaleElementReferenceException:
                continue

    def _active_popup_text(self) -> str:
        """Text of the on-screen modal/dialog, via JS so it works even though
        this site's modals render as `.modal.fade` (opacity 0) that Selenium's
        is_displayed() reports as hidden. Reads innerText of any modal whose
        computed display isn't 'none'."""
        try:
            return self.driver.execute_script("""
                var nodes = document.querySelectorAll(
                    'div[uib-modal-window], div[role="dialog"],' +
                    ' div.modal, div.reveal-modal, div.alert');
                var out = '';
                Array.prototype.forEach.call(nodes, function (n) {
                    if (getComputedStyle(n).display !== 'none') {
                        var t = (n.innerText || '').trim();
                        if (t.length > out.length) { out = t; }
                    }
                });
                return out.replace(/\\s+/g, ' ');
            """) or ""
        except Exception:
            return ""

    def _topmost_modal(self):
        """Return the active modal window element, or None.

        Chosen in JS by inline display + z-index, NOT Selenium is_displayed():
        this site's modal renders as `.modal.fade` (opacity 0, no `.in` class
        added under automation), so is_displayed() reports False even though the
        modal is functionally present and JS-interactable. Picking the
        highest-z-index window whose display isn't 'none' sidesteps that, and
        every popup action here is JS-driven so opacity is irrelevant."""
        return self.driver.execute_script("""
            var wins = Array.prototype.slice.call(
                document.querySelectorAll('div[uib-modal-window]'));
            var best = null, bestZ = -1;
            wins.forEach(function (w) {
                if (getComputedStyle(w).display === 'none') return;
                var z = parseInt(getComputedStyle(w).zIndex, 10) || 0;
                if (z >= bestZ) { bestZ = z; best = w; }
            });
            return best;
        """)

    def _handle_popup_confirmation(self, players: int) -> bool:
        try:
            # Wait for the pricing modal to actually render (its package rows
            # are the signal it's ready), scoped to the topmost visible modal.
            modal = None
            deadline = time.time() + 8
            while time.time() < deadline:
                m = self._topmost_modal()
                if m and m.find_elements(By.CSS_SELECTOR, "#addToCartBtn"):
                    modal = m
                    break
                time.sleep(0.2)
            if modal is None:
                log.error("Slot pricing modal never rendered. "
                          f"Popup text: {self._active_popup_text()!r}")
                self._dump_stage("popup_failed")
                return False

            # The single "Full" package auto-expands and is pre-selected; a JS
            # click on its price-listing (if present) is a harmless confirm.
            # Player count is a <select> bound to package.SelectedPlayerCount;
            # set it and fire change so Angular's model updates. Then click the
            # in-modal Continue (#addToCartBtn) — resolved WITHIN this modal so
            # a stacked leftover's duplicate id can't be hit.
            # Only touch controls that AREN'T already in the desired state, so
            # the request the site sees matches a human's. Redundant synthetic
            # clicks (re-selecting the already-active package, re-firing an
            # unchanged select) are a suspected trigger of a server 500 seen in
            # testing.
            self.driver.execute_script("""
                var modal = arguments[0], players = arguments[1];
                var price = modal.querySelector('.price-listing');
                if (price && price.className.indexOf('active') === -1) {
                    price.click();
                }
                var sel = modal.querySelector('select[data-ng-model$="SelectedPlayerCount"]')
                          || modal.querySelector('.pricing-players select');
                if (sel) {
                    var want = -1;
                    for (var i = 0; i < sel.options.length; i++) {
                        if (parseInt(sel.options[i].textContent, 10) === players) {
                            want = i; break;
                        }
                    }
                    if (want !== -1 && sel.selectedIndex !== want) {
                        sel.selectedIndex = want;
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                }
            """, modal, int(players))
            time.sleep(0.3)
            self._dump_stage("slot_popup")

            # Continue button, scoped to this modal.
            try:
                cont = WebDriverWait(self.driver, DEFAULT_WAIT).until(
                    lambda d: (lambda mm: mm.find_element(
                        By.CSS_SELECTOR, "#addToCartBtn") if mm else None
                    )(self._topmost_modal())
                )
                self.driver.execute_script("arguments[0].click();", cont)
            except TimeoutException:
                log.error("Continue button never became available. "
                          f"Popup text: {self._active_popup_text()!r}")
                self._dump_stage("popup_failed")
                return False

            # After Continue, the site pops an error dialog if a reservation
            # already exists for this day (its text mentions an "existing
            # reservation"). Catch it here — otherwise the OK click below
            # dismisses the error and the flow proceeds into a misleading
            # downstream failure. Poll briefly: the dialog renders just after
            # the Continue click.
            # After Continue the site may pop a confirm dialog (Yes/OK) and,
            # if a reservation already exists for the day, an error dialog.
            # All JS-driven and opacity-blind (see _active_popup_text / modal
            # rendering notes). Clicking Yes/OK is done in JS by its ng-click
            # binding so a 0-opacity dialog is still actionable.
            deadline = time.time() + 4
            while time.time() < deadline:
                popup = self._active_popup_text()
                if popup and re.search(
                    r"existing reservation"
                    r"|already.*(reservation|tee\s*time|booked)"
                    r"|one reservation per",
                    popup, re.I,
                ):
                    log.error(
                        "Booking rejected: an existing reservation already "
                        f"covers this day. Site message: {popup!r}"
                    )
                    raise AlreadyBookedError(popup)
                clicked = self.driver.execute_script("""
                    var btns = document.querySelectorAll(
                        '[data-ng-click="ec.onYesClick()"], [ng-click="ec.onYesClick()"]');
                    for (var i = 0; i < btns.length; i++) {
                        var b = btns[i];
                        if (getComputedStyle(b).display !== 'none' && !b.disabled) {
                            b.click(); return true;
                        }
                    }
                    return false;
                """)
                if clicked:
                    break
                time.sleep(0.2)

            # Enter partner names on the payment page (best-effort; it waits
            # for that page to load and never blocks the booking on a name it
            # can't enter — the reservation completes with blank partner slots).
            self._enter_additional_players()
            # Select the configured card HERE, on the payment page, before
            # submitting. (It used to run as a later step, after #buyTeeTime had
            # already submitted on the pre-selected default card.)
            self.select_payment_card()
            self._dump_stage("payment_page")

            # Final Continue on the payment page (proceeds to confirmation).
            self._click_when_clickable((By.ID, "buyTeeTime"))
            return True
        except AlreadyBookedError:
            raise
        except Exception as e:
            log.error(f"Popup interaction failed: {e}")
            return False

    # -------------------------------------------------------------------------
    # PAYMENT
    # -------------------------------------------------------------------------

    def select_payment_card(self) -> bool:
        """Select which saved card to use based on the 'work card' toggle.

        The checkout renders one radio per saved card (name='savedCard'); the
        card's last 4 digits live in a sibling <span data-ng-bind='card.Last4'>.
        We click the radio inside the same <label> as the matching last4 span.

        NON-FATAL: on any miss it logs a warning, dumps the page to
        payment_page_dump.html, and returns True so the booking still proceeds
        on the account's currently-default card.
        """
        pay = self.config.get("payment", {})
        # Fall back to the known card numbers if the config predates the
        # payment section (the GUI only writes the use_work_card toggle).
        if pay.get("use_work_card"):
            last4 = str(pay.get("work_card_last4") or "").strip()
        else:
            last4 = str(pay.get("default_card_last4") or "").strip()

        if not last4:
            log.info("No payment card configured; using account default card.")
            return True

        log.info(f"Selecting saved card ending {last4}...")
        xpath = (
            f"//label[.//span[@data-ng-bind='card.Last4']"
            f"[normalize-space()='{last4}']]//input[@name='savedCard']"
        )
        try:
            self.driver.switch_to.default_content()
            wait = WebDriverWait(self.driver, DEFAULT_WAIT)
            wait.until(EC.presence_of_element_located((By.XPATH, xpath)))

            # The saved-card list is an Angular ng-repeat that re-renders after
            # it loads, which invalidates any element handle we hold (the stale
            # reference we hit before). Re-find the radio on every attempt and
            # retry on staleness instead of caching the element.
            for attempt in range(5):
                try:
                    radio = self.driver.find_element(By.XPATH, xpath)
                    if radio.is_selected():
                        log.info(f"Card ending {last4} already selected.")
                        return True
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});", radio)
                    try:
                        radio.click()
                    except StaleElementReferenceException:
                        raise
                    except Exception:
                        # Overlapped by the label/card image; JS click still
                        # fires the ng-change handler that sets selectedCard.
                        self.driver.execute_script("arguments[0].click();", radio)
                    # Re-find to confirm the selection actually took.
                    if self.driver.find_element(By.XPATH, xpath).is_selected():
                        log.info(f"Selected card ending {last4}.")
                        return True
                except StaleElementReferenceException:
                    time.sleep(0.3)
                    continue

            log.warning(
                f"Could not confirm selection of card ending {last4} after "
                f"retries; proceeding on the account default card."
            )
            return True
        except TimeoutException:
            try:
                Path("payment_page_dump.html").write_text(
                    self.driver.page_source, encoding="utf-8")
            except Exception:
                pass
            log.warning(
                f"Card ending {last4} not found on the page (dumped to "
                f"payment_page_dump.html). Proceeding on the account default card."
            )
            return True
        except Exception as e:
            log.error(f"Card selection error (non-fatal): {e}")
            return True

    # -------------------------------------------------------------------------
    # CONFIRMATION & EMAIL
    # -------------------------------------------------------------------------
    def _save_reservation_pdf(self) -> str | None:
        """Save the current page as a PDF; return the filename or None."""
        try:
            res = self.driver.execute_cdp_cmd("Page.printToPDF", {"printBackground": True})
            fname = f"Reservation_{self.booking_details.get('date', 'new')}.pdf"
            with open(fname, "wb") as f:
                f.write(base64.b64decode(res["data"]))
            log.info(f"PDF saved: {fname}")
            return fname
        except Exception as e:
            log.warning(f"PDF save failed: {e}")
            return None

    def _visible_slot_input(self, slot_no: int):
        """Return the displayed typeahead input for a reservation slot, or None.

        name='slotN' appears twice (desktop .hidden-xs table and mobile
        .visible-xs table), so filter to the one actually shown."""
        for el in self.driver.find_elements(By.NAME, f"slot{slot_no}"):
            try:
                if el.is_displayed():
                    return el
            except StaleElementReferenceException:
                continue
        return None

    def _await_slot_input(self, slot_no: int, timeout: int = 12):
        """Wait for and return the displayed slot input, tolerating the table
        being mid-reload. The slot inputs live on the payment page, which loads
        a moment after the confirm step, so the wait is generous. Returns None
        if it never appears."""
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: self._visible_slot_input(slot_no) is not None
            )
        except TimeoutException:
            return None
        return self._visible_slot_input(slot_no)

    def _fill_slot(self, slot_no: int, name: str, attempts: int = 3) -> bool:
        """Type one partner name into slot {slot_no}. The table re-renders after
        each name commits, so re-locate the input and retry if it goes stale."""
        for attempt in range(1, attempts + 1):
            try:
                field = self._await_slot_input(slot_no)
                if field is None:
                    log.info(f"Slot {slot_no} input not present.")
                    return False
                if not field.is_enabled():
                    log.warning(f"Slot {slot_no} input is disabled; skipping '{name}'.")
                    return False

                # Focus via JS (not a native click): payment-page overlays
                # intercept clicks on the slot inputs. send_keys only needs the
                # field focused, not clickable-at-a-point.
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});"
                    "arguments[0].focus();", field
                )
                field.clear()
                field.send_keys(name)
                # ng-model has a 500ms debounce; let the typed value register.
                time.sleep(0.6)
                # Close the typeahead dropdown with ESCAPE before blurring so
                # typeahead-select-on-blur can't swap our free-text name for a
                # highlighted member match, then TAB to commit + move on.
                field.send_keys(Keys.ESCAPE)
                field.send_keys(Keys.TAB)
                log.info(f"Entered player for slot {slot_no}: {name}")
                time.sleep(0.4)  # allow the table to re-render after commit
                return True
            except StaleElementReferenceException:
                log.debug(
                    f"Slot {slot_no} went stale (attempt {attempt}/{attempts}); "
                    "re-locating and retrying."
                )
                time.sleep(0.4)
            except Exception as e:
                log.warning(f"Failed to enter player for slot {slot_no} ('{name}'): {e}")
                return False
        log.warning(f"Slot {slot_no} kept going stale after {attempts} attempts.")
        return False

    def _enter_additional_players(self) -> bool:
        """Type playing-partner names into the reservation slot inputs.

        The slot table renders one text input per non-primary slot (slot 1 is
        the logged-in member): name='slot2', 'slot3', ... bound to ng-model
        'slot.SlotName' with a 500ms debounce. Config 'additional_players' maps
        in order to slots 2..N, formatted 'Last, First'. Names are typed as
        free text; the member-lookup dropdown is ignored. The reservation table
        reloads after each name commits, so each slot is located fresh and
        retried on a stale reference.

        BEST-EFFORT: the site lets a reservation complete with partner slots
        left blank, so a name we can't enter must NEVER block the booking.
        Securing the member's tee time beats losing it because a partner name
        wouldn't type. Always returns True; failures are recorded so they can
        be surfaced and added manually, never fatal."""
        # The slot inputs and the final #buyTeeTime both live on the payment
        # page, which loads a few seconds AFTER the modal's Continue/OK. Wait
        # for that page before touching slots — the prior bug was typing before
        # it had rendered ("Slot 2 input not present"), not a bad selector.
        try:
            WebDriverWait(self.driver, DEFAULT_WAIT).until(
                EC.presence_of_element_located((By.ID, "buyTeeTime"))
            )
        except TimeoutException:
            log.warning("Payment page did not load in time; "
                        "attempting player entry anyway.")

        names = self.config.get("additional_players") or []
        missed = []
        for offset, name in enumerate(names):
            slot_no = offset + 2  # slot 1 is the primary member
            if not self._fill_slot(slot_no, name):
                log.warning(
                    f"Could not enter player '{name}' into slot {slot_no}; "
                    "proceeding without it (booking is not blocked)."
                )
                missed.append(f"slot {slot_no}: {name}")
        if missed:
            self.booking_details["missed_players"] = missed
        return True

    def _pause_for_inspection(self, where: str) -> None:
        """Block until the user presses Enter so the browser stays open for
        manual inspection. Falls back to a fixed wait when there is no
        interactive console (e.g. a detached/auto run)."""
        prompt = (
            f"\n[TEST MODE] Paused at: {where}\n"
            "Inspect the browser, then press Enter here to finish "
            "(the browser will then close)... "
        )
        try:
            input(prompt)
        except EOFError:
            log.info("No interactive console; pausing 120s for inspection.")
            time.sleep(120)

    def confirm_booking(self) -> bool:
        try:
            wait = WebDriverWait(self.driver, DEFAULT_WAIT)
            # The real Finish button is id=topFinishBtn / ec.onFinishClick();
            # match that first, with the visible text as a fallback. Use
            # presence (not clickable): a leftover pricing modal can overlay it,
            # which we clear + bypass with a JS click below.
            finish_locator = (
                By.CSS_SELECTOR,
                "#topFinishBtn, [data-ng-click='ec.onFinishClick()']",
            )
            try:
                btn = wait.until(EC.presence_of_element_located(finish_locator))
            except TimeoutException:
                btn = wait.until(EC.presence_of_element_located((
                    By.XPATH, "//button[contains(text(), 'Finish Reservation')]")))

            if self.testing:
                log.warning(
                    "TEST MODE: reached the final stage. Stopping BEFORE "
                    "clicking 'Finish Reservation'. No reservation was made, "
                    "so there is no cancellation-fee risk."
                )
                self._pause_for_inspection(
                    "final stage, before 'Finish Reservation'"
                )
                return True

            self._dump_stage("pre_finish")
            log.info("Finalizing booking...")
            # Clear any leftover modal (e.g. the pricing modal) that lingers
            # over the confirmation page and intercepts the click, then JS-click
            # so nothing painted on top can swallow it.
            self._dismiss_dialogs()
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", btn)
            try:
                btn.click()
            except ElementClickInterceptedException:
                self.driver.execute_script("arguments[0].click();", btn)

            # Confirm from the confirmation page itself. The page is titled
            # "Booking Confirmation" and shows "Reservation Complete" (verified
            # from a real captured run), so positive detection is reliable and
            # we DON'T need the reservations-page ground-truth check — which is
            # intentionally disabled here (it opened a flaky second tab).
            outcome = "unknown"
            deadline = time.time() + 15
            confirm_re = re.compile(
                r"reservation\s*(complete|confirmed)|booking\s*confirmation"
                r"|(reservation|booking).{0,40}(confirm|complete|success)"
                r"|confirmation\s*(number|#)|thank you", re.I)
            error_re = re.compile(
                r"no longer available|sold out|unexpected error|error 500"
                r"|unable to|could not|payment (failed|declined)", re.I)
            while time.time() < deadline:
                try:
                    title = (self.driver.title or "").lower()
                    body_text = self.driver.find_element(By.TAG_NAME, "body").text or ""
                    if "confirmation" in title or confirm_re.search(body_text):
                        outcome = "confirmed"
                        break
                    popup = self._active_popup_text()
                    if popup and error_re.search(popup):
                        log.error(f"Finish rejected by the site: {popup!r}")
                        outcome = "failed"
                        break
                except StaleElementReferenceException:
                    pass  # page transitioning; poll again
                time.sleep(0.4)

            self._dump_stage("post_finish")

            if outcome == "failed":
                return False
            if outcome != "confirmed":
                # Reservations-page ground-truth check is disabled (flaky second
                # tab). Prefer a booking over none: proceed but flag loudly so
                # it gets checked by hand.
                log.warning(
                    "Could not positively confirm the booking from the page "
                    "within the window (no error seen either). Treating as "
                    "booked — CONFIRM MANUALLY.")
                self.booking_details["unconfirmed"] = True

            # Mark that a real reservation was actually completed. send_email
            # only fires when this is set, so test mode (which returns above,
            # before clicking Finish) never triggers a notification.
            self.booking_details["confirmed"] = True
            self._mark_booked()
            # Capture the confirmation number from the confirmation page while
            # we are still on it (the post-booking re-scrape uses it to key the
            # cache record and its notification annotation). Not fatal if it is
            # unreadable: the flow falls back to the reservation-time key.
            num = self._capture_confirmation_number()
            if num:
                self.booking_details["confirmation_number"] = num
                log.info(f"Confirmation number: {num}")
            else:
                log.warning("Could not read the confirmation number from the "
                            "confirmation page; will fall back to the "
                            "reservation-time key.")

            # Capture the confirmation page as a PDF (do this BEFORE navigating
            # away to the reservations page). Best-effort.
            pdf = self._save_reservation_pdf()
            if pdf:
                self.booking_details["pdf"] = pdf
            log.info("Booking confirmed.")

            # Double-confirm on the reservations page, in this same window
            # (booking is already done, so navigating away is safe now).
            try:
                verified = self.verify_reservation()
                if verified is True:
                    log.info("Double-confirmed: reservation is listed on the "
                             "My Reservations page.")
                    self.booking_details.pop("unconfirmed", None)
                elif verified is False and outcome != "confirmed":
                    log.error("Reservation NOT found on My Reservations and the "
                              "confirmation page was not detected — likely NOT "
                              "booked. CONFIRM MANUALLY.")
                    self.booking_details["unconfirmed"] = True
                elif verified is False:
                    log.warning("Confirmation page was seen but the reservation "
                                "is not listed yet (possible render lag). "
                                "CONFIRM MANUALLY.")
            except Exception as e:
                log.warning(f"Post-booking reservations double-check failed: {e}")
            return True
        except Exception as e:
            log.error(f"Confirm failed: {e}")
            return False

    # -------------------------------------------------------------------------
    # GROUND TRUTH: THE RESERVATIONS PAGE
    # -------------------------------------------------------------------------
    def _booked_marker(self) -> Path:
        return BASE_DIR / f"booked_{self.config.get('booking_date', 'unknown')}.flag"

    def _mark_booked(self) -> None:
        try:
            self._booked_marker().write_text(
                datetime.now().isoformat(), encoding="utf-8")
        except OSError as e:
            log.warning(f"Could not write booked marker: {e}")

    def _capture_confirmation_number(self) -> str | None:
        """Read the confirmation number off the post-finish confirmation page
        (dd[data-ng-bind='data.ConfirmationNumber'], pinned from the post_finish
        dump). Returns None if the element is absent or empty."""
        try:
            el = self.driver.find_element(
                By.CSS_SELECTOR, "dd[data-ng-bind='data.ConfirmationNumber']")
            num = (el.text or "").strip()
            return num or None
        except NoSuchElementException:
            return None
        except Exception as e:
            log.debug(f"Confirmation-number read failed: {e}")
            return None

    def _goto_route(self, sref: str, ready_css: str, timeout: int = DEFAULT_WAIT) -> bool:
        """Change the app's route IN THE CURRENT WINDOW by clicking its own
        a[ui-sref='...'] nav link (an in-app route change, not a full reload —
        so no new tab, no fresh Cloudflare, no session desync). Returns True
        once an element matching ready_css is present."""
        try:
            link = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, f"a[ui-sref='{sref}']"))
            )
            self.driver.execute_script("arguments[0].click();", link)
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ready_css))
            )
            return True
        except TimeoutException:
            return False

    def _return_to_search(self) -> bool:
        """Route back to the search page (current window) and wait for it."""
        return self._goto_route("search", "#players-button")

    def verify_reservation(self) -> bool | None:
        """Does the account hold a reservation for booking_date?

        Runs IN THE CURRENT WINDOW (no second tab/instance): clicks the app's
        own reservations nav link, scans the rendered list for the target date,
        returns True / False / None (couldn't tell). Callers that need to keep
        working on the search page afterward call _return_to_search()."""
        try:
            target = datetime.strptime(self.config["booking_date"], "%Y-%m-%d")
        except (KeyError, ValueError):
            return None
        variants = [
            target.strftime("%m/%d/%Y"),
            target.strftime("%b %d").replace(" 0", " "),
            target.strftime("%B %d").replace(" 0", " "),
        ]
        try:
            if not self._refresh_reservations_cache():
                return None
            text = self.driver.find_element(By.TAG_NAME, "body").text or ""
            found = any(v in text for v in variants)
            log.info(
                f"Reservations check for {variants[0]}: "
                f"{'FOUND' if found else 'not found'}"
            )
            return found
        except Exception as e:
            log.warning(f"Could not check reservations: {e}")
            return None

    # -------------------------------------------------------------------------
    # RESERVATIONS CACHE
    # -------------------------------------------------------------------------
    # The per-field data-ng-bind cells on the reservations table, pinned from
    # the captured dump (dumps/20260710_142544_reservations_page.html). The
    # date-bearing cells use a `| date : ...` filter in the bind expression,
    # so these are matched as substrings, not exact attribute values.
    _RES_FIELDS = (
        "ReservationTime",
        "CourseName",
        "Golfers",
        "Amount",
        "ReservationFee",
        "ConfirmationNumber",
        "CancellableUntil",
        "CancelFee",
    )

    @staticmethod
    def _res_cache_path() -> Path:
        return BASE_DIR / "reservations_cache.json"

    @staticmethod
    def _res_cache_key(rec: dict) -> str:
        """Stable identity for a reservation record: the confirmation number
        when present, else a ReservationTime+CourseName composite."""
        cn = str(rec.get("ConfirmationNumber") or "").strip()
        if cn:
            return f"cn:{cn}"
        return (f"ck:{str(rec.get('ReservationTime') or '').strip()}"
                f"|{str(rec.get('CourseName') or '').strip()}")

    def _extract_reservations(self) -> list:
        """Read the rendered pending-reservation rows from the DOM and return
        a list of structured records. Targets the desktop table's per-field
        data-ng-bind cells inside the Pending panel (the cancellable, future
        reservations). A parse miss logs a warning and yields an empty list
        rather than raising."""
        records = []
        try:
            rows = self.driver.find_elements(
                By.CSS_SELECTOR,
                "#reservations-panel-pending tr[data-ng-repeat='item in data']")
            for row in rows:
                rec = {}
                for key in self._RES_FIELDS:
                    rec[key] = self._row_field_text(row, f"item.{key}")
                # Skip a stray empty ng-repeat placeholder row.
                if any(rec.values()):
                    records.append(rec)
        except Exception as e:
            log.warning(f"Could not parse reservation rows: {e}")
            return []
        return records

    def _refresh_reservations_cache(self) -> bool:
        """Route to the reservations page in the current window and rewrite the
        cache from the rendered rows. Returns True once the page has rendered.
        Reused by verify_reservation and the cold-refresh / cancel flows."""
        if not self._goto_route(
            "reservations", ".reservation-list, .reservations, .no-reservations"
        ):
            # Route/ready element didn't resolve; fall back to a text check.
            if "reservation" not in (self.driver.find_element(
                    By.TAG_NAME, "body").text or "").lower():
                log.warning("Reservations page did not render.")
                return False
        # The list container renders before Angular fills the ng-repeat rows,
        # so a flat sleep can scrape an empty list even when reservations exist
        # (observed 2026-07-15: a page holding two reservations scraped as
        # zero). Wait for real pending rows to appear before reading; a truly
        # empty account falls through on timeout and scrapes (correctly) empty.
        try:
            WebDriverWait(self.driver, 12).until(
                lambda d: d.find_elements(
                    By.CSS_SELECTOR,
                    "#reservations-panel-pending "
                    "tr[data-ng-repeat='item in data']"))
        except TimeoutException:
            log.info("No pending reservation rows settled within the wait; "
                     "treating as no pending reservations.")
        time.sleep(0.5)
        self._dump_stage("reservations_page")
        self._write_res_cache(self._extract_reservations())
        return True

    def _read_res_cache(self) -> dict | None:
        try:
            return json.loads(self._res_cache_path().read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_res_cache(self, entries: list) -> None:
        """Rebuild the cache by merge: carry each record's app-local `notified`
        annotation forward from the previous cache (matched by confirmation
        number, or by the ReservationTime+CourseName composite when no number
        is present). A record that is no longer on the page is dropped, and its
        annotation goes with it. `notified` is never reset to empty here."""
        prior = self._read_res_cache() or {}
        prior_notified = {}
        for rec in prior.get("entries", []):
            if isinstance(rec, dict) and rec.get("notified"):
                prior_notified[self._res_cache_key(rec)] = rec["notified"]
        merged = []
        for rec in entries:
            rec = dict(rec)
            carried = prior_notified.get(self._res_cache_key(rec))
            if carried:
                rec["notified"] = carried
            merged.append(rec)
        try:
            self._res_cache_path().write_text(json.dumps({
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "entries": merged,
            }, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning(f"Could not write reservations cache: {e}")

    def run_show_reservations(self) -> bool:
        """Print the account's reservations. Serves from the cache when fresh
        (reservations_cache_max_age_hours, default 12); otherwise launches a
        session, scrapes the reservations page (which refreshes the cache),
        and prints the result."""
        max_age_h = float(self.config.get("reservations_cache_max_age_hours", 12))
        cache = self._read_res_cache()
        if cache:
            try:
                age = datetime.now() - datetime.fromisoformat(cache["fetched_at"])
                if age <= timedelta(hours=max_age_h):
                    self._print_reservations(cache, cached=True)
                    return True
                log.info(f"Reservations cache is {age} old; refreshing from the site.")
            except (KeyError, ValueError):
                pass
        try:
            self.setup_driver()
            if not self.login():
                log.error("Could not log in to refresh reservations.")
                return False
            self._refresh_reservations_cache()  # scrapes + rewrites the cache
        finally:
            self.close_driver()
        cache = self._read_res_cache()
        if not cache:
            log.error("Reservations refresh produced no cache.")
            return False
        self._print_reservations(cache, cached=False)
        return True

    def run_refresh_reservations(self) -> bool:
        """Force a cold refresh (full session): launch a browser, log in, scrape
        the reservations page (rewriting the cache via merge), and print the
        result. Unlike run_show_reservations it never serves the cache. Uses the
        same session pattern as run_show_reservations' refresh path."""
        try:
            self.setup_driver()
            if not self.login():
                log.error("Could not log in to refresh reservations.")
                return False
            self._refresh_reservations_cache()
        finally:
            self.close_driver()
        cache = self._read_res_cache()
        if not cache:
            log.error("Reservations refresh produced no cache.")
            return False
        self._print_reservations(cache, cached=False)
        return True

    def run_cancel_reservation(self, confirmation_number: str) -> bool:
        """Full session: log in, cancel the reservation, print the result. Same
        session/lock expectations as a booking run (it drives the browser)."""
        result = {"status": "failed", "message": "Cancel did not run."}
        try:
            self.setup_driver()
            if not self.login():
                log.error("Could not log in to cancel the reservation.")
                return False
            result = self.cancel_reservation(
                confirmation_number=confirmation_number)
        finally:
            self.close_driver()
        log.info(f"Cancel result: {result['status']} - {result['message']}")
        return result["status"] == "success"

    @staticmethod
    def _print_reservations(cache: dict, cached: bool) -> None:
        src = "cached" if cached else "fresh from the site"
        log.info(f"===== CURRENT RESERVATIONS ({src}, as of "
                 f"{cache.get('fetched_at', '?')}) =====")
        entries = cache.get("entries") or []
        if not entries:
            log.info("  (none found)")
        for e in entries:
            if isinstance(e, dict):
                line = (f"{e.get('ReservationTime', '?')}  "
                        f"{e.get('CourseName', '')}  "
                        f"players={e.get('Golfers', '')}  "
                        f"conf#={e.get('ConfirmationNumber', '')}  "
                        f"cancel-by {e.get('CancellableUntil', '')}  "
                        f"cancel-fee {e.get('CancelFee', '')}")
                line = " ".join(line.split())
                notified = e.get("notified") or {}
                if notified.get("recipients"):
                    line += f"  [notified: {', '.join(notified['recipients'])}]"
                log.info(f"  {line}")
            else:
                # Tolerate an old flat-string cache from a prior version.
                log.info(f"  {e}")
        log.info("===== END RESERVATIONS =====")

    # -------------------------------------------------------------------------
    # RESERVATION CANCELLATION
    # -------------------------------------------------------------------------
    @staticmethod
    def _parse_reservation_dt(text: str):
        """Parse a rendered reservation timestamp to a datetime, or None. Tries
        the formats seen across the captured pages. A parse miss returns None so
        the caller does not block on an unparseable deadline."""
        s = " ".join((text or "").split())
        if not s:
            return None
        for fmt in ("%a, %b %d, %Y - %I:%M %p",
                    "%a, %b %d, %Y %I:%M %p",
                    "%m/%d/%Y %I:%M %p"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    def _row_field_text(self, row, bind: str) -> str:
        """Rendered text of a reservation row's data-ng-bind cell (bind is the
        bind expression stem, e.g. 'item.CancelFee'). '' when the cell is
        absent. Matched as a substring because the date cells add a filter."""
        try:
            cell = row.find_element(
                By.CSS_SELECTOR, f"td[data-ng-bind*='{bind}']")
            return (cell.text or "").strip()
        except NoSuchElementException:
            return ""

    def _res_matches(self, rec: dict, cn: str, ck: str) -> bool:
        if cn and str(rec.get("ConfirmationNumber") or "").strip() == cn:
            return True
        if ck:
            rk = (f"{str(rec.get('ReservationTime') or '').strip()}"
                  f"|{str(rec.get('CourseName') or '').strip()}")
            return rk == ck
        return False

    def _find_reservation_row(self, cn: str, ck: str):
        """The pending-panel row matching the confirmation number (or the
        ReservationTime+CourseName composite), or None."""
        try:
            rows = self.driver.find_elements(
                By.CSS_SELECTOR,
                "#reservations-panel-pending tr[data-ng-repeat='item in data']")
        except Exception:
            return None
        for row in rows:
            if cn and self._row_field_text(row, "item.ConfirmationNumber") == cn:
                return row
            if ck:
                rk = (f"{self._row_field_text(row, 'item.ReservationTime')}"
                      f"|{self._row_field_text(row, 'item.CourseName')}")
                if rk == ck:
                    return row
        return None

    def _find_cancel_control(self, row):
        """The row's own cancel button.

        A pending/cancellable row was never present when the ground-truth dumps
        were captured (the account had no future reservations then), so the
        exact button class/markup could not be pinned. It is matched
        defensively: a button whose ng-click mentions 'ancel' (Cancel /
        onCancel / CancelClick), or failing that whose aria-label / title
        starts with 'Cancel'. Returns None when no such button exists so we
        never click a guessed control (the Print/Resend buttons are left
        alone). Confirm the real markup from a live pending row and tighten
        this if needed."""
        try:
            buttons = row.find_elements(By.TAG_NAME, "button")
        except Exception:
            return None
        for b in buttons:
            click = ((b.get_attribute("data-ng-click") or "")
                     + " " + (b.get_attribute("ng-click") or "")).lower()
            # Pinned from the live pending-row capture (2026-07-13): the real
            # control is data-ng-click="ec.onCancelReservationClick(item)".
            if "oncancelreservationclick" in click:
                return b
            if "ancel" in click:
                return b
        for b in buttons:
            label = ((b.get_attribute("aria-label") or "")
                     + " " + (b.get_attribute("title") or "")).strip().lower()
            if label.startswith("cancel"):
                return b
        return None

    def _confirm_cancel_modal(self) -> bool:
        """Work the site's cancel modals to completion.

        Clicking a row's cancel control opens a 'Modify Reservation' modal
        (live-captured 2026-07-13) that asks how many players to REMOVE; a
        full cancel removes ALL of them: set select[name=numberOfPlayers] to
        its maximum and click the OK bound to ec.onModifyReservationOKClick().
        A plain Yes/No confirm.html modal (ec.onYesClick) before or after is
        answered Yes. JS-driven so an opacity-0 modal under automation is
        still actionable. Returns True once at least one modal was completed;
        False if none appeared (the caller then relies on the post-cancel
        re-scrape to decide the outcome)."""
        deadline = time.time() + 12
        handled = False
        while time.time() < deadline:
            action = self.driver.execute_script("""
                var mods = document.querySelectorAll('.confirm-modal');
                for (var i = 0; i < mods.length; i++) {
                    var m = mods[i];
                    if (getComputedStyle(m).display === 'none') continue;
                    var sel = m.querySelector('select[name="numberOfPlayers"]');
                    var btns = m.querySelectorAll('button');
                    var ok = null, yes = null;
                    for (var j = 0; j < btns.length; j++) {
                        var b = btns[j];
                        var oc = (b.getAttribute('data-ng-click') || '')
                               + (b.getAttribute('ng-click') || '');
                        if (/onModifyReservationOKClick/i.test(oc)) ok = b;
                        if (/onYesClick/i.test(oc) ||
                            (b.textContent || '').trim().toLowerCase() === 'yes')
                            yes = b;
                    }
                    if (sel && ok) {
                        // Full cancel: remove every player in the reservation.
                        var best = null, bestNum = -1;
                        for (var k = 0; k < sel.options.length; k++) {
                            var n = parseInt(sel.options[k].text, 10);
                            if (!isNaN(n) && n > bestNum) {
                                bestNum = n; best = sel.options[k];
                            }
                        }
                        if (best && sel.value !== best.value) {
                            sel.value = best.value;
                            sel.dispatchEvent(
                                new Event('change', {bubbles: true}));
                        }
                        ok.click();
                        return 'modify-all-' + bestNum;
                    }
                    if (yes) { yes.click(); return 'yes'; }
                }
                return '';
            """)
            if action:
                handled = True
                log.info(f"Cancel modal handled ({action}).")
                # A completed modal may spawn a follow-up confirm; keep
                # polling a short grace window, and give the current modal
                # time to leave the DOM so it is not double-clicked.
                deadline = min(deadline, time.time() + 4)
                time.sleep(1.0)
                continue
            time.sleep(0.3)
        return handled

    def cancel_reservation(self, confirmation_number: str = "",
                           composite_key: str = "") -> dict:
        """Cancel a reservation identified by confirmation number (with a
        ReservationTime+CourseName composite key as a fallback).

        Routes to the reservations page in the current window (no fresh
        Cloudflare), locates the matching pending row, refuses when it is past
        its CancellableUntil deadline, invokes the site's own cancel control,
        confirms the site's Yes/No modal, then re-scrapes to verify the record
        is gone. On success it emails the reservation's `notified` recipients a
        cancellation notice (skipped when there are none). Returns
        {'status': 'success'|'failed'|'blocked', 'message': str}."""
        cn = str(confirmation_number or "").strip()
        ck = str(composite_key or "").strip()
        if not cn and not ck:
            return {"status": "failed",
                    "message": "No reservation identifier given."}

        if not self._refresh_reservations_cache():
            return {"status": "failed",
                    "message": "Reservations page did not render."}

        # Capture who to notify BEFORE the record disappears from the cache on
        # the post-cancel re-scrape (a dropped record loses its annotation).
        notify_recipients = []
        cache = self._read_res_cache() or {}
        for rec in cache.get("entries", []):
            if isinstance(rec, dict) and self._res_matches(rec, cn, ck):
                notify_recipients = list((rec.get("notified") or {}).get(
                    "recipients") or [])
                break

        row = self._find_reservation_row(cn, ck)
        if row is None:
            return {"status": "failed",
                    "message": "Reservation not found on the pending list."}

        # Past the feeless deadline a cancel is still allowed, it just incurs
        # the listed CancelFee (user decision 2026-07-13: the GUI's fee
        # acknowledgement is the money gate, not a hard block). Log it so the
        # fee is on the record; the site remains the real arbiter.
        until = self._row_field_text(row, "item.CancellableUntil")
        dt = self._parse_reservation_dt(until)
        if dt is not None and dt < datetime.now():
            fee = self._row_field_text(row, "item.CancelFee")
            log.warning(f"Cancelling past the feeless deadline ({until}); "
                        f"the site's cancel fee ({fee}) applies.")

        cancel_btn = self._find_cancel_control(row)
        if cancel_btn is None:
            return {"status": "failed",
                    "message": "Could not find the cancel control for this row."}
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", cancel_btn)
            self.driver.execute_script("arguments[0].click();", cancel_btn)
        except Exception as e:
            return {"status": "failed", "message": f"Cancel click failed: {e}"}

        if not self._confirm_cancel_modal():
            log.info("No cancel-confirmation modal detected; proceeding to "
                     "verify from the reservations page.")

        # Re-scrape and verify the record is gone.
        time.sleep(1.5)
        self._refresh_reservations_cache()
        if self._find_reservation_row(cn, ck) is not None:
            return {"status": "failed",
                    "message": "Reservation still present after cancel attempt."}

        if notify_recipients:
            subject = "Tee Time reservation CANCELLED"
            body = "A tee time reservation has been cancelled.\n\n"
            if cn:
                body += f"Confirmation #: {cn}\n"
            body += ("If this cancellation was unexpected, please check with "
                     "the organizer.")
            send_mail_raw(subject, body, notify_recipients)
        else:
            log.info("No notified recipients on the cancelled reservation; "
                     "skipping cancellation email.")

        return {"status": "success", "message": "Reservation cancelled."}

    def _annotate_notified(self, recipients: list, success: bool) -> None:
        """Record who was notified about the just-booked reservation onto its
        cache record (recipients, sent_at, success). Matches by the confirmation
        number captured at booking time, or by the reservation time text as a
        fallback. Runs AFTER the post-booking re-scrape has created the record,
        so it annotates rather than invents a record."""
        recipients = [r for r in (recipients or []) if r]
        cache = self._read_res_cache()
        if not cache or not isinstance(cache.get("entries"), list):
            log.warning("No reservations cache to annotate with notification "
                        "info.")
            return
        cn = str(self.booking_details.get("confirmation_number") or "").strip()
        tee_time = str(self.booking_details.get("time") or "").strip()
        target = None
        for rec in cache["entries"]:
            if (isinstance(rec, dict) and cn
                    and str(rec.get("ConfirmationNumber") or "").strip() == cn):
                target = rec
                break
        if target is None and tee_time:
            for rec in cache["entries"]:
                if (isinstance(rec, dict)
                        and tee_time in (rec.get("ReservationTime") or "")):
                    target = rec
                    break
        if target is None:
            log.warning("Could not match the booked reservation in the cache "
                        "to annotate notification recipients.")
            return
        target["notified"] = {
            "recipients": recipients,
            "sent_at": datetime.now().isoformat(timespec="seconds"),
            "success": bool(success),
        }
        try:
            self._res_cache_path().write_text(
                json.dumps(cache, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning(f"Could not write notification annotation: {e}")

    def _send_mail(self, subject: str, body: str, to_emails: list, pdf: str = None) -> bool:
        return send_mail_raw(subject, body, to_emails, pdf=pdf)

    def send_email(self):
        """Notify additional players via Gmail SMTP on a successful booking."""
        # Only notify when a real reservation was actually completed. Test mode
        # stops before clicking 'Finish Reservation', so no email is sent.
        if not self.booking_details.get("confirmed"):
            log.info("No booking was confirmed; skipping notification email.")
            return

        cfg = self.config.get("email_notifications", {})
        if not cfg.get("enabled"):
            return

        to_emails = cfg.get("to_emails") or []
        if not to_emails:
            log.warning("Email enabled but no recipients configured.")
            return

        pdf = self.booking_details.get("pdf")
        date = self.booking_details.get("date") or "(date unknown)"
        tee_time = self.booking_details.get("time")
        num_players = self.config.get("num_players")
        addl = [str(p).strip() for p in (self.config.get("additional_players") or [])
                if str(p).strip()]

        subject = f"Tee Time: {date}" + (f" at {tee_time}" if tee_time else "")
        body_lines = ["Your tee time is booked.", ""]
        body_lines.append(f"Date: {date}")
        if tee_time:
            body_lines.append(f"Tee time: {tee_time}")
        if num_players:
            body_lines.append(f"Players: {num_players}")
        if addl:
            body_lines.append("Group: " + ", ".join(addl))
        if self.booking_details.get("unconfirmed"):
            body_lines += [
                "",
                "IMPORTANT: the confirmation page was not positively detected, "
                "so this booking is UNVERIFIED — please check My Reservations "
                "manually to be sure it went through.",
            ]
        missed = self.booking_details.get("missed_players")
        if missed:
            body_lines += [
                "",
                "NOTE: these partner names could not be auto-entered and were "
                "left blank on the reservation. Add them manually:",
            ] + [f"  - {m}" for m in missed]
        if pdf and os.path.exists(pdf):
            body_lines += ["", "A confirmation PDF is attached."]

        sent = self._send_mail(subject, "\n".join(body_lines), to_emails, pdf=pdf)
        if sent and pdf and os.path.exists(pdf):
            try:
                os.remove(pdf)
            except OSError as e:
                log.debug(f"Could not remove PDF after email: {e}")
        # Record who was notified onto the just-booked reservation's cache
        # record. confirm() already re-scraped the reservations page (via
        # verify_reservation) before returning, so the record exists to annotate.
        self._annotate_notified(to_emails, sent)

    def send_failure_notification(self, attempts: int):
        """Alert on an exhausted auto-booking run (all attempts failed).

        Sends to email_notifications.failure_contacts, or the baked-in
        DEFAULT_FAILURE_CONTACTS if the config doesn't set any, independent of the
        success 'enabled' flag. Message kept short.
        """
        cfg = self.config.get("email_notifications", {})
        to_emails = cfg.get("failure_contacts") or DEFAULT_FAILURE_CONTACTS
        if not to_emails:
            log.warning("Auto-booking failed but no failure_contacts configured.")
            return

        date = self.config.get("booking_date", "(date unknown)")
        subject = f"Tee Time booking FAILED: {date}"
        body = (f"Auto-booking for {date} failed after {attempts} attempt(s). "
                "No booking could be confirmed. Check the site's My "
                "Reservations page before booking manually — a partially "
                "completed attempt may still have gone through.")
        self._send_mail(subject, body, to_emails)

    # -------------------------------------------------------------------------
    # BOOKING FLOW
    # -------------------------------------------------------------------------
    def _attempt_booking_flow(self, target_date: datetime, *, do_login: bool) -> bool:
        """Run the booking steps once. Returns True on success."""
        steps = []
        if do_login:
            steps.append(("login", self.login))

        steps.extend([
            ("set_player_count", lambda: self.set_player_count(int(self.config.get("num_players", 1)))),
            ("select_date", lambda: self.select_date(target_date)),
            ("select_time", lambda: self.set_time_filter(self.config.get("earliest_acceptable_time"), self.config.get("latest_acceptable_time"))),
            ("set_course_filter", self.set_course_filter),
            ("select_best_tee_time", self.select_best_tee_time),
            # Card selection now happens inside select_best_tee_time's popup
            # handler, on the payment page before #buyTeeTime is submitted.
            ("confirm_booking", self.confirm_booking),
        ])

        for name, step in steps:
            # Pause at the START of the phase so the pre-action state can be
            # inspected before the stage runs.
            self._step_pause(f"About to run '{name}'. Click Step to run it...")
            if not step():
                log.error(f"Booking failed at step: {name}")
                return False
            # And pause again after it completes. confirm_booking has its own
            # test-mode pause before 'Finish Reservation', so skip the after.
            if name != "confirm_booking":
                self._step_pause(f"Finished '{name}'. Click Step to continue...")
        return True

    def _step_pause(self, message: str) -> None:
        """In step mode, emit a marker and block until the controller (GUI)
        sends a line on stdin. No-op when not stepping."""
        if not self.step_mode:
            return
        log.info(f"[STEP] {message}")
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            line = sys.stdin.readline()
            if line == "":  # EOF: stdin pipe closed (e.g. Stop pressed)
                log.info("[STEP] Input closed; continuing without further pauses.")
                self.step_mode = False
        except Exception:
            self.step_mode = False

    def run_booking(self) -> bool:
        """Run a single end-to-end booking attempt."""
        target_date = datetime.strptime(self.config["booking_date"], "%Y-%m-%d")
        self.booking_details["date"] = self.config["booking_date"]

        if self._booked_marker().exists():
            log.info("Booked marker present; a booking for this date was "
                     "already made. Not booking again.")
            return True

        try:
            self.setup_driver()
            if not self._attempt_booking_flow(target_date, do_login=True):
                if not self.testing:
                    self.send_failure_notification(1)
                return False
            self.send_email()
            if self.testing:
                log.info("TEST MODE complete: full flow ran, stopped before final confirmation.")
            else:
                log.info("SUCCESS: Booking completed.")
            return True
        except AlreadyBookedError as e:
            return self._handle_already_booked(str(e))
        except Exception as e:
            log.error(f"Run error: {e}")
            if not self.testing:
                self.send_failure_notification(1)
            return False
        finally:
            self.close_driver()

    def _attempt_loop(self, target_date: datetime, max_tries: int, interval: int) -> bool:
        """Run up to max_tries booking attempts (fresh browser each retry).
        Returns True as soon as one succeeds, False if all are exhausted."""
        try:
            for i in range(1, max_tries + 1):
                log.info(f"Attempt {i}/{max_tries}")
                try:
                    # Fresh browser on every attempt, inside the try so a
                    # driver-startup crash burns one attempt and retries
                    # instead of aborting the whole run.
                    if i > 1:
                        self.close_driver()
                    self.setup_driver()

                    if not self.login():
                        time.sleep(interval)
                        continue

                    if self._attempt_booking_flow(target_date, do_login=False):
                        self.send_email()
                        if self.testing:
                            log.info("TEST MODE complete: full auto flow ran, stopped before final confirmation.")
                        else:
                            log.info("AUTO-BOOKING SUCCESS!")
                        return True

                except AlreadyBookedError as e:
                    return self._handle_already_booked(str(e))
                except PageChangedError:
                    pass  # alarm already sent; keep attempting anyway
                except (InvalidSessionIdException, WebDriverException) as e:
                    log.error(f"Driver error: {e}")
                    try:
                        self.close_driver()
                    except Exception:
                        pass  # Will re-setup at top of next iteration.
                except Exception as e:
                    log.error(f"Loop error: {e}")

                time.sleep(interval)
            return False
        finally:
            self.close_driver()

    def _handle_already_booked(self, site_msg: str) -> bool:
        """The site says a reservation already exists for this day. That IS the
        goal state: verify it, persist the marker, report success-equivalent."""
        verified = self.verify_reservation()
        if verified is not False:
            self._mark_booked()
        date = self.config.get("booking_date", "(date unknown)")
        if verified:
            body = (f"A reservation for {date} already exists on the account "
                    f"(verified on the My Reservations page). No further "
                    f"attempts will be made.\n\nSite message: {site_msg}")
        else:
            body = (f"The site reports an existing reservation for {date}, but "
                    f"it could not be verified on the My Reservations page — "
                    f"CHECK MANUALLY.\n\nSite message: {site_msg}")
        self.send_alert(f"Tee Time already booked: {date}", body, critical=True)
        return True

    def _recover_to_search(self, target_date: datetime) -> bool:
        """Tier-2 page recovery: refresh back to the search page in the SAME
        session (login cookie survives) and re-apply the search state. Far
        cheaper than a browser restart."""
        log.info("Recovering to the search page in-session...")
        try:
            self.driver.get(self.config.get("booking_url"))
            if not self.handle_cloudflare():
                return False
            if not self._is_logged_in():
                log.warning("Session logged out during recovery; re-logging in.")
                if not self.login():
                    return False
            if not self._wait_for_booking_page():
                return False
            self.set_player_count(int(self.config.get("num_players", 1)))
            self.select_date(target_date)
            self.set_time_filter(self.config.get("earliest_acceptable_time"),
                                 self.config.get("latest_acceptable_time"))
            self.set_course_filter()
            return True
        except (InvalidSessionIdException,) as e:
            raise SessionDeadError(str(e))
        except Exception as e:
            log.error(f"In-session recovery failed: {e}")
            return False


    def _prep_session(self, target_date: datetime) -> dict:
        """Warm everything that can be done BEFORE the release instant: browser,
        Cloudflare, login, filters, date (if already selectable). Returns a
        status dict."""
        status = {"ok": False, "date_open": False, "conflict": None, "notes": []}
        try:
            self.setup_driver()
            if not self.login():
                status["notes"].append("login FAILED")
                return status
            self._dump_stage("post_login")

            # Dupe-date conflict check, IN THIS SAME WINDOW (no second tab):
            # look at reservations, then route back to search. If a reservation
            # already covers the target date, striking would only hit the
            # one-per-day rejection.
            status["conflict"] = self.verify_reservation()
            if status["conflict"]:
                status["notes"].append(
                    "existing reservation FOUND for the target date")
            if not self._return_to_search():
                status["notes"].append("could not return to search after "
                                       "reservations check")
                return status

            if not self.set_player_count(int(self.config.get("num_players", 1))):
                status["notes"].append("player count failed")
                return status
            # The target date may not be selectable before release — that is
            # expected; the strike loop keeps re-applying it.
            status["date_open"] = self.select_date(target_date)
            if not status["date_open"]:
                status["notes"].append(
                    "target date not selectable yet (expected pre-release)")
            self.set_time_filter(self.config.get("earliest_acceptable_time"),
                                 self.config.get("latest_acceptable_time"))
            self.set_course_filter()
            self._dump_stage("prep_complete")
            status["ok"] = True
            return status
        except (InvalidSessionIdException, WebDriverException) as e:
            status["notes"].append(f"session error: {e}")
            return status
        except Exception as e:
            status["notes"].append(f"prep error: {e}")
            return status

    def _strike_loop(self, target_date: datetime, release_dt: datetime,
                     deadline: datetime) -> bool:
        """The race: tight in-page loop from just before release until deadline.
        Re-applies the date (its blur re-fires the search), scrapes, books.
        The browser is never torn down here — session death raises to the
        caller for Tier-3 handling."""
        auto = self.config.get("auto_booking", {})
        poll = float(auto.get("strike_poll_seconds", STRIKE_POLL_SECONDS))
        widen_after = int(auto.get("widen_after_seconds", WIDEN_AFTER_SECONDS))
        attempt = 0
        suspect_fails = 0  # consecutive "had slots but couldn't book" failures
        recoveries = 0     # mid-strike page/session losses recovered so far

        while self._now() < deadline:
            attempt += 1
            if suspect_fails >= 2:
                # The page is wedged (stuck modal, dead grid) — a reload is
                # cheaper than burning the window on doomed retries.
                log.warning("Two consecutive wedged attempts; reloading the "
                            "search page in-session.")
                suspect_fails = 0
                if not self._recover_to_search(target_date):
                    raise SessionDeadError("recovery reload failed mid-strike")
            try:
                if not self.select_date(target_date, wait_secs=3):
                    # A failed date-set has two very different causes. A
                    # healthy page that just hasn't opened the date yet
                    # (picker present, still logged in) means keep polling.
                    # A LOST page — the release-time refresh logging everyone
                    # out, a Cloudflare interstitial (which never renders the
                    # picker), the picker gone — can never fix itself by
                    # polling; recover in-session (reload + re-login +
                    # re-apply filters) instead of burning the strike window.
                    # Picker-first keeps the healthy path cheap: one
                    # find_elements, then the login probe.
                    page_lost = (
                        not self.driver.find_elements(By.ID, "pickerDate")
                        or not self._is_logged_in()
                    )
                    if page_lost:
                        recoveries += 1
                        log.warning(f"Search page/session lost mid-strike "
                                    f"(release-time reset?); recovering "
                                    f"in-session (recovery #{recoveries}).")
                        if recoveries == 3:
                            self.send_alert(
                                "TeeTimeBooker WARNING: repeated mid-strike "
                                "recoveries",
                                f"The page/session has been lost {recoveries} "
                                f"times since the strike began — the site may "
                                f"be force-resetting sessions at release. "
                                f"Still recovering and retrying, with pacing "
                                f"to avoid login-hammering.",
                            )
                        if not self._recover_to_search(target_date):
                            raise SessionDeadError(
                                "recovery after mid-strike page loss failed")
                        # Pace repeated recoveries: each cycle already costs a
                        # login, and an unthrottled loss loop is exactly the
                        # bot signature that gets an IP flagged.
                        time.sleep(poll if recoveries < 4 else 15)
                        continue
                    # Not selectable yet: normal seconds before release,
                    # alarming if it persists well past it (B3).
                    if (not self._alerted_date_not_open
                            and self._now() > release_dt + timedelta(seconds=90)):
                        self._alerted_date_not_open = True
                        self.send_alert(
                            "TeeTimeBooker WARNING: date still not open",
                            f"{target_date:%Y-%m-%d} is still not selectable "
                            f"90s past the expected release "
                            f"({release_dt:%H:%M}). The release offset/time "
                            f"may be wrong; still polling.",
                        )
                    time.sleep(poll)
                    continue

                self._wait_for_results_idle(timeout=8)
                widen = self._now() >= release_dt + timedelta(seconds=widen_after)
                if self.select_best_tee_time(widen_tolerance=widen):
                    # Card was already selected inside the popup handler,
                    # before the payment page was submitted.
                    if self.confirm_booking():
                        self.send_email()
                        return True
                    # Finish failed mid-checkout: get back to the grid and
                    # keep striking.
                    if not self._recover_to_search(target_date):
                        raise SessionDeadError("recovery after failed Finish")
                    continue
                suspect_fails = (suspect_fails + 1) if self._sel_had_candidates else 0

            except AlreadyBookedError as e:
                return self._handle_already_booked(str(e))
            except PageChangedError:
                pass  # alarm already sent; keep polling in case it's transient
            except TimeoutException as e:
                log.warning(f"Strike attempt {attempt} timed out: {e}")
            except (InvalidSessionIdException, WebDriverException) as e:
                raise SessionDeadError(str(e))

            time.sleep(poll)

        log.error(f"Strike window closed after {attempt} attempts.")
        return False

    def run_auto_booking(self):
        """Release-anchored auto-booking. Long-running: launch it any time
        (typically from the GUI) and it stays alive until the release.

        Tee times release at auto_booking.release_time (default 06:00) on
        (booking_date - open_days_before). Behavior:
          - already booked (marker file): report and exit.
          - earlier days: no browser, no login — sleep, waking once per night
            at the release hour for a short-lived probe (conflict check plus
            a few light attempts in case times opened early), then back to
            sleep. No failure alert.
          - release day: wait to T-prep_lead, warm the session (prep), wait to
            T-5s on the server-corrected clock, then strike in-page until
            release + strike_max_minutes. Session death falls back to the
            fresh-browser attempt loop (Tier 3). Alerts on exhaustion.

        Rehearsal: set env TTB_SPOOF_RELEASE to '+N' (minutes from now) or
        'YYYY-MM-DDTHH:MM' to act out the release timeline against today.
        """
        auto = self.config.get("auto_booking", {})
        interval = auto.get("check_interval_seconds", 5)
        max_tries = auto.get("max_attempts", 360)
        probe_tries = auto.get("probe_attempts", 2)
        open_offset = auto.get("open_days_before", 14)
        release_time = auto.get("release_time", RELEASE_TIME_DEFAULT)
        prep_lead = int(auto.get("prep_lead_minutes", PREP_LEAD_MINUTES))
        strike_max = int(auto.get("strike_max_minutes", STRIKE_MAX_MINUTES))

        target_date = datetime.strptime(self.config["booking_date"], "%Y-%m-%d")
        self.booking_details["date"] = self.config["booking_date"]

        if self._booked_marker().exists():
            log.info("Booked marker present; nothing to do.")
            self.send_alert(
                f"TeeTimeBooker heartbeat: {self.config['booking_date']} already booked",
                "A booking for this date was already made on a previous run. "
                "No attempts made tonight.",
            )
            return

        if self.testing:
            log.warning("TEST MODE is ON in auto mode: the full flow will run "
                        "but NO real booking will be made.")
            self.send_alert(
                "TeeTimeBooker heartbeat: running in TEST MODE",
                "Auto mode started with testing=true. No real booking will be "
                "made tonight. If this is release night, fix the config NOW.",
            )

        h, m = map(int, release_time.split(":"))
        release_dt = (target_date - timedelta(days=open_offset)).replace(
            hour=h, minute=m, second=0, microsecond=0)

        spoof = os.environ.get("TTB_SPOOF_RELEASE")
        if spoof:
            if spoof.startswith("+"):
                release_dt = datetime.now() + timedelta(minutes=float(spoof[1:]))
            else:
                release_dt = datetime.strptime(spoof, "%Y-%m-%dT%H:%M")
            log.warning(f"SPOOFED release time in effect: {release_dt} "
                        "(rehearsal only — unset TTB_SPOOF_RELEASE for real runs)")

        # Earlier days: stay alive without touching the site. Sleep until the
        # release hour each night, run one short-lived probe (fresh browser,
        # closed right after — nothing signed in is left to go stale), then
        # sleep again. Once the next release-hour tick is the release itself,
        # fall through to the release-day flow below.
        while True:
            next_probe = datetime.now().replace(hour=h, minute=m,
                                                second=0, microsecond=0)
            while next_probe <= datetime.now():
                next_probe += timedelta(days=1)
            if next_probe >= release_dt:
                break
            log.info(f"Release is {release_dt:%Y-%m-%d %H:%M}; sleeping until "
                     f"the nightly probe at {next_probe:%Y-%m-%d %H:%M}.")
            self._wait_until(next_probe)
            # Early conflict check: if a reservation already covers the target
            # date, say so DAYS before release night instead of discovering it
            # at 6 AM.
            try:
                self.setup_driver()
                if self.login() and self.verify_reservation():
                    self._handle_already_booked("found during probe-night check")
                    return
            except Exception as e:
                log.warning(f"Probe-night conflict check failed: {e}")
            finally:
                self.close_driver()
            if self._attempt_loop(target_date, probe_tries, interval):
                log.info("Probe attempt booked early — done.")
                return

        # --- Release day ---
        self._measure_clock_offset()
        effective_release = max(release_dt, self._now())
        strike_deadline = effective_release + timedelta(minutes=strike_max)
        prep_start = release_dt - timedelta(minutes=prep_lead)

        if self._now() < prep_start:
            log.info(f"Release at {release_dt:%H:%M:%S}; waiting until "
                     f"{prep_start:%H:%M:%S} to start prep.")
            self._wait_until(prep_start)
            # In the long-running flow the offset above was measured when this
            # branch was entered — possibly ~a day before release. Re-measure
            # after the sleep (and recompute the derived times) so the strike
            # entry rides a fresh offset, not yesterday's.
            self._measure_clock_offset()
            effective_release = max(release_dt, self._now())
            strike_deadline = effective_release + timedelta(minutes=strike_max)

        # Prep (at least once, even on a late start), retrying on failure
        # until the release moment forces the issue. Retries back off
        # (5→15→30→60s, clamped to the release) so a persistent failure
        # doesn't hammer the site with fresh logins — a bot-signature that
        # risks a Cloudflare/IP block. Email only on the first failure, a
        # CHANGED failure, or the failure→OK transition, not every retry.
        prep = None
        backoff = (5, 15, 30, 60)
        fails = 0
        last_fail_notes = None
        while prep is None or self._now() < release_dt:
            prep = self._prep_session(target_date)
            notes = "; ".join(prep["notes"]) or "none"
            status_body = (
                f"Prep status at {self._now():%H:%M:%S} "
                f"(release {release_dt:%H:%M:%S}):\n"
                f"  session ready: {prep['ok']}\n"
                f"  target date selectable: {prep['date_open']}\n"
                f"  existing-reservation conflict: {prep['conflict']}\n"
                f"  notes: {notes}"
            )
            if prep["ok"]:
                self.send_alert(
                    f"TeeTimeBooker prep OK: {self.config['booking_date']}",
                    status_body)
                break
            fails += 1
            if fails == 1 or notes != last_fail_notes:
                self.send_alert(
                    f"TeeTimeBooker prep FAILED: {self.config['booking_date']}",
                    status_body + f"\n  (failure #{fails}; further identical "
                    "failures will be logged, not emailed)")
            last_fail_notes = notes
            log.error(f"Prep failed ({prep['notes']}); restarting prep "
                      f"(failure #{fails}).")
            self.close_driver()
            delay = backoff[min(fails - 1, len(backoff) - 1)]
            until_release = (release_dt - self._now()).total_seconds()
            time.sleep(max(1.0, min(delay, until_release)))

        if not prep["ok"]:
            # The critical "release fired and we were never ready" moment:
            # say so NOW, while manual booking is still worth something,
            # instead of only after Tier 3 exhausts half an hour later.
            self.send_alert(
                f"TeeTimeBooker prep NEVER READY: {self.config['booking_date']}",
                f"The release ({release_dt:%H:%M:%S}) has passed and prep "
                f"never succeeded (last failure: {last_fail_notes or 'unknown'}). "
                "Falling back to fresh-browser attempts. If a specific time "
                "matters, book MANUALLY now.",
            )

        if prep and prep.get("conflict"):
            log.warning("Existing reservation already covers the target date; "
                        "striking anyway is pointless.")
            self._handle_already_booked("found during prep conflict check")
            self.close_driver()
            return

        try:
            booked = False
            if prep and prep.get("ok"):
                self._wait_until(effective_release - timedelta(seconds=5))
                log.info(f"Strike window open until {strike_deadline:%H:%M:%S}.")
                while not booked and self._now() < strike_deadline:
                    try:
                        booked = self._strike_loop(
                            target_date, effective_release, strike_deadline)
                        break
                    except SessionDeadError as e:
                        # Single-session: on a dead session, re-warm a fresh
                        # browser in place (no hot standby, no second instance).
                        log.error(f"Session died mid-strike ({e}); "
                                  "re-warming in a fresh browser.")
                        self.close_driver()
                        prep = self._prep_session(target_date)
                        if not prep["ok"]:
                            break
            if booked:
                log.info("AUTO-BOOKING SUCCESS!")
                return
        finally:
            self.close_driver()

        # Tier 3: the slow, reliable fallback — fresh browser per attempt.
        log.warning("Strike phase did not produce a booking; falling back to "
                    "the fresh-browser attempt loop.")
        if not self._attempt_loop(target_date, max_tries, interval):
            log.error(f"Auto-booking exhausted all {max_tries} attempts.")
            self.send_failure_notification(max_tries)

    # -------------------------------------------------------------------------
    # WAITING-FOR-OPENING MODE
    # -------------------------------------------------------------------------
    # A slow, long-running hunter for an ALREADY-RELEASED day, independent of
    # the release strike above. It watches the acceptable window on
    # booking_date, takes (or emails about) any slot that opens inside it,
    # then keeps narrowing symmetrically toward desired_time while
    # continuous_upgrade is on. Design: .claude/plans/
    # 2026-07-teetimebooker-waiting-mode.md plus the 2026-07-13 amendments.

    def _waiting_apply_window(self, st) -> None:
        """Express the current (possibly narrowed) window through the
        in-memory config so select_best_tee_time, the filter re-apply paths
        and the evaluator all see the same bounds. The config FILE is never
        written."""
        self.config["earliest_acceptable_time"] = (
            st["earliest"].strftime("%H:%M") if st["earliest"] else None)
        self.config["latest_acceptable_time"] = (
            st["latest"].strftime("%H:%M") if st["latest"] else None)

    def _force_research(self, st) -> None:
        """Force the site to run a FRESH tee-time query. Re-setting the date to
        the same value is a no-op under the page's ng-model-options
        updateOn:'blur', so a slot that freed up after the initial search never
        appears in the stale grid (observed 2026-07-15: a slot open for minutes
        went unseen across ~15 polls). Re-applying the time filter calls the
        page's own ec.onTeeTimeFilterHandleUp(), which always re-issues the
        search, then we wait for the results to settle before scraping."""
        self.set_time_filter(
            st["earliest"].strftime("%H:%M") if st["earliest"] else None,
            st["latest"].strftime("%H:%M") if st["latest"] else None)
        self._wait_for_results_idle(timeout=8)

    def _waiting_narrow(self, st, took) -> bool:
        """Record `took` as the held slot and narrow the window SYMMETRICALLY
        by distance-to-ideal: with d = abs(took - ideal), only slots strictly
        closer than d on EITHER side stay acceptable (a one-sided narrowing
        could auto-swap to a worse slot on the other side of the ideal).
        Returns False when the window has collapsed (the held slot IS the
        ideal)."""
        st["current_best"] = took
        d = abs(took - st["ideal"])
        if d == timedelta(0):
            return False
        lo = st["ideal"] - d + timedelta(minutes=1)
        hi = st["ideal"] + d - timedelta(minutes=1)
        st["earliest"] = max(st["orig_earliest"], lo) if st["orig_earliest"] else lo
        st["latest"] = min(st["orig_latest"], hi) if st["orig_latest"] else hi
        self._waiting_apply_window(st)
        log.info(f"Window narrowed to "
                 f"{self.config.get('earliest_acceptable_time')}-"
                 f"{self.config.get('latest_acceptable_time')} "
                 f"(holding {took:%H:%M}, ideal {st['ideal']:%H:%M}).")
        return True

    def _waiting_load_holds(self, st) -> None:
        """Collect the target day's reservations from the cache: identity keys
        for cancelling, tee time for the proximity rule, CancellableUntil for
        the money guard. When the currently-held slot's record is found, its
        own CancellableUntil re-anchors the run deadline (the site's field is
        authoritative over the computed fee_period_hours bound)."""
        st["holds"] = []
        if os.environ.get("TTB_WAIT_IGNORE_HOLDS"):
            return  # test harness: behave as if no reservations are held
        cache = self._read_res_cache() or {}
        for rec in cache.get("entries", []):
            if not isinstance(rec, dict):
                continue
            dt = self._parse_reservation_dt(rec.get("ReservationTime"))
            if dt is None or dt.date() != st["target_date"].date():
                continue
            st["holds"].append({
                "cn": str(rec.get("ConfirmationNumber") or "").strip(),
                "ck": (f"{str(rec.get('ReservationTime') or '').strip()}"
                       f"|{str(rec.get('CourseName') or '').strip()}"),
                "time": dt.replace(year=1900, month=1, day=1),
                "cancellable_until": self._parse_reservation_dt(
                    rec.get("CancellableUntil")),
            })
        cur = self._waiting_current_hold(st)
        if cur and cur["cancellable_until"]:
            st["deadline"] = cur["cancellable_until"]

    @staticmethod
    def _waiting_current_hold(st):
        """The tracked hold record matching the currently-held slot time
        (i.e. the most recent waiter-secured hold), or None."""
        if st["current_best"] is None:
            return None
        for h in st["holds"]:
            if h["time"] == st["current_best"]:
                return h
        return None

    def _waiting_cancel_allowed(self, st, hold) -> bool:
        """Money guard, enforced HERE: cancel_reservation itself now proceeds
        past CancellableUntil and lets the fee apply (the GUI checkbox is the
        gate there), so the waiter must refuse any automated cancel that
        would incur a fee unless fee_period_auto_swap explicitly allows
        spending. An unparseable deadline refuses too: never spend money on
        a guess."""
        if st["fee_auto_swap"]:
            return True
        until = hold.get("cancellable_until")
        if until is None:
            return False
        return self._now() < until

    def _waiting_notify_opening(self, st, slot, reason) -> None:
        """Email an opening instead of taking it, once per opening per run
        (deduped in-memory on date+time)."""
        key = f"{self.config.get('booking_date')}|{slot['time_str']}"
        if key in st["notified"]:
            return
        st["notified"].add(key)
        label = "[TEST] " if self.testing else ""
        subject = (f"{label}Tee time opening: {slot['time_str']} on "
                   f"{self.config.get('booking_date')}")
        body = (f"Waiting mode spotted an opening it did not take.\n\n"
                f"Date: {self.config.get('booking_date')}\n"
                f"Time: {slot['time_str']} (capacity {slot['players']})\n"
                f"Window: {self.config.get('earliest_acceptable_time')}-"
                f"{self.config.get('latest_acceptable_time')}, "
                f"ideal {st['ideal']:%H:%M}\n"
                f"Not taken because: {reason}\n\n"
                f"Book it manually if you want it.")
        to = self.config.get("email_notifications", {}).get("to_emails") or []
        if to:
            send_mail_raw(subject, body, to)
        else:
            log.warning("Opening spotted but email_notifications.to_emails "
                        "is empty; logging only.")
        log.info(f"Opening notified ({slot['time_str']}): {reason}")

    def _waiting_resume_search(self, target_date: datetime) -> bool:
        """Get back onto the search grid after a booking/cancel detour and
        re-apply the search state (current narrowed window included). Tries
        the in-app route first (no reload, no fresh Cloudflare); falls back
        to the full in-session recovery reload."""
        try:
            if self._return_to_search():
                self.set_player_count(int(self.config.get("num_players", 1)))
                if self.select_date(target_date):
                    self.set_time_filter(
                        self.config.get("earliest_acceptable_time"),
                        self.config.get("latest_acceptable_time"))
                    self.set_course_filter()
                    return True
        except (InvalidSessionIdException,) as e:
            raise SessionDeadError(str(e))
        except Exception as e:
            log.warning(f"In-app return to search failed ({e}); reloading.")
        return self._recover_to_search(target_date)

    def _waiting_rebook_slot(self, target_date: datetime, t) -> bool:
        """Emergency rebook of a just-cancelled slot: pin the window to that
        exact time and run the normal booking path once."""
        saved = (self.config.get("earliest_acceptable_time"),
                 self.config.get("latest_acceptable_time"))
        self.config["earliest_acceptable_time"] = t.strftime("%H:%M")
        self.config["latest_acceptable_time"] = t.strftime("%H:%M")
        try:
            if not self._waiting_resume_search(target_date):
                return False
            self._wait_for_results_idle(timeout=8)
            if self.select_best_tee_time():
                return self.confirm_booking()
            return False
        except AlreadyBookedError:
            # The site thinks a reservation is already held for this day, so
            # the swap booking may in fact have gone through.
            log.warning("Rebook hit an already-booked response; the swap "
                        "booking may have succeeded. CHECK MANUALLY.")
            return False
        except Exception as e:
            log.error(f"Rebook attempt failed: {e}")
            return False
        finally:
            (self.config["earliest_acceptable_time"],
             self.config["latest_acceptable_time"]) = saved

    def _waiting_swap(self, target_date: datetime, st, slot, hold):
        """Cancel-first swap: drop `hold`, then book `slot`. If the booking
        then fails, immediately try to rebook the just-cancelled slot; the
        sequence must never end holding nothing without a CRITICAL alert.
        Returns 'done' when the hunt is over, None to keep polling."""
        res = self.cancel_reservation(
            confirmation_number=hold["cn"], composite_key=hold["ck"])
        if res["status"] != "success":
            log.error(f"Cancel-first failed ({res['message']}); swap aborted.")
            self._waiting_notify_opening(
                st, slot, f"cancel-first failed: {res['message']}")
            self._waiting_resume_search(target_date)
            return None
        st["holds"] = [h for h in st["holds"] if h is not hold]
        booked = False
        try:
            if (self._waiting_resume_search(target_date)
                    and self.select_best_tee_time()):
                booked = self.confirm_booking()
        except AlreadyBookedError as e:
            log.error(f"Second booking still refused after the cancel: {e}")
        except Exception as e:
            log.error(f"Swap booking failed: {e}")
        if booked:
            return self._waiting_after_secured(target_date, st, slot)

        # Holding nothing: rebook the slot that was just cancelled.
        log.error(f"Swap stranded after cancelling {hold['time']:%H:%M}; "
                  "attempting to rebook it.")
        if self._waiting_rebook_slot(target_date, hold["time"]):
            log.warning("Rebooked the cancelled slot; the swap is undone.")
            self._waiting_load_holds(st)
            self._waiting_resume_search(target_date)
            return None
        self.send_alert(
            f"TeeTimeBooker waiting: NO RESERVATION HELD "
            f"({self.config.get('booking_date')})",
            f"A cancel-first swap cancelled the {hold['time']:%H:%M} "
            f"reservation, booking {slot['time_str']} then failed, and "
            f"rebooking the cancelled slot failed too. NOTHING is held for "
            f"this day right now. Book manually ASAP.",
            critical=True)
        # Nothing left to upgrade: hunt the whole original window again.
        st["current_best"] = None
        st["earliest"], st["latest"] = st["orig_earliest"], st["orig_latest"]
        self._waiting_apply_window(st)
        self._waiting_resume_search(target_date)
        return None

    def _waiting_after_secured(self, target_date: datetime, st, slot):
        """Bookkeeping after a slot is secured: notifications, cache reload
        (the NEW hold's CancellableUntil re-anchors the run deadline),
        symmetric narrowing, termination checks. Returns 'done' or None."""
        # select_best_tee_time may have booked a different (closer) slot than
        # the evaluated candidate if the grid moved; trust what it recorded.
        took_str = self.booking_details.get("time") or slot["time_str"]
        try:
            took = datetime.strptime(took_str, "%I:%M %p")
        except ValueError:
            took, took_str = slot["time"], slot["time_str"]
        st["last_secured"] = took_str
        st["current_best"] = took
        label = "[TEST] " if self.testing else ""
        if self.testing:
            log.warning(f"TEST MODE: full hunt reached {took_str} and "
                        "stopped before Finish Reservation.")
        self.send_email()  # real bookings only (gated on the confirmed flag)
        self.send_alert(
            f"{label}TeeTimeBooker waiting: secured {took_str} on "
            f"{self.config.get('booking_date')}",
            f"Waiting mode "
            f"{'would have booked' if self.testing else 'booked'} "
            f"{took_str} (ideal {st['ideal']:%H:%M}).")
        if not self.testing:
            # confirm_booking's verify step refreshed the cache; adopt the
            # new hold and re-anchor the deadline on ITS CancellableUntil.
            self._waiting_load_holds(st)
        if not st["continuous"]:
            st["done_reason"] = (f"slot {took_str} secured and "
                                 "continuous_upgrade is off")
            return "done"
        if not self._waiting_narrow(st, took):
            st["done_reason"] = f"ideal time {took_str} secured"
            return "done"
        if not self._waiting_resume_search(target_date):
            raise SessionDeadError("could not return to search after booking")
        return None

    def _waiting_take_slot(self, target_date: datetime, st, slot):
        """Try to secure `slot`. Book-first by default (a second same-day
        booking is believed allowed outside the ~1h proximity band, but is
        UNVERIFIED); if the site rejects it with the already-booked signal,
        adapt to cancel-first, gated on cancel_existing_on_upgrade AND the
        money guard. Returns 'done' when the hunt is over, None to keep
        polling."""
        prior = self._waiting_current_hold(st) or (
            st["holds"][0] if st["holds"] else None)
        near = [h for h in st["holds"]
                if abs((slot["time"] - h["time"]).total_seconds()) / 60.0
                <= WAIT_PROXIMITY_MINUTES]

        def swap_gate(hold):
            """Why a cancel-first swap of `hold` is not allowed, or None."""
            if not st["cancel_on_upgrade"]:
                return "cancel_existing_on_upgrade is off"
            if not self._waiting_cancel_allowed(st, hold):
                return ("cancelling the existing reservation would incur "
                        "its cancel fee (money guard)")
            if self.testing:
                return "test mode (cancel-first swap not executed)"
            return None

        if near:
            # Inside the ~1h band the site rejects the second booking, so
            # book-first is pointless: swap (cancel-first) or skip + notify.
            blocked = swap_gate(near[0])
            if blocked:
                self._waiting_notify_opening(
                    st, slot, f"within ~1h of the {near[0]['time']:%H:%M} "
                    f"reservation and {blocked}")
                return None
            return self._waiting_swap(target_date, st, slot, near[0])

        try:
            booked = (self.select_best_tee_time()
                      and self.confirm_booking())
        except AlreadyBookedError as e:
            log.warning(f"Site refused a second booking for this day ({e}); "
                        "adapting to cancel-first.")
            if prior is None:
                self._waiting_notify_opening(
                    st, slot, "site refused a second booking and no known "
                    "hold could be swapped")
                self._waiting_resume_search(target_date)
                return None
            blocked = swap_gate(prior)
            if blocked:
                self._waiting_notify_opening(
                    st, slot, f"site refused a second booking and {blocked}")
                self._waiting_resume_search(target_date)
                return None
            return self._waiting_swap(target_date, st, slot, prior)
        if not booked:
            log.warning(f"Could not secure {slot['time_str']}; resuming poll.")
            if not self._waiting_resume_search(target_date):
                raise SessionDeadError(
                    "could not return to search after a failed booking")
            return None

        # Booked (or test mode's stop-before-Finish). Optionally drop the
        # prior hold now that the better one is held (book-then-cancel: the
        # safe default order, never left holding nothing).
        if (not self.testing) and st["cancel_on_upgrade"] and prior is not None:
            if self._waiting_cancel_allowed(st, prior):
                res = self.cancel_reservation(
                    confirmation_number=prior["cn"],
                    composite_key=prior["ck"])
                if res["status"] != "success":
                    self.send_alert(
                        "TeeTimeBooker waiting: prior hold NOT cancelled",
                        f"Booked {slot['time_str']} but cancelling the prior "
                        f"{prior['time']:%H:%M} failed ({res['message']}). "
                        "Both reservations are currently held.")
            else:
                log.warning(f"Money guard: keeping the prior "
                            f"{prior['time']:%H:%M} hold (cancelling it "
                            "now would incur its fee).")

        return self._waiting_after_secured(target_date, st, slot)

    def _waiting_tick(self, target_date: datetime, st):
        """One in-session evaluation: express the current window, rank the
        grid READ-ONLY, then take or notify. Returns 'done' or None."""
        self._waiting_apply_window(st)
        # Re-issue a fresh search every tick so a newly-freed slot is actually
        # seen; select_date alone re-scrapes a stale grid (see _force_research).
        self._force_research(st)
        candidates, _slots = self._evaluate_tee_times()
        if not candidates:
            return None
        best = candidates[0][1]
        if not st["auto_take"]:
            self._waiting_notify_opening(
                st, best, "auto_take is off (notify-only run)")
            return None
        if self._now() >= st["deadline"] and not st["fee_auto_swap"]:
            # Deliberate policy, hold or no hold: inside the fee period the
            # waiter takes on NO fee-bound commitments at all; only
            # fee_period_auto_swap relaxes this.
            self._waiting_notify_opening(
                st, best, "inside the fee period (no-spend downgrade)")
            return None
        return self._waiting_take_slot(target_date, st, best)

    def _dismiss_session_warning(self) -> bool:
        """Click 'Continue Session' on the site's inactivity modal.

        sessionWarning.html renders a .confirm-modal ("Your session will
        expire in 1 minute due to inactivity") whose button is bound to
        ec.onContinueSessionClick(). The site raises it even while the waiter
        is polling, so handling it keeps ONE login alive indefinitely and
        makes the expensive login+Cloudflare reconnect a rare fallback rather
        than the routine path. JS-driven so an opacity-0 modal under
        automation is still actionable; dismissing it also clears an overlay
        that would otherwise sit over the results and block slot detection.
        Returns True if a warning was dismissed. Best-effort: a dead session
        just returns False and the poll loop's select_date surfaces the death."""
        try:
            return bool(self.driver.execute_script("""
                var mods = document.querySelectorAll('.confirm-modal');
                for (var i = 0; i < mods.length; i++) {
                    var m = mods[i];
                    if (getComputedStyle(m).display === 'none') continue;
                    var btns = m.querySelectorAll('button');
                    for (var j = 0; j < btns.length; j++) {
                        var oc = (btns[j].getAttribute('data-ng-click') || '')
                               + (btns[j].getAttribute('ng-click') || '');
                        if (/onContinueSessionClick/i.test(oc)) {
                            btns[j].click();
                            return true;
                        }
                    }
                }
                return false;
            """))
        except Exception:
            return False

    def _waiting_poll_loop(self, target_date: datetime, st) -> str:
        """POLL state: cheap in-session re-search on a jittered cadence until
        the session ends, the deadline passes, or the hunt is over. Session
        lifetime is DISCOVERED, not assumed: polls until _is_logged_in()
        goes false or the session dies, so the logged lifetime answers the
        idle-login question empirically. session_max_minutes is only an
        optional safety cap (0 = uncapped, the default). Returns the reason
        polling stopped."""
        wm = self.config.get("waiting_mode", {})
        poll = float(wm.get("in_session_poll_seconds", WAIT_POLL_SECONDS))
        session_max = int(wm.get("session_max_minutes", 0))
        cap = (self._now() + timedelta(minutes=session_max)
               if session_max > 0 else None)
        while True:
            now = self._now()
            if now >= st["window_end"]:
                st["done_reason"] = "target window has passed"
                return "done"
            if now >= st["deadline"] and not st["continue_fee"]:
                st["done_reason"] = "fee-period deadline reached"
                return "done"
            if cap and now >= cap:
                return "session_max_minutes safety cap"
            try:
                # Keep the one session alive across the inactivity timeout so
                # we can poll indefinitely without a fresh login+Cloudflare.
                if self._dismiss_session_warning():
                    log.info("Dismissed the session-expiry warning "
                             "(Continue Session); login kept alive.")
                if not self.select_date(target_date, wait_secs=3):
                    if (not self.driver.find_elements(By.ID, "pickerDate")
                            or not self._is_logged_in()):
                        return "logged out / page lost"
                    # Healthy page, date momentarily unsettable: keep polling.
                else:
                    self._wait_for_results_idle(timeout=8)
                    if self._waiting_tick(target_date, st) == "done":
                        return "done"
            except PageChangedError:
                pass  # alarm already sent; keep polling in case it's transient
            except SessionDeadError as e:
                log.error(f"Session died mid-poll: {e}")
                return "session died"
            except TimeoutException as e:
                log.warning(f"Waiting-poll tick timed out: {e}")
            except (InvalidSessionIdException, WebDriverException) as e:
                log.error(f"Session died mid-poll: {e}")
                return "session died"
            time.sleep(poll + random.uniform(0.0, min(5.0, poll * 0.2)))

    def run_waiting_mode(self):
        """Waiting-for-opening mode: hunt an already-released day for a slot
        inside the acceptable window, upgrading toward desired_time.

        Outer loop reconnects on a slow jittered cadence (fresh Cloudflare +
        login via _prep_session); inner loop re-searches cheaply in-session.
        The instance lock is RELEASED during each idle gap so reservations /
        cancel runs can use the browser, and reacquired before reconnecting.
        Test mode runs the full hunt but never books or cancels, and labels
        its emails."""
        wm = self.config.get("waiting_mode", {})
        recon_min = int(wm.get("reconnect_min_minutes",
                               WAIT_RECONNECT_MIN_MINUTES))
        recon_max = int(wm.get("reconnect_max_minutes",
                               WAIT_RECONNECT_MAX_MINUTES))
        recon_jit = int(wm.get("reconnect_jitter_seconds",
                               WAIT_RECONNECT_JITTER_SECONDS))
        fee_hours = int(wm.get("fee_period_hours", WAIT_FEE_PERIOD_HOURS))

        target_date = datetime.strptime(self.config["booking_date"], "%Y-%m-%d")
        self.booking_details["date"] = self.config["booking_date"]

        def parse_hm(key):
            v = self.config.get(key)
            return datetime.strptime(v, "%H:%M") if v else None

        ideal = datetime.strptime(
            self.config.get("desired_time", "09:00"), "%H:%M")
        earliest = parse_hm("earliest_acceptable_time")
        latest = parse_hm("latest_acceptable_time")
        window_close = latest or ideal
        tee_dt = target_date.replace(hour=ideal.hour, minute=ideal.minute)
        st = {
            "target_date": target_date,
            "ideal": ideal,
            "earliest": earliest,
            "latest": latest,
            "orig_earliest": earliest,
            "orig_latest": latest,
            "window_end": target_date.replace(hour=window_close.hour,
                                              minute=window_close.minute),
            "deadline": tee_dt - timedelta(hours=fee_hours),
            "current_best": None,
            "holds": [],
            "notified": set(),
            "done_reason": "",
            "last_secured": "",
            "auto_take": bool(wm.get("auto_take", True)),
            "continuous": bool(wm.get("continuous_upgrade", True)),
            "cancel_on_upgrade": bool(
                wm.get("cancel_existing_on_upgrade", False)),
            "continue_fee": bool(wm.get("continue_into_fee_period", False)),
            "fee_auto_swap": bool(wm.get("fee_period_auto_swap", False)),
        }

        if self._now() >= st["window_end"]:
            log.error("The target window has already passed; nothing to hunt.")
            self.send_alert(
                f"TeeTimeBooker waiting: window already passed "
                f"({self.config['booking_date']})",
                "Waiting mode was launched after the acceptable window on "
                "the target day; no run was started.")
            return

        if self.testing:
            log.warning("TEST MODE is ON in waiting mode: the full hunt runs "
                        "but NO booking or cancel will be made.")
            self.send_alert(
                "TeeTimeBooker heartbeat: waiting mode in TEST MODE",
                "Waiting mode started with testing=true. Openings are hunted "
                "and emailed, but nothing is booked or cancelled.")

        # BOOTSTRAP: seed the held slot from the (possibly stale) cache; the
        # first CONNECT refreshes it. A pre-existing in-window hold narrows
        # the window immediately so only strictly-closer slots are accepted.
        # TEST HARNESS ONLY: TTB_WAIT_IGNORE_HOLDS skips seeding/narrowing from
        # existing reservations, so the waiter polls the full window even when
        # the desired slot is already held (which normally makes it exit at
        # once). Lets a tester hold a slot, start the waiter, then cancel that
        # slot mid-run to confirm the in-session poll registers the freed time
        # and books it. Not for production use.
        if os.environ.get("TTB_WAIT_IGNORE_HOLDS"):
            log.warning("TTB_WAIT_IGNORE_HOLDS set: ignoring existing holds "
                        "(test harness); polling the full window regardless.")
        else:
            self._waiting_load_holds(st)
            seed = min(
                (h for h in st["holds"]
                 if (st["earliest"] is None or h["time"] >= st["earliest"])
                 and (st["latest"] is None or h["time"] <= st["latest"])),
                key=lambda h: abs((h["time"] - ideal).total_seconds()),
                default=None)
            if seed:
                log.info(f"Pre-existing in-window hold at {seed['time']:%H:%M}; "
                         "hunting only closer slots.")
                if not self._waiting_narrow(st, seed["time"]):
                    st["done_reason"] = "the ideal time is already held"
                elif not st["continuous"]:
                    st["done_reason"] = ("an acceptable slot is already held "
                                         "and continuous_upgrade is off")
                # The hold's own CancellableUntil is the authoritative free-cancel
                # boundary; prefer it over the computed fee_period_hours bound.
                if seed["cancellable_until"]:
                    st["deadline"] = seed["cancellable_until"]

        log.info(f"Waiting mode: {self.config['booking_date']} window "
                 f"{self.config.get('earliest_acceptable_time')}-"
                 f"{self.config.get('latest_acceptable_time')} ideal "
                 f"{ideal:%H:%M}; deadline {st['deadline']:%Y-%m-%d %H:%M}; "
                 f"auto_take={st['auto_take']} "
                 f"continuous_upgrade={st['continuous']}.")

        alerted_recover = False
        alerted_lock = False
        try:
            while not st["done_reason"]:
                now = self._now()
                if now >= st["window_end"]:
                    st["done_reason"] = "target window has passed"
                    break
                if now >= st["deadline"] and not st["continue_fee"]:
                    st["done_reason"] = "fee-period deadline reached"
                    break

                # CONNECT: full warm-up (fresh Cloudflare + login), reusing
                # the strike prep. It also refreshes the reservations cache
                # (via verify_reservation), keeping the holds and the fee
                # boundary current.
                self._measure_clock_offset()
                self._waiting_apply_window(st)
                prep = self._prep_session(target_date)
                if prep["ok"]:
                    self._waiting_load_holds(st)
                    session_start = self._now()
                    reason = self._waiting_poll_loop(target_date, st)
                    mins = ((self._now() - session_start).total_seconds()
                            / 60.0)
                    log.info(f"Session survived {mins:.1f} minutes under "
                             f"active polling ({reason}).")
                    if reason == "done":
                        self.close_driver()
                        break
                else:
                    # RECOVER: one-shot alert, no tight retry; the idle gap
                    # below is the backoff.
                    notes = "; ".join(prep["notes"]) or "unknown"
                    log.error(f"Waiting-mode connect failed: {notes}")
                    if not alerted_recover:
                        alerted_recover = True
                        self.send_alert(
                            "TeeTimeBooker waiting: connect failed",
                            f"A waiting-mode reconnect failed ({notes}). "
                            "Backing off to the normal idle gap and trying "
                            "again next cadence; further failures are "
                            "logged, not emailed.")

                # IDLE: browser down, lock released so reservations/cancel
                # runs can use the gap, jittered sleep, lock reacquired.
                self.close_driver()
                if st["done_reason"]:
                    break
                _release_single_instance_lock()
                gap = random.uniform(recon_min * 60.0, recon_max * 60.0)
                gap += random.uniform(-recon_jit, recon_jit)
                wake = self._now() + timedelta(seconds=max(60.0, gap))
                log.info(f"IDLE: browser closed, lock released; next "
                         f"reconnect at {wake:%Y-%m-%d %H:%M:%S}.")
                self._wait_until(wake)
                lock_fails = 0
                while True:
                    # Check for a holder BEFORE acquiring so a busy gap logs
                    # instead of firing the duplicate-instance email.
                    holder = _other_instance_pid()
                    if not holder and _acquire_single_instance_lock():
                        break
                    lock_fails += 1
                    log.warning(f"Instance lock is busy (PID {holder}); "
                                f"retrying in {recon_min} min.")
                    if lock_fails == 3 and not alerted_lock:
                        alerted_lock = True
                        self.send_alert(
                            "TeeTimeBooker waiting: lock contention",
                            f"The instance lock has been held by PID "
                            f"{holder} across {lock_fails} reconnect "
                            "attempts. A stray instance may be running.")
                    self._wait_until(self._now()
                                     + timedelta(minutes=recon_min))
        finally:
            self.close_driver()

        held = (f"{st['current_best']:%H:%M}" if st["current_best"]
                else "none")
        label = "[TEST] " if self.testing else ""
        log.info(f"Waiting mode finished: {st['done_reason'] or 'stopped'}; "
                 f"held slot: {held}.")
        self.send_alert(
            f"{label}TeeTimeBooker waiting finished: "
            f"{self.config['booking_date']}",
            f"Waiting mode ended: {st['done_reason'] or 'stopped'}.\n"
            f"Held slot: {held} (ideal {ideal:%H:%M}).\n"
            f"Last secured this run: {st['last_secured'] or 'none'}.")

    def _wait_until(self, dt: datetime):
        """Block until dt on the server-corrected clock: coarse sleeps down to
        the last 2 seconds, then a fine 10ms poll so the wake-up is sharp."""
        remaining = (dt - self._now()).total_seconds()
        if remaining > 0:
            log.info(f"Waiting until {dt:%H:%M:%S} ({remaining:.0f}s)...")
        while True:
            remaining = (dt - self._now()).total_seconds()
            if remaining <= 0:
                return
            time.sleep(min(remaining - 1, 30) if remaining > 2 else 0.01)


# =============================================================================
# CLI ENTRY POINT
# =============================================================================
def _run_setup_command():
    try:
        run_setup()
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def _run_calibrate_command():
    # Hold the instance lock for the whole calibration so a booking launched
    # mid-calibration is blocked (and vice versa) — calibration drives the
    # one system mouse the booker's Cloudflare clicks depend on.
    if _other_instance_pid() or not _acquire_single_instance_lock():
        print(
            "A booking run is active; calibration would fight it for the "
            "mouse. Stop it first or wait for it to finish.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        calibrate_cloudflare()
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def _load_config_or_exit(require_booking: bool = True) -> dict:
    try:
        return load_config(require_booking=require_booking)
    except CalibrationRequired as e:
        print(
            "Cloudflare calibration not done. Run: py TimmysTeeTimeBooker.py setup",
            file=sys.stderr,
        )
        send_crash_alert("config load (calibration required)", e)
        sys.exit(1)
    except (ConfigError, SystemExit) as e:
        print(str(e), file=sys.stderr)
        # An unattended run dying at config load must not be silent: this is
        # where a stale booking_date or moved .secret.key would otherwise
        # kill every scheduled run with nobody noticing.
        send_crash_alert("config load", e)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading config: {e}", file=sys.stderr)
        send_crash_alert("config load", e)
        sys.exit(1)


def main():
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "default"

    if cmd == "help":
        print(__doc__)
        return
    if cmd == "setup":
        _run_setup_command()
        return
    if cmd == "calibrate":
        _run_calibrate_command()
        return

    # Reservations verbs need valid sign-in but not a valid future booking,
    # so a stale booking_date must not block them.
    require_booking = cmd not in ("reservations", "reservations-refresh", "cancel")
    config = _load_config_or_exit(require_booking=require_booking)
    booker = TeeTimeBooker(config)

    try:
        if cmd == "reservations":
            # A live booking run owns the browser AND the mouse; a stale-cache
            # refresh here would launch a second driver into it (the exact
            # collision the instance lock prevents). Serve the cache only.
            # Otherwise TAKE the lock ourselves so the protection is
            # two-directional: a booking launched mid-refresh gets blocked too.
            if _other_instance_pid() or not _acquire_single_instance_lock():
                log.warning("A booking run is active; showing cached "
                            "reservations only (no browser refresh).")
                cache = booker._read_res_cache()
                if cache:
                    booker._print_reservations(cache, cached=True)
                else:
                    log.error("No cached reservations available while a "
                              "booking run is active; try again later.")
                return
            booker.run_show_reservations()
            return
        if cmd == "reservations-refresh":
            # A cold refresh drives the browser (login + Cloudflare), so it must
            # not run alongside a booking (shared driver + the one mouse cursor).
            if _other_instance_pid() or not _acquire_single_instance_lock():
                log.warning("A booking run is active; cannot refresh "
                            "reservations now (browser in use). Showing the "
                            "cached copy instead.")
                cache = booker._read_res_cache()
                if cache:
                    booker._print_reservations(cache, cached=True)
                else:
                    log.error("No cached reservations available while a "
                              "booking run is active; try again later.")
                return
            booker.run_refresh_reservations()
            return
        if cmd == "cancel":
            conf = sys.argv[2].strip() if len(sys.argv) > 2 else ""
            if not conf:
                log.error("Usage: cancel <confirmation_number>")
                return
            # Cancel drives the browser too; guard it like a booking run.
            if _other_instance_pid() or not _acquire_single_instance_lock():
                log.warning("A booking run is active; cannot cancel now "
                            "(browser in use). Try again later.")
                return
            booker.run_cancel_reservation(conf)
            return
        if not _acquire_single_instance_lock():
            sys.exit(1)
        auto_enabled = config.get("auto_booking", {}).get("enabled")
        waiting_enabled = config.get("waiting_mode", {}).get("enabled")
        # Explicit verbs always win over the enabled flags, so the GUI can
        # launch either long-running mode regardless of config toggles.
        if cmd == "waiting":
            booker.run_waiting_mode()
        elif cmd == "auto" or (cmd == "default" and auto_enabled):
            booker.run_auto_booking()
        elif cmd == "default" and waiting_enabled:
            booker.run_waiting_mode()
        else:
            booker.run_booking()
    except Exception as e:
        # Nothing above may die silently on an unattended VM.
        log.error(f"Unhandled crash: {e}")
        send_crash_alert("booking run", e)
        raise


if __name__ == "__main__":
    main()