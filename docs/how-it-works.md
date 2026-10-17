# How it works

Implementation notes for anyone reading or changing `sim_proxy.py`. Setup
instructions are in [android-setup.md](android-setup.md) and
[server-setup.md](server-setup.md).

## Main loop

```
while running:
    process_commands(timeout=POLL_INTERVAL)   # Telegram long poll, doubles as the sleep
    if now < next_device_check: continue
    if phone connected: poll SMS and missed calls
    else: alert once, check again in 60 s
```

`getUpdates` is called with `timeout=POLL_INTERVAL`: a command is handled as
soon as it arrives, and when nothing arrives the call returns after
`POLL_INTERVAL` seconds and the phone is polled. So an incoming SMS reaches
Telegram within about `POLL_INTERVAL` seconds plus a second or two of ADB.

## Delivery and state

- SMS: `adb shell su -c 'content query --uri content://sms/inbox'`.
- Calls: `content://call_log/calls`; `type=3` is a missed call
  (1 incoming, 2 outgoing).
- The last forwarded ids are stored in `~/.sim-proxy/last_sms_id` and
  `~/.sim-proxy/last_call_id`, written atomically (temp file and rename).
- Each new row is sent to Telegram first, and only then is its id written.
  If sending fails, the loop stops and the same rows are retried on the next
  poll. A crash or outage can therefore delay a message or, in a rare race,
  send it twice, but it does not drop it while the phone still has it.
- If a state file is missing at start, it is initialised with the current
  newest id, so a fresh install does not replay the whole inbox.

## Parsing `content query`

The output looks like `Row: N key=value, key=value, ...`.

- **Multi-line SMS:** the body can contain newlines. Lines that do not start
  with `Row: N` continue the previous row and are joined before parsing.
- **Commas in the body:** splitting on `, ` breaks on ordinary text. Instead
  the parser finds every `key=` with `(?:^|, )([a-zA-Z_]\w*)=` and takes the
  values between them.
- `date` is a Unix timestamp in **milliseconds**.
- Sorting is done in Python: `--sort` on the device does not survive the
  layers of shell quoting.

## Sending SMS

`service call isms ...` from a root shell does not work on Android 14 and
newer: the message shows up as sent but never leaves the phone. What works is
Google Messages' `NoConfirmationSmsSendService`:

```sh
am startservice -a android.intent.action.RESPOND_VIA_MESSAGE \
  -d "smsto:$NUMBER" --es android.intent.extra.TEXT "$TEXT" \
  -n com.google.android.apps.messaging/.shared.NoConfirmationSmsSendService
```

This is why Google Messages has to be the default SMS app. At start
sim-proxy pushes this as a small helper script to
`/data/local/tmp/sim-proxy-sendsms.sh` (`$1` is the number, the rest is the
text) and `/sms` calls the helper.

## Shell quoting

A command passes through several shells: Python → `adb` → the device shell →
`su` → `sh -c`. `adb` joins its arguments with spaces and the device shell
parses the result again, so arguments are not preserved as they are.

- `adb_shell(command)` sends `su -c '<command>'`, with the whole command as
  one quoted word, so the device shell hands it to `su` intact and `su` runs
  it with `sh -c`.
- Anything user-supplied inside `command` is quoted with `shlex.quote`. The
  SMS text is quoted this way, and the number must match `^\+?\d{3,15}$`.
- `/ussd` codes go into an `am start` command line and are limited to
  dial-pad characters (`0-9 * # +`).

## Signal strength

From `dumpsys telephony.registry`: find `primary=CellSignalStrength<Type>`
(for example `CellSignalStrengthLte`), then read `rsrp=` from the matching
`m<Type>=` section. Do not take the first `level=` in the output: it can
belong to an inactive radio. RSRP is graded with the AOSP thresholds:
≥ -85 excellent, ≥ -95 good, ≥ -105 fair, ≥ -115 weak, below that very weak.

## SIM number

Tried in order:

1. `dumpsys iphonesubinfo` (root): `Phone Number = +XXX`
2. `content://telephony/siminfo`: the `number` column
3. `getprop gsm.sim.operator.numeric`: only MCC/MNC, as a last resort

Many SIMs do not store their own number, so the result can be missing.

## Errors and alerts

- ADB cannot see the phone: one alert to Telegram with the time of the last
  contact, then a check every 60 seconds; commands keep working meanwhile.
  When the phone is back, a new health check is sent.
- Everything is logged to `~/.sim-proxy/sim-proxy.log` (rotated, 5 MB × 3).
  `ERROR` lines are also sent to the chat, through `curl` so that a broken
  `urllib` path can still report itself.
- Single `getUpdates` failures are normal on a flaky link and stay in the
  log. After 5 failures in a row one error goes to the chat, and a recovery
  message follows when polling works again.
