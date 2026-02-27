"""
ArduinoVision API Server - Companion REST endpoints

Run alongside main.py for REST API access:
    uv run api_server.py

Provides:
- GET /api/boards - List connected Arduino boards
- POST /api/upload - Upload code to Arduino
- POST /api/token - Generate Stream user token
- GET /api/health - Health check
"""

import os
import time
import jwt
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from arduino_tools import (
    list_arduino_boards,
    write_sketch,
    compile_sketch,
    upload_sketch,
)

load_dotenv()

app = FastAPI(title="ArduinoVision API", version="1.0.0")

# Stream credentials from environment
STREAM_API_KEY = os.getenv("STREAM_API_KEY", "")
STREAM_API_SECRET = os.getenv("STREAM_API_SECRET", "")

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TokenRequest(BaseModel):
    user_id: str


class UploadRequest(BaseModel):
    code: str
    board: str  # Port like /dev/ttyUSB0
    board_fqbn: str = "arduino:avr:uno"
    sketch_name: str = "frontend_sketch"


@app.get("/api/health")
async def health():
    return {"status": "ok", "stream_configured": bool(STREAM_API_KEY and STREAM_API_SECRET)}


@app.get("/api/stream-config")
async def get_stream_config():
    """Get Stream API key (public) for frontend."""
    if not STREAM_API_KEY:
        raise HTTPException(status_code=500, detail="STREAM_API_KEY not configured")
    return {"apiKey": STREAM_API_KEY}


@app.post("/api/token")
async def generate_token(req: TokenRequest):
    """Generate a Stream user token for video calls."""
    if not STREAM_API_SECRET:
        raise HTTPException(status_code=500, detail="STREAM_API_SECRET not configured")
    
    # Create JWT token for Stream
    # Stream tokens use HS256 algorithm
    now = int(time.time())
    payload = {
        "user_id": req.user_id,
        "iat": now,
        "exp": now + 3600 * 24,  # 24 hour expiry
    }
    
    token = jwt.encode(payload, STREAM_API_SECRET, algorithm="HS256")
    return {"token": token, "user_id": req.user_id}


@app.get("/api/boards")
async def get_boards():
    """List connected Arduino boards."""
    boards = list_arduino_boards()
    # Return just the ports for the dropdown
    ports = [b["port"] for b in boards]
    return {
        "boards": ports,
        "details": boards,
    }


@app.post("/api/upload")
async def upload_code(req: UploadRequest):
    """Upload code to Arduino - write, compile, upload."""
    try:
        # Write sketch
        sketch_path = write_sketch(req.code, req.sketch_name)
        
        # Compile
        compile_result = compile_sketch(sketch_path, req.board_fqbn)
        if not compile_result["success"]:
            raise HTTPException(
                status_code=400,
                detail=f"Compilation failed: {compile_result['errors']}"
            )
        
        # Upload
        upload_result = upload_sketch(sketch_path, req.board, req.board_fqbn)
        if not upload_result["success"]:
            raise HTTPException(
                status_code=400,
                detail=f"Upload failed: {upload_result['errors']}"
            )
        
        return {
            "success": True,
            "message": f"Code uploaded successfully to {req.board}!",
            "sketch_path": sketch_path,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("API_PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
