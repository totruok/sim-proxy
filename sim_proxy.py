#!/usr/bin/env python3
"""sim-proxy: forward SMS and missed calls from Android to Telegram via ADB."""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".sim-proxy"
LOG_FILE = STATE_DIR / "sim-proxy.log"
HEARTBEAT_INTERVAL = 24 * 60 * 60  # 24 hours

CONFIG = {}
logger = logging.getLogger("sim-proxy")
running = True
start_time = time.time()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        print(f"ERROR: {env_path} not found. Run install.sh first.", file=sys.stderr)
        sys.exit(1)

    config = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            config[key.strip()] = val.strip().strip('"').strip("'")

    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not config.get(key):
            print(f"ERROR: {key} not set in .env", file=sys.stderr)
            sys.exit(1)

    config["POLL_INTERVAL"] = int(config.get("POLL_INTERVAL", "30"))
    return config


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG)

    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(sh)


# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------

def adb_cmd(args, timeout=30):
    cmd = ["adb"]
    serial = CONFIG.get("ADB_DEVICE_SERIAL")
    if serial:
        cmd += ["-s", serial]
    cmd += args
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"ADB error: {result.stderr.strip()}")
    return result.stdout


def adb_shell(command):
    return adb_cmd(["shell", "su", "-c", command])


def check_device_connected():
    try:
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=10
        )
        serial = CONFIG.get("ADB_DEVICE_SERIAL")
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                if serial:
                    if parts[0] == serial:
                        return True
                else:
                    return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Content query parser
# ---------------------------------------------------------------------------

def parse_content_query(output):
    rows = []
    for line in output.strip().splitlines():
        line = line.strip()
        m = re.match(r"^Row:\s+\d+\s+(.*)", line)
        if not m:
            continue
        rest = m.group(1)
        record = {}
        key_positions = list(re.finditer(r"(?:^|, )([a-zA-Z_]\w*)=", rest))
        for i, km in enumerate(key_positions):
            key = km.group(1)
            val_start = km.end()
            if i + 1 < len(key_positions):
                val_end = key_positions[i + 1].start()
                val = rest[val_start:val_end]
                if val.endswith(", "):
                    val = val[:-2]
            else:
                val = rest[val_start:]
            record[key] = val
        if record:
            rows.append(record)
    return rows


# ---------------------------------------------------------------------------
# SMS & calls
# ---------------------------------------------------------------------------

def get_sms_inbox():
    output = adb_shell("content query --uri content://sms/inbox")
    rows = parse_content_query(output)
    rows.sort(key=lambda r: int(r.get("_id", 0)))
    return rows


def get_missed_calls():
    output = adb_shell("content query --uri content://call_log/calls")
    rows = parse_content_query(output)
    rows = [r for r in rows if r.get("type") == "3"]
    rows.sort(key=lambda r: int(r.get("_id", 0)))
    return rows


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def read_last_id(filename):
    path = STATE_DIR / filename
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_last_id(filename, id_val):
    path = STATE_DIR / filename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(id_val) + "\n")
    tmp.rename(path)


def state_file_exists(filename):
    return (STATE_DIR / filename).exists()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text):
    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/sendMessage"
    data = json.dumps({
        "chat_id": CONFIG["TELEGRAM_CHAT_ID"],
        "text": text,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        raise


def send_telegram_curl(text):
    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/sendMessage"
    payload = json.dumps({"chat_id": CONFIG["TELEGRAM_CHAT_ID"], "text": text})
    try:
        subprocess.run(
            ["curl", "-s", "-X", "POST", url,
             "-H", "Content-Type: application/json",
             "-d", payload],
            timeout=15, check=False,
        )
    except Exception as e:
        logger.error("Telegram curl fallback failed: %s", e)


def telegram_send_safe(text):
    try:
        send_telegram(text)
    except Exception:
        send_telegram_curl(text)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_timestamp(ms):
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return "unknown"


def format_sms(record):
    address = record.get("address", "unknown")
    body = record.get("body", "")
    date = record.get("date", "0")
    ts = format_timestamp(date)
    return f"\U0001f4e9 SMS \u043e\u0442 {address}\n{body}\n\U0001f550 {ts}"


def format_missed_call(record):
    number = record.get("number", "unknown")
    date = record.get("date", "0")
    ts = format_timestamp(date)
    return f"\U0001f4de \u041f\u0440\u043e\u043f\u0443\u0449\u0435\u043d\u043d\u044b\u0439 \u0437\u0432\u043e\u043d\u043e\u043a \u043e\u0442 {number}\n\U0001f550 {ts}"


# ---------------------------------------------------------------------------
# Device info & health
# ---------------------------------------------------------------------------

def get_device_info():
    info = {}
    try:
        info["model"] = adb_cmd(["shell", "getprop", "ro.product.model"]).strip()
    except Exception:
        info["model"] = "unknown"
    try:
        battery_out = adb_cmd(["shell", "dumpsys", "battery"])
        m = re.search(r"level:\s*(\d+)", battery_out)
        info["battery"] = m.group(1) if m else "?"
    except Exception:
        info["battery"] = "?"
    try:
        info["operator"] = adb_cmd(["shell", "getprop", "gsm.operator.alpha"]).strip()
    except Exception:
        info["operator"] = "unknown"
    try:
        info["signal"] = adb_cmd(["shell", "getprop", "gsm.network.type"]).strip()
    except Exception:
        info["signal"] = "unknown"
    return info


def send_health_check():
    info = get_device_info()
    text = (
        "\u2705 sim-proxy \u0437\u0430\u043f\u0443\u0449\u0435\u043d\n"
        f"\U0001f4f1 \u0423\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u043e: {info['model']}\n"
        f"\U0001f50b \u0411\u0430\u0442\u0430\u0440\u0435\u044f: {info['battery']}%\n"
        f"\U0001f4f6 \u041e\u043f\u0435\u0440\u0430\u0442\u043e\u0440: {info['operator']}\n"
        f"\U0001f4e1 \u0421\u0438\u0433\u043d\u0430\u043b: {info['signal']}"
    )
    telegram_send_safe(text)
    logger.info("Health check sent")


def format_uptime():
    elapsed = int(time.time() - start_time)
    days = elapsed // 86400
    hours = (elapsed % 86400) // 3600
    if days > 0:
        return f"{days}\u0434 {hours}\u0447"
    return f"{hours}\u0447"


def send_heartbeat():
    info = get_device_info()
    text = (
        "\U0001f493 sim-proxy \u0436\u0438\u0432\n"
        f"\U0001f50b \u0411\u0430\u0442\u0430\u0440\u0435\u044f: {info['battery']}%\n"
        f"\u23f1 Uptime: {format_uptime()}"
    )
    telegram_send_safe(text)
    logger.info("Heartbeat sent")


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def init_state_if_needed():
    if not state_file_exists("last_sms_id"):
        try:
            rows = get_sms_inbox()
            max_id = max((int(r.get("_id", 0)) for r in rows), default=0)
            write_last_id("last_sms_id", max_id)
            logger.info("Initialized last_sms_id to %d", max_id)
        except Exception as e:
            logger.warning("Could not initialize SMS state: %s", e)
            write_last_id("last_sms_id", 0)

    if not state_file_exists("last_call_id"):
        try:
            rows = get_missed_calls()
            max_id = max((int(r.get("_id", 0)) for r in rows), default=0)
            write_last_id("last_call_id", max_id)
            logger.info("Initialized last_call_id to %d", max_id)
        except Exception as e:
            logger.warning("Could not initialize call state: %s", e)
            write_last_id("last_call_id", 0)


def poll_once():
    # SMS
    last_sms = read_last_id("last_sms_id")
    try:
        sms_rows = get_sms_inbox()
        new_sms = [r for r in sms_rows if int(r.get("_id", 0)) > last_sms]
        for sms in new_sms:
            msg = format_sms(sms)
            send_telegram(msg)
            sms_id = int(sms["_id"])
            write_last_id("last_sms_id", sms_id)
            logger.info("Forwarded SMS #%d from %s", sms_id, sms.get("address", "?"))
            time.sleep(0.1)
    except Exception as e:
        logger.error("SMS poll error: %s", e)

    # Missed calls
    last_call = read_last_id("last_call_id")
    try:
        call_rows = get_missed_calls()
        new_calls = [r for r in call_rows if int(r.get("_id", 0)) > last_call]
        for call in new_calls:
            msg = format_missed_call(call)
            send_telegram(msg)
            call_id = int(call["_id"])
            write_last_id("last_call_id", call_id)
            logger.info("Forwarded missed call #%d from %s", call_id, call.get("number", "?"))
            time.sleep(0.1)
    except Exception as e:
        logger.error("Call poll error: %s", e)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main_loop():
    device_connected = True
    last_contact_time = time.time()
    last_heartbeat = time.time()

    while running:
        connected_now = check_device_connected()

        if connected_now:
            if not device_connected:
                logger.info("Device reconnected")
                send_health_check()
                device_connected = True

            last_contact_time = time.time()

            try:
                poll_once()
            except subprocess.TimeoutExpired:
                logger.warning("ADB command timed out")
            except Exception as e:
                logger.exception("Poll error: %s", e)

            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                send_heartbeat()
                last_heartbeat = now

            time.sleep(CONFIG["POLL_INTERVAL"])
        else:
            if device_connected:
                logger.warning("Device disconnected")
                ts = datetime.fromtimestamp(last_contact_time).strftime("%Y-%m-%d %H:%M")
                alert = (
                    "\U0001f6a8 \u0422\u0435\u043b\u0435\u0444\u043e\u043d \u043d\u0435 \u043e\u0442\u0432\u0435\u0447\u0430\u0435\u0442!\n"
                    f"\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0439 \u043a\u043e\u043d\u0442\u0430\u043a\u0442: {ts}"
                )
                telegram_send_safe(alert)
                device_connected = False

            time.sleep(60)

    logger.info("Shutting down")


# ---------------------------------------------------------------------------
# Signal handling & entry point
# ---------------------------------------------------------------------------

def shutdown_handler(signum, _frame):
    global running
    logger.info("Received signal %d, shutting down...", signum)
    running = False


def main():
    global CONFIG, start_time

    CONFIG = load_config()
    setup_logging()
    start_time = time.time()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    logger.info("sim-proxy starting")

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if check_device_connected():
        init_state_if_needed()
        send_health_check()
    else:
        logger.warning("Device not connected at startup")

    main_loop()


if __name__ == "__main__":
    main()
