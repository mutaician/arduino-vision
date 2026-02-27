#!/usr/bin/env bash
# fix_serial.sh — one-shot serial port permission fix for WSL
#
# Run this once per WSL session before starting the agent if you see
# "Permission denied" on /dev/ttyUSB0 or /dev/ttyACM0.
#
# Usage:  bash fix_serial.sh
#
# For a permanent fix (survives WSL restart):
#   Option A – add yourself to the uucp group (restart WSL after):
#       sudo usermod -a -G uucp $USER
#
#   Option B – install the udev rule (one-time, works forever):
#       sudo cp 99-usb-serial.rules /etc/udev/rules.d/
#       sudo udevadm control --reload-rules
#       sudo udevadm trigger

set -e

PORTS=$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true)

if [ -z "$PORTS" ]; then
    echo "No USB serial ports found. Make sure the Arduino is plugged in."
    exit 1
fi

for PORT in $PORTS; do
    echo "Fixing permissions on $PORT ..."
    sudo chmod a+rw "$PORT"
    echo "  Done: $(ls -la $PORT)"
done

echo ""
echo "Permissions fixed. You can now run: uv run main.py serve"
