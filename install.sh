#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$HOME/.sim-proxy"

info()  { printf "\033[1;34m[INFO]\033[0m %s\n" "$1"; }
warn()  { printf "\033[1;33m[WARN]\033[0m %s\n" "$1"; }
error() { printf "\033[1;31m[ERROR]\033[0m %s\n" "$1"; }
ok()    { printf "\033[1;32m[OK]\033[0m %s\n" "$1"; }

# --- Prerequisites ---
command -v adb >/dev/null 2>&1 || { error "ADB not found. Install Android SDK platform-tools."; exit 1; }
command -v python3 >/dev/null 2>&1 || { error "Python 3 not found."; exit 1; }
command -v curl >/dev/null 2>&1 || { error "curl not found."; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { error "Python 3.8 or newer is needed."; exit 1; }
ADB_DIR="$(dirname "$(command -v adb)")"
ok "adb, python3 and curl found"

if [[ "$OSTYPE" == "darwin"* ]]; then
    # Same USB backend as the service (see the LaunchAgent below).
    export ADB_LIBUSB=0
fi

# Read KEY from an .env file the way sim_proxy.py does (not with `source`,
# which would run the file as shell code).
env_get() {
    python3 - "$1" "$2" <<'PYEOF'
import sys
value = ""
for line in open(sys.argv[1]):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, val = line.partition("=")
    if key.strip() == sys.argv[2]:
        value = val.strip().strip('"').strip("'")
print(value)
PYEOF
}

# --- Telegram credentials ---
ENV_FILE="$SCRIPT_DIR/.env"
ENV_NEW="$SCRIPT_DIR/.env.new"
# New answers go to .env.new and replace a working .env only after the checks
# below pass: a typo must not break the running service on its next restart.
cleanup() {
    if [ -f "$ENV_NEW" ]; then
        rm -f "$ENV_NEW"
        info "Your existing .env was left unchanged."
    fi
}
trap cleanup EXIT

ASK=1
if [ -f "$SCRIPT_DIR/.env" ]; then
    read -rp "Existing .env found. Overwrite? [y/N] " overwrite || overwrite=""
    if [ "$overwrite" != "y" ] && [ "$overwrite" != "Y" ]; then
        info "Keeping existing .env"
        ASK=0
    fi
fi

if [ "$ASK" -eq 1 ]; then
    echo ""
    no_input() { echo ""; error "No input: run install.sh in a terminal, or create .env first."; exit 1; }
    read -rp "Telegram Bot Token: " BOT_TOKEN || no_input
    read -rp "Telegram Chat ID: " CHAT_ID || no_input
    read -rp "ADB Device Serial (leave empty if only one phone): " SERIAL || no_input
    while true; do
        read -rp "Poll interval in seconds, 2 to 300 [10]: " INTERVAL || no_input
        INTERVAL=${INTERVAL:-10}
        if [[ "$INTERVAL" =~ ^[0-9]+$ ]] && (( 10#$INTERVAL >= 2 && 10#$INTERVAL <= 300 )); then
            break
        fi
        error "Enter a whole number of seconds from 2 to 300."
    done
    # Keep a proxy that is already configured: the installer does not ask for it.
    OLD_PROXY=""
    if [ -f "$SCRIPT_DIR/.env" ]; then
        OLD_PROXY=$(env_get "$SCRIPT_DIR/.env" TELEGRAM_PROXY)
        ENV_FILE="$ENV_NEW"
    fi
    (
        umask 077
        {
            echo "TELEGRAM_BOT_TOKEN=$BOT_TOKEN"
            echo "TELEGRAM_CHAT_ID=$CHAT_ID"
            echo "ADB_DEVICE_SERIAL=$SERIAL"
            echo "POLL_INTERVAL=$INTERVAL"
            if [ -n "$OLD_PROXY" ]; then echo "TELEGRAM_PROXY=$OLD_PROXY"; fi
        } > "$ENV_FILE"
    )
fi
# It holds the bot token.
chmod 600 "$ENV_FILE"

ADB_DEVICE_SERIAL=$(env_get "$ENV_FILE" ADB_DEVICE_SERIAL)
POLL_INTERVAL=$(env_get "$ENV_FILE" POLL_INTERVAL)
TELEGRAM_PROXY=$(env_get "$ENV_FILE" TELEGRAM_PROXY)
if [ -n "$POLL_INTERVAL" ]; then
    if [[ ! "$POLL_INTERVAL" =~ ^[0-9]+$ ]]; then
        warn "POLL_INTERVAL=$POLL_INTERVAL in .env is not a whole number: the service will use 10."
    elif (( 10#$POLL_INTERVAL < 2 || 10#$POLL_INTERVAL > 300 )); then
        warn "POLL_INTERVAL=$POLL_INTERVAL in .env is outside 2 to 300: the service will use the nearest allowed value."
    fi
fi
if [ -n "$TELEGRAM_PROXY" ]; then
    info "Using TELEGRAM_PROXY=$TELEGRAM_PROXY from .env"
elif [ -n "${HTTPS_PROXY:-}${https_proxy:-}${HTTP_PROXY:-}${http_proxy:-}${ALL_PROXY:-}${all_proxy:-}" ]; then
    warn "This shell has a proxy set, but the service will not use it, so the"
    warn "Telegram test below does not use it either. If Telegram needs a proxy"
    warn "here, add TELEGRAM_PROXY=... to .env (see docs/server-setup.md)."
fi
ADB=(adb)
if [ -n "$ADB_DEVICE_SERIAL" ]; then
    ADB+=(-s "$ADB_DEVICE_SERIAL")
fi

# --- Check device ---
# Read the state the way the service does, so unauthorized, offline or
# several phones are named. adb reads stdin; </dev/null keeps it from eating
# the answers above.
DEVICES=$(adb devices </dev/null 2>/dev/null | tail -n +2 | grep -v '^\*' | grep -v '^[[:space:]]*$' || true)
if [ -n "$ADB_DEVICE_SERIAL" ]; then
    DEVICES=$(printf '%s\n' "$DEVICES" | awk -F '\t' -v s="$ADB_DEVICE_SERIAL" '$1 == s')
fi
if [ -z "$DEVICES" ]; then
    error "No Android phone found${ADB_DEVICE_SERIAL:+ with serial $ADB_DEVICE_SERIAL}."
    error "Connect the phone and enable USB debugging (see docs/android-setup.md)."
    exit 1
fi
if [ "$(printf '%s\n' "$DEVICES" | wc -l | tr -d ' ')" -gt 1 ]; then
    error "Several phones are connected: set ADB_DEVICE_SERIAL in .env to one of:"
    printf '%s\n' "$DEVICES" | cut -f1
    exit 1
fi
DEVICE_STATE=$(printf '%s\n' "$DEVICES" | cut -f2-)
if [ "$DEVICE_STATE" != "device" ]; then
    error "The phone is connected but not usable: $DEVICE_STATE"
    if [ "$DEVICE_STATE" = "unauthorized" ]; then
        error "Accept the \"Allow USB debugging\" prompt on the phone and tick \"Always allow\"."
    fi
    exit 1
fi
DEVICE_MODEL=$("${ADB[@]}" shell getprop ro.product.model </dev/null 2>/dev/null | tr -d '\r' || echo "unknown")
ok "Device found: $DEVICE_MODEL"
if "${ADB[@]}" shell "su -c id" </dev/null 2>/dev/null | grep -q "uid=0"; then
    ok "Root access works"
else
    warn "No root access over adb: sim-proxy cannot read SMS without it."
    warn "Root the phone with Magisk and grant root to the shell (see docs/android-setup.md)."
fi

# --- State directory ---
mkdir -p "$STATE_DIR"
ok "State directory: $STATE_DIR"

# --- Test Telegram ---
# Same code path and proxy as the service: urllib, with TELEGRAM_PROXY from
# .env and no proxy otherwise.
info "Testing Telegram connection..."
if python3 - "$ENV_FILE" <<'PYEOF'
import json, os, sys, urllib.request, urllib.error
config = {}
for line in open(sys.argv[1]):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, val = line.partition("=")
    config[key.strip()] = val.strip().strip('"').strip("'")
for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(var, None)
if config.get("TELEGRAM_PROXY"):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        os.environ[var] = config["TELEGRAM_PROXY"]
req = urllib.request.Request(
    f"https://api.telegram.org/bot{config.get('TELEGRAM_BOT_TOKEN', '')}/sendMessage",
    data=json.dumps({"chat_id": config.get("TELEGRAM_CHAT_ID", ""),
                     "text": "✅ sim-proxy install test"}).encode(),
    headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=15).read()
except urllib.error.HTTPError as e:
    print(f"Telegram answered HTTP {e.code}: {e.read().decode(errors='replace')}")
    sys.exit(1)
except Exception as e:
    print(f"Cannot reach Telegram: {e}")
    sys.exit(1)
PYEOF
then
    ok "Telegram message sent"
else
    error "Telegram test failed. Check the token and chat ID, that you pressed"
    error "Start in the chat with your bot, and, if api.telegram.org is blocked"
    error "here, TELEGRAM_PROXY in .env."
    exit 1
fi

if [ "$ENV_FILE" = "$ENV_NEW" ]; then
    mv "$ENV_NEW" "$SCRIPT_DIR/.env"
    ENV_FILE="$SCRIPT_DIR/.env"
fi
ok ".env saved"

# --- Autostart ---
PYTHON3=$(command -v python3)
MAIN_SCRIPT="$SCRIPT_DIR/sim_proxy.py"
SERVICE_PATH_VALUE="$ADB_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if [[ "$OSTYPE" == "darwin"* ]]; then
    PLIST_PATH="$HOME/Library/LaunchAgents/com.sim-proxy.plist"
    PLIST_NEW="$STATE_DIR/com.sim-proxy.plist.new"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST_NEW" <<PLISTEOF
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
        <string>$SERVICE_PATH_VALUE</string>
        <!-- Native IOKit USB backend: the default libusb backend segfaults
             in its hotplug thread when any other USB device re-enumerates
             (seen 2026-10 with a flapping ASMedia controller). -->
        <key>ADB_LIBUSB</key>
        <string>0</string>
    </dict>
</dict>
</plist>
PLISTEOF
    # Check it before stopping a running service.
    plutil -lint -s "$PLIST_NEW" || { error "Generated LaunchAgent is invalid: $PLIST_NEW"; exit 1; }

    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    mv "$PLIST_NEW" "$PLIST_PATH"
    # The adb server started above lives on and the service would reuse it:
    # stop it so the service starts its own with the environment above.
    adb kill-server 2>/dev/null || true
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

[Service]
Type=simple
ExecStart=$PYTHON3 $MAIN_SCRIPT
Restart=always
RestartSec=10
Environment=PATH=$SERVICE_PATH_VALUE

[Install]
WantedBy=default.target
SVCEOF

    systemctl --user daemon-reload
    systemctl --user enable sim-proxy
    systemctl --user restart sim-proxy
    ok "systemd service installed and started"
    if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
        warn "Lingering is off: the service stops when you log out and does"
        warn "not start after a reboot. Turn it on with:"
        warn "    sudo loginctl enable-linger $USER"
    fi
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
