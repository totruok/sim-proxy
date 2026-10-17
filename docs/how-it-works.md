# How it works

Implementation notes for anyone reading or changing `sim_proxy.py`. Setup
instructions are in [android-setup.md](android-setup.md) and
[server-setup.md](server-setup.md).

## Main loop

```
while True:
    long-poll Telegram until the next phone check is due, handle commands
    if Telegram failed: pause (up to POLL_INTERVAL) instead of spinning
    send queued notices
    every POLL_INTERVAL: check the phone, forward new SMS and missed calls
```

`getUpdates` waits for commands until the next phone check is due, so a
command is handled as soon as it arrives and the phone is still checked
every `POLL_INTERVAL` seconds, however many messages reach the bot. `/sms`
and `/ussd` wait up to 30 seconds for a result and keep checking the phone
while they wait. An SMS
gets to Telegram within about `POLL_INTERVAL` seconds plus a couple of
seconds of ADB.

## Reading the phone safely

`content query` prints rows as `Row: N col=value, col=value` and does not
escape values. An SMS body can therefore contain text that looks like
another column or another row. sim-proxy never parses a body next to other
columns it relies on:

1. It lists matching rows with only digit columns:
   `--projection _id:date --where "<condition>"`. Any line that is not
   exactly `Row: N _id=<digits>, date=<digits>` is an error.
2. It reads each row on its own (`--where "_id=<id>"`) with the one
   free-text column last (`body` for SMS, `number` for calls) and takes
   everything after `body=` as the body, newlines included.

Sorting and filtering happen in the `--where` clause or in Python.

## Delivery and state

- SMS come from `content://sms/inbox`; missed calls from
  `content://call_log/calls` with `type=3`.
- `~/.sim-proxy/state.json` holds, for SMS and for calls, the highest `_id`
  and the highest `date` forwarded so far. It is written atomically (temp
  file, fsync, rename).
- A row is new if its `_id` **or** its `date` is above the mark. The `_id`
  alone is not enough: a replaced or wiped phone starts again from `_id` 1,
  and SQLite hands the highest `_id` out again after that row is deleted
  (for example when Messages deletes old one-time codes).
- Each new row is sent to Telegram first, and only then is the mark moved
  past it. If sending fails, the same row is tried again on the next poll. A
  crash at the wrong moment can send a message twice, but never drops it
  while the phone still has it.
- Messages longer than Telegram's limit are split. If Telegram refuses a
  message outright (HTTP 400), a short notice is sent instead and the mark
  moves on, so one bad message cannot hold back everything after it. A
  refusal about the chat itself ("chat not found", missing rights), or one
  that hits the notice as well, keeps the message for another try.
- A query that prints nothing at all is treated as a failure, not as "no
  messages": the `content` tool reports provider errors only on stderr.
- SMS and missed calls are set up, saved and polled separately, so a call
  log that cannot be read never holds back SMS.
- With no state yet, everything already on the phone counts as forwarded,
  so a fresh install does not replay the inbox. If the phone cannot be read
  at that moment, nothing is recorded and it is tried again later. The
  single-number files of older versions (`last_sms_id`, `last_call_id`) are
  imported once and renamed to `*.imported`.

## Sending SMS

`service call isms ...` from a root shell does not work on Android 14 and
newer: the message shows up as sent but never leaves the phone. What works is
Google Messages' `NoConfirmationSmsSendService`:

```sh
am startservice -a android.intent.action.RESPOND_VIA_MESSAGE \
  -d "smsto:$NUMBER" --es android.intent.extra.TEXT "$TEXT" \
  -n com.google.android.apps.messaging/.shared.NoConfirmationSmsSendService
```

This is why Google Messages has to be the default SMS app. After handing the
SMS over, sim-proxy watches the system SMS store for up to 30 seconds and
reports what happened to it: sent (`type=2`), failed (`type=5`), or still
queued.

## Shell quoting

A command passes through several shells: Python → `adb` → the device shell →
`su` → `sh -c`. `adb` joins its arguments with spaces and the device shell
parses the result again, so arguments are not preserved as they are.

- `adb_shell(command)` sends `su -c '<command>'`, with the whole command as
  one quoted word, so the device shell hands it to `su` intact and `su` runs
  it with `sh -c`.
- Anything user-supplied inside `command` is quoted with `shlex.quote`. The
  SMS text is quoted this way, and the number must match `^\+?\d{3,15}$`.

## USSD

`/ussd` accepts only USSD and other MMI codes (`*...#` or `#...#`): anything
else would be dialled as an ordinary voice call. The code is sent with an
`android.intent.action.CALL` intent after waking the screen. The phone shows
the operator's reply in a dialog, so sim-proxy reads the screen with
`uiautomator dump` every few seconds until a dialog with a button appears,
returns its text and closes it with the back key.

## Signal strength

From `dumpsys telephony.registry`: find `primary=CellSignalStrength<Type>`,
then read `rsrp=` from the `mLte=` section (LTE) or `ssRsrp` from the `mNr=`
section (5G). Do not take the first `level=` in the output: it can belong to
an inactive radio. Values outside -140 to -43 dBm, such as 2147483647, mean
"not available" and are not shown. RSRP is graded with the AOSP thresholds:
≥ -85 excellent, ≥ -95 good, ≥ -105 fair, ≥ -115 weak, below that very weak.

## SIM number

Read from `content://telephony/siminfo`, from the row of the SIM that is in
the phone now (`sim_id` 0 or above; SIMs seen before have -1). Many SIMs do
not store their own number; the health check then says so.

## Alerts and notices

- **Startup:** "sim-proxy started" with a health check, or a warning that
  the phone is not available.
- **Phone gone:** after two checks in a row without a usable phone, one alert
  that names the adb state (`unauthorized`, `offline`, not listed). When it
  is back, "Phone is connected" with a new health check. A single missed
  check is ignored, but three of them within ten minutes give one warning
  that the USB connection is unstable. After `/reboot` an absent phone is
  expected for four minutes and does not raise an alarm.
- **Phone present but unreadable** (root lost, SMS store failing, a query
  that prints nothing at all): one alert after three failed polls in a row,
  and a message when it works again. SMS and the call log are tracked
  separately; a call log alert says that SMS forwarding goes on, and when
  SMS cannot be read either, the SMS alert covers both.
- **Telegram unreachable:** single `getUpdates` failures stay in the log;
  after 5 in a row one error goes out, and a message follows when polling
  works again. Notices created while Telegram is down are queued and sent
  once it is back.
- **Stale commands:** Telegram keeps undelivered messages for 24 hours. A
  command older than five minutes reached sim-proxy late, because it was not
  running or could not reach Telegram; it is not executed, and the bot says
  so.
- **Unexpected errors** are sent to the chat, the same one at most every ten
  minutes.

Everything is logged to `~/.sim-proxy/sim-proxy.log` (rotated, 5 MB × 3).
Log lines are also printed to the console when there is one or when running
under systemd (the journal rotates them); under launchd the console is a
file that never rotates, so nothing is printed there. `ERROR` lines are sent
to the chat through `curl`, so that a broken `urllib` path can still report
itself.
