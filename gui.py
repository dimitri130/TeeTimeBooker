"""
Timmy's Tee Time Booker GUI — entry point for both interactive (GUI) and CLI dispatch modes.

When run directly:          py gui.py                       -> launches the GUI
When run with a mode arg:   py gui.py single                -> routes to TimmysTeeTimeBooker.main()
When frozen (PyInstaller):  TimmysTeeTimeBooker.exe         -> launches the GUI
                            TimmysTeeTimeBooker.exe single  -> routes to booking CLI
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

# Polygon rendering is ~20x cheaper to repaint than the default font-glyph
# shapes, which matters with this many canvases; guarded so a customtkinter
# internals change can never break startup.
try:
    from customtkinter.windows.widgets.core_rendering import DrawEngine
    DrawEngine.preferred_drawing_method = "polygon_shapes"
except Exception:
    pass

# ---------------------------------------------------------------------------
# Path resolution: frozen exe uses sys.executable's parent so that
# config.json and .secret.key are created next to the exe, not in a temp dir.
# ---------------------------------------------------------------------------
BASE_DIR = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).parent
)

from run_setup import (
    CONFIG_FILE,
    DEFAULT_CONFIG,
    SENSITIVE_FIELDS,
    SENDER_EMAIL,
    HEARTBEAT_CONTACTS,
    validate_config,
    prepare_config,
    _get_nested,
    _set_nested,
    ConfigError,
    CalibrationRequired,
)
import crypto_utils

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

MAX_EXTRA_PLAYERS = 3  # additional player slots (players 2–4)

# Waiting-for-opening mode ships gated off until its first live verification
# run (session lifetime, in-session slot visibility). All the code stays; the
# GUI just refuses to launch it. Flip to True to re-enable.
WAITING_ENABLED = False

# Every Settings entry field by dotted config path. The tk Vars for these are
# created eagerly in __init__ (cheap, no canvases) so _load_config/_save_config
# always see the full set; the sub-tab widgets bind to them lazily on first view.
_SETTINGS_ENTRY_KEYS = (
    "username",
    "password",
    "earlier_tolerance_minutes",
    "later_tolerance_minutes",
    "auto_booking.release_time",
    "auto_booking.prep_lead_minutes",
    "auto_booking.strike_max_minutes",
    "auto_booking.strike_poll_seconds",
    "auto_booking.widen_after_seconds",
    "auto_booking.check_interval_seconds",
    "auto_booking.max_attempts",
    "auto_booking.open_days_before",
    "auto_booking.probe_attempts",
    "waiting_mode.reconnect_min_minutes",
    "waiting_mode.reconnect_max_minutes",
    "waiting_mode.reconnect_jitter_seconds",
    "waiting_mode.in_session_poll_seconds",
    "waiting_mode.session_max_minutes",
    "waiting_mode.fee_period_hours",
    "reservations_cache_max_age_hours",
    "booking_url",
)

# Checkbox fields by dotted config path (same eager-var scheme as above).
_SETTINGS_BOOL_KEYS = (
    "waiting_mode.auto_take",
    "waiting_mode.continuous_upgrade",
    "waiting_mode.cancel_existing_on_upgrade",
    "waiting_mode.continue_into_fee_period",
    "waiting_mode.fee_period_auto_swap",
    "email_notifications.local_popup",
)

_LEVEL_COLORS: dict[str, str] = {
    "ERROR":   "#ff5555",
    "WARNING": "#ffcc00",
    "DEBUG":   "#888888",
    "INFO":    "#d0d0d0",
}


def _line_level(line: str) -> str:
    for lvl in ("ERROR", "WARNING", "DEBUG", "INFO"):
        if f" - {lvl} - " in line or line.startswith(lvl + " "):
            return lvl
    return "INFO"


# ===========================================================================
# Main application window
# ===========================================================================

class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Timmy's Tee Time Booker")
        self.geometry("640x860")
        self.minsize(600, 700)

        self._config: dict = {}
        self._config_mtime: float | None = None
        self._raw_enc: dict[str, str] = {}   # original ENC:... strings per field path
        self._proc: subprocess.Popen | None = None
        self._last_run_mode: str = ""        # mode of the most recent subprocess
        self._res_cancel_buttons: list[ctk.CTkButton] = []
        self._res_rendered_fetched_at: object = None  # cache stamp at last render

        # Subprocess stdout is buffered by the reader thread and drained by a
        # single after() flush loop (one insert batch per tick), instead of one
        # after(0) callback per line.
        self._out_lock = threading.Lock()
        self._out_buf: list[str] = []
        self._out_rc: int | None = None      # reader-thread exit code when done

        # Booking tab widget references
        self._bvars: dict[str, tk.StringVar] = {}
        self._num_cb:  ctk.CTkComboBox | None = None
        self._pref_cb: ctk.CTkComboBox | None = None
        self._testing_var = tk.BooleanVar(value=False)
        self._work_card_var = tk.BooleanVar(value=False)
        self._step_var = tk.BooleanVar(value=False)
        self._step_active = False   # True while a step-through run is in progress

        # Additional player rows (label + name/email entry pair per slot)
        self._player_labels: list[ctk.CTkLabel] = []
        self._player_entries: list[tuple[ctk.CTkEntry, ctk.CTkEntry]] = []
        self._player_name_vars:  list[tk.StringVar] = []
        self._player_email_vars: list[tk.StringVar] = []

        # Settings sub-tab widget vars, created eagerly so load/save work
        # before the lazily-built sub-tabs exist.
        self._adv_vars: dict[str, tk.StringVar] = {
            key: tk.StringVar() for key in _SETTINGS_ENTRY_KEYS}
        self._auto_enabled_var  = tk.BooleanVar(value=False)
        self._email_enabled_var = tk.BooleanVar(value=False)
        self._dumps_var         = tk.BooleanVar(value=False)
        self._pw_show_var       = tk.BooleanVar(value=False)
        # Checkbox vars keyed by dotted config path (waiting_mode booleans,
        # local_popup); load/save iterate this dict.
        self._bool_vars: dict[str, tk.BooleanVar] = {
            path: tk.BooleanVar(value=False) for path in _SETTINGS_BOOL_KEYS}

        self._build_ui()
        self._load_config(show_log_tail=True)
        self.after(1500, self._poll_config)
        # Closing the window must not orphan a running booking subprocess:
        # an orphan keeps running overnight and collides with the next launch
        # (shared chromedriver patch + the one mouse cursor Cloudflare needs).
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Three font tiers shared across all tabs: section headers, body
        # (theme default), and gray hints/metadata.
        self._font_section = ctk.CTkFont(size=13, weight="bold")
        self._font_hint = ctk.CTkFont(size=11)

        # Root grid: tabs and output are the two vertical stretch regions
        # (3:2 split), the action bar keeps a fixed weight-0 strip at the
        # bottom so the buttons are never clipped when the window shrinks.
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=3, minsize=380)
        self.grid_rowconfigure(1, weight=2, minsize=160)
        self.grid_rowconfigure(2, weight=0)

        self.tab_view = ctk.CTkTabview(self, command=self._on_main_tab_change)
        self.tab_view.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 5))
        self.tab_view.add("Booking")
        self.tab_view.add("Settings")
        self.tab_view.add("Reservations")

        self._build_booking_tab(self.tab_view.tab("Booking"))
        self._build_settings_tab(self.tab_view.tab("Settings"))
        self._build_reservations_tab(self.tab_view.tab("Reservations"))
        self._build_output_panel()
        self._build_action_bar()

    def _build_booking_tab(self, tab: ctk.CTkFrame) -> None:
        self._booking_tab = tab
        # Three columns: labels, then two equal columns that the player
        # name/email entries grid into directly (no wrapper frame per row).
        # Single-widget rows sit in column 1 with a fixed width, so the
        # extra column doesn't move them.
        tab.grid_columnconfigure(0, minsize=180, weight=0)
        tab.grid_columnconfigure((1, 2), weight=1, uniform="pcols")

        r = 0
        for label, key, width in [
            ("Booking Date (YYYY-MM-DD):", "booking_date", 120),
            ("Desired Time (HH:MM):",      "desired_time", 80),
            ("Earliest Time (HH:MM):",     "earliest_acceptable_time", 80),
            ("Latest Time (HH:MM):",       "latest_acceptable_time", 80),
        ]:
            ctk.CTkLabel(tab, text=label, anchor="w").grid(
                row=r, column=0, sticky="w", padx=(10, 5), pady=4)
            var = tk.StringVar()
            self._bvars[key] = var
            ctk.CTkEntry(tab, textvariable=var, width=width,
                         corner_radius=0).grid(
                row=r, column=1, sticky="w", padx=(0, 10), pady=4)
            r += 1

        # Num players
        ctk.CTkLabel(tab, text="Num Players:", anchor="w").grid(
            row=r, column=0, sticky="w", padx=(10, 5), pady=4)
        self._num_cb = ctk.CTkComboBox(
            tab, values=["1", "2", "3", "4"], width=80, state="readonly",
            command=self._on_num_players_change)
        self._num_cb.set("2")
        self._num_cb.grid(row=r, column=1, sticky="w", padx=(0, 10), pady=4)
        r += 1

        # Payment toggle: click to switch between the Work and Play
        # cards. _work_card_var stays the source of truth (True = Work).
        # Deliberately kept on the main tab and separate from Run Mode so the
        # card in use is always visible before a run.
        ctk.CTkLabel(tab, text="Payment:", anchor="w").grid(
            row=r, column=0, sticky="w", padx=(10, 5), pady=4)
        self._mode_btn = ctk.CTkButton(tab, width=90, command=self._toggle_mode)
        self._mode_btn.grid(row=r, column=1, sticky="w", padx=(0, 10), pady=4)
        self._refresh_mode_btn()
        r += 1

        # Run mode: how the Run button launches the booker. "standard" keeps
        # the existing auto/single routing; "waiting" launches the
        # waiting-for-opening hunter (its settings live in Settings >
        # Automation). Plain unpersisted selector: nothing is written to
        # config.json.
        ctk.CTkLabel(tab, text="Run Mode:", anchor="w").grid(
            row=r, column=0, sticky="w", padx=(10, 5), pady=4)
        self._run_mode_cb = ctk.CTkComboBox(
            tab, values=["standard", "waiting"], width=120, state="readonly")
        self._run_mode_cb.set("standard")
        self._run_mode_cb.grid(row=r, column=1, sticky="w", padx=(0, 10), pady=4)
        r += 1

        # Additional players section header
        ctk.CTkLabel(
            tab, text="Additional Players:",
            font=self._font_section, anchor="w",
        ).grid(row=r, column=0, columnspan=3, sticky="w", padx=(10, 5), pady=(16, 4))
        r += 1

        ctk.CTkLabel(tab, text="Name (LastName, FirstName)",
                     font=self._font_hint, text_color="gray60",
                     anchor="w").grid(
            row=r, column=1, sticky="w")
        ctk.CTkLabel(tab, text="Email", font=self._font_hint,
                     text_color="gray60", anchor="w").grid(
            row=r, column=2, sticky="w", padx=(4, 0))
        r += 1

        # Pre-create all player rows; show/hide based on num_players.
        # Entries grid straight into the tab (no per-row wrapper frame).
        for i in range(MAX_EXTRA_PLAYERS):
            name_var  = tk.StringVar()
            email_var = tk.StringVar()
            self._player_name_vars.append(name_var)
            self._player_email_vars.append(email_var)

            lbl = ctk.CTkLabel(tab, text=f"Player {i + 2}:", anchor="w")
            lbl.grid(row=r, column=0, sticky="w", padx=(10, 5), pady=2)
            self._player_labels.append(lbl)

            name_entry = ctk.CTkEntry(tab, textvariable=name_var,
                                      placeholder_text="Doe, John",
                                      corner_radius=0)
            name_entry.grid(row=r, column=1, sticky="ew", padx=(0, 4), pady=2)
            email_entry = ctk.CTkEntry(tab, textvariable=email_var,
                                       placeholder_text="john@example.com",
                                       corner_radius=0)
            email_entry.grid(row=r, column=2, sticky="ew", padx=(4, 10), pady=2)
            self._player_entries.append((name_entry, email_entry))
            r += 1

        # Spacer row: keeps content top-anchored on tall windows.
        tab.grid_rowconfigure(r, weight=1)

        self._update_player_slots(2)

    # Sub-tabs whose content overflows the 380px tabview minimum get a
    # CTkScrollableFrame; one that fits would use a plain CTkFrame (a scroll
    # canvas costs extra repaints on every resize). Measured 2026-07-13: every
    # sub-tab overflows at the minimum (Advanced, the shortest, needs ~378
    # logical px vs ~285 available), so all four keep the scrollframe.
    _SUBTAB_SCROLL = {
        "Defaults": True,
        "Automation": True,
        "Notifications": True,
        "Advanced": True,
    }

    def _build_settings_tab(self, tab: ctk.CTkFrame) -> None:
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        # Sub-tabs are created empty and built lazily on first selection
        # (each builder creates dozens of canvases; deferring them cuts
        # startup time roughly in half). All tk Vars already exist eagerly,
        # so load/save never depend on a sub-tab being built.
        sub = ctk.CTkTabview(tab, command=self._on_settings_subtab)
        sub.grid(row=0, column=0, sticky="nsew")
        self._subtab_builders = {
            "Defaults": self._build_defaults_subtab,
            "Automation": self._build_automation_subtab,
            "Notifications": self._build_notifications_subtab,
            "Advanced": self._build_advanced_subtab,
        }
        self._built_subtabs: set[str] = set()
        for name in self._subtab_builders:
            sub.add(name)
            frame = sub.tab(name)
            frame.grid_rowconfigure(0, weight=1)
            frame.grid_columnconfigure(0, weight=1)
        self._settings_tabs = sub
        # The default-visible sub-tab is built now so Settings never shows empty.
        self._ensure_subtab_built(sub.get())

    def _on_settings_subtab(self) -> None:
        self._ensure_subtab_built(self._settings_tabs.get())

    def _ensure_subtab_built(self, name: str) -> None:
        if name in self._built_subtabs or name not in self._subtab_builders:
            return
        self._built_subtabs.add(name)
        frame = self._settings_tabs.tab(name)
        if self._SUBTAB_SCROLL[name]:
            # height is only the initial canvas height; the weighted row
            # stretches it, and it doubles as a sane floor at minsize.
            parent = ctk.CTkScrollableFrame(frame, height=200)
        else:
            parent = ctk.CTkFrame(frame, fg_color="transparent")
        parent.grid(row=0, column=0, sticky="nsew")
        parent.grid_columnconfigure(0, minsize=180, weight=0)
        parent.grid_columnconfigure(1, weight=1)
        self._subtab_builders[name](parent)

    # Shared row builders for the Settings sub-tabs. Each takes the target
    # grid row and returns the next free row, so the builders thread a
    # simple row counter through them.

    def _section_row(self, parent, r: int, text: str) -> int:
        ctk.CTkLabel(parent, text=text, font=self._font_section,
                     anchor="w").grid(
            row=r, column=0, columnspan=2, sticky="w", padx=(10, 5),
            pady=(6, 4) if r == 0 else (16, 4))
        return r + 1

    def _entry_row(self, parent, r: int, label: str, key: str,
                   masked: bool = False, width: int = 80,
                   wide: bool = False) -> tuple[int, ctk.CTkEntry]:
        ctk.CTkLabel(parent, text=label, anchor="w").grid(
            row=r, column=0, sticky="w", padx=(10, 5), pady=4)
        var = self._adv_vars.setdefault(key, tk.StringVar())
        entry = ctk.CTkEntry(parent, textvariable=var, width=width,
                             corner_radius=0, show="*" if masked else "")
        entry.grid(row=r, column=1, sticky="ew" if wide else "w",
                   padx=(0, 10), pady=4)
        return r + 1, entry

    def _check_row(self, parent, r: int, label: str, var: tk.BooleanVar,
                   text: str = "", command=None,
                   indent: bool = False) -> tuple[int, ctk.CTkCheckBox]:
        ctk.CTkLabel(parent, text=label, anchor="w").grid(
            row=r, column=0, sticky="w",
            padx=(30, 5) if indent else (10, 5), pady=4)
        check = ctk.CTkCheckBox(parent, text=text, variable=var,
                                corner_radius=0, command=command)
        check.grid(row=r, column=1, sticky="w", padx=(0, 10), pady=4)
        return r + 1, check

    def _hint_row(self, parent, r: int, text: str) -> int:
        ctk.CTkLabel(parent, text=text, font=self._font_hint,
                     text_color="gray60", anchor="w", justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", padx=(10, 5),
            pady=(0, 4))
        return r + 1

    def _bool_var(self, path: str) -> tk.BooleanVar:
        var = self._bool_vars.get(path)
        if var is None:
            var = tk.BooleanVar(value=False)
            self._bool_vars[path] = var
        return var

    def _build_defaults_subtab(self, parent) -> None:
        r = self._section_row(parent, 0, "Credentials")
        r, _ = self._entry_row(parent, r, "Username:", "username", width=240)
        r, pw_entry = self._entry_row(parent, r, "Password:", "password",
                                      masked=True, width=240)

        # Show/hide toggle for the password field.
        def _toggle_pw() -> None:
            pw_entry.configure(show="" if self._pw_show_var.get() else "*")

        ctk.CTkCheckBox(parent, text="Show password", corner_radius=0,
                        variable=self._pw_show_var, command=_toggle_pw).grid(
            row=r, column=1, sticky="w", padx=(0, 10), pady=(0, 4))
        r += 1
        r = self._hint_row(parent, r, "Stored encrypted in config.json.")

        r = self._section_row(parent, r, "Booking Defaults")
        ctk.CTkLabel(parent, text="Preference If Equal:", anchor="w").grid(
            row=r, column=0, sticky="w", padx=(10, 5), pady=4)
        self._pref_cb = ctk.CTkComboBox(parent, values=["earlier", "later"],
                                        width=120, state="readonly")
        self._pref_cb.set("earlier")
        self._pref_cb.grid(row=r, column=1, sticky="w", padx=(0, 10), pady=4)
        r += 1
        r, _ = self._entry_row(parent, r, "Earlier Tolerance (min):",
                               "earlier_tolerance_minutes")
        r, _ = self._entry_row(parent, r, "Later Tolerance (min):",
                               "later_tolerance_minutes")
        # Card identity is display-only; the Payment toggle on the Booking
        # tab picks which one is charged. Text filled in by _load_config.
        self._card_label = ctk.CTkLabel(parent, text="",
                                        font=self._font_hint,
                                        text_color="gray60", anchor="w")
        self._card_label.grid(row=r, column=0, columnspan=2, sticky="w",
                              padx=(10, 5), pady=(0, 4))
        r += 1

        r = self._section_row(parent, r, "Auto-Booking")
        r, _ = self._check_row(parent, r, "Enabled:", self._auto_enabled_var,
                               text="Run auto-booking at release time")
        r, _ = self._entry_row(parent, r, "Release Time (HH:MM):",
                               "auto_booking.release_time")
        r, _ = self._entry_row(parent, r, "Prep Lead (min):",
                               "auto_booking.prep_lead_minutes")
        r, _ = self._entry_row(parent, r, "Strike Max (min):",
                               "auto_booking.strike_max_minutes")
        r, _ = self._entry_row(parent, r, "Strike Poll (sec):",
                               "auto_booking.strike_poll_seconds")
        r, _ = self._entry_row(parent, r, "Widen Window After (sec):",
                               "auto_booking.widen_after_seconds")
        r, _ = self._entry_row(parent, r, "Fallback Interval (sec):",
                               "auto_booking.check_interval_seconds")
        r, _ = self._entry_row(parent, r, "Fallback Max Attempts:",
                               "auto_booking.max_attempts")
        r, _ = self._entry_row(parent, r, "Open Days Before:",
                               "auto_booking.open_days_before")
        r, _ = self._entry_row(parent, r, "Probe Attempts:",
                               "auto_booking.probe_attempts")

    def _build_automation_subtab(self, parent) -> None:
        # Waiting-mode runs are launched via the Booking tab's Run Mode
        # combobox; waiting_mode.enabled stays a config-file-only default-run
        # flag with no GUI control.
        r = self._section_row(parent, 0, "Waiting for Opening")
        r, _ = self._entry_row(parent, r, "Reconnect Min (min):",
                               "waiting_mode.reconnect_min_minutes")
        r, _ = self._entry_row(parent, r, "Reconnect Max (min):",
                               "waiting_mode.reconnect_max_minutes")
        r, _ = self._entry_row(parent, r, "Reconnect Jitter (sec):",
                               "waiting_mode.reconnect_jitter_seconds")
        r, _ = self._entry_row(parent, r, "In-Session Poll (sec):",
                               "waiting_mode.in_session_poll_seconds")
        r, _ = self._entry_row(parent, r, "Session Max (min):",
                               "waiting_mode.session_max_minutes")
        r = self._hint_row(parent, r,
                           "Reconnects every min-max minutes +/- jitter. "
                           "Session Max 0 = uncapped.")

        r, _ = self._check_row(
            parent, r, "Auto Take:",
            self._bool_var("waiting_mode.auto_take"),
            text="Book automatically when a matching slot opens")
        r, _ = self._check_row(
            parent, r, "Continuous Upgrade:",
            self._bool_var("waiting_mode.continuous_upgrade"),
            text="Keep watching for a better time after booking",
            command=self._update_waiting_gates)
        r, self._cancel_upgrade_check = self._check_row(
            parent, r, "Cancel On Upgrade:",
            self._bool_var("waiting_mode.cancel_existing_on_upgrade"),
            text="Cancel the old reservation when upgrading", indent=True)

        r, _ = self._entry_row(parent, r, "Fee Period (hours):",
                               "waiting_mode.fee_period_hours")
        r, _ = self._check_row(
            parent, r, "Into Fee Period:",
            self._bool_var("waiting_mode.continue_into_fee_period"),
            text="Keep hunting inside the cancellation-fee window",
            command=self._update_waiting_gates)
        r, self._fee_swap_check = self._check_row(
            parent, r, "Fee Auto Swap:",
            self._bool_var("waiting_mode.fee_period_auto_swap"),
            text="Allow fee-incurring swaps inside that window", indent=True)
        # Lazily built: vars were loaded before the checkboxes existed, so
        # apply the dependent-checkbox gating now.
        self._update_waiting_gates()

    def _update_waiting_gates(self) -> None:
        """Dependent checkboxes stay visible but disabled while their parent
        is off (Step-through is the one hide-based exception)."""
        if not hasattr(self, "_fee_swap_check"):
            return  # Automation sub-tab not built yet
        self._cancel_upgrade_check.configure(
            state="normal"
            if self._bool_vars["waiting_mode.continuous_upgrade"].get()
            else "disabled")
        self._fee_swap_check.configure(
            state="normal"
            if self._bool_vars["waiting_mode.continue_into_fee_period"].get()
            else "disabled")

    def _build_notifications_subtab(self, parent) -> None:
        r = self._section_row(parent, 0, "Notify Other Players")
        r, _ = self._check_row(parent, r, "Enabled:", self._email_enabled_var,
                               text="Email booking confirmations to players")
        # Recipients come from the per-player email fields on the Booking tab.
        # The sender address and password are fixed in code, not editable here.
        r = self._hint_row(parent, r,
                           "Recipients: player emails from the Booking tab.")

        r = self._section_row(parent, r, "Failure Contacts")
        self._failure_tb = ctk.CTkTextbox(parent, height=64)
        self._failure_tb.grid(row=r, column=0, columnspan=2, sticky="ew",
                              padx=(10, 10), pady=4)
        r += 1
        r = self._hint_row(parent, r,
                           "One address per line. Empty falls back to the "
                           "built-in defaults.")

        r = self._section_row(parent, r, "Heartbeat Contacts")
        self._heartbeat_tb = ctk.CTkTextbox(parent, height=64)
        self._heartbeat_tb.grid(row=r, column=0, columnspan=2, sticky="ew",
                                padx=(10, 10), pady=4)
        r += 1
        r = self._hint_row(parent, r,
                           "One address per line. Defaults to Dimitri's "
                           "addresses; empty falls back to the built-ins ("
                           + ", ".join(HEARTBEAT_CONTACTS) + ").")

        r, _ = self._check_row(
            parent, r, "Local Popup:",
            self._bool_var("email_notifications.local_popup"),
            text="Show a local popup when an alert email is sent")
        self._hint_row(parent, r, f"Sent from: {SENDER_EMAIL}")
        # Lazily built: the config loaded before these textboxes existed.
        self._populate_contact_textboxes()

    def _populate_contact_textboxes(self) -> None:
        """Contact textboxes have no textvariable; repopulate them explicitly
        from the loaded config (also covers the _poll_config reload path)."""
        if not hasattr(self, "_failure_tb"):
            return  # Notifications sub-tab not built yet
        for tb, key in ((self._failure_tb,
                         "email_notifications.failure_contacts"),
                        (self._heartbeat_tb,
                         "email_notifications.heartbeat_contacts")):
            items = _get_nested(self._config, key) or []
            tb.delete("1.0", "end")
            tb.insert("1.0", "\n".join(str(i) for i in items))

    def _build_advanced_subtab(self, parent) -> None:
        r = self._section_row(parent, 0, "Testing")
        r, _ = self._check_row(parent, r, "Testing Mode:", self._testing_var,
                               text="(stops before confirming booking)",
                               command=self._on_testing_toggle)
        # Step-through (test mode only): pause after each stage, advance with
        # Next. Keeps its historical hide (not disable) gating because it also
        # swaps the action bar into Step/End form.
        self._step_label = ctk.CTkLabel(parent, text="Step Through:",
                                        anchor="w")
        self._step_label.grid(row=r, column=0, sticky="w", padx=(30, 5),
                              pady=4)
        self._step_check = ctk.CTkCheckBox(
            parent, text="(pause after each stage)", corner_radius=0,
            variable=self._step_var)
        self._step_check.grid(row=r, column=1, sticky="w", padx=(0, 10),
                              pady=4)
        r += 1

        r = self._section_row(parent, r, "Diagnostics")
        r, _ = self._check_row(parent, r, "Capture Page Dumps:",
                               self._dumps_var,
                               text="(save DOM snapshots to dumps/)")
        r, _ = self._entry_row(parent, r, "Reservations Cache Age (hours):",
                               "reservations_cache_max_age_hours")

        r = self._section_row(parent, r, "Site")
        r, _ = self._entry_row(parent, r, "Booking URL:", "booking_url",
                               wide=True)
        # Calibration values are written by the Calibrate flow, never typed.
        self._calibration_label = ctk.CTkLabel(parent, text="",
                                               font=self._font_hint,
                                               text_color="gray60",
                                               anchor="w")
        self._calibration_label.grid(row=r, column=0, columnspan=2,
                                     sticky="w", padx=(10, 5), pady=(4, 0))
        r += 1
        self._hint_row(parent, r,
                       "Use the Calibrate Cloudflare button below to update.")
        # Lazily built: the config loaded before these widgets existed.
        self._populate_calibration_label()
        self._update_step_visibility()

    def _populate_calibration_label(self) -> None:
        """Calibration status line from the loaded config (read-only, written
        by the Calibrate flow; also covers the _poll_config reload path)."""
        if not hasattr(self, "_calibration_label"):
            return  # Advanced sub-tab not built yet
        cfg = self._config
        if _get_nested(cfg, "cloudflare_checkbox.calibrated"):
            cx = _get_nested(cfg, "cloudflare_checkbox.relative_x")
            cy = _get_nested(cfg, "cloudflare_checkbox.relative_y")
            try:
                cal_text = f"Calibrated: yes ({float(cx):.4f}, {float(cy):.4f})"
            except (TypeError, ValueError):
                cal_text = "Calibrated: yes"
        else:
            cal_text = "Calibrated: no"
        self._calibration_label.configure(text=cal_text)

    # -----------------------------------------------------------------------
    # Reservations tab
    # -----------------------------------------------------------------------

    def _build_reservations_tab(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(tab, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(4, 2))
        self._res_refresh_btn = ctk.CTkButton(
            header, text="Refresh", width=90, command=self._refresh_reservations)
        self._res_refresh_btn.pack(side="left", padx=4)
        ctk.CTkLabel(header, text="(full site login)", font=self._font_hint,
                     text_color="gray60").pack(side="left", padx=(0, 4))
        self._res_asof_label = ctk.CTkLabel(header, text="",
                                            font=self._font_hint,
                                            text_color="gray60", anchor="e")
        self._res_asof_label.pack(side="right", padx=6)

        self._res_scroll = ctk.CTkScrollableFrame(tab, height=200)
        self._res_scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        # Info labels wrap to the panel width so a narrow window wraps the text
        # instead of clipping it; the resize bind keeps the wrap in sync.
        self._res_info_labels: list = []
        self._res_scroll.bind("<Configure>", self._on_res_resize)
        self._render_reservations()

    def _on_res_resize(self, event=None) -> None:
        """Re-wrap the reservation info text to the current panel width."""
        try:
            width = self._res_scroll.winfo_width()
        except Exception:
            return
        # Leave room for the fee label + Cancel button in column 1 and padding.
        wrap = max(160, width - 170)
        for lbl in getattr(self, "_res_info_labels", []):
            try:
                lbl.configure(wraplength=wrap)
            except Exception:
                pass

    @staticmethod
    def _read_res_cache() -> dict | None:
        try:
            return json.loads(
                (BASE_DIR / "reservations_cache.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _res_parse_dt(text):
        """Parse a rendered reservation timestamp to a datetime, or None. Mirrors
        the backend's parser so the Cancel button can be disabled past the
        deadline. A parse miss returns None (button stays enabled; the backend
        and the site remain the real guards)."""
        s = " ".join(str(text or "").split())
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

    @staticmethod
    def _parse_money(text) -> float:
        try:
            return float(str(text or "").replace("$", "").replace(",", "").strip()
                         or 0)
        except ValueError:
            return 0.0

    def _effective_cancel_fee(self, entry: dict, now=None):
        """The fee that applies if the reservation is cancelled RIGHT NOW:
        $0.00 before the feeless deadline (CancellableUntil), the listed
        CancelFee after it. The raw CancelFee field is the site's late-cancel
        fee, not the current cost. An unparseable deadline falls back to the
        listed fee (conservative: never claims free when it can't tell).
        Returns (fee_text, fee_value, parsed_deadline)."""
        now = now or datetime.now()
        until = self._res_parse_dt(entry.get("CancellableUntil"))
        if until is not None and now < until:
            return "$0.00", 0.0, until
        fee_text = entry.get("CancelFee") or "$0.00"
        return fee_text, self._parse_money(fee_text), until

    def _render_reservations(self) -> None:
        """Rebuild the reservations list from reservations_cache.json."""
        for w in self._res_scroll.winfo_children():
            w.destroy()
        self._res_cancel_buttons = []
        self._res_info_labels = []

        cache = self._read_res_cache() or {}
        self._res_rendered_fetched_at = cache.get("fetched_at")
        self._res_asof_label.configure(
            text=f"as of {cache.get('fetched_at', 'never')}")
        entries = [e for e in (cache.get("entries") or []) if isinstance(e, dict)]
        if not entries:
            ctk.CTkLabel(
                self._res_scroll,
                text="No reservations cached.\nUse Refresh to fetch from the site.",
                font=self._font_hint, text_color="gray60",
                justify="center").pack(pady=20)
            return

        now = datetime.now()
        for entry in entries:
            # Label/fee/button grid directly into the card (no top/right
            # wrapper frames); same visible layout, fewer canvases.
            card = ctk.CTkFrame(self._res_scroll)
            card.pack(fill="x", padx=8, pady=6)
            card.grid_columnconfigure(0, weight=1)

            info = (f"{entry.get('ReservationTime', '?')}\n"
                    f"{entry.get('CourseName', '')}  |  "
                    f"{entry.get('Golfers', '?')} players  |  "
                    f"amount {entry.get('Amount', '?')}  |  "
                    f"cancel by {entry.get('CancellableUntil', '?')}")
            info_lbl = ctk.CTkLabel(card, text=info, justify="left", anchor="w")
            info_lbl.grid(row=0, column=0, rowspan=2, sticky="ew", padx=8, pady=6)
            self._res_info_labels.append(info_lbl)

            # The fee that applies if cancelled right now ($0.00 until the
            # feeless deadline, which is shown in the info line), centered
            # above the Cancel button.
            fee_now_text, _fee_now, _until = self._effective_cancel_fee(
                entry, now)
            ctk.CTkLabel(
                card, text=f"cancel fee now: {fee_now_text}",
                justify="center", font=self._font_hint,
                text_color="gray60").grid(row=0, column=1, padx=6, pady=(4, 0))

            # Always enabled: past the feeless deadline a cancel still works,
            # it just costs the fee, and the dialog's fee acknowledgement is
            # the gate (user decision 2026-07-13).
            btn = ctk.CTkButton(
                card, text="Cancel", width=90,
                fg_color="#cc2222", hover_color="#aa1111",
                command=lambda e=entry: self._open_cancel_dialog(e))
            btn.grid(row=1, column=1, sticky="n", padx=6, pady=(4, 4))
            self._res_cancel_buttons.append(btn)

            notified = entry.get("notified") or {}
            if notified.get("recipients"):
                ctk.CTkLabel(
                    card,
                    text="notified: " + ", ".join(notified["recipients"]),
                    font=self._font_hint, text_color="gray60",
                    justify="left").grid(row=2, column=0, columnspan=2,
                                         sticky="w", padx=8, pady=(0, 4))

        # Apply the current width now so the first paint wraps too, not just
        # after a later resize event.
        self._on_res_resize()

    def _refresh_reservations(self) -> None:
        """Cold refresh: full session (login + Cloudflare) that re-scrapes the
        site and rewrites the cache. The list reloads when the run finishes."""
        if not self._validate_before_run(require_booking=False):
            return
        self._append_output("Refreshing reservations from the site...\n", "INFO")
        self._run("reservations-refresh")

    def _open_cancel_dialog(self, entry: dict) -> None:
        """Confirmation dialog with a fee gate. When CancelFee > 0 the fee is
        called out and a second acknowledgement is required, so an incidental
        click cannot cost money."""
        cn = str(entry.get("ConfirmationNumber") or "").strip()
        # Fee that applies RIGHT NOW: $0.00 before the feeless deadline, the
        # listed CancelFee only after it (the raw field is the late fee, not
        # the current cost). The confirm dialog itself always appears; only
        # the extra fee acknowledgement is gated on a real cost.
        fee_text, fee, _until = self._effective_cancel_fee(entry)

        # No fixed geometry: the window sizes to its packed content, so long
        # course names or the fee checkbox can never be clipped.
        win = ctk.CTkToplevel(self)
        win.title("Cancel Reservation")
        win.grab_set()

        ctk.CTkLabel(win, text="Cancel this reservation?",
                     font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(16, 6))
        ctk.CTkLabel(
            win,
            text=f"{entry.get('ReservationTime', '?')}\n"
                 f"{entry.get('CourseName', '')}",
            justify="center").pack(pady=2)
        ctk.CTkLabel(
            win,
            text=f"Free cancellation until: {entry.get('CancellableUntil', '?')}"
            ).pack(pady=2)
        ctk.CTkLabel(
            win, text=f"Fee to cancel now: {fee_text}",
            text_color="#ff5555" if fee > 0 else "#d0d0d0",
            font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(8, 4))

        def do_confirm() -> None:
            win.destroy()
            self._start_cancel(cn)

        # Buttons are created as children of their row frame (packing a
        # win-parented widget into a sibling via in_= left the confirm button
        # unrendered), and the row is packed after the optional fee checkbox
        # so nothing pushes it out of the window.
        btns = ctk.CTkFrame(win, fg_color="transparent")
        ctk.CTkButton(btns, text="Keep Reservation", width=150,
                      command=win.destroy).pack(side="left", padx=6)
        confirm_btn = ctk.CTkButton(
            btns, text="Confirm Cancellation", width=180,
            fg_color="#cc2222", hover_color="#aa1111", command=do_confirm)
        confirm_btn.pack(side="left", padx=6)

        if fee > 0:
            ack_var = tk.BooleanVar(value=False)

            def on_ack() -> None:
                confirm_btn.configure(
                    state="normal" if ack_var.get() else "disabled")

            ctk.CTkCheckBox(
                win, text=f"I understand this cancellation will cost {fee_text}",
                corner_radius=0, variable=ack_var, command=on_ack).pack(pady=(6, 4))
            confirm_btn.configure(state="disabled")

        btns.pack(pady=12)

    def _start_cancel(self, confirmation_number: str) -> None:
        if not confirmation_number:
            self._append_output(
                "This reservation has no confirmation number; cannot cancel "
                "it from here. Cancel it on the website.\n", "WARNING")
            return
        if not self._validate_before_run(require_booking=False):
            return
        self._append_output(
            f"Cancelling reservation {confirmation_number}...\n", "INFO")
        self._run("cancel", extra_args=[confirmation_number])

    def _build_output_panel(self) -> None:
        frame = ctk.CTkFrame(self)
        frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)

        hdr = ctk.CTkFrame(frame, fg_color="transparent")
        hdr.pack(fill="x", padx=5, pady=(5, 0))
        ctk.CTkLabel(hdr, text="Output", font=self._font_section).pack(side="left")
        ctk.CTkButton(hdr, text="Clear", width=60, height=24,
                      command=self._clear_output).pack(side="right")

        scrollbar = tk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")

        self.output_text = tk.Text(
            frame, height=12, bg="#1a1a2e", fg="#d0d0d0",
            font=("Consolas", 11), state="disabled", wrap="word",
            yscrollcommand=scrollbar.set, relief="flat", bd=0,
        )
        self.output_text.pack(fill="both", expand=True, padx=(5, 0), pady=5)
        scrollbar.config(command=self.output_text.yview)

        for lvl, color in _LEVEL_COLORS.items():
            self.output_text.tag_config(lvl, foreground=color)

    def _build_action_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))

        # Single Save button for every tab: _save_config already writes the
        # whole form at once, so the old per-tab buttons were duplicates.
        self.btn_save = ctk.CTkButton(
            bar, text="Save Config", width=100, height=28,
            command=self._save_config)
        self.btn_save.pack(side="left", padx=4)

        self.btn_run = ctk.CTkButton(
            bar, text="Run", width=100, height=28,
            command=self._run_booking)
        self.btn_run.pack(side="left", padx=4)

        self.btn_validate = ctk.CTkButton(
            bar, text="Validate", width=100, height=28,
            command=self._validate_config_file)
        self.btn_validate.pack(side="left", padx=4)

        self.btn_calibrate = ctk.CTkButton(
            bar, text="Calibrate Cloudflare", width=100, height=28,
            command=self._calibrate)
        self.btn_calibrate.pack(side="left", padx=4)

        # Stop stays narrower than the 100px convention so the full bar plus
        # status label still fits at the 600px minsize.
        self.btn_stop = ctk.CTkButton(
            bar, text="Stop", width=60, height=28,
            fg_color="#cc2222", hover_color="#aa1111",
            command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)

        # Next: only packed/visible during a step-through run.
        self.btn_next = ctk.CTkButton(
            bar, text="Step ▶", width=100, height=28,
            fg_color="#2a8f2a", hover_color="#1f6f1f",
            command=self._advance_step)

        self.status_label = ctk.CTkLabel(bar, text="Ready", anchor="e")
        self.status_label.pack(side="right", padx=4)

    # -----------------------------------------------------------------------
    # Player slot visibility
    # -----------------------------------------------------------------------

    def _on_num_players_change(self, value: str) -> None:
        try:
            self._update_player_slots(int(value))
        except ValueError:
            pass

    def _update_player_slots(self, num_players: int) -> None:
        visible = max(0, num_players - 1)
        for i in range(MAX_EXTRA_PLAYERS):
            widgets = (self._player_labels[i], *self._player_entries[i])
            for w in widgets:
                if i < visible:
                    w.grid()
                else:
                    w.grid_remove()

    # -----------------------------------------------------------------------
    # Config load
    # -----------------------------------------------------------------------

    def _load_config(self, show_log_tail: bool = False) -> None:
        # First run: create a default config next to the program automatically.
        if not CONFIG_FILE.exists():
            try:
                CONFIG_FILE.write_text(
                    json.dumps(DEFAULT_CONFIG, indent=4), encoding="utf-8")
                self._append_output(
                    f"First run: created default config at {CONFIG_FILE}\n", "INFO")
            except Exception as exc:
                self._append_output(
                    f"Failed to create config.json: {exc}\n", "ERROR")
                return

        try:
            self._config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            self._config_mtime = CONFIG_FILE.stat().st_mtime
        except Exception as exc:
            self._append_output(f"Failed to read config.json: {exc}\n", "ERROR")
            return

        cfg = self._config

        for key, var in self._bvars.items():
            val = _get_nested(cfg, key)
            var.set(str(val) if val is not None else "")

        n = int(_get_nested(cfg, "num_players") or 1)
        self._num_cb.set(str(n))
        self._update_player_slots(n)
        self._pref_cb.set(str(_get_nested(cfg, "preference_if_equal") or "earlier"))
        self._testing_var.set(bool(_get_nested(cfg, "testing")))
        self._work_card_var.set(bool(_get_nested(cfg, "payment.use_work_card")))
        self._refresh_mode_btn()
        self._update_step_visibility()
        self._update_test_indicators()

        names  = list(_get_nested(cfg, "additional_players") or [])
        emails = list(_get_nested(cfg, "email_notifications.to_emails") or [])
        for i in range(MAX_EXTRA_PLAYERS):
            self._player_name_vars[i].set(names[i]  if i < len(names)  else "")
            self._player_email_vars[i].set(emails[i] if i < len(emails) else "")

        # Sensitive fields: decrypt for display, cache original ENC strings
        for path in SENSITIVE_FIELDS:
            if path not in self._adv_vars:
                continue
            raw = _get_nested(cfg, path)
            if raw and str(raw).startswith("ENC:"):
                self._raw_enc[path] = str(raw)
                self._adv_vars[path].set(crypto_utils.decrypt(str(raw)) or "")
            else:
                self._adv_vars[path].set(str(raw) if raw else "")

        self._auto_enabled_var.set(bool(_get_nested(cfg, "auto_booking.enabled")))
        self._dumps_var.set(bool(_get_nested(cfg, "capture_page_dumps")))
        # Every non-sensitive entry binding loads by its dotted path;
        # sensitive fields were handled (decrypted) above.
        for key, var in self._adv_vars.items():
            if key in SENSITIVE_FIELDS:
                continue
            val = _get_nested(cfg, key)
            var.set(str(val) if val is not None else "")

        self._email_enabled_var.set(bool(_get_nested(cfg, "email_notifications.enabled")))
        for path, var in self._bool_vars.items():
            val = _get_nested(cfg, path)
            if path == "email_notifications.local_popup":
                # Defaults ON when the key is missing (matches the booker).
                var.set(True if val is None else bool(val))
            else:
                var.set(bool(val))
        self._update_waiting_gates()

        # Widget-only state (no tk Vars) on the lazily built sub-tabs; each
        # helper no-ops until its sub-tab has been built, and the builders
        # call them again on construction.
        self._populate_contact_textboxes()
        self._populate_calibration_label()

        # Read-only status line (written by other flows, never typed here).
        work4 = _get_nested(cfg, "payment.work_card_last4") or "?"
        play4 = _get_nested(cfg, "payment.default_card_last4") or "?"
        self._card_label.configure(
            text=f"Work card •{work4} / Play card •{play4} (display only)")

        # Show last 10 lines of the log file (startup only; avoid duplicating on reloads)
        if show_log_tail:
            log_file = BASE_DIR / "tee_time_booking.log"
            if log_file.exists():
                try:
                    lines = log_file.read_text(
                        encoding="utf-8", errors="replace").splitlines()
                    if lines:
                        self._append_output(
                            "----- previous session -----\n", "DEBUG")
                    for line in lines[-10:]:
                        self._append_output(line + "\n")
                except Exception:
                    pass

    def _poll_config(self) -> None:
        """Reload the form if config.json changed on disk (Setup/Calibrate/external edit)."""
        try:
            if CONFIG_FILE.exists():
                mtime = CONFIG_FILE.stat().st_mtime
                if mtime != getattr(self, "_config_mtime", None):
                    self._load_config()
                    self.status_label.configure(text="Config reloaded")
        except Exception:
            pass
        self.after(1500, self._poll_config)

    # -----------------------------------------------------------------------
    # Config save
    # -----------------------------------------------------------------------

    def _on_main_tab_change(self) -> None:
        """Auto-save whenever the user switches the main tab. Logs the save,
        but never pops a modal on a malformed field; validation and write
        errors are left for a finalizing action (Save button, Run) to
        surface, so a mid-edit tab click is never interrupted."""
        self._save_config(autosave=True)

    def _save_config(self, autosave: bool = False) -> None:
        if not self._config:
            self._config = copy.deepcopy(DEFAULT_CONFIG)

        cfg = self._config

        # Booking tab
        for key, var in self._bvars.items():
            _set_nested(cfg, key, var.get().strip())

        n = int(self._num_cb.get())
        _set_nested(cfg, "num_players", n)
        _set_nested(cfg, "preference_if_equal", self._pref_cb.get())
        _set_nested(cfg, "testing", self._testing_var.get())
        _set_nested(cfg, "payment.use_work_card", self._work_card_var.get())

        visible = max(0, n - 1)
        names  = [self._player_name_vars[i].get().strip()  for i in range(visible)]
        emails = [self._player_email_vars[i].get().strip() for i in range(visible)]
        _set_nested(cfg, "additional_players", names)
        _set_nested(cfg, "email_notifications.to_emails",
                    [e for e in emails if e])

        # Settings sub-tabs
        _set_nested(cfg, "auto_booking.enabled", self._auto_enabled_var.get())
        _set_nested(cfg, "capture_page_dumps", self._dumps_var.get())
        _set_nested(cfg, "email_notifications.enabled", self._email_enabled_var.get())
        for path, var in self._bool_vars.items():
            _set_nested(cfg, path, var.get())
        for key, var in self._adv_vars.items():
            if key in SENSITIVE_FIELDS:
                continue
            raw_val = var.get().strip()
            # Numeric fields must save as ints, so try-int every value; the
            # non-numeric ones (times, URL) simply keep their string, and
            # true garbage in an int field is caught by validate_config.
            try:
                raw_val = int(raw_val)
            except ValueError:
                pass
            _set_nested(cfg, key, raw_val)

        # Contact textboxes: one address per line (commas tolerated). If the
        # Notifications sub-tab was never built, the config values are
        # untouched (nothing could have been edited).
        if hasattr(self, "_failure_tb"):
            for tb, key in ((self._failure_tb,
                             "email_notifications.failure_contacts"),
                            (self._heartbeat_tb,
                             "email_notifications.heartbeat_contacts")):
                raw = tb.get("1.0", "end").replace(",", "\n")
                _set_nested(cfg, key,
                            [p.strip() for p in raw.split("\n") if p.strip()])

        # Credential fields: write back original ENC string if unchanged; re-encrypt if changed
        for path in SENSITIVE_FIELDS:
            if path not in self._adv_vars:
                continue
            current = self._adv_vars[path].get()
            original_enc = self._raw_enc.get(path)
            if original_enc:
                original_plain = crypto_utils.decrypt(original_enc) or ""
                if current == original_plain:
                    _set_nested(cfg, path, original_enc)
                    continue
            _set_nested(cfg, path, crypto_utils.encrypt(current) if current else "")

        # Validate before writing. Saving must succeed even when required
        # fields are still empty (completeness is enforced at Run time);
        # only malformed present values block a save.
        try:
            validate_config(cfg, require_complete=False)
        except ConfigError as exc:
            if autosave:
                # A tab-switch auto-save never interrupts with a modal; the
                # malformed field is left for a finalizing action to surface.
                self.status_label.configure(text="Auto-save skipped (invalid field)")
                return
            self._show_error(
                "Config Validation Error"
                + self._error_tab_suffix(str(exc)), str(exc))
            return

        try:
            CONFIG_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
            # Record our own write so the poller doesn't reload (and clobber) it.
            self._config_mtime = CONFIG_FILE.stat().st_mtime
            self.status_label.configure(text="Saved")
            self._append_output(
                "Config auto-saved.\n" if autosave else "Config saved.\n", "INFO")
        except Exception as exc:
            if autosave:
                self.status_label.configure(text="Auto-save failed")
                self._append_output(f"Auto-save failed: {exc}\n", "WARNING")
            else:
                self._show_error("Save Error", str(exc))

    # First failing key -> Settings sub-tab, so a validation error on a
    # sub-tab the user isn't looking at names where to click. Order matters:
    # more specific prefixes first (to_emails maps to the main Booking tab's
    # player rows, so it gets no suffix).
    _ERROR_TAB_PREFIXES = (
        ("username", "Defaults"),
        ("password", "Defaults"),
        ("auto_booking.", "Defaults"),
        ("waiting_mode.", "Automation"),
        ("email_notifications.to_emails", ""),
        ("email_notifications.", "Notifications"),
        ("preference_if_equal", "Defaults"),
        ("earlier_tolerance_minutes", "Defaults"),
        ("later_tolerance_minutes", "Defaults"),
        ("testing", "Advanced"),
        ("capture_page_dumps", "Advanced"),
        ("reservations_cache_max_age_hours", "Advanced"),
        ("booking_url", "Advanced"),
    )

    def _error_tab_suffix(self, message: str) -> str:
        """Map the first failing key path in a validation message to its
        Settings sub-tab name, or an empty string for main-tab fields."""
        for line in message.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            path = line[2:].split(":", 1)[0].strip()
            for prefix, tab in self._ERROR_TAB_PREFIXES:
                if path.startswith(prefix):
                    return f" ({tab})" if tab else ""
            return ""
        return ""

    # -----------------------------------------------------------------------
    # Run / stop / calibrate
    # -----------------------------------------------------------------------

    # Human label + on-screen location per config path, for the pre-run
    # completeness dialog. Prefixes matched after an exact-path lookup.
    _FIELD_LOCATIONS = (
        ("booking_date", "Booking Date", "Booking tab"),
        ("desired_time", "Desired Time", "Booking tab"),
        ("earliest_acceptable_time", "Earliest Time", "Booking tab"),
        ("latest_acceptable_time", "Latest Time", "Booking tab"),
        ("num_players", "Num Players", "Booking tab"),
        ("additional_players", "Additional Players", "Booking tab"),
        ("email_notifications.to_emails", "Player Emails", "Booking tab"),
        ("username", "Username", "Settings > Defaults"),
        ("password", "Password", "Settings > Defaults"),
        ("preference_if_equal", "Preference If Equal", "Settings > Defaults"),
        ("earlier_tolerance_minutes", "Earlier Tolerance", "Settings > Defaults"),
        ("later_tolerance_minutes", "Later Tolerance", "Settings > Defaults"),
        ("auto_booking.", "Auto-Booking", "Settings > Defaults"),
        ("waiting_mode.", "Waiting for Opening", "Settings > Automation"),
        ("email_notifications.", "Notifications", "Settings > Notifications"),
        ("booking_url", "Booking URL", "Settings > Advanced"),
        ("reservations_cache_max_age_hours", "Reservations Cache Age",
         "Settings > Advanced"),
        ("cloudflare_checkbox.", "Cloudflare Calibration",
         "Calibrate Cloudflare button"),
    )

    def _describe_errors(self, message: str) -> str:
        """Turn a ConfigError's '- path: detail' lines into human-labelled
        lines with the field's on-screen location."""
        out = []
        for line in message.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            path, _, detail = line[2:].partition(":")
            base = path.strip().split("[", 1)[0]
            for prefix, label, loc in self._FIELD_LOCATIONS:
                if base == prefix.rstrip(".") or base.startswith(prefix):
                    # A sub-key (e.g. auto_booking.release_time) keeps its
                    # own leaf name so the exact field is still named.
                    if prefix.endswith(".") and base != prefix.rstrip("."):
                        leaf = base[len(prefix):].replace("_", " ")
                        label = f"{label}: {leaf}"
                    out.append(f"{label} ({loc}):{detail}")
                    break
            else:
                out.append(f"{path.strip()}:{detail}")
        return "\n".join(out) if out else message

    def _validate_before_run(self, require_booking: bool = True) -> bool:
        """Full completeness check before launching a run. Saving is lenient
        (empty fields allowed); running is where completeness is enforced.

        require_booking=False (reservations refresh/cancel) checks only sign-in
        essentials plus calibration, so a stale booking_date never blocks the
        reservations features."""
        try:
            validate_config(self._config, require_booking=require_booking)
        except CalibrationRequired:
            self._show_error(
                "Cannot Run",
                "Cloudflare is not calibrated. Use the Calibrate Cloudflare "
                "button first.")
            return False
        except ConfigError as exc:
            self._show_error(
                "Cannot Run - Missing or Invalid Fields",
                "Fix these fields, then Save Config and run again:\n\n"
                + self._describe_errors(str(exc)))
            return False
        return True

    def _run_booking(self) -> None:
        """Run in auto or single mode depending on the auto-booking setting.

        Step-through (test mode only) implies a single attempt — stepping
        through repeated auto-poll attempts doesn't make sense.
        """
        if not self._validate_before_run():
            return
        if self._run_mode_cb.get() == "waiting":
            if not WAITING_ENABLED:
                self._show_error(
                    "Waiting Mode Disabled",
                    "Waiting-for-opening mode is not yet enabled in this "
                    "build (pending live verification). Run Mode has been "
                    "reset to standard.")
                self._run_mode_cb.set("standard")
                return
            self._run("waiting")
            return
        step = self._testing_var.get() and self._step_var.get()
        if step:
            mode = "single"
        else:
            mode = "auto" if self._auto_enabled_var.get() else "single"
        self._run(mode, step=step)

    def _run(self, mode: str, step: bool = False, extra_args: list | None = None) -> None:
        if self._proc is not None:
            return
        args = [mode] + list(extra_args or [])
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, *args]
        else:
            cmd = [sys.executable, str(BASE_DIR / "gui.py"), *args]
        self._last_run_mode = mode

        # Step mode signals the booker (via env) to pause after each stage and
        # wait for a newline on stdin, which the Next button supplies.
        env = os.environ.copy()
        if step:
            env["TTB_STEP_MODE"] = "1"
        self._step_active = step

        # CREATE_NEW_PROCESS_GROUP lets us reliably kill the whole tree
        # (Python child + chromedriver + Chrome) when the user hits Stop.
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if step else None,
                text=True, cwd=str(BASE_DIR),
                env=env,
                creationflags=creationflags,
            )
        except Exception as exc:
            self._append_output(f"Failed to launch process: {exc}\n", "ERROR")
            self._step_active = False
            return

        self._set_running(True, mode)
        self._out_rc = None
        threading.Thread(
            target=self._stream_output, args=(self._proc,), daemon=True
        ).start()
        self.after(80, self._flush_output)

    def _advance_step(self) -> None:
        """Send a newline to the running booker so it continues to the next pause."""
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write("\n")
                self._proc.stdin.flush()
                self.status_label.configure(text="Stepping...")
            except Exception as exc:
                self._append_output(f"Step error: {exc}\n", "ERROR")

    def _update_step_visibility(self) -> None:
        """Show the step-through option only when Testing Mode is on."""
        if not hasattr(self, "_step_check"):
            return  # Advanced sub-tab not built yet
        if self._testing_var.get():
            self._step_label.grid()
            self._step_check.grid()
        else:
            self._step_var.set(False)
            self._step_label.grid_remove()
            self._step_check.grid_remove()

    def _on_testing_toggle(self) -> None:
        self._update_step_visibility()
        self._update_test_indicators()

    def _ready_text(self) -> str:
        return "Ready (TEST MODE)" if self._testing_var.get() else "Ready"

    def _update_test_indicators(self) -> None:
        """Keep test mode discoverable now that its checkbox lives in
        Settings > Advanced: flag it on the Run button and idle status."""
        self.btn_run.configure(
            text="Run (test)" if self._testing_var.get() else "Run")
        if self._proc is None:
            self.status_label.configure(text=self._ready_text())

    def _toggle_mode(self) -> None:
        """Flip between Work and Play card modes when the button is clicked."""
        self._work_card_var.set(not self._work_card_var.get())
        self._refresh_mode_btn()

    def _refresh_mode_btn(self) -> None:
        """Update the mode button's label/color to reflect the current state."""
        work = self._work_card_var.get()
        self._mode_btn.configure(
            text="Work" if work else "Play",
            fg_color="#b5651d" if work else "#2a8f2a",
            hover_color="#9c5618" if work else "#1f6f1f",
        )

    def _stream_output(self, proc: subprocess.Popen) -> None:
        """Reader thread: buffer lines for the GUI-side flush loop. One
        after(0) per line caused visible jank on chatty runs; the flush loop
        batches a whole tick's worth into a single Text update."""
        for line in proc.stdout:
            with self._out_lock:
                self._out_buf.append(line)
        proc.wait()
        # Set last: the flush loop treats a non-None code plus a drained
        # buffer as end-of-stream, and no lines are appended after this.
        self._out_rc = proc.returncode

    def _flush_output(self) -> None:
        """Drain the reader thread's buffer into the Text widget in one batch
        (per-line tags, then a single trim + scroll), and reschedule until the
        process has exited and the buffer is fully drained."""
        with self._out_lock:
            lines, self._out_buf = self._out_buf, []
        if lines:
            self.output_text.configure(state="normal")
            for line in lines:
                self.output_text.insert("end", line, _line_level(line))
            line_count = int(self.output_text.index("end-1c").split(".")[0])
            if line_count > 2000:
                self.output_text.delete("1.0", f"{line_count - 2000}.0")
            self.output_text.see("end")
            self.output_text.configure(state="disabled")
        rc = self._out_rc
        if rc is not None and not self._out_buf:
            # Reader finished and everything it wrote has been displayed.
            self._on_done(rc)
            return
        self.after(80, self._flush_output)

    def _on_done(self, returncode: int) -> None:
        self._proc = None
        self._step_active = False
        self._set_running(False, "")
        self.status_label.configure(
            text="Done" if returncode == 0 else f"Exited ({returncode})")
        # A refresh or cancel rewrites reservations_cache.json in the
        # subprocess; reload the tab from disk now that it has finished,
        # but skip the (expensive) re-render if the cache stamp is unchanged
        # (e.g. the run failed before fetching).
        if self._last_run_mode in ("reservations-refresh", "cancel"):
            cache = self._read_res_cache() or {}
            if cache.get("fetched_at") != self._res_rendered_fetched_at:
                self._render_reservations()

    def _stop(self) -> None:
        if not self._proc:
            return
        # terminate() only kills the Python child; chromedriver and Chrome are
        # separate processes that keep the stdout pipe open, so the stream
        # thread never sees EOF and the buttons never re-enable (and the browser
        # stays up). taskkill /T kills the whole tree, which closes the pipe,
        # lets _stream_output finish, and fires _on_done to re-enable the UI.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                capture_output=True,
            )
        except Exception as exc:
            self._append_output(f"Stop error: {exc}\n", "ERROR")
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._append_output("Process stopped by user.\n", "WARNING")

    def _on_close(self) -> None:
        """Window close = stop everything. Kill the child process tree (same
        taskkill path as the Stop button) before tearing the window down."""
        if self._proc is not None:
            self._stop()
        self.destroy()

    def _calibrate(self) -> None:
        if getattr(sys, "frozen", False):
            inner = f'"{sys.executable}" calibrate'
        else:
            inner = f'"{sys.executable}" "{BASE_DIR / "gui.py"}" calibrate'
        # /c runs then closes the spawned window (was /k, which left it open
        # after calibration finished); "|| pause" keeps it up only when the
        # calibrate command fails, so an error is still readable.
        subprocess.Popen(f'start "Calibrate" cmd /c "{inner} || pause"',
                         shell=True, cwd=str(BASE_DIR))
        self._append_output(
            "Calibration launched in a new terminal window "
            "(it closes when done).\n", "INFO")

    def _validate_config_file(self) -> None:
        """Confirm config.json is valid. Default-config creation and encryption
        of plaintext sensitive fields happen automatically inside
        prepare_config(); this button only reports the result."""
        self.status_label.configure(text="Validating...")
        try:
            prepare_config()
        except CalibrationRequired:
            self._append_output(
                "Config is valid, but Cloudflare is not calibrated. "
                "Use 'Calibrate Cloudflare' before booking.\n", "WARNING")
            self.status_label.configure(text="Calibration needed")
        except ConfigError as exc:
            self._append_output(f"Config is INVALID:\n{exc}\n", "ERROR")
            self._show_error("Config Invalid", str(exc))
            self.status_label.configure(text="Invalid")
        except Exception as exc:
            self._append_output(f"Validation error: {exc}\n", "ERROR")
            self.status_label.configure(text="Error")
        else:
            self._append_output("Config is valid.\n", "INFO")
            self.status_label.configure(text="Config valid")
        finally:
            # prepare_config() may rewrite the file when encrypting plaintext;
            # record our own write so the poller doesn't reload and clobber any
            # unsaved edits in the form.
            try:
                if CONFIG_FILE.exists():
                    self._config_mtime = CONFIG_FILE.stat().st_mtime
            except Exception:
                pass

    def _set_running(self, running: bool, mode: str) -> None:
        step = running and self._step_active
        # Re-lay the action bar for the current state. In step-through mode the
        # setup buttons are hidden entirely (not just dimmed) so only Step and
        # End remain, with Step leftmost.
        for btn in (self.btn_save, self.btn_run, self.btn_validate,
                    self.btn_calibrate, self.btn_next, self.btn_stop):
            btn.pack_forget()

        if step:
            self.btn_next.pack(side="left", padx=4)       # Step (leftmost)
            self.btn_stop.configure(text="End")
            self.btn_stop.pack(side="left", padx=4)        # End
        else:
            state = "disabled" if running else "normal"
            for btn in (self.btn_save, self.btn_run, self.btn_validate,
                        self.btn_calibrate):
                btn.configure(state=state)
                btn.pack(side="left", padx=4)
            self.btn_stop.configure(text="Stop")
            self.btn_stop.pack(side="left", padx=4)

        self.btn_stop.configure(state="normal" if running else "disabled")

        # Reservations tab controls share the single subprocess, so lock them
        # while any run is in flight (and vice versa).
        res_state = "disabled" if running else "normal"
        if hasattr(self, "_res_refresh_btn"):
            self._res_refresh_btn.configure(state=res_state)
        for b in self._res_cancel_buttons:
            try:
                if b.cget("text") != "Expired":
                    b.configure(state=res_state)
            except Exception:
                pass

        self.status_label.configure(
            text=f"Running ({mode})..." if running else self._ready_text())

    # -----------------------------------------------------------------------
    # Output panel helpers
    # -----------------------------------------------------------------------

    def _append_output(self, text: str, level: str | None = None) -> None:
        tag = level if level else _line_level(text)
        self.output_text.configure(state="normal")
        self.output_text.insert("end", text, tag)
        # Trim the widget on long runs; Tk Text slows down as it grows.
        line_count = int(self.output_text.index("end-1c").split(".")[0])
        if line_count > 2000:
            self.output_text.delete("1.0", f"{line_count - 2000}.0")
        self.output_text.see("end")
        self.output_text.configure(state="disabled")

    def _clear_output(self) -> None:
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.configure(state="disabled")

    def _show_error(self, title: str, message: str) -> None:
        # No fixed geometry: the window sizes to the wrapped message, so a
        # long validation list is never clipped.
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.grab_set()
        ctk.CTkLabel(win, text=message, wraplength=380,
                     justify="left").pack(padx=20, pady=20)
        ctk.CTkButton(win, text="OK", command=win.destroy).pack(pady=(0, 12))


# ===========================================================================
# Entry point
# ===========================================================================

_GUI_MUTEX = None  # keep the handle alive for the process lifetime


def _first_gui_instance() -> bool:
    """One GUI window at a time, via a named Windows mutex. The OS releases
    the mutex when the process dies (including crashes), so it can't wedge."""
    global _GUI_MUTEX
    try:
        import ctypes
        _GUI_MUTEX = ctypes.windll.kernel32.CreateMutexW(
            None, False, "TimmysTeeTimeBooker_GUI_SingleInstance")
        ERROR_ALREADY_EXISTS = 183
        return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True  # never block the GUI on a mutex failure


def main() -> None:
    if not _first_gui_instance():
        import tkinter.messagebox as mb
        root = tk.Tk()
        root.withdraw()
        mb.showwarning(
            "Timmy's Tee Time Booker",
            "The Tee Time Booker is already open.\n\n"
            "Use the existing window (check the taskbar).")
        root.destroy()
        return
    app = App()
    app.mainloop()


if __name__ == "__main__":
    # In frozen mode this exe is the single entry point for all modes.
    # Route CLI args to TimmysTeeTimeBooker's main(); otherwise open the GUI.
    if len(sys.argv) > 1 and sys.argv[1] in ("single", "auto", "waiting", "setup", "calibrate", "reservations", "reservations-refresh", "cancel", "help"):
        import TimmysTeeTimeBooker
        TimmysTeeTimeBooker.main()
    else:
        main()
