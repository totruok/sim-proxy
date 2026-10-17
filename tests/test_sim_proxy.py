"""Tests for sim_proxy.py.

They drive the real code against a fake phone that answers like Android's
`content` tool and a fake Telegram, so no phone, adb or network is needed.
Run from the repo root:

    python3 -m unittest discover -s tests
"""
import json
import re
import shlex
import sys
import tempfile
import time as real_time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sim_proxy as sp  # noqa: E402

# Module attributes the tests replace, restored after each test.
PATCHED = ("adb_cmd", "device_state", "send_telegram", "send_telegram_curl",
           "get_telegram_updates", "phone_tick", "SCRIPT_DIR", "STATE_DIR",
           "STATE_FILE", "time")
ORIGINALS = {name: getattr(sp, name) for name in PATCHED}


class FakeTime:
    """Clock for sim_proxy: sleep() returns at once and moves the clock on."""

    def __init__(self):
        self.now = real_time.time()
        self.mono = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        self.mono += seconds


class FakePhone:
    def __init__(self):
        self.sms = []
        self.calls = []
        self.siminfo = [{"sim_id": 0, "number": "+447700900123"}]
        self.state = "device"
        self.booted = True
        self.now_ms = 1_760_000_000_000
        self.fail_su = False
        self.empty_uri = None      # this URI prints nothing, like a provider error
        self.commands = []
        self.sent_box = []

    def add_sms(self, _id, body, date=None, address="+447700900999"):
        self.now_ms += 1000
        self.sms.append({"_id": _id, "date": date or self.now_ms, "address": address, "body": body})

    def add_call(self, _id, type_=3, number="+447700900888", presentation=1, date=None):
        self.now_ms += 1000
        self.calls.append({"_id": _id, "date": date or self.now_ms, "type": type_,
                           "duration": 0, "presentation": presentation, "number": number})

    def table(self, uri):
        return {"content://sms/inbox": self.sms, "content://call_log/calls": self.calls,
                "content://telephony/siminfo": self.siminfo,
                "content://sms": self.sent_box}[uri]

    def content(self, argv):
        assert argv[:2] == ["content", "query"], argv
        opts = dict(zip(argv[2::2], argv[3::2]))
        if opts["--uri"] == self.empty_uri:
            return ""
        rows = list(self.table(opts["--uri"]))
        if "--where" in opts:
            expr = opts["--where"]
            expr = re.sub(r"(?<![<>!=])=(?!=)", "==", expr)
            expr = re.sub(r"\bAND\b", " and ", expr)
            expr = re.sub(r"\bOR\b", " or ", expr)
            expr = re.sub(r"\bIN\b", " in ", expr)
            rows = [r for r in rows if eval(expr, {}, dict(r))]
        cols = opts["--projection"].split(":")
        if not rows:
            return "No result found.\n"
        lines = []
        for i, r in enumerate(rows):
            lines.append(f"Row: {i} " + ", ".join(
                f"{c}={'NULL' if r.get(c) is None else r[c]}" for c in cols))
        return "\n".join(lines) + "\n"

    def adb_cmd(self, args, timeout=30, quiet=False):
        self.commands.append(args)
        if self.state != "device":
            raise sp.PhoneError("ADB error: device not found")
        if args[0] == "shell" and len(args) == 2 and args[1].startswith("su -c "):
            if self.fail_su:
                raise sp.PhoneError("ADB error: su: permission denied")
            inner = shlex.split(args[1])
            assert inner[:2] == ["su", "-c"] and len(inner) == 3, inner
            argv = shlex.split(inner[2])
            if argv[0] == "content":
                return self.content(argv)
            if argv[:2] == ["am", "startservice"]:
                self.sent_box.append({"_id": len(self.sent_box) + 1, "type": 2,
                                      "date": self.now_ms, "argv": argv})
                return "Starting service: Intent { ... }\n"
            if argv == ["id", "-u"]:
                return "0\n"
            raise AssertionError(f"unexpected su command {argv}")
        if args[:2] == ["shell", "getprop"]:
            return {"sys.boot_completed": "1" if self.booted else "",
                    "ro.product.model": "Pixel 6a", "gsm.operator.alpha": "EE,",
                    "gsm.network.type": "LTE,Unknown"}.get(args[2], "") + "\n"
        if args[:2] == ["shell", "date"]:
            return f"{self.now_ms // 1000}\n"
        if args[:3] == ["shell", "dumpsys", "battery"]:
            return "  level: 80\n"
        if args[:3] == ["shell", "dumpsys", "telephony.registry"]:
            return ("mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: rssi=-75 "
                    "rsrp=-97 rsrq=-11 level=3, mNr=CellSignalStrengthNr:{ ssRsrp = 2147483647 },"
                    "primary=CellSignalStrengthLte}\n")
        if args[:2] == ["shell", "cat"]:
            return "93600.5 1000.0\n"
        if args == ["reboot"]:
            self.state = None
            return ""
        raise AssertionError(f"unexpected adb {args}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sp.STATE_DIR = Path(self.tmp.name)
        sp.STATE_FILE = sp.STATE_DIR / "state.json"
        sp.CONFIG.clear()
        sp.CONFIG.update({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42",
                          "POLL_INTERVAL": 5, "ADB_DEVICE_SERIAL": ""})
        sp.STATE.clear()
        sp.pending_notices.clear()
        sp.phone.update(ready=False, status="startup", missing=0, alerted=False,
                        last_contact=None, reboot_deadline=0.0,
                        poll_failures={"sms": 0, "calls": 0}, read_alerted=set(),
                        drops=[], unstable_quiet_until=0.0)
        sp.next_poll = 0.0
        self.p = FakePhone()
        self.sent = []
        self.curl = []
        self.telegram_error = None
        sp.adb_cmd = self.p.adb_cmd
        sp.device_state = lambda: self.p.state
        sp.send_telegram = self.fake_send
        sp.send_telegram_curl = self.curl.append
        self.clock = sp.time = FakeTime()
        self.handlers = list(sp.logger.handlers)
        for h in self.handlers:
            sp.logger.removeHandler(h)
        self.errors = []
        test = self

        class Capture(sp.logging.Handler):
            def emit(self, record):
                if record.levelno >= sp.logging.ERROR:
                    test.errors.append(record.getMessage())
        self.capture = Capture()
        sp.logger.addHandler(self.capture)

    def tearDown(self):
        for name, value in ORIGINALS.items():
            setattr(sp, name, value)
        sp.logger.removeHandler(self.capture)
        for h in self.handlers:
            sp.logger.addHandler(h)
        self.tmp.cleanup()

    def fake_send(self, text):
        if self.telegram_error:
            err = self.telegram_error
            if callable(err):
                err = err(text)
            if err:
                raise err
        self.sent.append(text)
        return {"ok": True}

    def forwarded(self):
        return [m for m in self.sent if m.startswith(("\U0001f4e9 SMS", "\U0001f4de Missed"))]


class Parsing(Base):
    def test_body_cannot_fake_id(self):
        self.p.add_sms(1, "old")
        sp.phone_tick()  # baseline
        self.sent.clear()
        self.p.add_sms(2, "Your code is 123456, _id=99999999, seen=1")
        self.p.add_sms(3, "next code 654321")
        sp.phone_tick()
        msgs = self.forwarded()
        self.assertEqual(len(msgs), 2)
        self.assertIn("Your code is 123456, _id=99999999, seen=1", msgs[0])
        self.assertIn("654321", msgs[1])
        self.assertEqual(sp.STATE["sms"]["last_id"], 3)

    def test_body_with_comma_equals_and_newlines(self):
        sp.phone_tick()
        self.p.add_sms(1, "Code: 4321, valid=10 min\nRow: 5 _id=7, body=x\nend")
        sp.phone_tick()
        self.assertIn("Code: 4321, valid=10 min\nRow: 5 _id=7, body=x\nend", self.forwarded()[0])

    def test_null_address(self):
        sp.phone_tick()
        self.p.add_sms(1, "hi", address=None)
        sp.phone_tick()
        self.assertIn("SMS from unknown sender", self.forwarded()[0])


class Watermark(Base):
    def test_first_start_does_not_replay(self):
        for i in range(1, 6):
            self.p.add_sms(i, f"old {i}")
        sp.phone_tick()
        self.assertEqual(self.forwarded(), [])
        self.assertTrue(self.sent[0].startswith("✅ sim-proxy started"))
        self.assertEqual(sp.STATE["sms"]["last_id"], 5)

    def test_failed_first_init_does_not_store_zero(self):
        for i in range(1, 4):
            self.p.add_sms(i, f"old {i}")
        self.p.fail_su = True
        sp.phone_tick()
        self.assertNotIn("sms", sp.STATE)
        self.assertFalse(sp.STATE_FILE.exists())
        self.p.fail_su = False
        sp.phone_tick()
        self.assertEqual(self.forwarded(), [])
        self.assertEqual(sp.STATE["sms"]["last_id"], 3)

    def test_phone_replaced_ids_restart(self):
        for i in range(1, 75):
            self.p.add_sms(i, f"old {i}")
        sp.phone_tick()
        self.sent.clear()
        self.p.sms.clear()           # new phone, empty store, ids from 1
        self.p.add_sms(1, "code 111")
        self.p.add_sms(2, "code 222")
        sp.phone_tick()
        self.assertEqual(len(self.forwarded()), 2)
        sp.phone_tick()
        self.assertEqual(len(self.forwarded()), 2, "no duplicates on the next poll")

    def test_reused_id_after_delete(self):
        self.p.add_sms(1, "a")
        sp.phone_tick()
        self.p.add_sms(2, "otp 1")
        sp.phone_tick()
        self.p.sms = [r for r in self.p.sms if r["_id"] != 2]   # auto-deleted OTP
        self.p.add_sms(2, "otp 2")                              # SQLite reuses _id 2
        sp.phone_tick()
        self.assertEqual([m.split("\n")[1] for m in self.forwarded()], ["otp 1", "otp 2"])

    def test_equal_dates_after_phone_swap_survive_a_send_failure(self):
        for i in range(1, 11):
            self.p.add_sms(i, f"old {i}")
        sp.phone_tick()
        self.p.sms.clear()
        d = self.p.now_ms + 5000
        self.p.add_sms(1, "X", date=d)
        self.p.add_sms(2, "Y", date=d)
        calls = {"n": 0}
        def flaky(text):
            if "\nY\n" in text and calls["n"] == 0:
                calls["n"] += 1
                return sp.TelegramError("timeout")
            return None
        self.telegram_error = flaky
        sp.phone_tick()
        self.telegram_error = None
        sp.phone_tick()
        bodies = [m.split("\n")[1] for m in self.forwarded()]
        self.assertIn("Y", bodies)

    def test_legacy_import(self):
        for i in range(1, 6):
            self.p.add_sms(i, f"old {i}")
        self.p.add_call(1)
        (sp.STATE_DIR / "last_sms_id").write_text("4\n")
        (sp.STATE_DIR / "last_call_id").write_text("1\n")
        sp.phone_tick()
        # SMS 5 arrived after the old version stopped: it must be forwarded.
        self.assertEqual([m.split("\n")[1] for m in self.forwarded()], ["old 5"])
        self.assertEqual(sp.STATE["calls"]["last_id"], 1)
        self.assertFalse((sp.STATE_DIR / "last_sms_id").exists())
        self.assertTrue((sp.STATE_DIR / "last_sms_id.imported").exists())
        saved = json.loads(sp.STATE_FILE.read_text())
        self.assertEqual(saved["sms"]["last_id"], 5)

    def test_legacy_renamed_when_import_spans_two_ticks(self):
        for i in range(1, 6):
            self.p.add_sms(i, f"old {i}")
        (sp.STATE_DIR / "last_sms_id").write_text("5\n")
        (sp.STATE_DIR / "last_call_id").write_text("0\n")
        self.p.empty_uri = sp.CALLS_URI
        sp.phone_tick()
        self.p.empty_uri = None
        sp.phone_tick()
        names = sorted(p.name for p in sp.STATE_DIR.iterdir())
        self.assertIn("last_sms_id.imported", names)
        self.assertIn("last_call_id.imported", names)
        self.assertNotIn("last_sms_id", names)

    def test_broken_state_file_rebaselines(self):
        sp.STATE_FILE.write_text("{not json")
        with self.assertRaises(sp.StateError):
            sp.load_state()


class Telegram(Base):
    def test_long_sms_is_split_and_does_not_block(self):
        sp.phone_tick()
        self.p.add_sms(1, "x" * 9000)
        self.p.add_sms(2, "after")
        sp.phone_tick()
        self.assertTrue(all(sp.utf16_len(m) <= 4096 for m in self.sent))
        self.assertIn("after", self.sent[-1])
        self.assertEqual(sp.STATE["sms"]["last_id"], 2)

    def test_split_counts_utf16(self):
        chunks = sp.split_message("\U0001f600" * 3000)
        self.assertTrue(all(sp.utf16_len(c) <= sp.TELEGRAM_MAX_TEXT for c in chunks))
        self.assertEqual("".join(chunks), "\U0001f600" * 3000)

    def test_rejected_message_is_skipped_with_notice(self):
        sp.phone_tick()
        self.p.add_sms(1, "bad")
        self.p.add_sms(2, "good")
        self.telegram_error = lambda t: sp.TelegramRejected("HTTP 400 bad") if "\nbad\n" in t else None
        sp.phone_tick()
        self.assertTrue(any("Could not forward sms #1" in m for m in self.sent))
        self.assertIn("good", self.forwarded()[-1])
        self.assertEqual(sp.STATE["sms"]["last_id"], 2)

    def test_chat_level_400_does_not_skip(self):
        sp.phone_tick()
        self.p.add_sms(1, "code 1")
        self.telegram_error = sp.TelegramError("HTTP 400 Bad Request: chat not found")
        sp.phone_tick()
        self.assertEqual(sp.STATE["sms"]["last_id"], 0)
        self.telegram_error = None
        sp.phone_tick()
        self.assertEqual(len(self.forwarded()), 1)

    def test_rejected_notice_also_refused_does_not_skip(self):
        sp.phone_tick()
        self.p.add_sms(1, "code 1")
        self.telegram_error = sp.TelegramRejected("HTTP 400 something")
        sp.phone_tick()
        self.assertEqual(sp.STATE["sms"]["last_id"], 0)

    def test_chat_problem_classified(self):
        self.assertTrue(sp.CHAT_PROBLEM_RE.search("Bad Request: chat not found"))
        self.assertFalse(sp.CHAT_PROBLEM_RE.search("Bad Request: message is too long"))

    def test_network_error_keeps_message_for_retry(self):
        sp.phone_tick()
        self.p.add_sms(1, "code")
        self.telegram_error = sp.TelegramError("timed out")
        sp.phone_tick()
        self.assertEqual(sp.STATE["sms"]["last_id"], 0)
        self.telegram_error = None
        sp.phone_tick()
        self.assertEqual(len(self.forwarded()), 1)
        self.assertEqual(self.errors, [], "a Telegram outage is not a phone error")

    def test_startup_notice_retried_until_delivered(self):
        self.telegram_error = sp.TelegramError("down")
        sp.phone_tick()
        self.assertEqual(len(sp.pending_notices), 1)
        self.telegram_error = None
        sp.flush_notices()
        self.assertTrue(self.sent[0].startswith("✅ sim-proxy started"))


class PhoneHealth(Base):
    def test_missing_at_startup_alerts(self):
        self.p.state = None
        sp.phone_tick()
        self.assertIn("But the phone is not available: not connected", self.sent[0])
        sp.phone_tick()
        self.assertEqual(len(self.sent), 1, "alert only once")
        self.p.state = "device"
        sp.phone_tick()
        self.assertTrue(self.sent[1].startswith("\U0001f4f1 Phone is connected"))
        self.assertIn("sms", sp.STATE, "state initialised when the phone appears later")

    def test_disconnect_alert_needs_two_checks(self):
        sp.phone_tick()
        self.p.state = None
        sp.phone_tick()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(sp.phone["ready"], "one missed check keeps the phone ready")
        sp.phone_tick()
        self.assertIn("Phone is not responding!", self.sent[1])
        self.p.state = "unauthorized"
        sp.phone_tick()
        self.assertEqual(len(self.sent), 2)

    def test_unauthorized_state_is_named(self):
        sp.phone_tick()
        self.p.state = "unauthorized"
        sp.phone_tick()
        sp.phone_tick()
        self.assertIn("confirm the USB debugging prompt", self.sent[-1])

    def test_root_lost_alerts_once_then_recovers(self):
        sp.phone_tick()
        self.p.fail_su = True
        for _ in range(10):
            sp.phone_tick()
        alerts = [m for m in self.sent if "Cannot read SMS on the phone" in m]
        self.assertEqual(len(alerts), 1, self.sent)
        self.p.fail_su = False
        sp.phone_tick()
        self.assertIn("Reading SMS works again", self.sent[-1])

    def test_read_alert_survives_telegram_outage(self):
        sp.phone_tick()
        self.p.fail_su = True
        self.telegram_error = sp.TelegramError("down")
        for _ in range(5):
            sp.phone_tick()
        self.telegram_error = None
        sp.flush_notices()
        self.assertTrue(any("Cannot read SMS on the phone" in m for m in self.sent))

    def test_unreadable_at_startup_is_reported(self):
        self.p.fail_su = True
        for _ in range(5):
            sp.phone_tick()
        self.assertIn("But SMS cannot be read on the phone", self.sent[0])
        self.assertEqual(len(self.sent), 1, self.sent)
        self.p.fail_su = False
        sp.phone_tick()
        self.assertTrue(any(m.startswith("\U0001f4f1 Phone is connected") for m in self.sent))

    def test_empty_provider_output_is_an_error(self):
        for i in range(1, 31):
            self.p.add_sms(i, f"old {i}")
        self.p.empty_uri = sp.SMS_URI
        for _ in range(4):
            sp.phone_tick()
        self.assertNotIn("sms", sp.STATE, "no baseline from an empty answer")
        self.p.empty_uri = None
        sp.phone_tick()
        self.assertEqual(self.forwarded(), [])
        self.p.add_sms(31, "new")
        self.p.empty_uri = sp.SMS_URI
        for _ in range(3):
            sp.phone_tick()
        self.assertTrue(any("Cannot read SMS on the phone" in m for m in self.sent))

    def test_empty_output_during_legacy_import_does_not_replay(self):
        for i in range(1, 77):
            self.p.add_sms(i, f"old {i}")
        (sp.STATE_DIR / "last_sms_id").write_text("76\n")
        self.p.empty_uri = sp.SMS_URI
        sp.phone_tick()
        self.assertTrue((sp.STATE_DIR / "last_sms_id").exists())
        self.p.empty_uri = None
        sp.phone_tick()
        self.assertEqual(self.forwarded(), [])

    def test_broken_call_log_does_not_hold_back_sms(self):
        for i in range(1, 77):
            self.p.add_sms(i, f"old {i}")
        (sp.STATE_DIR / "last_sms_id").write_text("76\n")
        (sp.STATE_DIR / "last_call_id").write_text("0\n")
        self.p.empty_uri = sp.CALLS_URI
        sp.phone_tick()
        self.assertIn("Call log cannot be read", self.sent[0])
        self.assertIn("sms", json.loads(sp.STATE_FILE.read_text()), "SMS state saved at once")
        self.assertTrue((sp.STATE_DIR / "last_sms_id.imported").exists())
        self.p.add_sms(77, "code 777")
        for _ in range(5):
            sp.phone_tick()
        self.assertEqual([m.split("\n")[1] for m in self.forwarded()], ["code 777"])
        self.assertFalse(any("Cannot read the call log" in m for m in self.sent),
                         "already told in the startup report")
        self.p.empty_uri = None
        self.p.add_call(1)
        sp.phone_tick()
        self.assertTrue(any("Reading the call log works again" in m for m in self.sent))
        sp.phone_tick()
        self.assertTrue(any("Missed call" in m for m in self.forwarded()))

    def test_restart_while_call_log_broken_keeps_sms(self):
        for i in range(1, 31):
            self.p.add_sms(i, f"old {i}")
        self.p.empty_uri = sp.CALLS_URI
        sp.phone_tick()
        self.p.add_sms(31, "arrived meanwhile")
        # restart: memory is gone, state.json is what survives
        sp.STATE.clear()
        sp.STATE.update(sp.load_state())
        sp.phone.update(ready=False, status="startup")
        sp.phone_tick()
        self.assertTrue(any("arrived meanwhile" in m for m in self.forwarded()))

    def test_sms_alert_after_calls_alert(self):
        sp.phone_tick()
        self.p.empty_uri = sp.CALLS_URI
        for _ in range(3):
            sp.phone_tick()
        self.assertTrue(any("Cannot read the call log" in m for m in self.sent))
        self.p.fail_su = True
        for _ in range(3):
            sp.phone_tick()
        self.assertTrue(any("Cannot read SMS on the phone" in m for m in self.sent))

    def test_short_blip_is_silent_flapping_warns(self):
        sp.phone_tick()
        self.sent.clear()
        self.p.state = None
        sp.phone_tick()
        self.p.state = "device"
        sp.phone_tick()
        self.assertEqual(self.sent, [], "one missed check says nothing")
        for _ in range(2):
            self.p.state = None
            sp.phone_tick()
            self.p.state = "device"
            sp.phone_tick()
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertIn("keeps dropping", self.sent[0])

    def test_reboot_is_not_an_alarm(self):
        sp.phone_tick()
        sp.handle_reboot()
        for _ in range(5):
            sp.phone_tick()
        self.assertFalse(any("not responding" in m for m in self.sent))
        self.p.state = "device"
        self.p.booted = False
        sp.phone_tick()
        self.p.booted = True
        sp.phone_tick()
        self.assertTrue(self.sent[-1].startswith("\U0001f4f1 Phone is back after the reboot"))

    def test_reboot_that_never_returns_alerts(self):
        sp.phone_tick()
        sp.handle_reboot()
        sp.phone_tick()                      # gone, as expected
        sp.phone["reboot_deadline"] = sp.time.monotonic() - 1
        sp.phone_tick()                      # still gone after the grace period
        self.assertTrue(any("did not come back" in m for m in self.sent))

    def test_health_report(self):
        sp.phone_tick()
        r = self.sent[0]
        self.assertIn("Number: +447700900123", r)
        self.assertIn("Signal: LTE (-97 dBm, fair)", r)
        self.assertIn("Operator: EE", r)
        self.assertIn("SMS access: ok", r)

    def test_number_not_stored_and_old_sim_ignored(self):
        self.p.siminfo = [{"sim_id": -1, "number": "+447000000000"}, {"sim_id": 0, "number": None}]
        self.assertIsNone(sp.get_sim_number())


class Calls(Base):
    def test_missed_call_forwarded_hidden_number(self):
        sp.phone_tick()
        self.p.add_call(1, type_=1)
        self.p.add_call(2, type_=3, number="", presentation=2)
        sp.phone_tick()
        self.assertEqual(len(self.forwarded()), 1)
        self.assertIn("Missed call from withheld number", self.forwarded()[0])


class Commands(Base):
    def cmd(self, text, age=0):
        update = {"update_id": 1, "message": {"text": text, "chat": {"id": 42},
                                              "date": int(sp.time.time()) - age}}
        sp.get_telegram_updates = lambda timeout=0: [update]
        self.assertTrue(sp.process_commands())

    def test_stale_command_not_run(self):
        sp.phone_tick()
        self.cmd("/reboot", age=3600)
        self.assertNotIn(["reboot"], self.p.commands)
        self.assertIn("Ignored /reboot", self.sent[-1])
        self.assertIn("reached sim-proxy late", self.sent[-1])

    def test_ussd_rejects_plain_number(self):
        self.cmd("/ussd 100")
        self.assertIn("Usage: /ussd", self.sent[-1])
        self.assertFalse(any("android.intent.action.CALL" in a for c in self.p.commands for a in c))
        self.assertTrue(sp.USSD_CODE_RE.match("*100#"))
        self.assertTrue(sp.USSD_CODE_RE.match("*#06#"))
        self.assertFalse(sp.USSD_CODE_RE.match("+447700900123"))

    def test_sms_quoting_and_confirmation(self):
        self.cmd("/sms +447700900123 it's \"fine\"; $(id) & done")
        argv = self.p.sent_box[-1]["argv"]
        self.assertIn("smsto:+447700900123", argv)
        self.assertIn("it's \"fine\"; $(id) & done", argv)
        self.assertIn("SMS to +447700900123 sent", self.sent[-1])

    def test_sms_status_read_error_is_not_a_failure(self):
        real = self.p.content
        def flaky(argv):
            if "content://sms" in argv and "--uri" in argv and argv[argv.index("--uri") + 1] == "content://sms":
                return ""
            return real(argv)
        self.p.content = flaky
        self.cmd("/sms +447700900123 YES")
        self.assertIn("probably sent", self.sent[-1])
        self.assertNotIn("failed", self.sent[-1])

    def test_inbox_capped_and_split(self):
        for i in range(1, 40):
            self.p.add_sms(i, "y" * 500)
        self.cmd("/inbox 500")
        self.assertTrue(all(sp.utf16_len(m) <= 4096 for m in self.sent))
        self.assertIn("Last 20 SMS", self.sent[0])

    def test_inbox_zero_means_default(self):
        self.p.add_sms(1, "a")
        self.cmd("/inbox 0")
        self.assertIn("Last 1 SMS", self.sent[-1])

    def test_status(self):
        self.cmd("/status")
        self.assertIn("Phone uptime: 1d 2h", self.sent[-1])
        self.assertIn("sim-proxy running for", self.sent[-1])

    def test_start_shows_help(self):
        self.cmd("/start")
        self.assertIn("Commands:", self.sent[-1])

    def test_command_error_reported_once(self):
        self.p.fail_su = True
        self.cmd("/inbox")
        self.assertEqual(len(self.errors), 0)
        self.assertIn("/inbox failed", self.sent[-1])


class MainLoop(Base):
    def test_no_busy_spin_when_telegram_fails_fast(self):
        sp.get_telegram_updates = lambda timeout=0: None
        calls = {"n": 0}
        real_tick = sp.phone_tick

        def tick():
            calls["n"] += 1
            if calls["n"] >= 3:
                raise KeyboardInterrupt
            real_tick()
        sp.phone_tick = tick
        try:
            sp.main_loop()
        except KeyboardInterrupt:
            pass
        sleeps = self.clock.sleeps
        self.assertTrue(sleeps and all(s >= 1.0 for s in sleeps), sleeps)


class Config(Base):
    def test_poll_interval_validated(self):
        env = Path(self.tmp.name) / ".env"
        sp.SCRIPT_DIR = Path(self.tmp.name)
        env.write_text("TELEGRAM_BOT_TOKEN=1:a\nTELEGRAM_CHAT_ID=1\nPOLL_INTERVAL=0\n")
        c = sp.load_config()
        self.assertEqual(c["POLL_INTERVAL"], 2)
        self.assertTrue(c["WARNINGS"])
        env.write_text("TELEGRAM_BOT_TOKEN=1:a\nTELEGRAM_CHAT_ID=1\nPOLL_INTERVAL=15s\n")
        self.assertEqual(sp.load_config()["POLL_INTERVAL"], 10)
        env.write_text("TELEGRAM_BOT_TOKEN=1:a\nTELEGRAM_CHAT_ID=1\n")
        self.assertEqual(sp.load_config()["POLL_INTERVAL"], 10)

    def test_proxy_from_env_file(self):
        env = Path(self.tmp.name) / ".env"
        sp.SCRIPT_DIR = Path(self.tmp.name)
        env.write_text("TELEGRAM_BOT_TOKEN=1:a\nTELEGRAM_CHAT_ID=1\nTELEGRAM_PROXY=http://127.0.0.1:10809\n")
        saved = {k: sp.os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")}
        try:
            sp.load_config()
            self.assertEqual(sp.os.environ["https_proxy"], "http://127.0.0.1:10809")
        finally:
            for k, v in saved.items():
                if v is None:
                    sp.os.environ.pop(k, None)
                else:
                    sp.os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
