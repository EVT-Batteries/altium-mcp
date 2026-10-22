import asyncio
import base64
import contextlib
import glob
import io
import json
import logging
import msvcrt
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import win32api
import win32con
import win32gui
import win32process
import win32ui
from altium_files import read_altium_text, write_altium_text
from dblib import color_to_tcolor, find_dblib, load_csv_table, load_dblib
from harness_layout import build_layout_spec, check_layout, parse_ldrdoc
from harness_parser import (
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
from outjob_parser import parse_outjob

# Log to stderr and to the per-user data folder (not the source tree).
# ALTIUM_MCP_LOG_LEVEL=DEBUG for detailed logs.
_LOG_DIR = Path(os.environ.get("ALTIUM_MCP_HOME") or Path.home() / ".altium-mcp")
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=os.environ.get("ALTIUM_MCP_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(_LOG_DIR / "altium_mcp.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("AltiumMCPServer")

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
BRIDGE_LOCK_FILE = EXCHANGE_DIR / "bridge.lock"
# Longer than one script's 120 s response timeout
BRIDGE_LOCK_TIMEOUT = 300

# Initialize FastMCP server
mcp = MCPServer("AltiumMCP")


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
        """Create a default configuration file with improved Altium executable discovery"""

        # Try to find Altium directories dynamically
        altium_base_path = r"C:\Program Files\Altium"
        altium_exe_path = None

        if os.path.exists(altium_base_path):
            # Find all directories that match the pattern AD*
            ad_dirs = glob.glob(os.path.join(altium_base_path, "AD*"))

            if ad_dirs:
                # Sort directories by version number (extract the number after "AD")
                def get_version_number(dir_path):
                    match = re.search(r"AD(\d+)", os.path.basename(dir_path))
                    if match:
                        return int(match.group(1))
                    return 0

                # Sort directories by version number (highest first)
                ad_dirs.sort(key=get_version_number, reverse=True)

                # Try each directory until we find one with X2.EXE
                for ad_dir in ad_dirs:
                    potential_exe = os.path.join(ad_dir, "X2.EXE")
                    if os.path.exists(potential_exe):
                        altium_exe_path = potential_exe
                        break

        # Set the found path (or empty string if nothing found)
        self.altium_exe_path = altium_exe_path if altium_exe_path else ""

        # Save the configuration
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

    def verify_paths(self):
        """Verify that the paths in the configuration exist, prompt for input if they don't"""

        # Initialize variables
        root = None
        paths_verified = True

        # Check Altium executable
        if not self.altium_exe_path or not os.path.exists(self.altium_exe_path):
            paths_verified = False

            # Before prompting, try an automatic discovery
            altium_base_path = r"C:\Program Files\Altium"
            if os.path.exists(altium_base_path):
                logger.info(f"Attempting automatic discovery in {altium_base_path}")
                # Find all directories that match the pattern AD*
                ad_dirs = glob.glob(os.path.join(altium_base_path, "AD*"))

                if ad_dirs:
                    # Sort directories by version number (extract the number after "AD")
                    def get_version_number(dir_path):
                        match = re.search(r"AD(\d+)", os.path.basename(dir_path))
                        if match:
                            return int(match.group(1))
                        return 0

                    # Sort directories by version number (highest first)
                    ad_dirs.sort(key=get_version_number, reverse=True)

                    # Try each directory until we find one with X2.EXE
                    for ad_dir in ad_dirs:
                        potential_exe = os.path.join(ad_dir, "X2.EXE")
                        if os.path.exists(potential_exe):
                            self.altium_exe_path = potential_exe
                            logger.info(
                                f"Automatically found Altium at: {self.altium_exe_path}"
                            )
                            paths_verified = True
                            break

            # If automatic discovery failed, prompt for input
            if not self.altium_exe_path or not os.path.exists(self.altium_exe_path):
                if root is None:
                    import tkinter as tk
                    from tkinter import filedialog

                    root = tk.Tk()
                    root.withdraw()  # Hide the main window

                logger.info(
                    "Altium executable not found. Prompting user for selection..."
                )
                logger.info(
                    f"Altium executable not found. Searched in: "
                    f"C:\\Program Files\\Altium\\AD*\\X2.EXE; "
                    f"Last known path: {self.altium_exe_path}"
                )

                self.altium_exe_path = filedialog.askopenfilename(
                    title="Select Altium Executable",
                    filetypes=[("Executable files", "*.exe")],  # Only allow .exe files
                    initialdir="C:/Program Files/Altium",
                )

                if not self.altium_exe_path:
                    logger.error(
                        "No Altium executable selected. Some functionality may not work."
                    )
                    paths_verified = False

        # Check script path
        if not os.path.exists(self.script_path):
            paths_verified = False

            if root is None:
                import tkinter as tk
                from tkinter import filedialog

                root = tk.Tk()
                root.withdraw()  # Hide the main window

            logger.info(
                f"Script file not found at {self.script_path}. Prompting user for selection..."
            )

            selected_path = filedialog.askopenfilename(
                title="Select Altium Project File",
                filetypes=[
                    ("Altium Project files", "*.PrjScr")
                ],  # Changed to PrjScr for script project
                initialdir=str(MCP_DIR),
            )

            if selected_path:
                self.script_path = selected_path
            else:
                logger.error(
                    "No script file selected. Some functionality may not work."
                )
                paths_verified = False

        # Clean up tkinter root if created
        if root is not None:
            root.destroy()

        # Save the updated configuration
        self.save_config()

        return paths_verified


class AltiumBridge:
    def __init__(self):
        # Ensure the MCP directory exists
        MCP_DIR.mkdir(exist_ok=True)

        # Load configuration
        self.config = AltiumConfig()
        self.config.verify_paths()

        # Commands share a single request.json/response.json pair, so
        # concurrent tool calls must be serialized or they clobber each other
        self._command_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def exclusive(self):
        """Hold the Altium bridge: an asyncio lock for this server and an OS
        lock on BRIDGE_LOCK_FILE for every other server process using the same
        exchange folder (e.g. one started by Gemini and one by Claude). The OS
        releases the lock if a process dies."""
        async with self._command_lock:
            with open(BRIDGE_LOCK_FILE, "a+b") as handle:  # noqa: ASYNC230
                deadline = time.time() + BRIDGE_LOCK_TIMEOUT
                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.time() > deadline:
                            raise TimeoutError(
                                "Altium bridge busy: another MCP server process held "
                                f"{BRIDGE_LOCK_FILE} for {BRIDGE_LOCK_TIMEOUT} s"
                            ) from None
                        await asyncio.sleep(0.25)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    async def execute_command(
        self, command: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a command in Altium via the bridge script"""
        try:
            async with self.exclusive():
                return await self._execute_command_locked(command, params)
        except TimeoutError as e:
            return {"success": False, "error": str(e)}

    async def _execute_command_locked(
        self, command: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            blocking = _blocking_altium_dialogs()
            if blocking:
                return {
                    "success": False,
                    "error": f"Altium is blocked; {_blocked_ask_user(blocking)}",
                    "executor_wedged": True,
                }

            # Clean up any existing response file
            if RESPONSE_FILE.exists():
                RESPONSE_FILE.unlink()

            # Write the request file with command and parameters
            with open(REQUEST_FILE, "w") as f:
                json.dump(
                    {
                        "command": command,
                        **params,  # Include parameters directly in the main JSON object
                    },
                    f,
                    indent=2,
                )

            logger.info(f"Wrote request file for command: {command}")

            # Run the Altium script
            success = await self.run_altium_script()
            if not success:
                return {"success": False, "error": "Failed to run Altium script"}

            # Wait for the response file
            logger.info("Waiting for response file to appear...")
            timeout = 120  # seconds
            start_time = time.time()
            while not RESPONSE_FILE.exists() and time.time() - start_time < timeout:
                await asyncio.sleep(0.5)

            if not RESPONSE_FILE.exists():
                logger.error("Timeout waiting for response from Altium")
                # Usually the script paused in the debugger, which blocks
                # every later call until it is stopped
                # Tools pass only "error" on, so the outcome goes into it
                recovery = await _recover_script_executor()
                if recovery["recovered"]:
                    detail = (
                        f"script engine unblocked automatically ({recovery['method']}); "
                        "the command did not complete, check its document before retrying"
                    )
                else:
                    detail = recovery["ask_user"]
                return {
                    "success": False,
                    "error": f"No response received from Altium (timeout); {detail}",
                    "executor_wedged": not recovery["recovered"],
                }

            # Read the response file and print it for debugging
            logger.info("Response file found, reading response")
            response_text = read_altium_text(RESPONSE_FILE)

            # Log the raw response for debugging
            logger.info(f"Raw response (first 200 chars): {response_text[:200]}")

            # Parse the JSON response with detailed error handling
            try:
                response = json.loads(response_text)
                logger.info("Successfully parsed JSON response")
                return response
            except json.JSONDecodeError as e:
                logger.error(f"Error parsing JSON response: {e}")
                logger.error(
                    f"Error at position {e.pos}, line {e.lineno}, column {e.colno}"
                )
                logger.error(
                    f"Character at error position: '{response_text[e.pos : e.pos + 10]}...'"
                )

                # Try to manually fix common JSON issues
                logger.info("Attempting to fix JSON response...")
                fixed_text = response_text

                # Fix 1: If there's a quoted JSON array, try to fix it
                if '"[' in fixed_text and ']"' in fixed_text:
                    fixed_text = fixed_text.replace('"[', "[").replace(']"', "]")
                    logger.info("Fixed double-quoted JSON array")

                # Fix 2: Handle escaped quotes in JSON strings
                fixed_text = fixed_text.replace('\\"', '"')

                # Try to parse the fixed JSON
                try:
                    fixed_response = json.loads(fixed_text)
                    logger.info("Successfully parsed fixed JSON response")
                    return fixed_response
                except json.JSONDecodeError as e2:
                    logger.error(f"Still failed to parse JSON after fixes: {e2}")

                # If all else fails, return a structured error
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
            logger.error(
                f"Altium executable not found at: {self.config.altium_exe_path}"
            )
            return False

        if not os.path.exists(self.config.script_path):
            logger.error(f"Script file not found at: {self.config.script_path}")
            return False

        try:
            # Resolve MSIX-virtualized path so Altium (an external process
            # outside the MSIX sandbox) can find the script files
            script_path = self._resolve_msix_path(self.config.script_path)

            # Command format: "X2.EXE" -RScriptingSystem:RunScript(ProjectName="path\file.PrjScr"|ProcName="ModuleName>Run")
            command = f'"{self.config.altium_exe_path}" -RScriptingSystem:RunScript(ProjectName="{script_path}"^|ProcName="Altium_API>Run")'

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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
            "Only place pins on the left and right side of the symbol. "
            "Place power rail pins at the upper right, ground pins in the bottom left, "
            "no connect pins in the bottom right, inputs on the left, outputs on the right, "
            "and try to group other pins together by similar functionality (for example, SPI, I2C, RGMII, etc.). "
            "Always separate groups by 100mil gaps unless there is extra spacing, then space out groups equal distance from each other. "
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


@mcp.tool()
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


@mcp.tool()
async def search_library_symbol(
    ctx: Context, symbol_name: str, library_path: str = ""
) -> str:
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


@mcp.tool()
async def create_schematic_symbol(
    ctx: Context,
    symbol_name: str,
    description: str,
    pins: list,
    part_count: int = 1,
    graphics: list | None = None,
) -> str:
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
    Instructions: pins should be grouped together via function and only placed on
                  the left and right side in 100 mil increments

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
                    Pin orientations: eRotate0 (right), eRotate90 (down), eRotate180 (left), eRotate270 (up)
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


@mcp.tool()
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
                    {"components": h_components, **_disk_source(h_path)}, indent=2, ensure_ascii=False
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
                    {"components": h_components, **_disk_source(h_path)}, indent=2, ensure_ascii=False
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
                        {"components": h_components, **_disk_source(h_path)}, indent=2, ensure_ascii=False
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
        return json.dumps({"error": f"Failed to process schematic data: {e!s}"})


@mcp.tool()
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


@mcp.tool()
async def set_pcb_layer_visibility(
    ctx: Context, layer_names: list, visible: bool
) -> str:
    """
    Set visibility for specified PCB layers

    Args:
        layer_names (list): List of layer names to modify (e.g., ["Top Layer", "Bottom Layer", "Mechanical 1"])
        visible (bool): Whether to show (True) or hide (False) the specified layers

    Returns:
        str: JSON object with the result of the operation
    """
    logger.info(f"Setting layers visibility: {layer_names} to {visible}")

    # Execute the command in Altium to set layer visibility
    response = await altium_bridge.execute_command(
        "set_pcb_layer_visibility", {"layer_names": layer_names, "visible": visible}
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


@mcp.tool()
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
                return json.dumps({**h_data, **_disk_source(h_path)}, indent=2, ensure_ascii=False)
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
                return json.dumps({**h_data, **_disk_source(h_path)}, indent=2, ensure_ascii=False)
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
                    return json.dumps({**h_data, **_disk_source(h_path)}, indent=2, ensure_ascii=False)

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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
SANDBOX_PRJ = SANDBOX_DIR / "Sandbox.PrjScr"
SANDBOX_LOG = EXCHANGE_DIR / "sandbox_log.txt"
SANDBOX_RESULT = EXCHANGE_DIR / "sandbox_result.json"
SANDBOX_BEGIN = "// === BEGIN EXPERIMENT"
SANDBOX_END = "// === END EXPERIMENT"


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


def _dismiss_altium_dialogs():
    """Close Altium modal popups that would otherwise block a script run.

    Altium uses two kinds: Win32 task dialogs (#32770) and Delphi TMessageForm
    error/warning boxes. It also shows "Select Item To Run"
    (TForm_RunScriptParameters) when it cannot find the script project; that
    one stays open and blocks every later script run. Only windows owned by
    the Altium process (X2.EXE) are touched, so dialogs of other applications
    are never closed.

    Returns (handle, class name, title, text) of the windows that were closed,
    so the caller can tell the user (a closed dialog may have been one the
    user had open). text is the message of a Win32 dialog; Altium's own
    TMessageForm draws its message without a window, so its text is ''.
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
        if _window_process_name(hwnd).lower() != "x2.exe":
            return True
        found.append((hwnd, cls.value, title, _dialog_text(hwnd)))
        return True

    user32.EnumWindows(cb, 0)
    for h, *_ in found:
        user32.PostMessageW(h, 0x0010, 0, 0)
    return found


def _dialog_text(hwnd) -> str:
    """Text of a dialog's static controls (the message of a #32770 box)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    parts = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(child, lparam):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(child, cls, 64)
        if cls.value == "Static":
            n = user32.GetWindowTextLengthW(child)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(child, buf, n + 1)
            if buf.value.strip():
                parts.append(buf.value.strip())
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return "\n".join(parts)


def _altium_main_window():
    """Handle of Altium's main frame (largest visible X2.EXE window titled
    "Altium Designer"), or None."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    best = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if "Altium Designer" not in buf.value:
            return True
        if _window_process_name(hwnd).lower() != "x2.exe":
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        best.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
        return True

    user32.EnumWindows(cb, 0)
    return max(best)[1] if best else None


def _blocking_altium_dialogs() -> list[str]:
    """Titles of the modal windows that block Altium, after closing the ones
    _dismiss_altium_dialogs may close; [] when Altium is free.

    Altium disables its main window while a modal window is up. One the MCP
    must not close, such as the .NET "Error" report (Send and Close may end
    Altium), also blocks the command line: X2.EXE started then shows "another
    instance ... is busy" and waits, so nothing may be launched until the user
    has closed it.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []
    user32 = ctypes.windll.user32
    for _ in range(2):
        main_hwnd = _altium_main_window()
        if not main_hwnd or user32.IsWindowEnabled(main_hwnd):
            return []
        if not _dismiss_altium_dialogs():
            break
        time.sleep(1.0)
    else:
        main_hwnd = _altium_main_window()
        if not main_hwnd or user32.IsWindowEnabled(main_hwnd):
            return []
    titles = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lparam):
        if hwnd == main_hwnd or not user32.IsWindowVisible(hwnd):
            return True
        if not user32.IsWindowEnabled(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value and _window_process_name(hwnd).lower() == "x2.exe":
            titles.append(buf.value)
        return True

    user32.EnumWindows(cb, 0)
    return titles or ["(untitled window)"]


def _blocked_ask_user(titles: list[str]) -> str:
    return (
        f"Altium shows a modal window the MCP must not close ({', '.join(titles)}). "
        "Stop retrying and ask the user to read it and close it (for Altium's error "
        "report, choose to continue if offered, not Send and Close), then try again."
    )


def _send_ctrl_f3() -> bool:
    """Press Ctrl+F3 (Run » Stop, stops a paused script) in Altium's main
    window. Keys carry scan codes: Altium ignores virtual-key-only events.
    Returns False when Altium could not be brought to the front, in which case
    nothing is typed (the keys would go to another application)."""
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = _altium_main_window()
    if not hwnd:
        return False
    # A synthetic Alt press lets a background process take the foreground
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        time.sleep(0.1)
        fg = user32.GetForegroundWindow()
        if fg and _window_process_name(fg).lower() == "x2.exe":
            break
    else:
        return False
    for vk, flags in ((0x11, 0), (0x72, 0), (0x72, 2), (0x11, 2)):  # Ctrl, F3
        user32.keybd_event(vk, user32.MapVirtualKeyW(vk, 0), flags, 0)
        time.sleep(0.05)
    return True


async def _script_engine_responds() -> bool:
    """True when Altium runs a trivial sandbox script to its end."""
    result = json.loads(await _run_sandbox("ResultText := 'pong';", 20, recover=False))
    return result.get("success") is True and result.get("result") == "pong"


async def _recover_script_executor() -> dict[str, Any]:
    """Unblock Altium's script engine after a script paused in the debugger
    or a run that never started. Tries EditScript:Stop, then Ctrl+F3 in the
    Altium window, checking after each with a trivial script. The caller holds
    the bridge lock.

    Returns {"recovered": bool, "method": "stop" | "ctrl_f3" | None}; when
    nothing worked, "ask_user" holds what to ask the user to do.
    """
    _dismiss_altium_dialogs()
    blocking = _blocking_altium_dialogs()
    if blocking:
        # EditScript:Stop would wait behind it ("another instance ... busy")
        return {
            "recovered": False,
            "method": None,
            "blocking_dialogs": blocking,
            "ask_user": _blocked_ask_user(blocking),
        }
    exe = altium_bridge.config.altium_exe_path
    subprocess.run(f'"{exe}" -REditScript:Stop', shell=True)
    await asyncio.sleep(2)
    if await _script_engine_responds():
        return {"recovered": True, "method": "stop"}
    if _send_ctrl_f3():
        await asyncio.sleep(2)
        if await _script_engine_responds():
            return {"recovered": True, "method": "ctrl_f3"}
    return {
        "recovered": False,
        "method": None,
        "ask_user": "Altium's script engine is still blocked after EditScript:Stop and "
        "Ctrl+F3. Stop retrying and ask the user to focus the stuck script tab in "
        "Altium and press Ctrl+F3 (Run » Stop), close any open Altium dialog, or "
        "restart Altium; then try again.",
    }


@mcp.tool()
async def run_altium_script(
    ctx: Context, script: str, timeout_seconds: int = 120
) -> str:
    """
    Run a DelphiScript snippet inside an isolated Altium sandbox and report
    what happened, step by step.

    Use this to develop and verify Altium API code before relying on it.
    Altium has no headless test mode and its failure modes are hostile: a
    runtime error leaves the script PAUSED IN THE DEBUGGER with no dialog,
    after which every later script run silently does nothing until the
    debugger is stopped (Ctrl+F3) or Altium is restarted. This tool detects
    that state, reports exactly which statement died, and then unblocks the
    executor itself (EditScript:Stop, then Ctrl+F3 in the Altium window,
    each checked with a trivial script). If both fail, "executor_wedged"
    stays true and "recovery" says what to ask the user: do not retry then.

    The script runs in a SEPARATE script project, so a crash here can never
    break the other MCP tools.

    Writing the script:
    - Call SandboxLog('...') before each risky statement. The log is flushed
      after every call, so the last logged line identifies what failed.
    - Assign findings to the string variable ResultText - it is returned.
    - DelphiScript has NO inline variable declarations. Reuse the provided
      scratch variables: S1..S3 (String), I1..I3 and B1 (Integer),
      Obj1..Obj5 (IDispatch), List1 (TStringList), IntMan, DbDoc.
    - try/except does NOT catch runtime errors such as bad conversions or
      invalid API calls, so it cannot be relied on to keep a script alive.
    - The sandbox is standalone: helpers and constants from the production
      units (TrimJSON, AddJSONProperty, ...) are NOT available; REPLACEALL is.
    - Never register objects into a library document and never write to shared
      or network library paths. Verify the target document kind first
      (ObjectID 32 = schematic, 33 = symbol library).

    For API guidance - interfaces, object models, worked examples - use the
    "altium-script" skill. ensure_altium_script_skill reports whether that
    skill is installed and can install it.

    Args:
        script (str): DelphiScript statements to execute (body only).
        timeout_seconds (int): How long to wait for completion (default 120).

    Returns:
        str: JSON with success, the step log, the script's ResultText, the
             dialogs it closed (titles, and texts where Windows exposes them),
             and on failure the last step reached, whether the executor is
             still wedged after the automatic recovery, and "recovery".
    """
    logger.info(f"run_altium_script: {len(script.splitlines())} lines")

    if not SANDBOX_PAS.exists() or not SANDBOX_PRJ.exists():
        return json.dumps(
            {"success": False, "error": f"sandbox project missing at {SANDBOX_DIR}"}
        )

    async with altium_bridge.exclusive():
        return await _run_sandbox(script, timeout_seconds)


async def _run_sandbox(script: str, timeout_seconds: int, recover: bool = True) -> str:
    # The tracked SandboxScript/ files are a template: each run works on a
    # copy in the exchange folder, so running scripts never dirties the repo.
    work_dir = EXCHANGE_DIR / "sandbox"
    work_dir.mkdir(parents=True, exist_ok=True)
    work_pas = work_dir / SANDBOX_PAS.name
    work_prj = work_dir / SANDBOX_PRJ.name
    try:
        work_prj.write_bytes(SANDBOX_PRJ.read_bytes())
        src = SANDBOX_PAS.read_text(encoding="utf-8")
        pre, rest = src.split(SANDBOX_BEGIN, 1)
        marker_line, rest = rest.split("\n", 1)
        _, post = rest.split(SANDBOX_END, 1)
        body = "\n".join(
            "        " + ln if ln.strip() else ln
            for ln in script.strip("\n").splitlines()
        )
        work_pas.write_text(
            pre
            + SANDBOX_BEGIN
            + marker_line
            + "\n"
            + body
            + "\n        "
            + SANDBOX_END
            + post,
            encoding="utf-8",
        )
    except Exception as e:
        return json.dumps({"success": False, "error": f"could not inject script: {e}"})

    for f in (SANDBOX_LOG, SANDBOX_RESULT):
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass

    # Altium runs outside Claude Desktop's MSIX package and cannot see the
    # virtualized AppData path this process sees (issue #19)
    blocking = _blocking_altium_dialogs()
    if blocking:
        return json.dumps(
            {
                "success": False,
                "error": "Altium is blocked by a modal window; the script did not run",
                "executor_wedged": True,
                "recovery": _blocked_ask_user(blocking),
            }
        )

    sandbox_prj = AltiumBridge._resolve_msix_path(str(work_prj))
    cmd = (
        f'"{altium_bridge.config.altium_exe_path}" -RScriptingSystem:RunScript('
        f'ProjectName="{sandbox_prj}"^|ProcName="Sandbox>Run")'
    )
    subprocess.Popen(cmd, shell=True)

    start = time.time()
    # Keyed by window: a dialog that ignores WM_CLOSE (Yes/No boxes do) is
    # found again on every pass and must be reported once
    dismissed = {}
    error_seen_at = None
    while not SANDBOX_RESULT.exists() and time.time() - start < timeout_seconds:
        await asyncio.sleep(0.5)
        if time.time() - start > 6:
            for h, c, t, text in _dismiss_altium_dialogs():
                dismissed[h] = (c, t, text)
                if t == "Error" and error_seen_at is None:
                    error_seen_at = time.time()
        # Altium's error box means the script died or never started: no
        # result is coming, so stop waiting soon after it
        if error_seen_at and time.time() - error_seen_at > 3:
            break

    dialogs = list(dismissed.values())
    titles = [t for _, t, _ in dialogs]
    texts = [text for _, _, text in dialogs if text]
    steps = []
    if SANDBOX_LOG.exists():
        steps = read_altium_text(SANDBOX_LOG).splitlines()
    report = {"dialogs_dismissed": len(dialogs), "dismissed_dialog_titles": titles}
    if texts:
        report["dismissed_dialog_texts"] = texts

    if SANDBOX_RESULT.exists():
        result_text = read_altium_text(SANDBOX_RESULT).strip()
        return json.dumps(
            {"success": True, "result": result_text, "steps": steps, **report}, indent=2
        )

    if any(c == "TForm_RunScriptParameters" for c, _, _ in dialogs):
        return json.dumps(
            {
                "success": False,
                "error": "Altium could not find the sandbox script project",
                "diagnosis": f"Altium was asked to run {sandbox_prj} and showed 'Select Item To "
                "Run' instead, so it cannot open that path. The script executor is "
                "not wedged.",
                "executor_wedged": False,
                **report,
            },
            indent=2,
        )

    if steps:
        failure = {
            "success": False,
            "error": "script started but did not finish",
            "last_step_reached": steps[-1],
            "diagnosis": "The statement AFTER the last step is what crashed or paused the script.",
            "steps": steps,
        }
    else:
        failure = {
            "success": False,
            "error": "script never started",
            "diagnosis": "Usually a COMPILE error in the script (e.g. an undeclared "
            "variable), or a previously paused script blocking execution.",
        }
    failure.update(report)
    if not recover:
        failure["executor_wedged"] = True
        return json.dumps(failure, indent=2)
    # A paused script blocks every later run: unblock it here rather than
    # leaving the caller to retry against a wedged executor
    recovery = await _recover_script_executor()
    failure["executor_wedged"] = not recovery["recovered"]
    if recovery["recovered"]:
        failure["recovery"] = (
            f"Recovered automatically ({recovery['method']}); the executor is free. "
            "Fix the script before running it again."
        )
    else:
        failure["recovery"] = recovery["ask_user"]
    return json.dumps(failure, indent=2)


@mcp.tool()
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


@mcp.tool()
async def get_footprint_primitives(
    ctx: Context, library_path: str = "", footprint_name: str = ""
) -> str:
    """
    Read the primitives of footprints in a PCB library (.PcbLib).

    Modes:
    - footprint_name omitted: inventory of every footprint with per-type
      primitive counts (pads, tracks, arcs, fills, texts, regions, vias,
      component_bodies)
    - footprint_name given (exact, case-insensitive): full geometry dump -
      pads (position, rotation, layer, sizes/shape per stack, hole size/
      type/width/rotation, plating), tracks, arcs, fills, texts, regions
      (outline vertices). Coordinates in mils; shapes and hole types as raw
      Altium enum ints; layers as names. 3D component bodies are models,
      not 2D primitives, and are excluded.
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
             [{name, description, <type counts>}]}; dump: primitives list
             per footprint
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
    return json.dumps(result, indent=2) if not isinstance(result, str) else result


@mcp.tool()
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
            PAD|name|x|y|rot|layer|plated|hole_size|hole_type|hole_width|hole_rot|top_x|top_y|top_shape[|corner_pct[|mode|mid_x|mid_y|mid_shape|bot_x|bot_y|bot_shape]]
            TRACK|x1|y1|x2|y2|width|layer
            ARC|cx|cy|radius|start_angle|end_angle|width|layer
            FILL|x1|y1|x2|y2|rotation|layer
            TEXT|x|y|size|width|rotation|layer|mirror|ttf|text
            REGION|layer|kind|x1|y1|x2|y2|...

    Returns:
        str: JSON with created count, primitive_errors, failed names
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


@mcp.tool()
async def build_schematic(
    ctx: Context,
    parts: list,
    wires: list | None = None,
    junctions: list | None = None,
    net_labels: list | None = None,
    power_ports: list | None = None,
    notes: list | None = None,
) -> str:
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

    Returns:
        JSON with the created sheet name, counts, and the pin map - every
        placed pin's true connection point, for routing a follow-up call.
    """
    logger.info(f"Building schematic: {len(parts)} parts")

    lines = []
    for p in parts:
        for key in ("designator", "symbol_library", "symbol", "x", "y"):
            if key not in p:
                return json.dumps(
                    {
                        "success": False,
                        "error": f"part missing required key '{key}': {p}",
                    }
                )
        lines.append(
            "PART|{}|{}|{}|{}|{}|{}|{}|{}".format(
                p["designator"],
                p["symbol_library"],
                p["symbol"],
                p.get("design_item_id", ""),
                int(p["x"]),
                int(p["y"]),
                int(p.get("orientation", 0)),
                1 if p.get("mirror") else 0,
            )
        )
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
            return json.dumps(
                {
                    "success": False,
                    "error": f"wire needs an even count of >=4 coords: {route}",
                }
            )
        lines.append("WIRE|" + "|".join(str(int(v)) for v in route))
    for j in junctions or []:
        lines.append(f"JUNCTION|{int(j['x'])}|{int(j['y'])}")
    for n in net_labels or []:
        lines.append(
            "NETLABEL|{}|{}|{}|{}".format(
                int(n["x"]), int(n["y"]), int(n.get("orientation", 0)), n["text"]
            )
        )
    for pw in power_ports or []:
        lines.append(
            "POWER|{}|{}|{}|{}|{}|{}".format(
                int(pw["x"]),
                int(pw["y"]),
                int(pw.get("orientation", 3)),
                int(pw.get("style", 5)),
                pw["text"],
                1 if pw.get("show_net_name") else 0,
            )
        )
    for nt in notes or []:
        lines.append(f"NOTE|{int(nt['x'])}|{int(nt['y'])}|{nt['text']}")

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
                pin_map.setdefault(f[1], {})[f[2]] = [int(f[3]), int(f[4])]
    result["pin_map"] = pin_map
    result["pin_map_note"] = (
        "Absolute electrical connection points. Route "
        "wires from these, not from predicted offsets."
    )
    return json.dumps(result, indent=2)


HARNESS_SPEC_FILE = EXCHANGE_DIR / "harness_wiring_spec.txt"

# TSheetStyle ordinals accepted by ISch_Document.SheetStyle
SHEET_STYLES = {
    "A4": 0, "A3": 1, "A2": 2, "A1": 3, "A0": 4,
    "A": 5, "B": 6, "C": 7, "D": 8, "E": 9,
    "LETTER": 10, "LEGAL": 11, "TABLOID": 12,
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


def _cavity_type(value: Any) -> int | None:
    if isinstance(value, int) and value in CAVITY_TYPE_NAMES:
        return value
    return CAVITY_TYPES.get(str(value if value is not None else "crimp").strip().lower())


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
    from harness_router import label_positions

    des_pos = item.get("designator_pos")
    com_pos = item.get("comment_pos")
    if not des_pos or not com_pos:
        auto_des, auto_com = label_positions(
            pts, str(item.get("label_position", "above")).lower()
        )
        des_pos = des_pos or auto_des
        com_pos = com_pos or auto_com
    return [int(v) for v in des_pos], [int(v) for v in com_pos]


@mcp.tool()
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
) -> str:
    """
    Add or update wires, splices, no-connects, text, cavity parts, twists, shields, cables, wire breaks and parameters on a Harness Wiring Diagram (.WirDoc) or Schematic.

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
                    "net": "BMSS_C01_POS",           # written as the wire's Comment
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
                {"x": 16700, "y": 8400, "orientation": 0, "text": "BMSS_C01_POS"}
            ]
        net_labels (list): Deprecated alias of labels.
        wire_updates (list): Parameters to upsert on existing wires, e.g.:
            [{"designator": "W1", "parameters": {"Gauge": "22 AWG"}, "tcolor": 0}]
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
            [{"x1": 13300, "y1": 8000, "x2": 13400, "y2": 8600, "designator": "TW1"}].
            Altium links every wire the rectangle crosses or touches. Sending the rectangle of
            an existing twist (or shield) with a designator only re-annotates it.
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
            Placing an identical rectangle or a break at the same point again updates it.
        relink (bool): Reopen the document after saving to recompute those links even when
            no geometry changed (e.g. twists copied in the GUI keep the copied links).
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
        component_updates (list): Parameters to upsert on placed connectors/components, e.g.:
            [{"designator": "P1", "parameters": {"Max Operating Temperature": "105°C"}}]
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
        return json.dumps(
            {"success": False, "error": "document_path is required"}
        )

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
            )
        )

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
        if wu.get("tcolor") is not None:
            lines.append(_spec_line("WIRECOLOR", w_des, int(wu["tcolor"])))

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
            _spec_line("TWIST", *_rect(t), _rotation(t), str(t.get("designator", "")).strip())
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
                "SHIELD", *_rect(sh), _rotation(sh), style, str(sh.get("designator", "")).strip()
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
        lines.append(_spec_line("WIREBREAK", int(wb.get("x", 0)), int(wb.get("y", 0))))

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
        _check_harness_links(result, document_path, twists, shields, cables, wire_breaks)
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


@mcp.tool()
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
    and edited in place, so their links to the wiring diagram survive. Coordinates are true
    mils (get_harness_data reports DXP units: x 10), lengths mm, orientations degrees
    (0, 90, 180, 270). Omitted fields keep their value.

    Altium connects bundle ends to the connection points under them when it loads a drawing,
    so after a geometry change the tool saves, closes, reopens and saves the drawing again
    ("reloaded": true; the undo history is lost), then reads it back: "bundles" lists each
    bundle the call drew or moved with its ends and lengths, "problems" what did not connect
    or lost a connection (success is false then), "unrouted_wires" the wires with no bundle
    path between the connection points of their two ends ("newly_unrouted_wires": those
    this call left without one).

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
            the picture only after loading the project from disk, so that closes and reopens
            the whole harness project (its other tabs close too) and updates the connector
            from its library (as changing its part does: database parameters are refreshed,
            its pins in the layout get new UniqueIDs, wires stay linked). Refused, with
            nothing rendered, while a document of that project has unsaved changes. A new
            view's side and zoom are Altium's defaults (change them in Altium).
        connection_points (list): New or existing connection points, e.g.
            [{"designator": "CP1", "x": 12000, "y": 12000, "style": "circle",
              "show_name": True, "orientation": 0, "objects": ["SP1"],
              "designator_pos": [12200, 12400]}].
            style is "circle", "square" or "insulator". "objects" are connectors or splices
            to hold here (taken from the connection point holding them); "release" removes
            held ones; "new_designator" renames. A wire is routed through the bundles between
            the connection points holding its two ends, so every splice needs one. Moving a
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
              "source_library": "evt-components.DbLib", "thickness": 3,
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

    Not supported (Altium's script interface has no setter): wire length offsets
    ("Offset Length", calculated type), user-defined wire lengths and manual wire paths.
    get_harness_data reports all of them once set in Altium (length_type, length_offset,
    via_bundles, length).

    Returns:
        str: JSON with success, saved, reloaded, counts, not_found, warnings, bundles,
             problems, unrouted_wires and newly_unrouted_wires.
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
    if not spec["records"]:
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

    try:
        async with altium_bridge.exclusive():
            HARNESS_LAYOUT_SPEC_FILE.parent.mkdir(parents=True, exist_ok=True)
            write_altium_text(HARNESS_LAYOUT_SPEC_FILE, "\n".join(lines) + "\n")
            response = await altium_bridge._execute_command_locked(
                "edit_harness_layout", {}
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
            if problems:
                result["problems"] = problems
                result["success"] = False
    return json.dumps(result, indent=2)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def reroute_harness_wiring(
    ctx: Context,
    document_path: str,
    trunk_x_start: int | None = None,
    channel_spacing_mils: int = 300,
    dry_run: bool = False,
) -> str:
    """Re-route every wire of an Altium Harness Wiring Diagram (.WirDoc) orthogonally, in place.

    Use after connectors moved and wires rubber-banded. Endpoints stay on their pins and
    splices; only the path between them and the label positions change. Wires are moved in
    place, so their UniqueIDs, parameters, colours, library links and layout-drawing (.LdrDoc)
    links survive. Text, rectangles, splices and no-connects are not touched.

    Routing rules (see harness_router.py): straight wires stay straight; vertical drops live
    in per-connector-pair corridors near the destination side; 300 mil channel pitch; UP and
    DOWN wires nested as concentric staircases; channels shared only when that adds no
    crossing; channels and labels keep clear of pin names and splices; labels side by side on
    the longest horizontal run, staggered by 900 mils on tight parallel runs.

    The routes are computed from the SAVED file: save the document in Altium first.

    Args:
        document_path: Full path to target .WirDoc file. Required.
        trunk_x_start: Optional X (mils) of the first channel of every corridor. By default each
            corridor starts next to its destination side.
        channel_spacing_mils: Distance between vertical channels (default 300 mils).
        dry_run: Only compute and report the routes, change nothing.

    Returns:
        JSON with success, summary (corridors, channels, crossings, ...), warnings
        (pin-name or splice clearance problems, wires that could not be rerouted) and
        Altium's report.
    """
    import harness_router

    if not document_path or Path(document_path).suffix.lower() != ".wirdoc":
        return json.dumps(
            {"success": False, "error": "document_path to a .WirDoc is required"}
        )

    try:
        routing = harness_router.calculate_harness_reroute(
            document_path=document_path,
            trunk_x_start=trunk_x_start,
            channel_spacing_mils=channel_spacing_mils,
        )
    except Exception as e:
        logger.error(f"Error calculating harness reroute: {e}")
        return json.dumps(
            {"success": False, "error": f"Failed to calculate reroute: {e!s}"}
        )

    if dry_run:
        return json.dumps({"success": True, "dry_run": True, **routing}, indent=2)

    res_str = await add_harness_wiring(
        ctx, document_path=document_path, reroutes=routing["reroutes"]
    )
    try:
        res_json = json.loads(res_str)
    except ValueError:
        res_json = {"success": False, "raw": res_str}

    return json.dumps(
        {
            "success": bool(res_json.get("success")),
            "document_path": str(Path(document_path).resolve()),
            "summary": routing["summary"],
            "warnings": routing["warnings"],
            "altium_response": res_json,
        },
        indent=2,
    )


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
                    {"designator": des, "reason": f"PartNumber {part_num!r} not in database"}
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
        current part of the same number carries beyond the database row
        (e.g. a strip length typed in Altium) are kept: replacing the part
        would otherwise drop them."""
        kept = {
            p["part_number"]: p.get("parameters", {}) for p in current or [] if p.get("part_number")
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
            for name, value in kept.get(pn, {}).items():
                if name not in params and name != "Description" and value:
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
                            {"designator": f"{conn}.{pin}", "reason": f"unknown cavity slot {slot!r}"}
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
                            {"connector": key[0], "pin": key[1], "type": cav_type, "part_number": ""}
                        )
                        continue
                    current = [
                        p
                        for p in pin.get("cavity_parts", [])
                        if _cavity_type(p.get("type", "crimp")) == cav_type
                    ]
                    for part in library_parts(where, pns, current) or []:
                        cavity_parts.append(
                            {"connector": key[0], "pin": key[1], "type": cav_type, **part}
                        )
        for conn, pin in wanted:
            unresolved.append(
                {"designator": f"{conn}.{pin}", "reason": "pin not found on the sheet"}
            )

    associated_parts = []
    if sync_associated:
        wanted_assoc = {str(k): [str(p) for p in v if p] for k, v in (associated_map or {}).items()}
        owners = [*doc_data.get("connectors", []), *doc_data.get("splices", [])]
        for owner in owners:
            des = owner["designator"]
            current = [p["part_number"] for p in owner.get("associated_parts", []) if p["part_number"]]
            pns = wanted_assoc.pop(des, current)
            if not pns:
                if current:  # emptied by associated_map
                    associated_parts.append({"owner": des, "part_number": ""})
                continue
            for part in library_parts(des, pns, owner.get("associated_parts")) or []:
                associated_parts.append({"owner": des, **part})
        for des in wanted_assoc:
            unresolved.append({"designator": des, "reason": "connector or splice not found on the sheet"})

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
        for c in doc_data.get("connectors", []):
            current = c.get("parameters", {})
            part_num = current.get("PartNumber")
            if not part_num:
                continue
            found = library.find(part_num)
            if found is None:
                unresolved.append(
                    {"designator": c["designator"], "reason": f"PartNumber {part_num!r} not in database"}
                )
                continue
            table, row = found
            changed = {
                name: value
                for name, value in table.parameters(row).items()
                if name in current and current[name] != value
            }
            if changed:
                component_updates.append({"designator": c["designator"], "parameters": changed})

    if wire_updates or cavity_parts or component_updates or associated_parts or cable_updates:
        res_str = await add_harness_wiring(
            ctx,
            document_path=target_path,
            wire_updates=wire_updates,
            cavity_parts=cavity_parts,
            component_updates=component_updates,
            associated_parts=associated_parts,
            cables=cable_updates,
        )
        try:
            res_json = json.loads(res_str)
        except ValueError:
            res_json = {"success": False, "raw": res_str}
    else:
        res_json = {"success": True, "note": "nothing to update"}

    return {
        "success": bool(res_json.get("success")) and not unresolved,
        "document_path": target_path,
        "source_library": src_lib,
        "wires_synced_count": res_json.get("wires_updated", len(wire_updates)),
        "cavities_synced_count": res_json.get(
            "crimps", sum(1 for p in cavity_parts if p["type"] == 0 and p["part_number"])
        ),
        "cavity_parts_synced_count": res_json.get(
            "cavity_parts", sum(1 for p in cavity_parts if p["part_number"])
        ),
        "associated_parts_synced_count": res_json.get(
            "associated_parts", sum(1 for p in associated_parts if p["part_number"])
        ),
        "cables_synced_count": res_json.get("cables", len(cable_updates)),
        "connectors_refreshed": {u["designator"]: sorted(u["parameters"]) for u in component_updates},
        "unresolved": unresolved,
        "warnings": warnings,
        "altium_response": res_json,
    }


@mcp.tool()
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

    Returns:
        str: JSON with counts, unresolved items, warnings and Altium's report.
    """
    if not document_path or not Path(document_path).exists():
        return json.dumps(
            {"success": False, "error": f"document_path to an existing .WirDoc is required: {document_path!r}"}
        )
    lib_path = Path(dblib_path) if dblib_path else find_dblib(Path(document_path).resolve())
    if not lib_path or not lib_path.exists():
        return json.dumps(
            {"success": False, "error": "No .DbLib found; pass dblib_path or set ALTIUM_DBLIB_PATH"}
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
    )
    result["dblib_path"] = str(lib_path)
    return json.dumps(result, indent=2)


@mcp.tool()
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
                return json.dumps({"success": False, "error": "Wire database not found. Please provide wires_db_path."})
            library = load_dblib(lib_path)
            db_label = str(lib_path)
    except (OSError, ValueError) as e:
        return json.dumps({"success": False, "error": f"Cannot read wire database: {e}"})

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


@mcp.tool()
async def create_symbols_batch(ctx: Context, spec_file: str) -> str:
    """
    Create many schematic symbols in a single Altium script run.

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
            Each SYMBOL line starts a new symbol; PIN/GRAPHIC lines belong
            to the most recent SYMBOL.

    Returns:
        str: JSON object with created count and a failed name list
    """
    logger.info(f"Creating symbols batch from {spec_file}")

    response = await altium_bridge.execute_command(
        "create_symbols_batch", {"spec_file": spec_file}
    )

    if not response.get("success", False):
        error_msg = response.get("error", "Unknown error")
        logger.error(f"Error in batch symbol creation: {error_msg}")
        return json.dumps(
            {"success": False, "error": f"Failed batch creation: {error_msg}"}
        )

    result = response.get("result", {})
    return json.dumps(result, indent=2) if not isinstance(result, str) else result


@mcp.tool()
async def get_symbol_primitives(
    ctx: Context, library_path: str = "", symbol_name: str = ""
) -> str:
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
             primitives: [...]}
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


@mcp.tool()
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


@mcp.tool()
async def create_net_class(ctx: Context, class_name: str, net_names: list) -> str:
    """
    Create a new net class and add specified nets to it

    Args:
        class_name (str): Name of the net class to create or modify
        net_names (list): List of net names to add to the class

    Returns:
        str: JSON object with the result of the operation
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


@mcp.tool()
async def set_component_position(
    ctx: Context, cmp_designator: str, x: float, y: float, rotation: float = -1
) -> str:
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


@mcp.tool()
async def move_components(
    ctx: Context,
    cmp_designators: list,
    x_offset: float,
    y_offset: float,
    rotation: float = 0,
) -> str:
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


@mcp.tool()
async def place_components(ctx: Context, placements: list) -> str:
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

        try:
            entry = f"{designator}|{float(x)}|{float(y)}|{float(rotation)}|{layer}"
        except (TypeError, ValueError):
            errors.append(f"placements[{idx}] x, y, and rotation must be numbers")
            continue
        entries.append(entry)

    if errors:
        return json.dumps({"success": False, "error": "; ".join(errors)})
    if not entries:
        return json.dumps({"success": False, "error": "No placements provided"})

    response = await altium_bridge.execute_command(
        "place_components", {"placements": entries}
    )

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


@mcp.tool()
async def get_net_connections(
    ctx: Context, cmp_designators: list | None = None, max_pads_per_net: int = 40
) -> str:
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


@mcp.tool()
async def check_orientation(
    ctx: Context, cmp_designators: list | None = None, min_improvement_mils: float = 25
) -> str:
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


@mcp.tool()
async def check_placement(
    ctx: Context, cmp_designators: list | None = None, clearance_mils: float = 6
) -> str:
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


@mcp.tool()
async def get_screenshot(
    ctx: Context,
    view_type: str = "pcb",
    zoom_to: list | None = None,
    document_path: str | None = None,
):
    """
    Take a screenshot of the Altium window, returned as viewable image content.

    Args:
        view_type (str): Type of view to capture - 'pcb' or 'sch'
        zoom_to (list, optional): List of component designators (e.g. ["U12", "R42"]).
            PCB view only: Altium zooms to the bounding box of these components
            (plus a margin) before the capture, so the components fill the frame.
            Omit to capture at the current zoom level.
        document_path (str, optional): Absolute or relative path to a schematic or
            harness document to focus and capture.

    Returns:
        Image content of the captured window plus a JSON metadata text block
        (window title, size, zoomed component count)
    """
    logger.info(
        f"Taking screenshot of Altium {view_type} window (zoom_to={zoom_to}, doc={document_path})"
    )

    try:
        # First, execute the Altium command to ensure the right document type
        # is focused, optionally zooming to the requested components
        params = {"view_type": view_type.lower()}
        if zoom_to:
            params["designators"] = zoom_to
        if document_path:
            params["document_path"] = str(Path(document_path).resolve())
        response = await altium_bridge.execute_command("take_view_screenshot", params)

        focused_kind = ""
        focused_document = ""
        # Check for success
        if not response.get("success", False):
            error_msg = response.get("error", "Unknown error")
            logger.warning(
                f"Could not focus {view_type} document via API ({error_msg}); "
                "capturing currently active Altium window"
            )
        else:
            # Altium reports which document ended up focused. If it is not the
            # kind that was asked for (typically: no document of that kind is
            # open), say so instead of returning the wrong editor labelled as
            # the requested view.
            focus_info = response.get("result", {})
            focus_info = focus_info if isinstance(focus_info, dict) else {}
            focused_kind = str(focus_info.get("focused_kind", "") or "").upper()
            focused_document = focus_info.get("focused_document", "")
            wanted_kind = {"pcb": "PCB", "sch": "SCH"}.get(view_type.lower())
            is_sch_match = wanted_kind == "SCH" and focused_kind in (
                "SCH",
                "HARNESSWIRINGDIAGRAM",
                "HARNESSLAYOUTDRAWING",
            )
            is_pcb_match = wanted_kind == "PCB" and focused_kind in ("PCB", "PCBLIB")
            if wanted_kind and focused_kind and not (is_sch_match or is_pcb_match):
                logger.error(
                    f"Requested {view_type} view but {focused_kind} document is focused"
                )
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Requested a '{view_type}' view but Altium has a "
                            f"{focused_kind} document focused ({focused_document}). Is a "
                            f"{wanted_kind} document open in the active project?"
                        ),
                        "requested_view_type": view_type,
                        "focused_kind": focused_kind,
                        "focused_document": focused_document,
                    }
                )

        # Run the screenshot capture in a separate thread
        import queue
        import threading

        from PIL import Image

        result_queue = queue.Queue()

        def capture_screenshot_thread():
            try:
                try:
                    import win32service

                    hw = win32service.OpenWindowStation(
                        "WinSta0", True, win32con.GENERIC_ALL
                    )
                    hw.SetProcessWindowStation()
                    hd = win32service.OpenDesktop(
                        "Default", 0, True, win32con.GENERIC_ALL
                    )
                    hd.SetThreadDesktop()
                except Exception:
                    pass

                # Find Altium windows
                altium_windows = []
                altium_fallback_windows = []

                def collect_altium_windows(hwnd, _):
                    if win32gui.IsWindowVisible(hwnd):
                        title = win32gui.GetWindowText(hwnd)

                        # First, look for main Altium design project windows (.PrjPcb, .PrjHar, .PrjMbd, or Designer)
                        is_main = (
                            any(
                                ext in title
                                for ext in (".PrjPcb", ".PrjHar", ".PrjMbd")
                            )
                            or "Altium Designer" in title
                        )
                        if "Altium" in title and is_main:
                            altium_windows.append(
                                {
                                    "handle": hwnd,
                                    "title": title,
                                    "class_name": win32gui.GetClassName(hwnd),
                                    "rect": win32gui.GetWindowRect(hwnd),
                                }
                            )
                        # Collect any window with Altium in the title as fallback
                        elif "Altium" in title:
                            altium_fallback_windows.append(
                                {
                                    "handle": hwnd,
                                    "title": title,
                                    "class_name": win32gui.GetClassName(hwnd),
                                    "rect": win32gui.GetWindowRect(hwnd),
                                }
                            )
                    return True

                win32gui.EnumWindows(collect_altium_windows, 0)

                # If no specific Altium design project windows found, use the fallback
                pool = altium_windows or altium_fallback_windows

                if not pool:
                    result_queue.put(
                        {
                            "success": False,
                            "error": f"No Altium windows found for {view_type} view",
                        }
                    )
                    return

                # Altium owns several top-level windows with its name in the
                # title, and while it switches documents a small transient one
                # can come first. Take the largest window that is not
                # minimized: that is the main frame.
                def safe_is_iconic(h):
                    try:
                        return win32gui.IsWindow(h) and win32gui.IsIconic(h)
                    except Exception:
                        return True

                def safe_get_rect(h):
                    try:
                        if win32gui.IsWindow(h):
                            return win32gui.GetWindowRect(h)
                    except Exception:
                        pass
                    return (0, 0, 0, 0)

                def area(w):
                    l, t, r, b = safe_get_rect(w["handle"])
                    return max(0, r - l) * max(0, b - t)

                candidates = [
                    w
                    for w in pool
                    if win32gui.IsWindow(w["handle"])
                    and not safe_is_iconic(w["handle"])
                ]
                if not candidates:
                    candidates = [w for w in pool if win32gui.IsWindow(w["handle"])]
                if not candidates:
                    result_queue.put(
                        {
                            "success": False,
                            "error": f"No valid Altium windows found for {view_type} view",
                        }
                    )
                    return
                window = max(candidates, key=area)
                hwnd = window["handle"]

                # Bring Altium to the front and let it paint. Altium only
                # renders a schematic view once it has actually been shown on
                # screen, so a capture taken while it sits behind another
                # window comes back with a blank canvas. Windows may refuse
                # SetForegroundWindow from a process that neither is nor was
                # started by the foreground process; a synthetic Alt press
                # before the call is the standard unlock and is harmless.
                activated = False
                altium_pid = win32process.GetWindowThreadProcessId(hwnd)[1]

                def altium_is_foreground():
                    # Altium owns several top-level windows; any of them
                    # being foreground means Altium is in front.
                    fg = win32gui.GetForegroundWindow()
                    return (
                        bool(fg)
                        and win32process.GetWindowThreadProcessId(fg)[1] == altium_pid
                    )

                try:
                    import ctypes

                    fg_hwnd = win32gui.GetForegroundWindow()
                    if fg_hwnd:
                        fg_tid = win32process.GetWindowThreadProcessId(fg_hwnd)[0]
                        cur_tid = win32api.GetCurrentThreadId()
                        if fg_tid != cur_tid:
                            try:
                                win32process.AttachThreadInput(fg_tid, cur_tid, True)
                            except Exception:
                                pass
                    ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOWMAXIMIZED)
                    ctypes.windll.user32.SwitchToThisWindow(hwnd, True)
                    try:
                        win32gui.SetForegroundWindow(hwnd)
                        win32gui.BringWindowToTop(hwnd)
                    except Exception:
                        pass
                    deadline = time.time() + 2.0
                    while time.time() < deadline:
                        time.sleep(0.1)
                        if altium_is_foreground():
                            activated = True
                            break
                    if fg_hwnd and fg_tid != cur_tid:
                        try:
                            win32process.AttachThreadInput(fg_tid, cur_tid, False)
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f"Could not bring window to foreground: {e}")
                if not activated:
                    logger.warning(
                        "Altium is not the foreground window; the capture may show an unpainted view"
                    )
                # A freshly switched-to document needs a moment to render.
                time.sleep(1.0)

                # Measure AFTER activation: restoring or switching can change
                # the frame's size.
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                width = right - left
                height = bottom - top
                if width < 200 or height < 150:
                    result_queue.put(
                        {
                            "success": False,
                            "error": f"Altium window is too small to capture ({width}x{height}); is it minimized?",
                        }
                    )
                    return

                # Take screenshot using GDI functions instead of ImageGrab
                try:
                    # Get device context
                    hwndDC = win32gui.GetWindowDC(hwnd)
                    mfcDC = win32ui.CreateDCFromHandle(hwndDC)
                    saveDC = mfcDC.CreateCompatibleDC()

                    # Create a bitmap object
                    saveBitMap = win32ui.CreateBitmap()
                    saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)
                    saveDC.SelectObject(saveBitMap)

                    # Copy the screen into the bitmap using PrintWindow (works even in background)
                    pw_success = False
                    try:
                        import ctypes

                        pw_res = ctypes.windll.user32.PrintWindow(
                            hwnd, saveDC.GetSafeHdc(), 2
                        )
                        pw_success = bool(pw_res)
                    except Exception:
                        pass
                    if not pw_success:
                        saveDC.BitBlt(
                            (0, 0), (width, height), mfcDC, (0, 0), win32con.SRCCOPY
                        )

                    # Convert the bitmap to an Image
                    bmpinfo = saveBitMap.GetInfo()
                    bmpstr = saveBitMap.GetBitmapBits(True)
                    img = Image.frombuffer(
                        "RGB",
                        (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
                        bmpstr,
                        "raw",
                        "BGRX",
                        0,
                        1,
                    )

                    # Save a local copy of the screenshot for debugging (non-fatal if it fails)
                    try:
                        debug_filename = str(_data_dir() / f"screenshot_{view_type}.png")
                        img.save(debug_filename)
                        logger.info(f"Saved debug screenshot to {debug_filename}")
                    except Exception as save_error:
                        logger.warning(
                            f"Could not save debug screenshot to {debug_filename}: {save_error}"
                        )
                        debug_filename = None  # Clear it since save failed

                    # Clean up GDI resources
                    win32gui.DeleteObject(saveBitMap.GetHandle())
                    saveDC.DeleteDC()
                    mfcDC.DeleteDC()
                    win32gui.ReleaseDC(hwnd, hwndDC)

                    # Convert to base64
                    buffer = io.BytesIO()
                    img.save(buffer, format="PNG")
                    buffer.seek(0)
                    img_base64 = base64.b64encode(buffer.read()).decode("utf-8")

                    # Put result in queue
                    result_queue.put(
                        {
                            "success": True,
                            "width": width,
                            "height": height,
                            "window_title": window["title"],
                            "window_class": window["class_name"],
                            "view_type": view_type,
                            "foreground_confirmed": activated,
                            "image_format": "PNG",
                            "encoding": "base64",
                            "debug_file": debug_filename,
                            "image_data": img_base64,
                        }
                    )

                except Exception as e:
                    import traceback

                    trace = traceback.format_exc()
                    logger.error(f"GDI screenshot error: {e}\n{trace}")
                    result_queue.put(
                        {
                            "success": False,
                            "error": f"GDI screenshot failed: {e!s}",
                            "traceback": trace,
                        }
                    )

            except Exception as e:
                import traceback

                result_queue.put(
                    {
                        "success": False,
                        "error": f"Screenshot thread error: {e!s}",
                        "traceback": traceback.format_exc(),
                    }
                )

        # Start the thread
        thread = threading.Thread(target=capture_screenshot_thread)
        thread.daemon = True
        thread.start()

        # Wait for the thread to complete
        thread.join(timeout=10)  # 10 second timeout

        if thread.is_alive():
            logger.error("Screenshot thread timed out")
            return json.dumps(
                {"success": False, "error": "Screenshot operation timed out"}
            )

        # Get the result from the queue
        if result_queue.empty():
            logger.error("Screenshot thread did not return a result")
            return json.dumps(
                {"success": False, "error": "Screenshot thread did not return a result"}
            )

        result = result_queue.get()

        if not result.get("success", False):
            error_msg = result.get("error", "Unknown error")
            logger.error(f"Screenshot error: {error_msg}")
            return json.dumps({"success": False, "error": error_msg})

        logger.info(
            f"Screenshot taken successfully, size: {result['width']}x{result['height']}"
        )

        # Return the PNG as a proper MCP image content block instead of inline
        # base64 text: raw base64 in the text result exceeds client token
        # limits (a full-window capture is ~300 KB), while image blocks are
        # rendered natively by clients
        image_base64 = result.pop("image_data")
        result.pop("encoding", None)
        zoom_info = response.get("result", {})
        if isinstance(zoom_info, dict) and "zoomed_component_count" in zoom_info:
            result["zoomed_component_count"] = zoom_info["zoomed_component_count"]
        # What was actually captured, as opposed to what was requested
        result["captured_kind"] = focused_kind or None
        result["captured_document"] = focused_document or None
        return [
            json.dumps(result),
            MCPImage(data=base64.b64decode(image_base64), format="png"),
        ]

    except Exception as e:
        logger.error(f"Error in screenshot function: {e!s}")
        return json.dumps(
            {"success": False, "error": f"Failed to take screenshot: {e!s}"}
        )


@mcp.tool()
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


@mcp.tool()
async def layout_duplicator_apply(
    ctx: Context, source_designators: list, destination_designators: list
) -> str:
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


@mcp.tool()
async def get_pcb_rules(ctx: Context) -> str:
    """
    Get all design rules from the current Altium PCB

    Returns:
        str: JSON array of PCB design rules with their properties
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


@mcp.tool()
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


@mcp.tool()
async def get_output_job_containers(ctx: Context) -> str:
    """
    Get all available output job containers from a specified OutJob file

    Args:
        outjob_path (str): Path to the OutJob file (optional, will use first open OutJob if not provided)

    Returns:
        str: JSON array with all output job containers and their properties
    """
    logger.info("Getting output job containers from the first open OutJob")

    # Execute the command in Altium to get output job containers
    response = await altium_bridge.execute_command(
        "get_output_job_containers",
        {},  # No parameters needed - will use first open OutJob
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


@mcp.tool()
async def get_outjob_settings(ctx: Context, outjob_path: str) -> str:
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

    Returns:
        str: JSON with path, group_name, variant, containers (name, type,
             output paths, file name expression, add_to_project) and outputs
             (type, name, document_path, variant, connected_containers and a
             decoded summary for Gerber, NC Drill, test points, pick and
             place and print outputs, plus raw_settings)
    """
    try:
        data = parse_outjob(outjob_path)
    except FileNotFoundError:
        return json.dumps({"error": f"OutJob not found: {outjob_path}"})
    except Exception as e:
        return json.dumps({"error": f"Could not parse OutJob: {e}"})
    return json.dumps(data, indent=2, ensure_ascii=False)


@mcp.tool()
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
    no_connects. Edit it with edit_harness_layout.

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
             connected_pins, net_label_count), connectors (designator,
             lib_reference, description, location, unique_id, associated_parts, pins
             with nets and cavity_parts), wires, splices, twists, shields, cables,
             wire_breaks, no_connects, net_labels and source.
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
                data["wires"] = [w for w in data["wires"] if w["designator"] in touching]
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


@mcp.tool()
async def get_project_info(ctx: Context) -> str:
    """
    Get the focused Altium project: parameters, documents and variants.

    Read-only. Use it to check that nothing has unsaved changes before
    generating outputs, and to read project parameters and variants.

    Returns:
        str: JSON with project_path, parameters [{name, value}],
             schematic_sheet_count, modified_document_count, documents
             [{path, kind, state}] where state is "closed", "open" or
             "modified" (unsaved changes in memory), and variants
             [{name, variations [{designator, kind}]}] where kind is
             "not_fitted" or "alternate"
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


@mcp.tool()
async def get_project_components(ctx: Context, include_parameters: bool = False) -> str:
    """
    List every component of the compiled project (schematic or harness side).

    Compiles the focused project in memory first (nothing is saved). If the focused
    project is an Altium Harness project (.PrjHar), it automatically parses the wiring
    diagrams (.WirDoc) directly.

    Args:
        include_parameters (bool): Also return every component parameter
            (e.g. Manufacturer_Part_Number). Default False - the output is
            much larger with parameters.

    Returns:
        str: JSON with component_count and components [{designator, comment,
             footprint, library_reference, sheet, parameters?, pins?}]
    """
    response = await altium_bridge.execute_command(
        "get_project_components", {"include_parameters": include_parameters}
    )
    if response.get("success", False):
        result = response.get("result", {})
        return (
            result
            if isinstance(result, str)
            else json.dumps(result, indent=2, ensure_ascii=False)
        )

    # Fallback for Harness projects (.PrjHar / .WirDoc)
    try:
        h_path = await _get_active_harness_path()
        if h_path:
            components = get_harness_components(
                h_path, include_parameters=include_parameters
            )
            return json.dumps(
                {
                    "component_count": len(components),
                    "components": components,
                    **_disk_source(h_path),
                },
                indent=2,
                ensure_ascii=False,
            )
    except Exception as e:
        logger.warning(f"Harness fallback failed: {e}")

    error_msg = response.get("error", "Unknown error")
    logger.error(f"Error getting project components: {error_msg}")
    return json.dumps({"error": f"Failed to get project components: {error_msg}"})


@mcp.tool()
async def get_erc_violations(ctx: Context, compile: bool = True) -> str:
    """
    Compile the focused project and list its ERC/compiler violations.

    Compiling works in memory and does not save any document (Altium may
    refresh the derived .PrjPcbStructure file). For harness projects (.PrjHar),
    performs native rule-checking (unconnected pins, single pin nets, unannotated
    connectors).

    Args:
        compile (bool): Compile before reading (default True). False returns
            the violations of the last compilation.

    Returns:
        str: JSON with total, fatal, errors, warnings and violations
             [{level, type, detail}]
    """
    response = await altium_bridge.execute_command(
        "get_erc_violations", {"compile": compile}
    )
    if response.get("success", False):
        result = response.get("result", {})
        return (
            result
            if isinstance(result, str)
            else json.dumps(result, indent=2, ensure_ascii=False)
        )

    # Fallback for harness projects where compilation returns no flattened document
    try:
        h_path = await _get_active_harness_path()
        if h_path:
            violations = get_harness_erc_violations(h_path)
            return json.dumps({**violations, **_disk_source(h_path)}, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"ERC harness fallback failed: {e}")

    error_msg = response.get("error", "Unknown error")
    logger.error(f"Error getting ERC violations: {error_msg}")
    return json.dumps({"error": f"Failed to get ERC violations: {error_msg}"})


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def run_output_jobs(ctx: Context, container_names: list) -> str:
    """
    Run specified output job containers

    Args:
        container_names (list): List of container names to run

    Returns:
        str: JSON object with results of running the output jobs
    """
    logger.info("Running output jobs")
    logger.info(f"Containers to run: {container_names}")

    # Execute the command in Altium to run output jobs
    response = await altium_bridge.execute_command(
        "run_output_jobs", {"container_names": container_names}
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


@mcp.tool()
async def create_pcb_footprint(
    ctx: Context,
    footprint_name: str,
    description: str,
    pads: list,
    courtyard_x_mm: float = 0,
    courtyard_y_mm: float = 0,
) -> str:
    """
    Create a new PCB footprint in the currently active PcbLib document.
    The PcbLib (e.g. Discrete.PcbLib) must be the focused document in Altium.

    Pad format: each element is "pad_number|x_mm|y_mm|width_mm|height_mm|shape"
                shape options: Rect (default), Round, Oval
                Coordinates are in mm relative to component origin (0,0).
                Pin 1 is indicated by a gap in the top-left silkscreen corner.

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


@mcp.tool()
async def get_server_status(ctx: Context) -> str:
    """Get the current status of the Altium MCP server"""
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

    # Verify configuration before starting
    if not altium_bridge.config.verify_paths():
        logger.warning("Configuration not complete. Some functionality may not work.")

    # Log status
    logger.info(f"Altium executable: {altium_bridge.config.altium_exe_path}")
    logger.info(f"Script path: {altium_bridge.config.script_path}")

    # Run the server
    mcp.run(transport="stdio")
