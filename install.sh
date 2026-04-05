#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$HOME/.sim-proxy"

info()  { printf "\033[1;34m[INFO]\033[0m %s\n" "$1"; }
error() { printf "\033[1;31m[ERROR]\033[0m %s\n" "$1"; }
ok()    { printf "\033[1;32m[OK]\033[0m %s\n" "$1"; }

# --- Prerequisites ---
command -v adb >/dev/null 2>&1 || { error "ADB not found. Install Android SDK platform-tools."; exit 1; }
command -v python3 >/dev/null 2>&1 || { error "Python 3 not found."; exit 1; }
ok "adb and python3 found"

# --- Check device ---
DEVICE_COUNT=$(adb devices | grep -c 'device$' || true)
if [ "$DEVICE_COUNT" -eq 0 ]; then
    error "No Android device found. Connect phone and enable USB debugging."
    exit 1
fi

DEVICE_MODEL=$(adb shell getprop ro.product.model 2>/dev/null || echo "unknown")
ok "Device found: $DEVICE_MODEL"

# --- Telegram credentials ---
SKIP_ENV=0
if [ -f "$SCRIPT_DIR/.env" ]; then
    read -rp "Existing .env found. Overwrite? [y/N] " overwrite
    if [ "$overwrite" != "y" ] && [ "$overwrite" != "Y" ]; then
        info "Keeping existing .env"
        SKIP_ENV=1
    fi
fi

if [ "$SKIP_ENV" -eq 0 ]; then
    echo ""
    read -rp "Telegram Bot Token: " BOT_TOKEN
    read -rp "Telegram Chat ID: " CHAT_ID
    read -rp "ADB Device Serial (leave empty if only one phone): " SERIAL
    read -rp "Poll interval in seconds [30]: " INTERVAL
    INTERVAL=${INTERVAL:-30}

    cat > "$SCRIPT_DIR/.env" <<EOF
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
TELEGRAM_CHAT_ID=$CHAT_ID
ADB_DEVICE_SERIAL=$SERIAL
POLL_INTERVAL=$INTERVAL
EOF
    ok ".env created"
fi

# --- State directory ---
mkdir -p "$STATE_DIR"
ok "State directory: $STATE_DIR"

# --- Test Telegram ---
info "Testing Telegram connection..."
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.env"
RESPONSE=$(curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": \"\u2705 sim-proxy install test\"}")

if echo "$RESPONSE" | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('ok') else 1)" 2>/dev/null; then
    ok "Telegram message sent"
else
    error "Telegram test failed. Check token and chat ID."
    echo "Response: $RESPONSE"
    exit 1
fi

# --- Make script executable ---
chmod +x "$SCRIPT_DIR/sim_proxy.py"

# --- Autostart ---
PYTHON3=$(command -v python3)
MAIN_SCRIPT="$SCRIPT_DIR/sim_proxy.py"

if [[ "$OSTYPE" == "darwin"* ]]; then
    PLIST_PATH="$HOME/Library/LaunchAgents/com.sim-proxy.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST_PATH" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sim-proxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON3</string>
        <string>$MAIN_SCRIPT</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$STATE_DIR/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$STATE_DIR/launchd-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLISTEOF

    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    launchctl load "$PLIST_PATH"
    ok "LaunchAgent installed and started"
    echo ""
    info "To stop:    launchctl unload $PLIST_PATH"
    info "To start:   launchctl load $PLIST_PATH"
    info "Logs:       tail -f $STATE_DIR/sim-proxy.log"

elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    SERVICE_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SERVICE_DIR"
    SERVICE_PATH="$SERVICE_DIR/sim-proxy.service"
    cat > "$SERVICE_PATH" <<SVCEOF
[Unit]
Description=sim-proxy SMS/call forwarder
After=network.target

[Service]
Type=simple
ExecStart=$PYTHON3 $MAIN_SCRIPT
Restart=always
RestartSec=10
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
SVCEOF

    systemctl --user daemon-reload
    systemctl --user enable sim-proxy
    systemctl --user start sim-proxy
    ok "systemd service installed and started"
    echo ""
    info "Status:  systemctl --user status sim-proxy"
    info "Stop:    systemctl --user stop sim-proxy"
    info "Logs:    journalctl --user -u sim-proxy -f"
    info "         tail -f $STATE_DIR/sim-proxy.log"

else
    info "Unknown OS type: $OSTYPE. Skipping autostart setup."
    info "Run manually: python3 $MAIN_SCRIPT"
fi

echo ""
ok "Installation complete!"
