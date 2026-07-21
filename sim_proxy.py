#!/usr/bin/env python3
"""sim-proxy: forward SMS and missed calls from Android to Telegram via ADB."""

import json
import logging
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

CONFIG = {}
logger = logging.getLogger("sim-proxy")
running = True
start_time = time.time()
device_connected = True
telegram_update_offset = 0


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

class TelegramLogHandler(logging.Handler):
    """Send ERROR+ log messages to Telegram."""

    _sending = False

    def emit(self, record):
        if self._sending:
            return
        self._sending = True
        try:
            text = f"\u26a0\ufe0f sim-proxy {record.levelname}\n{record.getMessage()}"
            if len(text) > 4000:
                text = text[:4000] + "..."
            send_telegram_curl(text)
        except Exception:
            pass
        finally:
            self._sending = False


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

    th = TelegramLogHandler()
    th.setLevel(logging.ERROR)
    logger.addHandler(th)


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
    # Join continuation lines (body can contain newlines)
    raw_lines = output.strip().splitlines()
    lines = []
    for raw in raw_lines:
        if re.match(r"^Row:\s+\d+\s+", raw):
            lines.append(raw)
        elif lines:
            lines[-1] += "\n" + raw
    for line in lines:
        m = re.match(r"^Row:\s+\d+\s+(.*)", line, re.DOTALL)
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


def get_call_log():
    output = adb_shell("content query --uri content://call_log/calls")
    return parse_content_query(output)


def get_missed_calls():
    rows = get_call_log()
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
# Telegram send
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
# Telegram commands (getUpdates)
# ---------------------------------------------------------------------------

def get_telegram_updates(timeout=0):
    global telegram_update_offset
    url = (
        f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/getUpdates"
        f"?offset={telegram_update_offset}&timeout={timeout}"
        f"&allowed_updates=%5B%22message%22%5D"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            return []
        updates = data.get("result", [])
        if updates:
            telegram_update_offset = updates[-1]["update_id"] + 1
        return updates
    except Exception as e:
        logger.error("getUpdates failed: %s", e)
        return []


def process_commands(timeout=0):
    updates = get_telegram_updates(timeout=timeout)
    for update in updates:
        message = update.get("message", {})
        text = message.get("text", "")
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != CONFIG["TELEGRAM_CHAT_ID"]:
            continue
        if not text.startswith("/"):
            continue
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        logger.info("Command: %s %s", cmd, args)
        try:
            handle_command(cmd, args)
        except Exception as e:
            logger.exception("Command handler error: %s", e)
            telegram_send_safe(f"Ошибка: {e}")


def handle_command(cmd, args):
    handlers = {
        "/status": lambda: handle_status(),
        "/sms": lambda: handle_sms(args),
        "/reboot": lambda: handle_reboot(),
        "/ussd": lambda: handle_ussd(args),
        "/calls": lambda: handle_calls(args),
        "/inbox": lambda: handle_inbox(args),
        "/help": lambda: handle_help(),
    }
    handler = handlers.get(cmd)
    if handler:
        handler()
    else:
        send_telegram(f"Неизвестная команда: {cmd}\nИспользуй /help")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_timestamp(ms):
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return "unknown"


def format_uptime():
    elapsed = int(time.time() - start_time)
    days = elapsed // 86400
    hours = (elapsed % 86400) // 3600
    mins = (elapsed % 3600) // 60
    if days > 0:
        return f"{days}д {hours}ч"
    if hours > 0:
        return f"{hours}ч {mins}м"
    return f"{mins}м"


def format_sms(record):
    address = record.get("address", "unknown")
    body = record.get("body", "")
    date = record.get("date", "0")
    ts = format_timestamp(date)
    return f"\U0001f4e9 SMS от {address}\n{body}\n\U0001f550 {ts}"


def format_missed_call(record):
    number = record.get("number", "unknown")
    date = record.get("date", "0")
    ts = format_timestamp(date)
    return f"\U0001f4de Пропущенный звонок от {number}\n\U0001f550 {ts}"


# ---------------------------------------------------------------------------
# Device info
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
    try:
        reg = adb_cmd(["shell", "dumpsys", "telephony.registry"])
        m = re.search(r"primary=(CellSignalStrength(\w+))", reg)
        if m:
            primary_type = m.group(2)  # e.g. "Lte", "Nr"
            pattern = rf"m{primary_type}=.*?rsrp=(-?\d+)"
            rm = re.search(pattern, reg)
            if rm:
                rsrp = int(rm.group(1))
                if rsrp >= -85:
                    quality = "отличный"
                elif rsrp >= -95:
                    quality = "хороший"
                elif rsrp >= -105:
                    quality = "нормальный"
                elif rsrp >= -115:
                    quality = "слабый"
                else:
                    quality = "очень слабый"
                info["signal_level"] = f"{rsrp} dBm, {quality}"
            else:
                info["signal_level"] = None
        else:
            info["signal_level"] = None
    except Exception:
        info["signal_level"] = None
    return info


def get_sim_number():
    # Method 1: dumpsys iphonesubinfo (rooted)
    try:
        output = adb_shell("dumpsys iphonesubinfo")
        m = re.search(r"Phone Number\s*=\s*(\+?\d+)", output, re.IGNORECASE)
        if m:
            return m.group(1)
    except Exception:
        pass
    # Method 2: telephony/siminfo content provider
    try:
        output = adb_shell("content query --uri content://telephony/siminfo")
        rows = parse_content_query(output)
        for row in rows:
            number = row.get("number", "")
            if number and number != "NULL" and number.strip():
                return number
    except Exception:
        pass
    # Method 3: getprop
    try:
        output = adb_cmd(["shell", "getprop", "gsm.sim.operator.numeric"]).strip()
        if output:
            return output
    except Exception:
        pass
    return "unknown"


def send_health_check():
    info = get_device_info()
    number = get_sim_number()
    sig = info["signal"]
    if info.get("signal_level") is not None:
        sig += f" ({info['signal_level']})"
    text = (
        "\u2705 sim-proxy запущен\n"
        f"\U0001f4f1 Устройство: {info['model']}\n"
        f"\U0001f4de Номер: {number}\n"
        f"\U0001f50b Батарея: {info['battery']}%\n"
        f"\U0001f4f6 Оператор: {info['operator']}\n"
        f"\U0001f4e1 Сигнал: {sig}"
    )
    telegram_send_safe(text)
    logger.info("Health check sent")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def handle_status():
    if not check_device_connected():
        send_telegram("\u26a0\ufe0f Телефон не подключён")
        return
    info = get_device_info()
    number = get_sim_number()
    uptime = format_uptime()
    sig = info["signal"]
    if info.get("signal_level") is not None:
        sig += f" ({info['signal_level']})"
    text = (
        f"\U0001f4ca Статус\n"
        f"\U0001f4f1 {info['model']}\n"
        f"\U0001f4de Номер: {number}\n"
        f"\U0001f50b Батарея: {info['battery']}%\n"
        f"\U0001f4f6 Оператор: {info['operator']}\n"
        f"\U0001f4e1 Сигнал: {sig}\n"
        f"\u23f1 Uptime: {uptime}"
    )
    send_telegram(text)


SMS_HELPER = "/data/local/tmp/sim-proxy-sendsms.sh"


def ensure_sms_helper():
    """Deploy SMS sender script to the device (avoids ADB quoting issues)."""
    script = (
        '#!/system/bin/sh\n'
        'NUMBER="$1"\n'
        'shift\n'
        'TEXT="$*"\n'
        'am startservice'
        ' -a android.intent.action.RESPOND_VIA_MESSAGE'
        ' -d "smsto:$NUMBER"'
        ' --es android.intent.extra.TEXT "$TEXT"'
        ' -n com.google.android.apps.messaging/.shared.NoConfirmationSmsSendService\n'
    )
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(script)
        tmp = f.name
    try:
        adb_cmd(["push", tmp, SMS_HELPER])
        adb_shell(f"chmod 755 {SMS_HELPER}")
    finally:
        Path(tmp).unlink(missing_ok=True)


def handle_sms(args):
    if not check_device_connected():
        send_telegram("\u26a0\ufe0f Телефон не подключён")
        return
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        send_telegram("Формат: /sms +7XXXXXXXXXX текст сообщения")
        return
    number, text = parts
    try:
        result = adb_shell(f"{SMS_HELPER} {number} {text}")
        if "error" in result.lower():
            send_telegram(f"\u274c Ошибка отправки: {result.strip()}")
        else:
            send_telegram(f"\u2705 SMS отправлено на {number}")
    except Exception as e:
        send_telegram(f"\u274c Ошибка отправки SMS: {e}")


def handle_reboot():
    if not check_device_connected():
        send_telegram("\u26a0\ufe0f Телефон не подключён")
        return
    send_telegram("\U0001f504 Перезагрузка телефона...")
    try:
        adb_cmd(["reboot"])
    except Exception as e:
        send_telegram(f"\u274c Ошибка: {e}")


def handle_ussd(code):
    if not check_device_connected():
        send_telegram("\u26a0\ufe0f Телефон не подключён")
        return
    code = code.strip()
    if not code:
        send_telegram("Формат: /ussd *100#")
        return
    try:
        # Clear logcat before sending
        adb_cmd(["logcat", "-c"])
        # Send USSD via intent
        encoded = code.replace("#", "%23")
        adb_cmd([
            "shell", "am", "start",
            "-a", "android.intent.action.CALL",
            "-d", f"tel:{encoded}",
        ])
        send_telegram(f"\U0001f4e1 USSD {code} отправлен, жду ответ...")
        time.sleep(7)
        # Try to capture response from logcat
        output = adb_cmd(["logcat", "-d", "-t", "100"])
        response = None
        for line in output.splitlines():
            lower = line.lower()
            if "ussd" in lower or "mmi" in lower:
                m = re.search(
                    r"(?:response|message|text|result)[=: ]+(.+)",
                    line, re.IGNORECASE,
                )
                if m:
                    response = m.group(1).strip()
                    break
        if response:
            send_telegram(f"\U0001f4e1 USSD ответ:\n{response}")
        else:
            send_telegram(
                "\u26a0\ufe0f Не удалось захватить ответ USSD автоматически.\n"
                "Проверь экран телефона."
            )
    except Exception as e:
        send_telegram(f"\u274c Ошибка USSD: {e}")


def handle_calls(args):
    if not check_device_connected():
        send_telegram("\u26a0\ufe0f Телефон не подключён")
        return
    n = 5
    args = args.strip()
    if args.isdigit():
        n = int(args)
    try:
        rows = get_call_log()
        rows.sort(key=lambda r: int(r.get("_id", 0)), reverse=True)
        rows = rows[:n]
        if not rows:
            send_telegram("Журнал звонков пуст")
            return
        call_types = {
            "1": "\U0001f4f2 Входящий",
            "2": "\U0001f4f1 Исходящий",
            "3": "\U0001f4f5 Пропущенный",
        }
        lines = [f"\U0001f4de Последние {len(rows)} звонков:"]
        for r in rows:
            ct = call_types.get(r.get("type", ""), f"? ({r.get('type', '?')})")
            number = r.get("number", "?")
            ts = format_timestamp(r.get("date", "0"))
            duration = r.get("duration", "0")
            lines.append(f"\n{ct} {number}\n\U0001f550 {ts} ({duration}с)")
        send_telegram("\n".join(lines))
    except Exception as e:
        send_telegram(f"\u274c Ошибка: {e}")


def handle_inbox(args):
    if not check_device_connected():
        send_telegram("\u26a0\ufe0f Телефон не подключён")
        return
    n = 5
    args = args.strip()
    if args.isdigit():
        n = int(args)
    try:
        rows = get_sms_inbox()
        rows.sort(key=lambda r: int(r.get("_id", 0)), reverse=True)
        rows = rows[:n]
        if not rows:
            send_telegram("Входящие пусто")
            return
        lines = [f"\U0001f4e9 Последние {len(rows)} SMS:"]
        for r in rows:
            address = r.get("address", "?")
            body = r.get("body", "")
            ts = format_timestamp(r.get("date", "0"))
            lines.append(f"\n{address} ({ts}):\n{body}")
        send_telegram("\n".join(lines))
    except Exception as e:
        send_telegram(f"\u274c Ошибка: {e}")


def handle_help():
    send_telegram(
        "\U0001f4cb Команды:\n"
        "/status — статус телефона\n"
        "/sms +7XXX текст — отправить SMS\n"
        "/inbox [N] — последние N SMS (по умолчанию 5)\n"
        "/calls [N] — последние N звонков (по умолчанию 5)\n"
        "/ussd *100# — USSD запрос\n"
        "/reboot — перезагрузить телефон"
    )


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
    global device_connected
    last_contact_time = time.time()
    next_device_check = 0

    while running:
        # Process Telegram commands (long poll doubles as sleep)
        try:
            process_commands(timeout=CONFIG["POLL_INTERVAL"])
        except Exception as e:
            logger.error("Command processing error: %s", e)

        now = time.time()
        if now < next_device_check:
            continue

        connected_now = check_device_connected()

        if connected_now:
            if not device_connected:
                logger.info("Device reconnected")
                send_health_check()
                device_connected = True

            last_contact_time = now
            next_device_check = now

            try:
                poll_once()
            except subprocess.TimeoutExpired:
                logger.warning("ADB command timed out")
            except Exception as e:
                logger.exception("Poll error: %s", e)
        else:
            if device_connected:
                logger.warning("Device disconnected")
                ts = datetime.fromtimestamp(last_contact_time).strftime("%Y-%m-%d %H:%M")
                alert = (
                    "\U0001f6a8 Телефон не отвечает!\n"
                    f"Последний контакт: {ts}"
                )
                telegram_send_safe(alert)
                device_connected = False

            next_device_check = now + 60

    logger.info("Shutting down")


# ---------------------------------------------------------------------------
# Signal handling & entry point
# ---------------------------------------------------------------------------

def shutdown_handler(signum, _frame):
    global running
    logger.info("Received signal %d, shutting down...", signum)
    running = False


def main():
    global CONFIG, start_time, device_connected

    CONFIG = load_config()
    setup_logging()
    start_time = time.time()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    logger.info("sim-proxy starting")
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if check_device_connected():
        device_connected = True
        init_state_if_needed()
        ensure_sms_helper()
        send_health_check()
    else:
        device_connected = False
        logger.warning("Device not connected at startup")

    main_loop()


if __name__ == "__main__":
    main()
