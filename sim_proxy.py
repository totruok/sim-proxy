#!/usr/bin/env python3
"""sim-proxy: forward SMS and missed calls from Android to Telegram via ADB."""

import json
import logging
import math
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".sim-proxy"
LOG_FILE = STATE_DIR / "sim-proxy.log"
STATE_FILE = STATE_DIR / "state.json"

CONFIG = {}
logger = logging.getLogger("sim-proxy")
start_time = time.time()
telegram_update_offset = 0
telegram_poll_failures = 0

# Where forwarding has got to, per kind: {"sms": {"last_id": .., "last_date": ..}}
STATE = {}

# What we know about the phone between polls.
phone = {
    "ready": False,          # connected, booted and its state initialised
    "status": "startup",     # why it is not ready: "startup", "missing" or "rebooting"
    "missing": 0,            # checks in a row without a usable phone
    "alerted": False,        # the "not responding" alert for this outage was sent
    "last_contact": None,    # time.time() of the last good check
    "reboot_deadline": 0.0,  # time.monotonic() until which an absent phone is expected
    "poll_failures": {"sms": 0, "calls": 0},  # polls in a row that failed, per kind
    "read_alerted": set(),   # kinds the chat was told cannot be read
    "drops": [],             # time.monotonic() of recent one-check drops
    "unstable_quiet_until": 0.0,
}

# Notices that must reach the chat even if Telegram is down when they are
# created (startup, phone lost, phone back). Sent in order, retried each loop.
pending_notices = []

TELEGRAM_MAX_TEXT = 4000           # Telegram's limit is 4096; keep a margin
STALE_COMMAND_SECONDS = 300        # commands older than this are not executed
MISSING_CHECKS_BEFORE_ALERT = 2    # a single failed check is often an adb blip
POLL_FAILURES_BEFORE_ALERT = 3
REBOOT_GRACE_SECONDS = 240
MAX_LIST_ITEMS = 20
UNSTABLE_DROPS = 3                 # short drops within UNSTABLE_WINDOW ...
UNSTABLE_WINDOW = 600              # ... that make the link worth a warning

SMS_URI = "content://sms/inbox"
CALLS_URI = "content://call_log/calls"

# What is forwarded. The legacy file is the single-id state of older versions.
KINDS = {
    "sms": {"uri": SMS_URI, "filter": None, "legacy": "last_sms_id", "name": "SMS"},
    "calls": {"uri": CALLS_URI, "filter": "type=3", "legacy": "last_call_id",
              "name": "the call log"},
}


class PhoneError(Exception):
    """adb or the phone failed."""


class TelegramError(Exception):
    """A message could not be sent; trying again later may help."""


class TelegramRejected(TelegramError):
    """Telegram refused this particular message (HTTP 400); retrying will not help."""


class StateError(Exception):
    """The state file exists but cannot be used."""


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

    # Exiting here would leave the service in a silent restart loop: use a
    # safe value instead and say so once logging and Telegram are up.
    config["WARNINGS"] = []
    raw = config.get("POLL_INTERVAL") or "10"
    try:
        interval = int(raw)
    except ValueError:
        interval = 10
    clamped = min(max(interval, 2), 300)
    if clamped != interval or not raw.isdigit():
        config["WARNINGS"].append(
            f"POLL_INTERVAL={raw} in .env is not a whole number from 2 to 300; "
            f"using {clamped}")
    config["POLL_INTERVAL"] = clamped

    proxy = config.get("TELEGRAM_PROXY")
    if proxy:
        # urllib and the curl fallback both read these.
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            os.environ[var] = proxy
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
            text = f"⚠️ sim-proxy {record.levelname}\n{record.getMessage()}"
            send_telegram_curl(text)
        except Exception:
            pass
        finally:
            self._sending = False


def setup_logging():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    fh = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Under systemd the console goes to the journal, which rotates. Under
    # launchd it goes to a plain file that never rotates, so skip it there.
    if sys.stderr.isatty() or os.environ.get("INVOCATION_ID"):
        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    th = TelegramLogHandler()
    th.setLevel(logging.ERROR)
    logger.addHandler(th)


# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------

def adb_cmd(args, timeout=30, quiet=False):
    cmd = ["adb"]
    serial = CONFIG.get("ADB_DEVICE_SERIAL")
    if serial:
        cmd += ["-s", serial]
    cmd += args
    if not quiet:
        logger.debug("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise PhoneError(f"adb timed out after {timeout} s ({' '.join(args)[:80]})")
    except OSError as e:
        raise PhoneError(f"cannot run adb: {e}")
    if result.returncode != 0:
        raise PhoneError(f"ADB error: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def adb_shell(command, timeout=30, quiet=False):
    """Run a command as root on the phone."""
    # adb joins its argv with spaces and the device shell parses the result
    # again, so pass su the whole command as a single quoted word. su then
    # runs it with sh -c, which is where the caller's own quoting applies.
    return adb_cmd(["shell", "su -c " + shlex.quote(command)],
                   timeout=timeout, quiet=quiet)


def device_state():
    """The adb state of the phone ("device", "unauthorized", ...), or None if
    adb does not list it."""
    try:
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True,
            timeout=10, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    rows = []
    for line in result.stdout.splitlines()[1:]:
        if not line.strip() or line.startswith("*"):  # "* daemon started ..."
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            rows.append((parts[0], parts[1].strip()))
    serial = CONFIG.get("ADB_DEVICE_SERIAL")
    if serial:
        rows = [r for r in rows if r[0] == serial]
    if not rows:
        return None
    if len(rows) > 1:
        return "several phones connected, set ADB_DEVICE_SERIAL"
    return rows[0][1]


STATE_HINTS = {
    None: "not connected",
    "unauthorized": "unauthorized, confirm the USB debugging prompt on the phone",
    "offline": "offline, try replugging the cable",
    "booting": "connected, still booting",
}


def describe_state(state):
    return STATE_HINTS.get(state, state)


def phone_booted():
    try:
        return adb_cmd(["shell", "getprop", "sys.boot_completed"],
                       quiet=True).strip() == "1"
    except PhoneError:
        return False


def phone_now_ms():
    return int(adb_cmd(["shell", "date", "+%s"], quiet=True).strip()) * 1000


# ---------------------------------------------------------------------------
# Content queries
# ---------------------------------------------------------------------------
#
# `content query` prints rows as "Row: N col=value, col=value" with values
# unescaped, so a message body can contain anything that looks like another
# column or row. To stay safe: list rows with digit-only columns, then read
# each row on its own with the one free-text column last.

def query_numbers(uri, columns, where=None):
    """Rows matching `where` as tuples of ints, for digit-only columns."""
    cmd = f"content query --uri {uri} --projection {':'.join(columns)}"
    if where:
        cmd += " --where " + shlex.quote(where)
    out = adb_shell(cmd, quiet=True)
    if not out.strip():
        # A real empty result prints "No result found."; the content tool
        # reports provider errors on stderr and prints nothing here.
        raise PhoneError(f"content query on {uri} printed nothing (provider error?)")
    pattern = re.compile(
        r"^Row: \d+ " + ", ".join(f"{c}=(-?\\d+|NULL)" for c in columns) + "$"
    )
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line == "No result found.":
            continue
        m = pattern.match(line)
        if not m:
            raise PhoneError(f"unexpected content query output: {line[:120]}")
        rows.append(tuple(0 if v == "NULL" else int(v) for v in m.groups()))
    return rows


# Columns that only ever hold numbers; anything else is free text.
NUMERIC_COLUMNS = {"_id", "date", "type", "duration", "presentation", "sim_id"}


def query_row(uri, row_id, columns):
    """One row as a dict, or None if it no longer exists. The last column may
    hold any text, including commas, "col=" and newlines."""
    cmd = (f"content query --uri {uri} --projection {':'.join(columns)} "
           f"--where " + shlex.quote(f"_id={int(row_id)}"))
    out = adb_shell(cmd, quiet=True)
    if out.strip() == "No result found.":
        return None
    if out.endswith("\n"):
        out = out[:-1]
    parts = []
    for i, c in enumerate(columns):
        if i == len(columns) - 1:
            value = "(.*)"
        elif c in NUMERIC_COLUMNS:
            value = r"(-?\d+|NULL)"
        else:
            value = "(.*?)"
        parts.append(f"{re.escape(c)}={value}")
    m = re.match(r"\ARow: \d+ " + ", ".join(parts) + r"\Z", out, re.DOTALL)
    if not m:
        raise PhoneError(f"unexpected content query output: {out[:120]}")
    return dict(zip(columns, m.groups()))


def clean(value):
    """content query prints missing values as NULL."""
    return "" if value in (None, "NULL") else value


def get_sms(row_id):
    return query_row(SMS_URI, row_id, ["_id", "date", "address", "body"])


def get_call(row_id):
    return query_row(CALLS_URI, row_id,
                     ["_id", "date", "type", "duration", "presentation", "number"])


def with_filter(kind, condition=None):
    f = KINDS[kind]["filter"]
    if f and condition:
        return f"{f} AND ({condition})"
    return f or condition


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------
#
# A row counts as new if its _id OR its date is above what was forwarded so
# far. The _id alone is not enough: a replaced or wiped phone starts again
# from _id 1, and SQLite reuses the highest _id after that row is deleted.

def load_state():
    try:
        data = json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise StateError(str(e))
    if not isinstance(data, dict):
        raise StateError("not a JSON object")
    state = {}
    for kind, mark in data.items():
        if kind not in KINDS:
            continue
        try:
            state[kind] = {"last_id": int(mark["last_id"]),
                           "last_date": int(mark["last_date"])}
        except (TypeError, KeyError, ValueError):
            raise StateError(f"bad entry for {kind!r}")
    return state


def save_state():
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(STATE, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


def baseline(kind):
    """Starting point for a kind that has no state yet: everything already on
    the phone counts as forwarded."""
    info = KINDS[kind]
    legacy = STATE_DIR / info["legacy"]
    try:
        last_id = int(legacy.read_text().strip())
    except (OSError, ValueError):
        last_id = None
    if last_id is not None:
        # Older versions forwarded everything up to this _id.
        rows = query_numbers(info["uri"], ["_id", "date"],
                             with_filter(kind, f"_id<={last_id}"))
        mark = {"last_id": last_id,
                "last_date": max((d for _, d in rows), default=None)}
        if mark["last_date"] is None:
            mark["last_date"] = phone_now_ms()
        logger.info("Imported %s state from %s: %s", kind, legacy.name, mark)
        return mark
    rows = query_numbers(info["uri"], ["_id", "date"], with_filter(kind))
    mark = {"last_id": max((i for i, _ in rows), default=0),
            "last_date": max([d for _, d in rows] + [phone_now_ms()])}
    logger.info("Starting %s from the phone's current messages: %s", kind, mark)
    return mark


def ensure_state():
    """Give every kind a starting point. Each one is saved as soon as it is
    known, so a kind that cannot be read never holds back the others.
    Returns {kind: error} for the kinds that could not be read."""
    errors = {}
    for kind in KINDS:
        if kind in STATE:
            continue
        try:
            STATE[kind] = baseline(kind)
        except PhoneError as e:
            errors[kind] = e
            continue
        save_state()
        retire_legacy_files()
    return errors


def retire_legacy_files():
    """Imported once: if state.json is ever lost, an old file must not bring
    back its stale position and replay everything after it."""
    for kind in STATE:
        legacy = STATE_DIR / KINDS[kind]["legacy"]
        if legacy.exists():
            legacy.replace(legacy.with_name(legacy.name + ".imported"))


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
    except urllib.error.HTTPError as e:
        try:
            description = json.loads(e.read()).get("description", "")
        except Exception:
            description = ""
        error = f"HTTP {e.code} {description}".strip()
        if e.code == 400 and not CHAT_PROBLEM_RE.search(description):
            raise TelegramRejected(error)
        raise TelegramError(error)
    except Exception as e:
        raise TelegramError(str(e))


# HTTP 400 answers that are about the chat, not about the message: every
# message would fail the same way, so nothing may be skipped.
CHAT_PROBLEM_RE = re.compile(
    r"chat not found|upgraded to a supergroup|not enough rights|have no rights"
    r"|bot was kicked|bot is not a member|PEER_ID_INVALID", re.IGNORECASE)


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def split_message(text, limit=TELEGRAM_MAX_TEXT):
    """Split text into chunks Telegram accepts, preferring line breaks."""
    chunks = []
    while utf16_len(text) > limit:
        cut, units = 0, 0
        for i, ch in enumerate(text):
            units += 2 if ord(ch) > 0xFFFF else 1
            if units > limit:
                break
            cut = i + 1
        newline = text.rfind("\n", 0, cut)
        if newline > cut // 2:
            cut = newline + 1
        chunks.append(text[:cut])
        text = text[cut:]
    chunks.append(text)
    return chunks


def send_long(text):
    for chunk in split_message(text):
        send_telegram(chunk)


def send_telegram_curl(text):
    url = f"https://api.telegram.org/bot{CONFIG['TELEGRAM_BOT_TOKEN']}/sendMessage"
    payload = json.dumps({"chat_id": CONFIG["TELEGRAM_CHAT_ID"],
                          "text": text[:TELEGRAM_MAX_TEXT]})
    try:
        # The URL holds the bot token: pass it on stdin, not in argv where
        # other users could see it in the process list.
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "15",
             "-K", "-", "-H", "Content-Type: application/json", "-d", payload],
            input=f'url = "{url}"\n', capture_output=True, text=True, timeout=20,
        )
        if result.stdout.strip() != "200":
            logger.warning("Telegram curl fallback failed: HTTP %s",
                           result.stdout.strip() or "none")
    except Exception as e:
        # The exception text can carry curl's argv: log only the error type.
        logger.warning("Telegram curl fallback failed: %s", type(e).__name__)


def telegram_send_safe(text):
    try:
        send_long(text)
    except TelegramError:
        send_telegram_curl(text)


def queue_notice(text):
    pending_notices.append(text)
    flush_notices()


def flush_notices():
    while pending_notices:
        try:
            send_long(pending_notices[0])
        except TelegramRejected as e:
            logger.warning("Dropping a notice Telegram refused: %s", e)
        except TelegramError as e:
            logger.warning("Notice not sent yet, will retry: %s", e)
            return
        pending_notices.pop(0)


# ---------------------------------------------------------------------------
# Telegram commands (getUpdates)
# ---------------------------------------------------------------------------

# How many getUpdates calls must fail back-to-back before we treat polling
# as genuinely broken (vs. a one-off long-poll timeout) and alert Telegram.
# A failed call is followed by a pause of up to POLL_INTERVAL, so 5 in a row
# is at least a minute of the bot being deaf to commands.
TELEGRAM_FAIL_THRESHOLD = 5


def get_telegram_updates(timeout=0):
    """New updates, or None if Telegram could not be reached."""
    global telegram_update_offset, telegram_poll_failures
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
            # API-level rejection (e.g. bad token): count it as a failure.
            raise RuntimeError(data.get("description", "getUpdates returned ok=false"))
        if telegram_poll_failures >= TELEGRAM_FAIL_THRESHOLD:
            telegram_send_safe(
                "✅ sim-proxy: Telegram connection restored "
                f"(after {telegram_poll_failures} failed polls)"
            )
        telegram_poll_failures = 0
        updates = data.get("result", [])
        if updates:
            telegram_update_offset = updates[-1]["update_id"] + 1
        return updates
    except Exception as e:
        telegram_poll_failures += 1
        if telegram_poll_failures < TELEGRAM_FAIL_THRESHOLD:
            # A single blip is the normal long-poll outcome on a flaky link.
            # Keep it in the log file only, never spam the chat for these.
            benign = isinstance(e, (socket.timeout, TimeoutError)) or (
                isinstance(e, urllib.error.URLError)
                and isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError))
            )
            logger.log(
                logging.DEBUG if benign else logging.WARNING,
                "getUpdates failed (%d in a row): %s", telegram_poll_failures, e,
            )
        elif telegram_poll_failures == TELEGRAM_FAIL_THRESHOLD:
            # Now persistent: the bot can't receive commands. Surface it once.
            logger.error(
                "getUpdates failing repeatedly (%d in a row), commands unavailable: %s",
                telegram_poll_failures, e,
            )
        else:
            # Already alerted; stay quiet in the log until it recovers.
            logger.warning(
                "getUpdates still failing (%d in a row): %s",
                telegram_poll_failures, e,
            )
        return None


def process_commands(timeout=0):
    """Handle new commands. Returns False if Telegram could not be reached."""
    updates = get_telegram_updates(timeout=timeout)
    if updates is None:
        return False
    for update in updates:
        message = update.get("message") or {}
        text = message.get("text", "")
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != CONFIG["TELEGRAM_CHAT_ID"]:
            continue
        if not text.startswith("/"):
            continue
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        sent_at = message.get("date", 0)
        if time.time() - sent_at > STALE_COMMAND_SECONDS:
            # Telegram keeps undelivered updates for 24 h: this one waited
            # while sim-proxy was down or could not reach Telegram. Running it
            # now could be wrong.
            logger.info("Ignoring stale command %s sent at %s", cmd, sent_at)
            telegram_send_safe(
                f"\u23ed Ignored {cmd} sent at {format_time(sent_at * 1000)}: it "
                "reached sim-proxy late (it was down or could not reach "
                "Telegram). Send it again if you still need it."
            )
            continue
        logger.info("Command: %s %s", cmd, args)
        try:
            handle_command(cmd, args)
        except Exception as e:
            logger.warning("Command %s failed: %s", cmd, e, exc_info=True)
            telegram_send_safe(f"❌ {cmd} failed: {e}")
    return True


def handle_command(cmd, args):
    handlers = {
        "/status": lambda: handle_status(),
        "/sms": lambda: handle_sms(args),
        "/reboot": lambda: handle_reboot(),
        "/ussd": lambda: handle_ussd(args),
        "/calls": lambda: handle_calls(args),
        "/inbox": lambda: handle_inbox(args),
        "/help": lambda: handle_help(),
        "/start": lambda: handle_help(),
    }
    handler = handlers.get(cmd)
    if handler:
        handler()
    else:
        send_telegram(f"Unknown command: {cmd}\nSee /help")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_time(ms):
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, TypeError, OSError, OverflowError):
        return "unknown time"


def format_duration(seconds):
    seconds = int(seconds)
    days, hours, mins = seconds // 86400, (seconds % 86400) // 3600, (seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def format_sms(record):
    address = clean(record["address"]) or "unknown sender"
    return (f"\U0001f4e9 SMS from {address}\n{clean(record['body'])}\n"
            f"\U0001f550 {format_time(record['date'])}")


# CallLog.Calls.PRESENTATION_*: the caller's number is not available.
HIDDEN_CALLERS = {"2": "withheld number", "3": "unknown number", "4": "payphone"}


def caller(record):
    hidden = HIDDEN_CALLERS.get(record.get("presentation"))
    return hidden or clean(record.get("number")) or "unknown number"


def format_missed_call(record):
    return (f"\U0001f4de Missed call from {caller(record)}\n"
            f"\U0001f550 {format_time(record['date'])}")


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------

def getprop(name):
    try:
        values = adb_cmd(["shell", "getprop", name], quiet=True).strip().split(",")
    except PhoneError:
        return ""
    # Dual-SIM phones list one value per slot.
    return next((v.strip() for v in values if v.strip()), "")


def get_battery():
    try:
        out = adb_cmd(["shell", "dumpsys", "battery"], quiet=True)
    except PhoneError:
        return "?"
    m = re.search(r"level:\s*(\d+)", out)
    return m.group(1) if m else "?"


def get_signal():
    """Signal of the network in use as "-95 dBm, good", or None."""
    try:
        reg = adb_cmd(["shell", "dumpsys", "telephony.registry"], quiet=True)
    except PhoneError:
        return None
    m = re.search(r"primary=CellSignalStrength(\w+)", reg)
    if not m:
        return None
    if m.group(1) == "Lte":
        rm = re.search(r"mLte=CellSignalStrengthLte:.*?\brsrp=(-?\d+)", reg)
    elif m.group(1) == "Nr":
        rm = re.search(r"mNr=CellSignalStrengthNr:.*?\bssRsrp\s*=\s*(-?\d+)", reg)
    else:
        return None
    if not rm:
        return None
    rsrp = int(rm.group(1))
    if not -140 <= rsrp <= -43:  # 2147483647 means "not available"
        return None
    if rsrp >= -85:
        quality = "excellent"
    elif rsrp >= -95:
        quality = "good"
    elif rsrp >= -105:
        quality = "fair"
    elif rsrp >= -115:
        quality = "weak"
    else:
        quality = "very weak"
    return f"{rsrp} dBm, {quality}"


SIMINFO_ROW = re.compile(r"^Row: \d+ sim_id=(-?\d+), number=(.*)$")


def get_sim_number():
    """The number stored on the inserted SIM, or None. Many SIMs store none."""
    try:
        out = adb_shell("content query --uri content://telephony/siminfo "
                        "--projection sim_id:number", quiet=True)
    except PhoneError:
        return None
    for line in out.splitlines():
        m = SIMINFO_ROW.match(line.strip())
        # sim_id is the slot index, -1 for SIMs that were seen before but
        # are not in the phone now.
        if m and int(m.group(1)) >= 0 and clean(m.group(2)).strip():
            return clean(m.group(2)).strip()
    return None


def get_phone_uptime():
    try:
        return float(adb_cmd(["shell", "cat", "/proc/uptime"], quiet=True).split()[0])
    except (PhoneError, ValueError, IndexError):
        return None


def device_report(title, extra=()):
    try:
        query_numbers(SMS_URI, ["_id"], "_id<0")
        access = "ok"
    except PhoneError as e:
        access = f"FAILED ({e})"
    network = getprop("gsm.network.type") or "no network"
    signal = get_signal()
    lines = [
        title,
        f"\U0001f4f1 Device: {getprop('ro.product.model') or 'unknown'}",
        f"\U0001f4de Number: {get_sim_number() or 'not stored on the SIM'}",
        f"\U0001f50b Battery: {get_battery()}%",
        f"\U0001f4f6 Operator: {getprop('gsm.operator.alpha') or 'none'}",
        f"\U0001f4e1 Signal: {network}" + (f" ({signal})" if signal else ""),
        f"✉️ SMS access: {access}",
    ]
    return "\n".join(lines + list(extra))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

PHONE_NUMBER_RE = re.compile(r"^\+?\d{3,15}$")
# USSD and other MMI codes: start with * or # and end with #. Anything else
# would be dialled as an ordinary voice call.
USSD_CODE_RE = re.compile(r"^[*#][0-9*#]{0,38}#$")


def require_phone():
    state = device_state()
    if state == "device":
        return True
    send_telegram(f"⚠️ Phone not available: {describe_state(state)}")
    return False


def handle_status():
    if not require_phone():
        return
    uptime = get_phone_uptime()
    extra = [
        f"⏱ Phone uptime: {format_duration(uptime) if uptime is not None else 'unknown'}",
        f"⏱ sim-proxy running for: {format_duration(time.time() - start_time)}",
    ]
    send_telegram(device_report("\U0001f4ca Status", extra))


# Message.Sms.TYPE_*: where an outgoing SMS ended up.
SMS_SENT, SMS_OUTBOX, SMS_FAILED, SMS_QUEUED = 2, 4, 5, 6


def handle_sms(args):
    if not require_phone():
        return
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        send_telegram("Usage: /sms +447700900123 message text")
        return
    number, text = parts
    if not PHONE_NUMBER_RE.match(number):
        send_telegram("Number: digits only, optionally starting with +")
        return
    since = phone_now_ms() - 2000
    # Google Messages sends it; service call isms from a root shell does not
    # work on Android 14+.
    out = adb_shell(
        "am startservice -a android.intent.action.RESPOND_VIA_MESSAGE "
        f"-d {shlex.quote('smsto:' + number)} "
        f"--es android.intent.extra.TEXT {shlex.quote(text)} "
        "-n com.google.android.apps.messaging/.shared.NoConfirmationSmsSendService"
    )
    if "Error" in out:
        send_telegram(f"❌ Google Messages did not accept the SMS: {out.strip()}")
        return
    # Messages records the outgoing SMS in the system SMS store: watch it.
    deadline = time.monotonic() + 30
    status, last_error = None, None
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            tick_if_due()
            rows = query_numbers("content://sms", ["_id", "type"],
                                 f"date>{since} AND type IN (2,4,5,6)")
        except PhoneError as e:
            # The SMS is already handed over: a read error must not be
            # reported as a failed send, or it gets sent twice.
            last_error = e
            continue
        if rows:
            status = max(rows)[1]
            if status in (SMS_SENT, SMS_FAILED):
                break
    if status == SMS_SENT:
        send_telegram(f"✅ SMS to {number} sent")
    elif status == SMS_FAILED:
        send_telegram(f"❌ SMS to {number} failed to send. Check the SIM's credit.")
    elif status is None and last_error:
        send_telegram(f"\u23f3 SMS to {number} was handed to Google Messages, but "
                      f"its status could not be read ({last_error}). It was "
                      "probably sent: check before sending it again.")
    else:
        send_telegram(f"\u23f3 SMS to {number} was handed to Google Messages, but "
                      "it was not confirmed as sent within 30 s")


def handle_reboot():
    if not require_phone():
        return
    send_telegram("\U0001f504 Rebooting the phone...")
    adb_cmd(["reboot"])
    phone["reboot_deadline"] = time.monotonic() + REBOOT_GRACE_SECONDS


def read_screen():
    """Nodes of what is on the phone screen as (resource-id, text) pairs."""
    out = adb_cmd(["exec-out", "uiautomator", "dump", "/dev/tty"],
                  timeout=20, quiet=True)
    start, end = out.find("<hierarchy"), out.rfind("</hierarchy>")
    if start < 0 or end < 0:
        raise PhoneError("uiautomator returned no screen dump")
    root = ET.fromstring(out[start:end + len("</hierarchy>")])
    return [(n.get("resource-id", ""), n.get("text", "")) for n in root.iter("node")]


def wait_for_ussd_reply(timeout=30):
    """Text of the USSD reply dialog, which the phone shows on screen."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            tick_if_due()
            nodes = read_screen()
        except (PhoneError, ET.ParseError):
            continue
        # While the request runs the dialog has only a spinner; the reply
        # dialog has an OK (or Send) button.
        if not any(rid.endswith(":id/button1") for rid, _ in nodes):
            continue
        texts = [t for rid, t in nodes if t and rid.endswith(":id/message")]
        if not texts:
            texts = [t for rid, t in nodes
                     if t and not re.search(r":id/button\d$", rid)]
        adb_cmd(["shell", "input", "keyevent", "KEYCODE_BACK"])  # close it
        return "\n".join(texts)
    return None


def handle_ussd(code):
    if not require_phone():
        return
    code = code.strip()
    if not USSD_CODE_RE.match(code):
        send_telegram("Usage: /ussd *100#")
        return
    adb_cmd(["shell", "input", "keyevent", "KEYCODE_WAKEUP"])
    adb_cmd([
        "shell", "am", "start",
        "-a", "android.intent.action.CALL",
        "-d", shlex.quote("tel:" + code.replace("#", "%23")),
    ])
    send_telegram(f"\U0001f4e1 USSD {code} sent, waiting for the reply...")
    reply = wait_for_ussd_reply()
    if reply:
        send_long(f"\U0001f4e1 USSD reply:\n{reply}")
    else:
        send_telegram("⚠️ No USSD reply appeared on the phone within 30 s")


def list_count(args):
    args = args.strip()
    n = int(args) if args.isdigit() else 5
    return max(1, min(n, MAX_LIST_ITEMS))


def latest(uri, n):
    rows = query_numbers(uri, ["_id", "date"])
    return sorted(rows, key=lambda r: (r[1], r[0]), reverse=True)[:n]


CALL_TYPES = {
    "1": "\U0001f4f2 Incoming",
    "2": "\U0001f4f1 Outgoing",
    "3": "\U0001f4f5 Missed",
    "4": "\U0001f4e8 Voicemail",
    "5": "⛔ Rejected",
    "6": "\U0001f6ab Blocked",
    "7": "\U0001f4f2 Answered elsewhere",
}


def handle_calls(args):
    if not require_phone():
        return
    rows = [get_call(i) for i, _ in latest(CALLS_URI, list_count(args))]
    rows = [r for r in rows if r]
    if not rows:
        send_telegram("Call log is empty")
        return
    lines = [f"\U0001f4de Last {len(rows)} calls:"]
    for r in rows:
        kind = CALL_TYPES.get(r["type"], f"Call type {r['type']}")
        lines.append(f"\n{kind} {caller(r)}\n"
                     f"\U0001f550 {format_time(r['date'])} ({clean(r['duration']) or 0}s)")
    send_long("\n".join(lines))


def handle_inbox(args):
    if not require_phone():
        return
    rows = [get_sms(i) for i, _ in latest(SMS_URI, list_count(args))]
    rows = [r for r in rows if r]
    if not rows:
        send_telegram("Inbox is empty")
        return
    lines = [f"\U0001f4e9 Last {len(rows)} SMS:"]
    for r in rows:
        lines.append(f"\n{clean(r['address']) or 'unknown sender'} "
                     f"({format_time(r['date'])}):\n{clean(r['body'])}")
    send_long("\n".join(lines))


def handle_help():
    send_telegram(
        "\U0001f4cb Commands:\n"
        "/status - phone status\n"
        "/sms +447700900123 text - send an SMS\n"
        f"/inbox [N] - last N SMS (default 5, up to {MAX_LIST_ITEMS})\n"
        f"/calls [N] - last N calls (default 5, up to {MAX_LIST_ITEMS})\n"
        "/ussd *100# - USSD request\n"
        "/reboot - reboot the phone"
    )


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

FETCH = {"sms": get_sms, "calls": get_call}
FORMAT = {"sms": format_sms, "calls": format_missed_call}


def poll_kind(kind):
    mark = STATE[kind]
    condition = f"_id>{mark['last_id']} OR date>{mark['last_date']}"
    rows = query_numbers(KINDS[kind]["uri"], ["_id", "date"],
                         with_filter(kind, condition))
    # Rows found only by date (a replaced phone, a reused _id) go first, then
    # the rest by _id: last_id then never passes a row still waiting.
    by_date_only = {r[0] for r in rows if r[0] <= mark["last_id"]}
    rows.sort(key=lambda r: (0, r[1], r[0]) if r[0] in by_date_only else (1, r[0]))
    for i, (row_id, date) in enumerate(rows):
        record = FETCH[kind](row_id)
        if record is None:
            logger.warning("%s #%d disappeared before it could be read", kind, row_id)
        else:
            try:
                send_long(FORMAT[kind](record))
                logger.info("Forwarded %s #%d", kind, row_id)
            except TelegramRejected as e:
                # Retrying the same text would fail forever and hold back
                # everything after it.
                logger.warning("Telegram refused %s #%d: %s", kind, row_id, e)
                try:
                    send_telegram(f"⚠️ Could not forward {kind} #{row_id} "
                                  f"from {format_time(date)}: {e}. "
                                  "See /inbox or /calls.")
                except TelegramRejected as notice_error:
                    # Even a short plain notice is refused: the problem is
                    # the chat, not this message. Keep it and retry.
                    raise TelegramError(f"{e}; notice refused too: {notice_error}")
        # A TelegramError other than a refusal propagates: nothing is marked
        # as forwarded and the same row is tried again on the next poll.
        mark["last_id"] = max(mark["last_id"], row_id)
        following = rows[i + 1] if i + 1 < len(rows) else None
        if (row_id in by_date_only and following and following[1] == date
                and following[0] in by_date_only):
            # The next row is found only by this same date: moving last_date
            # to it now would hide that row if sending it fails.
            mark["last_date"] = max(mark["last_date"], date - 1)
        else:
            mark["last_date"] = max(mark["last_date"], date)
        save_state()


def record_poll_failure(kind, error):
    phone["poll_failures"][kind] += 1
    n = phone["poll_failures"][kind]
    name = KINDS[kind]["name"]
    logger.warning("Reading %s failed (%d in a row): %s", name, n, error)
    if n == POLL_FAILURES_BEFORE_ALERT:
        if kind != "sms" and "sms" in phone["read_alerted"]:
            return  # the SMS alert already covers it (root lost, phone gone)
        goes_on = " SMS forwarding goes on." if kind != "sms" else ""
        queue_notice(f"\U0001f6a8 Cannot read {name} on the phone "
                     f"({n} checks in a row): {error}.{goes_on}")
        phone["read_alerted"].add(kind)


def record_poll_success(kind):
    if kind in phone["read_alerted"]:
        queue_notice(f"\u2705 Reading {KINDS[kind]['name']} works again")
        phone["read_alerted"].discard(kind)
    phone["poll_failures"][kind] = 0


def poll_phone():
    # Each kind on its own: the call log failing must not hold back SMS.
    for kind in KINDS:
        try:
            if kind in STATE:
                poll_kind(kind)
            else:
                error = ensure_state().get(kind)
                if error:
                    raise error
        except PhoneError as e:
            record_poll_failure(kind, e)
            continue
        except TelegramError as e:
            logger.warning("Telegram send failed, will retry: %s", e)
            return
        record_poll_success(kind)


def phone_became_ready():
    errors = ensure_state()
    if "sms" in errors:
        if phone["status"] == "startup":
            queue_notice("\u2705 sim-proxy started\n"
                         f"\U0001f6a8 But SMS cannot be read on the phone: {errors['sms']}")
            phone["status"] = "missing"
            # Already told: no second alert, but a notice when it recovers.
            phone["poll_failures"]["sms"] = POLL_FAILURES_BEFORE_ALERT
            phone["read_alerted"].add("sms")
        else:
            record_poll_failure("sms", errors["sms"])
        return
    extra = []
    if "calls" in errors:
        extra.append(f"\u26a0\ufe0f Call log cannot be read ({errors['calls']}); "
                     "SMS forwarding goes on")
        phone["poll_failures"]["calls"] = max(phone["poll_failures"]["calls"],
                                              POLL_FAILURES_BEFORE_ALERT)
        phone["read_alerted"].add("calls")
    titles = {
        "startup": "✅ sim-proxy started",
        "missing": "\U0001f4f1 Phone is connected",
        "rebooting": "\U0001f4f1 Phone is back after the reboot",
    }
    title = titles[phone["status"]]
    phone.update(ready=True, missing=0, alerted=False, reboot_deadline=0.0)
    queue_notice(device_report(title, extra))


def note_short_drop():
    """The phone missed a check and came back: say so if it keeps happening."""
    now = time.monotonic()
    drops = [t for t in phone["drops"] if now - t < UNSTABLE_WINDOW] + [now]
    phone["drops"] = drops
    logger.warning("Phone missed a check (%d short drops in 10 min)", len(drops))
    if len(drops) >= UNSTABLE_DROPS and now >= phone["unstable_quiet_until"]:
        queue_notice(f"\u26a0\ufe0f The USB connection to the phone keeps dropping "
                     f"({len(drops)} short drops in 10 min). Forwarding goes on, "
                     "but check the cable and the port.")
        phone["unstable_quiet_until"] = now + 3600


def phone_unavailable(state):
    phone["missing"] += 1
    if time.monotonic() < phone["reboot_deadline"]:
        phone.update(ready=False, status="rebooting")
        return
    if phone["ready"]:
        if phone["missing"] < MISSING_CHECKS_BEFORE_ALERT:
            return  # possibly an adb blip: decide on the next check
        phone.update(ready=False, status="missing")
    if phone["alerted"]:
        return
    if phone["status"] == "startup":
        queue_notice("✅ sim-proxy started\n"
                     f"\U0001f6a8 But the phone is not available: {describe_state(state)}")
        phone["status"] = "missing"
    elif phone["missing"] >= MISSING_CHECKS_BEFORE_ALERT:
        if phone["status"] == "rebooting":
            phone["status"] = "missing"
            first = "\U0001f6a8 Phone did not come back after the reboot!"
        else:
            first = "\U0001f6a8 Phone is not responding!"
        contact = (format_time(phone["last_contact"] * 1000)
                   if phone["last_contact"] else "never")
        queue_notice(f"{first}\nadb: {describe_state(state)}\nLast contact: {contact}")
    else:
        return
    phone["alerted"] = True


def phone_tick():
    state = device_state()
    if state == "device" and (phone["ready"] or phone_booted()):
        if phone["ready"] and phone["missing"]:
            note_short_drop()
        phone["missing"] = 0
        if not phone["ready"]:
            phone_became_ready()
        if phone["ready"]:
            phone["last_contact"] = time.time()
            poll_phone()
        return
    phone_unavailable("booting" if state == "device" else state)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def report_unexpected(error, last):
    """Log an unexpected error; repeat it to the chat at most every 10 min."""
    key = f"{type(error).__name__}: {error}"
    now = time.monotonic()
    if last and last[0] == key and now - last[1] < 600:
        logger.warning("Unexpected error again: %s", key, exc_info=True)
        return last
    logger.exception("Unexpected error: %s", key)
    return (key, now)


next_poll = 0.0


def tick_if_due():
    """Check the phone if POLL_INTERVAL has passed since the last check."""
    global next_poll
    if time.monotonic() >= next_poll:
        next_poll = time.monotonic() + CONFIG["POLL_INTERVAL"]
        phone_tick()


def main_loop():
    interval = CONFIG["POLL_INTERVAL"]
    last_unexpected = None
    while True:
        try:
            # Wait for commands until the next poll is due: the long poll
            # doubles as the sleep, and a command is handled at once.
            timeout = max(0, math.ceil(next_poll - time.monotonic()))
            if not process_commands(timeout=timeout):
                # Telegram failed, possibly instantly: pause instead of spinning.
                time.sleep(max(1.0, min(interval, next_poll - time.monotonic())))
            flush_notices()
            tick_if_due()
        except Exception as e:
            last_unexpected = report_unexpected(e, last_unexpected)
            time.sleep(interval)


def main():
    global CONFIG

    CONFIG = load_config()
    setup_logging()
    logger.info("sim-proxy starting")

    for warning in CONFIG["WARNINGS"]:
        logger.warning(warning)
        pending_notices.append(f"\u26a0\ufe0f {warning}")

    try:
        STATE.update(load_state())
        retire_legacy_files()
    except StateError as e:
        broken = STATE_FILE.with_suffix(".broken")
        try:
            STATE_FILE.replace(broken)
        except OSError:
            pass
        message = (f"State file {STATE_FILE} was unreadable ({e}) and was moved "
                   f"to {broken.name}. Starting from the phone's current "
                   "messages: check /inbox for anything that arrived while it "
                   "was broken.")
        logger.warning(message)
        pending_notices.append(f"\U0001f6a8 {message}")

    main_loop()


if __name__ == "__main__":
    main()
