# Setting up the server and the Telegram bot

The server is the computer the phone is plugged into over USB: Linux (tested
on Raspberry Pi OS / Debian) or macOS. sim-proxy runs here.

The phone should already be prepared as described in
[android-setup.md](android-setup.md).

## 1. adb on the server

**Linux (Debian, Ubuntu, Raspberry Pi OS)**

```bash
sudo apt install adb
```

Without a udev rule Linux shows the phone as `no permissions`:

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", MODE="0660", GROUP="plugdev"' \
  | sudo tee /etc/udev/rules.d/51-android.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb
adb kill-server && adb devices
```

`18d1` is Google's USB vendor id (Pixel). For another manufacturer take the
vendor id from `lsusb`. Your user must be in the `plugdev` group.

**macOS**

```bash
brew install --cask android-platform-tools
```

Then get `adb devices` to show `device`. If it says `unauthorized`, accept
the prompt on the phone ("Always allow"). Check root with
`adb shell su -c id` (should print `uid=0`). Note the serial: it goes into
`.env` if more than one phone is connected.

## 2. Telegram bot

- **New bot:** [@BotFather](https://t.me/BotFather) → `/newbot` → copy the
  token. Get your chat id from [@userinfobot](https://t.me/userinfobot).
- **One bot per running instance.** If two programs poll `getUpdates` with
  the same token, Telegram answers one of them with `409 Conflict` and
  updates go to whichever asked last, so commands get lost. With several
  phones, create a bot for each. If you reuse an existing bot, stop whatever
  used it before, for good (`systemctl --user disable --now <old-unit>` or
  the equivalent).

## 3. If api.telegram.org is blocked where the server is

sim-proxy talks to Telegram with `urllib` and `curl`, and both honour the
`http_proxy` / `https_proxy` environment variables. Any local HTTP proxy that
gets past the block will do, for example xray with an HTTP inbound on
`127.0.0.1:10809`.

**Before installing**, check the path by hand:

```bash
curl -x http://127.0.0.1:10809 "https://api.telegram.org/bot<TOKEN>/getMe"
# {"ok":true,...}
```

Do not go further until `getMe` works.

**Linux:** add the proxy to the service with a drop-in, so it survives the
installer rewriting the unit:

```bash
mkdir -p ~/.config/systemd/user/sim-proxy.service.d
cat > ~/.config/systemd/user/sim-proxy.service.d/proxy.conf <<EOF
[Service]
Environment=HTTP_PROXY=http://127.0.0.1:10809
Environment=HTTPS_PROXY=http://127.0.0.1:10809
Environment=http_proxy=http://127.0.0.1:10809
Environment=https_proxy=http://127.0.0.1:10809
EOF
systemctl --user daemon-reload
```

Both spellings are set because different tools look at different ones.

**macOS:** after running `install.sh`, add the same variables to the
`EnvironmentVariables` dictionary in
`~/Library/LaunchAgents/com.sim-proxy.plist`, then unload and load the agent.

Export the variables in the shell you run `install.sh` from as well: its
test message is sent with `curl` and has to go through the proxy too.

## 4. Install sim-proxy

```bash
git clone https://github.com/totruok/sim-proxy.git
cd sim-proxy
./install.sh
```

`install.sh` checks `adb` and `python3`, asks for the token, chat id, serial
and poll interval, writes `.env`, sends a test message to Telegram, creates
the service (a systemd user unit on Linux, a LaunchAgent on macOS) and starts
it. If you prefer to fill in `.env` yourself, copy `.env.example` to `.env`
first and answer `n` when the installer asks whether to overwrite it.

Things to know:

- **Non-interactive runs:** `adb shell` inside `install.sh` reads stdin, so
  `echo n | ./install.sh` does not work. Use `yes n | ./install.sh`.
- **Linux:** to keep the `systemd --user` service running without an open
  SSH session and across reboots:

  ```bash
  sudo loginctl enable-linger $USER
  ```

- **macOS: the service needs a logged-in user.** A LaunchAgent starts only
  when its user logs in. After a reboot, sim-proxy stays down until someone
  logs in, so on a headless Mac turn on automatic login (System Settings →
  Users & Groups → Automatically log in as). macOS allows automatic login
  only with FileVault turned off; decide which trade-off you prefer.
- **macOS: adb USB backend.** The generated LaunchAgent sets `ADB_LIBUSB=0`,
  which makes adb use macOS's native USB stack instead of libusb. With
  libusb, the adb server crashes whenever any other USB device on the Mac
  re-enumerates, and every crash drops the connection to the phone. The
  variable applies to the adb server that sim-proxy starts; if you start the
  adb server yourself from a terminal first, set it there too
  (`export ADB_LIBUSB=0`) or run `adb kill-server` and let sim-proxy start it.

## 5. Verify it for real

1. A health check arrived in Telegram when the service started: model, SIM
   number, battery, operator, signal.
2. `/status` to the bot gets an answer within `POLL_INTERVAL` seconds.
3. **A real SMS over the mobile network** reaches the chat. Send one from
   another phone, or without a second phone send one to the SIM itself:
   `/sms <this SIM's number> test`. It leaves the SIM and comes back as an
   incoming SMS. Outgoing SMS need credit on the SIM.
4. The logs are clean:

   ```bash
   tail -f ~/.sim-proxy/sim-proxy.log
   journalctl --user -u sim-proxy -f      # Linux
   ```

5. Reconnect test: unplug and replug the USB cable. A "phone is not
   responding" alert should arrive, followed by a new health check once the
   phone is back. No new USB debugging approval is needed.

## 6. Remote control of phone power (Raspberry Pi)

A Raspberry Pi can switch USB port power in software. That is the remote
equivalent of unplugging and replugging the cable, and the only way to wake
a phone that is off with the cable plugged in (see `off-mode-charge` in
[android-setup.md](android-setup.md#4-power-on-automatically-when-power-returns)).

On a Raspberry Pi 4:

```bash
sudo apt install uhubctl
sudo uhubctl -l 1-1 -a off && sudo uhubctl -l 2 -a off   # power off
sleep 15
sudo uhubctl -l 1-1 -a on && sudo uhubctl -l 2 -a on     # power applied
# a phone that is off starts booting by itself (about 25 s to full boot)
```

The Pi switches all ports together, so make sure nothing else important is
plugged into USB.

This is also the honest way to test a power cut: shut the phone down, cut
power, wait, restore it. The phone should boot by itself, the SIM should
register (no SIM PIN), and sim-proxy should send a health check.

## Troubleshooting

- **`unauthorized`:** the phone needs a tap on the USB debugging prompt. The
  server's key is in `~/.android/adbkey*`; a fresh adb install means a new
  key and a new prompt.
- **SMS arrive twice or commands get lost:** two programs are polling the
  same bot token (`409 Conflict` in the log). Find and stop the other one
  (section 2).
- **Health check works but `/sms` does not send:** check the SIM's credit.
  The operator usually answers with an incoming SMS, which lands in the same
  chat.
- **macOS: `protocol fault (couldn't read status)`, empty `ADB error`
  messages, `daemon not running; starting now`:** the adb server is
  crashing. Run `adb server-status`: it should say `usb_backend: NATIVE`. If
  it says `LIBUSB`, an adb server was started without `ADB_LIBUSB=0`; run
  `adb kill-server` and restart sim-proxy. Crash reports are in
  `~/Library/Logs/DiagnosticReports/adb-*.ips`.
- **"Phone is not responding" and it does not come back:** check the cable
  and the port, then cycle USB power with `uhubctl` (section 6).
- **After a server reboot the bot is silent:** on Linux,
  `loginctl enable-linger` is missing; on macOS, nobody is logged in
  (section 4).
