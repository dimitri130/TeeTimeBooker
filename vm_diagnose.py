#!/usr/bin/env python3
"""vm_diagnose.py -- RDP connected-vs-detached environment probe.

Standalone diagnostic for the unattended-Cloudflare failure. The booker works
when RDP stays connected and fails when it is closed; the 2026-07-12 controlled
run showed Cloudflare passing PASSIVELY (spinner only) when connected but
escalating to an INTERACTIVE "Verify you are human" checkbox when detached. The
click was never the real problem -- the browser environment changes on detach
and Cloudflare fingerprints the difference.

The observation paradox this solves: the detached state can only be observed
while NOT connected, but connecting to look changes the state. So this probe is
built to run unattended in a now-disconnected session (started with --delay, or
fired by a scheduled task after you close RDP) and ship its results out by
email, so the detached state is captured without anyone watching it.

Two rules make the capture valid, and both differ from the booker on purpose:
  1. It NEVER calls tscon to reattach the session. The booker reattaches to
     make clicks land; here that would rewrite the very state we are measuring.
     We record the raw WTS connect-state instead.
  2. It uses the booker's own init_uc_driver, so Chrome launches identically to
     production -- otherwise the fingerprint comparison would be meaningless.

It does NOT log in and does NOT book. Navigating to the booking URL is enough to
trigger Cloudflare, so the probe stays read-only against the site.

Usage (on the VM):
    # while connected -- immediate baseline:
    python vm_diagnose.py --mode connected
    # detached capture -- start it, then close RDP within the delay window:
    python vm_diagnose.py --delay 120
Each run writes diag/<state>_<ts>.json, emails it (plus a screenshot), and if a
snapshot of the opposite state already exists, emails a field-by-field diff.
"""
import argparse
import ctypes
import json
import smtplib
import sys
import time
from ctypes import wintypes
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from run_setup import (
    init_uc_driver,
    SENDER_EMAIL,
    SENDER_PASSWORD,
    SMTP_SERVER,
    SMTP_PORT,
    HEARTBEAT_CONTACTS,
)

BASE_DIR = (
    Path(sys.executable).parent if getattr(sys, "frozen", False)
    else Path(__file__).parent
)
DIAG_DIR = BASE_DIR / "diag"
CONFIG_FILE = BASE_DIR / "config.json"
LOG_FILE = BASE_DIR / "vm_diagnose.log"

# Same interstitial containers the booker keys on, so "still blocking" here
# means the same thing it does in production.
CLOUDFLARE_IDS = ["challenge-form", "challenge-running", "cf-wrapper"]

# The exact environment Cloudflare fingerprints. Kept in lockstep with the
# booker's _dump_cf_fingerprint JS so the two logs are directly comparable.
FINGERPRINT_JS = r"""
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
"""


def _log(msg: str) -> None:
    """Print and append to vm_diagnose.log. No dependency on the booker's
    logging config so this runs cleanly on its own."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} - {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def session_connect_state() -> dict:
    """The raw WTS connect-state of THIS process's session. This is the fact
    everything else is compared against -- it names which state we measured.
    Deliberately read-only: unlike the booker, it never reattaches."""
    states = {0: "active", 1: "connected", 2: "connectquery", 3: "shadow",
              4: "disconnected", 5: "idle", 6: "listen", 7: "reset",
              8: "down", 9: "init"}
    out = {"session_id": None, "state_code": None, "state": "unknown"}
    try:
        kernel32 = ctypes.windll.kernel32
        wtsapi = ctypes.windll.wtsapi32
        sid = wintypes.DWORD()
        if not kernel32.ProcessIdToSessionId(
                kernel32.GetCurrentProcessId(), ctypes.byref(sid)):
            return out
        out["session_id"] = sid.value
        buf = ctypes.c_void_p()
        size = wintypes.DWORD()
        WTS_CONNECT_STATE = 8
        if not wtsapi.WTSQuerySessionInformationW(
                None, sid, WTS_CONNECT_STATE, ctypes.byref(buf),
                ctypes.byref(size)):
            return out
        code = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
        wtsapi.WTSFreeMemory(buf)
        out["state_code"] = code
        out["state"] = states.get(code, f"code-{code}")
    except Exception as e:
        out["state"] = f"error:{e}"
    return out


def desktop_size() -> str:
    """Physical input-desktop dimensions (pyautogui), or 'n/a'. A drop here on
    detach is one of the deltas under suspicion."""
    try:
        import pyautogui
        s = pyautogui.size()
        return f"{s.width}x{s.height}"
    except Exception as e:
        return f"n/a ({e})"


def cf_blocking(driver) -> bool:
    """True while a Cloudflare interstitial is up, by the booker's own rule."""
    try:
        if "just a moment" in (driver.title or "").lower():
            return True
        css = ", ".join(f"#{cf_id}" for cf_id in CLOUDFLARE_IDS)
        from selenium.webdriver.common.by import By
        for el in driver.find_elements(By.CSS_SELECTOR, css):
            if el.is_displayed():
                return True
    except Exception:
        pass
    return False


def challenge_state(driver) -> str:
    """'interactive' if a Verify-you-are-human checkbox/iframe is present,
    'passive' if only the spinner, 'unknown' otherwise. The crux distinction."""
    try:
        return driver.execute_script("""
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


def capture_fingerprint(driver) -> dict:
    """The full fingerprint plus a timer-throttle probe. A detached/background
    session may throttle timers, stalling the challenge's own JS."""
    fp = driver.execute_script(FINGERPRINT_JS)
    if not isinstance(fp, dict):
        fp = {"error": "fingerprint script returned non-dict"}
    try:
        drift = driver.execute_async_script("""
            var cb = arguments[0], t0 = performance.now();
            setTimeout(function () { cb(performance.now() - t0); }, 100);
        """)
        fp["timer_100ms_actual"] = round(float(drift))
    except Exception as e:
        fp["timer_100ms_actual"] = f"err:{e}"
    return fp


def take_screenshot(tag: str):
    """Save a real desktop screenshot; return (path, luma_lo, luma_hi) or None.
    A pure-black frame means the session has no rendered desktop."""
    try:
        import pyautogui
        DIAG_DIR.mkdir(exist_ok=True)
        path = DIAG_DIR / f"shot_{tag}_{datetime.now():%H%M%S}.png"
        img = pyautogui.screenshot()
        img.save(str(path))
        lo, hi = img.convert("L").getextrema()
        return str(path), lo, hi
    except Exception as e:
        _log(f"screenshot FAILED ({e}) -- session likely has no usable desktop.")
        return None


def read_booking_url() -> str:
    """booking_url from config.json. It is plaintext (not an ENC: value), so we
    read it directly and never touch credentials -- the probe needs no login."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        url = cfg.get("booking_url")
        if url:
            return url
    except Exception as e:
        _log(f"Could not read booking_url from config.json: {e}")
    return ""


def diff_snapshots(old: dict, new: dict) -> dict:
    """Field-by-field delta of the two fingerprints plus the session facts.
    This is the payoff: it names exactly what changed between states."""
    changed = {}
    keys = set(old.keys()) | set(new.keys())
    for k in sorted(keys):
        a, b = old.get(k), new.get(k)
        if a != b:
            changed[k] = {"from": a, "to": b}
    return changed


def _flatten(snap: dict) -> dict:
    """Merge the session/desktop facts and the fingerprint into one flat dict
    for diffing."""
    flat = {
        "state": snap.get("session", {}).get("state"),
        "desktop": snap.get("desktop"),
        "challenge_state": snap.get("challenge_state"),
        "cleared_after_s": snap.get("cleared_after_s"),
        "screenshot_luma": snap.get("screenshot_luma"),
    }
    flat.update(snap.get("fingerprint", {}))
    return flat


def send_diag_email(subject: str, body: str, attachments: list) -> bool:
    """Email the snapshot out. Survives RDP disconnect (SMTP, no desktop
    needed), which is why it is the return channel for the detached capture.
    Attaches PNG screenshots inline as images."""
    if not HEARTBEAT_CONTACTS:
        _log("No recipients; skipping email (results still saved to diag/).")
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = SENDER_EMAIL
        msg["To"] = ", ".join(HEARTBEAT_CONTACTS)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        for path in attachments:
            try:
                with open(path, "rb") as f:
                    img = MIMEImage(f.read())
                    img.add_header("Content-Disposition", "attachment",
                                   filename=Path(path).name)
                    msg.attach(img)
            except Exception as e:
                _log(f"Could not attach {path}: {e}")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SENDER_EMAIL, SENDER_PASSWORD)
            s.send_message(msg)
        _log(f"Emailed results to {', '.join(HEARTBEAT_CONTACTS)}.")
        return True
    except Exception as e:
        _log(f"Email failed ({e}) -- results are still saved under diag/.")
        return False


def run_diagnostic(mode: str, delay: int, wait_clear: int) -> None:
    DIAG_DIR.mkdir(exist_ok=True)

    if delay > 0:
        _log(f"Waiting {delay}s before capture -- disconnect RDP now to "
             f"measure the detached state.")
        time.sleep(delay)

    sess = session_connect_state()
    # mode 'auto' trusts the live WTS state to label the snapshot, so a
    # mislabel is impossible; an explicit mode is only a naming override.
    label = mode if mode != "auto" else sess["state"]
    _log(f"Capturing (mode={mode}, measured session state={sess['state']}, "
         f"session_id={sess['session_id']}).")

    desktop = desktop_size()
    _log(f"Desktop (physical input) size: {desktop}")

    url = read_booking_url()
    if not url:
        _log("No booking_url; cannot trigger Cloudflare. Aborting.")
        return

    driver = None
    snap = {
        "captured_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "mode": mode,
        "session": sess,
        "desktop": desktop,
    }
    try:
        _log("Launching Chrome via the booker's init_uc_driver (production "
             "parity)...")
        driver = init_uc_driver(log_fn=_log)
        try:
            driver.maximize_window()
        except Exception:
            pass
        _log(f"Navigating to {url} ...")
        driver.get(url)
        time.sleep(3)  # let the interstitial render

        snap["fingerprint"] = capture_fingerprint(driver)
        _log(f"FINGERPRINT: {snap['fingerprint']}")

        state = challenge_state(driver)
        blocking = cf_blocking(driver)
        snap["challenge_state"] = state if blocking else "no-challenge"
        _log(f"Cloudflare blocking={blocking}, challenge_state={state}")

        # Watch whether it clears on its own (the passive pass) within the
        # window, WITHOUT clicking or reattaching -- the true unattended outcome.
        cleared_after = None
        start = time.time()
        while blocking and time.time() - start < wait_clear:
            time.sleep(1.0)
            if not cf_blocking(driver):
                cleared_after = round(time.time() - start, 1)
                break
        if blocking:
            snap["cleared_after_s"] = cleared_after
            if cleared_after is not None:
                _log(f"Cloudflare cleared PASSIVELY after {cleared_after}s "
                     f"(no click, no reattach).")
            else:
                _log(f"Cloudflare did NOT clear within {wait_clear}s "
                     f"(state={challenge_state(driver)}). This is the "
                     f"unattended failure, captured raw.")
        else:
            snap["cleared_after_s"] = 0.0

        shot = take_screenshot(label)
        if shot:
            path, lo, hi = shot
            snap["screenshot"] = Path(path).name
            snap["screenshot_luma"] = f"{lo}-{hi}"
            if hi == 0:
                _log("Screenshot is PURE BLACK -- no rendered desktop.")
            _log(f"Screenshot saved {Path(path).name} (luma {lo}-{hi}).")
    except Exception as e:
        snap["error"] = str(e)
        _log(f"Capture error: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    # Persist the snapshot.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_path = DIAG_DIR / f"{label}_{ts}.json"
    snap_path.write_text(json.dumps(snap, indent=2, default=str),
                         encoding="utf-8")
    _log(f"Snapshot saved: {snap_path.name}")

    # If a snapshot of the OTHER state exists, compute and include the diff.
    diff_text = ""
    try:
        others = sorted(
            (p for p in DIAG_DIR.glob("*.json")
             if not p.name.startswith(label + "_")),
            key=lambda p: p.stat().st_mtime, reverse=True)
        if others:
            other = json.loads(others[0].read_text(encoding="utf-8"))
            other_label = other.get("mode")
            if other_label == "auto":
                other_label = other.get("session", {}).get("state")
            delta = diff_snapshots(_flatten(other), _flatten(snap))
            diff_text = (
                f"\n\nDIFF vs {others[0].name} "
                f"({other_label} -> {label}):\n"
                + json.dumps(delta, indent=2, default=str))
            _log(f"Diff vs {others[0].name}: {delta}")
    except Exception as e:
        _log(f"Diff computation failed: {e}")

    body = (f"VM diagnostic capture\n"
            f"state: {label} (measured {sess['state']})\n"
            f"captured_at: {snap['captured_at']}\n\n"
            + json.dumps(snap, indent=2, default=str)
            + diff_text)
    attachments = [str(DIAG_DIR / snap["screenshot"])] if snap.get("screenshot") else []
    send_diag_email(
        f"TTB-DIAG [{label}] challenge={snap.get('challenge_state')} "
        f"cleared={snap.get('cleared_after_s')}",
        body, attachments)
    _log("Done.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="RDP connected-vs-detached Cloudflare environment probe.")
    ap.add_argument("--mode", choices=["auto", "connected", "detached"],
                    default="auto",
                    help="Snapshot label. 'auto' (default) uses the live WTS "
                         "state, so it cannot be mislabeled.")
    ap.add_argument("--delay", type=int, default=0,
                    help="Seconds to wait before capturing, so you can close "
                         "RDP first to measure the detached state.")
    ap.add_argument("--wait-clear", type=int, default=25,
                    help="Seconds to watch for Cloudflare to clear on its own.")
    args = ap.parse_args()
    _log("=" * 60)
    run_diagnostic(args.mode, args.delay, args.wait_clear)


if __name__ == "__main__":
    main()
