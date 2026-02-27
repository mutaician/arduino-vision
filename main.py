"""
ArduinoVision - AI-Powered Hardware Debugging Agent

Uses VisionAgents SDK with:
- VLM mode for reliable video frame processing
- Server mode for frontend connection
- Event system for tool call debugging
- Arduino tools via function calling

Usage:
    uv run main.py run              # Single-agent console mode (uses default UI)
    uv run main.py serve --port 8000  # Server mode for custom frontend
"""

import json
import logging
import os
import time
from typing import Optional
import jwt
from datetime import datetime
from dotenv import load_dotenv
from fastapi import HTTPException, Request
from pydantic import BaseModel

from vision_agents.core import Agent, AgentLauncher, User, Runner
from vision_agents.plugins import getstream, gemini, deepgram, nvidia, openai
from vision_agents.core.llm.events import ToolStartEvent, ToolEndEvent

from arduino_tools import (
    list_arduino_boards,
    write_sketch,
    compile_sketch,
    upload_sketch,
    read_serial,
)

load_dotenv()

# Set up logging for debugging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("arduinovision")

# Store tool calls for debugging (accessible via events)
tool_call_history: list[dict] = []

# System prompt for the Arduino debugging agent
SYSTEM_PROMPT = """You are ArduinoVision, an AI assistant specialized in debugging Arduino and IoT projects.

## YOUR CAPABILITIES

You can SEE the user's hardware setup through their camera. You can identify:
- Arduino boards (Uno, Nano, Mega, ESP32, etc.)
- Electronic components (LEDs, resistors, buttons, sensors, wires)
- Breadboard layouts and pin connections
- Whether LEDs are on/off, displays showing content

You have access to these tools - USE THEM when needed:
- list_boards: Detect connected Arduino boards (ALWAYS call this first to find the port)
- write_code: Write Arduino sketch code to a file
- compile_code: Compile the Arduino sketch  
- upload_code: Upload compiled sketch to Arduino
- read_serial: Read serial output from Arduino for debugging
- deploy_code: One-step write + compile + upload (convenience)

## DEBUGGING WORKFLOW

When a user reports an issue:

1. **OBSERVE**: Look at their setup through the camera
   - Describe what you see: "I can see an Arduino Uno with a red LED connected to..."
   - Identify the physical pin connections

2. **DETECT**: Call list_boards to find connected Arduino
   - You MUST call this to get the port (e.g., /dev/ttyUSB0)

3. **ANALYZE**: Compare visual setup with their code
   - Look for pin number mismatches
   - Check for missing pinMode() calls
   - Verify wiring matches code logic

4. **FIX**: Offer to fix and deploy corrected code
   - Explain the issue clearly: "Your LED is on pin 8 but code uses pin 9"
   - Write the fix and upload it

5. **VERIFY**: Confirm the fix worked
   - Ask user if LED is now blinking
   - Use read_serial to check debug output

## COMMUNICATION STYLE

- Be conversational and helpful
- Always describe what you SEE before suggesting fixes
- Be specific about pin numbers and connections
- Celebrate success: "Great! I can see the LED is now blinking!"

## IMPORTANT

- ALWAYS call list_boards first to detect the Arduino port
- The port is needed for upload_code and read_serial
- If you can't see something clearly, ask the user to adjust the camera
- Default board type is arduino:avr:uno unless user specifies otherwise
"""


async def create_agent(**kwargs) -> Agent:
    """Create the ArduinoVision agent with registered tools."""
    
    llm=openai.Realtime(model="gpt-realtime", voice="cedar", fps=1)
    
    # Register Arduino tools with the LLM
    @llm.register_function(
        description="List all connected Arduino boards. Returns port, board name, and FQBN. ALWAYS call this first to find the port needed for upload."
    )
    async def list_boards() -> dict:
        """Detect connected Arduino boards."""
        boards = list_arduino_boards()
        logger.info(f"🔍 list_boards found {len(boards)} board(s)")
        if boards:
            return {
                "found": True,
                "count": len(boards),
                "boards": boards,
                "message": f"Found {len(boards)} board(s). Use the 'port' for upload operations."
            }
        return {
            "found": False,
            "count": 0,
            "boards": [],
            "message": "No Arduino boards detected. Make sure board is connected via USB."
        }
    
    @llm.register_function(
        description="Write Arduino code to a sketch file. Returns the sketch path needed for compile/upload."
    )
    async def write_code(code: str, sketch_name: str = "debug_sketch") -> dict:
        """Write Arduino code to a file."""
        try:
            path = write_sketch(code, sketch_name)
            logger.info(f"📝 write_code saved to {path}")
            return {
                "success": True,
                "sketch_path": path,
                "message": f"Code saved to {path}"
            }
        except Exception as e:
            logger.error(f"❌ write_code failed: {e}")
            return {"success": False, "error": str(e)}
    
    @llm.register_function(
        description="Compile an Arduino sketch. Returns success status and any error messages."
    )
    async def compile_code(sketch_path: str, board_fqbn: str = "arduino:avr:uno") -> dict:
        """Compile an Arduino sketch."""
        logger.info(f"🔨 compile_code: {sketch_path} for {board_fqbn}")
        result = compile_sketch(sketch_path, board_fqbn)
        if result["success"]:
            logger.info("✅ Compilation successful")
            return {"success": True, "message": "Compilation successful!"}
        logger.error(f"❌ Compilation failed: {result['errors']}")
        return {"success": False, "message": "Compilation failed", "errors": result["errors"]}
    
    @llm.register_function(
        description="Upload a compiled sketch to Arduino. Requires sketch_path, port (from list_boards), and board type."
    )
    async def upload_code(sketch_path: str, port: str, board_fqbn: str = "arduino:avr:uno") -> dict:
        """Upload sketch to Arduino."""
        logger.info(f"📤 upload_code: {sketch_path} to {port}")
        result = upload_sketch(sketch_path, port, board_fqbn)
        if result["success"]:
            logger.info(f"✅ Upload successful to {port}")
            return {"success": True, "message": f"Code uploaded successfully to {port}!"}
        logger.error(f"❌ Upload failed: {result['errors']}")
        return {"success": False, "message": "Upload failed", "errors": result["errors"]}
    
    @llm.register_function(
        description="Read serial output from Arduino. Useful for debugging with Serial.println(). Returns captured output."
    )
    async def serial_monitor(port: str, duration_seconds: float = 3.0, baudrate: int = 9600) -> dict:
        """Read serial output from Arduino."""
        logger.info(f"📡 serial_monitor: {port} for {duration_seconds}s at {baudrate} baud")
        output = await read_serial(port, duration_seconds, baudrate)
        logger.info(f"📡 Serial output: {output[:100]}..." if len(output) > 100 else f"📡 Serial: {output}")
        return {"output": output, "duration": duration_seconds, "baudrate": baudrate}
    
    @llm.register_function(
        description="Deploy code in one step: write, compile, and upload. The fastest way to test a fix. Requires code and port."
    )
    async def deploy_code(code: str, port: str, sketch_name: str = "quick_fix", board_fqbn: str = "arduino:avr:uno") -> dict:
        """Complete deployment: write + compile + upload."""
        logger.info(f"🚀 deploy_code to {port}")
        
        # Write
        try:
            path = write_sketch(code, sketch_name)
        except Exception as e:
            logger.error(f"❌ Deploy failed at write: {e}")
            return {"success": False, "stage": "write", "error": str(e)}
        
        # Compile
        compile_result = compile_sketch(path, board_fqbn)
        if not compile_result["success"]:
            logger.error(f"❌ Deploy failed at compile")
            return {"success": False, "stage": "compile", "error": compile_result["errors"]}
        
        # Upload
        upload_result = upload_sketch(path, port, board_fqbn)
        if not upload_result["success"]:
            logger.error(f"❌ Deploy failed at upload")
            return {"success": False, "stage": "upload", "error": upload_result["errors"]}
        
        logger.info("✅ Deploy successful!")
        return {
            "success": True,
            "message": "Code deployed successfully! Check if your hardware is now working.",
            "sketch_path": path
        }
    
    # Create the agent with VLM, STT, and TTS
    agent = Agent(
        edge=getstream.Edge(),
        agent_user=User(name="ArduinoVision", id="arduino-vision-agent"),
        instructions=SYSTEM_PROMPT,
        llm=llm,
        # stt=deepgram.STT(),  # Speech-to-text for voice input
        # tts=deepgram.TTS(),  # Text-to-speech for voice output (free tier)
    )
    
    # Subscribe to tool events for debugging
    @agent.events.subscribe
    async def on_tool_start(event: ToolStartEvent):
        """Log when a tool starts executing."""
        tool_entry = {
            "event": "start",
            "tool": event.tool_name,
            "args": event.arguments,
            "timestamp": datetime.now().isoformat(),
        }
        tool_call_history.append(tool_entry)
        logger.info(f"🔧 TOOL START: {event.tool_name}")
        logger.info(f"   Args: {json.dumps(event.arguments, indent=2) if event.arguments else 'None'}")
    
    @agent.events.subscribe  
    async def on_tool_end(event: ToolEndEvent):
        """Log when a tool completes."""
        tool_entry = {
            "event": "end",
            "tool": event.tool_name,
            "success": event.success,
            "execution_time_ms": event.execution_time_ms,
            "timestamp": datetime.now().isoformat(),
        }
        if not event.success:
            tool_entry["error"] = event.error
        tool_call_history.append(tool_entry)
        
        if event.success:
            logger.info(f"✅ TOOL END: {event.tool_name} ({event.execution_time_ms:.0f}ms)")
        else:
            logger.error(f"❌ TOOL FAILED: {event.tool_name} - {event.error}")
    
    return agent


async def join_call(agent: Agent, call_type: str, call_id: str, **kwargs) -> None:
    """Handle joining a call and initial interaction."""
    # create_user must be called before create_call so the edge transport
    # has agent_user_id set (it's only populated inside agent.join() otherwise)
    await agent.create_user()
    call = await agent.create_call(call_type, call_id)
    
    async with agent.join(call):
        # Initial greeting - the agent will speak this
        await agent.simple_response(
            "Hello! I'm ArduinoVision, your AI hardware debugging assistant. "
            "I can see through your camera and help debug Arduino issues. "
            "Show me your setup and tell me what's not working!"
        )
        
        # Wait for the call to end
        await agent.finish()


# ── Custom REST endpoints ──────────────────────────────────────────────────

class TokenRequest(BaseModel):
    user_id: str


class UploadRequest(BaseModel):
    code: str
    board: str  # port like /dev/ttyUSB0
    fqbn: str = "arduino:avr:uno"


class MessageRequest(BaseModel):
    text: str
    session_id: Optional[str] = None


runner = Runner(AgentLauncher(create_agent=create_agent, join_call=join_call))


@runner.fast_api.get("/stream-config")
def get_stream_config():
    """Return the public Stream API key."""
    api_key = os.getenv("STREAM_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="STREAM_API_KEY not configured in .env")
    return {"apiKey": api_key}


@runner.fast_api.post("/token")
def generate_token(req: TokenRequest):
    """Generate a Stream JWT for the given user ID."""
    secret = os.getenv("STREAM_API_SECRET", "")
    if not secret:
        raise HTTPException(status_code=500, detail="STREAM_API_SECRET not configured in .env")
    now = int(time.time())
    payload = {
        "user_id": req.user_id,
        "iat": now,
        "exp": now + 86400,  # 24 hours
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    return {"token": token, "user_id": req.user_id}


@runner.fast_api.get("/boards")
def get_boards():
    """List connected Arduino boards."""
    boards = list_arduino_boards()
    return {
        "boards": [b["port"] for b in boards],
        "details": boards,
    }


@runner.fast_api.post("/upload")
async def upload_code_endpoint(req: UploadRequest):
    """Write, compile, and upload an Arduino sketch from the editor."""
    from arduino_tools import write_sketch, compile_sketch, upload_sketch

    # Write sketch
    try:
        path = write_sketch(req.code, "editor_sketch")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write sketch: {e}")

    # Determine FQBN: use provided one, or try to look it up from board details
    fqbn = req.fqbn
    if not fqbn or fqbn == "arduino:avr:uno":
        boards = list_arduino_boards()
        matched = next((b for b in boards if b["port"] == req.board), None)
        if matched and matched.get("fqbn"):
            fqbn = matched["fqbn"]

    # Compile
    compile_result = compile_sketch(path, fqbn)
    if not compile_result["success"]:
        return {
            "success": False,
            "stage": "compile",
            "error": compile_result.get("errors", "Compilation failed"),
            "message": "Compilation failed — see error for details",
        }

    # Upload
    upload_result = upload_sketch(path, req.board, fqbn)
    if not upload_result["success"]:
        return {
            "success": False,
            "stage": "upload",
            "error": upload_result.get("errors", "Upload failed"),
            "message": "Upload failed — see error for details",
        }

    return {"success": True, "message": f"Code uploaded successfully to {req.board}!", "fqbn": fqbn}


@runner.fast_api.post("/message")
async def send_message_to_agent(req: MessageRequest, request: Request):
    """Inject a user text message into the active agent session so it responds via TTS + chat."""
    launcher = request.app.state.launcher

    # Find the target session
    session = None
    if req.session_id:
        session = launcher.get_session(req.session_id)
    if session is None:
        # Fall back to first active session
        sessions = list(launcher._sessions.values())
        session = sessions[0] if sessions else None

    if session is None:
        raise HTTPException(status_code=404, detail="No active agent session")

    await session.agent.simple_response(req.text)
    return {"ok": True}


if __name__ == "__main__":
    runner.cli()
