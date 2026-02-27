"""
Arduino Tools for ArduinoVision Agent

Provides functions to interact with Arduino boards programmatically:
- Detect connected boards
- Write, compile, and upload sketches
- Read serial output
"""

import subprocess
import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False
    print("Warning: pyserial not installed. Serial functions will not work.")

# Directory for storing sketches
SKETCH_DIR = Path(__file__).parent / "sketches"
SKETCH_DIR.mkdir(exist_ok=True)


def fix_port_permissions(port: str) -> dict:
    """
    Attempt to fix serial port permissions via sudo chmod.
    Works on Linux/WSL where the user may not be in the dialout/uucp group.

    Returns a dict with success status and message.
    """
    try:
        result = subprocess.run(
            ["sudo", "-n", "chmod", "a+rw", port],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return {"success": True, "message": f"Fixed permissions on {port}"}
        return {
            "success": False,
            "message": (
                f"Auto-fix failed (sudo -n requires passwordless sudo).\n"
                f"Manual fix options:\n"
                f"  1. Quick fix (resets on unplug): sudo chmod a+rw {port}\n"
                f"  2. Permanent fix: sudo usermod -a -G uucp $USER  then restart WSL\n"
                f"  3. Udev rule: sudo cp 99-usb-serial.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger"
            ),
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


def check_port_accessible(port: str) -> bool:
    """Return True if the process can open the serial port for writing."""
    try:
        mode = os.stat(port).st_mode
        # Check world-writable, or owner/group write where uid/gid matches
        uid = os.getuid()
        gids = os.getgroups()
        st = os.stat(port)
        owner_write = (st.st_uid == uid) and bool(st.st_mode & stat.S_IWUSR)
        group_write = (st.st_gid in gids) and bool(st.st_mode & stat.S_IWGRP)
        world_write = bool(st.st_mode & stat.S_IWOTH)
        return owner_write or group_write or world_write
    except Exception:
        return False


def _permission_denied_hint(port: str) -> str:
    return (
        f"Permission denied on {port}.\n"
        f"Fix options (run in WSL terminal):\n"
        f"  Quick (resets on unplug):  sudo chmod a+rw {port}\n"
        f"  Permanent (needs re-login): sudo usermod -a -G uucp $USER\n"
        f"  Permanent (udev rule):     sudo cp 99-usb-serial.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger"
    )


def list_arduino_boards() -> list[dict]:
    """
    Detect connected Arduino boards.
    
    Returns:
        List of dictionaries with board info (port, description, fqbn)
    """
    arduino_boards = []
    
    # Known Arduino / clone USB-to-serial VIDs
    ARDUINO_VIDS = {
        0x2341,  # Arduino LLC (official)
        0x1a86,  # QinHeng CH340/CH341 (common in clones)
        0x0403,  # FTDI
        0x10c4,  # Silicon Labs CP210x
        0x067b,  # Prolific PL2303
    }

    # Method 1: PySerial detection
    if HAS_SERIAL:
        ports = serial.tools.list_ports.comports()
        for port in ports:
            is_arduino = (
                (port.vid in ARDUINO_VIDS)
                or (port.description and "arduino" in port.description.lower())
            )
            if is_arduino:
                arduino_boards.append({
                    "port": port.device,
                    "description": port.description or "Unknown Arduino",
                    "hwid": port.hwid,
                    "vid": hex(port.vid) if port.vid else None,
                    "pid": hex(port.pid) if port.pid else None,
                })

    # Method 2: Arduino CLI detection
    # Include ALL detected serial ports (not just ones with matching_boards)
    # Clones often have no matching_boards but are still programmable
    try:
        result = subprocess.run(
            ["arduino-cli", "board", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for detected in data.get("detected_ports", []):
                port_info = detected.get("port", {})
                address = port_info.get("address", "")
                if not address:
                    continue
                protocol = port_info.get("protocol", "")
                # Only include serial ports (skip network/bluetooth)
                if protocol and "serial" not in protocol:
                    continue
                if detected.get("matching_boards"):
                    board_info = detected["matching_boards"][0]
                    arduino_boards.append({
                        "port": address,
                        "board_name": board_info.get("name", "Unknown Board"),
                        "fqbn": board_info.get("fqbn", "arduino:avr:uno"),
                        "protocol": protocol,
                    })
                else:
                    # Clone or unrecognised board — include with default FQBN
                    arduino_boards.append({
                        "port": address,
                        "board_name": "Arduino (clone/unrecognised)",
                        "fqbn": "arduino:avr:uno",
                        "protocol": protocol,
                    })
    except FileNotFoundError:
        print("Warning: arduino-cli not found. Install it for full functionality.")
    except subprocess.TimeoutExpired:
        print("Warning: arduino-cli timed out")
    except json.JSONDecodeError:
        print("Warning: Could not parse arduino-cli output")
    
    # Deduplicate by port
    seen_ports = set()
    unique_boards = []
    for board in arduino_boards:
        if board["port"] not in seen_ports:
            seen_ports.add(board["port"])
            unique_boards.append(board)
    
    return unique_boards


def write_sketch(code: str, name: str = "sketch") -> str:
    """
    Write Arduino sketch code to a file.
    
    Arduino sketches must be in a folder with the same name as the .ino file.
    
    Args:
        code: The Arduino C++ code
        name: Name for the sketch (becomes folder and file name)
    
    Returns:
        Path to the sketch folder (for use with compile/upload)
    """
    # Sanitize name
    safe_name = "".join(c for c in name if c.isalnum() or c == "_").lower()
    if not safe_name:
        safe_name = "sketch"
    
    sketch_folder = SKETCH_DIR / safe_name
    sketch_folder.mkdir(exist_ok=True)
    
    sketch_file = sketch_folder / f"{safe_name}.ino"
    sketch_file.write_text(code)
    
    return str(sketch_folder)


def compile_sketch(sketch_path: str, fqbn: str = "arduino:avr:uno") -> dict:
    """
    Compile an Arduino sketch.
    
    Args:
        sketch_path: Path to the sketch folder (containing .ino file)
        fqbn: Fully Qualified Board Name (e.g., "arduino:avr:uno")
    
    Returns:
        Dictionary with success status, output, and errors
    """
    try:
        result = subprocess.run(
            ["arduino-cli", "compile", "--fqbn", fqbn, sketch_path],
            capture_output=True,
            text=True,
            timeout=120  # Compilation can take a while
        )
        return {
            "success": result.returncode == 0,
            "output": result.stdout,
            "errors": result.stderr if result.returncode != 0 else None
        }
    except FileNotFoundError:
        return {
            "success": False,
            "output": "",
            "errors": "arduino-cli not found. Please install it first."
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "output": "",
            "errors": "Compilation timed out after 120 seconds."
        }


def upload_sketch(sketch_path: str, port: str, fqbn: str = "arduino:avr:uno") -> dict:
    """
    Upload a compiled sketch to an Arduino board.
    
    Args:
        sketch_path: Path to the sketch folder
        port: Serial port (e.g., "/dev/ttyUSB0" or "/dev/ttyACM0")
        fqbn: Fully Qualified Board Name
    
    Returns:
        Dictionary with success status, output, and errors
    """
    # Pre-flight: check port is accessible before even calling arduino-cli
    if not check_port_accessible(port):
        # Try auto-fix via sudo -n (works if NOPASSWD is configured)
        fix = fix_port_permissions(port)
        if fix["success"]:
            pass  # proceed with the upload
        else:
            return {
                "success": False,
                "output": "",
                "errors": _permission_denied_hint(port),
            }

    try:
        result = subprocess.run(
            ["arduino-cli", "upload", "-p", port, "--fqbn", fqbn, sketch_path],
            capture_output=True,
            text=True,
            timeout=60
        )
        success = result.returncode == 0
        errors = result.stderr if not success else None

        # Surface permission errors with actionable hint
        if not success and errors and "permission denied" in errors.lower():
            errors = _permission_denied_hint(port) + "\n\nOriginal error:\n" + errors

        return {
            "success": success,
            "output": result.stdout,
            "errors": errors,
        }
    except FileNotFoundError:
        return {
            "success": False,
            "output": "",
            "errors": "arduino-cli not found. Please install it first."
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "output": "",
            "errors": "Upload timed out after 60 seconds."
        }


async def read_serial(port: str, duration: float = 3.0, baudrate: int = 9600) -> str:
    """
    Read serial output from Arduino.
    
    Args:
        port: Serial port
        duration: How long to read (seconds)
        baudrate: Serial baud rate (default 9600)
    
    Returns:
        String containing serial output lines
    """
    if not HAS_SERIAL:
        return "Error: pyserial not installed"
    
    output_lines = []
    try:
        with serial.Serial(port, baudrate, timeout=1) as ser:
            # Wait for Arduino to reset after connection
            await asyncio.sleep(2)
            
            end_time = asyncio.get_event_loop().time() + duration
            while asyncio.get_event_loop().time() < end_time:
                if ser.in_waiting:
                    try:
                        line = ser.readline().decode("utf-8", errors="ignore").strip()
                        if line:
                            output_lines.append(line)
                    except Exception:
                        pass
                await asyncio.sleep(0.1)
                
    except serial.SerialException as e:
        return f"Serial error: {e}"
    except Exception as e:
        return f"Error reading serial: {e}"
    
    return "\n".join(output_lines) if output_lines else "No serial output received"


def deploy_code(code: str, port: str, name: str = "quick_deploy", fqbn: str = "arduino:avr:uno") -> dict:
    """
    Complete deployment pipeline: write, compile, and upload.
    
    Args:
        code: Arduino code to deploy
        port: Serial port of the Arduino
        name: Name for the sketch
        fqbn: Fully Qualified Board Name
    
    Returns:
        Dictionary with success status and details
    """
    # Step 1: Write
    try:
        sketch_path = write_sketch(code, name)
    except Exception as e:
        return {"success": False, "stage": "write", "error": str(e)}
    
    # Step 2: Compile
    compile_result = compile_sketch(sketch_path, fqbn)
    if not compile_result["success"]:
        return {
            "success": False, 
            "stage": "compile", 
            "error": compile_result["errors"],
            "sketch_path": sketch_path
        }
    
    # Step 3: Upload
    upload_result = upload_sketch(sketch_path, port, fqbn)
    if not upload_result["success"]:
        return {
            "success": False, 
            "stage": "upload", 
            "error": upload_result["errors"],
            "sketch_path": sketch_path
        }
    
    return {
        "success": True,
        "message": "Code deployed successfully!",
        "sketch_path": sketch_path,
        "port": port
    }


# CLI for testing
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python arduino_tools.py <command>")
        print("Commands: list, test")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "list":
        boards = list_arduino_boards()
        if boards:
            print(f"Found {len(boards)} Arduino board(s):")
            for board in boards:
                print(f"  - Port: {board.get('port')}")
                print(f"    Board: {board.get('board_name', board.get('description', 'Unknown'))}")
                if 'fqbn' in board:
                    print(f"    FQBN: {board['fqbn']}")
                print()
        else:
            print("No Arduino boards found.")
    
    elif command == "test":
        print("Testing Arduino tools...")
        
        # Test board detection
        boards = list_arduino_boards()
        print(f"1. Board detection: {'PASS' if boards else 'No boards'} ({len(boards)} found)")
        
        # Test sketch writing
        test_code = "void setup() {} void loop() {}"
        path = write_sketch(test_code, "test_sketch")
        print(f"2. Sketch write: PASS ({path})")
        
        # Test compilation (if board found)
        if boards:
            result = compile_sketch(path)
            print(f"3. Compilation: {'PASS' if result['success'] else 'FAIL'}")
            if not result['success']:
                print(f"   Error: {result['errors']}")
        else:
            print("3. Compilation: SKIPPED (no boards)")
