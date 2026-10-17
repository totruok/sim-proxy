# sim-proxy

Forward SMS and missed calls from a rooted Android phone to Telegram, and
control the phone from Telegram.

The phone lives on a shelf, plugged into a small always-on computer (a
Raspberry Pi, any Linux box or a Mac) over USB. sim-proxy runs on that
computer and talks to the phone through ADB. Nothing is installed on the
phone apart from root.

It is meant for a SIM that has to stay in one place while you don't: a
number that receives 2FA codes, bank notifications or calls from people who
only know that number.

## How it works

```
 SIM ─▶ Android phone ──USB (adb)──▶ server running ──HTTPS──▶ Telegram ──▶ your chat
        rooted, Magisk               sim_proxy.py              Bot API
                        ◀──────────── /sms, /status, /inbox ... ◀──────────────
```

- Every `POLL_INTERVAL` seconds sim-proxy reads the SMS inbox and the call
  log with `adb shell su -c 'content query ...'`.
- It remembers the id of the last forwarded SMS and missed call in
  `~/.sim-proxy/`. The id moves forward only after Telegram has accepted the
  message, so if the server, the phone or the network goes down, nothing is
  lost: once everything is back, every SMS newer than that id is sent.
- On the very first start it records the current newest id and does not
  flood you with old messages.
- Commands are accepted only from the chat in `TELEGRAM_CHAT_ID`. Messages
  from anyone else are ignored without a reply.

More detail: [docs/how-it-works.md](docs/how-it-works.md).

## Features

- Incoming SMS forwarded in full, including multi-line messages
- Missed call notifications
- Send an SMS from Telegram (`/sms`), through Google Messages, without
  touching the phone
- Health check on start and after the phone reconnects: model, SIM number,
  battery, operator, signal in dBm
- Alert when the phone stops responding; error log lines are sent to the
  chat as well
- Runs as a LaunchAgent (macOS) or a systemd user service (Linux) and
  restarts on failure
- Python standard library only, no dependencies

## Telegram commands

| Command | What it does |
|---|---|
| `/status` | Model, SIM number, battery, operator, signal, uptime |
| `/sms +447700900123 text` | Send an SMS from the phone |
| `/inbox [N]` | Last N SMS (default 5) |
| `/calls [N]` | Last N calls (default 5) |
| `/ussd *100#` | Send a USSD request and try to capture the reply |
| `/reboot` | Reboot the phone |
| `/help` | List of commands |

## Requirements

**Phone**
- Android phone with an unlockable bootloader, rooted with
  [Magisk](https://github.com/topjohnwu/Magisk). Tested on a Pixel 6a with
  Android 16.
- Google Messages as the default SMS app (only needed for `/sms`).
- A data-capable USB cable.

**Server**
- Linux (tested on Raspberry Pi OS / Debian) or macOS
- Python 3.8+
- `adb` from Android SDK platform-tools
- `curl`

**Telegram**
- A bot token from [@BotFather](https://t.me/BotFather)
- Your chat id (ask [@userinfobot](https://t.me/userinfobot))

## Setup

1. **Prepare the phone:** [docs/android-setup.md](docs/android-setup.md).
   Unlocking the bootloader wipes the phone, so do this first.
2. **Prepare the server:** [docs/server-setup.md](docs/server-setup.md).
3. **Install:**

   ```bash
   git clone https://github.com/totruok/sim-proxy.git
   cd sim-proxy
   ./install.sh
   ```

   `install.sh` checks for `adb`, `python3` and a connected phone, asks for
   the bot token, chat id, device serial and poll interval, writes `.env`,
   sends a test message to Telegram, then installs and starts the service.

4. **Check it end to end:** you should get a health check message, `/status`
   should answer, and a real SMS sent to the SIM should arrive in the chat.
   The full checklist is in [docs/server-setup.md](docs/server-setup.md#5-verify-it-for-real).

## Configuration

`.env` next to `sim_proxy.py` (see [.env.example](.env.example)):

| Variable | Meaning | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | yes |
| `TELEGRAM_CHAT_ID` | The only chat that receives messages and may send commands | yes |
| `ADB_DEVICE_SERIAL` | Serial from `adb devices`, needed only if several phones are connected | no |
| `POLL_INTERVAL` | Seconds between checks, default 30. For 2FA codes use 5 to 10 | no |

## Managing the service

**macOS**

```bash
launchctl unload ~/Library/LaunchAgents/com.sim-proxy.plist   # stop
launchctl load ~/Library/LaunchAgents/com.sim-proxy.plist     # start
```

**Linux**

```bash
systemctl --user status sim-proxy
systemctl --user restart sim-proxy
journalctl --user -u sim-proxy -f
```

**Logs** (both): `tail -f ~/.sim-proxy/sim-proxy.log`

## Security model

Read this before you point a SIM that receives 2FA codes at it.

- **Your Telegram account becomes the second factor.** Whoever controls the
  chat gets every SMS, can read the whole inbox with `/inbox` and can send
  SMS from your number. Protect the account: cloud password on, no unknown
  sessions.
- **The bot token is a secret.** Keep `.env` out of git (it is in
  `.gitignore`). With the token someone can impersonate the bot.
- **The phone is wide open on purpose.** It is rooted, has USB debugging on,
  no screen lock and no SIM PIN, otherwise it could not recover on its own
  after a power cut. Anyone who can touch it owns it and the SIM, so keep it
  somewhere physically safe.
- **The server controls the phone.** The adb key in `~/.android/adbkey` gives
  root on the phone to anyone with a shell on the server.
- **SMS text goes through Telegram's servers.** Bot chats are not end-to-end
  encrypted.
- sim-proxy only makes outgoing HTTPS requests. It opens no ports.

## Troubleshooting

- **Commands are lost, Telegram returns `409 Conflict`:** two programs are
  polling the same bot token. Run exactly one instance per bot; use a separate
  bot for each phone.
- **macOS: `protocol fault (couldn't read status)` or `daemon not running`
  errors:** the adb server is crashing. `install.sh` sets `ADB_LIBUSB=0` for
  the service; check that it is in effect with `adb server-status` (should say
  `usb_backend: NATIVE`). Details in
  [docs/server-setup.md](docs/server-setup.md#troubleshooting).
- **`unauthorized` in `adb devices`:** confirm the "Allow USB debugging"
  prompt on the phone and tick "Always allow".
- More in [docs/android-setup.md](docs/android-setup.md#troubleshooting) and
  [docs/server-setup.md](docs/server-setup.md#troubleshooting).

## License

[MIT](LICENSE)
