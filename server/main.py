import asyncio
import contextlib
import glob
import io
import json
import logging
import logging.handlers
import math
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Tk only serves the file dialogs that ask for a missing path. A Python built
# without it (some uv-managed builds) still runs the server and the tests;
# a missing path is then reported instead of asked for (see verify_paths).
try:
    import tkinter as tk
    from tkinter import filedialog
except ImportError:
    tk = filedialog = None

import label_blocks
import pcb_layout
import pcblib_file
import routing
import sandbox
import script_bundle
import silkscreen
import win32api
import win32con
import win32gui
import win32process
import win32ui
import win_processes
from altium_files import (
    long_path,
    read_altium_text,
    spec_with_long_library,
    write_altium_text,
)
from altium_install import find_altium_exe
from dblib import color_to_tcolor, find_dblib, load_csv_table, load_dblib
from exchange_lock import ExchangeLock
from harness_layout import (
    build_layout_spec,
    check_layout,
    parse_ldrdoc,
    unrouted_reasons,
)
from harness_parser import (
    COMPONENT_KIND_NAMES,
    get_harness_component_data,
    get_harness_component_pins,
    get_harness_components,
    get_harness_designators,
    get_harness_erc_violations,
    get_harness_net_connections,
    get_harness_nets,
    get_harness_property_names,
    get_harness_property_values,
    get_harness_schematic_data,
    get_harness_wiring_docs,
    parse_prjhar,
    parse_wirdoc,
)
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.utilities.types import Image as MCPImage
from mcp.types import ToolAnnotations
from outjob_parser import parse_outjob
from PIL import Image

# Set MCP_DIR to the directory of the current Python file
MCP_DIR = Path(__file__).parent


# config.json lives in a per-user folder OUTSIDE the install directory and
# outside AppData. Claude Desktop replaces the extension folder on every
# update, which used to reset a hand-picked Altium path; and being an MSIX
# package it redirects its child processes' AppData writes into its own
# LocalCache. start_server.py passes the folder it chose in ALTIUM_MCP_HOME.
# LEGACY_CONFIG_FILE is read once to migrate.
def _data_dir() -> Path:
    override = os.environ.get("ALTIUM_MCP_HOME")
    return Path(override) if override else Path.home() / ".altium-mcp"


class _RotatingLog(logging.handlers.RotatingFileHandler):
    """Rotates like its base class, but keeps logging when it cannot.

    Windows will not rename a file another process has open, and two servers
    (two Claude sessions) often share this log. The base class then fails
    on every record; this one keeps appending and leaves the rotation to the
    next server that starts alone.
    """

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            self.maxBytes = 0
            if self.stream is None:
                self.stream = self._open()


# The log sits beside config.json, for the same reasons, and rotates at
# 1 MB with 3 backups. INFO by default: DEBUG adds a line for every MCP
# protocol message. Set ALTIUM_MCP_LOG_LEVEL=DEBUG to get them back.
LOG_FILE = _data_dir() / "altium_mcp.log"
_log_level = logging.getLevelName(os.environ.get("ALTIUM_MCP_LOG_LEVEL", "INFO").upper())
_log_handlers = [logging.StreamHandler()]  # stderr: stdout carries the MCP protocol
try:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _log_handlers.append(_RotatingLog(str(LOG_FILE), maxBytes=1_000_000, backupCount=3,
                                      encoding="utf-8"))
except OSError:
    pass  # no log file is no reason not to start
logging.basicConfig(
    level=_log_level if isinstance(_log_level, int) else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=_log_handlers
)
logger = logging.getLogger("AltiumMCPServer")

LEGACY_CONFIG_FILE = MCP_DIR / "config.json"
CONFIG_FILE = _data_dir() / "config.json"
DEFAULT_SCRIPT_PATH = MCP_DIR / "AltiumScript" / "Altium_API.PrjScr"

# Use a fixed exchange directory for request/response JSON files.
# Both the Python MCP server and the Altium DelphiScript need to independently
# resolve to the same directory. C:\Users\Public is writable by all users and
# exists on every Windows machine. This avoids fragile script-project-path
# resolution that breaks when Altium caches stale script projects.
EXCHANGE_DIR = Path("C:/Users/Public/altium_mcp")
EXCHANGE_DIR.mkdir(exist_ok=True)
REQUEST_FILE = EXCHANGE_DIR / "request.json"
RESPONSE_FILE = EXCHANGE_DIR / "response.json"
# Exchange lock shared by every server process using EXCHANGE_DIR (see
# exchange_lock.py and AltiumBridge.exclusive)
BRIDGE_LOCK_FILE = EXCHANGE_DIR / "bridge.lock"


class BridgeUnavailable(TimeoutError):
    """The bridge cannot take a command now; nothing was sent to Altium.

    A TimeoutError so that callers of AltiumBridge.exclusive written for the
    old lock (which raised TimeoutError when another server held it) keep
    reporting the reason as their error.
    """

# Written by the bridge script when it starts a command and, at most once a
# second, while a long command (a batch) is still working
PROGRESS_FILE = EXCHANGE_DIR / "progress.txt"

# Copies of command results (design data) that older bridge scripts left in
# this folder, which every account on the machine can read. Nothing reads
# them. Named one by one: users keep their own script output here too.
LEFTOVER_TEMP_FILES = (
    "temp_board_summary.json", "temp_component_data.json",
    "temp_component_primitives.json", "temp_erc.json",
    "temp_footprint_primitives.json", "temp_json_output.json",
    "temp_layers_data.json", "temp_net_connections.json", "temp_nets_data.json",
    "temp_pins_data.json", "temp_project_components.json",
    "temp_project_info.json", "temp_rules_data.json",
    "temp_schematic_data.json", "temp_search_symbol.json",
    "temp_selected_components.json", "temp_stackup_data.json",
    "temp_symbol_primitives.json", "temp_symbol_reference.json",
)


def _remove_leftover_temp_files(folder: Path = EXCHANGE_DIR):
    for name in LEFTOVER_TEMP_FILES:
        try:
            (folder / name).unlink()
        except OSError:
            pass  # absent, or held open: best effort


_remove_leftover_temp_files()

# Seconds for a running Altium to pick up a request (normally ~1-5 s), for a
# cold Altium launch to get that far, for a started command to go without
# reporting progress, and for any command to finish at all
START_TIMEOUT = 20
START_TIMEOUT_COLD = 240
RESPONSE_TIMEOUT = 120
RESPONSE_TIMEOUT_MAX = 30 * 60

# How long to wait while another server's command holds the exchange folder
LOCK_TIMEOUT = START_TIMEOUT_COLD + RESPONSE_TIMEOUT

# Commands that change the design or write files. When one of them times out
# it may have done some or all of its work, so it must not just be retried.
MUTATING_COMMANDS = {
    "create_net_class", "create_schematic_symbol", "set_component_position",
    "move_components", "place_components", "create_symbols_batch", "build_circuit",
    "create_footprints_batch", "layout_duplicator", "layout_duplicator_apply",
    "run_output_jobs", "create_pcb_footprint",
}

# Present while a script the MCP launched is stuck (paused in Altium's
# debugger). Only then is the debugger stopped, so a script the user is
# running or debugging is never stopped. Every server using this exchange
# folder sees it, including after a restart.
STUCK_MARKER = EXCHANGE_DIR / "mcp_script_stuck.txt"


def _mark_script_stuck(what: str):
    try:
        STUCK_MARKER.write_text(what, encoding="utf-8")
    except OSError:
        pass


def _clear_script_stuck():
    try:
        STUCK_MARKER.unlink()
    except OSError:
        pass


ALTIUM_HUNG_ERROR = (
    "Not run: Altium is not responding. A script or a long command is still running in it, or "
    "a script is stuck in a loop. Nothing can be sent to Altium until it finishes. Scripts "
    "started by run_altium_script stop their while/repeat loops at their deadline; if Altium "
    "stays frozen, only ending X2.EXE frees it, and unsaved work is then lost.")


def _lift_failed_result(response: Dict[str, Any]) -> Dict[str, Any]:
    """Make a command's own failure the response's failure.

    Several bridge commands report failure as their result, e.g.
    {"success": false, "error": "No layer names provided"}, and the bridge
    wraps any result in "success": true. Tools then took the failure for
    success: get_screenshot even captured after its focus step had failed.
    Fields besides the message (per-container results of run_output_jobs,
    for instance) are kept in the error text.
    """
    result = response.get("result")
    if response.get("success") and isinstance(result, dict) and result.get("success") is False:
        error = result.get("error") or result.get("message") or "the command reported failure"
        details = {k: v for k, v in result.items() if k not in ("success", "error", "message")}
        if details:
            error = f"{error} {json.dumps(details)[:2000]}"
        return {**response, "success": False, "error": error}
    return response


# Initialize the MCP server (mcp 2: MCPServer, formerly FastMCP)
mcp = MCPServer("AltiumMCP", instructions="Altium integration through the Model Context Protocol")

READ_ONLY = ToolAnnotations(readOnlyHint=True)


def _tool(read_only: bool = False):
    """Register a tool.

    read_only marks a tool that only reads from Altium, so clients can tell it
    from the tools that change a design. Results stay plain text content: by
    default mcp would infer an output schema from "-> str" and send every
    result twice, once more as structuredContent.
    """
    return mcp.tool(annotations=READ_ONLY if read_only else None, structured_output=False)

class AltiumConfig:
    def __init__(self):
        self.altium_exe_path = ""
        self.script_path = str(DEFAULT_SCRIPT_PATH)
        self.load_config()

    def load_config(self):
        """Load configuration, migrating a pre-relocation config.json once."""
        source = CONFIG_FILE if CONFIG_FILE.exists() else LEGACY_CONFIG_FILE
        if source.exists():
            try:
                with open(source, "r") as f:
                    config = json.load(f)
                self.altium_exe_path = config.get("altium_exe_path", "")
                # Only a hand-picked script project is persisted. A saved path
                # that no longer exists (old install folder) falls back to the
                # project shipped beside this file instead of prompting.
                saved_script = config.get("script_path", "")
                if saved_script and os.path.exists(saved_script):
                    self.script_path = saved_script
                logger.info(f"Loaded configuration from {source}")
                if source is LEGACY_CONFIG_FILE:
                    self.save_config()
                else:
                    self._saved = self._as_dict()
            except Exception as e:
                logger.error(f"Error loading configuration: {e}")
                self._create_default_config()
        else:
            logger.info("No configuration file found, creating default")
            self._create_default_config()

    def _create_default_config(self):
        """Create a default configuration file, with Altium's path if it can be found"""
        self.altium_exe_path = find_altium_exe()
        self.save_config()

    def _as_dict(self):
        config = {"altium_exe_path": self.altium_exe_path}
        # The default script project belongs to whichever install is running
        # (extension folder or a dev checkout), so it is never written down:
        # both share this file and must not steal each other's scripts.
        if Path(self.script_path) != DEFAULT_SCRIPT_PATH:
            config["script_path"] = self.script_path
        return config

    def save_config(self):
        """Save configuration to file, only when something changed"""
        config = self._as_dict()
        if config == getattr(self, "_saved", None) and CONFIG_FILE.exists():
            return

        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            self._saved = config
            logger.info(f"Saved configuration to {CONFIG_FILE}")
        except Exception as e:
            logger.error(f"Error saving configuration: {e}")

    def discover_altium(self) -> bool:
        """Keep the saved Altium path if it still exists, else look for one.

        Never asks the user, so it is safe at import time. Called again
        before commands, so an Altium started after the server, or one
        updated to a new folder, is picked up without a restart.
        """
        if self.altium_exe_path and os.path.exists(self.altium_exe_path):
            return True
        found = find_altium_exe()
        if not found:
            return False
        logger.info(f"Automatically found Altium at: {found}")
        self.altium_exe_path = found
        self.save_config()
        return True

    def verify_paths(self):
        """Verify that the paths in the configuration exist, prompt for input if they don't

        Shows file dialogs, so it must not run on the event loop, and never
        before the server has answered the client's initialize request: a
        dialog nobody sees would hold the startup until the client gives up.
        Messages go to the log only; under the stdio transport stdout is the
        protocol channel.
        """

        # Initialize variables
        root = None
        paths_verified = True

        if tk is None:
            logger.warning("This Python has no tkinter: no file dialog for a missing path. "
                           f"Set it in {CONFIG_FILE} instead.")
            return self.discover_altium() and os.path.exists(self.script_path)

        # Check Altium executable
        if not self.discover_altium():
            root = self._dialog_root()
            logger.info("Altium executable not found in the registry, among running "
                        "processes or under C:\\Program Files\\Altium. Prompting user for selection...")

            self.altium_exe_path = filedialog.askopenfilename(
                parent=root,
                title="Select Altium Executable (X2.EXE)",
                filetypes=[("Executable files", "*.exe")],  # Only allow .exe files
                initialdir="C:/Program Files/Altium"
            )

            if not self.altium_exe_path:
                logger.error("No Altium executable selected. Some functionality may not work.")
                paths_verified = False

        # Check script path
        if not os.path.exists(self.script_path):
            paths_verified = False

            if root is None:
                root = self._dialog_root()

            logger.info(f"Script file not found at {self.script_path}. Prompting user for selection...")

            selected_path = filedialog.askopenfilename(
                parent=root,
                title="Select Altium Project File",
                filetypes=[
                    ("Altium Project files", "*.PrjScr")
                ],  # Changed to PrjScr for script project
                initialdir=str(MCP_DIR),
            )

            if selected_path:
                self.script_path = selected_path
            else:
                logger.error("No script file selected. Some functionality may not work.")
                paths_verified = False

        # Clean up tkinter root if created
        if root is not None:
            root.destroy()

        # Save the updated configuration
        self.save_config()

        return paths_verified

    @staticmethod
    def _dialog_root():
        # A hidden, topmost owner window, so the file dialog opens in front of
        # the client instead of behind it where nobody notices it
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        return root


# Two Altium resources run out over a long session, and at either limit
# Altium fails and then crashes, losing unsaved work:
# - USER objects (windows, menus), capped per process at 10,000 by default.
#   Running a script PROJECT leaked about 105 per call (hidden copies of the
#   forms of the user's menu scripts); the bridge now runs a script file,
#   but the user's own menu scripts still leak.
# - Memory: Altium's script engine leaks a little on every property read of
#   a board object, about 25 MB per silkscreen export on a 40k-object board
#   and 50 MB per full silkscreen check, and never gives it back.
# Calls stop while there is still room to save.
USER_OBJECT_RESERVE = 1000
MEMORY_SHARE_LIMIT = 0.5        # of physical RAM

def altium_resources() -> Optional[dict]:
    """USER objects, their quota, private memory and the machine's RAM (MB)
    for the running Altium, or None when it is not running."""
    import ctypes
    from ctypes import wintypes

    class MemCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    class MemStatus(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows") as key:
            quota = int(winreg.QueryValueEx(key, "USERProcessHandleQuota")[0])
    except OSError:
        quota = 10000
    kernel32, user32, psapi = ctypes.windll.kernel32, ctypes.windll.user32, ctypes.windll.psapi
    kernel32.OpenProcess.restype = wintypes.HANDLE
    status = MemStatus()
    status.dwLength = ctypes.sizeof(MemStatus)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    found = None
    for pid in win32process.EnumProcesses():
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            name = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if (kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size))
                    and os.path.basename(name.value).upper() == "X2.EXE"):
                counters = MemCounters()
                counters.cb = ctypes.sizeof(MemCounters)
                psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
                # Launcher instances are short-lived; the main one is the biggest
                here = {"user_objects": user32.GetGuiResources(handle, 1),     # GR_USEROBJECTS
                        "private_mb": counters.PrivateUsage / 2 ** 20}
                if found is None or here["private_mb"] > found["private_mb"]:
                    found = here
        finally:
            kernel32.CloseHandle(handle)
    if found is not None:
        found.update(quota=quota, ram_mb=status.ullTotalPhys / 2 ** 20)
    return found

def altium_limit_problem() -> Optional[str]:
    """Why calling into Altium now would risk a crash, or None."""
    r = altium_resources()
    if r is None:
        return None
    if r["user_objects"] > r["quota"] - USER_OBJECT_RESERVE:
        return (f"Altium is using {r['user_objects']} of its {r['quota']} window handles and will fail, "
                "then crash, near the limit. Ask the user to save their work in Altium and restart it.")
    if r["private_mb"] > MEMORY_SHARE_LIMIT * r["ram_mb"]:
        return (f"Altium is using {r['private_mb'] / 1024:.1f} GB of memory ({r['ram_mb'] / 1024:.0f} GB in the "
                "machine); its script engine leaks memory on every call and it will run out. Ask the user "
                "to save their work in Altium and restart it.")
    return None

class AltiumBridge:
    def __init__(self):
        # Ensure the MCP directory exists
        MCP_DIR.mkdir(exist_ok=True)

        # Load configuration. Finding Altium needs nobody, so it happens now;
        # asking for a path waits for the first command (see ensure_paths)
        self.config = AltiumConfig()
        self.config.discover_altium()
        self._asked_for_paths = False

        # Commands share a single request.json/response.json pair, so
        # concurrent tool calls must be serialized or they clobber each other:
        # within this server by the asyncio lock, and against other servers
        # using the same exchange folder by the file lock
        self._command_lock = asyncio.Lock()
        self._exchange_lock = ExchangeLock(BRIDGE_LOCK_FILE)

    async def ensure_paths(self, need_script: bool = True) -> Optional[str]:
        """None when Altium (and the bridge script project) can be found,
        else what is missing and where to set it.

        The file dialogs of verify_paths are shown at most once per server
        run, in a worker thread so the event loop keeps answering the
        client; after that a missing path is reported straight away.
        """
        def missing():
            if not self.config.discover_altium():
                return "altium_exe_path"
            if need_script and not os.path.exists(self.config.script_path):
                return "script_path"
            return None

        if missing() and not self._asked_for_paths:
            self._asked_for_paths = True
            await asyncio.to_thread(self.config.verify_paths)
        what = missing()
        if what == "altium_exe_path":
            return (f"Altium Designer was not found (looked in the registry, at running "
                    f"processes and under C:\\Program Files\\Altium). Set \"altium_exe_path\" "
                    f"in {CONFIG_FILE} to the full path of X2.EXE, then retry. The folder "
                    f"holding config.json can be changed with the ALTIUM_MCP_HOME "
                    f"environment variable.")
        if what == "script_path":
            return (f"The bridge script project was not found at {self.config.script_path}. "
                    f"Reinstall the extension, or set \"script_path\" in {CONFIG_FILE} to "
                    f"Altium_API.PrjScr, then retry.")
        return None

    @contextlib.asynccontextmanager
    async def exclusive(self):
        """Hold the Altium bridge for one or more bridge runs.

        An asyncio lock serializes this server's calls, and the exchange lock
        (a byte-range lock on BRIDGE_LOCK_FILE that the OS releases when a
        process dies) every other server process using the same exchange
        folder (two Claude sessions, Claude and Gemini). Tools that write a
        spec file and then run a command, or make several runs in a row,
        hold it across all of them so no other server can interleave.
        Raises BridgeUnavailable, before anything reaches Altium, when Altium
        cannot be found, another server kept the lock or Altium is hung.
        """
        async with self._command_lock:
            problem = await self.ensure_paths()
            if problem:
                raise BridgeUnavailable(problem)
            # BRIDGE_LOCK_FILE is read at each hold (tests point it elsewhere)
            self._exchange_lock.path = BRIDGE_LOCK_FILE
            if not await self._exchange_lock.acquire(LOCK_TIMEOUT):
                raise BridgeUnavailable(
                    f"Not run: another Altium MCP server (another Claude session) "
                    f"has had a command running in Altium for over {LOCK_TIMEOUT} s. "
                    f"Retry when it has finished.")
            try:
                # A launch sent to a hung Altium only queues up behind whatever
                # holds its UI thread; say so now instead of after the start
                # timeout. Checked with the exchange lock held: while waiting
                # for it, Altium is busy with another server's command, which
                # is no reason to give up.
                if await asyncio.to_thread(sandbox.altium_hung):
                    raise BridgeUnavailable(ALTIUM_HUNG_ERROR)
                yield
            finally:
                self._exchange_lock.release()

    async def execute_command(self, command: str, params: Dict[str, Any], timeout: float = RESPONSE_TIMEOUT) -> Dict[str, Any]:
        """Execute a command in Altium via the bridge script"""
        try:
            async with self.exclusive():
                response = await self._execute_command_locked(command, params, timeout)
        except BridgeUnavailable as e:
            return {"success": False, "error": str(e)}
        return _lift_failed_result(response)

    @staticmethod
    def _altium_running() -> bool:
        # Read in-process: starting tasklist took about 110 ms on every
        # command and held up the event loop while it ran
        try:
            return bool(win_processes.pids_by_name("X2.EXE"))
        except Exception:
            return True  # unknown: assume running and use the normal timeouts

    async def stop_script_debugger(self):
        """Stop a script of ours paused in Altium's debugger.

        A runtime error leaves a script paused in the debugger, after which
        Altium refuses every new RunScript until it is stopped. Each refused
        launch leaves a modal "Another script executing now" error open, and
        while it is open Altium turns the Stop request away as well ("another
        instance ... is busy"), so Altium's message boxes are closed first.
        Harmless when nothing is paused.
        """
        _dismiss_altium_dialogs(message_boxes_only=True)
        await asyncio.sleep(1)
        logger.warning(
            "Sending -REditScript:Stop to Altium to unblock the script executor"
        )
        try:
            subprocess.Popen(
                f'"{self.config.altium_exe_path}" -REditScript:Stop', shell=True
            )
        except Exception as e:
            logger.error(f"Could not send -REditScript:Stop: {e}")

    @staticmethod
    async def _wait_for_start(timeout: float) -> bool:
        """The bridge script deletes request.json as soon as it has read it."""
        start = time.time()
        while time.time() - start < timeout:
            if not REQUEST_FILE.exists() or RESPONSE_FILE.exists():
                return True
            await asyncio.sleep(0.25)
        return not REQUEST_FILE.exists() or RESPONSE_FILE.exists()

    @staticmethod
    async def _read_response_text() -> str:
        """Read response.json, retrying while Altium may still be writing it."""
        text = ""
        for _ in range(12):
            try:
                text = read_altium_text(RESPONSE_FILE)
                json.loads(text, strict=False)
                return text
            except (OSError, json.JSONDecodeError):
                await asyncio.sleep(0.25)
        return text

    async def _execute_command_locked(self, command: str, params: Dict[str, Any], timeout: float = RESPONSE_TIMEOUT) -> Dict[str, Any]:
        try:
            problem = altium_limit_problem()
            if problem:
                return {"success": False, "error": f"Not running '{command}': {problem}"}

            # Holding the exchange lock, any reply or progress mark still
            # here is left over from a command that timed out. A late writer
            # may still hold it open; the request_id check below covers that.
            for leftover in (RESPONSE_FILE, PROGRESS_FILE):
                try:
                    leftover.unlink()
                except OSError:
                    pass

            # The script echoes request_id so a late reply to an earlier,
            # timed-out command is never mistaken for this command's result
            request_id = uuid.uuid4().hex[:12]
            with open(REQUEST_FILE, "w") as f:
                json.dump(
                    {
                        "command": command,
                        "request_id": request_id,
                        **params,  # Include parameters directly in the main JSON object
                    },
                    f,
                    indent=2,
                )

            logger.info(
                f"Wrote request file for command: {command} (request_id={request_id})"
            )

            cold_start = not self._altium_running()
            if cold_start:
                logger.info(
                    "Altium is not running - launching it, allowing time for startup"
                )

            success = await self.run_altium_script()
            if not success:
                return {"success": False, "error": "Failed to run Altium script"}

            # A running Altium picks the request up within seconds. If it does
            # not and a script of ours is stuck in the debugger, stop it and
            # launch once more. The script consumes request.json, so a queued
            # first launch cannot run it twice. Any other script is left alone:
            # it may be one the user is running or debugging.
            started = await self._wait_for_start(
                START_TIMEOUT_COLD if cold_start else START_TIMEOUT
            )
            recovered = False
            if not started and STUCK_MARKER.exists():
                logger.warning(
                    f"Altium did not pick up the request within {START_TIMEOUT}s "
                    f"and a script of ours is stuck - attempting recovery"
                )
                await self.stop_script_debugger()
                await asyncio.sleep(2)
                recovered = True
                if REQUEST_FILE.exists():
                    await self.run_altium_script()
                started = await self._wait_for_start(START_TIMEOUT)

            if not started:
                try:
                    REQUEST_FILE.unlink()  # a launch Altium runs later must find nothing to do
                except OSError:
                    pass
                # Each refused launch left an OK-only "Another script executing
                # now" box in Altium: do not leave it in front of the user
                _dismiss_altium_dialogs(message_boxes_only=True)
                logger.error("Altium never started the bridge script")
                # Tools pass on only "error", so the explanation goes in it
                why = (
                    "A script of ours was stuck in Altium's debugger and stopping it did "
                    "not free Altium."
                    if recovered
                    else "Altium is not accepting script runs: a dialog may be open, Altium may "
                    "be busy or still starting up, or a script of the user's own may be "
                    "running or paused in its debugger. Nothing was stopped."
                )
                return {
                    "success": False,
                    "error": f"Altium did not start the command (not executed). {why} Check "
                    f"the Altium window for an open dialog or a running or paused "
                    f"script, then retry.",
                }
            _clear_script_stuck()

            logger.info("Bridge script started, waiting for response file...")
            # Wait as long as the command keeps reporting progress, up to
            # RESPONSE_TIMEOUT_MAX: a large batch is not stopped mid-way
            start_time = last_progress = time.monotonic()
            progress_mark = None
            response_text = None
            ignored_reply = None
            while response_text is None:
                try:
                    reply_mark = RESPONSE_FILE.stat().st_mtime
                except OSError:
                    reply_mark = None
                if reply_mark is not None and reply_mark != ignored_reply:
                    response_text = await self._read_response_text()
                    try:
                        rid = json.loads(response_text, strict=False).get("request_id")
                    except (json.JSONDecodeError, AttributeError):
                        rid = None
                    if rid is None or rid == request_id:
                        break
                    # Someone else's reply: a late one, or that of a server
                    # still without the exchange lock. Leave it for its owner;
                    # ours overwrites it when it comes.
                    logger.warning(f"Ignoring a reply for request_id={rid}")
                    response_text, ignored_reply = None, reply_mark
                now = time.monotonic()
                try:
                    mark = PROGRESS_FILE.stat().st_mtime
                except OSError:
                    mark = None
                if mark != progress_mark:
                    progress_mark, last_progress = mark, now
                if now - last_progress > timeout or now - start_time > max(RESPONSE_TIMEOUT_MAX, timeout):
                    break
                await asyncio.sleep(0.25)

            if response_text is None:
                logger.error("Timeout waiting for response from Altium")
                # The script started but never finished: it is most likely
                # paused in the debugger now. Unblock it for the next command.
                _mark_script_stuck(f"bridge command {command}")
                await self.stop_script_debugger()
                # A command that changes the design may have done some or all
                # of its work; a plain retry would then do it twice
                outcome = (f" '{command}' changes the design and may have done some or all of "
                           f"its work, so its outcome is unknown: check the design before "
                           f"running it again." if command in MUTATING_COMMANDS else "")
                return {"success": False,
                        "error": f"No response from Altium after "
                                 f"{int(time.monotonic() - start_time)} s, the last "
                                 f"{timeout:g} s without progress: command '{command}' "
                                 f"started but did not finish. It probably hit a runtime error "
                                 f"and paused in Altium's debugger (now stopped, so the next "
                                 f"command can run), or it is still working on a very large "
                                 f"design.{outcome}"}

            if recovered:
                logger.info("Command succeeded after automatic executor recovery")

            # Log the raw response for debugging
            logger.info(f"Raw response (first 200 chars): {response_text[:200]}")

            # Parse the JSON response. strict=False accepts control characters
            # inside strings: JSONEscapeString escapes only CR, LF and TAB, and
            # Altium text (descriptions, parameters) can hold others.
            try:
                response = json.loads(response_text, strict=False)
                logger.info(f"Successfully parsed JSON response")
                return response
            except json.JSONDecodeError as e:
                logger.error(f"Error parsing JSON response: {e}")
                logger.error(f"Error at position {e.pos}, line {e.lineno}, column {e.colno}")
                logger.error(f"Character at error position: '{response_text[e.pos:e.pos+10]}...'")
                return {
                    "success": False,
                    "error": f"Invalid JSON response: {e}",
                    "raw_response": response_text[
                        :500
                    ],  # Include part of the raw response for diagnosis
                }

        except Exception as e:
            logger.error(f"Error executing command: {e}")
            return {"success": False, "error": str(e)}

    @staticmethod
    def _resolve_msix_path(virtual_path: str) -> str:
        """Resolve an MSIX-virtualized path to the real filesystem path.

        When Claude Desktop is installed via MSIX (the standard .exe installer
        on modern Windows), file paths are virtualized under AppData\\Roaming\\
        but the real files live at AppData\\Local\\Packages\\Claude_*\\
        LocalCache\\Roaming\\. Child processes of the MSIX app (like Python)
        can see the virtualized paths, but external apps (like Altium) cannot.
        This resolves the path so external processes can find the files.
        """
        appdata = os.environ.get("APPDATA", "")
        if not appdata or not virtual_path.startswith(appdata):
            return virtual_path

        localappdata = os.environ.get("LOCALAPPDATA", "")
        packages_dir = os.path.join(localappdata, "Packages")
        if not os.path.isdir(packages_dir):
            return virtual_path

        try:
            for item in os.listdir(packages_dir):
                if item.startswith("Claude_"):
                    relative = os.path.relpath(virtual_path, appdata)
                    real_path = os.path.join(
                        packages_dir, item, "LocalCache", "Roaming", relative
                    )
                    if os.path.exists(real_path):
                        logger.info(
                            f"Resolved MSIX path: {virtual_path} -> {real_path}"
                        )
                        return real_path
        except Exception as e:
            logger.warning(f"Error resolving MSIX path: {e}")

        return virtual_path

    async def run_altium_script(self) -> bool:
        """Run the Altium bridge script"""
        if not os.path.exists(self.config.altium_exe_path):
            logger.error(f"Altium executable not found at: {self.config.altium_exe_path}")
            return False

        if not os.path.exists(self.config.script_path):
            logger.error(f"Script file not found at: {self.config.script_path}")
            return False

        try:
            # Resolve MSIX-virtualized path so Altium (an external process
            # outside the MSIX sandbox) can find the script files
            script_path = self._resolve_msix_path(self.config.script_path)

            # Run the units merged into one script FILE: running the script
            # project leaks window handles in Altium (see script_bundle.py).
            # The project stays the source, and the fallback.
            try:
                bundle = script_bundle.ensure_bundle(script_path, EXCHANGE_DIR / "bridge")
                command = (f'"{self.config.altium_exe_path}" -RScriptingSystem:RunScriptFile('
                           f'FileName={bundle}^|ProcName=Run)')
            except (OSError, ValueError) as e:
                logger.warning(f"Could not build the script bundle ({e}); running the project")
                command = (f'"{self.config.altium_exe_path}" -RScriptingSystem:RunScript('
                           f'ProjectName="{script_path}"^|ProcName="Altium_API>Run")')
            
            logger.info(f"Running command: {command}")

            # Start the process
            process = subprocess.Popen(command, shell=True)

            # Don't wait for completion - Altium will run the script and generate the response
            logger.info(f"Launched Altium with script, process ID: {process.pid}")
            return True

        except Exception as e:
            logger.error(f"Error launching Altium: {e}")
            return False


# Create a global bridge instance
altium_bridge = AltiumBridge()


async def _get_active_harness_path() -> Path | None:
    """The harness project (.PrjHar) or wiring document Altium has focused.

    Asked from Altium, never guessed from disk: a folder search returns
    whichever harness project sorts first, not the one being edited. None
    when Altium cannot answer or the focused document is not a harness.
    """
    response = await altium_bridge.execute_command("get_active_document", {})
    if not response.get("success", False):
        return None
    info = response.get("result", {})
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except ValueError:
            return None
    project = Path(info.get("project_path") or "")
    if project.suffix.lower() == ".prjhar" and project.exists():
        return project
    document = Path(info.get("document_path") or "")
    if document.suffix.lower() in (".wirdoc", ".ldrdoc") and document.exists():
        return document
    return None


def _disk_source(path: Path) -> dict[str, str]:
    """Marks a result that was parsed from saved files instead of Altium."""
    return {
        "source": "harness_file",
        "source_path": str(Path(path).resolve()),
        "note": "Parsed from the saved file; unsaved edits in Altium are not included.",
    }


async def _pcb_only_tool_error(tool_name: str, original_error: str) -> str | None:
    """If a PCB layout tool fails on a harness project, return a helpful explanation."""
    h_path = await _get_active_harness_path()
    if h_path:
        return json.dumps(
            {
                "error": (
                    f"Command '{tool_name}' requires an active PCB layout document (.PcbDoc), "
                    f"but the active project is an Altium Harness project ({h_path.name}). "
                    "For harness wiring, connectors, pinouts, and layout data, use "
                    "'get_harness_data', 'get_project_components', 'get_schematic_data', or 'get_all_nets'."
                ),
                "project_type": "harness",
                "project_path": str(h_path.resolve()),
                "original_error": original_error,
            },
            indent=2,
        )
    return None


@_tool(read_only=True)
async def get_all_component_property_names(ctx: Context) -> str:
    """
    Get all available component property names (JSON keys) from PCB/Schematic components or harness connectors.

    Returns:
        str: JSON array with all unique property names
    """
    logger.info("Getting all component property names")

    # Execute the command in Altium to get component data
    response = await altium_bridge.execute_command("get_all_component_data", {})

    # Check for success
    if not response.get("success", False) or not response.get("result"):
        h_path = await _get_active_harness_path()
        if h_path:
            property_list = get_harness_property_names(h_path)
            logger.info(f"Found {len(property_list)} unique harness property names")
            return json.dumps(property_list, indent=2)
        if not response.get("success", False):
            error_msg = response.get("error", "Unknown error")
            logger.error(f"Error getting component data: {error_msg}")
            return json.dumps({"error": f"Failed to get component data: {error_msg}"})

    # Get the component data
    components_data = response.get("result", [])

    if not components_data:
        h_path = await _get_active_harness_path()
        if h_path:
            property_list = get_harness_property_names(h_path)
            return json.dumps(property_list, indent=2)
        logger.info("No component data found")
        return json.dumps({"error": "No component data found"})

    try:
        # Parse the data if it's a string
        if isinstance(components_data, str):
            components_list = json.loads(components_data)
        else:
            components_list = components_data

        # Extract all unique property names from all components
        property_names = set()
        for component in components_list:
            property_names.update(component.keys())

        # Convert set to sorted list for consistent output
        property_list = sorted(property_names)

        logger.info(f"Found {len(property_list)} unique property names")
        return json.dumps(property_list, indent=2)
    except Exception as e:
        logger.error(f"Error processing component data: {e}")
        return json.dumps({"error": f"Failed to process component data: {e!s}"})


@_tool(read_only=True)
async def get_component_property_values(ctx: Context, property_name: str) -> str:
    """
    Get values of a specific property for all components (PCB/Schematic or harness connectors).

    Args:
        property_name (str): The name of the property to get values for

    Returns:
        str: JSON array with objects containing designator and property value
    """
    logger.info(f"Getting values for property: {property_name}")

    # Execute the command in Altium to get component data
    response = await altium_bridge.execute_command("get_all_component_data", {})

    # Check for success
    if not response.get("success", False) or not response.get("result"):
        h_path = await _get_active_harness_path()
        if h_path:
            property_values = get_harness_property_values(h_path, property_name)
            logger.info(
                f"Found {len(property_values)} harness components with property '{property_name}'"
            )
            return json.dumps(property_values, indent=2)
        if not response.get("success", False):
            error_msg = response.get("error", "Unknown error")
            logger.error(f"Error getting component data: {error_msg}")
            return json.dumps({"error": f"Failed to get component data: {error_msg}"})

    # Get the component data
    components_data = response.get("result", [])

    if not components_data:
        h_path = await _get_active_harness_path()
        if h_path:
            property_values = get_harness_property_values(h_path, property_name)
            return json.dumps(property_values, indent=2)
        logger.info("No component data found")
        return json.dumps({"error": "No component data found"})

    try:
        # Parse the data if it's a string
        if isinstance(components_data, str):
            components_list = json.loads(components_data)
        else:
            components_list = components_data

        # Extract the property values along with designators
        property_values = []
        for component in components_list:
            designator = component.get("designator")
            if designator and property_name in component:
                property_values.append(
                    {"designator": designator, "value": component.get(property_name)}
                )

        logger.info(
            f"Found {len(property_values)} components with property '{property_name}'"
        )
        return json.dumps(property_values, indent=2)
    except Exception as e:
        logger.error(f"Error processing component data: {e}")
        return json.dumps({"error": f"Failed to process component data: {e!s}"})


@_tool(read_only=True)
async def get_symbol_placement_rules(ctx: Context) -> str:
    """
    Get schematic symbol placement rules from a local configuration file

    Returns:
        str: JSON object with rules for placing pins on schematic symbols
    """
    logger.info("Getting symbol placement rules")

    # Define the rules file path in the MCP directory
    rules_file_path = MCP_DIR / "symbol_placement_rules.txt"

    # Check if the rules file exists
    if not rules_file_path.exists():
        logger.info("Symbol placement rules file not found, suggesting creation")

        # Default rules content
        default_rules = (
            "Your own pin placement rules, for example where power, ground, "
            "inputs, outputs and no-connect pins go and how pin groups are spaced."
        )

        # Create a helpful message for the user
        message = {
            "success": False,
            "error": f"Rules file not found at: {rules_file_path}",
            "message": f"Let the user know that they can optionally update the file {rules_file_path} with custom symbol placement rules. "
            f"Suggested content: {default_rules}",
        }

        return json.dumps(message, indent=2)

    # Read the rules file if it exists
    try:
        with open(rules_file_path, "r") as f:
            rules_content = f.read()

        logger.info("Successfully read symbol placement rules file")
        if not rules_content.strip():
            rules_content = """No symbol placement rules are configured. Follow the conventions of a comparable symbol in the user's library (find one with search_library_symbol and read it with get_symbol_primitives) and any instructions from the user. Keep pins on a 100 mil grid so wires connect to them."""
        
        # Return the rules with a message about how to modify them
        result = {
            "success": True,
            "message": f"Modify {rules_file_path} with custom symbol placement instructions",
            "rules": rules_content,
        }

        return json.dumps(result, indent=2)

    except Exception as e:
        logger.error(f"Error reading symbol placement rules file: {e}")
        return json.dumps(
            {"success": False, "error": f"Failed to read rules file: {e!s}"}, indent=2
        )


@_tool(read_only=True)
async def get_library_symbol_reference(ctx: Context) -> str:
    """
    Get the currently open symbol from a schematic library to use as reference for creating a new symbol.
    This tool should be used before creating a new symbol to understand the structure of existing symbols.

    Returns:
        str: JSON object with the reference symbol data including pins, their types, positions, and orientations
    """
    logger.info("Getting library symbol reference data")

    # Execute the command in Altium to get symbol reference data
    response = await altium_bridge.execute_command("get_library_symbol_reference", {})

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting symbol reference: {error_msg}")
        return json.dumps({"error": f"Failed to get symbol reference: {error_msg}"})

    # Get the symbol reference data
    symbol_data = response.get("result", {})

    if not symbol_data:
        logger.info("No symbol reference data found")
        return json.dumps(
            {
                "error": "No symbol reference data found or no symbol is currently selected in the library"
            }
        )

    logger.info("Retrieved symbol reference data")
    return json.dumps(symbol_data, indent=2)

@_tool(read_only=True)
async def search_library_symbol(ctx: Context, symbol_name: str, library_path: str = "") -> str:
    """
    Search for a symbol by name in a schematic library (.SchLib) and navigate to it.
    Supports partial name matching (case-insensitive). Returns all matches and navigates
    to the best match (exact match preferred, otherwise first partial match).

    This tool will automatically open the library file in Altium if a path is provided,
    so no SchLib needs to be open beforehand.

    Args:
        symbol_name (str): Name or partial name of the symbol to search for
        library_path (str): Full file path to the .SchLib file (e.g. "C:\\Libraries\\MyParts.SchLib").
                           The tool will open this file in Altium if it is not already open.
                           If empty, uses the currently open library.
                           If no library is open and no path is provided, ask the user for the file path.

    Returns:
        str: JSON object with search results including matches, navigated symbol, and full symbol list
    """
    logger.info(
        f"Searching for symbol: {symbol_name} in library: {library_path or '(current)'}"
    )

    # Execute the command in Altium
    params = {"symbol_name": symbol_name}
    if library_path:
        params["library_path"] = library_path

    response = await altium_bridge.execute_command("search_library_symbol", params)

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error searching for symbol: {error_msg}")
        return json.dumps({"error": f"Failed to search for symbol: {error_msg}"})

    # Get the result data
    result = response.get("result", {})

    if not result:
        logger.info("No search results returned")
        return json.dumps({"error": "No results returned from symbol search"})

    logger.info(f"Symbol search complete. Found: {result.get('found', False)}")
    return json.dumps(result, indent=2)

@_tool()
async def create_schematic_symbol(ctx: Context, symbol_name: str, description: str, pins: list, part_count: int = 1, graphics: list = None) -> str:
    r"""
    Before executing, run get_symbol_placement_rules first.

    For Altium API guidance while scripting, use the "altium-script" skill
    (ensure_altium_script_skill reports whether it is installed).

    Also look for a similar existing symbol to use as a style reference
    before drawing: search a company/library .SchLib for a comparable part
    (same category - op-amp, comparator, MCU, regulator, diode...) with
    search_library_symbol, then dump it with get_symbol_primitives and
    mirror its conventions (body style, pin lengths, pin name visibility,
    grid spacing, glyph shapes). This produces symbols consistent with the
    user's library. If no symbol library is available to reference, that is
    fine - skip this step and proceed with the defaults below; do not treat
    a missing reference as an error.

    Create a new schematic symbol in the current library with the specified pins
    Instructions: follow the rules from get_symbol_placement_rules and the
                  reference symbol's conventions; keep pins on a 100 mil grid

    Pin name inversion/overbar: To show an overbar on a pin name (for active-low signals),
                  place a backslash after EACH character that should be overbarred.
                  Examples: R\E\S\E\T\ renders as RESET with overbar.
                           C\S\/A0 renders as CS with overbar followed by /A0 without overbar.
                  Do NOT use ~{...} or other notation — only the backslash-per-character format works in Altium.

    Args:
        symbol_name (str): Name of the symbol to create
        description (str): Description of the schematic symbol
        pins (list): List of pin data in format
                    "pin_number|pin_name|pin_type|pin_orientation|x|y[|owner_part_id[|length[|show_name[|show_designator]]]]"
                    Pin types: eElectricHiZ, eElectricInput, eElectricIO, eElectricOpenCollector,
                               eElectricOpenEmitter, eElectricOutput, eElectricPassive, eElectricPower
                    Pin orientations: eRotate0 (right), eRotate90 (up), eRotate180 (left), eRotate270 (down)
                    x, y is where the pin meets the body; the pin extends from there
                    in its orientation's direction (left-side pins eRotate180, top pins eRotate90)
                    X,Y coordinates in mils
                    owner_part_id (optional): Part number the pin belongs to (1-based).
                               Use 0 for pins shared across all parts (e.g. power/GND).
                               Defaults to 1 if omitted. Only needed for multi-part symbols.
                    length (optional): pin length in mils (default 300)
                    show_name / show_designator (optional): 1 or 0 to show/hide
                               the pin name / number (e.g. op-amp pins often hide names)
        part_count (int): Number of parts in the symbol (default 1).
                         Use >1 for multi-part symbols like quad op-amps or hex buffers.
        graphics (list, optional): Explicit body graphics. When given, the
                    default auto-sized body rectangle is NOT drawn - the
                    graphics fully define the symbol body (triangles for
                    op-amps, diode glyphs, etc.). Entry formats (coordinates
                    in mils; part = owner part id, 1-based; width 0-3 =
                    zero/small/medium/large; solid 1 or 0):
                    - "line|part|width|x1|y1|x2|y2"
                    - "polyline|part|width|x1|y1|x2|y2|..." (any number of vertices)
                    - "polygon|part|width|solid|x1|y1|x2|y2|..." (closed/filled shape)
                    - "rectangle|part|width|solid|x1|y1|x2|y2"
                    - "arc|part|width|cx|cy|radius|start_angle|end_angle" (degrees CCW from 3 o'clock)
                    - "elliptical_arc|part|width|cx|cy|radius|secondary_radius|start_angle|end_angle"
                    - "ellipse|part|width|solid|cx|cy|radius|secondary_radius"
                    - "label|part|x|y|text" (free text annotation)
                    Tip: to reproduce an existing symbol's style, dump it first
                    with get_symbol_primitives and mirror its primitives.

    Returns:
        str: JSON object with the result of the component creation
    """
    logger.info(
        f"Creating schematic symbol: {symbol_name} with {len(pins)} pins, {part_count} part(s)"
    )

    params = {
        "symbol_name": symbol_name,
        "description": description,
        "part_count": part_count,
        "pins": pins,
    }
    if graphics:
        params["graphics"] = graphics

    # Execute the command in Altium to create the symbol
    response = await altium_bridge.execute_command("create_schematic_symbol", params)

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error creating symbol: {error_msg}")
        return json.dumps(
            {"success": False, "error": f"Failed to create symbol: {error_msg}"}
        )

    # Get the result data
    result = response.get("result", {})

    logger.info(f"Symbol {symbol_name} created successfully with {len(pins)} pins")
    return json.dumps(result, indent=2)

@_tool(read_only=True)
async def get_schematic_data(ctx: Context, cmp_designators: list) -> str:
    """
    Get schematic data for components in Altium (or connectors in harness wiring documents).

    Args:
        cmp_designators (list): List of designators of the components (e.g., ["R1", "C5", "P1"])

    Returns:
        str: JSON object with schematic/connector component data for requested designators
    """
    logger.info(f"Getting schematic data for components: {cmp_designators}")

    # Execute the command in Altium to get schematic data
    response = await altium_bridge.execute_command(
        "get_schematic_data",
        {},  # No parameters needed for this command in the Altium script
    )

    # Check for success
    if not response.get("success", False) or not response.get("result"):
        h_path = await _get_active_harness_path()
        if h_path:
            h_components = get_harness_schematic_data(h_path, cmp_designators)
            if h_components:
                logger.info(
                    f"Retrieved {len(h_components)} harness schematic components as fallback"
                )
                return json.dumps(
                    {"components": h_components, **_disk_source(h_path)},
                    indent=2,
                    ensure_ascii=False,
                )
        if not response.get("success", False):
            error_msg = response.get("error", "Unknown error")
            logger.error(f"Error getting schematic data: {error_msg}")
            return json.dumps({"error": f"Failed to get schematic data: {error_msg}"})

    # Get the schematic data
    schematic_data = response.get("result", [])

    if not schematic_data:
        h_path = await _get_active_harness_path()
        if h_path:
            h_components = get_harness_schematic_data(h_path, cmp_designators)
            if h_components:
                return json.dumps(
                    {"components": h_components, **_disk_source(h_path)},
                    indent=2,
                    ensure_ascii=False,
                )
        logger.info("No schematic data found")
        return json.dumps({"error": "No schematic data found"})

    try:
        # Parse the data if it's a string
        if isinstance(schematic_data, str):
            schematic_list = json.loads(schematic_data)
        else:
            schematic_list = schematic_data

        # Filter components by designator
        components = []
        missing_designators = []

        for designator in cmp_designators:
            found = False
            for component in schematic_list:
                if component.get("designator") == designator:
                    components.append(component)
                    found = True
                    break

            if not found:
                missing_designators.append(designator)

        if not components and missing_designators:
            h_path = await _get_active_harness_path()
            if h_path:
                h_components = get_harness_schematic_data(h_path, cmp_designators)
                if h_components:
                    return json.dumps(
                        {"components": h_components, **_disk_source(h_path)},
                        indent=2,
                        ensure_ascii=False,
                    )

        result = {
            "components": components,
        }

        if missing_designators:
            result["missing_designators"] = missing_designators
            logger.info(
                f"Some designators not found in schematic data: {missing_designators}"
            )

        logger.info(f"Found schematic data for {len(components)} components")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error processing schematic data: {e}")
        return json.dumps({"error": f"Failed to process schematic data: {str(e)}"})
    
@_tool(read_only=True)
async def get_pcb_layers(ctx: Context) -> str:
    """
    Get detailed information about all layers in the current Altium PCB

    Returns:
        str: JSON object with detailed layer information including copper layers,
             mechanical layers, and special layers with their properties
    """
    logger.info("Getting detailed PCB layer information")

    # Execute the command in Altium to get all layers data
    response = await altium_bridge.execute_command(
        "get_pcb_layers",
        {},  # No parameters needed
    )

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("get_pcb_layers", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error getting PCB layers: {error_msg}")
        return json.dumps({"error": f"Failed to get PCB layers: {error_msg}"})

    # Get the layers data
    layers_data = response.get("result", [])

    if not layers_data:
        logger.info("No PCB layers found")
        return json.dumps({"message": "No PCB layers found in the current document"})

    logger.info("Retrieved PCB layers data")
    return json.dumps(layers_data, indent=2)

@_tool()
async def set_pcb_layer_visibility(ctx: Context, layer_names: list, visible: bool, exclusive: bool = True) -> str:
    """
    Set visibility for specified PCB layers

    Showing layers is exclusive by default: the listed layers are shown and
    every other layer is hidden, which suits a screenshot of just those
    layers. Pass exclusive=False to show the listed layers and leave the rest
    as they are. Hiding only ever hides the listed layers.

    Args:
        layer_names (list): List of layer names to modify (e.g., ["Top Layer", "Bottom Layer", "Mechanical 1"])
        visible (bool): Whether to show (True) or hide (False) the specified layers
        exclusive (bool): With visible=True, hide every layer not listed (default True)
        
    Returns:
        str: JSON object with the result of the operation
    """
    logger.info(f"Setting layers visibility: {layer_names} to {visible}")

    # Execute the command in Altium to set layer visibility
    response = await altium_bridge.execute_command(
        "set_pcb_layer_visibility",
        {
            "layer_names": layer_names,
            "visible": visible,
            "exclusive": exclusive
        }
    )

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("set_pcb_layer_visibility", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error setting layer visibility: {error_msg}")
        return json.dumps(
            {"success": False, "error": f"Failed to set layer visibility: {error_msg}"}
        )

    # Get the result data
    result = response.get("result", {})

    logger.info("Layer visibility set successfully")
    return json.dumps(result, indent=2)

@_tool(read_only=True)
async def get_component_data(ctx: Context, cmp_designators: list) -> str:
    """
    Get all data for components in Altium PCB/Schematic or harness design.

    Args:
        cmp_designators (list): List of designators of the components (e.g., ["R1", "C5", "P1"])

    Returns:
        str: JSON object with all component data for requested designators
    """
    logger.info(f"Getting data for components: {cmp_designators}")

    # Execute the command in Altium to get all component data
    response = await altium_bridge.execute_command(
        "get_all_component_data",
        {},  # No parameters needed for this command in the Altium script
    )

    # Check for success
    if not response.get("success", False) or not response.get("result"):
        h_path = await _get_active_harness_path()
        if h_path:
            h_data = get_harness_component_data(h_path, cmp_designators)
            if h_data.get("components"):
                logger.info(
                    f"Retrieved {len(h_data['components'])} harness components as fallback"
                )
                return json.dumps(
                    {**h_data, **_disk_source(h_path)}, indent=2, ensure_ascii=False
                )
        if not response.get("success", False):
            error_msg = response.get("error", "Unknown error")
            logger.error(f"Error getting component data: {error_msg}")
            return json.dumps({"error": f"Failed to get component data: {error_msg}"})

    # Get the component data
    component_data = response.get("result", [])

    if not component_data:
        h_path = await _get_active_harness_path()
        if h_path:
            h_data = get_harness_component_data(h_path, cmp_designators)
            if h_data.get("components"):
                return json.dumps(
                    {**h_data, **_disk_source(h_path)}, indent=2, ensure_ascii=False
                )
        logger.info("No component data found")
        return json.dumps({"error": "No component data found"})

    try:
        # Parse the data if it's a string
        if isinstance(component_data, str):
            component_list = json.loads(component_data)
        else:
            component_list = component_data

        # Filter components by designator
        components = []
        missing_designators = []

        for designator in cmp_designators:
            found = False
            for component in component_list:
                if component.get("designator") == designator:
                    components.append(component)
                    found = True
                    break

            if not found:
                missing_designators.append(designator)

        if not components and missing_designators:
            h_path = await _get_active_harness_path()
            if h_path:
                h_data = get_harness_component_data(h_path, cmp_designators)
                if h_data.get("components"):
                    return json.dumps(
                        {**h_data, **_disk_source(h_path)}, indent=2, ensure_ascii=False
                    )

        result = {
            "components": components,
        }

        if missing_designators:
            result["missing_designators"] = missing_designators
            logger.info(f"Some designators not found: {missing_designators}")

        logger.info(f"Found data for {len(components)} components")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error processing component data: {e}")
        return json.dumps({"error": f"Failed to process component data: {e!s}"})


@_tool(read_only=True)
async def get_selected_components_coordinates(ctx: Context) -> str:
    """
    Get coordinates and positioning information for selected components in Altium layout

    Returns:
        str: JSON array with positioning data (designator, x, y, rotation, width, height)
    """
    logger.info("Getting coordinates for selected components")

    # Execute the command in Altium to get selected components coordinates
    response = await altium_bridge.execute_command(
        "get_selected_components_coordinates",
        {},  # No parameters needed
    )

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error(
            "get_selected_components_coordinates", error_msg
        )
        if h_err:
            return h_err
        logger.error(f"Error getting selected components coordinates: {error_msg}")
        return json.dumps(
            {"error": f"Failed to get selected components coordinates: {error_msg}"}
        )

    # Get the components coordinates data
    components_coords = response.get("result", [])

    if not components_coords:
        logger.info("No selected components found")
        return json.dumps(
            {"message": "No components are currently selected in the layout"}
        )

    logger.info("Retrieved positioning data for selected components")
    return json.dumps(components_coords, indent=2)

@_tool(read_only=True)
async def get_all_designators(ctx: Context) -> str:
    """
    Get all component designators from the current Altium board or harness

    Returns:
        str: JSON array of all component designators on the current board/harness
    """
    logger.info("Getting all component designators")

    # Execute the command in Altium to get all component data
    response = await altium_bridge.execute_command(
        "get_all_component_data",
        {},  # No parameters needed
    )

    # Check for success
    if not response.get("success", False) or not response.get("result"):
        h_path = await _get_active_harness_path()
        if h_path:
            h_designators = get_harness_designators(h_path)
            if h_designators:
                logger.info(
                    f"Retrieved {len(h_designators)} harness designators as fallback"
                )
                return json.dumps(h_designators)
        if not response.get("success", False):
            error_msg = response.get("error", "Unknown error")
            logger.error(f"Error getting component data: {error_msg}")
            return json.dumps({"error": f"Failed to get component data: {error_msg}"})

    # Get the component data
    component_data = response.get("result", [])

    if not component_data:
        h_path = await _get_active_harness_path()
        if h_path:
            h_designators = get_harness_designators(h_path)
            if h_designators:
                return json.dumps(h_designators)
        logger.info("No component data found")
        return json.dumps({"error": "No component data found"})

    try:
        # Parse the data if it's a string
        if isinstance(component_data, str):
            component_list = json.loads(component_data)
        else:
            component_list = component_data

        # Extract designators
        designators = [
            comp.get("designator") for comp in component_list if "designator" in comp
        ]
        if not designators:
            h_path = await _get_active_harness_path()
            if h_path:
                h_designators = get_harness_designators(h_path)
                if h_designators:
                    return json.dumps(h_designators)

        logger.info(f"Found {len(designators)} designators")
        return json.dumps(designators)
    except Exception as e:
        logger.error(f"Error processing component data: {e}")
        return json.dumps({"error": f"Failed to process component data: {e!s}"})


@_tool(read_only=True)
async def get_component_pins(ctx: Context, cmp_designators: list) -> str:
    """
    Get pin data for components in Altium PCB/Schematic, or connectors in harness wiring diagrams.

    Args:
        cmp_designators (list): List of designators of the components (e.g., ["R1", "C5", "P1"])

    Returns:
        str: JSON array, one entry per component with pins list
    """
    logger.info(f"Getting pin data for components: {cmp_designators}")

    # Execute the command in Altium to get pin data
    response = await altium_bridge.execute_command(
        "get_component_pins",
        {"designators": cmp_designators},  # Pass the list of designators
    )

    # Check for success
    if not response.get("success", False) or not response.get("result"):
        h_path = await _get_active_harness_path()
        if h_path:
            h_pins = get_harness_component_pins(h_path, cmp_designators)
            if h_pins:
                logger.info(
                    f"Retrieved pin data for {len(h_pins)} harness components as fallback"
                )
                return json.dumps(h_pins, indent=2, ensure_ascii=False)
        if not response.get("success", False):
            error_msg = response.get("error", "Unknown error")
            logger.error(f"Error getting pin data: {error_msg}")
            return json.dumps({"error": f"Failed to get pin data: {error_msg}"})

    # Get the components pins data
    pins_data = response.get("result", [])

    if not pins_data:
        h_path = await _get_active_harness_path()
        if h_path:
            h_pins = get_harness_component_pins(h_path, cmp_designators)
            if h_pins:
                return json.dumps(h_pins, indent=2, ensure_ascii=False)
        logger.info(f"No pin data found for designators: {cmp_designators}")
        return json.dumps({"message": "No pin data found for the specified components"})

    logger.info("Retrieved pin data for components")
    return json.dumps(pins_data, indent=2)


SANDBOX_DIR = MCP_DIR / "SandboxScript"
SANDBOX_PAS = SANDBOX_DIR / "Sandbox.pas"
# Run files live in the exchange folder: Altium runs outside Claude Desktop's
# MSIX package and cannot see the virtualized AppData path (issue #19)
SANDBOX_RUNS = EXCHANGE_DIR / "runs"
# Pictures of the dialogs a run answered (their text is often not readable)
SANDBOX_DIALOGS = EXCHANGE_DIR / "dialogs"


def _window_process_name(hwnd) -> str:
    """Executable file name of the process that owns a window ('' if unknown)."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return ""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(
        0x1000, False, pid.value
    )  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _dismiss_altium_dialogs(message_boxes_only: bool = False):
    """Close Altium modal popups that would otherwise block a script run.

    Altium uses two kinds: Win32 task dialogs (#32770) and Delphi TMessageForm
    error/warning boxes. It also shows "Select Item To Run"
    (TForm_RunScriptParameters) when it cannot find the script project; that
    one stays open and blocks every later script run. Only windows owned by
    the Altium process (X2.EXE) are touched, so dialogs of other applications
    are never closed. With message_boxes_only, only the OK-only Error and
    Information boxes are closed: the ones a refused script launch raises.

    Returns (handle, class name, title) of the windows that were closed, so
    the caller can tell the user (a closed dialog may have been one the user
    had open).
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value not in ("#32770", "TMessageForm", "TForm_RunScriptParameters"):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if cls.value == "TMessageForm" and title not in (
            "Error",
            "Warning",
            "Information",
            "Confirm",
        ):
            return True
        if message_boxes_only and (
            cls.value != "TMessageForm" or title not in ("Error", "Information")
        ):
            return True
        if _window_process_name(hwnd).lower() != "x2.exe":
            return True
        found.append((hwnd, cls.value, title))
        return True

    user32.EnumWindows(cb, 0)
    for h, *_ in found:
        user32.PostMessageW(h, 0x0010, 0, 0)
    return found


def _sandbox() -> sandbox.Sandbox:
    return sandbox.Sandbox(
        altium_exe=altium_bridge.config.altium_exe_path,
        template=SANDBOX_PAS,
        runs_dir=SANDBOX_RUNS,
    )


@_tool()
async def run_altium_script(ctx: Context, script: str, timeout_seconds: int = 120,
                            skip_lint: bool = False) -> str:
    """
    Run a DelphiScript snippet inside an isolated Altium sandbox and report
    what happened, step by step.

    Use this to develop and verify Altium API code before relying on it.
    Each call writes the script into a fresh copy of SandboxScript/Sandbox.pas
    with a unique file name and runs it with ScriptingSystem:RunScriptFile, so
    a script that failed earlier can never be re-run from Altium's editor.
    When Altium reports a compile or run-time error, the tool returns Altium's
    message with the line in YOUR script, answers the dialog, stops the
    debugger and confirms with a probe run that the next call will work. No
    manual Ctrl+F3 or -REditScript:Stop is needed. Only scripts the MCP
    started are ever stopped: a script the user is running or debugging in
    Altium is left alone, and the tool says Altium is busy instead.

    Writing the script:
    - Call SandboxLog('...') before each risky statement. The log is flushed
      after every call, so the last logged line identifies what failed.
    - Assign findings to the string variable ResultText - it is returned.
    - Variables: either start the script with your own block, e.g.
          var
              Doc, Comp : IDispatch;
              X, Y : Integer;
      or reuse the scratch variables S1..S9 (String), I1..I9 and B1..B3
      (Integer), F1..F4 (Double), Obj1..Obj9 (IDispatch), List1..List3
      (TStringList, created empty by the sandbox - reuse with .Clear, never
      Free them), IntMan, DbDoc. Assigning an undeclared plain name is
      refused before Altium is called. Do not assign to a for-loop variable
      inside its loop (compile error).
    - Calls known to crash Altium (GetState_DescriptionString, reading
      .Corner or .BoundingRectangle through an IDispatch variable, ...) and
      a PreProcess without PostProcess are refused before Altium is called:
      a script that dies between PreProcess and PostProcess leaves Altium's
      undo history broken until Altium is restarted.
    - Every dialog the tool answers is saved as a picture first, listed in
      "dialog_snapshots": Altium's own message boxes cannot be read from
      outside, so open the picture to see the message.
    - Helpers: MM(mm) converts millimetres to internal units;
      OpenSchDoc(path) returns the ISch_Document, opening the file if needed
      (GetSchDocumentByPath alone returns nil for a closed file, and a call on
      nil stops the script; a .WirDoc / .LdrDoc opens as a harness document,
      once its harness project is open); SaveDoc(path) marks a document
      modified and saves it.
    - Errors inside the script (undeclared identifiers, calls on nil, bad
      conversions) are caught by the sandbox and come back as "script error"
      with Altium's message and the last step; no try/except is needed.
    - Loops: a running script cannot be stopped from outside, so a loop that
      never ends would freeze Altium until it is killed. Every while and
      repeat loop is therefore guarded: about 10 s before timeout_seconds all
      loops exit, the rest of the script still runs (so a PostProcess after
      the loop is not skipped), SaveDoc no longer saves, and the tool returns
      "loop guard" with the loop's line. Make loops that delete or retry
      objects bounded anyway (a fixed count, never the same object twice).
      for loops are not guarded.
    - The sandbox is standalone: helpers and constants from the production
      units (TrimJSON, AddJSONProperty, ...) are NOT available; REPLACEALL is.
    - Undo: wrap each edit in SchServer.ProcessControl.PreProcess(Doc, '') /
      PostProcess(Doc, '') (PCBServer.PreProcess / PostProcess on a PCB), and
      report changes to the robot manager (SCHM_PrimitiveRegistration for a new
      object, SCHM_BeginModify / SCHM_EndModify around a change). An undo group
      left open makes the user's own later edits in that document impossible
      to undo until Altium restarts, so the sandbox closes any group the script
      leaves open, even after an error, and reports it as
      "undo_groups_closed": fix the script when you see it.
    - Never register objects into a library document and never write to shared
      or network library paths. Verify the target document kind first
      (ObjectID 32 = schematic, 33 = symbol library).
    - Copper: add routed tracks with route_track instead of computing segment
      ends here, and run check_routing after moving tracks, vias or parts.

    For API guidance - interfaces, object models, worked examples - use the
    "altium-script" skill. ensure_altium_script_skill reports whether that
    skill is installed and can install it.

    Args:
        script (str): DelphiScript statements to execute (body only, optionally
            preceded by a var block).
        timeout_seconds (int): How long to wait for completion (default 120).
        skip_lint (bool): Run even though the known-crash check refused the
            script. Only for a false positive.

    Returns:
        str: JSON with success, the step log and the script's ResultText; on
             failure Altium's error message and its location in the script,
             the last step reached, and whether the executor was recovered.
    """
    logger.info(f"run_altium_script: {len(script.splitlines())} lines")

    if not SANDBOX_PAS.exists():
        return json.dumps(
            {"success": False, "error": f"sandbox template missing: {SANDBOX_PAS}"}
        )

    problem = altium_limit_problem()
    if problem:
        return json.dumps({"success": False, "error": problem})

    split = sandbox.split_var_block(script)
    problems = sandbox.check_script(
        split.body, sandbox.declared_names(split.decls), split.body_offset
    )
    if problems:
        return json.dumps({"success": False,
                           "error": "refused before execution: the script would not compile",
                           "problems": problems, "executor_wedged": False}, indent=2)
    if not skip_lint:
        problems = sandbox.lint_script(split.body, split.body_offset, split.decls)
        if problems:
            return json.dumps({"success": False,
                               "error": "refused before execution: the script uses a call known to "
                                        "crash Altium or leaves a document edit open",
                               "problems": problems,
                               "note": "Altium was not touched. Pass skip_lint=True only for a false positive.",
                               "executor_wedged": False}, indent=2)

    problem = await altium_bridge.ensure_paths(need_script=False)
    if problem:
        return json.dumps({"success": False, "error": problem, "executor_wedged": False}, indent=2)

    if await asyncio.to_thread(sandbox.altium_hung):
        return json.dumps({"success": False, "error": ALTIUM_HUNG_ERROR, "executor_wedged": False}, indent=2)

    sb = _sandbox()
    report = {}
    # A script-error dialog left open blocks every new run. Answer it only
    # when a script of ours is stuck; one from the user's own script is theirs.
    if any(d.is_script_error for d in sandbox.altium_dialogs()):
        if not STUCK_MARKER.exists():
            return json.dumps(
                {
                    "success": False,
                    "error": "not run: Altium is showing a script error dialog from a script the MCP "
                    "did not start. Answer or close it in Altium, then retry.",
                    "executor_wedged": False,
                },
                indent=2,
            )
        report["recovered_before_run"] = await asyncio.to_thread(sb.recover)
        if report["recovered_before_run"]["recovered"]:
            _clear_script_stuck()

    # The loops stop early enough for the script to finish and its result to
    # arrive before this tool gives up on it
    guard_seconds = max(5, timeout_seconds - 10)
    try:
        files = sb.new_run_files()
        built = sb.write(files, script, guard_seconds)
    except Exception as e:
        return json.dumps(
            {"success": False, "error": f"could not write the run file: {e}"}
        )
    sb.launch(files)
    report["run_file"] = str(files.script)

    start = time.time()
    # Keyed by window: a dialog that ignores WM_CLOSE (Yes/No boxes do) is
    # found again on every pass and must be reported once
    dismissed = {}
    snapshots = {}
    error_dialog = None

    def snapshot(d):
        if d.hwnd not in snapshots:
            try:
                snapshots[d.hwnd] = sandbox.snapshot_dialog(d, SANDBOX_DIALOGS)
            except Exception as e:
                logger.warning(f"dialog snapshot failed: {e}")
                snapshots[d.hwnd] = None

    while not files.result.exists() and time.time() - start < timeout_seconds:
        await asyncio.sleep(0.5)
        for d in sandbox.altium_dialogs():
            # A syntax error stops the script before 'sandbox start' is
            # logged and shows Altium's Error box instead of the
            # "Continue execution?" dialog
            if d.is_script_error or (
                d.cls == "TMessageForm"
                and d.title == "Error"
                and not files.log.exists()
            ):
                error_dialog = d
                snapshot(d)
                sandbox.answer_dialog(d)
                break
            if time.time() - start > 6:
                snapshot(d)
                sandbox.answer_dialog(d)
                dismissed[d.hwnd] = d
        if error_dialog:
            break

    report["dialogs_dismissed"] = len(dismissed)
    report["dismissed_dialog_titles"] = [d.title for d in dismissed.values()]
    report["dialog_snapshots"] = [p for p in snapshots.values() if p]
    steps = (sandbox.read_when_ready(files.log, read_altium_text).splitlines()
             if files.log.exists() else [])

    if files.result.exists():
        _clear_script_stuck()  # our script ran to the end: Altium is free
        result_text = sandbox.read_when_ready(files.result, read_altium_text).strip()
        sb.prune()
        closed = [s[len(sandbox.UNDO_PREFIX):] for s in steps if s.startswith(sandbox.UNDO_PREFIX)]
        if closed:
            report["undo_groups_closed"] = closed
            report["undo_warning"] = (
                "The script called PreProcess without a matching PostProcess (or failed between "
                "them). The sandbox closed the group so the user can still undo their own edits "
                "in that document; pair every PreProcess with a PostProcess.")
        guard = next((s for s in steps if s.startswith(sandbox.GUARD_PREFIX)), None)
        if guard is not None:
            m = re.search(r"line (\d+)", guard)
            return json.dumps({
                "success": False,
                "error": "loop guard",
                "altium_message": guard[len(sandbox.GUARD_PREFIX):],
                "line": int(m.group(1)) if m else None,
                "diagnosis": f"A while or repeat loop was still running after {guard_seconds} s, "
                             "so every loop was cut short and the script ran to its end; Altium is "
                             "free again. Statements after the loops ran on partial results and "
                             "SaveDoc did not save: check the document before saving it. Make the "
                             "loop bounded (a fixed count, never retrying the same object).",
                "result": result_text, "steps": steps, "executor_wedged": False, **report}, indent=2)
        exc = next((s for s in steps if s.startswith(sandbox.EXCEPTION_PREFIX)), None)
        if exc is not None:
            before = steps[: steps.index(exc)]
            return json.dumps(
                {
                    "success": False,
                    "error": "script error",
                    "altium_message": exc[len(sandbox.EXCEPTION_PREFIX) :],
                    "last_step_reached": before[-1] if before else None,
                    "diagnosis": "The statement after 'last_step_reached' failed (a call on nil is "
                    "reported as 'Undeclared identifier: <method>'). The script was "
                    "stopped cleanly; the executor is not wedged.",
                    "steps": steps,
                    "executor_wedged": False,
                    **report,
                },
                indent=2,
            )
        return json.dumps(
            {"success": True, "result": result_text, "steps": steps, **report}, indent=2
        )

    if error_dialog:
        # The dialog came from our run, so the stuck script is ours
        _mark_script_stuck("run_altium_script")
        recovery = await asyncio.to_thread(sb.recover)
        if recovery["recovered"]:
            _clear_script_stuck()
            # The script stopped before its end, so the template could not
            # close the undo groups it had opened; a group left open makes
            # the user's own edits in that document impossible to undo
            closed = await asyncio.to_thread(sb.close_leftover_undo, files)
            if closed:
                report["undo_groups_closed"] = closed
        started = bool(steps)
        return json.dumps({
            "success": False,
            "error": "run-time error" if started else "compile error",
            "altium_message": error_dialog.text or (
                "Altium showed an Error box before the script started; its text cannot be read "
                "from outside Altium - open the picture in 'dialog_snapshots'. This is usually "
                "a syntax error (for example a construct DelphiScript does not support, such "
                "as 'on E: Exception do')."),
            "location": sandbox.map_error_line(built, error_dialog.text),
            "last_step_reached": steps[-1] if steps else None,
            "diagnosis": ("The script started; the statement at 'location' failed while running "
                          "(a call on nil is reported as 'Undeclared identifier: <method>')."
                          if started else "The script did not compile; nothing in it ran."),
            "steps": steps,
            "executor_wedged": not recovery["recovered"],
            "recovery": recovery, **report}, indent=2)

    if any(d.cls == "TForm_RunScriptParameters" for d in dismissed.values()):
        return json.dumps(
            {
                "success": False,
                "error": "Altium could not find the run file",
                "diagnosis": f"Altium was asked to run {files.script} and showed 'Select Item To "
                "Run' instead, so it cannot open that path.",
                "executor_wedged": False,
                **report,
            },
            indent=2,
        )

    # Still running: Stop and the recovery probe are window messages Altium
    # is not reading, and each probe launch would only queue up behind it
    if steps and await asyncio.to_thread(sandbox.altium_hung):
        _mark_script_stuck("run_altium_script")
        return json.dumps({
            "success": False,
            "error": f"no result after {timeout_seconds} s: the script is still running and "
                     "Altium is not responding",
            "last_step_reached": steps[-1],
            "diagnosis": f"The script's while/repeat loops stop at {guard_seconds} s, so the time "
                         "is going into a for loop or a single long Altium call. A running script "
                         "cannot be stopped from outside. Wait for Altium; if it stays frozen, only "
                         "ending X2.EXE frees it (unsaved work is lost). Do not retry until "
                         "Altium responds.",
            "steps": steps, "executor_wedged": True, **report}, indent=2)

    # No result and no error dialog. If our script started, it is ours: stop
    # it. If it never started, something else holds Altium - possibly a
    # script the user is running or debugging - so stop only when a script
    # of ours is known to be stuck.
    if steps or STUCK_MARKER.exists():
        _mark_script_stuck("run_altium_script")
        recovery = await asyncio.to_thread(sb.recover)
        if recovery["recovered"]:
            _clear_script_stuck()
        return json.dumps(
            {
                "success": False,
                "error": f"no result after {timeout_seconds} s",
                "last_step_reached": steps[-1] if steps else None,
                "diagnosis": (
                    "The script started but did not finish: it was still running or "
                    "stopped without a dialog. The debugger has been stopped, so it is no "
                    "longer running."
                    if steps
                    else "Altium never started the script: a script of ours was stuck and has "
                    "been stopped."
                ),
                "steps": steps,
                "executor_wedged": not recovery["recovered"],
                "recovery": recovery,
                **report,
            },
            indent=2,
        )
    return json.dumps(
        {
            "success": False,
            "error": f"no result after {timeout_seconds} s: Altium never started the script",
            "diagnosis": "Altium may be busy, showing a dialog, or running or debugging a script of "
            "the user's own. Nothing was stopped.",
            "steps": steps,
            **report,
        },
        indent=2,
    )


@_tool()
async def ensure_altium_script_skill(ctx: Context, install: bool = False) -> str:
    """
    Check whether the "altium-script" skill is installed, and optionally
    install it.

    That skill documents the Altium DelphiScript API - interfaces, object
    models, worked examples, conventions, and how to discover undocumented
    processes - and is the reference to consult before writing scripts for
    run_altium_script or debugging Altium API calls.

    Source: https://github.com/coffeenmusic/altium-scripts-skill

    Installing writes into the user's skills directory, so it only happens when
    install=True is passed explicitly. Skills load at client startup, so a
    newly installed skill becomes available after restarting the client.

    Args:
        install (bool): Install the skill if missing (requires git on PATH).

    Returns:
        str: JSON with installed (bool), the path checked, and the next step.
    """
    skill_dir = Path.home() / ".claude" / "skills" / "altium-script"
    repo = "https://github.com/coffeenmusic/altium-scripts-skill"

    if (skill_dir / "SKILL.md").exists():
        return json.dumps(
            {
                "installed": True,
                "path": str(skill_dir),
                "note": "Use the altium-script skill for API guidance.",
            },
            indent=2,
        )

    if not install:
        return json.dumps(
            {
                "installed": False,
                "path": str(skill_dir),
                "source": repo,
                "next_step": "Call again with install=true to clone it, or install manually "
                "into the path above.",
            },
            indent=2,
        )

    try:
        skill_dir.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", repo, str(skill_dir)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            return json.dumps(
                {
                    "installed": False,
                    "path": str(skill_dir),
                    "error": (proc.stderr or proc.stdout)[:400],
                    "hint": f"Requires git on PATH; otherwise download {repo} and extract "
                    "to the path above.",
                },
                indent=2,
            )
        return json.dumps(
            {
                "installed": True,
                "path": str(skill_dir),
                "source": repo,
                "next_step": "Restart the client so the skill is loaded.",
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps(
            {"installed": False, "path": str(skill_dir), "error": str(e)[:300]},
            indent=2,
        )


@_tool(read_only=True)
async def get_footprint_primitives(ctx: Context, library_path: str = "", footprint_name: str = "") -> str:
    """
    Read the primitives of footprints in a PCB library (.PcbLib).

    Modes:
    - footprint_name omitted: inventory of every footprint with per-type
      primitive counts (pads, tracks, arcs, fills, texts, regions, vias,
      component_bodies)
    - footprint_name given (exact, case-insensitive): full geometry dump -
      pads (position, rotation, layer, sizes/shape per stack, hole size/
      type/width/rotation, plating, paste_mode/paste_expansion,
      mask_mode/mask_expansion, locked), tracks, arcs, fills, texts,
      regions (outline vertices). Coordinates in mils relative to the
      footprint origin (also for the footprint shown in the editor, which
      Altium holds offset by the library origin); shapes and hole types as
      raw Altium enum ints; layers as names. 3D component bodies are listed
      apart in bodies_3d, not among the primitives, so the
      create_footprints_batch round-trip is unchanged: model_file,
      model_embedded, model_x/model_y (offset from the footprint origin),
      standoff_height and overall_height (mils), layer, and
      rotation_x/y/z (degrees) and model_z_offset (mils). The rotations and
      offset come from the saved library file (Altium's script API cannot
      return them); "placement" says so, and they are null for a body that
      is not saved yet.
    - footprint_name "*": full dump of every footprint

    Use as the reference when recreating or validating footprints, and to
    survey what a library requires.

    Args:
        library_path (str, optional): Full path to the .PcbLib. Omit to use
            the currently focused PCB library (an already-open library is
            only focused, never reloaded).
        footprint_name (str, optional): Exact footprint name, or "*".

    Returns:
        str: JSON - inventory: {library_name, footprint_count, footprints:
             [{name, description, <type counts>}]}; dump: primitives and
             bodies_3d per footprint
    """
    logger.info(
        f"Getting footprint primitives (library={library_path}, footprint={footprint_name})"
    )

    response = await altium_bridge.execute_command(
        "get_footprint_primitives",
        {"library_path": library_path, "footprint_name": footprint_name},
    )

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        return json.dumps(
            {
                "success": False,
                "error": f"Failed to get footprint primitives: {error_msg}",
            }
        )

    result = response.get("result", {})
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return result
    if isinstance(result, dict):
        pcblib_file.add_body_placements(result)
    return json.dumps(result, indent=2)

@_tool()
async def create_footprints_batch(ctx: Context, spec_file: str) -> str:
    """
    Create many PCB footprints in a single Altium script run.

    The batch equivalent of create_pcb_footprint with far broader coverage:
    through-hole and SMD pads (full pad stack, holes, slots, plating,
    rotation), tracks, arcs, fills, texts, and regions on any layer.
    Verified by exact round-trips of complete production footprint
    libraries. Prefer this for bulk imports/migrations; use
    get_footprint_primitives on an existing footprint to learn the exact
    field conventions.

    Args:
        spec_file (str): Path to a plain-text spec file, one record per line
            (coords in mils, layers as names, shapes/hole types as raw
            Altium enum ints, booleans as 1/0):
            FPLIB|<path to .PcbLib>   (optional first line: opens/focuses)
            FOOTPRINT|<name>|<description>
            EDITFOOTPRINT|<name>      add MODEL3D records to an existing
                                      footprint (never creates one; other
                                      records under it are reported and
                                      ignored)
            PAD|name|x|y|rot|layer|plated|hole_size|hole_type|hole_width|hole_rot|top_x|top_y|top_shape[|corner_pct[|mode|mid_x|mid_y|mid_shape|bot_x|bot_y|bot_shape[|paste_mode|paste_expansion|mask_mode|mask_expansion]]]
                Leave a field empty to skip it and still give a later one.
                paste_mode/mask_mode are TMaskExpansionMode ints: 1 rule,
                2 manual (with the expansion in mils). 0 (no mask) cannot
                be set on a pad by script and is reported as a problem.
                Omitted: rule. Every pad the tool creates is locked.
            TRACK|x1|y1|x2|y2|width|layer
            ARC|cx|cy|radius|start_angle|end_angle|width|layer
            FILL|x1|y1|x2|y2|rotation|layer
            TEXT|x|y|size|width|rotation|layer|mirror|ttf|text
            REGION|layer|kind|x1|y1|x2|y2|...
            MODEL3D|<STEP file>|<layer>|<rot_x>|<rot_y>|<rot_z>|<z_offset>[|<x>|<y>[|<embed 1/0>]]
                add a 3D body from a STEP file: rotations in degrees,
                z_offset (model height above the board) and x|y (offset
                from the footprint origin) in mils, embedded in the
                library unless embed is 0. Read the result back with
                get_footprint_primitives (bodies_3d); its rotations and
                z offset appear only once the library is saved.

    The library is left modified and unsaved; the user saves it.

    Returns:
        str: JSON with created and edited counts, models_added,
             primitive_errors, failed names and problems (e.g. a missing
             model file, mask mode 0, a record EDITFOOTPRINT ignores)
    """
    logger.info(f"Creating footprints batch from {spec_file}")

    response = await altium_bridge.execute_command(
        "create_footprints_batch", {"spec_file": spec_file}
    )

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        return json.dumps(
            {"success": False, "error": f"Failed batch footprint creation: {error_msg}"}
        )

    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

@_tool()
async def build_schematic(ctx: Context, parts: list, wires: list = None,
                          junctions: list = None, net_labels: list = None,
                          power_ports: list = None, notes: list = None) -> str:
    """
    Build a wired schematic on a NEW sheet from a circuit description.

    READ dev/SCHEMATIC_CONVENTIONS.md BEFORE CALLING. This tool places exactly
    what it is given; it does not lay out or correct spacing. The conventions
    file records the drafting rules that make the result readable, each one
    learned by getting it wrong. The ones that most often produce a plausible
    but wrong sheet:

      * A pin's connection point is NOT Pin.Location - it is PinLength further
        along the pin. Do not guess coordinates. Call with parts only first,
        read the returned pin_map, then call again with wires routed from those
        measured coordinates.
      * Never run a riser in a pin's connection column; it drives through every
        pin sharing that column.
      * Give a shunt part one grid of wire between its pin and the node it taps,
        rather than putting the junction on the pin.
      * A net label that does not TOUCH its wire names nothing.
      * A shunt part occupies the band below its rail. Do not route another net
        through that band - the wire will pass through the symbol body.
      * A ground port's "GND" label is always drawn (ShowNetName cannot hide it)
        and occupies ~300 mil below the port. Keep wires out of that band.

    Once the sheet is saved, run check_schematic on it and fix what it finds.

    Args:
        parts: component dicts. Required keys: designator, symbol_library
            (full path of a .SchLib file; .IntLib is not supported),
            symbol, x, y (mils). Optional: design_item_id, orientation (0-3),
            mirror (0/1), comment, description, footprint,
            parameters ({name: value}).
        wires: each a flat list of alternating x,y in mils, e.g.
            [3900, 3000, 5300, 3000, 5300, 4900] - two segments, three points.
        junctions: [{"x":..,"y":..}] - only where 3+ branches actually meet.
        net_labels: [{"x":..,"y":..,"orientation":0,"text":"LED+"}] - the
            coordinate must lie on a wire.
        power_ports: [{"x":..,"y":..,"orientation":3,"style":5,"text":"GND",
            "show_net_name":false}]. Harvest style/orientation from an existing
            sheet rather than guessing: 5 = digital ground, 2 = supply bar.
        notes: [{"x":..,"y":..,"text":".."}] free text.

        All coordinates are mils and keep their fractions, so a metric sheet
        can be drawn exactly (1 mm = 39.3700787 mil). The pin_map has the
        same precision: route from its values unrounded, or a wire ends up
        a few microns out of level and turns diagonal when its part moves.

    Returns:
        JSON with the created sheet name, counts, and the pin map - every
        placed pin's true connection point, for routing a follow-up call.
    """
    logger.info(f"Building schematic: {len(parts)} parts")

    # The spec carries Altium coords (10000 per mil). Whole mils missed a
    # metric grid by up to half a mil, and a wire that far out of level turns
    # diagonal when its part is dragged.
    def c(v):
        return str(int(round(float(v) * 10000)))

    lines = []
    for p in parts:
        for key in ("designator", "symbol_library", "symbol", "x", "y"):
            if key not in p:
                return json.dumps({"success": False,
                                   "error": f"part missing required key '{key}': {p}"})
        lines.append("PART|{}|{}|{}|{}|{}|{}|{}|{}".format(
            p["designator"], p["symbol_library"], p["symbol"],
            p.get("design_item_id", ""), c(p["x"]), c(p["y"]),
            int(p.get("orientation", 0)), 1 if p.get("mirror") else 0))
        if p.get("comment"):
            lines.append(f"COMMENT|{p['comment']}")
        if p.get("footprint"):
            lines.append(f"FOOTPRINT|{p['footprint']}")
        if p.get("description"):
            lines.append(f"DESCRIPTION|{p['description']}")
        for name, val in (p.get("parameters") or {}).items():
            lines.append(f"PARAM|{name}|{val}")

    # The bridge can only read .SchLib files, and asked to open a path that
    # does not exist Altium creates an empty library of that name instead -
    # which used to give an empty sheet reported as success (issue #19)
    for lib in sorted({str(p["symbol_library"]) for p in parts}):
        if not lib.lower().endswith(".schlib") or not os.path.isfile(lib):
            return json.dumps(
                {
                    "success": False,
                    "error": "symbol_library must be the full path of an existing "
                    ".SchLib file (integrated libraries such as .IntLib "
                    f"are not supported): {lib}",
                }
            )

    for route in wires or []:
        if len(route) < 4 or len(route) % 2:
            return json.dumps({"success": False,
                               "error": f"wire needs an even count of >=4 coords: {route}"})
        lines.append("WIRE|" + "|".join(c(v) for v in route))
    for j in (junctions or []):
        lines.append(f"JUNCTION|{c(j['x'])}|{c(j['y'])}")
    for n in (net_labels or []):
        lines.append("NETLABEL|{}|{}|{}|{}".format(
            c(n["x"]), c(n["y"]), int(n.get("orientation", 0)), n["text"]))
    for pw in (power_ports or []):
        lines.append("POWER|{}|{}|{}|{}|{}|{}".format(
            c(pw["x"]), c(pw["y"]), int(pw.get("orientation", 3)),
            int(pw.get("style", 5)), pw["text"],
            1 if pw.get("show_net_name") else 0))
    for nt in (notes or []):
        lines.append(f"NOTE|{c(nt['x'])}|{c(nt['y'])}|{nt['text']}")

    spec_path = Path("C:/Users/Public/altium_mcp/circuit_spec.txt")
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    # Part descriptions carry characters that are not plain ASCII
    write_altium_text(spec_path, "\n".join(lines) + "\n")

    response = await altium_bridge.execute_command("build_circuit", {})
    if not response.get("success", False):
        return json.dumps(
            {"success": False, "error": response.get("error", "unknown error")}
        )

    result = response.get("result", {})
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return result

    # Hand back the measured pin map so the caller can route a second pass
    pin_map = {}
    pm = Path("C:/Users/Public/altium_mcp/pin_map.txt")
    if pm.is_file():
        for line in read_altium_text(pm).splitlines():
            f = line.strip().split("|")
            if len(f) == 5 and f[0] == "PIN":
                pin_map.setdefault(f[1], {})[f[2]] = [int(f[3]) / 10000, int(f[4]) / 10000]
    result["pin_map"] = pin_map
    result["pin_map_note"] = (
        "Absolute electrical connection points. Route "
        "wires from these, not from predicted offsets."
    )
    return json.dumps(result, indent=2)


HARNESS_SPEC_FILE = EXCHANGE_DIR / "harness_wiring_spec.txt"

# Harness bridge commands that change documents: a timed-out one must not be
# retried blindly (see MUTATING_COMMANDS)
MUTATING_COMMANDS.update({"add_harness_wiring", "edit_harness_layout",
                          "convert_netlabels_to_text"})

# TSheetStyle ordinals accepted by ISch_Document.SheetStyle
SHEET_STYLES = {
    "A4": 0,
    "A3": 1,
    "A2": 2,
    "A1": 3,
    "A0": 4,
    "A": 5,
    "B": 6,
    "C": 7,
    "D": 8,
    "E": 9,
    "LETTER": 10,
    "LEGAL": 11,
    "TABLOID": 12,
}
# ...and the names get_sheet_style reports for them (OrCAD sizes are read-only)
SHEET_STYLE_NAMES = {v: k for k, v in SHEET_STYLES.items()} | {
    13: "ORCAD_A",
    14: "ORCAD_B",
    15: "ORCAD_C",
    16: "ORCAD_D",
    17: "ORCAD_E",
}
# TSheetOrientation ordinals (ISch_Document.WorkspaceOrientation)
SHEET_ORIENTATIONS = {0: "landscape", 1: "portrait"}


def _spec_line(*fields: Any) -> str:
    """One spec record. Fields are percent-encoded ('%', '|', line breaks), so
    values may contain them; the script decodes them again (SpecField)."""
    return "|".join(
        str(f)
        .replace("%", "%25")
        .replace("|", "%7C")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        for f in fields
    )


def _param_pairs(params: dict | None) -> list[tuple[str, str]]:
    """Non-empty (name, value) pairs of a parameter dict."""
    return [
        (str(k), str(v))
        for k, v in (params or {}).items()
        if v is not None and str(v).strip()
    ]


def _wire_points(item: dict) -> list[int] | None:
    pts = item.get("points", [])
    if len(pts) < 4 or len(pts) % 2 != 0:
        return None
    return [int(v) for v in pts]


# Cavity slots of a harness connector pin (ISch_HarnessCavityPartsOwner)
CAVITY_TYPES = {"crimp": 0, "terminal": 0, "seal": 1, "plug": 2, "other": 3}
CAVITY_TYPE_NAMES = {0: "crimp", 1: "seal", 2: "plug", 3: "other"}
# Harness shield styles (ISch_HarnessShield.Style)
SHIELD_STYLES = {"shield": 0, "plain": 0, "with_connection": 1, "connected": 1}
# Component kinds of a harness wire (TComponentKind); "standard_no_bom" draws a
# connection (e.g. a bus bar inside a fuse holder) that is not a BOM wire
COMPONENT_KINDS = {name: kind for kind, name in COMPONENT_KIND_NAMES.items()}
# Wire break (cross-sheet connector) styles: the arrows of "right" point away
# from the wire end, those of "left" towards it, whatever the orientation
WIRE_BREAK_STYLES = {"left": 0, "in": 0, "right": 1, "out": 1}


def _component_kind(value: Any) -> int | None:
    if isinstance(value, int) and value in COMPONENT_KIND_NAMES:
        return value
    return COMPONENT_KINDS.get(str(value).strip().lower().replace(" ", "_"))


def _wire_extra_lines(des: str, item: dict) -> list[str]:
    """WIREKIND / WIREPARAMVIS records for a wire's component_kind,
    hide_parameters and show_parameters (des "" = the wire just placed).
    Raises ValueError on an unknown component kind."""
    lines = []
    if item.get("component_kind") is not None:
        kind = _component_kind(item["component_kind"])
        if kind is None:
            raise ValueError(
                f"unknown component_kind {item['component_kind']!r}; "
                f"use one of {sorted(COMPONENT_KINDS)}"
            )
        lines.append(_spec_line("WIREKIND", des, kind))
    for name in item.get("hide_parameters") or []:
        lines.append(_spec_line("WIREPARAMVIS", des, str(name), "1"))
    for name in item.get("show_parameters") or []:
        lines.append(_spec_line("WIREPARAMVIS", des, str(name), "0"))
    return lines


# ObjectIds of the harness connectivity objects copy_objects leaves out unless
# include_wiring is set: wires, splices, no-connects, twists, shields, cables
# and wire breaks
WIRING_OBJECT_IDS = (82, 83, 94, 92, 93, 98, 106)

# remove_objects types and their REMOVE record kinds
REMOVE_KINDS = {
    "wire": "WIRE",
    "splice": "SPLICE",
    "no_connect": "NOCONNECT",
    "noconnect": "NOCONNECT",
}


def _remove_object_lines(items: list | None) -> list[str]:
    """REMOVE records for remove_objects: REMOVE|UID|<unique id>,
    REMOVE|WIRE / SPLICE / NOCONNECT|<designator or no-connect name> or
    REMOVE|TEXT|x|y|<text>."""
    lines = []
    for item in items or []:
        if not isinstance(item, dict):
            # ValueError: the caller turns it into the tool's error result
            raise ValueError(f"remove_objects entries must be objects: {item!r}")  # noqa: TRY004
        uid = str(item.get("unique_id") or "").strip()
        if uid:
            lines.append(_spec_line("REMOVE", "UID", uid))
            continue
        kind_name = str(item.get("type") or "").strip().lower()
        if kind_name == "text":
            text = str(item.get("text") or "")
            if item.get("x") is None or item.get("y") is None or not text:
                raise ValueError(
                    f"remove_objects text entry needs x, y (mils) and text: {item!r}"
                )
            lines.append(
                _spec_line("REMOVE", "TEXT", int(item["x"]), int(item["y"]), text)
            )
            continue
        kind = REMOVE_KINDS.get(kind_name)
        name = str(item.get("designator") or item.get("name") or "").strip()
        if kind is None or not name:
            raise ValueError(
                "remove_objects entry needs a unique_id, or a type (wire, splice, "
                f"no_connect) and a designator, or a text with x / y: {item!r}"
            )
        lines.append(_spec_line("REMOVE", kind, name))
    return lines


def _copy_object_lines(copies: list | None) -> list[str]:
    """COPYDES / COPYTEXT / COPYFROM records for copy_objects. The renames and
    text replacements of a copy come before its COPYFROM, which uses them up.
    Raises ValueError on a malformed entry."""
    lines = []
    for c in copies or []:
        source = str(c.get("source", "")).strip()
        region = c.get("region") or []
        offset = c.get("offset") or [0, 0]
        if not source or len(region) != 4 or len(offset) != 2:
            raise ValueError(
                "copy_objects entries need source, region [x1, y1, x2, y2] and "
                f"offset [dx, dy] (mils): {c}"
            )
        x1, y1, x2, y2 = (int(v) for v in region)
        for old, new in (c.get("designators") or {}).items():
            lines.append(_spec_line("COPYDES", str(old).strip(), str(new).strip()))
        for old, new in (c.get("replace_text") or {}).items():
            if str(old):
                lines.append(_spec_line("COPYTEXT", str(old), str(new)))
        exclude = "" if c.get("include_wiring") else ",".join(map(str, WIRING_OBJECT_IDS))
        lines.append(
            _spec_line(
                "COPYFROM",
                Path(source).resolve(),
                min(x1, x2),
                min(y1, y2),
                max(x1, x2),
                max(y1, y2),
                int(offset[0]),
                int(offset[1]),
                exclude,
            )
        )
    return lines


def _wire_break_orientation(
    x: int, y: int, document_path: str, new_wires: list | None
) -> int | None:
    """Orientation (0-3) that puts a wire break at x, y (mils) on the side
    away from the wire ending there: 0 when the wire runs off to the left,
    2 to the right, 1 down, 3 up. Wires of this call first, then the saved
    sheet; None when no wire ends there."""
    ends: list[tuple[int, int, int, int]] = []
    for w in new_wires or []:
        pts = _wire_points(w)
        if pts:
            ends += [(*pts[0:2], *pts[2:4]), (*pts[-2:], *pts[-4:-2])]
    try:
        for w in parse_wirdoc(Path(document_path).resolve()).get("wires", []):
            p = [round(v * 10) for v in w["points"]]
            if len(p) >= 4:
                ends += [(*p[0:2], *p[2:4]), (*p[-2:], *p[-4:-2])]
    except Exception as e:  # noqa: BLE001 - an unreadable sheet only loses the default
        logger.warning(f"wire break orientation: sheet not read: {e}")
    for ex, ey, nx, ny in ends:
        if (ex, ey) == (x, y):
            if nx < x:
                return 0
            if nx > x:
                return 2
            return 1 if ny < y else 3
    return None


def _cavity_type(value: Any) -> int | None:
    if isinstance(value, int) and value in CAVITY_TYPE_NAMES:
        return value
    return CAVITY_TYPES.get(
        str(value if value is not None else "crimp").strip().lower()
    )


def _part_fields(item: dict) -> list[str]:
    """PartNumber, SourceLibraryName, DatabaseTableName, Description and the
    Name/Value parameter pairs of a harness part (cavity or associated part).

    A "Description" parameter becomes the description: Altium deletes
    Description parameters on these parts whenever it saves the document.
    """
    params = dict(item.get("parameters") or {})
    desc = item.get("description") or params.pop("Description", "")
    params.pop("Description", None)
    fields = [
        str(item.get("part_number") or item.get("terminal") or "").strip(),
        str(item.get("source_library", "")).strip(),
        str(item.get("source_table", "")).strip(),
        str(desc or "").strip(),
    ]
    for pk, pv in _param_pairs(params):
        fields.extend([pk, pv])
    return fields


def _rect(item: dict) -> list[int]:
    return [int(item.get(k, 0)) for k in ("x1", "y1", "x2", "y2")]


def _rotation(item: dict) -> str:
    rot = item.get("rotation")
    return "" if rot is None else str(int(rot) % 4)


def _wire_label_positions(item: dict, pts: list[int]) -> tuple[list, list]:
    from harness_check import label_positions

    des_pos = item.get("designator_pos")
    com_pos = item.get("comment_pos")
    if not des_pos or not com_pos:
        auto_des, auto_com = label_positions(
            pts, str(item.get("label_position", "above")).lower()
        )
        des_pos = des_pos or auto_des
        com_pos = com_pos or auto_com
    return [int(v) for v in des_pos], [int(v) for v in com_pos]


@_tool()
async def add_harness_wiring(
    ctx: Context,
    document_path: str | None = None,
    wires: list | None = None,
    splices: list | None = None,
    no_connects: list | None = None,
    net_labels: list | None = None,
    labels: list | None = None,
    connectors: list | None = None,
    rectangles: list | None = None,
    parts: list | None = None,
    wire_updates: list | None = None,
    reroutes: list | None = None,
    cavity_crimps: list | None = None,
    component_updates: list | None = None,
    clear_existing: bool = False,
    sheet_style: str | None = None,
    cavity_parts: list | None = None,
    associated_parts: list | None = None,
    twists: list | None = None,
    shields: list | None = None,
    cables: list | None = None,
    wire_breaks: list | None = None,
    label_updates: list | None = None,
    relink: bool = False,
    designator_updates: list | None = None,
    clear_hidden_cavity_parts: list | None = None,
    copy_objects: list | None = None,
    remove_objects: list | None = None,
) -> str:
    """
    Add, update or remove wires, splices, no-connects, text, cavity parts, twists, shields, cables, wire breaks and parameters on a Harness Wiring Diagram (.WirDoc) or Schematic.

    document_path is required: the target is always named explicitly, matched by full path,
    and saved to disk afterwards.

    Parameters are upserted: setting a parameter that already exists updates it and removes
    any duplicates with the same name, so repeated runs never stack conflicting values.
    Any value may contain '|' or line breaks.

    The result reports what was done ("wires_updated", "parameters_set", "crimps", ...) and lists
    every wire, connector, pin or symbol that could not be found under "not_found"; "success" is
    false when anything was not found or the document could not be saved.

    Coordinates Convention:
        All coordinates (points, splices, no-connects, label positions) MUST be specified in true mils
        (1 DXP schematic unit = 10 mils, e.g., X=1640 in Altium -> 16400 in API).

    Args:
        document_path (str): Target .WirDoc / .SchDoc path. Required.
        wires (list): New wires, e.g.:
            [
                {
                    "designator": "W1",
                    "net": "SIG_A",                  # written as the wire's Comment
                    "points": [16700, 8400, 11600, 8400, 11600, 13200, 6500, 13200],
                    "parameters": {"Gauge": "22 AWG", "Color": "Orange"},
                    "tcolor": 42495,                 # optional explicit drawing colour (TColor)
                    "label_position": "above",       # "above" (default) or "below"
                    "designator_pos": [13500, 8415], # optional [x, y] in mils
                    "comment_pos": [13875, 8415]     # optional [x, y] in mils
                }
            ]
            points are alternating x, y coordinates in true mils (even length >= 4).
            Omitted label positions default to side-by-side (375 mils apart) at the midpoint of
            the longest horizontal segment.
            Optional on a wire (and on wire_updates): "component_kind" ("standard",
            "standard_no_bom" for a drawn connection that is no BOM wire, e.g. a bus bar
            link; also "mechanical", "graphical", "net_tie_bom", "net_tie_no_bom",
            "jumper"), "hide_parameters" / "show_parameters" (names, e.g. ["Net"]).
        reroutes (list): Move EXISTING wires in place (keeps UniqueID, parameters, colour and
            library link), e.g. [{"designator": "W1", "points": [...]}] with optional
            designator_pos / comment_pos / label_position as for wires.
        splices (list, optional): List of splice dicts, e.g.:
            [
                {"designator": "SP1", "x": 7800, "y": 13300}
            ]
        no_connects (list): List of no-connect dicts, e.g.:
            [
                {"x": 16700, "y": 8100, "name": "NC1", "show_name": False}
            ]
        labels (list): Text strings (ISch_Label, never net labels), e.g.:
            [
                {"x": 16700, "y": 8400, "orientation": 0, "text": "SIG_A"}
            ]
        net_labels (list): Deprecated alias of labels.
        wire_updates (list): Parameters to upsert on existing wires, and names of parameters
            to delete from them, e.g.:
            [{"designator": "W1", "parameters": {"Gauge": "22 AWG"}, "tcolor": 0,
              "delete_parameters": ["Wire Gauge"]}]
        cavity_crimps (list): Terminals to put in connector cavities, e.g.:
            [{"connector": "P1", "pin": "1", "terminal": "64900613722DEC",
              "source_library": "my.DbLib", "source_table": "terminals-db.csv",
              "parameters": {"Description": "...", "Manufacturer 1": "..."}}]
            Same as cavity_parts with type "crimp".
        cavity_parts (list): Parts in connector cavities: crimp terminal, seal, cavity plug or
            any number of "other" parts, e.g.:
            [{"connector": "P1", "pin": "3", "type": "seal", "part_number": "7158-3138",
              "source_library": "my.DbLib", "source_table": "seals-db.csv",
              "description": "...", "parameters": {"Manufacturer 1": "..."}}]
            type is "crimp" (default), "seal", "plug" or "other". The listed parts replace
            what that slot of that cavity held; "part_number": "" only empties the slot.
        associated_parts (list): Parts attached to a connector or splice (boot, backshell,
            wedgelock, heat shrink...), e.g.:
            [{"owner": "P1", "part_number": "DTM04-4P-BOOT", "source_library": "my.DbLib",
              "source_table": "accessories-db.csv", "description": "...", "parameters": {}}]
            The listed parts replace the owner's associated parts; "part_number": "" only
            removes them.
        twists (list): Twist marks; a rectangle in mils across the wires to twist, e.g.
            [{"x1": 13300, "y1": 7900, "x2": 13800, "y2": 8400, "designator": "TW1"}]
            for wires at y 8000 and 8300 (100 mils beyond each wire puts the mark's hook
            tips on them; rotation 1 for a vertical pair).
            Altium links every wire the rectangle crosses or touches. A twist (or shield)
            with the designator of an existing one is moved / resized to the new rectangle;
            sending the rectangle of an existing one with a new designator re-annotates it.
        shields (list): Shields, a rectangle across the shielded wires, e.g.
            [{"x1": 13800, "y1": 7700, "x2": 14300, "y2": 8700, "style": "with_connection",
              "rotation": 0, "designator": "SH1"}]
            style "with_connection" adds the shield's connection pin, where a drain wire
            ends: at rotation 0 the middle of the bottom edge ((x1+x2)/2, min y), rotation 1
            the middle of the right edge, 2 the top edge, 3 the left edge.
        cables (list): Cables (an outer jacket over wires), e.g.
            [{"designator": "C1", "x1": ..., "y1": ..., "x2": ..., "y2": ...,
              "comment": "BELDEN 8771", "part_number": "8771", "source_library": "my.DbLib",
              "parameters": {"Manufacturer": "Belden"}}]
            A cable with the same designator is moved/updated instead of placed again.
        wire_breaks (list): Wire breaks on free wire ends, e.g. [{"x": 14700, "y": 7700}].
            The break takes the wire's designator; wires with the same designator on other
            sheets of the harness project become one connection.
            Twists, shields, cables and wire breaks are linked to wires only when Altium loads
            a document, so after placing them (or wires on a sheet that has them) the tool
            closes and reopens the saved document ("relinked": true; the undo history is
            lost) and reports the links read back from the file under "links". A
            rectangle that links no wire or a break on no wire end is listed in not_found.
            Placing an identical rectangle (or a twist / shield / cable with the same
            designator) or a break at the same point again updates it.
            A break may give its direction: {"x", "y", "direction": "out"} (arrows away
            from the wire end) or "in" (arrows towards it); the orientation then follows
            from the wire ending there (the symbol on the side away from the wire). Or
            set "style" ("left"/"right") and "orientation" (0-3) explicitly. Sent for an
            existing break, they update it in place.
        relink (bool): Reopen the document after saving to recompute those links even when
            no geometry changed (e.g. twists copied in the GUI keep the copied links), and
            so the project compiler sees changed wire parameters and cavity parts (until
            the reload it reports "Mismatched parameters in connected wire segments").
        label_updates (list): Change existing sheet texts (eLabel), e.g.
            [{"x": 7400, "y": 8200, "text": "OLD", "new_text": "NEW"}]; x/y in mils, the
            label's location; "text" (optional) must match the current text.
        designator_updates (list): Rename existing connectors, wires, splices (Designator)
            or no-connects (name) in place, found by the unique_id get_harness_data reports,
            e.g. [{"unique_id": "QOWDFIRS", "designator": "P3"},
                  {"unique_id": "HDWLTHQJ", "designator": "NC1", "show_name": False}].
            Use it for unannotated objects ("P?", "*") that no designator can address.
            Applied before every other change of the call, so later entries may use the
            new designators. show_name only applies to no-connects.
            Multi-part connectors (each part, e.g. sockets 1-10 / 11-20 of a fuse holder,
            is its own object): renaming one part renames every part of that component
            (same designator and library reference); "part_only": True renames just it.
            Cavity parts, parameters and connector lookups by designator always use the
            part on which the pin is drawn.
        clear_hidden_cavity_parts (list): Connector designators whose pins hidden on
            their part (another part's pins, never shown) are emptied of cavity parts,
            e.g. ["FH1"]: tools that ignored the part used to put terminals there, and
            they stay in the document. get_harness_data lists them per connector under
            "hidden_pin_cavity_parts".
        copy_objects (list): Copy objects from another wiring diagram of an open harness
            project (or from this one), as Altium's copy / paste would: harness
            connectors (with pins, cavity parts and parameters), frames, texts, notes and
            pictures. Every object the source sheet owns whose bounding box lies inside
            region (mils) is copied and moved by offset (mils), e.g.
            [{"source": r"C:/.../Sheet1.WirDoc", "region": [7900, 4200, 15400, 12300],
              "offset": [-1050, 4950], "designators": {"X1": "X7", "X2": "X8"},
              "replace_text": {"#1": "#2"}}]
            designators renames copied connectors (and copied wires / splices);
            replace_text replaces text in copied texts and notes. Harness wiring
            (wires, splices, no-connects, twists, shields, cables, wire breaks) is left
            out unless "include_wiring": True. Copies get new UniqueIds (pins included)
            and are placed before the call's connector moves and wires, so those can
            use them. The result counts them under "copied".
        remove_objects (list): Delete harness wires, splices or no-connects, found by
            designator (a no-connect by its name) or by the unique_id get_harness_data
            reports, and sheet texts, found by location (mils) and text, e.g.
            [{"type": "wire", "designator": "W3"}, {"type": "splice", "designator": "SP1"},
             {"type": "no_connect", "designator": "NC9"}, {"unique_id": "QOWDFIRS"},
             {"type": "text", "x": 4300, "y": 11600, "text": "OLD NOTE"}]
            Connectors are never removed (a unique_id naming one is not_found). Removed
            before the call's copies and
            wires (which may reuse the designators), after designator_updates; the
            result counts them under "removed", a missing one is listed in not_found.
            Wires that ended on a removed splice are left with a free end: reroute or
            remove them too. A removed wire's links from a layout drawing (.LdrDoc)
            go with it; import the changes into the layout afterwards.
        component_updates (list): Parameters to upsert on placed connectors/components, and
            names of parameters to delete from them, e.g.:
            [{"designator": "P1", "parameters": {"Max Operating Temperature": "105°C"},
              "delete_parameters": ["Old Column"]}]
        clear_existing (bool, optional): If True, removes existing harness wires, splices,
            and harness no-connects (only those) before adding new ones. Defaults to False.
        sheet_style (str, optional): Change the sheet size, e.g. "D" or "A3". Left unchanged
            when omitted.

    Returns:
        str: JSON string reporting counts, saved state and not_found items.
    """
    logger.info(
        f"Adding harness wiring: {len(wires or [])} wires, "
        f"{len(reroutes or [])} reroutes, "
        f"{len(splices or [])} splices, "
        f"{len(no_connects or [])} no-connects, "
        f"{len(labels or net_labels or [])} labels"
    )

    if not document_path:
        return json.dumps({"success": False, "error": "document_path is required"})

    lines = [_spec_line("DOCUMENT", Path(document_path).resolve())]
    if clear_existing:
        lines.append("CLEAR_EXISTING")
    if sheet_style:
        style = SHEET_STYLES.get(str(sheet_style).strip().upper())
        if style is None:
            return json.dumps(
                {
                    "success": False,
                    "error": f"unknown sheet_style {sheet_style!r}; "
                    f"use one of {sorted(SHEET_STYLES)}",
                }
            )
        lines.append(_spec_line("SHEET_STYLE", style))

    for du in designator_updates or []:
        show = du.get("show_name")
        lines.append(
            _spec_line(
                "DESIGNATOR",
                str(du.get("unique_id", "")).strip(),
                str(du.get("designator", "")).strip(),
                "" if show is None else ("1" if show else "0"),
                *(["1"] if du.get("part_only") else []),
            )
        )
    for conn in clear_hidden_cavity_parts or []:
        lines.append(_spec_line("CLEARHIDDEN", str(conn).strip()))
    # Removals come before copies and wires, which may reuse their designators;
    # copies come before connector moves and wires, which may then use them
    try:
        lines.extend(_remove_object_lines(remove_objects))
        lines.extend(_copy_object_lines(copy_objects))
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)})

    for c in connectors or []:
        lines.append(
            _spec_line(
                "CONNECTOR",
                str(c.get("designator", "")).strip(),
                int(c.get("x", 0)),
                int(c.get("y", 0)),
            )
        )

    for s in splices or []:
        lines.append(
            _spec_line(
                "SPLICE",
                str(s.get("designator", "SP?")).strip(),
                int(s.get("x", 0)),
                int(s.get("y", 0)),
            )
        )

    for w in wires or []:
        pts = _wire_points(w)
        if pts is None:
            return json.dumps(
                {
                    "success": False,
                    "error": f"wire needs an even count of >=4 coords: {w}",
                }
            )
        des_pos, com_pos = _wire_label_positions(w, pts)
        lines.append(
            _spec_line(
                "WIRE",
                str(w.get("designator", "")).strip(),
                str(w.get("net", "")).strip(),
                "AT",
                *des_pos,
                *com_pos,
                *pts,
            )
        )

        params = dict(w.get("parameters", {}))
        if "gauge" in w and "Gauge" not in params:
            params["Gauge"] = w["gauge"]
        if "color" in w and "Color" not in params:
            params["Color"] = w["color"]
        if "border" in w and "Border" not in params:
            params["Border"] = w["border"]
        for pk, pv in _param_pairs(params):
            lines.append(_spec_line("WIREPARAM", pk, pv))
        if w.get("tcolor") is not None:
            lines.append(_spec_line("WIRECOLOR", "", int(w["tcolor"])))
        try:
            lines.extend(_wire_extra_lines("", w))
        except ValueError as e:
            return json.dumps({"success": False, "error": str(e)})

    for rr in reroutes or []:
        pts = _wire_points(rr)
        if pts is None:
            return json.dumps(
                {
                    "success": False,
                    "error": f"reroute needs an even count of >=4 coords: {rr}",
                }
            )
        des_pos, com_pos = _wire_label_positions(rr, pts)
        lines.append(
            _spec_line(
                "REROUTE",
                str(rr.get("designator", "")).strip(),
                "AT",
                *des_pos,
                *com_pos,
                *pts,
            )
        )

    for i, nc in enumerate(no_connects or []):
        lines.append(
            _spec_line(
                "NOCONNECT",
                int(nc.get("x", 0)),
                int(nc.get("y", 0)),
                nc.get("name") or nc.get("designator") or f"NC{i + 1}",
                "1" if nc.get("show_name", False) else "0",
            )
        )

    for nl in labels or net_labels or []:
        text = str(nl.get("text", "")).strip()
        if text:
            lines.append(
                _spec_line(
                    "LABEL",
                    int(nl.get("x", 0)),
                    int(nl.get("y", 0)),
                    int(nl.get("orientation", 0)),
                    text,
                )
            )

    for r in rectangles or []:
        lines.append(
            _spec_line(
                "RECTANGLE",
                int(r.get("x1", 0)),
                int(r.get("y1", 0)),
                int(r.get("x2", 0)),
                int(r.get("y2", 0)),
                int(r.get("line_width", 2)),
                int(r.get("color", 16711935)),
                int(r.get("area_color", 11599871)),
            )
        )

    for p in parts or []:
        lines.append(
            _spec_line(
                "PART",
                str(p.get("designator", "")).strip(),
                str(p.get("symbol_library", "")).strip(),
                str(p.get("symbol", "")).strip(),
                int(p.get("x", 0)),
                int(p.get("y", 0)),
                int(p.get("orientation", 0)),
                "1" if p.get("mirror", False) else "0",
                str(p.get("comment", "")).strip(),
                str(p.get("description", "")).strip(),
                str(p.get("footprint", "")).strip(),
            )
        )
        for pk, pv in _param_pairs(p.get("parameters")):
            lines.append(_spec_line("PARTPARAM", pk, pv))

    for wu in wire_updates or []:
        w_des = str(wu.get("designator", "")).strip()
        for pk, pv in _param_pairs(wu.get("parameters")):
            lines.append(_spec_line("UPDATE_WIREPARAM", w_des, pk, pv))
        for pk in wu.get("delete_parameters") or []:
            lines.append(_spec_line("DELETE_WIREPARAM", w_des, str(pk)))
        if wu.get("tcolor") is not None:
            lines.append(_spec_line("WIRECOLOR", w_des, int(wu["tcolor"])))
        try:
            lines.extend(_wire_extra_lines(w_des, wu))
        except ValueError as e:
            return json.dumps({"success": False, "error": str(e)})

    crimp_parts = []
    for cc in cavity_crimps or []:
        terminal = str(
            cc.get("terminal")
            or cc.get("crimp")
            or cc.get("crimp_part_number")
            or cc.get("part_number")
            or ""
        ).strip()
        if terminal:
            crimp_parts.append({**cc, "type": 0, "part_number": terminal})
    for cp in crimp_parts + list(cavity_parts or []):
        conn_des = str(cp.get("connector", "")).strip()
        pin_des = str(cp.get("pin") or cp.get("cavity", "")).strip()
        cav_type = _cavity_type(cp.get("type"))
        if cav_type is None:
            return json.dumps(
                {
                    "success": False,
                    "error": f"unknown cavity part type {cp.get('type')!r}; "
                    f"use one of {sorted(CAVITY_TYPES)}",
                }
            )
        if conn_des and pin_des:
            lines.append(
                _spec_line("CAVITYPART", conn_des, pin_des, cav_type, *_part_fields(cp))
            )

    for ap in associated_parts or []:
        owner = str(ap.get("owner") or ap.get("connector") or "").strip()
        if owner:
            lines.append(_spec_line("ASSOCPART", owner, *_part_fields(ap)))

    for t in twists or []:
        lines.append(
            _spec_line(
                "TWIST", *_rect(t), _rotation(t), str(t.get("designator", "")).strip()
            )
        )

    for sh in shields or []:
        style = sh.get("style", 0)
        if not isinstance(style, int):
            style = SHIELD_STYLES.get(str(style).strip().lower())
        if style not in (0, 1):
            return json.dumps(
                {
                    "success": False,
                    "error": f"unknown shield style {sh.get('style')!r}; "
                    f"use one of {sorted(SHIELD_STYLES)}",
                }
            )
        lines.append(
            _spec_line(
                "SHIELD",
                *_rect(sh),
                _rotation(sh),
                style,
                str(sh.get("designator", "")).strip(),
            )
        )

    for cb in cables or []:
        fields = [
            "CABLE",
            *_rect(cb),
            _rotation(cb),
            str(cb.get("designator", "")).strip(),
            str(cb.get("comment", "")).strip(),
            str(cb.get("part_number", "")).strip(),
            str(cb.get("source_library", "")).strip(),
        ]
        params = dict(cb.get("parameters") or {})
        if cb.get("description"):
            params["Description"] = cb["description"]
        for pk, pv in _param_pairs(params):
            fields.extend([pk, pv])
        lines.append(_spec_line(*fields))

    for wb in wire_breaks or []:
        x, y = int(wb.get("x", 0)), int(wb.get("y", 0))
        style_key = wb.get("style", wb.get("direction"))
        style: Any = ""
        if style_key is not None:
            style = (
                style_key
                if style_key in (0, 1)
                else WIRE_BREAK_STYLES.get(str(style_key).strip().lower())
            )
            if style is None:
                return json.dumps(
                    {
                        "success": False,
                        "error": f"unknown wire break style/direction {style_key!r}; "
                        f"use one of {sorted(WIRE_BREAK_STYLES)}",
                    }
                )
        orientation: Any = wb.get("orientation")
        if orientation is None and wb.get("direction") is not None:
            orientation = _wire_break_orientation(x, y, document_path, wires)
        orientation = "" if orientation is None else int(orientation) % 4
        extra = [style, orientation] if (style, orientation) != ("", "") else []
        lines.append(_spec_line("WIREBREAK", x, y, *extra))

    for lu in label_updates or []:
        lines.append(
            _spec_line(
                "LABELTEXT",
                int(lu.get("x", 0)),
                int(lu.get("y", 0)),
                str(lu.get("text", "")),
                str(lu.get("new_text", "")),
            )
        )
    if relink:
        lines.append("RELINK")

    for cu in component_updates or []:
        c_des = str(cu.get("designator", "")).strip()
        for pk, pv in _param_pairs(cu.get("parameters")):
            lines.append(_spec_line("UPDATE_COMPPARAM", c_des, pk, pv))
        for pk in cu.get("delete_parameters") or []:
            lines.append(_spec_line("DELETE_COMPPARAM", c_des, str(pk)))

    # Spec file and command under one hold of the bridge: another server
    # process must not replace the spec between writing and running it
    try:
        async with altium_bridge.exclusive():
            HARNESS_SPEC_FILE.parent.mkdir(parents=True, exist_ok=True)
            write_altium_text(HARNESS_SPEC_FILE, "\n".join(lines) + "\n")
            response = await altium_bridge._execute_command_locked(
                "add_harness_wiring", {}
            )
    except TimeoutError as e:
        response = {"success": False, "error": str(e)}
    if not response.get("success", False):
        return json.dumps(
            {"success": False, "error": response.get("error", "unknown error")}
        )

    result = response.get("result", {})
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return result
    if (
        isinstance(result, dict)
        and result.get("saved")
        and (twists or shields or cables or wire_breaks)
    ):
        _check_harness_links(
            result, document_path, twists, shields, cables, wire_breaks
        )
    return json.dumps(result, indent=2)


def _check_harness_links(
    result: dict,
    document_path: str,
    twists: list | None,
    shields: list | None,
    cables: list | None,
    wire_breaks: list | None,
) -> None:
    """Read the links Altium computed back from the saved file into
    result["links"]; a rectangle linking no wire or a break on no wire goes to
    not_found (and success becomes false)."""
    try:
        data = parse_wirdoc(Path(document_path).resolve())
    except Exception as e:  # noqa: BLE001 - report, never fail the write
        result.setdefault("warnings", []).append(f"links not verified: {e}")
        return
    misses = result.setdefault("not_found", [])
    links: dict[str, list] = {}

    def rect_key(item: dict) -> tuple:
        x1, y1, x2, y2 = _rect(item)
        return (min(x1, x2) / 10, min(y1, y2) / 10, max(x1, x2) / 10, max(y1, y2) / 10)

    for kind, items in (("twist", twists), ("shield", shields), ("cable", cables)):
        placed = {
            (o["x1"], o["y1"], o["x2"], o["y2"]): o for o in data.get(f"{kind}s", [])
        }
        by_des = {o.get("designator"): o for o in data.get(f"{kind}s", [])}
        for item in items or []:
            obj = by_des.get(item.get("designator")) if kind == "cable" else None
            obj = obj or placed.get(rect_key(item))
            label = f"{kind}@{','.join(str(v) for v in _rect(item))}"
            if obj is None or not obj["wires"]:
                misses.append(f"{label} (links no wire)")
            entry = {"rect_mils": _rect(item), "wires": obj["wires"] if obj else []}
            if obj and obj.get("designator"):
                entry["designator"] = obj["designator"]
            if kind == "shield" and obj:
                entry["drain_wires"] = obj["drain_wires"]
                pin = obj.get("pin")
                entry["pin_mils"] = [pin["x"] * 10, pin["y"] * 10] if pin else None
            if kind == "cable":
                entry["designator"] = item.get("designator", "")
            links.setdefault(f"{kind}s", []).append(entry)

    breaks = {(b["x"], b["y"]): b for b in data.get("wire_breaks", [])}
    for wb in wire_breaks or []:
        x, y = int(wb.get("x", 0)), int(wb.get("y", 0))
        brk = breaks.get((x / 10, y / 10))
        if brk is None or not brk["wire"]:
            misses.append(f"wire_break@{x},{y} (no wire end there)")
        links.setdefault("wire_breaks", []).append(
            {"at_mils": [x, y], "wire": brk["wire"] if brk else None}
        )
    result["links"] = links
    if misses:
        result["success"] = False


HARNESS_LAYOUT_SPEC_FILE = EXCHANGE_DIR / "harness_layout_spec.txt"


@_tool()
async def edit_harness_layout(
    ctx: Context,
    document_path: str = "",
    connectors: list | None = None,
    connection_points: list | None = None,
    bundles: list | None = None,
    coverings: list | None = None,
    layout_labels: list | None = None,
    delete: list | None = None,
    wires: list | None = None,
    dry_run: bool = False,
) -> str:
    """
    Create, move, edit and delete objects on a Harness Layout Drawing (.LdrDoc): connectors
    (including their physical / symbol view), connection points, bundles, coverings, layout
    labels and wire lengths.

    Objects are named by designator (unnamed ones by the unique_id get_harness_data reports)
    and edited in place, so their links to the wiring diagram survive. A designator several
    objects share (unannotated "B?" bundles, "CP?" points) is refused: give "unique_id"
    (with or without the designator). Coordinates are true
    mils (get_harness_data reports DXP units: x 10), lengths mm, orientations degrees
    (0, 90, 180, 270). Omitted fields keep their value.

    Altium connects bundle ends to the connection points under them when it loads a drawing,
    so after a geometry change the tool saves, closes, reopens and saves the drawing again
    ("reloaded": true; the undo history is lost), then reads it back: "bundles" lists each
    bundle the call drew or moved with its ends and lengths, "problems" what did not connect
    or lost a connection (success is false then), "unrouted_wires" the wires with no bundle
    path between the connection points of their two ends ("newly_unrouted_wires": those
    this call left without one). The wires of a twist are routed together: twisted wires
    whose routes share no bundle are all unrouted ("unrouted_reasons" says so); give them a
    common bundle before they part.

    Args:
        document_path (str): Target .LdrDoc. Required.
        connectors (list): Move / turn / mirror connectors, e.g.
            [{"designator": "P5", "x": 22900, "y": 8900, "orientation": 270, "mirrored": True}].
            The connector's own connection point (CP_P5) follows with the same offset, turned
            with the connector, unless it is given a position in connection_points or here as
            "connection_point": {"x": ..., "y": ...}; bundles on it follow too.
            "view": "physical" shows the connector as a picture of its footprint's 3D model
            (top view, 50 mm box, turned with the connector), "symbol" as its schematic
            symbol, e.g. [{"designator": "P2", "view": "physical"}]. As Altium's Properties
            panel does, "symbol" hides the connector's views and "physical" shows them again.
            A connector that has never had a rendered view gets a new one: Altium renders
            the picture only after loading the project from disk, so after saving the edits
            the tool closes and reopens the whole harness project (its other tabs close too)
            and then updates the connector from its library, each in a script run of its own
            (in one run Altium raised a .NET error) (as changing its part does: database parameters are refreshed,
            its pins in the layout get new UniqueIDs, wires stay linked). Refused, with
            nothing rendered, while a document of that project has unsaved changes. A new
            view's side and zoom are Altium's defaults (change them in Altium).
        connection_points (list): New or existing connection points, e.g.
            [{"designator": "CP1", "x": 12000, "y": 12000, "style": "circle",
              "show_name": True, "orientation": 0, "objects": ["SP1"],
              "designator_pos": [12200, 12400]}].
            style is "circle", "square" or "insulator". show_name shows or hides the
            name (its Designator text). "objects" are connectors, splices or
            no-connects (a free wire end) to hold here (taken from the connection point holding
            them); "release" removes held ones; "new_designator" renames. A wire is routed
            through the bundles between the connection points holding its two ends, so every
            splice and wire-end no-connect needs one. Moving a
            connection point moves the ends of its bundles (a straight end segment stays
            straight).
        bundles (list): New or existing bundles, e.g.
            [{"designator": "B1", "from": "CP_J1", "to": "CP1", "via": [[12000, 17300]],
              "length_mm": 254, "show_break_symbol": True, "line_width": 2, "comment": ""}].
            from/to are connection points (or connectors, meaning their own connection
            point); via are the bend points between them. Instead of from/to/via, "points"
            [x1, y1, x2, y2, ...] with both ends exactly on connection points. "length_mm":
            a number sets the physical length manually, None returns it to the drawn length.
            New bundles without a designator are named B<n>. Drawing a bundle across a
            connection point does not split it: give one bundle per span.
        coverings (list): New or existing coverings (sleeves, tape, tubes) on bundles, e.g.
            [{"designator": "COV1", "bundles": ["B1", "B2"], "part_number": "...",
              "source_library": "my.DbLib", "thickness": 3,
              "start_offset_mm": -10, "end_offset_mm": 0}].
            Offsets are from the ends of the covered bundles: negative insets the covering,
            positive extends it past the end. "bundles" replaces the covered bundles.
        layout_labels (list): New or existing layout labels, e.g.
            [{"designator": "L1", "text": "LABEL", "x": 10000, "y": 17300,
              "orientation": 0, "alignment": 1, "show_only_first_line": False,
              "part_number": "...", "source_library": "..."}].
        delete (list): Designators / unique ids of bundles, coverings, layout labels or
            connection points (which must hold nothing) to remove.
        wires (list): Fixed total wire lengths, e.g. [{"designator": "W1", "length_mm": 520}];
            None returns the wire to the length of its bundle route. Altium shows a fixed
            length as length type "MCAD CoDesigner" and no longer follows bundle changes.
        dry_run (bool): Only validate and return the spec records.

    Texts: every connector, connection point, bundle, covering and layout label takes
    "texts" to place or hide its texts, e.g. {"designator": "B1", "texts": {"designator":
    [3500, 15030], "length": [3380, 14880], "comment": {"hidden": True}}}: name -> [x, y]
    (mils, the text's bottom-left corner) or {"x", "y", "hidden", "justification"}. Names
    are Altium's (Designator, Comment, a bundle's Length, or a parameter name such as a
    connector's PartNumber, shown with "hidden": False), any case. Positions are absolute
    and applied after the call's moves. A placed text is justified bottom-left, so x, y is
    its bottom-left corner, unless "justification" names the corner or edge x, y is:
    bottom_left, bottom_center, bottom_right, center_left, center, center_right, top_left,
    top_center or top_right (mirroring an object turns its texts' left into right).
    get_harness_data reports each object's texts (DXP units) with their justification.

    Altium routes a wire through the bundles only from pins a connection point holds by
    UniqueID. An update from the library, and Altium's own import, give pins new UniqueIDs
    and leave the old ones in the connection points, so no wire gets routed. Every call
    repairs that ("connection_point_pins_synced"); a call with only document_path does just
    the repair. get_harness_data lists each connector's "unassigned_pins".

    Wire lengths for the fabrication drawing: a wire's length is the sum of the physical
    lengths of the bundles on its route (bundle "length_mm", independent of the drawn
    length) plus its offset, unless a fixed length is set (wires).

    Not supported (Altium's script interface has no setter): wire length offsets
    ("Offset Length", calculated type), user-defined wire lengths and manual wire paths.
    get_harness_data reports all of them once set in Altium (length_type, length_offset,
    via_bundles, length).

    Returns:
        str: JSON with success, saved, reloaded, counts (texts_placed, ...), not_found,
             warnings, bundles, problems, unrouted_wires and newly_unrouted_wires.
    """
    if not document_path:
        return json.dumps({"success": False, "error": "document_path is required"})
    doc_path = Path(document_path).resolve()
    if doc_path.suffix.lower() != ".ldrdoc" or not doc_path.exists():
        return json.dumps(
            {"success": False, "error": f"not an existing .LdrDoc: {doc_path}"}
        )
    try:
        layout = parse_ldrdoc(doc_path)
        spec = build_layout_spec(
            layout,
            connectors=connectors,
            connection_points=connection_points,
            bundles=bundles,
            coverings=coverings,
            layout_labels=layout_labels,
            delete=delete,
            wires=wires,
        )
    except (KeyError, TypeError, ValueError, IndexError) as e:
        return json.dumps({"success": False, "error": f"invalid request: {e!r}"})
    if spec["errors"]:
        return json.dumps(
            {"success": False, "errors": spec["errors"], "warnings": spec["warnings"]},
            indent=2,
        )
    # Every run also repairs the connection points' pin lists (Altium routes
    # wires only from pins a connection point holds), so a call without
    # changes still runs when pins are held by none
    if not spec["records"] and not any(
        c.get("unassigned_pins") for c in layout["connectors"]
    ):
        return json.dumps({"success": False, "error": "nothing to change"})
    lines = [_spec_line("DOCUMENT", str(doc_path))]
    lines += [_spec_line(*rec) for rec in spec["records"]]
    if dry_run:
        return json.dumps(
            {
                "success": True,
                "dry_run": True,
                "records": lines,
                "warnings": spec["warnings"],
                "expect": spec["expect"],
            },
            indent=2,
        )

    async def run(spec_lines: list[str]) -> dict[str, Any]:
        write_altium_text(HARNESS_LAYOUT_SPEC_FILE, "\n".join(spec_lines) + "\n")
        response = await altium_bridge._execute_command_locked(
            "edit_harness_layout", {}
        )
        if not response.get("success", False):
            return {"success": False, "error": response.get("error", "unknown error")}
        out = response.get("result", {})
        if isinstance(out, str):
            try:
                out = json.loads(out)
            except ValueError:
                return {"success": False, "error": out}
        return out

    try:
        async with altium_bridge.exclusive():
            HARNESS_LAYOUT_SPEC_FILE.parent.mkdir(parents=True, exist_ok=True)
            result = await run(lines)
            pending = result.get("render_pending") or []
            if result.get("saved") and pending:
                # New views render only after a project reload. Altium raised
                # a .NET error when the reload and the rendering ran in the
                # same script run as the edits, so each gets a run of its own.
                # The connectors to render hold no pins meanwhile (rendering one
                # with routed wires raised that error too); the last run, with
                # no records, gives them their pins back.
                steps = [await run([lines[0], _spec_line("RELOADPROJECT")])]
                if steps[-1].get("success"):
                    steps.append(
                        await run(
                            [lines[0]] + [_spec_line("RENDER", uid) for uid in pending]
                        )
                    )
                    if steps[-1].get("saved"):
                        steps.append(await run([lines[0]]))
                rendered = sum(s.get("views_rendered", 0) for s in steps)
                result["views_rendered"] = rendered
                result["connection_point_pins_synced"] = result.get(
                    "connection_point_pins_synced", 0
                ) + sum(s.get("connection_point_pins_synced", 0) for s in steps)
                result["not_found"] = result.get("not_found", []) + [
                    miss for s in steps for miss in s.get("not_found", [])
                ]
                result["render_pending"] = [] if rendered else pending
                result["reloaded"] = bool(steps[-1].get("saved"))
                failed = next((s for s in steps if not s.get("success")), None)
                if failed is not None:
                    result["success"] = False
                    if failed.get("error"):
                        result["error"] = f"views not rendered: {failed['error']}"
    except TimeoutError as e:
        result = {"success": False, "error": str(e)}
    if "saved" not in result:
        return json.dumps(
            {"success": False, "error": result.get("error", "unknown error")}
        )
    if spec["warnings"]:
        result["warnings"] = spec["warnings"]
    if result.get("saved"):
        try:
            after = parse_ldrdoc(doc_path)
        except Exception as e:  # noqa: BLE001 - report, never fail the write
            result.setdefault("warnings", []).append(f"not verified: {e}")
        else:
            problems, found = check_layout(after, spec["expect"], before=layout)
            result["bundles"] = found
            result["unrouted_wires"] = after["unrouted_wires"]
            result["newly_unrouted_wires"] = sorted(
                set(after["unrouted_wires"]) - set(layout["unrouted_wires"])
            )
            reasons = unrouted_reasons(after)
            if reasons:
                result["unrouted_reasons"] = reasons
            if problems:
                result["problems"] = problems
                result["success"] = False
    return json.dumps(result, indent=2)


@_tool()
async def convert_netlabels_to_text(ctx: Context, document_path: str = "") -> str:
    """Convert all net labels (eNetLabel, RECORD=25) in a Harness Wiring Diagram (.WirDoc)
    or Schematic back to standard text strings (ISch_Label, RECORD=4).

    Args:
        document_path (str): Absolute or relative path to target .WirDoc file. Required.

    Returns:
        str: JSON string reporting conversion status and count.
    """
    logger.info(f"Converting netlabels to text strings for doc: {document_path}")
    if not document_path:
        return json.dumps({"success": False, "error": "document_path is required"})
    doc_path = str(Path(document_path).resolve())

    response = await altium_bridge.execute_command(
        "convert_netlabels_to_text", {"document_path": doc_path}
    )
    if not response.get("success", False):
        return json.dumps(
            {"success": False, "error": response.get("error", "unknown error")}
        )

    result = response.get("result", {})
    if isinstance(result, str):
        try:
            return json.dumps(json.loads(result), indent=2)
        except ValueError:
            return result
    return json.dumps(result, indent=2)


@_tool(read_only=True)
async def get_sheet_style(ctx: Context, document_path: str = "") -> str:
    """Report the sheet size (A4, A3, ..., A-E, LETTER, ...) of a Harness Wiring Diagram
    (.WirDoc) or Schematic (.SchDoc), as Altium currently holds it (unsaved edits included).

    Read-only: the document is opened in Altium only if it is not open yet, and is never
    modified or saved.

    Args:
        document_path (str): Path to the target .WirDoc or .SchDoc. Required.

    Returns:
        str: JSON with "sheet_style" (a name add_harness_wiring's sheet_style accepts, an
        ORCAD_* size, or "CUSTOM" when a custom sheet size is in use), "standard_style"
        (the standard size selected underneath), "sheet_style_index" (TSheetStyle ordinal),
        "use_custom_sheet", "orientation", "sheet_size_mils" [x, y], "document" and
        "opened" (True when this call had to open the document).
    """
    if not document_path:
        return json.dumps({"success": False, "error": "document_path is required"})
    doc = Path(document_path).resolve()
    if doc.suffix.lower() not in (".wirdoc", ".schdoc"):
        return json.dumps(
            {"success": False, "error": f"not a .WirDoc or .SchDoc: {str(doc)!r}"}
        )
    if not doc.exists():
        return json.dumps({"success": False, "error": f"no such file: {str(doc)!r}"})

    response = await altium_bridge.execute_command(
        "get_sheet_style", {"document_path": str(doc)}
    )
    if not response.get("success", False):
        return json.dumps(
            {"success": False, "error": response.get("error", "unknown error")}
        )
    result = response.get("result", {})
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return json.dumps(
                {
                    "success": False,
                    "error": f"unexpected Altium reply: {result[:200]!r}",
                }
            )
    if not result.get("success", False):
        return json.dumps(
            {"success": False, "error": result.get("error", "unknown error")}
        )

    index = int(result.get("sheet_style_index", -1))
    standard = SHEET_STYLE_NAMES.get(index)
    custom = bool(result.get("use_custom_sheet", False))
    out: dict[str, Any] = {
        "success": True,
        "document": result.get("document", str(doc)),
        "sheet_style": "CUSTOM" if custom else standard,
        "standard_style": standard,
        "sheet_style_index": index,
        "use_custom_sheet": custom,
        "orientation": SHEET_ORIENTATIONS.get(int(result.get("orientation_index", -1))),
        "sheet_size_mils": [
            result.get("sheet_size_x_mils"),
            result.get("sheet_size_y_mils"),
        ],
        "opened": bool(result.get("opened", False)),
    }
    if standard is None:
        out["warning"] = f"unknown TSheetStyle ordinal {index}"
    return json.dumps(out, indent=2)


@_tool(read_only=True)
async def check_harness_wiring(ctx: Context, file_path: str) -> str:
    """Check the drawing of an Altium Harness Wiring Diagram (.WirDoc); read-only.

    Run it after placing or moving wires (add_harness_wiring wires / reroutes / twists),
    together with a screenshot: it sees the whole sheet, including what a screenshot shows
    too small or off-screen. It reads the SAVED file and works without Altium.

    errors: diagonal segments; wire ends on no pin hot end, splice or wire break; wires
    drawn on top of each other (shared stretch); wires running through another
    connector's pins.
    warnings: crossings with < 100 mils straight next to them; parallel horizontal runs
    < 100 mils apart; vertical runs < 200 mils apart; vertical runs in front of pin names
    or pin callout text (+ 150 mils); wire labels on other wires, labels, sheet text or
    pin names; labels away from their wire; twist marks linking other than 2 wires, on
    wire labels, or not in the sheet style (rectangle 100 mils beyond each wire so the
    hook tips land on the wires, 500 mils along them).
    Crossings are counted and listed but are not problems: those forced by the pin order
    of two connectors cannot be routed away.

    Args:
        file_path: Full path to the .WirDoc.

    Returns:
        JSON with clean (no problems at all), summary (wire_count, crossings, errors,
        warnings, by_type), problems [{severity, type, wires, detail, at (mils)}] and
        crossings [{wires, at}] (each list capped at 200 entries).
    """
    import harness_check

    if not file_path or Path(file_path).suffix.lower() != ".wirdoc":
        return json.dumps(
            {"success": False, "error": "file_path to a .WirDoc is required"}
        )
    try:
        result = harness_check.check_harness_wiring(file_path)
    except Exception as e:
        logger.error(f"Error checking harness wiring: {e}")
        return json.dumps(
            {"success": False, "error": f"Failed to check {file_path}: {e!s}"}
        )
    return json.dumps(
        {"success": True, "file_path": str(Path(file_path).resolve()), **result},
        indent=2,
    )


# Parameters a synced object carries that are no database column: the library
# link, Altium's own fields and its built-in harness wire properties. Never
# reported as stale.
NON_DATABASE_PARAMETERS = {
    "comment",
    "description",
    "designator",
    "partnumber",
    "design item id",
    "source library",
    "source table",
    "library reference",
    "crimp",
    "net",
    "systemdesignuniqueid",
    # Altium's built-in harness wire properties (read-only parameters)
    "gauge",
    "color",
    "border",
    "min. bending radius",
    "thickness",
    "include cut",
}


def _awg(text: str | None) -> str | None:
    """Gauge number in a text like '22 AWG (Twisted Pair)'."""
    match = re.search(r"(\d+(?:/\d+)?)\s*AWG", text or "", re.IGNORECASE)
    return match.group(1) if match else None


async def _sync_harness_from_library(
    ctx: Context,
    document_path: str | None,
    library: Any,
    sync_wires: bool,
    sync_cavities: bool,
    sync_connectors: bool,
    wire_designator_map: dict | None,
    cavity_map: dict | None,
    color_map: dict | None,
    parameter_aliases: dict | None,
    extra_wire_parameters: dict | None,
    source_library_name: str | None,
    sync_associated: bool = False,
    sync_cables: bool = False,
    associated_map: dict | None = None,
    delete_stale_parameters: bool | list | None = None,
) -> dict[str, Any]:
    """Shared engine of sync_harness_library and sync_harness_wires."""
    target_path = str(Path(document_path).resolve())
    src_lib = source_library_name or library.name
    aliases = parameter_aliases or {}
    doc_data = parse_wirdoc(target_path)
    unresolved: list[dict[str, str]] = []
    warnings: list[str] = []

    def with_aliases(params: dict[str, str]) -> dict[str, str]:
        out = dict(params)
        for alias, column in aliases.items():
            if params.get(column):
                out[alias] = params[column]
        return out

    # Stale parameters: on a synced object but no column of its database
    # table (a column renamed or deleted since the last sync)
    not_stale = NON_DATABASE_PARAMETERS | {
        n.lower() for n in [*aliases, *(extra_wire_parameters or {})]
    }
    if isinstance(delete_stale_parameters, (list, tuple, set)):
        delete_names = {str(n).lower() for n in delete_stale_parameters}
    else:
        delete_names = None
    stale: dict[str, list[str]] = {}
    stale_deleted: dict[str, list[str]] = {}

    def stale_in(where: str, table: Any, params: dict[str, str]) -> list[str]:
        """Record the stale parameter names of one object; return those to delete."""
        columns = {c.lower() for c in table.columns}
        to_delete = []
        for name in params:
            if name.lower() in columns or name.lower() in not_stale:
                continue
            if delete_stale_parameters is True or (
                delete_names is not None and name.lower() in delete_names
            ):
                stale_deleted.setdefault(name, []).append(where)
                to_delete.append(name)
            else:
                stale.setdefault(name, []).append(where)
        return to_delete

    wire_updates = []
    if sync_wires:
        des_map = wire_designator_map or {}
        for w in doc_data.get("wires", []):
            des = w.get("designator", "").strip()
            if not des or des == "?":
                continue
            wp = w.get("parameters", {})
            part_num = (
                des_map.get(des)
                or wp.get("PartNumber")
                or wp.get("Design Item Id")
                or w.get("design_item_id")
            )
            if not part_num:
                gauge = _awg(wp.get("Gauge") or wp.get("Wire Gauge"))
                color = wp.get("Color", "").lower()
                matches = [
                    pn
                    for table in library.tables.values()
                    for pn, row in table.rows.items()
                    if row.get("Color", "").lower() == color
                    and _awg(row.get("Wire Gauge") or row.get("Gauge")) == gauge
                ]
                if len(matches) != 1:
                    unresolved.append(
                        {
                            "designator": des,
                            "reason": f"{len(matches)} database rows match gauge "
                            f"{gauge!r} and color {color!r}",
                        }
                    )
                    continue
                part_num = matches[0]
            found = library.find(part_num)
            if found is None:
                unresolved.append(
                    {
                        "designator": des,
                        "reason": f"PartNumber {part_num!r} not in database",
                    }
                )
                continue
            table, row = found
            params = with_aliases(table.parameters(row))
            params.update(extra_wire_parameters or {})
            params["Design Item Id"] = part_num
            params["PartNumber"] = part_num
            params["Source Table"] = table.name
            if src_lib:
                params["Source Library"] = src_lib
            update: dict[str, Any] = {"designator": des, "parameters": params}
            to_delete = stale_in(des, table, wp)
            if to_delete:
                update["delete_parameters"] = to_delete
            tcolor = color_to_tcolor(row.get("Color", ""), color_map)
            if tcolor is not None:
                update["tcolor"] = tcolor
            elif row.get("Color"):
                warnings.append(
                    f"{des}: no drawing colour for {row['Color']!r}; pass color_map"
                )
            wire_updates.append(update)

    def library_parts(
        where: str, part_numbers: list[str], current: list[dict] | None = None
    ) -> list[dict] | None:
        """Database-backed part dicts for part_numbers, or None (reported)
        when one is not in the database: a slot is replaced as a whole, so
        it is left alone rather than losing that part. Parameters the
        current part of the same number carries that are no database column
        (e.g. a strip length typed in Altium) are kept, unless they are
        stale ones to delete: replacing the part would otherwise drop them."""
        kept = {
            p["part_number"]: p.get("parameters", {})
            for p in current or []
            if p.get("part_number")
        }
        parts = []
        for pn in part_numbers:
            found = library.find(pn)
            if found is None:
                unresolved.append(
                    {"designator": where, "reason": f"part {pn!r} not in database"}
                )
                return None
            table, row = found
            params = with_aliases(table.parameters(row))
            columns = {c.lower() for c in table.columns}
            dropped = set(stale_in(f"{where} {pn}", table, kept.get(pn, {})))
            for name, value in kept.get(pn, {}).items():
                if (
                    name not in params
                    and name != "Description"
                    and value
                    and name.lower() not in columns  # emptied in the database
                    and name not in dropped
                ):
                    params[name] = value
            parts.append(
                {
                    "part_number": pn,
                    "source_library": src_lib,
                    "source_table": table.name,
                    "description": params.pop("Description", ""),
                    "parameters": params,
                }
            )
        return parts

    cavity_parts = []
    if sync_cavities:
        wanted: dict[tuple[str, str], dict[int, list[str]]] = {}
        for conn, pins in (cavity_map or {}).items():
            for pin, spec in pins.items():
                slots = spec if isinstance(spec, dict) else {"crimp": spec}
                for slot, pns in slots.items():
                    cav_type = _cavity_type(slot)
                    if cav_type is None:
                        unresolved.append(
                            {
                                "designator": f"{conn}.{pin}",
                                "reason": f"unknown cavity slot {slot!r}",
                            }
                        )
                        continue
                    pn_list = pns if isinstance(pns, list) else [pns]
                    wanted.setdefault((str(conn), str(pin)), {})[cav_type] = [
                        str(p) for p in pn_list if p
                    ]
        for c in doc_data.get("connectors", []):
            for pin in c.get("pins", []):
                key = (c["designator"], pin["designator"])
                slots: dict[int, list[str]] = {}
                for part in pin.get("cavity_parts", []):
                    cav_type = _cavity_type(part.get("type", "crimp"))
                    if part.get("part_number"):
                        slots.setdefault(cav_type, []).append(part["part_number"])
                slots.update(wanted.pop(key, {}))
                where = f"{key[0]}.{key[1]}"
                for cav_type, pns in sorted(slots.items()):
                    if not pns:  # emptied by cavity_map
                        cavity_parts.append(
                            {
                                "connector": key[0],
                                "pin": key[1],
                                "type": cav_type,
                                "part_number": "",
                            }
                        )
                        continue
                    current = [
                        p
                        for p in pin.get("cavity_parts", [])
                        if _cavity_type(p.get("type", "crimp")) == cav_type
                    ]
                    for part in library_parts(where, pns, current) or []:
                        cavity_parts.append(
                            {
                                "connector": key[0],
                                "pin": key[1],
                                "type": cav_type,
                                **part,
                            }
                        )
        for conn, pin in wanted:
            unresolved.append(
                {"designator": f"{conn}.{pin}", "reason": "pin not found on the sheet"}
            )

    associated_parts = []
    if sync_associated:
        wanted_assoc = {
            str(k): [str(p) for p in v if p] for k, v in (associated_map or {}).items()
        }
        owners = [*doc_data.get("connectors", []), *doc_data.get("splices", [])]
        for owner in owners:
            des = owner["designator"]
            current = [
                p["part_number"]
                for p in owner.get("associated_parts", [])
                if p["part_number"]
            ]
            pns = wanted_assoc.pop(des, current)
            if not pns:
                if current:  # emptied by associated_map
                    associated_parts.append({"owner": des, "part_number": ""})
                continue
            for part in library_parts(des, pns, owner.get("associated_parts")) or []:
                associated_parts.append({"owner": des, **part})
        for des in wanted_assoc:
            unresolved.append(
                {
                    "designator": des,
                    "reason": "connector or splice not found on the sheet",
                }
            )

    # Cables carry their database columns as parameters (they keep a
    # Description parameter, unlike cavity parts)
    cable_updates = []
    if sync_cables:
        for cb in doc_data.get("cables", []):
            if not cb.get("part_number"):
                continue
            found = library.find(cb["part_number"])
            if found is None:
                unresolved.append(
                    {
                        "designator": cb.get("designator") or "cable",
                        "reason": f"PartNumber {cb['part_number']!r} not in database",
                    }
                )
                continue
            table, row = found
            cable_updates.append(
                {
                    "designator": cb.get("designator", ""),
                    **{k: round(cb[k] * 10) for k in ("x1", "y1", "x2", "y2")},
                    "part_number": cb["part_number"],
                    "source_library": src_lib,
                    "parameters": with_aliases(table.parameters(row)),
                }
            )

    # Connectors were placed from the DbLib by Altium itself, so they already
    # carry the database columns as parameters. Refresh only those that exist
    # and differ (e.g. values mangled by an earlier wrong CSV encoding); never
    # add parameters Altium did not place.
    component_updates = []
    if sync_connectors:
        refreshed: set[str] = set()
        for c in doc_data.get("connectors", []):
            current = c.get("parameters", {})
            part_num = current.get("PartNumber")
            # the parts of a multi-part connector are updated together
            if not part_num or c["designator"] in refreshed:
                continue
            refreshed.add(c["designator"])
            found = library.find(part_num)
            if found is None:
                unresolved.append(
                    {
                        "designator": c["designator"],
                        "reason": f"PartNumber {part_num!r} not in database",
                    }
                )
                continue
            table, row = found
            changed = {
                name: value
                for name, value in table.parameters(row).items()
                if name in current and current[name] != value
            }
            to_delete = stale_in(c["designator"], table, current)
            if changed or to_delete:
                component_updates.append(
                    {
                        "designator": c["designator"],
                        "parameters": changed,
                        "delete_parameters": to_delete,
                    }
                )

    # Cavity parts in pins hidden on their part of a multi-part connector
    # were put there by tools that ignored the part: never valid, removed
    hidden_parts = {
        c["designator"]: c["hidden_pin_cavity_parts"]
        for c in doc_data.get("connectors", [])
        if sync_cavities and c.get("hidden_pin_cavity_parts")
    }
    if hidden_parts:
        warnings.append(
            "cavity parts removed from pins hidden on their part of multi-part "
            f"connector(s) {sorted(hidden_parts)} (see hidden_pin_cavity_parts_removed)"
        )

    if (
        wire_updates
        or cavity_parts
        or component_updates
        or associated_parts
        or cable_updates
        or hidden_parts
    ):
        res_str = await add_harness_wiring(
            ctx,
            document_path=target_path,
            wire_updates=wire_updates,
            cavity_parts=cavity_parts,
            component_updates=component_updates,
            associated_parts=associated_parts,
            cables=cable_updates,
            clear_hidden_cavity_parts=sorted(hidden_parts),
            # the project compiler sees changed wire parameters and cavity
            # parts only after the sheet is reloaded
            relink=True,
        )
        try:
            res_json = json.loads(res_str)
        except ValueError:
            res_json = {"success": False, "raw": res_str}
    else:
        res_json = {"success": True, "note": "nothing to update"}

    if stale:
        warnings.append(
            f"{len(stale)} parameter name(s) under stale_parameters are no column of their "
            "database table any more (renamed or deleted) and were kept: ask the user whether "
            "to keep or delete them, then run again with delete_stale_parameters=True (all) "
            "or a list of the names to delete"
        )

    return {
        "success": bool(res_json.get("success")) and not unresolved,
        "document_path": target_path,
        "source_library": src_lib,
        "wires_synced_count": res_json.get("wires_updated", len(wire_updates)),
        "cavities_synced_count": res_json.get(
            "crimps",
            sum(1 for p in cavity_parts if p["type"] == 0 and p["part_number"]),
        ),
        "cavity_parts_synced_count": res_json.get(
            "cavity_parts", sum(1 for p in cavity_parts if p["part_number"])
        ),
        "associated_parts_synced_count": res_json.get(
            "associated_parts", sum(1 for p in associated_parts if p["part_number"])
        ),
        "cables_synced_count": res_json.get("cables", len(cable_updates)),
        "connectors_refreshed": {
            u["designator"]: sorted(u["parameters"])
            for u in component_updates
            if u["parameters"]
        },
        "stale_parameters": {name: sorted(set(w)) for name, w in sorted(stale.items())},
        "stale_parameters_deleted": {
            name: sorted(set(w)) for name, w in sorted(stale_deleted.items())
        },
        "parameters_deleted_count": res_json.get("parameters_deleted", 0),
        "hidden_pin_cavity_parts_removed": hidden_parts,
        "unresolved": unresolved,
        "warnings": warnings,
        "altium_response": res_json,
    }


@_tool()
async def sync_harness_library(
    ctx: Context,
    document_path: str,
    dblib_path: str | None = None,
    wires: bool = True,
    cavities: bool = True,
    connectors: bool = True,
    wire_designator_map: dict | None = None,
    cavity_map: dict | None = None,
    color_map: dict | None = None,
    parameter_aliases: dict | None = None,
    associated_parts: bool = True,
    cables: bool = True,
    associated_map: dict | None = None,
    delete_stale_parameters: bool | list | None = None,
) -> str:
    """Link harness wires, cavity parts, associated parts and cables to a local CSV-backed .DbLib and copy their parameters.

    Altium's harness editor cannot pick wires or cavity parts from a desktop .DbLib, so this
    tool writes the library link (Design Item Id, Source Library, Source Table) and every part
    parameter from the database row onto the .WirDoc itself, then saves it. Running it again
    updates values in place (no duplicates). A part's Description column becomes its component
    description (Altium deletes Description parameters on cavity and associated parts).

    Wires are matched by wire_designator_map, else their PartNumber / Design Item Id, else the one
    row whose gauge and Color match the wire's. Cavity slots (crimp, seal, plug, other) and
    connector/splice associated parts keep their current parts unless cavity_map /
    associated_map assign others. Anything that cannot be matched is listed under "unresolved"
    and left unchanged.

    Stale parameters - on a synced wire, cavity part, associated part or connector but no
    column of its database table any more (the column was renamed or deleted) - are listed
    under "stale_parameters" ({name: [objects]}) and kept. Ask the user whether to keep or
    delete them, then run again with delete_stale_parameters. The library link, Comment,
    Description, Net, SystemDesignUniqueId and Altium's built-in wire properties (Gauge,
    Color, Border, Min. Bending Radius, Thickness, Include Cut) are never stale.

    Args:
        document_path: Target .WirDoc path. Required.
        dblib_path: The .DbLib to read. Defaults to $ALTIUM_DBLIB_PATH, else the first .DbLib
            found walking up from the document (in each folder and up to two levels below).
        wires: Sync harness wires (default True).
        cavities: Sync cavity parts - crimps, seals, plugs, other (default True).
        connectors: Refresh placed connectors' existing database parameters whose values differ
            from the database row (default True; Altium's "Update Parameters From Database").
        associated_parts: Sync connector/splice associated parts (default True).
        cables: Refresh placed cables that have a part number from their database row
            (default True).
        wire_designator_map: Wire designator -> part number, e.g. {"W1": "5875 BK005"}.
        cavity_map: Cavity parts to assign: {connector: {pin: part number}} for the crimp, or
            {connector: {pin: {slot: part number(s)}}} with slot "crimp", "seal", "plug" or
            "other" (a list for several "other" parts; an empty list empties the slot), e.g.
            {"J1": {"1": "1060-20-0122", "3": {"crimp": "1060-20-0122", "seal": "0413-204-2005"},
                    "4": {"plug": "114017"}}}.
        associated_map: Associated parts to assign: {connector or splice: [part numbers]}, e.g.
            {"J1": ["DTM04-4P-BOOT"]}; an empty list removes them.
        color_map: Extra wire drawing colours, {name: "#RRGGBB"}, on top of the defaults
            (black, brown, red, orange, yellow, green, blue, violet, purple, gray/grey, white,
            pink, tan, light blue, light green). Striped names use their base colour.
        parameter_aliases: Extra parameter names copied from a column, {alias: column},
            e.g. {"Gauge": "Wire Gauge"}.
        delete_stale_parameters: True deletes every stale parameter, a list deletes the stale
            parameters of those names (e.g. ["Wire Gauge", "VoltageRating"]); default None
            only reports them. Only ever applied to stale parameters.

    Returns:
        str: JSON with counts, stale_parameters, stale_parameters_deleted, unresolved items,
        warnings and Altium's report.
    """
    if not document_path or not Path(document_path).exists():
        return json.dumps(
            {
                "success": False,
                "error": f"document_path to an existing .WirDoc is required: {document_path!r}",
            }
        )
    lib_path = (
        Path(dblib_path) if dblib_path else find_dblib(Path(document_path).resolve())
    )
    if not lib_path or not lib_path.exists():
        return json.dumps(
            {
                "success": False,
                "error": "No .DbLib found; pass dblib_path or set ALTIUM_DBLIB_PATH",
            }
        )
    try:
        library = load_dblib(lib_path)
    except (OSError, ValueError) as e:
        return json.dumps({"success": False, "error": f"Cannot read {lib_path}: {e}"})

    result = await _sync_harness_from_library(
        ctx,
        document_path,
        library,
        sync_wires=wires,
        sync_cavities=cavities,
        sync_connectors=connectors,
        wire_designator_map=wire_designator_map,
        cavity_map=cavity_map,
        color_map=color_map,
        parameter_aliases=parameter_aliases,
        extra_wire_parameters=None,
        source_library_name=None,
        sync_associated=associated_parts,
        sync_cables=cables,
        associated_map=associated_map,
        delete_stale_parameters=delete_stale_parameters,
    )
    result["dblib_path"] = str(lib_path)
    return json.dumps(result, indent=2)


@_tool()
async def sync_harness_wires(
    ctx: Context,
    document_path: str | None = None,
    wires_db_path: str | None = None,
    source_library_name: str | None = None,
    wire_designator_map: dict | None = None,
    color_map: dict | None = None,
) -> str:
    """Synchronize harness wire parameters, design item IDs, and visual colors from a wire database CSV onto a .WirDoc.

    Wire-only variant of sync_harness_library, kept for existing workflows. On top of the database
    columns it writes the legacy parameters Gauge (from Wire Gauge), VoltageRating (from Voltage
    Rating), Border=None and ComponentType=Wire.

    Because Altium Designer's harness engine rejects desktop .DbLib components in the GUI
    'Replace Component' dialog, this tool writes the library link and all parameters itself and
    saves the document. Parameters are updated in place (no duplicates).

    Args:
        document_path (str): Target .WirDoc file path. Required.
        wires_db_path (str, optional): Path to the wire database CSV. If omitted, uses the
            ALTIUM_WIRES_DB_PATH environment variable, else the .DbLib found walking up from the
            document (see sync_harness_library).
        source_library_name (str, optional): Name of the parent database library (e.g. 'components.DbLib').
        wire_designator_map (dict, optional): Wire designator -> PartNumber, e.g. {"W1": "5875 BK005"}.
            Otherwise each wire is resolved from its PartNumber / Design Item Id, or from the one
            database row matching both its gauge and color. Wires that cannot be resolved
            unambiguously are listed under "unresolved" and left unchanged.
        color_map (dict, optional): Extra wire drawing colours, {name: "#RRGGBB"}.

    Returns:
        str: JSON string detailing updated wires, unresolved wires and Altium's report.
    """
    if not document_path or not Path(document_path).exists():
        return json.dumps(
            {
                "success": False,
                "error": f"document_path to an existing .WirDoc is required: {document_path!r}",
            }
        )

    csv_path = wires_db_path or os.environ.get("ALTIUM_WIRES_DB_PATH")
    try:
        if csv_path:
            library = load_csv_table(Path(csv_path).resolve())
            if not source_library_name:
                found = find_dblib(Path(csv_path).resolve().parent)
                source_library_name = found.name if found else None
            db_label = str(Path(csv_path).resolve())
        else:
            lib_path = find_dblib(Path(document_path).resolve())
            if not lib_path:
                return json.dumps(
                    {
                        "success": False,
                        "error": "Wire database not found. Please provide wires_db_path.",
                    }
                )
            library = load_dblib(lib_path)
            db_label = str(lib_path)
    except (OSError, ValueError) as e:
        return json.dumps(
            {"success": False, "error": f"Cannot read wire database: {e}"}
        )

    result = await _sync_harness_from_library(
        ctx,
        document_path,
        library,
        sync_wires=True,
        sync_cavities=False,
        sync_connectors=False,
        wire_designator_map=wire_designator_map,
        cavity_map=None,
        color_map=color_map,
        parameter_aliases={"Gauge": "Wire Gauge", "VoltageRating": "Voltage Rating"},
        extra_wire_parameters={"Border": "None", "ComponentType": "Wire"},
        source_library_name=source_library_name,
    )
    result["wires_db_path"] = db_label
    return json.dumps(result, indent=2)


@_tool()
async def edit_schematic(ctx: Context, sheet_path: str, operations: list, unit: str = "mil",
                         save: bool = False) -> str:
    """
    Change an existing schematic sheet: move, rotate, delete or place parts,
    set parameters, move designators and parameter text, add wires, net
    labels, power ports, junctions and no-ERC markers, and delete objects.

    The whole edit is one undo step in Altium (Ctrl+Z takes it all back),
    and the user's own edits stay undoable. Operations run in order; one
    that fails is reported in "errors" and the rest still run.

    Coordinates are absolute sheet coordinates, Altium's origin at the
    bottom left, in `unit` ("mil" or "mm"). check_schematic reports
    positions and suggestions in the sheet's own unit (mm for a metric
    grid), so pass the same unit to apply its suggestions directly.

    Operations, each a dict with "op":
      move_component    designator, [part], [x, y], [rotation 0/90/180/270],
                        [mirror true/false]. x, y is the part's origin.
      delete_component  designator, [part]
      place_component   library (.SchLib path), symbol, designator, x, y,
                        [rotation], [mirror], [comment]
      set_parameter     designator, name, value, [part], [visible]
                        (creates the parameter when missing)
      move_text         designator, name ("Designator", "Comment" or a
                        parameter), [part], [x, y], [justification 0-8:
                        bottom-left, bottom-centre, bottom-right, middle-left,
                        ..., top-right], [orientation 0-3], [visible]
      add_wire          points [[x, y], ...] (a pin's connection point is
                        its outer end, as build_schematic's pin_map gives)
      add_net_label     x, y (on a wire), text, [orientation], [justification]
      add_power_port    x, y, text, [style: bar, arrow, circle, wave,
                        power_ground, signal_ground, earth], [orientation
                        0-3, default 1 = up], [show_net_name]
      add_junction      x, y
      add_no_erc        x, y
      delete_at         kind (wire, net_label, power_port, junction, no_erc,
                        port, note), x, y, [text]: removes a wire passing
                        through the point, or the object located there
    "part" picks one part of a multi-part component (1 = A, ...).

    Args:
        sheet_path (str): Full path of the .SchDoc; opened if not open.
        operations (list): The operations above, applied in order.
        unit (str): "mil" (default) or "mm".
        save (bool): Save the sheet afterwards (needed before
            check_schematic, which reads the saved file). Otherwise it is
            left modified for the user to review and save.

    Returns:
        str: JSON with how many operations were applied and the errors.
    """
    import sch_edit

    # Long paths, or Altium opens a second copy of a sheet or library that
    # is already open
    sheet_path = long_path(sheet_path)
    operations = [dict(op, library=long_path(op["library"]))
                  if isinstance(op, dict) and op.get("op") == "place_component" and op.get("library") else op
                  for op in (operations or [])]
    lines, error = sch_edit.encode_operations(operations, unit)
    if error:
        return json.dumps({"success": False, "error": error})
    if not sheet_path.lower().endswith(".schdoc"):
        return json.dumps({"success": False, "error": "sheet_path must be a .SchDoc"})
    logger.info(f"Editing {sheet_path}: {len(lines)} operations")
    # Part names and parameter values may hold text that is not plain ASCII
    write_altium_text(EXCHANGE_DIR / "sch_edit_spec.txt", "\n".join(lines) + "\n")
    response = await altium_bridge.execute_command("edit_schematic", {
        "sheet_path": sheet_path, "save": bool(save)})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to edit the sheet: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    if result.get("errors"):
        result["success"] = False
    return json.dumps(result, indent=2)

# Changes the design: a timeout leaves its outcome unknown, so no blind retry
MUTATING_COMMANDS.add("edit_schematic")

@_tool(read_only=True)
async def check_schematic(ctx: Context, path: str, max_issues_per_sheet: int = 150) -> str:
    """
    Check a schematic sheet for drafting problems that make it hard to read.

    Run it after drawing or editing a sheet (build_schematic, run_altium_script
    or by hand), fix what it reports, and run it again. It reads the SAVED
    file and does not need Altium, so save the sheet first; unsaved edits are
    not seen.

    Issue types, most important first:
      * pin_tap - a two-pin part (decoupling cap, pull-up) joins its net AT its
        own pin. Move the part so a short wire (a couple of grids) runs from
        the pin to a junction on the net; suggest.junction_at shows where.
      * label_not_at_end - a net label sits along a wire, away from the wire's
        free end. Put it at the end (suggest gives the point).
      * label_justification - a label at the free end whose text hangs off
        the end of the wire. Justify it so the text runs back along the wire:
        bottom-right (2) when the wire ends to the right, bottom-left (0)
        when it ends to the left. A middle-justified (3/5) label anchored on
        the end, reading outward like a port, is also accepted.
      * sheet_entry_margin - a sheet symbol's first entry sits well below its
        top edge. Altium's default is one grid (100 mil; 3 mm on a 1 mm grid);
        extra room at the bottom is fine.
      * sheet_symbol_text - a sheet symbol's designator or file name is not
        above it. Both go above: file name on the top edge, designator a line
        (100 mil) above it, both bottom-left justified at the left edge.
      * label_orientation, label_off_wire, dangling_wire, wire_through_body.
      * pin_off_grid - a part whose pins sit a few microns (up to 1 mil) off
        the grid. Every wire drawn to them comes out slanted. When the part
        itself is on the grid its drawing was shifted from its origin. When
        every pin is off by the same amount, suggest gives the fix:
        move_component_to (an on-grid position for edit_schematic's
        move_component) when the whole part is that far off the grid, else
        shift_contents_by. Pins off by different amounts get no suggestion:
        the symbol needs fixing in its library.
      * slanted_wire - a wire segment a little (1 um to half a grid) out of
        level or plumb. It looks straight, but dragging a part on it turns it
        diagonal. Usually a symbol whose pins are off the grid (then the
        issue names the pin) or coordinates rounded to whole mils.
        suggest.move_end/to give the end to move, at full precision:
        delete_at the wire and add_wire it again with the new end.
      * text_overlap - a designator, parameter, net label or port text
        overlapping a wire, a pin, a part or other text. For part text the
        issue carries suggest.texts: positions (x, y, justification) for the
        designator and each visible parameter that collide with nothing, e.g.
        designator left and value right of a vertical cap.

    Coordinates are in the sheet's units ("unit": mm for a metric grid,
    otherwise mil) with Altium's origin at the bottom left.

    Args:
        path: a .SchDoc, a .PrjPcb (every sheet it lists), or a folder (every
            .SchDoc in it, not recursive).
        max_issues_per_sheet: cap on issues listed per sheet; counts are
            always complete.

    Returns:
        JSON: per sheet, the issue counts and the issues.
    """
    import schematic_check

    p = Path(path)
    if p.is_dir():
        sheets = sorted(p.glob("*.SchDoc"))
    elif p.suffix.lower() == ".prjpcb" and p.is_file():
        sheets = schematic_check.project_sheets(p)
    elif p.suffix.lower() == ".schdoc" and p.is_file():
        sheets = [p]
    else:
        return json.dumps({"success": False,
                           "error": f"not a .SchDoc, .PrjPcb or folder: {path}"})

    results, totals = [], {}
    for sheet in sheets:
        if not Path(sheet).is_file():
            results.append({"sheet": str(sheet),
                            "error": "listed in the project but not on disk - save it in Altium first"})
            continue
        try:
            r = schematic_check.check_sheet(sheet)
        except Exception as e:
            results.append({"sheet": str(sheet), "error": f"{type(e).__name__}: {e}"})
            continue
        for k, v in r["issue_counts"].items():
            totals[k] = totals.get(k, 0) + v
        if len(r["issues"]) > max_issues_per_sheet:
            r["issues"] = r["issues"][:max_issues_per_sheet]
            r["truncated"] = True
        results.append(r)
    return json.dumps({"success": True, "sheets": len(sheets), "issue_counts": totals,
                       "results": results}, indent=1)


@_tool()
async def create_symbols_batch(ctx: Context, spec_file: str) -> str:
    """
    Create many schematic symbols, or edit existing ones, in a single
    Altium script run.

    Use instead of repeated create_schematic_symbol calls when creating
    more than a handful of symbols (bulk library imports/migrations): one
    script launch instead of one per symbol, and pipe-delimited plain text
    instead of JSON so field text (commas, brackets, spaces) is preserved
    exactly. Verified by exact round-trips of a complete 284-symbol
    production library.

    Args:
        spec_file (str): Path to a plain-text spec file, one record per line:
            LIBRARY|<path to .SchLib>   (optional first line: opens/focuses;
                                         an already-open library is only
                                         focused, never reloaded)
            SYMBOL|<name>|<description>|<part_count>
            PIN|<same pipe fields as create_schematic_symbol pins>
            GRAPHIC|<same entry format as create_schematic_symbol graphics>
            EDITSYMBOL|<name>   edit an existing symbol instead (its pins
                                and graphics are left alone; a missing
                                symbol is reported as failed, never created)
            COMMENT|<text>[|<visible 1/0>]   set the Comment (empty text
                                is allowed); hidden unless visible is 1
            DESCRIPTION|<text>  set the Description
            SYMPARAM|<name>|<value>|<visible 1/0>[|x|y[|justification[|font]]]
                                add a symbol parameter, or replace one with
                                that name. By default, visible ones without
                                x|y go below the body's bottom-left corner,
                                left-aligned, 100 mil apart in record order
                                (leave a field empty to skip it and still
                                give a later one). justification:
                                bottom_left, bottom_center, bottom_right,
                                center_left, center, center_right, top_left,
                                top_center or top_right. font: <font
                                name>:<size>, e.g. "Arial:10" (resolved by
                                name, so it works in any document). Omitted
                                fields keep the parameter's own. A parameter
                                the tool positions gets Autoposition off, so
                                Altium does not move it later.
            PINPARAM|<pin number>|<name>|<value>   add (or replace) a hidden
                                parameter on every pin with that number
            PINDESC|<pin number>|<text>   set the Description of every pin
                                with that number
            PINSYMBOL|<pin number>|<edge>|<symbol>   set an IEEE symbol of
                                every pin with that number. edge: inside,
                                inside_edge, outside_edge or outside.
                                symbol: a TIeeeSymbol name without the
                                leading "e", any case - none, dot, clock,
                                activelowinput, activelowoutput, schmitt,
                                analogsignalin, digitalsignalin, opencollector,
                                hiz, ... (the full list is in the
                                altium-script skill, SCH_API_Reference.md,
                                TIeeeSymbol). An unknown edge or symbol is
                                reported under problems.
            PINFUNC|<pin number>|<F1>/<F2>/...   optional: replace the
                                Functions list (Properties panel > Pin >
                                Functions) of every pin with that number,
                                in the order given, e.g.
                                PINFUNC|23|ETH1_MDIO/USART2_TX/TIM2_CH3.
                                Use it only when the user asks for pin
                                functions; nothing sets them otherwise, not
                                even from a pin name with '/' in it. Names
                                cannot contain '/' or '|'. A script cannot
                                clear the list, so an empty one is reported
                                under problems; Altium merges repeated names,
                                and a stored list that differs is reported.
            These records work after SYMBOL and EDITSYMBOL alike.
            Each SYMBOL or EDITSYMBOL line starts a new symbol; the other
            records belong to the most recent one.

    Every created or edited symbol is one undo step and marks the library
    as modified; nothing is saved - the user saves the library.

    Returns:
        str: JSON object with created and edited counts, a failed name list
             and problems (e.g. a pin number not in the symbol, an unknown
             IEEE symbol or justification)
    """
    logger.info(f"Creating symbols batch from {spec_file}")

    # The script opens or focuses the LIBRARY| path as written, so a short
    # (8.3) or forward-slash path would open a second copy of a library that
    # is open. Altium gets a copy with the long path; the caller's file is
    # left as it is.
    copy = None
    try:
        fixed = spec_with_long_library(read_altium_text(spec_file))
    except OSError:
        fixed = None  # missing or unreadable: the script reports it
    if fixed is not None:
        fd, copy = tempfile.mkstemp(prefix="symbols_spec_", suffix=".txt", dir=EXCHANGE_DIR)
        os.close(fd)
        write_altium_text(copy, fixed)
    try:
        response = await altium_bridge.execute_command(
            "create_symbols_batch",
            {"spec_file": copy or spec_file}
        )
    finally:
        if copy:
            try:
                os.remove(copy)
            except OSError:
                pass

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error in batch symbol creation: {error_msg}")
        return json.dumps(
            {"success": False, "error": f"Failed batch creation: {error_msg}"}
        )

    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

@_tool(read_only=True)
async def get_symbol_primitives(ctx: Context, library_path: str = "", symbol_name: str = "") -> str:
    """
    Read the graphic primitives of symbols in a schematic library (.SchLib).

    Two modes:
    - symbol_name omitted: inventory of every symbol in the library with
      per-type primitive counts (pins, rectangles, lines, polylines,
      polygons, arcs, ellipses, beziers, labels, ...). Use this to survey
      what drawing features a library's symbols require.
    - symbol_name given (exact match, case-insensitive): full geometry dump
      of that symbol - every primitive with coordinates in mils, plus pin
      details (number, name, electrical type, orientation, length,
      owner_part_id). Use this as the reference/spec when recreating or
      validating a symbol.

    Args:
        library_path (str, optional): Full path to the .SchLib file to open.
            Omit to use the schematic library currently focused in Altium.
        symbol_name (str, optional): Exact symbol (LibReference) name to dump.

    Returns:
        str: JSON object - inventory mode: {library_name, symbol_count,
             symbols: [{name, description, part_count, <type counts>}]};
             dump mode: {library_name, symbol_name, description, part_count,
             comment: {text, visible}, parameters: [{name, value, visible,
             x, y, justification, autoposition, font: {name, size}}],
             primitives: [...]} - x and y are where Altium holds the
             parameter now. Pins also carry
             description, symbol_inside, symbol_inside_edge,
             symbol_outside_edge and symbol_outside (IEEE symbol names as
             PINSYMBOL takes them), their parameters [{name, value}]
             when they have any, and functions ["ETH1_MDIO", ...] (the
             Functions list, as PINFUNC sets it) when they have any
    """
    logger.info(
        f"Getting symbol primitives (library={library_path}, symbol={symbol_name})"
    )

    response = await altium_bridge.execute_command(
        "get_symbol_primitives",
        {"library_path": library_path, "symbol_name": symbol_name},
    )

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting symbol primitives: {error_msg}")
        return json.dumps(
            {"success": False, "error": f"Failed to get symbol primitives: {error_msg}"}
        )

    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

@_tool(read_only=True)
async def get_all_nets(ctx: Context) -> str:
    """
    Return every unique net name in the active PCB document or harness.

    Returns
    -------
    str :
        A JSON array of net names, e.g. ["GND", "VCC33", "USB_D+", ...]
    """
    logger.info("Getting all nets")

    response = await altium_bridge.execute_command("get_all_nets", {})

    if not response.get("success", False) or not response.get("result"):
        h_path = await _get_active_harness_path()
        if h_path:
            nets = get_harness_nets(h_path)
            if nets:
                logger.info(f"Retrieved {len(nets)} harness nets as fallback")
                return json.dumps(nets, indent=2)
        if not response.get("success", False):
            error_msg = response.get("error", "Unknown error")
            logger.error(f"Error getting nets: {error_msg}")
            return json.dumps({"error": f"Failed to get nets: {error_msg}"})

    # Result is already a JSON‑serialisable Python list
    return json.dumps(response.get("result", []), indent=2)

@_tool()
async def create_net_class(ctx: Context, class_name: str, net_names: list) -> str:
    """
    Create a new net class and add specified nets to it

    Args:
        class_name (str): Name of the net class to create or modify
        net_names (list): List of net names to add to the class

    Returns:
        str: JSON object with the result of the operation. nets_added counts
        the nets that were not in the class before; names the board has no
        net for are listed in nets_not_found and not added.
    """
    logger.info(f"Creating net class '{class_name}' with {len(net_names)} nets")

    # Execute the command in Altium to create the net class
    response = await altium_bridge.execute_command(
        "create_net_class", {"class_name": class_name, "net_names": net_names}
    )

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("create_net_class", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error creating net class: {error_msg}")
        return json.dumps(
            {"success": False, "error": f"Failed to create net class: {error_msg}"}
        )

    # Get the result data
    result = response.get("result", {})

    logger.info(f"Net class '{class_name}' created/modified successfully")
    return json.dumps(result, indent=2)
    
@_tool()
async def set_component_position(ctx: Context, cmp_designator: str, x: float, y: float, rotation: float = -1) -> str:
    """
    Set a component's absolute position in the PCB layout

    Args:
        cmp_designator (str): Designator of the component to position (e.g., "R1", "C5", "U3")
        x (float): Absolute X position in mils
        y (float): Absolute Y position in mils
        rotation (float): Rotation angle in degrees (0-360), use -1 to keep current rotation

    Returns:
        str: JSON object with the result of the position operation
    """
    logger.info(
        f"Setting component {cmp_designator} position to X:{x}, Y:{y}, Rotation:{rotation}"
    )

    response = await altium_bridge.execute_command(
        "set_component_position",
        {"designator": cmp_designator, "x": x, "y": y, "rotation": rotation},
    )

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("set_component_position", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error setting component position: {error_msg}")
        return json.dumps(
            {
                "success": False,
                "error": f"Failed to set component position: {error_msg}",
            }
        )

    result = response.get("result", {})
    logger.info("Component position set successfully")
    return json.dumps({"success": True, "result": result}, indent=2)

@_tool()
async def move_components(ctx: Context, cmp_designators: list, x_offset: float, y_offset: float, rotation: float = 0) -> str:
    """
    Move components by RELATIVE offset from their current position (not absolute positioning)

    IMPORTANT: This moves components BY the offset amount, not TO a position.
    For absolute positioning, use set_component_position instead.

    Args:
        cmp_designators (list): List of designators of the components to move (e.g., ["R1", "C5", "U3"])
        x_offset (float): X offset distance in mils (positive = right, negative = left)
        y_offset (float): Y offset distance in mils (positive = up, negative = down)
        rotation (float): New absolute rotation angle in degrees (0-360), if 0 the rotation is not changed

    Returns:
        str: JSON object with the result of the move operation
    """
    logger.info(
        f"Moving components: {cmp_designators} by X:{x_offset}, Y:{y_offset}, Rotation:{rotation}"
    )

    # Execute the command in Altium to move components
    response = await altium_bridge.execute_command(
        "move_components",
        {
            "designators": cmp_designators,
            "x_offset": x_offset,
            "y_offset": y_offset,
            "rotation": rotation,
        },
    )

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("move_components", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error moving components: {error_msg}")
        return json.dumps(
            {"success": False, "error": f"Failed to move components: {error_msg}"}
        )

    # Get the result data
    result = response.get("result", {})

    logger.info("Components moved successfully")
    return json.dumps({"success": True, "result": result}, indent=2)

@_tool()
async def place_components(ctx: Context, placements: list, board_path: str = "") -> str:
    """
    Place multiple components at absolute positions in a single Altium transaction.

    This is the batch version of set_component_position - prefer it whenever
    placing more than one component, since every tool call is a full round
    trip into Altium. The whole batch is one undo step.

    Coordinate conventions: x/y are in mils relative to the board origin;
    rotation is in degrees counterclockwise.

    Args:
        placements (list): One dict per component:
            - designator (str, required): e.g. "R1"
            - x (float, required): absolute X position in mils
            - y (float, required): absolute Y position in mils
            - rotation (float, optional): absolute rotation in degrees (0-360).
              Omit (or pass -1) to keep the current rotation.
            - layer (str, optional): "top" or "bottom" to set the board side
              (the footprint is mirrored when flipped). Omit to keep the
              current side.
            - locked (bool, optional): lock (true) or unlock (false) the
              component; omit to leave it as it is.
            - designator_text, comment_text (dict, optional): put the
              designator or comment text at {x, y (its anchor, as get_layout
              reports it), rotation, height, stroke_width, visible,
              autoposition ("manual" by default, or top_left ... bottom_right
              as get_layout reports it)} after the move. Omit to let it move
              with the part. (set_designator_positions places by centre.)
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used. Give it whenever more
            than one board is open, so the parts move on the right one.

    Example:
        placements=[{"designator": "R1", "x": 1000, "y": 2000, "rotation": 90},
                    {"designator": "C5", "x": 1050, "y": 2000, "layer": "bottom"}]

    Returns:
        str: JSON object with placed_count, missing_designators, and the final
             x/y/rotation/layer of each placed component as Altium reports them
    """
    logger.info(f"Placing {len(placements)} components")

    # Flatten each placement into a pipe-delimited string
    # ('Designator|X|Y|Rotation|Layer') - the DelphiScript side parses the
    # request line by line, so nested JSON objects are not safe to send
    entries = []
    errors = []
    for idx, placement in enumerate(placements):
        if not isinstance(placement, dict):
            errors.append(f"placements[{idx}] must be an object")
            continue

        designator = str(placement.get("designator", "")).strip()
        x = placement.get("x")
        y = placement.get("y")

        if not designator or x is None or y is None:
            errors.append(f"placements[{idx}] must include designator, x, and y")
            continue
        if "|" in designator:
            errors.append(f"placements[{idx}] designator must not contain '|'")
            continue

        rotation = placement.get("rotation", -1)
        layer = str(placement.get("layer", "") or "").strip().lower()
        if layer not in ("", "top", "bottom"):
            errors.append(f"placements[{idx}] layer must be 'top' or 'bottom'")
            continue

        locked = placement.get("locked")
        if locked is not None and not isinstance(locked, bool):
            errors.append(f"placements[{idx}] locked must be true or false")
            continue
        lock = "" if locked is None else ("1" if locked else "0")
        try:
            texts = "|".join(pcb_layout.text_spec(placement.get(k), f"placements[{idx}] {k}")
                             for k in ("designator_text", "comment_text"))
        except pcb_layout.LayoutError as e:
            errors.append(str(e))
            continue
        try:
            entry = f"{designator}|{float(x)}|{float(y)}|{float(rotation)}|{layer}|{lock}|{texts}"
        except (TypeError, ValueError):
            errors.append(f"placements[{idx}] x, y, and rotation must be numbers")
            continue
        entries.append(entry)

    if errors:
        return json.dumps({"success": False, "error": "; ".join(errors)})
    if not entries:
        return json.dumps({"success": False, "error": "No placements provided"})

    params = {"placements": entries}
    if board_path:
        params["board_path"] = long_path(board_path)
    response = await altium_bridge.execute_command("place_components", params)

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("place_components", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error placing components: {error_msg}")
        return json.dumps(
            {"success": False, "error": f"Failed to place components: {error_msg}"}
        )

    result = response.get("result", {})
    logger.info("Placed components successfully")
    return json.dumps({"success": True, "result": result}, indent=2)


def _mst_length(points: list) -> float:
    """Minimum-spanning-tree length over (x, y) points (Prim's algorithm)."""
    n = len(points)
    if n < 2:
        return 0.0
    import math

    in_tree = [False] * n
    best = [float("inf")] * n
    best[0] = 0.0
    total = 0.0
    for _ in range(n):
        u = min((i for i in range(n) if not in_tree[i]), key=lambda i: best[i])
        in_tree[u] = True
        total += best[u]
        ux, uy = points[u]
        for v in range(n):
            if not in_tree[v]:
                d = math.dist((ux, uy), points[v])
                best[v] = min(best[v], d)
    return total

@_tool(read_only=True)
async def get_net_connections(ctx: Context, cmp_designators: list = None, max_pads_per_net: int = 40) -> str:
    """
    Get net connectivity and airline (unrouted connection) lengths for the
    nets touching the given components (supports PCB layout and harness wiring).

    Use this to plan or score a placement: it shows every pad on each net -
    including pads of OTHER components outside the given set (e.g. an input
    filter the cluster must connect to) - plus the net's minimum-spanning-tree
    airline length in mils. For harness projects, returns connector pin-to-net
    mappings for all requested connector nets.

    Args:
        cmp_designators (list, optional): Components whose nets to analyze
            (e.g. ["U12", "R42"]). Omit to use the current Altium selection.
        max_pads_per_net (int): Nets with more pads than this (e.g. GND)
            return only pads belonging to the given components, plus the
            total pad_count. Their airline is also skipped. Default 40.

    Returns:
        str: JSON object with one entry per net: pad_count,
             airline_mst_mils (None for large nets), and pads
             [{designator, pin, x, y}, ...] in mils relative to the board
             origin. Large nets set pads_truncated=true.
    """
    logger.info(f"Getting net connections (designators={cmp_designators})")

    params = {}
    if cmp_designators:
        params["designators"] = cmp_designators

    response = await altium_bridge.execute_command("get_net_connections", params)

    if not response.get("success", False):
        h_path = await _get_active_harness_path()
        if h_path:
            h_conns = get_harness_net_connections(h_path)
            if cmp_designators:
                des_set = set(cmp_designators)
                h_conns = [
                    c
                    for c in h_conns
                    if any(p["designator"] in des_set for p in c["pins"])
                ]
            out_nets = []
            for c in h_conns:
                out_nets.append(
                    {
                        "net": c["net"],
                        "pad_count": c["pin_count"],
                        "airline_mst_mils": None,
                        "pads": [
                            {
                                "designator": p["designator"],
                                "pin": p["pin"],
                                "name": p["pin_name"],
                                "sheet": p.get("sheet", ""),
                            }
                            for p in c["pins"]
                        ],
                        "source": "harness_wiring",
                    }
                )
            logger.info(
                f"Retrieved {len(out_nets)} harness net connections as fallback"
            )
            return json.dumps({"nets": out_nets, **_disk_source(h_path)}, indent=2)

        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting net connections: {error_msg}")
        return json.dumps(
            {"success": False, "error": f"Failed to get net connections: {error_msg}"}
        )

    result = response.get("result", {})
    if isinstance(result, str):
        result = json.loads(result)

    target_set = set(cmp_designators or result.get("targets", []))

    # Group the flat pad list by net and compute airline lengths
    nets = {}
    for pad in result.get("pads", []):
        nets.setdefault(pad["net"], []).append(pad)

    out_nets = []
    for name in result.get("net_names", []):
        pads = nets.get(name, [])
        entry = {"net": name, "pad_count": len(pads)}
        if len(pads) <= max_pads_per_net:
            entry["airline_mst_mils"] = round(
                _mst_length([(p["x"], p["y"]) for p in pads]), 1
            )
            entry["pads"] = [
                {
                    "designator": p["designator"],
                    "pin": p["pin"],
                    "x": p["x"],
                    "y": p["y"],
                }
                for p in pads
            ]
        else:
            entry["airline_mst_mils"] = None
            entry["pads_truncated"] = True
            entry["pads"] = [
                {
                    "designator": p["designator"],
                    "pin": p["pin"],
                    "x": p["x"],
                    "y": p["y"],
                }
                for p in pads
                if not target_set or p["designator"] in target_set
            ]
        out_nets.append(entry)

    out_nets.sort(
        key=lambda e: (e["airline_mst_mils"] is None, -(e["airline_mst_mils"] or 0))
    )

    logger.info(f"Net connections: {len(out_nets)} nets")
    return json.dumps({"net_count": len(out_nets), "nets": out_nets}, indent=2)


def _rotate_offset(dx: float, dy: float, degrees: float) -> tuple:
    """Rotate a rotation-0 pad offset counterclockwise by the given angle."""
    import math

    rad = math.radians(degrees)
    return (
        dx * math.cos(rad) - dy * math.sin(rad),
        dx * math.sin(rad) + dy * math.cos(rad),
    )


@_tool(read_only=True)
async def check_orientation(ctx: Context, cmp_designators: list = None, min_improvement_mils: float = 25) -> str:
    """
    Advisory check: find 2-pad passives whose rotation could be improved.

    For each 2-pad component in the target set, every orthogonal rotation
    (0/90/180/270) is scored in place, summing one term per pad:
    - routable nets (few pads): the net's total airline (MST) length with
      this pad at the candidate position - so a pad that merely slides along
      a pass-through flow (e.g. a bulk cap in a power chain) scores as
      nearly free, while a genuine detour costs its real length
    - plane nets (many pads, e.g. GND): distance to the nearest same-net
      pad - a capacitor's ground-return loop is local, so proximity to the
      IC's GND/thermal pad is what matters
    A component is reported when a different rotation beats the current one
    by at least min_improvement_mils.

    This is advisory, not pass/fail: it measures geometry only and knows
    nothing about net criticality. Suggestions matter for loop-critical
    parts (capacitor ground returns, snubbers, input/output filters) and
    should usually be ignored for parts whose orientation is electrically
    arbitrary (pull-ups, strapping resistors, enables) or where a datasheet
    layout recommendation says otherwise. Weigh suggestions with judgement
    rather than applying them blindly. If a suggestion with
    bbox_changes=true is applied (a 90-degree change alters the body
    outline), re-run check_placement afterwards.

    Args:
        cmp_designators (list, optional): Components to check. Omit to use
            the current Altium selection. Components with more or fewer than
            2 pads are skipped.
        min_improvement_mils (float): Only report components where the best
            rotation improves the connection score by at least this many
            mils (default 25).

    Returns:
        str: JSON object with checked_count and suggestions, each having
             designator, current_rotation, suggested_rotation,
             improvement_mils, bbox_changes, and a per-pad breakdown of
             nearest same-net distances at the current vs suggested rotation.
    """
    import math

    logger.info(f"Checking orientation (designators={cmp_designators})")

    # Resolve the target designators from the selection when not given
    if not cmp_designators:
        sel_resp = await altium_bridge.execute_command(
            "get_selected_components_coordinates", {}
        )
        if not sel_resp.get("success", False):
            h_err = await _pcb_only_tool_error(
                "check_orientation", sel_resp.get("error", "Unknown error")
            )
            if h_err:
                return h_err
            return json.dumps(
                {"success": False, "error": sel_resp.get("error", "Unknown error")}
            )
        sel = sel_resp.get("result", [])
        if isinstance(sel, str):
            sel = json.loads(sel)
        cmp_designators = [c["designator"] for c in sel if "designator" in c]
        if not cmp_designators:
            return json.dumps(
                {
                    "success": False,
                    "error": "No components selected and no designators given",
                }
            )

    pins_resp = await altium_bridge.execute_command(
        "get_component_pins", {"designators": cmp_designators}
    )
    if not pins_resp.get("success", False):
        h_err = await _pcb_only_tool_error(
            "check_orientation", pins_resp.get("error", "Unknown error")
        )
        if h_err:
            return h_err
        return json.dumps(
            {"success": False, "error": pins_resp.get("error", "Unknown error")}
        )
    comps = pins_resp.get("result", [])
    if isinstance(comps, str):
        comps = json.loads(comps)

    nets_resp = await altium_bridge.execute_command(
        "get_net_connections", {"designators": cmp_designators}
    )
    if not nets_resp.get("success", False):
        return json.dumps(
            {"success": False, "error": nets_resp.get("error", "Unknown error")}
        )
    net_data = nets_resp.get("result", {})
    if isinstance(net_data, str):
        net_data = json.loads(net_data)

    # net name -> list of (designator, x, y) for every pad on the net
    net_pads = {}
    for p in net_data.get("pads", []):
        net_pads.setdefault(p["net"], []).append((p["designator"], p["x"], p["y"]))

    PLANE_NET_PAD_COUNT = 40  # nets above this are treated as planes

    suggestions = []
    checked = 0
    for comp in comps:
        pins = comp.get("pins", [])
        if len(pins) != 2 or "x" not in comp:
            continue
        mirror = comp.get("layer") == "Bottom Layer"

        def score(rotation):
            """Hybrid per-pad score (see docstring); None if no pad scores."""
            total, detail = 0.0, []
            for pin in pins:
                net = pin.get("net", "")
                cands = (
                    [
                        (x, y)
                        for (d, x, y) in net_pads.get(net, [])
                        if d != comp["designator"]
                    ]
                    if net
                    else []
                )
                if not cands:
                    continue
                dx = -pin["dx"] if mirror else pin["dx"]
                ox, oy = _rotate_offset(dx, pin["dy"], rotation)
                px, py = comp["x"] + ox, comp["y"] + oy
                if len(cands) > PLANE_NET_PAD_COUNT:
                    value = min(math.dist((px, py), c) for c in cands)
                    metric = "nearest_return_mils"
                else:
                    value = _mst_length(cands + [(px, py)])
                    metric = "net_airline_mils"
                total += value
                detail.append({"pin": pin["name"], "net": net, metric: round(value, 1)})
            return (total, detail) if detail else None

        current = comp.get("rotation", 0) % 360
        cur = score(current)
        if cur is None:
            continue
        checked += 1

        best_rot, best = current, cur
        for r in (0, 90, 180, 270):
            s = score(r)
            if s is not None and s[0] < best[0]:
                best_rot, best = r, s

        improvement = cur[0] - best[0]
        if best_rot != current and improvement >= min_improvement_mils:
            suggestions.append(
                {
                    "designator": comp["designator"],
                    "current_rotation": current,
                    "suggested_rotation": best_rot,
                    "improvement_mils": round(improvement, 1),
                    "bbox_changes": (best_rot - current) % 180 != 0,
                    "current_pads": cur[1],
                    "suggested_pads": best[1],
                }
            )

    suggestions.sort(key=lambda s: -s["improvement_mils"])
    logger.info(
        f"Orientation check: {len(suggestions)} suggestions from {checked} components"
    )
    return json.dumps(
        {
            "checked_count": checked,
            "min_improvement_mils": min_improvement_mils,
            "suggestion_count": len(suggestions),
            "suggestions": suggestions,
        },
        indent=2,
    )


@_tool(read_only=True)
async def check_placement(ctx: Context, cmp_designators: list = None, clearance_mils: float = 6) -> str:
    """
    Verify component placement: find overlaps and clearance violations.

    Checks each target component against every other component on the same
    side of the board. Bounding boxes (which include silkscreen) are used as
    a fast prefilter; close pairs are then measured precisely with Altium's
    primitive-to-primitive distance, so reported distances are true minimum
    distances between any primitives (pads, silk, etc.) of the two parts.

    Run this after placing components - a screenshot is not verification.

    Args:
        cmp_designators (list, optional): Components to check (e.g. ["U12", "R42"]).
            Omit to check the components currently selected in Altium.
        clearance_mils (float): Minimum allowed primitive-to-primitive distance
            in mils (default 6). Pairs closer than this are reported.

    Returns:
        str: JSON object with checked_count, violation_count, and a violations
             list. Each violation has a/b designators, type
             ("bounding_box_overlap" = the parts' outlines intersect, or
             "clearance" = distance below threshold), distance_mils (0 =
             touching/overlapping copper or silk), overlap sizes when boxes
             intersect, and the other part's x/y position. An empty violations
             list means the placement is clean at the given clearance.
    """
    logger.info(
        f"Checking placement (designators={cmp_designators}, clearance={clearance_mils})"
    )

    params = {"clearance_mils": clearance_mils}
    if cmp_designators:
        params["designators"] = cmp_designators

    response = await altium_bridge.execute_command("check_placement", params)

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("check_placement", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error checking placement: {error_msg}")
        return json.dumps(
            {"success": False, "error": f"Failed to check placement: {error_msg}"}
        )

    result = response.get("result", {})
    logger.info(
        f"Placement check complete: {result.get('violation_count', '?')} violations"
    )
    return json.dumps(result, indent=2)

@_tool(read_only=True)
async def check_routing(ctx: Context, nets: list = None, angle_tolerance_deg: float = 0.1,
                        max_findings: int = 50) -> str:
    """
    Check the routed copper of the focused PCB for segments that are not 0,
    45 or 90 degrees, and for right-angle and acute corners.

    Run this after changing tracks or vias - a screenshot does not show a
    stub that is 2 degrees off 45. Such stubs come from a diagonal whose end
    was snapped to a grid on its own, or from a track end dragged along
    when a via or component moved. Only tracks with a net that are not part
    of a component or polygon pour count. A corner is where exactly two
    track ends of one net meet on one layer; under a via or pad it is not
    a corner.

    Args:
        nets (list, optional): Check only these nets (exact names, any case).
        angle_tolerance_deg (float): How far a segment may be from a
            multiple of 45 degrees (default 0.1).
        max_findings (int): Most entries listed per kind (default 50). The
            counts always cover every segment.

    Returns:
        str: JSON with clean (true when nothing was found), the segment
             count, segment_angles (orthogonal / 45 / off-angle counts),
             corner_turns (straight / 45 / 90 / acute counts), and the
             off_angle segments and sharp_corners: net, layer, positions in
             mils relative to the board origin, angle or turn in degrees.
    """
    logger.info(f"Checking routing (nets={nets}, tolerance={angle_tolerance_deg})")

    response = await altium_bridge.execute_command("get_routing_geometry", {})
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error reading routing: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to read the routing: {error_msg}"})

    geo = routing.parse_geometry(str(response.get("result", "")))
    report = routing.check(geo, angle_tolerance_deg, nets)
    report["clean"] = not report["off_angle"] and not report["sharp_corners"]
    report["routed_arcs"] = len(geo["arcs"])
    for key in ("off_angle", "sharp_corners"):
        report[f"{key}_count"] = len(report[key])
        report[key] = report[key][:max(0, max_findings)]
    logger.info(f"Routing check: {report['off_angle_count']} off-angle, "
                f"{report['sharp_corners_count']} sharp corners in {report['segments']} segments")
    return json.dumps(report, indent=2)

@_tool()
async def place_polygon_pour(ctx: Context, outline: list, layer: str, net: str = "", name: str = "",
                             hatch: str = "solid", pour_over_same_net: bool = True,
                             remove_dead_copper: bool = True, track_width_mils: float = 0,
                             grid_mils: float = 0, board_path: str = "") -> str:
    """
    Add a polygon pour (copper area, e.g. a ground pour) to a PCB and pour it.

    The pour keeps the board's clearance rules from other nets and joins
    same-net pads and tracks as the polygon connect rule says. Cut holes in
    it with place_cutout.

    Args:
        outline (list): Corners [[x, y], ...] in mils relative to the board
            origin, in order around the shape (at least three; it closes by
            itself). Straight edges only.
        layer (str): Signal layer, by its stack name or Altium's name, e.g.
            "Top Layer".
        net (str): Net to pour; it must exist on the board. Empty for an
            unconnected pour.
        name (str): Polygon name; empty lets Altium name it.
        hatch (str): "solid" (default), "hatched_45", "hatched_90", or
            "none" (outline only).
        pour_over_same_net (bool): Pour over same-net tracks and pads.
        remove_dead_copper (bool): Remove islands not connected to the net.
        track_width_mils (float): Hatch track width; 0 for Altium's default.
        grid_mils (float): Hatch grid; 0 for Altium's default.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with the polygon's name, layer, net and what it poured:
             poured_regions, one per separate piece of solid copper, and
             hatch_tracks for a hatched pour. When nothing was poured it
             says why it may be: no room, or every piece touched nothing on
             its net and was removed as dead copper.
    """
    import pcb_shapes

    board_path = long_path(board_path)
    points, error = pcb_shapes.outline_points(outline)
    if error:
        return json.dumps({"success": False, "error": error})
    if hatch not in ("solid", "hatched_45", "hatched_90", "none"):
        return json.dumps({"success": False, "error": "hatch is solid, hatched_45, hatched_90 or none"})
    if track_width_mils < 0 or grid_mils < 0:
        return json.dumps({"success": False, "error": "track_width_mils and grid_mils cannot be negative"})
    logger.info(f"Placing a polygon pour on {layer} ({net or 'no net'})")
    response = await altium_bridge.execute_command("create_polygon_pour", {
        "outline": pcb_shapes.encode(points), "layer": layer, "net": net, "name": name,
        "hatch": hatch, "pour_over_same_net": "true" if pour_over_same_net else "false",
        "remove_dead_copper": "true" if remove_dead_copper else "false",
        "track_size": int(round(track_width_mils * pcb_shapes.COORD_PER_MIL)),
        "grid": int(round(grid_mils * pcb_shapes.COORD_PER_MIL)), "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to add the polygon: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    if hatch != "none" and not result.get("poured_regions") and not result.get("hatch_tracks"):
        result["warning"] = ("Nothing was poured: there is no room inside the outline, or every piece "
                             "touched nothing on its net and was removed as dead copper "
                             "(remove_dead_copper).")
    return json.dumps(result, indent=2)

@_tool()
async def place_cutout(ctx: Context, outline: list, kind: str = "polygon", layer: str = "",
                       board_path: str = "") -> str:
    """
    Add a cutout to a PCB: a hole in polygon pours, or in the board itself.

    kind "polygon" keeps polygon pours out of the area on one signal layer
    (or on every signal layer with layer "all"), as Altium's Polygon Pour
    Cutout does. kind "board" cuts the area out of the board (a slot or
    opening). Either way the polygons the cutout overlaps are repoured.

    Args:
        outline (list): Corners [[x, y], ...] in mils relative to the board
            origin, in order around the shape (at least three).
        kind (str): "polygon" (default) or "board".
        layer (str): For a polygon cutout, the signal layer ("Top Layer",
            a stack name) or "all". Ignored for a board cutout.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with how many cutout regions were added and how many
             polygons were repoured.
    """
    import pcb_shapes

    board_path = long_path(board_path)
    points, error = pcb_shapes.outline_points(outline)
    if error:
        return json.dumps({"success": False, "error": error})
    if kind not in ("polygon", "board"):
        return json.dumps({"success": False, "error": "kind is polygon or board"})
    if kind == "polygon" and not layer.strip():
        return json.dumps({"success": False, "error": "a polygon cutout needs a layer, or \"all\""})
    logger.info(f"Placing a {kind} cutout")
    response = await altium_bridge.execute_command("create_cutout", {
        "outline": pcb_shapes.encode(points), "kind": kind, "layer": layer, "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to add the cutout: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

@_tool()
async def repour_polygons(ctx: Context, names: list = None, net: str = "", layer: str = "",
                          area: list = None, include_shelved: bool = False, board_path: str = "") -> str:
    """
    Repour polygon pours on a PCB: all of them, or the ones the filters pick.

    Repour after moving parts, tracks or vias through a pour, or after
    changing clearance or polygon connect rules; Altium does not repour by
    itself. The whole repour is one undo step.

    Filters combine; with none, every polygon on the board is repoured.

    Args:
        names (list): Polygon names, any case. Names need not be unique (a
            "GND" pour on each layer is common), so add layer to pick one.
        net (str): Only polygons on this net.
        layer (str): Only polygons on this signal layer, by stack name or
            Altium's name ("Top Layer").
        area (list): [x1, y1, x2, y2] in mils from the board origin: only
            polygons overlapping it, e.g. around an edit. Overlapping, so a
            whole-board pour counts for any area on it.
        include_shelved (bool): Also repour shelved polygons, which
            unshelves them. By default they are left shelved and listed.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with how many polygons were repoured and, for each, its
             name, layer, net and poured_regions (separate pieces of
             copper) and hatch_tracks. shelved_skipped lists the shelved
             polygons that matched and were left alone.
    """
    import pcb_shapes

    board_path = long_path(board_path)
    names, encoded, error = pcb_shapes.polygon_names(names)
    if error:
        return json.dumps({"success": False, "error": error})
    box, error = pcb_shapes.area_box(area)
    if error:
        return json.dumps({"success": False, "error": error})
    logger.info(f"Repouring polygons (names={names or 'any'}, net={net or 'any'}, layer={layer or 'any'})")
    response = await altium_bridge.execute_command("repour_polygons", {
        "names": encoded, "net": net, "layer": layer, "area": box,
        "include_shelved": "true" if include_shelved else "false", "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to repour: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    missing = pcb_shapes.unmatched_names(names, result)
    if missing:
        result["names_not_matched"] = missing
    if not result.get("repoured") and not result.get("shelved_skipped"):
        result["warning"] = "No polygon matched the filters."
    return json.dumps(result, indent=2)

# Change the design: a timeout leaves their outcome unknown, so no blind retry
MUTATING_COMMANDS.update({"create_polygon_pour", "create_cutout", "repour_polygons"})

@_tool(read_only=True)
async def get_pour_report(ctx: Context, names: list = None, net: str = "", layer: str = "",
                          area: list = None, sliver_sq_mils: float = 100, board_path: str = "") -> str:
    """
    Check what polygon pours poured: islands, slivers, and pours with no net
    or no copper. Read only; repour first (repour_polygons) to check the
    copper as the rules now make it.

    Each polygon is listed with its pieces of solid copper and their sizes,
    and flags:
      * islands - poured into more than one separate piece;
      * slivers - a piece smaller than sliver_sq_mils;
      * no_net - the polygon has no net (dead copper);
      * no_copper - poured nothing (no room, or all removed as dead copper);
      * shelved - shelved, so it has no copper now.
    A piece's area is its outline, holes not subtracted: an upper bound.

    Args:
        names, net, layer, area: Filters, as for repour_polygons; with none,
            every polygon on the board.
        sliver_sq_mils (float): A piece below this area is a sliver
            (default 100 sq mil).
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with each polygon (name, layer, net, pieces, smallest
             piece, copper area, slivers, flags) and the flagged ones first.
    """
    import pcb_shapes

    names, encoded, error = pcb_shapes.polygon_names(names)
    if error:
        return json.dumps({"success": False, "error": error})
    box, error = pcb_shapes.area_box(area)
    if error:
        return json.dumps({"success": False, "error": error})
    if (not isinstance(sliver_sq_mils, (int, float)) or isinstance(sliver_sq_mils, bool)
            or not sliver_sq_mils >= 0 or sliver_sq_mils == float("inf")):
        return json.dumps({"success": False, "error": "sliver_sq_mils must be a number of at least 0"})
    response = await altium_bridge.execute_command("pour_report", {
        "names": encoded, "net": net, "layer": layer, "area": box,
        # Altium reads it as a 32-bit integer; no piece of copper is near this
        "sliver_sq_mils": int(round(min(sliver_sq_mils, 2e9))), "board_path": long_path(board_path)})
    if not response.get("success", False):
        return json.dumps({"success": False, "error": response.get("error", "unknown error")})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    polys = result.get("polygons", [])
    polys.sort(key=lambda p: (not p.get("flags"), p.get("layer", ""), p.get("name", "")))
    result["flagged"] = sum(1 for p in polys if p.get("flags"))
    return json.dumps(result, indent=2, ensure_ascii=False)

async def _edit_pcb_objects(params: dict, board_path: str) -> str:
    response = await altium_bridge.execute_command("edit_pcb_objects", {**params, "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False, "error": response.get("error", "unknown error")})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    if not result.get("matched"):
        result["warning"] = "No object matched the filters."
    elif result.get("matched", 0) > len(result.get("objects", [])):
        result["note"] = f"objects lists the first {len(result.get('objects', []))} of {result['matched']}."
    return json.dumps(result, indent=2)

@_tool()
async def delete_pcb_objects(ctx: Context, kinds: list = None, net: str = "", layer: str = "",
                             area: list = None, designators: list = None, margin_mils: float = 40,
                             include_locked: bool = False, dry_run: bool = False, board_path: str = "") -> str:
    """
    Delete copper from a PCB: tracks, arcs, vias, polygon pours, copper
    regions, fills or pour cutouts. Use it to rip up routing.

    Objects are picked by every filter given: kinds, net, layer and area,
    or a group's copper by designators. At least one must narrow the match;
    deleting all the copper on the board is refused. Only copper is ever
    touched: never pads, anything belonging to a component, keepouts, or
    objects on other layers (board outline, silkscreen, mechanical). The
    copper a pour poured goes with its pour. Locked objects are left alone
    and listed, unless include_locked. One undo step in Altium. Use dry_run
    first to see what would go.

    Deleting copper inside a pour leaves the pour as it was: repour it.
    Remove a via stitching group with Altium's own command rather than
    here, so the group goes too.

    Args:
        kinds (list): Some of "track", "arc", "via", "polygon", "region",
            "fill", "cutout" (a pour cutout). Empty: all but cutouts.
        net (str): Only objects on this net.
        layer (str): Only objects on this copper layer, by stack name or
            Altium's name ("Top Layer"); a via matches every layer it spans.
        area (list): [x1, y1, x2, y2] in mils from the board origin; only
            objects lying inside it.
        designators (list): Rip up a group's copper, as get_layout lists it:
            what lies inside the group's extent (grown by margin_mils, or
            area when given) plus the copper of its own nets (nets with no
            pad on another part) wherever it runs.
        margin_mils (float): See designators (default 40).
        include_locked (bool): Delete locked objects too.
        dry_run (bool): List what matches without deleting.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with matched, deleted, the first 50 objects (kind, net,
             layer, position in mils), and locked_skipped with the first
             20 locked objects left alone.
    """
    import pcb_edit

    board_path = long_path(board_path)
    params, error = pcb_edit.filters(kinds, net, layer, area, designators, margin_mils, include_locked)
    if error:
        return json.dumps({"success": False, "error": error})
    if not pcb_edit.narrowed(kinds, net, layer, area, designators):
        return json.dumps({"success": False, "error": (
            "Give at least one filter (kinds, net, layer, area or designators): this would delete all the "
            "copper on the board.")})
    logger.info(f"Deleting PCB objects {params} (dry run: {dry_run})")
    return await _edit_pcb_objects({**params, "action": "delete", "dry_run": "true" if dry_run else "false"},
                                   board_path)

@_tool()
async def modify_pcb_objects(ctx: Context, kinds: list = None, net: str = "", layer: str = "",
                             area: list = None, designators: list = None, margin_mils: float = 40,
                             width_mils: float = None, via_diameter_mils: float = None,
                             via_hole_mils: float = None, new_net: str = None, new_layer: str = "",
                             include_locked: bool = False, dry_run: bool = False, board_path: str = "") -> str:
    """
    Change copper on a PCB: the width of tracks and arcs, the size of vias,
    the net, or the layer.

    Objects are picked as for delete_pcb_objects, and at least one filter
    must narrow the match. Each change applies where it makes sense:
    width_mils to tracks and arcs, via_diameter_mils and via_hole_mils to
    vias, new_layer to tracks, arcs, fills and regions, new_net to all ("" removes
    the net). A via whose hole would not fit inside its diameter is left as
    it is and counted. A polygon given another net pours again. Locked
    objects are left alone unless include_locked. One undo step in Altium.

    Changed copper inside a pour leaves the pour as it was: repour it, and
    run DRC after changing widths or via sizes.

    Args:
        kinds, net, layer, area, designators, margin_mils, include_locked:
            The filters, as for delete_pcb_objects.
        width_mils (float): New width of tracks and arcs.
        via_diameter_mils, via_hole_mils (float): New via size; either alone
            is checked against each via's other size.
        new_net (str): New net; "" removes the net; leave out to keep it.
        new_layer (str): Copper layer to move tracks, arcs, fills and
            regions to.
        dry_run (bool): List what matches without changing it.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with matched, changed, unchanged (matched objects none of
             the changes applies to), vias_left_as_hole_would_not_fit,
             locked_skipped, and the first 50 objects as they were.
    """
    import pcb_edit

    board_path = long_path(board_path)
    params, error = pcb_edit.filters(kinds, net, layer, area, designators, margin_mils, include_locked)
    if error:
        return json.dumps({"success": False, "error": error})
    if not pcb_edit.narrowed(kinds, net, layer, area, designators):
        return json.dumps({"success": False, "error": (
            "Give at least one filter (kinds, net, layer, area or designators): this would change all the "
            "copper on the board.")})
    change, error = pcb_edit.changes(width_mils, via_diameter_mils, via_hole_mils, new_net, new_layer)
    if error:
        return json.dumps({"success": False, "error": error})
    logger.info(f"Modifying PCB objects {params}: {change} (dry run: {dry_run})")
    return await _edit_pcb_objects({**params, **change, "action": "modify",
                                    "dry_run": "true" if dry_run else "false"}, board_path)

MUTATING_COMMANDS.add("edit_pcb_objects")

@_tool()
async def route_track(ctx: Context, net_name: str, layer: str, width_mils: float, points: list,
                      diagonal_first: bool = False, corner_chamfer_mils: float = 0,
                      dry_run: bool = False) -> str:
    """
    Add a routed track through waypoints on the focused PCB, every segment
    exactly 0, 45 or 90 degrees.

    Between two waypoints that are not in line, the track takes a straight
    leg and a 45 degree diagonal. The diagonal is computed from its own
    start point, so its x and y runs are equal: nothing is snapped on its
    own, which is what leaves stubs a few degrees off 45. Use this instead
    of placing track segments from run_altium_script, and run
    check_routing afterwards. It adds tracks only: no vias, and no DRC.

    Args:
        net_name (str): Net of the track; it must exist on the board.
        layer (str): Layer name as get_component_pins and check_routing
            report it, e.g. "Top Layer" or "Bottom Layer".
        width_mils (float): Track width in mils.
        points (list): Waypoints [[x, y], ...] in mils relative to the
            board origin, at least two: usually a pad centre, any bends,
            and the far pad or via centre.
        diagonal_first (bool): Put the diagonal at the start of each
            stretch instead of its end (default: straight, then diagonal).
        corner_chamfer_mils (float): Cut each right-angle corner between two
            straight legs with a 45 degree segment, setting back this far on
            each leg (at most half the shorter leg). 0 leaves them square.
        dry_run (bool): Only compute and return the segments.

    Returns:
        str: JSON with the segments in mils ([x1, y1, x2, y2]) and, unless
             dry_run, how many tracks were added.
    """
    logger.info(f"Routing {net_name} on {layer} through {len(points or [])} points")

    def coord(v):
        return int(round(float(v) * routing.COORD_PER_MIL))

    try:
        waypoints = [(coord(p[0]), coord(p[1])) for p in points]
    except (TypeError, ValueError, IndexError):
        return json.dumps({"success": False, "error": "points must be a list of [x, y] pairs in mils"})
    if len(waypoints) < 2:
        return json.dumps({"success": False, "error": "points needs at least two waypoints"})
    if width_mils <= 0:
        return json.dumps({"success": False, "error": "width_mils must be positive"})

    segments = routing.route(waypoints, diagonal_first, coord(corner_chamfer_mils))
    if not segments:
        return json.dumps({"success": False, "error": "the waypoints are all the same point"})
    result = {"segments_mils": [[routing.to_mils(v) for v in s] for s in segments]}
    if dry_run:
        return json.dumps({"success": True, "dry_run": True, **result}, indent=2)

    response = await altium_bridge.execute_command("create_route_tracks", {
        "net_name": net_name,
        "layer": layer,
        "width": coord(width_mils),
        "segments": ";".join("|".join(str(v) for v in s) for s in segments),
    })
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error routing track: {error_msg}")
        return json.dumps({"success": False, "error": f"Failed to add the track: {error_msg}", **result})

    created = response.get("result", {})
    if isinstance(created, dict):
        result.update({k: v for k, v in created.items() if k != "success"})
    return json.dumps({"success": True, **result}, indent=2)


# Board snapshot for the silkscreen tools. An export takes a few seconds, so
# views and option queries reuse it. Moves made through
# set_designator_positions are written into it, check_silkscreen always
# refreshes it, and it is re-exported once older than SILK_SNAPSHOT_SECONDS.
# Edits made by hand in Altium are seen after that, or with refresh=True.
SILK_SNAPSHOT_SECONDS = 900
_silk_snapshot = {"board": None, "time": 0.0}

async def _silk_board(refresh: bool = False):
    """The board's silkscreen geometry, exported from Altium when needed."""
    snap = _silk_snapshot
    if refresh or snap["board"] is None or time.time() - snap["time"] > SILK_SNAPSHOT_SECONDS:
        response = await altium_bridge.execute_command("export_silkscreen_data", {})
        if not response.get("success", False):
            raise RuntimeError(response.get("error", "Unknown error"))
        result = response.get("result", {})
        if isinstance(result, str):
            result = json.loads(result)
        text = read_altium_text(result["file"])
        snap["board"], snap["time"] = silkscreen.Board(text), time.time()
    return snap["board"]

async def _check_silk_drc(designators: list = None) -> dict:
    params = {"designators": designators} if designators else {}
    response = await altium_bridge.execute_command("check_silkscreen", params)
    if not response.get("success", False):
        raise RuntimeError(response.get("error", "Unknown error"))
    result = response.get("result", {})
    return json.loads(result) if isinstance(result, str) else result

def _group_silk_violations(drc: dict) -> dict:
    """Altium's per-object silk violations as {designator: [description]}."""
    grouped = {}
    for v in drc.get("violations", []):
        what = v.get("object", "")
        if v.get("owner"):
            what += f" {v['owner']}" + (f"-{v['detail']}" if v.get("object") == "Pad" and v.get("detail") else "")
        elif v.get("detail"):
            what += f" '{v['detail']}'"
        if "distance_mils" in v:
            what += f" ({v['distance_mils']} mil)"
        grouped.setdefault(v["designator"], []).append(f"{v['rule']}: {what}")
    return dict(sorted(grouped.items()))

def _silk_side(side, designators, board) -> str:
    """'T' or 'B' from a side argument, else from the first designator."""
    if side:
        s = str(side).strip().lower()
        if s in ("top", "t"):
            return "T"
        if s in ("bottom", "b", "bot"):
            return "B"
        raise ValueError("side must be 'top' or 'bottom'")
    for d in designators or ():
        c = board.components.get(d)
        if c is not None and c.side in ("T", "B"):
            return c.side
    return "T"

def _current_boxes(board, side=None) -> dict:
    return {d: (c.text_box, c.text_rotation) for d, c in board.components.items()
            if c.visible and c.side in ("T", "B") and (side is None or c.side == side)}

def _focus_around(board, designators, side, min_size=300.0):
    """Area showing the parts, their current labels, and some surroundings."""
    bb = silkscreen.focus_box(board, designators, side)
    if bb is None:
        return None
    for d in designators:
        c = board.components.get(d)
        if c is not None and c.side == side and c.visible:
            bb = silkscreen._union(bb, c.text_box)
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    hw, hh = max((bb[2] - bb[0]) / 2, min_size / 2), max((bb[3] - bb[1]) / 2, min_size / 2)
    return (cx - hw, cy - hh, cx + hw, cy + hh)

def _box_info(rect, rotation) -> dict:
    return {"x": round((rect[0] + rect[2]) / 2, 2), "y": round((rect[1] + rect[3]) / 2, 2),
            "rotation": round(rotation) % 360,
            "width": round(rect[2] - rect[0], 1), "height": round(rect[3] - rect[1], 1)}

SILK_VIEW_LEGEND = ("grid lines labelled in mils (board origin); copper = solder mask openings "
                    "(dark = vias); white = silk; grey names = part centres; yellow = designators; "
                    "red = designators with problems; cyan = the designators asked about, with a line "
                    "to their part; green numbered = options")

AUTOPLACE_POSITIONS = ["TopCenter", "CenterRight", "BottomCenter", "CenterLeft",
                       "TopLeft", "TopRight", "BottomLeft", "BottomRight"]
AUTOPLACE_ROTATIONS = {"component": 0, "horizontal": 1, "along_side": 2,
                       "along_axis": 3, "along_pins": 4, "klc": 5}
# The script's GUI defaults, used when there is no saved settings file
AUTOPLACE_DEFAULTS = {
    "failed_action": "center", "avoid_vias": True, "rotation_strategy": 5,
    "try_altered_rotation": True, "second_pass": True, "unhide_all": False,
    "auto_hide": [], "text_height_mils": 31.496, "stroke_width_mils": 5.906,
    "position_delta_mils": 16.535, "positions": AUTOPLACE_POSITIONS[:4],
}

def _to_mils(text: str):
    """'30mil', '0.8mm', '0.05in' or a bare number (mils) -> mils."""
    m = re.match(r"\s*([-+]?\d*\.?\d+)\s*(mil|mm|in)?\s*$", str(text), re.I)
    if not m:
        return None
    value, unit = float(m.group(1)), (m.group(2) or "mil").lower()
    return value * {"mil": 1.0, "mm": 1000 / 25.4, "in": 1000.0}[unit]

def _saved_autoplacer_settings() -> dict:
    """Options saved by the AutoPlaceSilkscreen GUI (AutoPlaceSilkscreen.ini in
    Altium's application data folder), in this tool's terms. Empty if none."""
    import configparser
    files = glob.glob(os.path.join(os.environ.get("APPDATA", ""), "Altium", "*", "AutoPlaceSilkscreen.ini"))
    if not files:
        return {}
    ini = configparser.ConfigParser()
    try:
        ini.read(max(files, key=os.path.getmtime))
        g = ini["General"]
    except (configparser.Error, KeyError):
        return {}
    flag = lambda key, default: g.get(key, "1" if default else "0").strip() in ("1", "true", "True")
    out = {
        "failed_action": {"0": "center", "1": "hide", "2": "restore"}.get(g.get("FailedPlacementOptions", "0"), "center"),
        "avoid_vias": flag("AvoidVias", True),
        "rotation_strategy": int(g.get("RotationStrategy", "5")),
        "try_altered_rotation": flag("TryAlteredRotation", True),
        "second_pass": flag("WiggleEnabled", True),
        "unhide_all": flag("UnhideAllDesignators", False),
        "auto_hide": [x.strip() for x in g.get("AutoHideList", "").split(",") if x.strip()]
                     if flag("AutoHideEnabled", False) else [],
        "text_height_mils": (_to_mils(g.get("FixedSize", "")) or 0) if flag("FixedSizeEnabled", True) else 0,
        "stroke_width_mils": (_to_mils(g.get("FixedWidth", "")) or 0) if flag("FixedWidthEnabled", True) else 0,
        "position_delta_mils": _to_mils(g.get("PositionDelta", "")) or AUTOPLACE_DEFAULTS["position_delta_mils"],
        "positions": [p for i, p in enumerate(AUTOPLACE_POSITIONS, 1) if flag(f"Position{i}", i <= 4)],
    }
    return out

@_tool()
async def auto_place_silkscreen(ctx: Context, designators: list = None, selected_only: bool = False,
                                failed_action: str = None, avoid_vias: bool = None,
                                rotation_strategy: str = None, positions: list = None,
                                try_altered_rotation: bool = None, second_pass: bool = None,
                                unhide_all: bool = None, auto_hide: list = None,
                                allow_under: list = None, text_height_mils: float = None,
                                stroke_width_mils: float = None, position_delta_mils: float = None,
                                component_outline_layer: str = None,
                                use_saved_settings: bool = True) -> str:
    """
    Bulk-place designators with the Silkscreen Auto Placer script - step 1 of
    silkscreen cleanup. Then finish by hand with the other silkscreen tools.

    Runs a headless port of the AutoPlaceSilkscreen script inside Altium. For
    every part, smallest first, it tries the enabled autopositions on an
    offset grid with shrinking text, then a retry pass (rotation flip, wider
    grid) and a 2nd-pass "wiggle" search up to 100 mil out. It avoids pads,
    component bodies, other silk and (optionally) vias, and keeps the Board
    Outline Clearance rule's distance from the board edge. Typically 80-95%
    of designators end up placed; the rest are handled per failed_action.
    Everything is one undo step. A whole-board run moves every designator:
    ask the user to save the board first.

    It is fast and good at the bulk, but it does not judge readability:
    labels can land far from their part (2nd-pass label blocks), next to a
    neighbour, or over open vias when avoid_vias is off. Always follow up:
    1. check_silkscreen - lists what needs work (failed designators, DRC,
       ambiguous or distant labels).
    2. view_silkscreen / get_designator_options - look at each problem area
       and find legal spots.
    3. set_designator_positions - place them (dry_run first for neighbours).

    Options default to the settings last saved by the script's GUI
    (AutoPlaceSilkscreen.ini) when use_saved_settings is on, else the GUI's
    defaults. Any argument given here overrides them.

    Args:
        designators (list, optional): Only place these designators. Omit for
            the whole board (or the selection with selected_only).
        selected_only (bool): Place only the components selected in Altium.
        failed_action (str): What to do with designators that could not be
            placed: "center" (centre them on their part, where
            check_silkscreen flags them), "hide", or "restore" (put them back
            where they were).
        avoid_vias (bool): Treat vias as obstacles.
        rotation_strategy (str): "component", "horizontal", "along_side",
            "along_axis", "along_pins" or "klc" (KLC style).
        positions (list): Autopositions to try, in order: TopCenter,
            CenterRight, BottomCenter, CenterLeft, TopLeft, TopRight,
            BottomLeft, BottomRight.
        try_altered_rotation (bool): Also try the text turned 90 degrees.
        second_pass (bool): Run the wider "wiggle" search for failures.
        unhide_all (bool): Show hidden designators first so they get placed.
        auto_hide (list): Designator prefixes to hide instead of placing,
            e.g. ["TP", "MT", "FID"]; [] hides none.
        allow_under (list): Designators whose bodies silk may cover.
        text_height_mils (float): Fixed text height; 0 = size from the part
            (shrinks to 25 mil when needed).
        stroke_width_mils (float): Fixed stroke width; 0 = from the height.
        position_delta_mils (float): Extra gap between autoposition and part.
        component_outline_layer (str): Mechanical layer holding component
            bodies (default "Mechanical 13" or a "Component Outline" layer).
        use_saved_settings (bool): Start from the GUI's saved settings.

    Returns:
        str: JSON with placed/failed counts per pass, the failed designators,
             the settings used and the next step.
    """
    settings = dict(AUTOPLACE_DEFAULTS)
    source = "script defaults"
    if use_saved_settings:
        saved = _saved_autoplacer_settings()
        if saved:
            settings.update(saved)
            source = "saved GUI settings (AutoPlaceSilkscreen.ini)"
    overrides = {"failed_action": failed_action, "avoid_vias": avoid_vias,
                 "try_altered_rotation": try_altered_rotation, "second_pass": second_pass,
                 "unhide_all": unhide_all, "auto_hide": auto_hide,
                 "text_height_mils": text_height_mils, "stroke_width_mils": stroke_width_mils,
                 "position_delta_mils": position_delta_mils, "positions": positions}
    settings.update({k: v for k, v in overrides.items() if v is not None})
    if rotation_strategy is not None:
        key = str(rotation_strategy).strip().lower().replace(" ", "_")
        if key.isdigit() and 0 <= int(key) <= 5:
            settings["rotation_strategy"] = int(key)
        elif key in AUTOPLACE_ROTATIONS:
            settings["rotation_strategy"] = AUTOPLACE_ROTATIONS[key]
        else:
            return json.dumps({"success": False, "error": f"rotation_strategy must be one of {list(AUTOPLACE_ROTATIONS)}"})
    if settings["failed_action"] not in ("center", "hide", "restore"):
        return json.dumps({"success": False, "error": "failed_action must be center, hide or restore"})
    order = {p.lower(): p for p in AUTOPLACE_POSITIONS}
    try:
        settings["positions"] = [order[str(p).strip().lower()] for p in settings["positions"]]
    except KeyError as e:
        return json.dumps({"success": False, "error": f"unknown position {e}; use {AUTOPLACE_POSITIONS}"})
    if not settings["positions"]:
        return json.dumps({"success": False, "error": "enable at least one position"})

    params = {k: v for k, v in settings.items() if k not in ("auto_hide", "positions",
                                                             "text_height_mils", "stroke_width_mils")}
    params.update({
        "positions": settings["positions"],
        "fixed_size_mils": float(settings["text_height_mils"] or 0),
        "fixed_width_mils": float(settings["stroke_width_mils"] or 0),
        "selected_only": bool(selected_only),
    })
    # Arrays are only sent when non-empty: the script reads '"key": []' as absent
    for key, value in (("designators", designators), ("auto_hide", settings["auto_hide"]),
                       ("allow_under", allow_under)):
        if value:
            params[key] = [str(v) for v in value]
    if component_outline_layer:
        params["outline_layer"] = component_outline_layer

    logger.info(f"auto_place_silkscreen ({source}): {params}")
    response = await altium_bridge.execute_command("auto_place_silkscreen", params, timeout=1800)
    _silk_snapshot["board"] = None      # every designator may have moved
    if not response.get("success", False):
        return json.dumps({"success": False, "error": f"Auto placer failed: {response.get('error', 'Unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        result = json.loads(result)
    if not result.get("outline_layer_found"):
        result["warning"] = ("no component outline layer found - component bodies were not treated "
                             "as obstacles; pass component_outline_layer")
    result.pop("outline_layer_found", None)
    names = {v: k for k, v in AUTOPLACE_ROTATIONS.items()}
    result["settings"] = dict(settings, rotation_strategy=names[settings["rotation_strategy"]], source=source)
    result["next"] = ("Run check_silkscreen, then fix what it lists with view_silkscreen, "
                      "get_designator_options and set_designator_positions.")
    return json.dumps(result, indent=2)

@_tool(read_only=True)
async def check_silkscreen(ctx: Context, designators: list = None, preview: bool = False):
    """
    Find the designators that need work. Start and finish silkscreen cleanup
    with this.

    Two parts:
    - drc: each designator is tested with Altium's own Silk To Silk
      Clearance, Silk To Solder Mask Clearance and Board Outline Clearance
      rules (Rule.ActualCheck, the test a batch DRC runs) against nearby
      silk, pads, vias, mask objects and the board edge. Ground truth for
      fabrication.
    - quality: problems DRC does not flag - text far from its part or closer
      to another part of the same type, over another part's body or under its
      own (e.g. left on top of its part by an auto-placer), off the board, or
      rotated to read upside down.
    needs_attention lists every designator with either kind of problem;
    hidden lists designators whose display is turned off.

    Args:
        designators (list, optional): Designators to check. Omit for all.
        preview (bool): Also return a whole-board view with the problem
            designators in red (default False).

    Returns:
        JSON with needs_attention, drc and quality (both grouped by
        designator), hidden, plus optional view image(s).
    """
    try:
        drc = await _check_silk_drc(designators)
        board = await _silk_board(refresh=True)
    except Exception as e:
        return json.dumps({"success": False, "error": f"Failed to check silkscreen: {e}"})

    scope = designators or list(board.components)
    boxes = {d: b for d, b in _current_boxes(board).items() if d in scope}
    quality = {d: p for d, p in silkscreen.evaluate(board, boxes, geometry=False).items() if p}
    # Labels in an intact label block are linked to their parts: distance and
    # "reads as" problems do not apply. A disturbed block is a problem itself.
    blocks = []
    for rec in _load_blocks(board.file):
        broken = label_blocks.check_record(board, rec)
        blocks.append({"parts": rec["members"], "link": rec["link"], "intact": not broken,
                       **({"marker": rec["marker"]} if rec.get("marker") else {}),
                       **({"problems": broken} if broken else {})})
        for d in rec["members"]:
            if d not in scope:
                continue
            if broken:
                quality.setdefault(d, []).append("label block broken: " + "; ".join(broken))
            elif d in quality:
                keep = [p for p in quality[d] if not label_blocks.is_association_problem(p)]
                if keep:
                    quality[d] = keep
                else:
                    del quality[d]
    grouped = _group_silk_violations(drc)
    attention = sorted(set(grouped) | set(quality))
    summary = {
        "board": board.file,
        "checked_count": drc.get("checked_count"),
        "needs_attention_count": len(attention),
        "needs_attention": attention,
        "drc_violation_count": drc.get("violation_count"),
        "drc": grouped,
        "quality": quality,
        "hidden": sorted(d for d in scope if d in board.components and not board.components[d].visible),
    }
    if blocks:
        summary["label_blocks"] = blocks
    if drc.get("missing_designators"):
        summary["missing_designators"] = drc["missing_designators"]
    text = json.dumps(summary, indent=2)
    if not preview:
        return text
    images = []
    for side in ("T", "B"):
        side_boxes = {d: b for d, b in boxes.items() if board.components[d].side == side}
        if side_boxes:
            images.append(MCPImage(data=silkscreen.render_view(
                board, side_boxes, side=side, problems=attention, max_px=1600), format="png"))
    return [text] + images

@_tool(read_only=True)
async def view_silkscreen(ctx: Context, designators: list = None, region: list = None,
                          side: str = None, grid_mils: float = 0, refresh: bool = False):
    """
    Look at the silkscreen around some designators or in a region.

    Renders one board side cleanly - no Altium UI, no copper clutter - with a
    grid labelled in mils (board-origin coordinates, the same ones
    set_designator_positions takes), so a free spot can be read straight off
    the image. Shows solder mask openings, silk, part names at their centres,
    every designator box, and marks designators whose current spot has a
    problem in red. The designators asked about are cyan with a line to
    their part.

    Uses the board snapshot (refreshed by check_silkscreen, updated by
    set_designator_positions, at most 5 minutes old). Pass refresh=True after
    editing the board by hand in Altium.

    Args:
        designators (list, optional): Zoom to these parts and highlight them.
        region (list, optional): [x_min, y_min, x_max, y_max] in mils to show
            instead. Omit both for the whole board.
        side (str, optional): "top" or "bottom". Default: the side of the
            first designator, else top.
        grid_mils (float): Grid spacing; 0 picks one to suit the zoom.
        refresh (bool): Re-export the board from Altium first.

    Returns:
        JSON (view bounds, grid, legend, problems of the designators in view)
        plus the image.
    """
    try:
        board = await _silk_board(refresh)
        s = _silk_side(side, designators, board)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})

    if region:
        if len(region) != 4:
            return json.dumps({"success": False, "error": "region must be [x_min, y_min, x_max, y_max]"})
        x0, y0, x1, y1 = (float(v) for v in region)
        focus, margin = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)), 0.0
    elif designators:
        focus, margin = _focus_around(board, designators, s), 60.0
        if focus is None:
            return json.dumps({"success": False, "error": f"none of {designators} is on the {s} side"})
    else:
        focus, margin = None, 40.0

    boxes = _current_boxes(board, s)
    if focus is not None:
        view = (focus[0] - margin, focus[1] - margin, focus[2] + margin, focus[3] + margin)
        boxes = {d: b for d, b in boxes.items()
                 if not (b[0][2] < view[0] or b[0][0] > view[2] or b[0][3] < view[1] or b[0][1] > view[3])}
    problems = _block_exempt(board, {d: p for d, p in silkscreen.evaluate(board, boxes).items() if p})
    png = silkscreen.render_view(board, _current_boxes(board, s), focus=focus, side=s,
                                 targets=designators or (), problems=problems,
                                 margin=margin, grid=grid_mils or None)
    info = {
        "side": "top" if s == "T" else "bottom",
        "designators_in_view": len(boxes),
        "legend": SILK_VIEW_LEGEND,
        "problems": problems,
    }
    if focus is not None:
        info["view_mils"] = [round(v, 1) for v in view]
    return [json.dumps(info, indent=2), MCPImage(data=png, format="png")]

@_tool(read_only=True)
async def get_designator_options(ctx: Context, designators: list, count: int = 5,
                                 max_gap_mils: float = 50, min_height_mils: float = 0,
                                 refresh: bool = False):
    """
    Legal spots for designators, ranked, drawn as numbered boxes.

    For each designator: its current spot and what is wrong with it, then up
    to `count` distinct legal positions around its part - clear of silk, solder
    mask openings, untented vias, the board edge and every other designator
    where it is now, readable, and nearer its own part than any other part of
    the same type. Ranked by: close to the part, above > left/right > below,
    text along the part's long axis, centred. Pick one (or use the image to
    judge a better spot) and apply it with set_designator_positions using the
    option's x, y and rotation.

    The designators asked about are not obstacles to each other: when placing
    neighbours, check the chosen spots together with
    set_designator_positions(dry_run=True) first. When a designator has no
    legal spot, blocked_by says what is in the way: ask again together with
    the neighbouring designators (so they can move to make room), allow
    smaller text (min_height_mils), or place its row's designators as a
    block with get_label_block_options. Do not hide designators to get rid
    of problems; only mounting holes and fiducials go without one.

    Args:
        designators (list): Designators to find spots for.
        count (int): Options per designator (default 5).
        max_gap_mils (float): Furthest the text may sit from its part's pads
            and silk (default 50).
        min_height_mils (float): Also offer smaller text, down to this height,
            in 5 mil steps (0 = current size only).
        refresh (bool): Re-export the board from Altium first.

    Returns:
        JSON per designator (current spot + problems, options with n, x, y,
        rotation, side, gap_mils and height when reduced, or blocked_by) plus
        an image per board side with the options numbered.
    """
    try:
        board = await _silk_board(refresh)
    except Exception as e:
        return json.dumps({"success": False, "error": f"Failed to export silkscreen data: {e}"})
    scope, skipped = silkscreen.split_scope(board, designators or [])
    if not scope:
        return json.dumps({"success": False, "error": "no placeable designators", "skipped": skipped})

    opts = silkscreen.Options(max_gap=max_gap_mils, min_height=min_height_mils or None)
    found = silkscreen.options_for(board, scope, count=max(1, count), options=opts)
    current = silkscreen.evaluate(board, {d: (board.components[d].text_box,
                                              board.components[d].text_rotation) for d in scope})
    result = {}
    for d in scope:
        c = board.components[d]
        entry = {"current": dict(_box_info(c.text_box, c.text_rotation), problems=current[d])}
        entry["options"] = []
        for i, cand in enumerate(found[d]["options"], 1):
            o = {"n": i, "x": round(cand.center[0], 2), "y": round(cand.center[1], 2),
                 "rotation": round(cand.rotation) % 360,
                 "side": silkscreen.SIDE_NAMES.get(cand.side_name, cand.side_name),
                 "gap_mils": round(silkscreen._rect_rect_dist(cand.rect, c.extent), 1)}
            if cand.note == "reduced height":
                o["height"] = cand.height
                o["stroke_width"] = cand.stroke
            entry["options"].append(o)
        if found[d]["blocked_by"]:
            entry["blocked_by"] = found[d]["blocked_by"]
        result[d] = entry
    out = {"designators": result, "legend": SILK_VIEW_LEGEND}
    if skipped:
        out["skipped"] = skipped
    images = []
    for side in ("T", "B"):
        on_side = [d for d in scope if board.components[d].side == side]
        if not on_side:
            continue
        png = silkscreen.render_view(board, _current_boxes(board, side),
                                     focus=_focus_around(board, on_side, side), margin=60.0, side=side,
                                     targets=on_side, problems=[d for d in on_side if current[d]],
                                     options={d: found[d]["options"] for d in on_side})
        images.append(MCPImage(data=png, format="png"))
    return [json.dumps(out, indent=2)] + images

@_tool()
async def set_designator_positions(ctx: Context, placements: list, dry_run: bool = False,
                                   verify: bool = True) -> str:
    """
    Place designators - by coordinates or relative to their part - and get a
    verdict for each.

    Each placement is either
    - absolute: x, y = centre of the text box in mils (board origin), as read
      off view_silkscreen or taken from get_designator_options, or
    - relative: side = "above" | "below" | "left" | "right" | "inside" of its
      own part, with gap (mils from the part's pads/silk; default just clear
      of them) and offset (slide along that side, +x or +y).
    Optional on both: rotation (0/90 top, 0/270 bottom read correctly;
    default keeps a readable current rotation), height and stroke_width
    (mils), visible (show/hide - hide only mounting holes and fiducials;
    designators with no room go in a label block, see
    get_label_block_options). A placement with only visible (or only
    rotation/height) keeps the current centre.

    dry_run=True changes nothing: every placement is checked against silk,
    mask openings, the board edge, other designators where they are now and
    each other, plus placement quality. Use it to test a set of neighbours
    together before applying.

    Otherwise all moves are one undo step, and with verify (default) each
    moved designator is then checked with Altium's own silk rules and for
    quality. ok=true means clean. The moves only go to the board the
    snapshot was read from: if another PCB has been focused since, nothing
    moves and the error names both boards.

    Args:
        placements (list): Dicts with designator plus x/y or side (gap,
            offset), and optional rotation, height, stroke_width, visible.
        dry_run (bool): Only check the placements.
        verify (bool): After moving, run Altium's silk DRC on the moved
            designators (default True).

    Returns:
        str: JSON with one verdict per designator: final x, y, rotation,
             ok and problems.
    """
    try:
        board = await _silk_board()
    except Exception as e:
        return json.dumps({"success": False, "error": f"Failed to export silkscreen data: {e}"})

    errors, resolved, entries = [], {}, []
    for idx, p in enumerate(placements or []):
        des = str(p.get("designator", "")).strip() if isinstance(p, dict) else ""
        if not des or des not in board.components:
            errors.append(f"placements[{idx}]: unknown designator {des!r}")
            continue
        if "|" in des:
            errors.append(f"placements[{idx}]: designator must not contain '|'")
            continue
        if ("x" in p) != ("y" in p):
            errors.append(f"placements[{idx}]: give both x and y, or neither")
            continue
        try:
            rect, rot, height, stroke = silkscreen.resolve_placement(board, p)
        except (TypeError, ValueError) as e:
            errors.append(f"placements[{idx}]: {e}")
            continue
        visible = p.get("visible")
        resolved[des] = (rect, rot, visible)
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        size = f"{height:g}" if p.get("height") is not None else ""
        width = f"{stroke:g}" if p.get("height") is not None or p.get("stroke_width") is not None else ""
        vis = "" if visible is None else ("1" if visible else "0")
        entries.append(f"{des}|{cx:.3f}|{cy:.3f}|{rot:g}|{size}|{width}|{vis}")
    if errors:
        return json.dumps({"success": False, "error": "; ".join(errors)})
    if not entries:
        return json.dumps({"success": False, "error": "No placements provided"})

    shown = {d: (r, rot) for d, (r, rot, vis) in resolved.items()
             if vis is not False and (vis or board.components[d].visible)}
    if dry_run:
        problems = silkscreen.evaluate(board, shown, hiding=set(resolved) - set(shown))
        verdicts = {d: dict(_box_info(r, rot), ok=not problems.get(d), problems=problems.get(d, []))
                    for d, (r, rot) in shown.items()}
        for d in set(resolved) - set(shown):
            verdicts[d] = {"hidden": True}
        return json.dumps({"dry_run": True, "all_ok": all(v.get("ok", True) for v in verdicts.values()),
                           "designators": verdicts}, indent=2)

    params = {"placements": entries}
    if board.file:
        # Altium refuses the moves if another PCB is focused by now
        params["board"] = board.file.replace("\\", "/")
    response = await altium_bridge.execute_command("place_designators", params)
    if not response.get("success", False):
        return json.dumps({"success": False, "error": f"Failed to move designators: {response.get('error', 'Unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        result = json.loads(result)

    # Keep the snapshot in step with what Altium reports
    final = {}
    for item in result.get("designators", []):
        c = board.components.get(item["designator"])
        if c is None:
            continue
        cx, cy, w, h = item["cx"], item["cy"], item["width"], item["height"]
        c.text_rotation = item["rotation"]
        c.text_height = item.get("text_height", c.text_height)
        c.stroke = item.get("stroke_width", c.stroke)
        c.text_box = silkscreen.ink_box((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2),
                                        c.stroke, c.truetype)
        c.visible = item.get("visible", c.visible)
        c.autopos = 0
        final[c.designator] = c

    verdicts = {}
    moved = sorted(d for d, c in final.items() if c.visible)
    grouped = {}
    if verify and moved:
        try:
            grouped = _group_silk_violations(await _check_silk_drc(moved))
        except Exception as e:
            grouped = {d: [f"DRC check failed: {e}"] for d in moved}
    quality = silkscreen.evaluate(board, {d: (final[d].text_box, final[d].text_rotation) for d in moved},
                                  geometry=False)
    for d, c in sorted(final.items()):
        if not c.visible:
            verdicts[d] = {"hidden": True}
            continue
        problems = grouped.get(d, []) + quality.get(d, [])
        verdicts[d] = dict(_box_info(c.text_box, c.text_rotation), ok=not problems, problems=problems)
        if c.text_height:
            verdicts[d]["text_height"] = c.text_height
    out = {"moved_count": len(final), "verified": bool(verify),
           "all_ok": all(v.get("ok", True) for v in verdicts.values()), "designators": verdicts}
    if result.get("missing_designators"):
        out["missing_designators"] = result["missing_designators"]
    return json.dumps(out, indent=2)

# Label blocks applied with place_label_block, per board file, so that
# check_silkscreen can tell a block's labels from labels that wandered off.
# Kept in the exchange folder, never next to the board.
SILK_BLOCKS_FILE = EXCHANGE_DIR / "silk_label_blocks.json"
_block_options_cache = {}

def _load_blocks(board_file: str) -> list:
    try:
        data = json.loads(SILK_BLOCKS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get(board_file.lower(), [])

def _save_blocks(board_file: str, records: list):
    try:
        data = json.loads(SILK_BLOCKS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[board_file.lower()] = records
    SILK_BLOCKS_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")

def _update_snapshot_graphics(removed_records: list, added_option):
    """Keep the board snapshot in step with link graphics just drawn or
    deleted, instead of exporting the whole board again (every export leaks
    memory in Altium's script engine)."""
    board = _silk_snapshot["board"]
    if board is None:
        return
    for r in removed_records:
        label_blocks.drop_graphics(board, r["side"], r["graphics"])
    if added_option is not None and added_option.graphics:
        board._parse("\n".join(label_blocks.graphics_lines(added_option.side, added_option.graphics)))

def _replaced_graphics(records: list, members: list) -> list:
    """Link graphics of earlier blocks of these parts (placing replaces them)."""
    return [g for r in records if set(r["members"]) & set(members) for g in r["graphics"]]

def _block_members(board, designators: list, whole_row: bool = True) -> list:
    """The parts of a block: one designator stands for its whole row unless
    whole_row is off."""
    names = [str(d).strip() for d in designators or []]
    missing = [d for d in names if d not in board.components]
    if missing:
        raise ValueError(f"not on the board: {missing}")
    if len(names) == 1 and whole_row:
        return label_blocks.find_row(board, names[0])[1]
    return names

def _block_exempt(board, problems: dict) -> dict:
    """problems without the distance and "reads as" problems of labels in
    intact label blocks (their link answers those)."""
    out = dict(problems)
    for rec in _load_blocks(board.file):
        if label_blocks.check_record(board, rec):
            continue
        for d in rec["members"]:
            if d in out:
                keep = [p for p in out[d] if not label_blocks.is_association_problem(p)]
                if keep:
                    out[d] = keep
                else:
                    del out[d]
    return out

def _block_image(board, option) -> bytes:
    side = option.side
    boxes = {d: b for d, b in _current_boxes(board, side).items() if d not in option.members}
    for d, (rect, rot, _, _) in option.labels.items():
        boxes[d] = (rect, rot)
    focus = label_blocks._bounds(
        [label_blocks._bounds(board.components[d].extent for d in option.members), option.rect] +
        [r for g in option.graphics for r in label_blocks._graphic_rects(g)])
    return silkscreen.render_view(board, boxes, focus=focus, side=side, targets=option.members,
                                  margin=60.0, graphics=option.graphics)

@_tool(read_only=True)
async def get_label_block_options(ctx: Context, designators: list, count: int = 3,
                                  min_height_mils: float = 25, max_distance_mils: float = 500,
                                  link: str = "auto", whole_row: bool = True, refresh: bool = False):
    """
    Place a row of parts' designators together as a block, when they do not
    fit next to their parts one by one. Use this instead of hiding
    designators (only mounting holes and fiducials go without one).

    Parts in a row get their designators in a row too, in the same order,
    even some way off. Give one designator to use the whole row it belongs
    to (parts side by side along x or y, similar size across the row, gaps
    up to 50 mil), or list the parts yourself. For a block of just one part
    (its label alone, linked to it), give it with whole_row=False.

    Each option is a block of labels - across the row (in line with each
    part when the pitch allows, else side by side) or end to end along it -
    clear of silk, mask openings, the board edge, other designators and part
    bodies, plus a link when it is not right against the row:
    - leader: a silk line with an arrowhead from the block to the parts;
    - index: matching markers - a capital letter in a circle (then square,
      then triangle) - next to the block and next to the parts.
    Ranked: no link, then leader, then index; nearer and larger text first.
    Apply one with place_label_block.

    Args:
        designators (list): One designator (its row is used) or the block's parts.
        count (int): Options to return (default 3).
        min_height_mils (float): Smallest text allowed (default 25).
        max_distance_mils (float): Furthest the block may sit from the parts (default 500).
        link (str): "auto" (cheapest), "none", "leader" or "index".
        whole_row (bool): With one designator, use its whole row (default)
            or just that part.
        refresh (bool): Re-export the board from Altium first.

    Returns:
        JSON with the parts in order and the numbered options, plus an image
        per option (block labels cyan with lines to their parts, link magenta).
    """
    if link not in ("auto", "none", "leader", "index"):
        return json.dumps({"success": False, "error": "link must be auto, none, leader or index"})
    try:
        board = await _silk_board(refresh)
        members = _block_members(board, designators, whole_row)
        records = _load_blocks(board.file)
        used = label_blocks.used_markers(board, records)
        axis, members, options = label_blocks.block_options(
            board, members, count=max(1, count), min_height=min_height_mils,
            max_distance=max_distance_mils, link=link, used_markers=used,
            ignore=_replaced_graphics(records, members))
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
    _block_options_cache[(board.file.lower(), tuple(members))] = {
        "time": _silk_snapshot["time"], "options": options,
        "params": dict(min_height=min_height_mils, max_distance=max_distance_mils, link=link)}
    out = {"parts": members, "row_axis": axis,
           "options": [o.to_dict(i) for i, o in enumerate(options, 1)]}
    if not options:
        out["note"] = ("no block fits; allow a longer max_distance_mils or smaller text, or move "
                       "other designators out of the way first")
    return [json.dumps(out, indent=2)] + [MCPImage(data=_block_image(board, o), format="png") for o in options]

@_tool()
async def place_label_block(ctx: Context, designators: list, option: int = 1,
                            whole_row: bool = True) -> str:
    """
    Apply a label block from get_label_block_options: move the parts'
    designators into the block (shown, at the block's text size) and draw
    its link on the silkscreen, then check both with Altium's rules. An
    earlier block of any of these parts is replaced (its link removed).

    Args:
        designators (list): The same designators given to get_label_block_options.
        option (int): Which option (default 1).
        whole_row (bool): As given to get_label_block_options.

    Returns:
        JSON: the block's parts, link and marker, ok, and any Altium
        violations of the labels or the link graphics.
    """
    try:
        board = await _silk_board()
        members = _block_members(board, designators, whole_row)
        axis = label_blocks.row_axis(board, members)
        members = sorted(members, key=lambda d: label_blocks._reading_key(board.components[d], axis))
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
    records = _load_blocks(board.file)
    key = (board.file.lower(), tuple(members))
    cached = _block_options_cache.get(key)
    note = None
    if cached is None or cached["time"] != _silk_snapshot["time"]:
        params = cached["params"] if cached else {}
        _, _, options = label_blocks.block_options(
            board, members, count=max(3, option), used_markers=label_blocks.used_markers(board, records),
            ignore=_replaced_graphics(records, members), **params)
        note = "board changed since the options were listed - recomputed them"
    else:
        options = cached["options"]
    if not 1 <= option <= len(options):
        return json.dumps({"success": False, "error": f"option {option} does not exist ({len(options)} options)",
                           "note": note})
    opt = options[option - 1]

    old = [r for r in records if set(r["members"]) & set(members)]
    items = []
    for r in old:
        items += label_blocks.to_items(r["side"], r["graphics"], "-")
    items += label_blocks.to_items(opt.side, opt.graphics, "+")

    moved = json.loads(await set_designator_positions(ctx, opt.placements(), verify=False))
    if "designators" not in moved:
        return json.dumps({"success": False, "error": moved.get("error", "moving the labels failed")})
    graphic_problems = []
    if items:
        response = await altium_bridge.execute_command(
            "edit_silk_graphics", {"items": items, "board": board.file.replace("\\", "/")})
        if not response.get("success", False):
            _silk_snapshot["board"] = None
            return json.dumps({"success": False, "error": f"labels moved, but drawing the link failed: "
                                                          f"{response.get('error', 'Unknown error')}"})
        result = response.get("result", {})
        if isinstance(result, str):
            result = json.loads(result)
        graphic_problems = [f"{v['rule']}: {v['object']} {v.get('owner', '')} {v.get('detail', '')}".strip()
                            for v in result.get("violations", [])]
        if result.get("not_found"):
            graphic_problems.append(f"old link graphics not found: {result['not_found']}")
        _update_snapshot_graphics(old, opt)
    try:
        label_problems = _group_silk_violations(await _check_silk_drc(list(members)))
    except Exception as e:
        label_problems = {d: [f"DRC check failed: {e}"] for d in members}

    records = [r for r in records if r not in old] + [label_blocks.record(opt)]
    _save_blocks(board.file, records)
    out = {"parts": members, "summary": opt.summary, "link": opt.link,
           "ok": not label_problems and not graphic_problems,
           "label_problems": label_problems, "link_problems": graphic_problems}
    if opt.marker:
        out["marker"] = {"letter": opt.marker[0], "shape": opt.marker[1]}
    if old:
        out["replaced_blocks"] = [r["members"] for r in old]
    if note:
        out["note"] = note
    return json.dumps(out, indent=2)

@_tool()
async def remove_label_block(ctx: Context, designators: list) -> str:
    """
    Remove the label block of these parts: its link graphics (leader or
    index markers) are deleted; the designators stay where they are, ready
    to be placed again.

    Args:
        designators (list): Any of the block's parts.

    Returns:
        JSON with the removed block's parts and link.
    """
    try:
        board = await _silk_board()
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
    records = _load_blocks(board.file)
    names = {str(d).strip() for d in designators or []}
    old = [r for r in records if names & set(r["members"])]
    if not old:
        return json.dumps({"success": False, "error": f"no label block holds any of {sorted(names)}"})
    items = [i for r in old for i in label_blocks.to_items(r["side"], r["graphics"], "-")]
    if items:
        response = await altium_bridge.execute_command(
            "edit_silk_graphics", {"items": items, "board": board.file.replace("\\", "/")})
        if not response.get("success", False):
            _silk_snapshot["board"] = None
            return json.dumps({"success": False, "error": response.get("error", "Unknown error")})
        _update_snapshot_graphics(old, None)
    _save_blocks(board.file, [r for r in records if r not in old])
    return json.dumps({"removed": [{"parts": r["members"], "link": r["link"]} for r in old]}, indent=2)

@_tool()
async def place_vias(ctx: Context, vias: list, diameter_mils: float, hole_mils: float,
                     net: str = "", start_layer: str = "Top Layer", end_layer: str = "Bottom Layer",
                     board_path: str = "") -> str:
    """
    Add vias to a PCB: stitching, fan-out, or a via at the end of a track.

    Args:
        vias (list): Via positions in mils relative to the board origin,
            each [x, y] or an object {"x": .., "y": .., "net": ..,
            "diameter_mils": .., "hole_mils": .., "start_layer": ..,
            "end_layer": ..} whose keys override the defaults below for
            that via.
        diameter_mils (float): Via pad diameter.
        hole_mils (float): Drill diameter, smaller than diameter_mils.
        net (str): Net of the vias; it must exist on the board. Empty for
            vias without a net.
        start_layer (str): Layer the via starts on, e.g. "Top Layer".
        end_layer (str): Layer it ends on, e.g. "Bottom Layer". Set both for
            a blind or buried via.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Every net and layer is checked before anything is added, so a bad entry
    adds nothing. Run run_drc afterwards: a via is not checked against
    clearance when it is placed.

    Returns:
        str: JSON with how many vias were added.
    """
    board_path = long_path(board_path)
    records, error = routing.via_records(vias, diameter_mils, hole_mils, net, start_layer, end_layer)
    if error:
        return json.dumps({"success": False, "error": error})

    logger.info(f"Placing {len(records)} vias")
    response = await altium_bridge.execute_command("create_vias", {
        "vias": ";".join(records), "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to add the vias: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

# Changes the design: a timeout leaves its outcome unknown, so no blind retry
MUTATING_COMMANDS.add("create_vias")


def _check_area(area, what="area"):
    """[x1, y1, x2, y2] in mils as floats, or an error message."""
    if not isinstance(area, (list, tuple)) or len(area) != 4:
        return None, f"{what} must be [x1, y1, x2, y2] in mils"
    try:
        a = [float(v) for v in area]
    except (TypeError, ValueError):
        return None, f"{what} must hold four numbers"
    if not all(math.isfinite(v) and abs(v) <= pcb_layout.LIMIT_MILS for v in a):
        return None, f"{what} must lie within ±{pcb_layout.LIMIT_MILS} mil"
    if a[0] >= a[2] or a[1] >= a[3]:
        return None, f"{what} must have x1 < x2 and y1 < y2"
    return a, ""


def _names(values, what):
    """A list of names as strings, or an error message."""
    if values is None:
        return [], ""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
        return None, f"{what} must be a list of names"
    if any(";" in v or "|" in v for v in values):
        return None, f"{what} may not contain ';' or '|'"
    return [v.strip() for v in values], ""


@_tool(read_only=True)
async def get_layout(ctx: Context, designators: list = None, area: list = None, margin_mils: float = 40,
                     include_touching: bool = False, include_local_nets: bool = True, include_pads: bool = False,
                     board_path: str = "", out_file: str = "") -> str:
    """
    Read the layout of a group of components or of an area: the components'
    placement and the copper around them (tracks, arcs, vias, fills, copper
    regions and polygon pours, each pour with its full outline, arc edges
    included, and its pour settings). Read-only.

    The result is in exactly the form place_copper takes, so it can be
    edited and placed again, on this board or moved with place_copper's
    transform (to repeat a channel, for example). The components come in
    place_components' form.

    Args:
        designators (list): Components to read, e.g. ["U5", "C13", "Y1"].
            Without an area, the area is their extent (pads and bodies)
            grown by margin_mils.
        area (list): [x1, y1, x2, y2] in mils from the board origin. With
            no designators, the components wholly inside it are listed.
        margin_mils (float): How far the area reaches beyond the components
            (default 40).
        include_touching (bool): Also list copper that only reaches into the
            area, such as a track leaving the group. By default only copper
            wholly inside the area is listed: the group's own routing.
        include_local_nets (bool): With designators, also list the copper of
            their own nets (nets with no pad on any other part) wherever it
            runs, such as a route between two of them that leaves the area
            (default true).
        include_pads (bool): Also list every pad reaching into the area
            (designator, pad, x, y, layer, shape, size [x, y], rotation,
            hole, net), e.g. to see what new copper must keep clear of.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.
        out_file (str): Write the layout to this JSON file and return only a
            summary; for large areas.

    Never listed: copper that belongs to a component or that a pour poured
    (the pour itself is), keepouts, and objects on non-copper layers
    (counted in skipped).

    Returns:
        JSON: area; components (designator, x, y, rotation, layer, locked,
        footprint, designator_text and comment_text: {x, y, rotation,
        height, stroke_width, visible, autoposition}); tracks (net, layer,
        width, start, end); arcs (net, layer, width, center, radius,
        start_angle, end_angle: counter-clockwise from start to end); vias
        (net, x, y, diameter, hole, start_layer, end_layer); fills; regions;
        cutouts (layer, outline); pours (name, net, layer, hatch, pour_over,
        remove_dead_copper, remove_islands, island_area_sq_mils,
        remove_necks, neck_width_mils, grid, track_width, arc_approximation,
        pour_index, locked, obey_cutouts, optimal_void_rotation, outline:
        vertices {x, y} with "arc" {center, radius, start_angle, end_angle}
        when the edge to the next vertex is an arc); counts and
        missing_designators. All in mils and degrees.
    """
    names, err = _names(designators, "designators")
    if err:
        return json.dumps({"success": False, "error": err})
    box = None
    if area is not None:
        box, err = _check_area(area)
        if err:
            return json.dumps({"success": False, "error": err})
    if not names and box is None:
        return json.dumps({"success": False, "error": "Give designators, an area, or both"})
    try:
        margin = float(margin_mils)
        if not math.isfinite(margin) or not 0 <= margin <= 10000:
            raise ValueError
    except (TypeError, ValueError):
        return json.dumps({"success": False, "error": "margin_mils must be a number from 0 to 10000"})
    params = {"layout_designators": ";".join(names), "layout_margin": int(round(margin * pcb_layout.COORD_PER_MIL)),
              "layout_touching": bool(include_touching), "layout_local_nets": bool(include_local_nets),
              "layout_pads": bool(include_pads),
              "layout_area": "" if box is None else "|".join(str(int(round(v * pcb_layout.COORD_PER_MIL))) for v in box)}
    if board_path:
        params["board_path"] = long_path(board_path)
    response = await altium_bridge.execute_command("get_layout", params)
    if not response.get("success", False):
        return json.dumps({"success": False, "error": f"Failed to read the layout: {response.get('error', 'unknown error')}"})
    text = response.get("result", "")
    if not isinstance(text, str) or text.startswith("ERROR"):
        return json.dumps({"success": False, "error": str(text)[:500]})
    layout = pcb_layout.parse_layout(text)
    found = {d.upper() for d in layout.pop("found")}
    layout["missing_designators"] = [d for d in names if d.upper() not in found]
    layout["counts"] = {k: len(layout[k]) for k in ("components", "tracks", "arcs", "vias", "fills", "regions", "cutouts",
                                                     "pours", "pads")}
    if not include_pads:
        layout.pop("pads")
        layout["counts"].pop("pads")
    layout["success"] = True
    if out_file:
        path = Path(out_file)
        try:
            path.write_text(json.dumps(layout, indent=1), encoding="utf-8")
        except OSError as e:
            return json.dumps({"success": False, "error": f"Could not write {out_file}: {e}"})
        return json.dumps({"success": True, "out_file": str(path), "area": layout["area"], "counts": layout["counts"],
                           "missing_designators": layout["missing_designators"], "board": layout["board"]}, indent=2)
    return json.dumps(layout, indent=1)


@_tool()
async def place_copper(ctx: Context, tracks: list = None, arcs: list = None, vias: list = None, fills: list = None,
                       regions: list = None, cutouts: list = None, pours: list = None, layout: dict = None,
                       transform: dict = None,
                       rename_nets: dict = None, board_path: str = "") -> str:
    """
    Add copper to a PCB in one undo step: tracks and arcs at any angle, vias,
    fills, copper regions and polygon pours (arc edges, island and neck
    removal, pour order). Everything is checked first (each net must exist,
    each layer must be a signal layer), so a bad entry adds nothing. Pours go
    in last, in pour_index order, and are poured around the copper added
    with them. The nets the copper is on are analyzed afterwards, so the
    ratsnest is current (this can join straight track segments that continue
    each other; the copper stays the same). Run run_drc or
    get_unrouted_connections afterwards.

    Each list takes entries in get_layout's form (mils, degrees):
        tracks: {net, layer, width, start: [x, y], end: [x, y]}
        arcs:   {net, layer, width, center: [x, y], radius, start_angle,
                 end_angle}  (counter-clockwise from start to end)
        vias:   {net, x, y, diameter, hole, start_layer, end_layer}
                (layers default to Top Layer and Bottom Layer)
        fills:  {net, layer, corner1: [x, y], corner2: [x, y], rotation}
        regions: {net, layer, outline: [[x, y], ...]}
        cutouts: {layer, outline}  a polygon pour cutout, on a signal layer
                 or "Multi Layer" (all of them); pours with obey_cutouts
                 leave it free
        pours:  {net, layer, outline, name, hatch ("solid", "hatched_45",
                 "hatched_90", "vertical", "horizontal", "none"), pour_over
                 ("same_net", "same_net_polygons", "none"),
                 remove_dead_copper, remove_islands (below island_area_sq_mils,
                 default 2500), remove_necks (narrower than neck_width_mils,
                 default 5), grid, track_width, arc_approximation,
                 pour_index, locked, obey_cutouts (default true),
                 optimal_void_rotation (default false)}
                outline vertices are [x, y] or {x, y, arc: {center, radius,
                start_angle, end_angle}} for an arc edge to the next vertex.
    net "" is no net. Coordinates are mils from the board origin.

    Args:
        tracks, arcs, vias, fills, regions, cutouts, pours (list): Copper
            to add.
        layout (dict): A whole get_layout result; its copper lists are added
            too (its components are not moved: use place_components).
        transform (dict): Move everything: {"dx", "dy", "rotation" (degrees,
            about "about": [x, y], default the origin), "mirror" (mirror x
            before rotating)}. Fills only shift.
        rename_nets (dict): {old net: new net}, e.g. to repeat a channel.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        JSON: counts added (tracks, arcs, vias, fills, regions, cutouts) and each pour
        with the pieces of copper it poured (0 means it poured nothing), or
        the problems that stopped it.
    """
    merged = {k: list(v or []) for k, v in (("tracks", tracks), ("arcs", arcs), ("vias", vias), ("fills", fills),
                                            ("regions", regions), ("cutouts", cutouts), ("pours", pours))}
    if layout is not None:
        if not isinstance(layout, dict):
            return json.dumps({"success": False, "error": "layout must be a get_layout result"})
        for k in merged:
            merged[k].extend(layout.get(k) or [])
    if transform is not None and not isinstance(transform, dict):
        return json.dumps({"success": False, "error": "transform must be an object {dx, dy, rotation, about, mirror}"})
    if rename_nets is not None and not isinstance(rename_nets, dict):
        return json.dumps({"success": False, "error": "rename_nets must be an object {old: new}"})
    try:
        lines = pcb_layout.encode_copper(merged, transform, rename_nets)
    except (pcb_layout.LayoutError, KeyError, TypeError) as e:
        what = f"missing field {e}" if isinstance(e, KeyError) else str(e)
        return json.dumps({"success": False, "error": f"Nothing was added: {what}"})
    if not lines:
        return json.dumps({"success": False, "error": "Nothing to add"})
    fd, spec = tempfile.mkstemp(prefix="copper_spec_", suffix=".txt", dir=EXCHANGE_DIR)
    os.close(fd)
    try:
        write_altium_text(spec, "\n".join(lines) + "\n")
        params = {"copper_spec_file": spec}
        if board_path:
            params["board_path"] = long_path(board_path)
        response = await altium_bridge.execute_command("place_copper", params,
                                                       timeout=max(RESPONSE_TIMEOUT, 60 + len(lines) / 20))
    finally:
        try:
            os.remove(spec)
        except OSError:
            pass
    if not response.get("success", False):
        return json.dumps({"success": False, "error": f"Failed to add the copper: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result

# Changes the design: a timeout leaves its outcome unknown, so no blind retry
MUTATING_COMMANDS.add("place_copper")


@_tool()
async def get_unrouted_connections(ctx: Context, designators: list = None, nets: list = None, area: list = None,
                                   margin_mils: float = 40, max_connections: int = 200,
                                   board_path: str = "") -> str:
    """
    List the connections still to be routed (Altium's ratsnest lines): for a
    group of components, some nets, or the whole board. Unlike run_drc it
    has no 500-violation limit and needs no DRC run, so it is the quick way
    to see what a group still lacks after placing and routing it.

    Args:
        designators (list): Components whose nets to check. Their pads name
            the ends of each connection (from_pad, to_pad), and unless area is
            given, the area is their pads' extent grown by margin_mils.
        nets (list): Nets to check (exact names, any case). With neither
            designators nor nets, every net.
        area (list): [x1, y1, x2, y2] in mils from the board origin; each
            connection says whether both its ends lie in it (inside).
        margin_mils (float): See designators (default 40).
        max_connections (int): Most connections listed (default 200); the
            counts cover them all.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    With designators, only connections that end at one of their pads or lie
    wholly inside the area are listed: a group's own connections and the
    ones leaving it, not the rest of a board-wide net such as GND.

    Altium does not update its ratsnest after copper added by a script, so
    the nets are analyzed first. Analyzing can join straight track segments
    that continue each other into one (the copper stays the same), as
    Altium does after interactive routing.

    Returns:
        JSON: connection_count, inside_count (both ends in the area),
        by_net counts, area, and connections (net, from, to, from_pad,
        to_pad, length in mils, inside), longest first.
    """
    names, err = _names(designators, "designators")
    if err:
        return json.dumps({"success": False, "error": err})
    net_names, err = _names(nets, "nets")
    if err:
        return json.dumps({"success": False, "error": err})
    box = None
    if area is not None:
        box, err = _check_area(area)
        if err:
            return json.dumps({"success": False, "error": err})
    try:
        margin = float(margin_mils)
        limit = int(max_connections)
    except (TypeError, ValueError):
        return json.dumps({"success": False, "error": "margin_mils and max_connections must be numbers"})
    params = {"unrouted_nets": ";".join(net_names), "unrouted_designators": ";".join(names)}
    if board_path:
        params["board_path"] = long_path(board_path)
    response = await altium_bridge.execute_command("get_unrouted_connections", params)
    if not response.get("success", False):
        return json.dumps({"success": False, "error": f"Failed to read the connections: {response.get('error', 'unknown error')}"})
    text = response.get("result", "")
    if not isinstance(text, str) or text.startswith("ERROR"):
        return json.dumps({"success": False, "error": str(text)[:500]})
    pads, conns, board = pcb_layout.parse_unrouted(text)
    if box is None and pads:
        xs, ys = [p["x"] for p in pads], [p["y"] for p in pads]
        box = [min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin]
    labelled = pcb_layout.label_connections(pads, conns, box)
    if names:
        labelled = [c for c in labelled if c["from_pad"] or c["to_pad"] or c["inside"]]
    labelled.sort(key=lambda c: -c["length"])
    by_net = {}
    for c in labelled:
        by_net[c["net"]] = by_net.get(c["net"], 0) + 1
    found = {p["designator"].upper() for p in pads}
    missing = [d for d in names if d.upper() not in found]
    return json.dumps({"success": True, "board": board, "connection_count": len(labelled),
                       "inside_count": sum(1 for c in labelled if c["inside"]),
                       "by_net": dict(sorted(by_net.items(), key=lambda kv: -kv[1])),
                       "area": [round(v, 2) for v in box] if box else None,
                       "missing_designators": missing, "connections": labelled[:max(0, limit)]}, indent=1)


def _exact_segments(layer, net, width, pts):
    """place_copper tracks through pts (mils), every segment exactly 0, 45 or
    90 degrees: routing.route adds a short leg where an end point snapped
    onto the grid leaves a bend off 45."""
    ipts = [(int(round(x * routing.COORD_PER_MIL)), int(round(y * routing.COORD_PER_MIL))) for x, y in pts]
    segs = routing.route(ipts)
    c = routing.COORD_PER_MIL
    return [{"net": net, "layer": layer, "width": width, "start": [x1 / c, y1 / c], "end": [x2 / c, y2 / c]}
            for x1, y1, x2, y2 in segs if (x1, y1) != (x2, y2)]


def _route_params(width_mils, clearance_mils, via_diameter_mils, via_hole_mils, grid_mils):
    for name, v in (("width_mils", width_mils), ("clearance_mils", clearance_mils),
                    ("via_diameter_mils", via_diameter_mils), ("via_hole_mils", via_hole_mils), ("grid_mils", grid_mils)):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 < v < 1000:
            return f"{name} must be a positive number of mils"
    if via_hole_mils >= via_diameter_mils:
        return "via_hole_mils must be smaller than via_diameter_mils"
    if not 0.5 <= grid_mils <= 25:
        return "grid_mils must be from 0.5 to 25"
    return ""


async def _net_rules(nets, layer, board_path):
    """{net: {"min", "preferred", "max", "rule", "clearance"}} in mils, from the
    board's width and clearance rules for a track of each net on layer."""
    nets = sorted({n for n in nets if n and ";" not in n and "|" not in n})
    if not nets:
        return {}
    params = {"net_rules_nets": ";".join(nets), "net_rules_layer": layer}
    if board_path:
        params["board_path"] = long_path(board_path)
    response = await altium_bridge.execute_command("net_rules", params)
    text = response.get("result", "") if response.get("success") else ""
    out = {}
    for line in (text if isinstance(text, str) else "").splitlines():
        f = line.split("|")
        if f[0] == "N" and len(f) >= 7:
            mil = lambda v: int(v) / routing.COORD_PER_MIL if v.strip().lstrip("-").isdigit() else None
            out[f[1]] = {"min": mil(f[2]), "preferred": mil(f[3]), "max": mil(f[4]), "rule": f[5].strip(),
                         "clearance": mil(f[6]) if mil(f[6]) and mil(f[6]) > 0 else None}
    return out


@_tool()
async def route_connections(ctx: Context, designators: list = None, nets: list = None, area: list = None,
                            layers: list = None, width_mils: float = None, clearance_mils: float = None,
                            via_diameter_mils: float = 24, via_hole_mils: float = 12, grid_mils: float = 2.5,
                            include_leaving: bool = False, max_connections: int = 100, dry_run: bool = False,
                            board_path: str = "") -> str:
    """
    Route unrouted connections automatically: a maze router finds a path for
    each connection, every segment 0, 45 or 90 degrees, keeping the clearance
    from other nets' copper and changing layer through a via where a route
    needs it. For a group's local routing after it is placed (and fanned out
    with fanout_pads). Each net gets the preferred width of its width rule
    (the minimum where the preferred width finds no path) and the gap of
    its clearance rule, unless width_mils or clearance_mils is given. Wider
    nets are routed first, then shortest first; each route becomes an
    obstacle for the next. Polygon pours are not obstacles: they pour again
    around new copper (repour_polygons). Everything is added in one undo
    step; check with run_drc and get_unrouted_connections.

    Args:
        designators (list): Route the connections of these components that
            lie wholly inside their area (their pads' extent, as
            get_unrouted_connections gives it); include_leaving adds those
            leaving the group.
        nets (list): Route the connections of these nets (inside area when
            given).
        area (list): [x1, y1, x2, y2] in mils from the board origin.
        layers (list): Layers routes may use, e.g. ["Top Layer"] (default)
            or ["Top Layer", "Bottom Layer"]; with more than one, vias
            (through, Top to Bottom) change layer.
        width_mils (float): One width for every net, instead of the rules.
        clearance_mils (float): Gap to other nets' copper, instead of the
            largest clearance rule gap of the nets routed.
        via_diameter_mils, via_hole_mils (float): Size of a layer-change via.
        grid_mils (float): Routing grid (default 2.5); finer finds paths
            between fine-pitch pads but is slower.
        include_leaving (bool): See designators.
        max_connections (int): Most connections routed in one call.
        dry_run (bool): Find the routes and return them without adding them.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        JSON: routed and failed connections (net, from, to, pads, width, and
        why a route failed), tracks and vias added, the widths used per
        net, and the connections still unrouted afterwards. With dry_run,
        the tracks and vias themselves.
    """
    import router

    layers = layers or ["Top Layer"]
    if not isinstance(layers, list) or not all(isinstance(l, str) and l.strip() for l in layers):
        return json.dumps({"success": False, "error": "layers must be a list of layer names"})
    err = _route_params(width_mils or 6, clearance_mils or 6, via_diameter_mils, via_hole_mils, grid_mils)
    if err:
        return json.dumps({"success": False, "error": err})
    if not designators and not nets:
        return json.dumps({"success": False, "error": "Give designators or nets to route"})
    un = json.loads(await get_unrouted_connections(ctx, designators=designators, nets=nets, area=area,
                                                   max_connections=100000, board_path=board_path))
    if not un.get("success"):
        return json.dumps(un)
    conns = [c for c in un["connections"] if include_leaving or c["inside"] or (not designators and area is None)]
    conns.sort(key=lambda c: c["length"])
    conns = conns[:max(0, int(max_connections))]
    if not conns:
        return json.dumps({"success": True, "routed": 0, "failed": [], "note": "Nothing to route",
                           "unrouted": un.get("connection_count", 0)})
    rules = await _net_rules({c["net"] for c in conns}, layers[0], board_path)
    clearance = clearance_mils or max([r["clearance"] for r in rules.values() if r.get("clearance")] or [6.0])

    def widths(net):
        """The widths to try for a net: preferred, then minimum."""
        if width_mils:
            return [float(width_mils)]
        r = rules.get(net) or {}
        pref, low = r.get("preferred") or 6.0, r.get("min") or r.get("preferred") or 6.0
        return [pref] if abs(pref - low) < 1e-6 else [pref, low]

    xs = [p[0] for c in conns for p in (c["from"], c["to"])]
    ys = [p[1] for c in conns for p in (c["from"], c["to"])]
    margin = 150.0
    box = [min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin]
    lay = json.loads(await get_layout(ctx, area=box, include_touching=True, include_local_nets=False,
                                      include_pads=True, board_path=board_path))
    if not lay.get("success"):
        return json.dumps(lay)
    signal = sorted({p["layer"] for p in lay["pads"] if p["layer"] != "Multi Layer"} |
                    {t["layer"] for t in lay["tracks"]} | {a["layer"] for a in lay["arcs"]} | set(layers))
    base_shapes = router.shapes_from_layout(lay, signal)

    def end_layers(layer):
        return list(layers) if layer == "Multi Layer" else [layer]

    def attempt(order):
        """One routing pass over the connections in this order, in memory."""
        new_shapes, routed, failed, tracks, vias = [], [], [], [], []
        pending = [(c, 0) for c in order]   # (connection, index into widths(net))
        used = {}
        while pending:
            width = max(widths(c["net"])[k] for c, k in pending)
            batch = [(c, k) for c, k in pending if abs(widths(c["net"])[k] - width) < 1e-6]
            pending = [(c, k) for c, k in pending if abs(widths(c["net"])[k] - width) >= 1e-6]
            try:
                grid = router.Board(box, grid_mils, signal, width, clearance, via_diameter_mils,
                                    {c["net"] for c, _ in batch})
            except router.RouteError as e:
                return json.dumps({"success": False, "error": str(e)})
            for s in base_shapes + new_shapes:
                grid.add(s)
            for c, k in batch:
                info = {key: c[key] for key in ("net", "from", "to", "from_pad", "to_pad")}
                sl = [l for l in end_layers(c["from_layer"]) if l in layers]
                el = [l for l in end_layers(c["to_layer"]) if l in layers]
                if not sl or not el:
                    failed.append(dict(info, why=f"an end is on {c['from_layer'] if not sl else c['to_layer']}, "
                                                 "not on a routing layer"))
                    continue
                i1, j1 = grid.cell(min(c["from"][0], c["to"][0]) - 100, min(c["from"][1], c["to"][1]) - 100)
                i2, j2 = grid.cell(max(c["from"][0], c["to"][0]) + 100, max(c["from"][1], c["to"][1]) + 100)
                window = (max(0, i1), max(0, j1), min(grid.nx - 1, i2), min(grid.ny - 1, j2))
                found = router.route_one(grid, c["net"], c["from"], c["to"], sl, el, window=window) or \
                    router.route_one(grid, c["net"], c["from"], c["to"], sl, el)
                if found is None:
                    if k + 1 < len(widths(c["net"])):
                        pending.append((c, k + 1))   # try again at the rule's minimum width
                    else:
                        failed.append(dict(info, width=width, why="no path keeps the clearance on the routing layers"))
                    continue
                legs, via_pts = found
                for layer, pts in legs:
                    segs = _exact_segments(layer, c["net"], width, pts)
                    tracks.extend(segs)
                    for s in segs:
                        shape = router.Shape("seg", c["net"], [layer], ax=s["start"][0], ay=s["start"][1],
                                             bx=s["end"][0], by=s["end"][1], hw=width / 2)
                        grid.add(shape)
                        new_shapes.append(shape)
                for vx, vy in via_pts:
                    vias.append({"net": c["net"], "x": round(vx, 4), "y": round(vy, 4), "diameter": via_diameter_mils,
                                 "hole": via_hole_mils, "start_layer": "Top Layer", "end_layer": "Bottom Layer"})
                    shape = router.Shape("circle", c["net"], signal, cx=vx, cy=vy, r=via_diameter_mils / 2)
                    grid.add(shape)
                    new_shapes.append(shape)
                used[c["net"]] = width
                routed.append(dict(info, width=width))
        return routed, failed, tracks, vias, used

    # A connection that fails is often walled in by routes made before it:
    # route again with the failures first, and keep the best pass
    order, best = list(conns), None
    for _ in range(3):
        outcome = attempt(order)
        if isinstance(outcome, str):
            return outcome
        if best is None or len(outcome[1]) < len(best[1]):
            best = outcome
        if not outcome[1]:
            break
        failed_keys = {(f["net"], tuple(f["from"]), tuple(f["to"])) for f in outcome[1]}
        key = lambda c: (c["net"], tuple(c["from"]), tuple(c["to"]))
        order = [c for c in conns if key(c) in failed_keys] + [c for c in conns if key(c) not in failed_keys]
    routed, failed, tracks, vias, used = best
    result = {"success": True, "routed": len(routed), "failed": failed, "tracks": len(tracks), "vias": len(vias),
              "clearance_mils": clearance, "widths": used}
    if dry_run:
        result.update({"dry_run": True, "track_list": tracks, "via_list": vias})
        return json.dumps(result, indent=1)
    if tracks or vias:
        placed = json.loads(await place_copper(ctx, tracks=tracks, vias=vias, board_path=board_path))
        if not placed.get("success"):
            return json.dumps({"success": False, "error": f"Routes found but not added: {placed.get('error')}",
                               "problems": placed.get("problems")})
    after = json.loads(await get_unrouted_connections(ctx, designators=designators, nets=nets, area=area,
                                                      max_connections=20, board_path=board_path))
    result["unrouted_after"] = after.get("connection_count")
    result["inside_unrouted_after"] = after.get("inside_count")
    return json.dumps(result, indent=1)

MUTATING_COMMANDS.add("route_connections")


@_tool()
async def fanout_pads(ctx: Context, designators: list, nets: list = None, pads: list = None,
                      width_mils: float = None, clearance_mils: float = None, via_diameter_mils: float = 24,
                      via_hole_mils: float = 12, max_distance_mils: float = 80, grid_mils: float = 2.5,
                      dry_run: bool = False, board_path: str = "") -> str:
    """
    Fan out pads: a short track from each pad to a via next to it, e.g. to
    take an IC's ground and power pads down to their planes, or to free a
    pad for routing on another layer. Each via goes outward from the
    component first (then the other 45 degree directions), as near as
    clearance_mils from other nets' copper allows, within max_distance_mils.
    Pads that already have a track or via on them are left alone. One undo
    step; vias are through (Top to Bottom).

    Args:
        designators (list): Components whose pads to fan out.
        nets (list): Only pads on these nets, e.g. ["GND", "3V3"].
        pads (list): Only these pads, as "U1-5" or pad names.
        width_mils, clearance_mils (float): Track width and gap to other
            nets' copper. By default each net's width rule (its preferred
            width, but no wider than the pad and never below the rule's
            minimum) and the largest clearance rule gap of the nets.
        via_diameter_mils, via_hole_mils (float): Via size.
        max_distance_mils (float): Farthest a via may sit from its pad's
            centre (default 80).
        grid_mils (float): Grid for the clearance check (default 2.5).
        dry_run (bool): Return the vias and tracks without adding them.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        JSON: fanned out pads with their via positions, pads skipped (and
        why), and the counts added.
    """
    import router

    names, err = _names(designators, "designators")
    if err or not names:
        return json.dumps({"success": False, "error": err or "Give designators"})
    err = _route_params(width_mils or 8, clearance_mils or 6, via_diameter_mils, via_hole_mils, grid_mils)
    if err:
        return json.dumps({"success": False, "error": err})
    lay = json.loads(await get_layout(ctx, designators=names, margin_mils=max_distance_mils + 60,
                                      include_touching=True, include_local_nets=False, include_pads=True,
                                      board_path=board_path))
    if not lay.get("success"):
        return json.dumps(lay)
    want_nets = {n.upper() for n in (nets or [])}
    want_pads = {str(p).upper() for p in (pads or [])}
    upper = {n.upper() for n in names}
    comps = {c["designator"].upper(): c for c in lay["components"]}
    sel = [p for p in lay["pads"] if p["designator"].upper() in upper and p["net"]
           and (not want_nets or p["net"].upper() in want_nets)
           and (not want_pads or p["pad"].upper() in want_pads
                or f"{p['designator']}-{p['pad']}".upper() in want_pads)]
    signal = sorted({p["layer"] for p in lay["pads"] if p["layer"] != "Multi Layer"} |
                    {t["layer"] for t in lay["tracks"]} | {"Top Layer", "Bottom Layer"})
    rules = await _net_rules({p["net"] for p in sel}, "Top Layer", board_path)
    clearance = clearance_mils or max([r["clearance"] for r in rules.values() if r.get("clearance")] or [6.0])

    def width_for(p):
        if width_mils:
            return float(width_mils)
        r = rules.get(p["net"]) or {}
        pref, low = r.get("preferred") or 8.0, r.get("min") or 6.0
        return max(low, min(pref, min(p["size"])))

    widest = max([width_for(p) for p in sel] or [width_mils or 8.0])
    try:
        grid = router.Board(lay["area"], grid_mils, signal, widest, clearance, via_diameter_mils,
                            {p["net"] for p in sel})
    except router.RouteError as e:
        return json.dumps({"success": False, "error": str(e)})
    shapes = router.shapes_from_layout(lay, signal)
    for s in shapes:
        grid.add(s)
    # Copper already on a pad: a track end or a via (not the pads themselves)
    ends = {}
    for s in shapes:
        if s.g.get("pad"):
            continue
        if s.kind == "seg":
            ends.setdefault(s.net, []).extend([(s.g["ax"], s.g["ay"]), (s.g["bx"], s.g["by"])])
        elif s.kind == "circle":
            ends.setdefault(s.net, []).append((s.g["cx"], s.g["cy"]))
    done, skipped, tracks, vias = [], [], [], []
    for p in sorted(sel, key=lambda p: (p["net"], p["designator"], p["pad"])):
        name = f"{p['designator']}-{p['pad']}"
        if p["layer"] == "Multi Layer":
            skipped.append({"pad": name, "why": "through-hole pad: it already reaches every layer"})
            continue
        pad_shape = router.shapes_from_layout({"pads": [p]}, signal)[0]
        if any(pad_shape.dist(x, y) <= 0.01 for x, y in ends.get(p["net"], [])):
            skipped.append({"pad": name, "why": "already has a track or via on it"})
            continue
        comp = comps.get(p["designator"].upper())
        center = (comp["x"], comp["y"]) if comp else (p["x"], p["y"])
        site = router.fanout_site(grid, p, p["net"], width_for(p), via_diameter_mils, max_distance_mils, center,
                                  [p["layer"]])
        if site is None:
            skipped.append({"pad": name, "why": f"no via site within {max_distance_mils} mil keeps the clearance"})
            continue
        (vx, vy), legs = site
        w = width_for(p)
        for layer, pts in legs:
            segs = _exact_segments(layer, p["net"], w, pts)
            tracks.extend(segs)
            for s in segs:
                grid.add(router.Shape("seg", p["net"], [layer], ax=s["start"][0], ay=s["start"][1],
                                      bx=s["end"][0], by=s["end"][1], hw=w / 2))
        vias.append({"net": p["net"], "x": round(vx, 4), "y": round(vy, 4), "diameter": via_diameter_mils,
                     "hole": via_hole_mils, "start_layer": "Top Layer", "end_layer": "Bottom Layer"})
        grid.add(router.Shape("circle", p["net"], signal, cx=vx, cy=vy, r=via_diameter_mils / 2))
        done.append({"pad": name, "net": p["net"], "via": [round(vx, 2), round(vy, 2)]})
    result = {"success": True, "fanned_out": done, "skipped": skipped, "tracks": len(tracks), "vias": len(vias)}
    if dry_run or not vias:
        if dry_run:
            result.update({"dry_run": True, "track_list": tracks, "via_list": vias})
        return json.dumps(result, indent=1)
    placed = json.loads(await place_copper(ctx, tracks=tracks, vias=vias, board_path=board_path))
    if not placed.get("success"):
        return json.dumps({"success": False, "error": f"Fanout found but not added: {placed.get('error')}",
                           "problems": placed.get("problems")})
    return json.dumps(result, indent=1)

MUTATING_COMMANDS.add("fanout_pads")


def _bring_to_front(hwnd):
    """Give a window the foreground from this background process.

    Windows usually refuses SetForegroundWindow from a background process.
    While attached to the foreground thread, BringWindowToTop does the job
    without a key press. A synthetic Alt also lifts the refusal, but it lands
    on whichever window is in front: usually the program the user is typing
    in, and if that is Altium, its menu bar stays active for the next keys.
    """
    this_thread = win32api.GetCurrentThreadId()
    front = win32gui.GetForegroundWindow()
    front_thread = win32process.GetWindowThreadProcessId(front)[0] if front else 0
    attached = False
    try:
        if front_thread and front_thread != this_thread:
            win32process.AttachThreadInput(this_thread, front_thread, True)
            attached = True
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception as e:
            logger.debug(f"SetForegroundWindow refused: {e}")
        win32gui.BringWindowToTop(hwnd)
    except Exception as e:
        logger.debug(f"Could not bring the window to the front: {e}")
    finally:
        if attached:
            win32process.AttachThreadInput(this_thread, front_thread, False)


def _grab_window_image(hwnd, width: int, height: int) -> Image.Image:
    """Copy a window's pixels, frame included, into an RGB image.

    Every GDI object is released even when a step fails, so a failed capture
    does not leak device contexts or the bitmap.
    """
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = save_dc = bitmap = None
    try:
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        save_dc.BitBlt((0, 0), (width, height), mfc_dc, (0, 0), win32con.SRCCOPY)
        info = bitmap.GetInfo()
        return Image.frombuffer('RGB', (info['bmWidth'], info['bmHeight']),
                                bitmap.GetBitmapBits(True), 'raw', 'BGRX', 0, 1)
    finally:
        # The memory DC goes first: a bitmap still selected into a DC is not deleted
        if save_dc is not None:
            save_dc.DeleteDC()
        if bitmap is not None and bitmap.GetHandle():
            win32gui.DeleteObject(bitmap.GetHandle())
        if mfc_dc is not None:
            mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


@_tool(read_only=True)
async def get_screenshot(ctx: Context, view_type: str = "pcb", zoom_to: list = None,
                         document_path: str = None):
    """
    Take a screenshot of the Altium window, returned as viewable image content.

    Altium comes to the front for about a second, because it only paints
    what is on screen, and the window that was in front then gets the
    foreground back.

    Args:
        view_type (str): Type of view to capture - 'pcb', 'sch' (schematic,
            harness wiring diagram .WirDoc or layout drawing .LdrDoc), 'mbs'
            (multi-board schematic, .MbsDoc) or 'dwf' (Draftsman harness
            drawing, .HarDwf)
        zoom_to (list, optional): List of component designators (e.g. ["U12", "R42"]).
            PCB view only: Altium zooms to the bounding box of these components
            (plus a margin) before the capture, so the components fill the frame.
            Omit to capture at the current zoom level.
        document_path (str, optional): Absolute or relative path of the
            schematic, harness, multi-board or Draftsman document to focus
            and capture (it must be open in Altium or openable).

    Returns:
        Image content of the captured window plus a JSON metadata text block
        (window title, size, zoomed component count)
    """
    logger.info(f"Taking screenshot of Altium {view_type} window (zoom_to={zoom_to}, doc={document_path})")
    previous_window = win32gui.GetForegroundWindow()

    try:
        # First, execute the Altium command to ensure the right document type
        # is focused, optionally zooming to the requested components
        params = {"view_type": view_type.lower()}
        if zoom_to:
            params["designators"] = zoom_to
        if document_path:
            params["document_path"] = str(Path(document_path).resolve())
        response = await altium_bridge.execute_command(
            "take_view_screenshot",
            params
        )
        
        # Check for success
        if not response.get("success", False):
            error_msg = response.get("error", "Unknown error")
            logger.error(f"Error focusing {view_type} document: {error_msg}")
            return json.dumps({"success": False, "error": f"Failed to focus the correct document type: {error_msg}"})

        # Altium reports which document ended up focused. If it is not the
        # kind that was asked for (typically: no document of that kind is
        # open), say so instead of returning the wrong editor labelled as
        # the requested view.
        focus_info = response.get("result", {})
        focus_info = focus_info if isinstance(focus_info, dict) else {}
        focused_kind = str(focus_info.get("focused_kind", "") or "").upper()
        focused_document = focus_info.get("focused_document", "")
        wanted_kind = {"pcb": "PCB", "sch": "SCH", "mbs": "SDDOC",
                       "dwf": "HARNESSDRAWING"}.get(view_type.lower())
        # Kinds each view type accepts: a 'sch' view covers the harness
        # wiring diagrams and layout drawings, a 'pcb' view a PCB library
        accepted_kinds = {
            "SCH": ("SCH", "HARNESSWIRINGDIAGRAM", "HARNESSLAYOUTDRAWING"),
            "PCB": ("PCB", "PCBLIB"),
        }.get(wanted_kind, (wanted_kind,))
        if wanted_kind and focused_kind and focused_kind not in accepted_kinds:
            logger.error(f"Requested {view_type} view but {focused_kind} document is focused")
            return json.dumps({
                "success": False,
                "error": f"Requested a '{view_type}' view but Altium has a {focused_kind} "
                         f"document focused ({focused_document}). Is a "
                         f"{wanted_kind} document open in the active project?",
                "requested_view_type": view_type,
                "focused_kind": focused_kind,
                "focused_document": focused_document})

        # The capture runs in a worker thread: it waits for Altium to come to
        # the front and paint, which must not block the server's event loop
        import ctypes

        def capture_in_physical_pixels():
            # Work in physical pixels. Python is not DPI aware, so on a
            # scaled display Windows hands this process window rects
            # divided by the scale factor (1080x902 for a 2160x1804
            # window at 200%), while the BitBlt copies the window's real
            # pixels: the capture came back as only the top-left part of
            # the window. Per-monitor awareness for this thread gives
            # GetWindowRect the physical size. The worker thread is pooled
            # and reused, so its previous setting is restored afterwards.
            # V2 (-4) needs Windows 10 1703; V1 (-3) is the fallback.
            set_dpi_context = ctypes.windll.user32.SetThreadDpiAwarenessContext
            set_dpi_context.restype = ctypes.c_void_p
            set_dpi_context.argtypes = [ctypes.c_void_p]
            previous_dpi_context = set_dpi_context(-4) or set_dpi_context(-3)
            if not previous_dpi_context:
                logger.warning("Could not make the capture thread DPI aware; on a scaled display the capture may show only part of the window")
            try:
                return capture_screenshot()
            finally:
                if previous_dpi_context:
                    set_dpi_context(previous_dpi_context)

        def capture_screenshot():
            try:
                # Find Altium windows
                altium_windows = []
                altium_fallback_windows = []
                
                def collect_altium_windows(hwnd, _):
                    if win32gui.IsWindowVisible(hwnd):
                        title = win32gui.GetWindowText(hwnd)
                        
                        # First, look for main Altium design project windows
                        # (PCB, harness or multi-board project in the title)
                        if "Altium" in title and any(
                                ext in title for ext in (".PrjPcb", ".PrjHar", ".PrjMbd")):
                            altium_windows.append({
                                "handle": hwnd,
                                "title": title,
                                "class_name": win32gui.GetClassName(hwnd),
                                "rect": win32gui.GetWindowRect(hwnd)
                            })
                        # Collect any window with Altium in the title as fallback
                        elif "Altium" in title:
                            altium_fallback_windows.append({
                                "handle": hwnd,
                                "title": title,
                                "class_name": win32gui.GetClassName(hwnd),
                                "rect": win32gui.GetWindowRect(hwnd)
                            })
                    return True
                
                win32gui.EnumWindows(collect_altium_windows, 0)
                
                # If no specific Altium .PrjPcb windows found, use the fallback
                if not altium_windows and altium_fallback_windows:
                    altium_windows = altium_fallback_windows
                
                if not altium_windows:
                    return {
                        "success": False,
                        "error": f"No Altium windows found for {view_type} view"
                    }

                # Altium owns several top-level windows with its name in the
                # title, and while it switches documents a small transient one
                # can come first. Take the largest window that is not
                # minimized: that is the main frame.
                def area(w):
                    l, t, r, b = w["rect"]
                    return max(0, r - l) * max(0, b - t)
                candidates = [w for w in altium_windows if not win32gui.IsIconic(w["handle"])]
                window = max(candidates or altium_windows, key=area)
                hwnd = window["handle"]
                
                # Bring Altium to the front and let it paint. Altium only
                # renders a schematic view once it has actually been shown on
                # screen, so a capture taken while it sits behind another
                # window comes back with a blank canvas. Letters typed while
                # it is in front go to its menus (W then H tiles the
                # documents), so whoever was in front gets the foreground back.
                activated = False
                altium_pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                previous_pid = win32process.GetWindowThreadProcessId(previous_window)[1] if previous_window else 0

                def altium_is_foreground():
                    # Altium owns several top-level windows; any of them
                    # being foreground means Altium is in front.
                    fg = win32gui.GetForegroundWindow()
                    return bool(fg) and win32process.GetWindowThreadProcessId(fg)[1] == altium_pid

                try:
                    _bring_to_front(hwnd)
                    deadline = time.time() + 1.5
                    while time.time() < deadline:
                        time.sleep(0.1)
                        if altium_is_foreground():
                            activated = True
                            break
                except Exception as e:
                    logger.warning(f"Could not bring window to foreground: {e}")
                if not activated:
                    logger.warning("Altium is not the foreground window; the capture may show an unpainted view")
                # A freshly switched-to document needs a moment to render.
                time.sleep(1.0)

                # Measure AFTER activation: restoring or switching can change
                # the frame's size.
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                width = right - left
                height = bottom - top
                if width < 200 or height < 150:
                    return {"success": False, "error": f"Altium window is too small to capture ({width}x{height}); is it minimized?"}

                # Take screenshot using GDI functions instead of ImageGrab
                try:
                    try:
                        img = _grab_window_image(hwnd, width, height)
                    finally:
                        # Not when the user has already moved on by themselves
                        if previous_pid not in (0, altium_pid) and altium_is_foreground():
                            _bring_to_front(previous_window)

                    # Encode once: the same PNG bytes are returned and, when
                    # asked for, kept as a debug copy
                    buffer = io.BytesIO()
                    img.save(buffer, format='PNG')
                    png_bytes = buffer.getvalue()

                    # A debug copy only on request, and with the per-user
                    # data: the install folder is replaced on every update
                    # (non-fatal if it fails)
                    debug_filename = None
                    if os.environ.get("ALTIUM_MCP_DEBUG_SCREENSHOTS", "") not in ("", "0"):
                        try:
                            debug_path = _data_dir() / f"screenshot_{view_type}.png"
                            debug_path.parent.mkdir(parents=True, exist_ok=True)
                            debug_path.write_bytes(png_bytes)
                            debug_filename = str(debug_path)
                            logger.info(f"Saved debug screenshot to {debug_filename}")
                        except Exception as save_error:
                            logger.warning(f"Could not save debug screenshot: {save_error}")

                    return {
                        "success": True,
                        "width": width,
                        "height": height,
                        "window_title": window["title"],
                        "window_class": window["class_name"],
                        "view_type": view_type,
                        "foreground_confirmed": activated,
                        "image_format": "PNG",
                        "debug_file": debug_filename,
                        "image_data": png_bytes
                    }

                except Exception as e:
                    import traceback
                    trace = traceback.format_exc()
                    logger.error(f"GDI screenshot error: {e}\n{trace}")
                    return {
                        "success": False,
                        "error": f"GDI screenshot failed: {str(e)}",
                        "traceback": trace
                    }

            except Exception as e:
                import traceback
                return {
                    "success": False,
                    "error": f"Screenshot thread error: {str(e)}",
                    "traceback": traceback.format_exc()
                }

        # A capture that hangs (Altium never painting, a stuck window call)
        # gives up after 10 s; the worker thread then finishes on its own
        try:
            result = await asyncio.wait_for(asyncio.to_thread(capture_in_physical_pixels), timeout=10)
        except asyncio.TimeoutError:
            logger.error("Screenshot thread timed out")
            return json.dumps({"success": False, "error": "Screenshot operation timed out"})

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            logger.error(f"Screenshot error: {error_msg}")
            return json.dumps({"success": False, "error": error_msg})

        logger.info(f"Screenshot taken successfully, size: {result['width']}x{result['height']}")

        # Return the PNG as a proper MCP image content block instead of inline
        # base64 text: raw base64 in the text result exceeds client token
        # limits (a full-window capture is ~300 KB), while image blocks are
        # rendered natively by clients
        png_bytes = result.pop("image_data")
        zoom_info = response.get("result", {})
        if isinstance(zoom_info, dict) and "zoomed_component_count" in zoom_info:
            result["zoomed_component_count"] = zoom_info["zoomed_component_count"]
        # What was actually captured, as opposed to what was requested
        result["captured_kind"] = focused_kind or None
        result["captured_document"] = focused_document or None
        return [
            json.dumps(result),
            MCPImage(data=png_bytes, format="png"),
        ]

    except Exception as e:
        logger.error(f"Error in screenshot function: {str(e)}")
        return json.dumps({"success": False, "error": f"Failed to take screenshot: {str(e)}"})


@_tool()
async def layout_duplicator(ctx: Context) -> str:
    """
    First step of layout duplication. Selects source components and returns data to match with destination components.

    Returns:
        str: JSON object with source and destination component data for matching
    """
    logger.info("Starting layout duplication - selection phase")

    # Execute the command in Altium to get component data
    response = await altium_bridge.execute_command("layout_duplicator", {})

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("layout_duplicator", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error in layout duplication selection: {error_msg}")
        return json.dumps(
            {
                "success": False,
                "error": f"Failed to start layout duplication: {error_msg}",
            }
        )

    # Get the component data
    components_data = response.get("result", {})

    if not components_data:
        logger.info("No component data found")
        return json.dumps(
            {"success": False, "error": "No component data returned from Altium"}
        )

    # Parse the result to check if no source components were selected
    try:
        if isinstance(components_data, str):
            result_json = json.loads(components_data)
            if not result_json.get("success", True):
                logger.info(
                    f"Source component selection issue: {result_json.get('message', 'Unknown issue')}"
                )
                return json.dumps(result_json)
    except Exception as e:
        logger.error(f"Error parsing layout duplicator result: {e}")

    logger.info("Retrieved layout duplicator component data")
    return json.dumps(components_data, indent=2)

@_tool()
async def layout_duplicator_apply(ctx: Context, source_designators: list, destination_designators: list) -> str:
    """
    Second step of layout duplication. Applies the layout of source components to destination components.

    Args:
        source_designators (list): List of source component designators (e.g., ["R1", "C5", "U3"])
        destination_designators (list): List of destination component designators (e.g., ["R10", "C15", "U7"])

    Returns:
        str: JSON object with the result of the layout duplication
    """
    logger.info(
        f"Applying layout duplication from {source_designators} to {destination_designators}"
    )

    # Execute the command in Altium to apply layout duplication
    response = await altium_bridge.execute_command(
        "layout_duplicator_apply",
        {
            "source_designators": source_designators,
            "destination_designators": destination_designators,
        },
    )

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("layout_duplicator_apply", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error applying layout duplication: {error_msg}")
        return json.dumps(
            {
                "success": False,
                "error": f"Failed to apply layout duplication: {error_msg}",
            }
        )

    # Get the result data
    result = response.get("result", {})

    logger.info("Layout duplication applied successfully")
    return json.dumps(result, indent=2)
    
@_tool(read_only=True)
async def get_pcb_rules(ctx: Context) -> str:
    """
    Get all design rules from the current Altium PCB

    Returns:
        str: JSON array of PCB design rules: name, enabled, priority (1 is
             the highest within a rule kind), descriptor, rule_kind and the
             two scope expressions (filter1, filter2)
    """
    logger.info("Getting PCB design rules")

    # Execute the command in Altium to get rule data
    response = await altium_bridge.execute_command(
        "get_pcb_rules",
        {},  # No parameters needed
    )

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("get_pcb_rules", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error getting PCB rules: {error_msg}")
        return json.dumps({"error": f"Failed to get PCB rules: {error_msg}"})

    # Get the rules data
    rules_data = response.get("result", [])

    if not rules_data:
        logger.info("No PCB rules found")
        return json.dumps({"message": "No PCB rules found in the current document"})

    logger.info("Retrieved PCB rules data")
    return json.dumps(rules_data, indent=2)

@_tool()
async def run_drc(ctx: Context, board_path: str = "", max_violations: int = 200, rules: list = None,
                  nets: list = None, area: list = None) -> str:
    """
    Run the design rule check (DRC) on a PCB and list the violations.

    Runs Altium's own batch DRC with the board's rules and the options last
    set in its Design Rule Checker dialog. The dialog is answered from
    outside Altium: it may flash on screen. Altium writes its usual report
    beside the board (or in the project's output folder) and may open it.
    The violations stay on the board, marked as Altium marks them.

    A cancelled check leaves no violations, exactly like a clean board, so
    "ran" says whether the check really ran: the Run button was pressed and
    the dialog closed. "report" (when Altium wrote one) gives the per-rule
    counts and confirms it independently.

    Args:
        board_path (str): Full path of the .PcbDoc to check. It is opened if
            needed and brought to the front. Empty: the focused board, or
            the one the PCB tools last used.
        rules (list): Only violations of these rules: each is a rule's own
            name or a rule kind ("Clearance", "Width", "Short-Circuit",
            "Un-Routed Net", "Hole To Hole Clearance", ...), whole, in any
            case and with or without spaces. "Clearance" is the electrical
            clearance rules only, not hole-to-hole or silk clearances. The
            whole check still runs: Altium checks every enabled rule.
        nets (list): Only violations with an object on one of these nets.
        area (list): Only violations located in [x1, y1, x2, y2] (mils from
            the board origin), by their own or an object's position.
            With any filter, matching_count counts the matching violations;
            listed_by_rule always counts the ones listed.
        max_violations (int): How many violations to list in full.
            violation_count counts every violation the check found, but
            Altium stops the check at its own violation limit (500 unless
            changed in its Design Rule Checker dialog); "complete": false
            then says the board may have more.

    Returns:
        str: JSON with ran, violation_count, the violations (rule,
             description, x_mils/y_mils relative to the board origin, and
             the primitives involved with their net and layer), and the
             report's per-rule counts.
    """
    import drc

    # The filters are checked before the check runs, which takes a while.
    # A lone name counts as a list of one, not as letters.
    rules = [rules] if isinstance(rules, str) else rules
    nets = [nets] if isinstance(nets, str) else nets
    for label, names in (("rules", rules), ("nets", nets)):
        if names is not None and (not isinstance(names, (list, tuple))
                                  or not all(isinstance(n, str) for n in names)):
            return json.dumps({"success": False, "error": f"{label} is a list of names"})
    rules = [r for r in rules or () if r.strip()]
    nets = [n for n in nets or () if n.strip()]
    if area is not None and (not isinstance(area, (list, tuple)) or len(area) != 4 or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in area)):
        return json.dumps({"success": False, "error": "area is [x1, y1, x2, y2] in mils from the board origin"})
    filtered = bool(rules or nets or area is not None)

    board_path = long_path(board_path)
    logger.info(f"Running DRC on {board_path or 'the focused board'}")
    started = time.time()
    outcome = drc.DialogOutcome()
    driver = asyncio.create_task(drc.drive_dialog(outcome, PROGRESS_FILE))
    try:
        response = await altium_bridge.execute_command(
            "run_drc", {"board_path": board_path,
                        # filtered: the violations Altium kept (500 unless its
                        # limit was raised), up to a cap, then filter
                        "max_violations": drc.FILTER_FETCH if filtered else int(max_violations)})
    finally:
        driver.cancel()
        try:
            await driver
        except asyncio.CancelledError:
            pass

    if not response.get("success", False):
        return json.dumps({"success": False, "ran": outcome.pressed,
                           "error": f"DRC failed: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        try:
            result = json.loads(result, strict=False)
        except ValueError:
            return json.dumps({"success": False, "error": f"unexpected reply: {result[:200]}"})

    out = {"success": True, "ran": outcome.pressed}
    if not outcome.pressed:
        out["success"] = False
        out["error"] = ("The check did not run: " +
                        ("the Design Rule Checker dialog could not be started and was closed. "
                         if outcome.seen else "the Design Rule Checker dialog never appeared. ") +
                        "violation_count is what was already on the board, not a result.")
        if outcome.notes:
            out["notes"] = outcome.notes
    out.update({k: v for k, v in result.items() if k != "success"})
    # By how much each violation fails, as numbers; then the filters
    for v in out.get("violations", []):
        v.update(drc.measurement(v.get("description", "")))
    if filtered:
        fetched = out.get("violations", [])
        picked = [v for v in fetched if drc.matches(v, rules, nets, area)]
        out["matching_count"] = len(picked)
        out["violations"] = picked[:max(0, int(max_violations))]
        if out.get("violation_count", 0) > len(fetched):
            out["filter_note"] = (f"The filters looked at the first {len(fetched)} of "
                                  f"{out['violation_count']} violations; matching_count may be low.")
    by_rule = {}
    for v in out.get("violations", []):
        by_rule[v.get("rule", "")] = by_rule.get(v.get("rule", ""), 0) + 1
    out["listed_by_rule"] = by_rule
    report = drc.find_report(result.get("board", board_path), started)
    if report is not None:
        try:
            out["report"] = {"path": str(report),
                             **drc.parse_report(read_altium_text(report))}
        except OSError:
            pass
    if out.get("report", {}).get("stopped"):
        out["complete"] = False
        out["note"] = ("Altium stopped the check at its violation limit (set in its Design Rule "
                       "Checker dialog), so the board may have more violations than violation_count.")
    return json.dumps(out, indent=2)

@_tool(read_only=True)
async def get_pcb_layer_stackup(ctx: Context) -> str:
    """
    Get the detailed layer stackup information from the current Altium PCB including
    copper thickness, dielectric materials, constants, and heights

    Returns:
        str: JSON object with detailed layer stackup information
    """
    logger.info("Getting PCB layer stackup information")

    # Execute the command in Altium to get layer stackup data
    response = await altium_bridge.execute_command(
        "get_pcb_layer_stackup",
        {},  # No parameters needed
    )

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("get_pcb_layer_stackup", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error getting PCB layer stackup: {error_msg}")
        return json.dumps({"error": f"Failed to get PCB layer stackup: {error_msg}"})

    # Get the stackup data
    stackup_data = response.get("result", {})

    if not stackup_data:
        logger.info("No PCB layer stackup found")
        return json.dumps(
            {"message": "No PCB layer stackup found in the current document"}
        )

    logger.info("Retrieved PCB layer stackup data")
    return json.dumps(stackup_data, indent=2)

@_tool()
async def create_pcb_rule(ctx: Context, rule_type: str, name: str, values: dict,
                          scope1: str = "All", scope2: str = "All", comment: str = "",
                          replace: bool = False, board_path: str = "") -> str:
    """
    Add a design rule to a PCB.

    Values per rule_type, distances in mils:
      clearance              gap_mils (between scope1 and scope2)
      width                  min_mils, max_mils, preferred_mils (all signal layers)
      hole_size              min_mils, max_mils
      routing_via            min/max/preferred_diameter_mils,
                             min/max/preferred_hole_mils
      solder_mask_expansion  expansion_mils (negative shrinks the opening)
      paste_mask_expansion   expansion_mils
      polygon_connect        connect_style ("relief", "direct", "none"), and
                             optionally conductor_width_mils, air_gap_mils,
                             entries (2 or 4)

    Scopes are Altium query expressions, e.g. "All", "InNet('GND')",
    "InNetClass('Power')", "IsVia", "HasFootprint('0402')". A one-scope
    rule (width, hole size, via, mask, ...) uses scope1 only.

    PRIORITY: Altium makes a rule added this way priority 1, the highest
    of its kind, and moves the others down; a script cannot set priority.
    So a new rule wins wherever its scope applies. Give it a narrow scope
    (a net, class or object kind), or reorder priorities in Altium's PCB
    Rules and Constraints Editor afterwards. get_pcb_rules lists all rules
    with their priorities. Check the result's "descriptor": it is what
    Altium stored.

    Args:
        rule_type (str): One of the kinds above.
        name (str): Rule name, unique on the board.
        values (dict): The values for that kind, e.g. {"gap_mils": 8}.
        scope1 (str): First (or only) object scope.
        scope2 (str): Second scope, for two-object rules such as clearance.
        comment (str): Optional rule comment.
        replace (bool): Replace an existing rule of the same name instead
            of refusing.
        board_path (str): Full path of the .PcbDoc; empty for the focused
            board, or the one the PCB tools last used.

    Returns:
        str: JSON with the rule's name, descriptor, scopes, priority and
             whether it replaced an existing rule.
    """
    import pcb_rules

    board_path = long_path(board_path)
    encoded, error = pcb_rules.encode_values(rule_type, values)
    if error:
        return json.dumps({"success": False, "error": error})
    if not name.strip():
        return json.dumps({"success": False, "error": "name is empty"})
    logger.info(f"Creating {rule_type} rule {name}")
    response = await altium_bridge.execute_command("create_pcb_rule", {
        "rule_kind": rule_type, "rule_name": name.strip(), "scope1": scope1 or "All",
        "scope2": scope2 or "All", "comment": comment, "values": encoded,
        "replace": bool(replace), "board_path": board_path})
    if not response.get("success", False):
        return json.dumps({"success": False,
                           "error": f"Failed to create the rule: {response.get('error', 'unknown error')}"})
    result = response.get("result", {})
    if isinstance(result, str):
        return result
    others = result.get("other_rules_of_kind", 0)
    if others and (scope1 or "All").strip().lower() == "all" and (scope2 or "All").strip().lower() == "all":
        result["warning"] = (f"This rule applies to everything at priority 1, so it now overrides the "
                             f"{others} other {rule_type} rule(s) on the board. Narrow its scope or "
                             f"move it down in Altium's rules editor if that is not intended.")
    return json.dumps(result, indent=2)

# Changes the design: a timeout leaves its outcome unknown, so no blind retry
MUTATING_COMMANDS.add("create_pcb_rule")

@_tool(read_only=True)
async def get_output_job_containers(ctx: Context, outjob_path: str = "") -> str:
    """
    Get all available output job containers from a specified OutJob file


    Args:
        outjob_path (str): Full path to the OutJob file (optional). When empty,
            the first OutJob found in the open projects is used, in project
            order, regardless of which document has focus.

    Returns:
        str: JSON with outjob_path (the file actually used) and the containers
    """
    logger.info(
        f"Getting output job containers from {outjob_path or 'the first open OutJob'}"
    )

    # Execute the command in Altium to get output job containers
    response = await altium_bridge.execute_command(
        "get_output_job_containers", {"outjob_path": outjob_path}
    )

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting output job containers: {error_msg}")
        return json.dumps(
            {"error": f"Failed to get output job containers: {error_msg}"}
        )

    # Get the containers data
    containers_data = response.get("result", [])

    if not containers_data:
        logger.info("No output job containers found")
        return json.dumps({"message": "No output job containers found"})

    logger.info("Retrieved output job containers data")
    return containers_data  # Already in JSON format

@_tool(read_only=True)
async def get_outjob_settings(ctx: Context, outjob_path: str, include_raw_settings: bool = False) -> str:
    """
    Read an .OutJob file from disk and return its outputs, containers and settings.

    Works without Altium and on OutJobs that are not open. It reads the SAVED
    file: changes not yet saved in Altium are not visible.

    Notes for interpreting the result:
    - An output is generated when it is connected to a container
      (connected_containers non-empty). The per-output "enabled" flag mirrors
      OutputEnabledN in the file and does not always match the UI.
    - Gerber layers are decoded from Altium V7 layer IDs; unknown IDs are
      reported as "Unknown layer 0x...".

    Args:
        outjob_path (str): Full path to the .OutJob file
        include_raw_settings (bool): Also return raw_settings, the output's
            settings as the file has them, for outputs with a decoded
            summary. It doubles the reply. Outputs without a summary always
            have raw_settings.

    Returns:
        str: JSON with path, group_name, variant, containers (name, type,
             output paths, file name expression, add_to_project) and outputs
             (type, name, document_path, variant, connected_containers and a
             decoded summary for Gerber, NC Drill, test points, pick and
             place and print outputs, or sections by name for a BOM)
    """
    try:
        data = parse_outjob(outjob_path, include_raw=include_raw_settings)
    except FileNotFoundError:
        return json.dumps({"error": f"OutJob not found: {outjob_path}"})
    except Exception as e:
        return json.dumps({"error": f"Could not parse OutJob: {e}"})
    return json.dumps(data, indent=2, ensure_ascii=False)


@_tool(read_only=True)
async def get_harness_data(
    ctx: Context,
    file_path: str = "",
    summary_only: bool = False,
    designators: list | None = None,
) -> str:
    """
    Read an Altium Harness Wiring Diagram (.WirDoc) or Harness Project (.PrjHar).

    Works without Altium GUI and on files that are not open. It extracts
    connectors (with associated parts), pins (with cavity parts: crimp, seal, plug,
    other), wires, splices, twists, shields, cables, wire breaks and net labels from
    the SAVED file (unsaved Altium edits are not included). If file_path is omitted,
    it reads the harness project or document Altium has focused.

    Coordinates are Altium DXP units (1 = 10 mils). Twists, shields and cables are
    rectangles x1/y1/x2/y2 with the wires they link; a shield with connection
    (style 1) also gives its pin and drain_wires. Wire breaks give the wire they sit on;
    wires with the same designator on other sheets of the project are one connection.

    A layout drawing (.LdrDoc) gives connectors (location, orientation, views, connection
    point), connection_points (objects held, bundles), bundles (points, length and drawn
    length in mils and mm, end connection points, wires), coverings, layout_labels, wires
    (ends as "P3.12" / "SP1", route = bundles on the shortest path between the connection
    points of the two ends, routed_length), unrouted_wires, splices, twists and
    no_connects. Every connector, connection point, bundle, covering and label lists its
    texts (designator, comment, a bundle's length; a connector's shown parameters) with
    position, hidden flag and justification (the corner or edge the position names); a
    connection point's show_name is whether its Designator text is shown. Edit it with
    edit_harness_layout.

    A full wiring diagram can be very large; use summary_only or designators to keep
    the answer small.

    Args:
        file_path (str, optional): Full path to the .WirDoc, .LdrDoc, or .PrjHar file.
            Defaults to empty string (the document focused in Altium).
        summary_only (bool): Connectors without pins, wires without geometry. Default False.
        designators (list, optional): Only these connectors (with their pins) and the wires
            that touch them.

    Returns:
        str: JSON with file_path, summary (connector_count, total_pins,
             connected_pins, text_count, net_label_count), connectors (designator,
             lib_reference, description, location, unique_id, associated_parts, pins
             with nets and cavity_parts), wires, splices, twists, shields, cables,
             wire_breaks, no_connects, texts (sheet texts; never nets), net_labels
             (net labels, invalid in a harness project; convert_netlabels_to_text
             fixes them) and source.
    """
    try:
        p: Path | None = None
        if file_path and file_path.strip():
            p = Path(file_path.strip())
        else:
            p = await _get_active_harness_path()

        if not p or not p.exists():
            return json.dumps(
                {
                    "error": (
                        f"File not found: '{file_path}'. No active harness project (.PrjHar) "
                        "or wiring diagram (.WirDoc) detected in the current workspace."
                    )
                }
            )

        if p.suffix.lower() == ".prjhar":
            data = parse_prjhar(p)
        elif p.suffix.lower() == ".ldrdoc":
            data = parse_ldrdoc(p)
            if designators:
                wanted = set(designators)
                data["connectors"] = [
                    c for c in data["connectors"] if c["designator"] in wanted
                ]
                data["wires"] = [
                    w
                    for w in data["wires"]
                    if {w["end1"].split(".")[0], w["end2"].split(".")[0]} & wanted
                ]
            if summary_only:
                for c in data["connectors"]:
                    c.pop("views", None)
                data["wires"] = [
                    {
                        "designator": w["designator"],
                        "end1": w["end1"],
                        "end2": w["end2"],
                        "routed_length": w["routed_length"],
                    }
                    for w in data["wires"]
                ]
        elif p.suffix.lower() == ".wirdoc":
            data = parse_wirdoc(p)
            if designators:
                wanted = set(designators)
                data["connectors"] = [
                    c for c in data["connectors"] if c["designator"] in wanted
                ]
                touching = {
                    pin["connected_wire"]
                    for c in data["connectors"]
                    for pin in c["pins"]
                    if pin.get("connected_wire")
                }
                data["wires"] = [
                    w for w in data["wires"] if w["designator"] in touching
                ]
                for key in ("twists", "shields", "cables"):
                    data[key] = [
                        o
                        for o in data.get(key, [])
                        if touching & set(o["wires"] + o.get("drain_wires", []))
                    ]
                data["wire_breaks"] = [
                    b for b in data.get("wire_breaks", []) if b["wire"] in touching
                ]
            if summary_only:
                data["connectors"] = [
                    {
                        "designator": c["designator"],
                        "comment": c["comment"],
                        "lib_reference": c["lib_reference"],
                        "location": c["location"],
                        "pin_count": len(c["pins"]),
                    }
                    for c in data["connectors"]
                ]
                data["wires"] = [
                    {
                        "designator": w["designator"],
                        "net": w["net"],
                        "part_number": w.get("design_item_id", ""),
                    }
                    for w in data["wires"]
                ]
                data.pop("texts", None)
                data.pop("net_labels", None)
                data.pop("net_connections", None)
        else:
            return json.dumps(
                {
                    "error": f"Unsupported file type '{p.suffix}'. Expected .WirDoc, .LdrDoc, or .PrjHar"
                }
            )
    except Exception as e:
        return json.dumps({"error": f"Could not parse harness document: {e}"})
    data.update(_disk_source(p))
    return json.dumps(data, indent=2, ensure_ascii=False)


@_tool(read_only=True)
async def get_multiboard_data(
    ctx: Context,
    file_path: str = "",
    harness_map: dict | None = None,
    entry_map: dict | None = None,
    include_pins: bool = True,
    include_geometry: bool = False,
) -> str:
    """
    Read and review an Altium multi-board schematic (.MbsDoc) against its harness
    projects; read-only, works without Altium on the SAVED files.

    It lists the blocks (Custom Parts and child-project modules) with their entries,
    linked library components and pins (pin name = the net name Altium gives it), and
    the connections (harness, cable, direct) with what each entry mates: a block entry,
    or another harness's entry through an inline connector pair (with its pin pairs).
    Then it matches every harness connection to a harness project (.PrjHar) of the
    multi-board project by connector part numbers (the library's Mating Part, either
    way), each harness entry to one connector of that project, and traces nets pin by
    pin from device pin to device pin through the harness wires and inline connectors.
    Two harness connections built from the same project are traced as two instances.

    issues (severity error / warning / info):
      dangling_entry - harness entry mating nothing. Altium only joins two harnesses by
        an inline connector made by placing an entry ON a harness line (it splits that
        harness in two); an open harness end cannot be dropped onto another harness.
      unconnected_entry, entry_without_component, component_not_in_library,
      harness_not_linked (no harness project linked yet), duplicate_title,
      no_harness_connector (no connector of the matched project mates the device: wrong
        port, key or part), ambiguous_harness_connector (several identical connectors
        fit: pass entry_map), pin_missing_on_device / pin_missing_on_harness,
      wired_to_unused_pin (harness wire on a device pin named NC / RSVD),
      single_device_net (a wired net that reaches no other device pin, with every
        harness connector on it mapped).

    Args:
        file_path: .MbsDoc or .PrjMbd. Default: the first .PrjMbd in the workspace.
        harness_map: {"H_1": "<harness project name or path>"} to force the project of
            a harness connection.
        entry_map: {"H_3": {"Entry_5": "P5"}} to assign harness entries to harness
            connector designators where identical connectors make it ambiguous.
        include_pins: per-pin tables (pin_review) and traced nets (system_nets). Turn
            off for a short structural answer.
        include_geometry: also the drawn shape of each connection (geometry: entry
            points with their connect positions and directions, junctions, line
            segments; y up, grid_size units), to plan edit_multiboard_schematic
            routes.

    Returns:
        JSON with summary, project, child_projects, library, blocks, connections,
        inline_links, harness_review (matched_project, entries -> connector or
        candidates), pin_review [{entry, harness, pins [{pin, device, harness, net}],
        unwired_pins}], system_nets [{harness_nets, devices, unmapped_harness_pins}],
        issues and (on request) geometry.
    """
    from multiboard import drawing_geometry, find_workspace_prjmbd, review_multiboard

    p = (
        Path(file_path.strip())
        if file_path and file_path.strip()
        else find_workspace_prjmbd()
    )
    if not p or not p.exists() or p.suffix.lower() not in (".mbsdoc", ".prjmbd"):
        return json.dumps(
            {"success": False, "error": f"No .MbsDoc or .PrjMbd found ('{file_path}')"}
        )
    try:
        data = review_multiboard(
            p, harness_map=harness_map, entry_map=entry_map, include_pins=include_pins
        )
        if include_geometry:
            data["geometry"] = drawing_geometry(data["file_path"])
    except Exception as e:
        logger.error(f"Error reviewing multi-board schematic: {e}")
        return json.dumps({"success": False, "error": f"Could not read {p}: {e!s}"})
    data.update(_disk_source(Path(data["file_path"])))
    return json.dumps({"success": True, **data}, indent=2, ensure_ascii=False)


async def _multiboard_document(action: str, path: Path) -> dict | None:
    """Altium's hold on a document the server edits on disk (.MbsDoc, .HarDwf):
    {open, modified} after the action (status / close / open), or None when
    Altium could not be asked."""
    try:
        response = await altium_bridge.execute_command(
            "multiboard_document", {"action": action, "document_path": str(path)}
        )
    except Exception as e:
        logger.warning(f"multiboard_document {action} failed: {e}")
        return None
    if not response.get("success", False):
        return {"success": False, "error": response.get("error", "unknown error")}
    result = response.get("result", {})
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return {
                "success": False,
                "error": f"unexpected Altium reply: {result[:200]!r}",
            }
    return result


@_tool()
async def edit_multiboard_schematic(
    ctx: Context,
    document_path: str,
    operations: list,
    dry_run: bool = False,
) -> str:
    """
    Edit an Altium multi-board schematic (.MbsDoc): re-assign Custom Part
    connectors, join two harnesses with an inline connector, rename blocks.

    Altium's multi-board editor has no script interface, so the tool edits the saved
    file: if Altium holds the document it must have no unsaved changes (save it
    first); the tool closes it, writes the file (backup in
    C:/Users/Public/altium_mcp/backups), and opens it again. Operations apply in
    order; if one fails nothing is written. Plan routes with
    get_multiboard_data(include_geometry=True) and check the result with it and
    get_screenshot(view_type="mbs", document_path=...).

    operations (each a dict with "op"):
      {"op": "swap_entry_parts", "block": "MOD_5", "entries": ["J3", "J4"]}
          Swap the linked components (and designators) of two entries of a block;
          harnesses stay drawn where they are, so each now mates the other part.
          Use it when a harness is plugged into the wrong one of two connectors.
      {"op": "join_harnesses", "entry": "H_1.Entry_4", "to": "H_5",
       "part": "DTM04-6P", "mate_part": "DTM06-6S", "attach": [x, y],
       "via": [[x, y], ...], "entry_gender": "male"}
          Make an open harness end an inline connector (part) mated with a new entry
          of the other harness (mate_part), drawn two grid steps further along the
          end's direction - what Altium does when an entry is placed on a harness
          line. A new branch runs from the new entry's connect position through the
          optional via corners to attach: a junction of the other harness or a point
          on one of its lines (that line is split there). Every leg must be
          horizontal or vertical. Pins are copied from a component of the same part
          in the document, else numbered after a harness connector of that part in
          the multi-board project; pins pair by number.
      {"op": "set_title", "block": "MOD_2", "title": "Module #1 (High Side)"}
      {"op": "assign_entry_parts", "connection": "H_1",
       "entries": {"Entry_1": {"part": "MX34028SF1", "designator": "P1"}}}
          Give harness entries their connector component (the harness side of the
          system BOM): pins numbered after the harness connector of that part,
          mapped to the entry and mated by number with the device entry it plugs
          into (each takes the device pin's net name); the entry and component are
          named after the designator ("rename_entry": false keeps the entry name).
          An entry already carrying the part (inline connector) only gets the
          designator.
      {"op": "assign_harness_parts", "connections": ["H_1"], "entry_map": {...}}
          assign_entry_parts for every entry from its match with the harness
          project's connectors (get_multiboard_data harness_review); connections
          defaults to all harness connections, entry_map settles entries that fit
          several identical connectors. Unmatched entries are listed in skipped.

    Args:
        document_path: The .MbsDoc.
        operations: List of operations (see above).
        dry_run: Apply in memory and report, write nothing.

    Returns:
        JSON with success, results per operation, saved, backup, reopened and the
        review summary and issues of the saved document (get_multiboard_data,
        without pins).
    """
    import shutil
    from datetime import datetime

    from multiboard import (
        _harness_connectors,
        find_prjmbd,
        parse_prjmbd,
        review_multiboard,
    )
    from multiboard_edit import MbsDocument, MbsEditError, apply_operations

    p = Path(document_path).resolve() if document_path else None
    if not p or p.suffix.lower() != ".mbsdoc" or not p.exists():
        return json.dumps(
            {
                "success": False,
                "error": f"document_path to a .MbsDoc is required: {document_path!r}",
            }
        )
    if not isinstance(operations, list) or not operations:
        return json.dumps(
            {"success": False, "error": "operations must be a non-empty list"}
        )

    # assign_harness_parts: one assign_entry_parts per connection, from the
    # entries' match with the harness projects' connectors
    expanded: list = []
    skipped: list[dict] = []
    for op in operations:
        if not (isinstance(op, dict) and op.get("op") == "assign_harness_parts"):
            expanded.append(op)
            continue
        wanted = op.get("connections")
        for row in review_multiboard(
            p, entry_map=op.get("entry_map"), include_pins=False
        )["harness_review"]:
            if wanted and row["connection"] not in wanted:
                continue
            entries = {}
            for e in row["entries"]:
                name = e["entry"].split(".", 1)[1]
                if e.get("connector") and e.get("connector_part"):
                    entries[name] = {
                        "part": e["connector_part"],
                        "designator": e["connector"],
                    }
                else:
                    skipped.append(
                        {
                            "entry": e["entry"],
                            "reason": f"connector candidates {e['candidates']}; pass entry_map"
                            if e.get("candidates")
                            else "no harness connector matched",
                        }
                    )
            if not row["matched_project"]:
                skipped.append(
                    {
                        "entry": row["connection"],
                        "reason": f"no single harness project matches (candidates {row['candidates']})",
                    }
                )
            elif entries:
                expanded.append(
                    {
                        "op": "assign_entry_parts",
                        "connection": row["connection"],
                        "entries": entries,
                    }
                )
    operations = expanded
    if not operations:
        return json.dumps(
            {"success": False, "error": "nothing to assign", "skipped": skipped}
        )

    # Pins and library links for new harness-side components
    parts = {
        op.get(k)
        for op in operations
        if isinstance(op, dict) and op.get("op") == "join_harnesses"
        for k in ("part", "mate_part")
    } | {
        (spec if isinstance(spec, str) else spec.get("part"))
        for op in operations
        if isinstance(op, dict)
        and op.get("op") == "assign_entry_parts"
        and isinstance(op.get("entries"), dict)
        for spec in op["entries"].values()
        if isinstance(spec, (str, dict))
    }
    parts -= {None}
    pin_sources: dict[str, list[str]] = {}
    libraries: dict[str, tuple[str, str]] = {}
    if parts:
        prj = find_prjmbd(p)
        for child in parse_prjmbd(prj)["child_projects"] if prj else []:
            if child["kind"] != "harness" or not Path(child["path"]).exists():
                continue
            for k in _harness_connectors(Path(child["path"])):
                if k["part_number"] in parts and k["part_number"] not in pin_sources:
                    pin_sources[k["part_number"]] = list(k["pins"])
        dbp = find_dblib(p.parent)
        if dbp:
            try:
                db = load_dblib(dbp)
                for part in parts:
                    found = db.find(part)
                    if found:
                        libraries[part] = (db.name, found[0].name)
            except Exception as e:
                logger.warning(f"database library not read: {e}")

    try:
        doc = MbsDocument(p)
        results = apply_operations(doc, operations, pin_sources, libraries)
    except MbsEditError as e:
        return json.dumps({"success": False, "error": str(e), "saved": False})
    if dry_run:
        return json.dumps(
            {
                "success": True,
                "dry_run": True,
                "saved": False,
                "results": results,
                "skipped": skipped,
            },
            indent=2,
        )

    status = await _multiboard_document("status", p)
    was_open = bool(status and status.get("success") and status.get("open"))
    if status and status.get("success") and status.get("modified"):
        return json.dumps(
            {
                "success": False,
                "saved": False,
                "error": "Altium has unsaved changes in the document; save or discard them first",
            }
        )
    if was_open:
        closed = await _multiboard_document("close", p)
        if not closed or not closed.get("success") or closed.get("open"):
            return json.dumps(
                {
                    "success": False,
                    "saved": False,
                    "error": f"could not close the document in Altium: {closed}",
                }
            )
    backup_dir = EXCHANGE_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"{p.stem}.{datetime.now():%Y%m%d_%H%M%S}{p.suffix}"
    shutil.copy2(p, backup)
    try:
        doc.save()
    except OSError as e:
        return json.dumps(
            {
                "success": False,
                "saved": False,
                "error": f"write failed: {e}",
                "backup": str(backup),
            }
        )
    reopened = None
    if was_open:
        opened = await _multiboard_document("open", p)
        reopened = bool(opened and opened.get("success") and opened.get("open"))
    review = review_multiboard(p, include_pins=False)
    return json.dumps(
        {
            "success": True,
            "saved": True,
            "results": results,
            "skipped": skipped,
            "backup": str(backup),
            "was_open": was_open,
            "reopened": reopened,
            "summary": review["summary"],
            "issues": review["issues"],
        },
        indent=2,
        ensure_ascii=False,
    )


@_tool(read_only=True)
async def get_harness_drawing_data(
    ctx: Context,
    document_path: str,
    include_template_items: bool = False,
) -> str:
    """
    Review an Altium Draftsman harness drawing (.HarDwf, the fabrication drawing
    of a harness project) from the saved file. Read-only; Altium is not used.

    Reports every page (size, sheet template and whether its file exists, the
    page's own items: id, type, title / text, position and size in mm from the
    sheet's bottom-left corner, y up), every harness component view
    (designator, view side, stored projection lines) and the stored component
    projections, and lists issues: missing_template, empty_component_view (an
    empty projection is stored, which Draftsman never recomputes),
    outside_page, overlap (between the page's own items other than layout /
    wiring views), todo_text.

    Args:
        document_path: Path to the .HarDwf.
        include_template_items: Also list the items that come from the sheet
            template (frame, title block).

    Returns:
        JSON with document, source (the layout drawing it shows), pages,
        component_views, projections and issues.
    """
    from harness_drawing import review_drawing

    p = Path(document_path).resolve() if document_path else None
    if not p or p.suffix.lower() not in (".hardwf", ".hardot") or not p.exists():
        return json.dumps(
            {
                "success": False,
                "error": f"document_path to a .HarDwf (or .HarDot template) is required: {document_path!r}",
            }
        )
    try:
        review = review_drawing(p, include_template_items=include_template_items)
    except (OSError, ValueError) as e:
        return json.dumps({"success": False, "error": f"drawing not read: {e}"})
    return json.dumps({"success": True, **review}, indent=2, ensure_ascii=False)


@_tool()
async def edit_harness_drawing(
    ctx: Context,
    document_path: str,
    operations: list,
    dry_run: bool = False,
) -> str:
    """
    Edit an Altium Draftsman harness drawing (.HarDwf, or a .HarDot template) on
    the saved file:
    component views and their projections, item positions and texts, sheet
    templates.

    Draftsman has no script interface, so the tool edits the saved file: if
    Altium holds the document it must have no unsaved changes (save it first);
    the tool closes it, writes the file (backup in
    C:/Users/Public/altium_mcp/backups) and opens it again. Operations apply in
    order; if one fails nothing is written. Positions are mm from the sheet's
    bottom-left corner, y up (a view's position is its bottom-left corner).
    Review with get_harness_drawing_data and look with
    get_screenshot(view_type="dwf", document_path=...).

    Component views: Draftsman draws the projection stored for the view's
    designator and side, and computes one only when none is stored, from the
    connector footprint's 3D body found through the database library (the
    lookup passes no table name). When that fails, as it has for every
    connector of a CSV-backed DbLib tested so far, an empty projection is stored and
    never recomputed. Compute the visible edges of the footprint's 3D
    model yourself (extract the STEP with altium-society extract_step_model;
    build123d shape.project_to_viewport(dir * 1000, up, (0, 0, 0)) with
    Draftsman's frame per side, dir / up: Top (0,0,1)/(0,1,0), Bottom
    (0,0,-1)/(0,1,0), Front (0,-1,0)/(0,0,1), Back (0,1,0)/(0,0,1), Left
    (-1,0,0)/(0,0,1), Right (1,0,0)/(0,0,1); curves split into short lines)
    and store them with set_component_projection; Draftsman keeps them.

    Operations (dicts with "op"):
      set_component_view {item_id, designator?, view_side?, scale?}
          view_side Top|Bottom|Left|Right|Front|Back; scale e.g. 4 (4:1)
      set_view_scale {item_id, scale}    any view: 2 (2:1), 0.5 (1:2), 0.75
      set_view_title {item_id, title?, title_position?}
          any view; title may use =ComponentName, =ViewScale; title_position
          "auto" (Draftsman centres it above the view) or [dx_mm, dy_mm]
      set_component_projection {designator, view_side, lines_mm | lines_file}
          lines [[x1, y1, x2, y2], ...] in mm, view frame, y up; lines_file a
          JSON file with that list or {side: list}. Replaces the stored one.
      remove_component_projection {designator, view_side?}
          Drop stored projections so Draftsman computes them again on update.
      copy_item {item_id, x_mm?, y_mm?, designator?, view_side?, scale?,
                 component?}
          Copy an item on its page under a new id (e.g. a second view);
          component: the connector a copied connection table lists (e.g.
          a table for a connector added to the layout; Draftsman fills
          its rows from the layout, the review reports each table's
          component).
      add_callout {item_id, text, point_mm, leader_mm?}
          Custom-text callout on a component view (e.g. "Pin 1"), snapped
          to point_mm [x, y] of its projection (same frame as the lines);
          leader_mm [dx, dy] places the text on the page (default [10, 10]).
          Draftsman keeps it on the point when the view moves or rescales.
          Adds Draftsman's default arrow style when the document has none
          (a callout without it is not drawn).
      copy_table_format {from_item_id, from_document?, item_ids?}
          Copy a connection table's / wiring list's columns or a BOM's
          shown columns (set, order, visibility, sort, widths) onto the
          other tables of that type (or item_ids); from_document takes the
          reference table from another .HarDwf. Tables keep their content
          and position; check widths and overlaps on a screenshot.
      move_item {item_id, x_mm, y_mm}
      set_text {item_id, text}           Text items
      delete_item {item_id}              not template items
      set_template {to, pages? | from?}  point pages (1-based) or every page
          using template path "from" at an existing sheet template file

    Args:
        document_path: Path to the .HarDwf.
        operations: List of operations.
        dry_run: Apply in memory and report, write nothing.

    Returns:
        JSON with success, results per operation, saved, backup, was_open,
        reopened and the review's issues after the edit.
    """
    import shutil
    from datetime import datetime

    from harness_drawing import (
        DrawingEditError,
        HarnessDrawing,
        apply_operations,
        review_drawing,
    )

    p = Path(document_path).resolve() if document_path else None
    if not p or p.suffix.lower() not in (".hardwf", ".hardot") or not p.exists():
        return json.dumps(
            {
                "success": False,
                "error": f"document_path to a .HarDwf (or .HarDot template) is required: {document_path!r}",
            }
        )
    if not isinstance(operations, list) or not operations:
        return json.dumps(
            {"success": False, "error": "operations must be a non-empty list"}
        )
    try:
        doc = HarnessDrawing(p)
        results = apply_operations(doc, operations)
    except (DrawingEditError, OSError, ValueError) as e:
        return json.dumps({"success": False, "error": str(e), "saved": False})
    if dry_run:
        return json.dumps(
            {"success": True, "dry_run": True, "saved": False, "results": results},
            indent=2,
            ensure_ascii=False,
        )

    status = await _multiboard_document("status", p)
    was_open = bool(status and status.get("success") and status.get("open"))
    if status and status.get("success") and status.get("modified"):
        return json.dumps(
            {
                "success": False,
                "saved": False,
                "error": "Altium has unsaved changes in the document; save or discard them first",
            }
        )
    if was_open:
        closed = await _multiboard_document("close", p)
        if not closed or not closed.get("success") or closed.get("open"):
            return json.dumps(
                {
                    "success": False,
                    "saved": False,
                    "error": f"could not close the document in Altium: {closed}",
                }
            )
    backup_dir = EXCHANGE_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"{p.stem}.{datetime.now():%Y%m%d_%H%M%S}{p.suffix}"
    shutil.copy2(p, backup)
    try:
        doc.save()
    except OSError as e:
        return json.dumps(
            {
                "success": False,
                "saved": False,
                "error": f"write failed: {e}",
                "backup": str(backup),
            }
        )
    reopened = None
    if was_open:
        opened = await _multiboard_document("open", p)
        reopened = bool(opened and opened.get("success") and opened.get("open"))
    review = review_drawing(p)
    return json.dumps(
        {
            "success": True,
            "saved": True,
            "results": results,
            "backup": str(backup),
            "was_open": was_open,
            "reopened": reopened,
            "issues": review["issues"],
        },
        indent=2,
        ensure_ascii=False,
    )


def _harness_import_targets(path: Path) -> list[Path]:
    """The documents import_harness_changes updates for path: the document
    itself, a harness project's layout drawings followed by its Draftsman
    drawings (a drawing shows its layout, so the layout goes first), or every
    child harness subproject of a multi-board project (.PrjMbd)."""
    suffix = path.suffix.lower()
    if suffix == ".prjmbd":
        subprojects = [
            (path.parent / line.split("=", 1)[1].strip()).resolve()
            for line in read_altium_text(path).splitlines()
            if line.startswith("DocumentPath=")
        ]
        return [
            target
            for sub in subprojects
            if sub.suffix.lower() == ".prjhar" and sub.exists()
            for target in _harness_import_targets(sub)
        ]
    if suffix != ".prjhar":
        return [path]
    docs = [
        (path.parent / line.split("=", 1)[1].strip()).resolve()
        for line in read_altium_text(path).splitlines()
        if line.startswith("DocumentPath=")
    ]
    return [d for d in docs if d.suffix.lower() == ".ldrdoc"] + [
        d for d in docs if d.suffix.lower() == ".hardwf"
    ]


def _layout_import_report(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    """What a layout import changed: wires whose part or description changed,
    wires added / removed, and the routing state afterwards."""
    old = {w["designator"]: w for w in before["wires"]}
    new = {w["designator"]: w for w in after["wires"]}
    updated = [
        {
            "designator": name,
            "part_before": old[name].get("part_number", ""),
            "part_after": w.get("part_number", ""),
            "description": w.get("description", ""),
        }
        for name, w in new.items()
        if name in old
        and (
            old[name].get("part_number") != w.get("part_number")
            or old[name].get("description") != w.get("description")
        )
    ]
    return {
        "wires_updated": updated,
        "wires_added": sorted(set(new) - set(old)),
        "wires_removed": sorted(set(old) - set(new)),
        "wires_without_part": sorted(
            name for name, w in new.items() if not w.get("part_number")
        ),
        "unrouted_wires": after["unrouted_wires"],
        "newly_unrouted_wires": sorted(
            set(after["unrouted_wires"]) - set(before["unrouted_wires"])
        ),
        "unrouted_reasons": unrouted_reasons(after),
        "unassigned_pins": {
            c["designator"]: c["unassigned_pins"]
            for c in after["connectors"]
            if c.get("unassigned_pins")
        },
    }


def _drawing_import_report(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    """What a Draftsman update changed: items moved, resized, added or
    removed, and component projections it emptied."""

    def items(review: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {it["id"]: it for page in review["pages"] for it in page["items"]}

    def lines(review: dict[str, Any]) -> dict[tuple[str, str], int]:
        return {
            (p["designator"], p["view_side"]): p["lines"] for p in review["projections"]
        }

    def top_left(it: dict[str, Any]) -> tuple[float, float]:
        # Positions are bottom-left corners; a table that grows keeps its top
        y = (it.get("y_mm") or 0) + (it.get("height_mm") or 0)
        return it.get("x_mm") or 0, y

    old, new = items(before), items(after)
    moved, resized = [], []
    for item_id, it in new.items():
        prev = old.get(item_id)
        if prev is None:
            continue
        if any(abs(a - b) > 0.01 for a, b in zip(top_left(prev), top_left(it))):
            moved.append(item_id)
        if (prev.get("width_mm"), prev.get("height_mm")) != (
            it.get("width_mm"),
            it.get("height_mm"),
        ):
            resized.append(item_id)
    old_lines, new_lines = lines(before), lines(after)
    return {
        "items_moved": moved,
        "items_resized": resized,
        "items_added": sorted(set(new) - set(old)),
        "items_removed": sorted(set(old) - set(new)),
        "projections_emptied": [
            f"{des} {side}"
            for (des, side), n in old_lines.items()
            if n and not new_lines.get((des, side))
        ],
        "issues": after["issues"],
    }


@_tool()
async def import_harness_changes(ctx: Context, document_path: str) -> str:
    """
    Run Altium's "Import Changes From ..." on a harness project's layout drawing
    (.LdrDoc) or Draftsman fabrication drawing (.HarDwf), and save it.

    A layout drawing takes the changes of the project's wiring diagrams (Design >
    Import Changes From <project>: wire parts and parameters, new / removed wires,
    connectors, splices...; applied at once, no ECO dialog). Altium's import leaves
    connection points naming pins that no longer exist, so the tool then runs the
    pin repair of edit_harness_layout when a connector has unassigned pins. A
    Draftsman drawing re-reads its layout drawing (Tools > Import Changes From
    <layout>): BOM, wiring list, connection tables, layout view; the component
    projections stored with edit_harness_drawing are kept.

    document_path may be a .PrjHar (or a .PrjMbd to import all of its child
    .PrjHar subprojects): its layout drawings are imported first, then its
    Draftsman drawings. The harness project must be open in Altium with no
    unsaved changes in any of its documents (only the import is saved; a layout
    is checked against the saved wiring diagrams afterwards and imported again
    while they differ). A document that was not open is closed again afterwards.
    The import and the save run as standalone scripts (run_altium_script), so a
    sandbox recovery may follow a script error.

    Args:
        document_path: Full path to a .LdrDoc, .HarDwf, .PrjHar or .PrjMbd.

    Returns:
        JSON with success and, per document, changed / saved and a report: for a
        layout wires_updated (part before / after), wires_added, wires_removed,
        wires_without_part, unrouted_wires, newly_unrouted_wires, unassigned_pins and
        pin_repair; for a drawing items_moved, items_resized, items_added,
        items_removed, projections_emptied and the review's issues.
    """
    from harness_drawing import review_drawing

    path = Path(document_path).resolve() if document_path else None
    if (
        not path
        or not path.exists()
        or path.suffix.lower() not in (".ldrdoc", ".hardwf", ".prjhar", ".prjmbd")
    ):
        return json.dumps(
            {
                "success": False,
                "error": (
                    "document_path to a .LdrDoc, .HarDwf, .PrjHar or .PrjMbd "
                    f"is required: {document_path!r}"
                ),
            }
        )
    targets = _harness_import_targets(path)
    if not targets:
        return json.dumps(
            {
                "success": False,
                "error": f"{path.name} has no layout or Draftsman drawing",
            }
        )

    documents = []
    for doc in targets:
        is_layout = doc.suffix.lower() == ".ldrdoc"
        entry: dict[str, Any] = {"document": str(doc)}
        documents.append(entry)
        if not doc.exists():
            entry.update(success=False, error="file not found")
            continue
        try:
            before = parse_ldrdoc(doc) if is_layout else review_drawing(doc)
        except Exception as e:  # noqa: BLE001 - report per document
            entry.update(success=False, error=f"could not read the saved file: {e}")
            continue
        run = await _run_harness_import(doc)
        opened = run["open"] or {}
        entry.update(opened)
        entry.update(run["result"])
        entry["document"] = str(doc)
        entry["attempts"] = run["attempts"]
        if run["sync_gaps"]:
            entry["sync_gaps"] = run["sync_gaps"]
        if entry.get("success") and entry.get("changed"):
            await _report_harness_import(ctx, doc, is_layout, before, entry)
        if opened.get("success") and not opened.get("was_open"):
            closed = await _multiboard_document("close", doc)
            entry["closed"] = bool(
                closed and closed.get("success") and not closed.get("open")
            )
    return json.dumps(
        {
            "success": all(d.get("success") for d in documents),
            "documents": documents,
        },
        indent=2,
        ensure_ascii=False,
    )


def _layout_sync_gaps(layout: dict[str, Any], project: Path) -> list[str]:
    """Wires of the project's wiring diagrams the saved layout does not carry
    as they are: missing, or with another part number. Empty when in sync."""
    source = {
        w["unique_id"]: w
        for wirdoc in get_harness_wiring_docs(project)
        for w in parse_wirdoc(wirdoc)["wires"]
    }
    in_layout = {w["designator"] for w in layout["wires"]}
    gaps = [
        f"{name}: not in the layout"
        for name in sorted({w["designator"] for w in source.values()} - in_layout)
    ]
    for w in layout["wires"]:
        src = source.get(w.get("origin_uid"))
        if src and (src.get("design_item_id") or "") != (w.get("part_number") or ""):
            gaps.append(
                f"{w['designator']}: part {w.get('part_number') or '(none)'}, "
                f"wiring diagram {src.get('design_item_id') or '(none)'}"
            )
    return gaps


# Seconds to wait before each import attempt; a layout still differing from its
# wiring diagrams after the first is imported again after the longer wait.
HARNESS_IMPORT_WAITS = (1.0, 5.0)

# Standalone scripts (run_altium_script) that open, import and save, each a run
# of its own. Written so because a save in a bridge run wrote the layout
# without the import Altium held in memory (2026-10-29), while the same save
# as a standalone script wrote it. The bridge then ran as a script project;
# since it runs a script file (upstream v0.6.1) a bridge save keeps the import
# (verified 2026-11-04), so these steps could move into the bridge. Every
# script first focuses the document as the Projects panel does - a closed one
# is opened through its project (IDocument.DM_OpenAndFocusDocument), an open
# one only shown: on an open document DM_OpenAndFocusDocument reloads it from
# disk once the run
# returns, discarding its unsaved changes - and checks that the document and
# its project are focused; S2 holds the error if not.
_FOCUS_PRELUDE = """S1 := '{path}';
S2 := '';
S4 := '';
Obj3 := nil;
for I1 := 0 to GetWorkspace.DM_ProjectCount - 1 do
begin
    Obj1 := GetWorkspace.DM_Projects(I1);
    for I2 := 0 to Obj1.DM_LogicalDocumentCount - 1 do
        if UpperCase(Obj1.DM_LogicalDocuments(I2).DM_FullPath) = UpperCase(S1) then
        begin
            Obj3 := Obj1.DM_LogicalDocuments(I2);
            S4 := Obj1.DM_ProjectFullPath;
        end;
end;
if Obj3 = nil then
    S2 := 'error: no open project holds the document'
else
begin
    Obj5 := Client.GetDocumentByPath(S1);
    if Obj5 = nil then
    begin
        SandboxLog('open');
        Obj3.DM_OpenAndFocusDocument;
    end
    else
        Client.ShowDocument(Obj5);
    Obj4 := Client.GetCurrentView;
    if (Obj4 = nil) or (UpperCase(Obj4.OwnerDocument.FileName) <> UpperCase(S1)) then
        S2 := 'error: Altium did not focus the document'
    else if (UpperCase(GetWorkspace.DM_FocusedProject.DM_ProjectFullPath) <> UpperCase(S4)) then
        S2 := 'error: Altium did not focus the project of the document';
end;
"""

_OPEN_SCRIPT = (
    _FOCUS_PRELUDE
    + """if S2 <> '' then
    ResultText := S2
else
    ResultText := 'opened';"""
)

_IMPORT_SCRIPT = (
    _FOCUS_PRELUDE
    + """if S2 <> '' then
    ResultText := S2 + '; nothing was imported'
else
begin
    SandboxLog('{process}');
    ResetParameters;
    RunProcess('{process}');
    ResultText := 'imported';
end;"""
)

_SAVE_SCRIPT = (
    _FOCUS_PRELUDE
    + """if S2 <> '' then
    ResultText := S2 + '; nothing was saved'
else if Obj5 = nil then
    ResultText := 'error: the document is not open in Altium'
else
begin
    // Obj5 is late bound: 'if Obj5.Modified then' always reads False
    B1 := 0;
    if BoolToStr(Obj5.Modified, True) = 'True' then
    begin
        B1 := 1;
        SandboxLog('save');
        Obj5.DoFileSave('');
    end;
    if BoolToStr(Obj5.Modified, True) = 'True' then
        ResultText := 'error: the document was not saved'
    else
        ResultText := 'saved ' + IntToStr(B1);
end;"""
)


async def _harness_import_script(template: str, doc: Path, process: str = "") -> dict:
    """Run one of the import scripts on doc: {"success", "text"} or an error."""
    script = template.format(path=str(doc).replace("'", "''"), process=process)
    out = json.loads(await run_altium_script(None, script, 180))
    text = str(out.get("result", ""))
    if not out.get("success"):
        return {
            "success": False,
            "error": out.get("altium_message") or out.get("error", "script failed"),
            "executor_wedged": out.get("executor_wedged", False),
        }
    if text.startswith("error: "):
        return {"success": False, "error": text[len("error: ") :]}
    return {"success": True, "text": text}


async def _run_harness_import(doc: Path) -> dict[str, Any]:
    """Check (bridge command import_harness_changes), then open, import and
    save one document, each a standalone script run (_FOCUS_PRELUDE). A layout
    is checked against the wiring diagrams after the save, and imported again
    after a longer wait while they differ. Returns {"open": the check's result
    or None, "result": {success, saved, changed} or the failed step's error,
    "attempts", "sync_gaps"}."""
    try:
        response = await altium_bridge.execute_command(
            "import_harness_changes", {"document_path": str(doc)}
        )
    except TimeoutError as e:
        response = {"success": False, "error": str(e)}
    if not response.get("success", False):
        error = {"success": False, "error": response.get("error", "unknown error")}
        return {"open": None, "result": error, "attempts": 0, "sync_gaps": []}
    opened = response.get("result", {})
    if isinstance(opened, str):
        try:
            opened = json.loads(opened)
        except ValueError:
            opened = {
                "success": False,
                "error": f"unexpected Altium reply: {opened[:200]!r}",
            }
    if not opened.get("success"):
        return {"open": opened, "result": opened, "attempts": 0, "sync_gaps": []}
    shown = await _harness_import_script(_OPEN_SCRIPT, doc)
    if not shown.get("success"):
        return {"open": opened, "result": shown, "attempts": 0, "sync_gaps": []}

    attempts = 0
    gaps: list[str] = []
    changed = False
    result: dict[str, Any] = {}
    for wait in HARNESS_IMPORT_WAITS:
        attempts += 1
        await asyncio.sleep(wait)
        result = await _harness_import_script(
            _IMPORT_SCRIPT, doc, opened.get("process", "")
        )
        if not result.get("success"):
            break
        result = await _harness_import_script(_SAVE_SCRIPT, doc)
        if not result.get("success"):
            break
        changed = changed or result.pop("text") == "saved 1"
        result.update(saved=True, changed=changed)
        if doc.suffix.lower() != ".ldrdoc":
            break
        gaps = _layout_sync_gaps(parse_ldrdoc(doc), Path(opened.get("project") or ""))
        if not gaps:
            break
    if gaps and result.get("success"):
        result.update(
            success=False, error="the layout still differs from the wiring diagrams"
        )
    return {"open": opened, "result": result, "attempts": attempts, "sync_gaps": gaps}


async def _report_harness_import(
    ctx: Context, doc: Path, is_layout: bool, before: dict[str, Any], entry: dict
) -> None:
    """After an import: repair a layout's connection point pins, and add what
    changed to entry (success False when wires lost their route or a drawing
    lost a component projection)."""
    from harness_drawing import review_drawing

    if not is_layout:
        entry.update(_drawing_import_report(before, review_drawing(doc)))
        if entry["projections_emptied"]:
            entry["success"] = False
        return
    after = parse_ldrdoc(doc)
    # Repair only pins that carry a wire: an unassigned pin without one routes
    # nothing, and the repair costs a save and reload of the drawing
    wired = {w.get(end) for w in after["wires"] for end in ("end1", "end2")}
    if any(
        f"{c['designator']}.{p}" in wired
        for c in after["connectors"]
        for p in c.get("unassigned_pins", [])
    ):
        repair = json.loads(await edit_harness_layout(ctx, str(doc)))
        entry["pin_repair"] = {
            k: repair.get(k)
            for k in ("success", "saved", "connection_point_pins_synced", "error")
            if k in repair
        }
        if not repair.get("success"):
            entry["success"] = False
        after = parse_ldrdoc(doc)
    entry.update(_layout_import_report(before, after))
    if entry["newly_unrouted_wires"]:
        entry["success"] = False


async def _simple_command(command: str, params: dict, what: str) -> str:
    """Run a bridge command and return its result as JSON text."""
    response = await altium_bridge.execute_command(command, params)
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error getting {what}: {error_msg}")
        return json.dumps({"error": f"Failed to get {what}: {error_msg}"})
    result = response.get("result", {})
    return (
        result
        if isinstance(result, str)
        else json.dumps(result, indent=2, ensure_ascii=False)
    )


async def _bridge_json(command: str, params: dict, timeout: Optional[float] = None) -> Tuple[Optional[dict], str]:
    """(result, "") of a bridge command that returns JSON, or (None, error).
    One helper for the tools that work with the reply instead of passing it
    on; strict=False because Altium text can hold control characters."""
    if timeout is None:
        response = await altium_bridge.execute_command(command, params)
    else:
        response = await altium_bridge.execute_command(command, params, timeout=timeout)
    if not response.get("success", False):
        return None, response.get("error", "unknown error")
    result = response.get("result", {})
    if isinstance(result, str):
        try:
            result = json.loads(result, strict=False)
        except ValueError:
            return None, result if result.startswith("ERROR") else f"unexpected reply: {result[:300]}"
    if not isinstance(result, dict):
        return None, f"unexpected reply: {str(result)[:300]}"
    return result, ""


@_tool(read_only=True)
async def get_project_info(ctx: Context) -> str:
    """
    Get the focused Altium project: parameters, documents and variants.

    Read-only. Use it to check that nothing has unsaved changes before
    generating outputs, and to read project parameters and variants.

    Returns:
        str: JSON with project_path, optional parent_project_path (when the
             focused project is a child subproject of an open .PrjMbd),
             parameters [{name, value}], schematic_sheet_count,
             modified_document_count, documents [{path, kind, state}] where
             state is "closed", "open" or "modified" (unsaved changes in
             memory), optional subprojects [{project_path, kind, state,
             modified_document_count, documents}] (when the focused project
             is a .PrjMbd), and variants [{name, variations [{designator,
             kind}]}] where kind is "not_fitted" or "alternate"
    """
    response = await altium_bridge.execute_command("get_project_info", {})
    if response.get("success", False):
        result = response.get("result", {})
        return (
            result
            if isinstance(result, str)
            else json.dumps(result, indent=2, ensure_ascii=False)
        )

    # Fallback for offline harness projects
    try:
        h_path = await _get_active_harness_path()
        if h_path and h_path.suffix.lower() == ".prjhar":
            data = parse_prjhar(h_path)
            return json.dumps(
                {
                    "project_path": data["project_path"],
                    "parameters": [
                        {"name": k, "value": v} for k, v in data["parameters"].items()
                    ],
                    "schematic_sheet_count": len(
                        [
                            d
                            for d in data["documents"]
                            if d["kind"] == "HarnessWiringDiagram"
                        ]
                    ),
                    "modified_document_count": 0,
                    "documents": [
                        {"path": d["path"], "kind": d["kind"], "state": "closed"}
                        for d in data["documents"]
                    ],
                    "variants": [],
                    **_disk_source(h_path),
                },
                indent=2,
                ensure_ascii=False,
            )
    except Exception as e:
        logger.warning(f"Project info harness fallback failed: {e}")

    error_msg = response.get("error", "Unknown error")
    logger.error(f"Error getting project info: {error_msg}")
    return json.dumps({"error": f"Failed to get project info: {error_msg}"})


def _project_path(project_path: str):
    """A compile tool's project_path in its long form, and an error ('' when
    fine): empty for the focused project, else an existing .PrjPcb or .PrjHar
    (harness projects are read from their saved files). Altium would open any
    project file it is given, a script project too."""
    if not project_path:
        return "", ""
    path = long_path(project_path)
    if not path.lower().endswith((".prjpcb", ".prjhar")):
        return path, f"project_path must be a .PrjPcb or .PrjHar file: {project_path}"
    if not os.path.isfile(path):
        return path, f"project_path not found: {project_path}"
    return path, ""



def _is_harness_project(project_path: str) -> bool:
    """Whether a compile tool's project_path names a harness project (.PrjHar)."""
    return project_path.lower().endswith(".prjhar")


@_tool(read_only=True)
async def get_project_components(ctx: Context, include_parameters: bool = False, designators: list = None,
                                 sheet: str = "", offset: int = 0, limit: int = 0,
                                 parameter_names: list = None, project_path: str = "") -> str:
    """
    List every component of the compiled project (schematic or harness side).

    Compiles the project in memory first (nothing is saved): project_path,
    or the focused project, as for get_erc_violations. Unlike
    the PCB component tools, this returns the schematic Comment of each
    instance, which is where values of generic passives usually live.
    An Altium Harness project (.PrjHar, given as project_path or focused)
    is read from its saved wiring diagrams (.WirDoc) instead.

    A large project does not fit in one reply. A reply stops at about 60,000
    characters: when next_offset is not null, call again with
    offset=next_offset and the same filters for the rest. Filter first when
    you can: all parameters of every part can come to megabytes.

    Args:
        include_parameters (bool): Also return every component parameter
            (e.g. Manufacturer_Part_Number). Default False - the output is
            much larger with parameters.
        designators (list): Only these designators. Wildcards work and case
            does not matter, e.g. ["R*", "U1", "C1?"].
        sheet (str): Only this sheet, by file name ("Power.SchDoc");
            wildcards work.
        offset (int): Skip this many matching components.
        limit (int): At most this many components; 0 for as many as fit.
        parameter_names (list): Return only these parameters, e.g.
            ["Manufacturer_Part_Number"]. Implies include_parameters.
        project_path (str): Full path of the .PrjPcb to compile, or of a
            .PrjHar to read from its saved files.

    Returns:
        str: JSON with project_path, component_count (the whole project),
             matched, offset, returned, next_offset and components
             [{designator, comment, footprint, library_reference, sheet,
             parameters?}]
    """
    import project_components

    with_parameters = include_parameters or bool(parameter_names)
    project_path, error = _project_path(project_path)
    if error:
        return json.dumps({"success": False, "error": error})
    response = None
    if not _is_harness_project(project_path):
        response = await altium_bridge.execute_command(
            "get_project_components",
            {"include_parameters": with_parameters, "project_path": project_path})
    if response is not None and response.get("success", False):
        result = response.get("result", {})
        try:
            data = json.loads(result, strict=False) if isinstance(result, str) else result
        except ValueError:
            return result
        if not isinstance(data, dict) or "components" not in data:
            return result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False)
        return project_components.compact_json(project_components.select(
            data, designators=designators, sheet=sheet, offset=offset, limit=limit,
            parameter_names=parameter_names))

    # Harness projects (.PrjHar / .WirDoc), parsed from the saved files: the
    # one named, else the focused one. Their parameters are a {name: value}
    # dict, so names are picked here.
    try:
        h_path = (Path(project_path) if _is_harness_project(project_path)
                  else await _get_active_harness_path())
        if h_path:
            components = get_harness_components(h_path, include_parameters=with_parameters)
            if parameter_names:
                wanted = {str(n).upper() for n in parameter_names}
                for comp in components:
                    comp["parameters"] = {k: v for k, v in (comp.get("parameters") or {}).items()
                                          if str(k).upper() in wanted}
            data = {"project_path": str(h_path.resolve()), "component_count": len(components),
                    "components": components}
            page = project_components.select(data, designators=designators, sheet=sheet,
                                             offset=offset, limit=limit)
            return project_components.compact_json({**page, **_disk_source(h_path)})
    except Exception as e:
        logger.warning(f"Harness fallback failed: {e}")

    error_msg = (response or {}).get("error", "Unknown error")
    logger.error(f"Error getting project components: {error_msg}")
    return json.dumps({"error": f"Failed to get project components: {error_msg}"})


@_tool(read_only=True)
async def get_erc_violations(ctx: Context, compile: bool = True, project_path: str = "") -> str:
    """
    Compile a project and list its ERC/compiler violations.

    Give project_path to compile exactly that project (it is opened if
    needed): without it the focused project is compiled, which is whatever
    document last had focus, and "Free Documents" is refused. The result
    names the project it compiled.

    Compiling reads each sheet from disk, except a sheet open in Altium with
    unsaved changes: that one is compiled from Altium's copy and listed in
    unsaved_in_altium. Save it (or close it unsaved) for the file to count.
    Sheets the project lists but that are not on disk are in
    missing_on_disk. Compiling does not save any document (Altium may
    refresh the derived .PrjPcbStructure file).

    For harness projects (.PrjHar, given as project_path or focused),
    performs native rule-checking on the saved files (unconnected pins,
    single pin nets, unannotated connectors).

    Args:
        compile (bool): Compile before reading (default True). False returns
            the violations of the last compilation.
        project_path (str): Full path of the .PrjPcb to compile, or of a
            .PrjHar to read from its saved files.

    Returns:
        str: JSON with project_path, unsaved_in_altium, missing_on_disk,
             total, fatal, errors, warnings and violations
             [{level, type, detail}]
    """
    project_path, error = _project_path(project_path)
    if error:
        return json.dumps({"success": False, "error": error})
    response = None
    if not _is_harness_project(project_path):
        response = await altium_bridge.execute_command(
            "get_erc_violations", {"compile": compile, "project_path": project_path}
        )
    if response is not None and response.get("success", False):
        result = response.get("result", {})
        return (
            result
            if isinstance(result, str)
            else json.dumps(result, indent=2, ensure_ascii=False)
        )

    # Harness projects: the one named, else the focused one (a compile
    # returns no flattened document for them)
    try:
        h_path = (Path(project_path) if _is_harness_project(project_path)
                  else await _get_active_harness_path())
        if h_path:
            violations = get_harness_erc_violations(h_path)
            return json.dumps(
                {**violations, **_disk_source(h_path)}, indent=2, ensure_ascii=False
            )
    except Exception as e:
        logger.warning(f"ERC harness fallback failed: {e}")

    error_msg = (response or {}).get("error", "Unknown error")
    logger.error(f"Error getting ERC violations: {error_msg}")
    return json.dumps({"error": f"Failed to get ERC violations: {error_msg}"})


@_tool(read_only=True)
async def get_board_summary(ctx: Context) -> str:
    """
    Summarise the focused PCB for fabrication checks (or harness overview).

    Read-only. Coordinates are millimetres relative to the board origin.
    If the active project is an Altium Harness project, returns a harness
    system summary instead.

    Returns:
        str: JSON with PCB or Harness summary metrics.
    """
    response = await altium_bridge.execute_command("get_board_summary", {})
    if response.get("success", False):
        result = response.get("result", {})
        return (
            result
            if isinstance(result, str)
            else json.dumps(result, indent=2, ensure_ascii=False)
        )

    # Fallback for harness projects
    try:
        h_path = await _get_active_harness_path()
        if h_path:
            w_docs = get_harness_wiring_docs(h_path)
            components = get_harness_components(h_path)
            nets = get_harness_nets(h_path)
            return json.dumps(
                {
                    "project_type": "harness",
                    "message": "Active project is an Altium Harness project (no PCB board)",
                    "project_path": str(h_path.resolve()),
                    "harness_documents": [d.name for d in w_docs],
                    "connector_count": len(components),
                    "unique_nets_count": len(nets),
                    "connectors": [c["designator"] for c in components],
                    **_disk_source(h_path),
                },
                indent=2,
                ensure_ascii=False,
            )
    except Exception as e:
        logger.warning(f"Board summary harness fallback failed: {e}")

    error_msg = response.get("error", "Unknown error")
    logger.error(f"Error getting board summary: {error_msg}")
    return json.dumps({"error": f"Failed to get board summary: {error_msg}"})


@_tool(read_only=True)
async def get_component_primitives(ctx: Context, designators: list) -> str:
    """
    List the primitives of PCB components with the layer each one is on.

    Read-only. Useful to check footprints that were flipped to the bottom
    side (e.g. fiducials: the copper and the pad that opens the solder mask
    must end up on the same layer) and pad mask expansions.

    Args:
        designators (list): Component designators, e.g. ["FD1", "FD3"]

    Returns:
        str: JSON with components [{designator, footprint, layer, x_mm, y_mm,
             rotation, primitives [{type, layer, ...}]}] and
             missing_designators
    """
    response = await altium_bridge.execute_command(
        "get_component_primitives", {"designators": designators}
    )
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        h_err = await _pcb_only_tool_error("get_component_primitives", error_msg)
        if h_err:
            return h_err
        logger.error(f"Error getting component primitives: {error_msg}")
        return json.dumps({"error": f"Failed to get component primitives: {error_msg}"})
    result = response.get("result", {})
    return (
        result
        if isinstance(result, str)
        else json.dumps(result, indent=2, ensure_ascii=False)
    )


@_tool()
async def run_output_jobs(ctx: Context, container_names: list, outjob_path: str = "") -> str:
    """
    Run specified output job containers

    The OutJob's project must be open in Altium. A PDF (Publish) container
    reports output_file and output_verified: true once Altium has written
    that file. output_verified is false for generated-file containers and
    for PDFs that Altium names itself (after each output, or from an
    expression). When a PDF already exists and the container prompts before
    overwriting (the default), Altium asks in a dialog and the run waits for
    the answer.

    Args:
        container_names (list): List of container names to run
        outjob_path (str): Full path to the OutJob file (optional). When empty,
            the first OutJob found in the open projects is used, in project
            order, regardless of which document has focus.

    Returns:
        str: JSON object with outjob_path (the file actually used) and the
             results of running the output jobs
    """
    logger.info(f"Running output jobs from {outjob_path or 'the first open OutJob'}")
    logger.info(f"Containers to run: {container_names}")

    # Execute the command in Altium to run output jobs
    response = await altium_bridge.execute_command(
        "run_output_jobs",
        {"outjob_path": outjob_path, "container_names": container_names},
    )

    # Check for success
    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error running output jobs: {error_msg}")
        return json.dumps({"error": f"Failed to run output jobs: {error_msg}"})

    # Get the result data
    result_data = response.get("result", {})

    logger.info("Output jobs execution completed")

    # If result_data is a string, it's already in JSON format
    if isinstance(result_data, str):
        return result_data

    # Otherwise, convert to JSON
    return json.dumps(result_data, indent=2)

@_tool()
async def create_pcb_footprint(ctx: Context, footprint_name: str, description: str, pads: list, courtyard_x_mm: float = 0, courtyard_y_mm: float = 0) -> str:
    """
    Create a new PCB footprint in the currently active PcbLib document.
    The PcbLib (e.g. Discrete.PcbLib) must be the focused document in Altium.

    Pad format: each element is "pad_number|x_mm|y_mm|width_mm|height_mm|shape"
                shape options: Rect (default), Round, Oval
                Coordinates are in mm relative to component origin (0,0).
                Pin 1 is indicated by a gap in the top-left silkscreen corner.
                Pads are created locked.

    Courtyard & silkscreen are auto-generated from pad extents + 0.25 mm margin
    unless courtyard_x_mm / courtyard_y_mm are provided explicitly (half-dimensions).

    Args:
        footprint_name (str): Footprint name as it will appear in the library
        description (str): Description string
        pads (list): List of pad definitions, e.g. ["1|-0.9|0.55|1.0|0.8|Rect", ...]
        courtyard_x_mm (float): Half-width of courtyard in mm (0 = auto)
        courtyard_y_mm (float): Half-height of courtyard in mm (0 = auto)

    Returns:
        str: JSON object with result
    """
    logger.info(f"Creating PCB footprint: {footprint_name} with {len(pads)} pads")

    response = await altium_bridge.execute_command(
        "create_pcb_footprint",
        {
            "footprint_name": footprint_name,
            "description": description,
            "pads": pads,
            "courtyard_x_mm": courtyard_x_mm,
            "courtyard_y_mm": courtyard_y_mm,
        },
    )

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error creating footprint: {error_msg}")
        return json.dumps(
            {"success": False, "error": f"Failed to create footprint: {error_msg}"}
        )

    result = response.get("result", {})
    logger.info(f"Footprint {footprint_name} created successfully")
    return json.dumps(result, indent=2)

@_tool(read_only=True)
async def get_server_status(ctx: Context) -> str:
    """Get the current status of the Altium MCP server"""
    altium_bridge.config.discover_altium()
    status = {
        "server": "Running",
        "altium_exe": altium_bridge.config.altium_exe_path,
        "script_path": altium_bridge.config.script_path,
        "altium_found": os.path.exists(altium_bridge.config.altium_exe_path),
        "script_found": os.path.exists(altium_bridge.config.script_path),
    }

    return json.dumps(status, indent=2)


if __name__ == "__main__":
    logger.info("Starting Altium MCP Server...")
    logger.info(f"Using MCP directory: {MCP_DIR}")

    # Initialize the directory
    MCP_DIR.mkdir(exist_ok=True)

    # Create the AltiumScript directory if it doesn't exist
    script_dir = MCP_DIR / "AltiumScript"
    script_dir.mkdir(exist_ok=True)
    
    # Paths are verified, with a file dialog if need be, on the first command
    # that needs Altium: a dialog here would hold up the client's initialize.
    # Logged, not printed: under the stdio transport stdout is the protocol.
    logger.info(f"Altium executable: {altium_bridge.config.altium_exe_path or '(not found yet)'}")
    logger.info(f"Script path: {altium_bridge.config.script_path}")
    
    # Run the server
    mcp.run(transport='stdio')
