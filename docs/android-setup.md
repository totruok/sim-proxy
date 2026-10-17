# Setting up the Android phone

How to turn an Android phone into an SMS and call relay that sits on a
shelf 24/7, plugged into the server over USB, and is managed only through
ADB and Telegram.

Tested on a Pixel 6a (bluejay, Android 16) with a Raspberry Pi and with a
Mac. All `adb` and `fastboot` commands run on the server the phone is plugged
into. Install `adb` there first: [server-setup.md](server-setup.md#1-adb-on-the-server).

> **Unlocking the bootloader erases the phone.** Use a phone with nothing on
> it that you need.

## Hardware

| Item | Why |
|---|---|
| Google Pixel 6a or newer (second-hand is fine) | The relay phone |
| The SIM whose SMS you want to receive | Receives SMS and calls |
| USB-C cable that carries data, not just power | Connects the phone to the server |

**A Pixel is recommended.** Google allows unlocking the bootloader
officially, root with Magisk is simple and well documented, and factory
images are public. Carrier-locked Pixels (for example US Verizon models)
usually cannot be unlocked: check before buying.

Other phones work if their bootloader can be unlocked, but some steps below
are written for Pixels and use Google's tools or Pixel firmware. They are
marked **Pixel-specific**, each with a note on what to do on other phones.

## 1. First boot

1. Go through the setup **without a Google account**. The phone does not
   need one: anything you need can be installed with `adb install`.
2. Connect to Wi-Fi and install every system update (repeat until it says
   "Your system is up to date").
3. Enable Developer options: Settings → About phone → tap **Build number**
   seven times.
4. In Settings → System → Developer options turn on **USB debugging** and
   **OEM unlocking**.
   - If OEM unlocking is greyed out, the phone may need to stay online for a
     while, or it is carrier-locked. For a known Pixel 6a workaround see
     [this video](https://www.youtube.com/watch?v=4sgtc5XO9O0).
5. Plug the phone into the server. On the phone, accept **Allow USB
   debugging** and tick **Always allow from this computer**. The approval is
   stored on the phone and survives reboots.
6. Check on the server:

   ```bash
   adb devices
   # 1A2B3C4D5E6F7G    device
   ```

## 2. Unlock the bootloader

**This wipes all data on the phone.**

```bash
adb reboot bootloader
fastboot flashing unlock
# on the phone: volume keys to "Unlock the bootloader", then the power key
```

> **Pixel-specific.** This is how Pixels are unlocked. Other manufacturers
> have their own procedure (Xiaomi, for example, requires its own unlock tool
> and a waiting period), and some phones cannot be unlocked at all. Find out
> how it works for your model before you buy.

After the wipe, go through the setup again (still without a Google
account), connect Wi-Fi, enable Developer options and USB debugging, and
accept "Always allow" again.

## 3. Root with Magisk

Root is needed because reading the SMS database
(`content query --uri content://sms/inbox`) without it fails with
"Permission denied". With root the server does everything over ADB and no
extra app has to run on the phone.

1. Download the factory image for your model with **exactly the same build
   number** as the phone (Settings → About phone → Build number) from
   <https://developers.google.com/android/images>. Unpack it, unpack the
   `image-*.zip` inside, and take out `boot.img`.
   - Pixel 7 and newer use `init_boot.img` instead of `boot.img`. Use that
     file in the steps below and flash it with `fastboot flash init_boot ...`.
   - **Pixel-specific.** Other manufacturers do not publish images like
     this. Take `boot.img` (or `init_boot.img`) from the official firmware
     for your exact build; often it has to be extracted from the
     `payload.bin` inside the update package.
2. Install Magisk: download the APK from
   <https://github.com/topjohnwu/Magisk/releases> and run
   `adb install Magisk-vXX.X.apk`.
3. Patch the image on the phone:

   ```bash
   adb push boot.img /sdcard/Download/
   # on the phone: Magisk → Install → Select and Patch a File → boot.img
   adb shell ls /sdcard/Download/          # find magisk_patched-XXXXX.img
   adb pull /sdcard/Download/magisk_patched-XXXXX.img
   ```

4. Flash it:

   ```bash
   adb reboot bootloader
   fastboot flash boot magisk_patched-XXXXX.img
   fastboot reboot
   ```

5. Check root:

   ```bash
   adb shell su -c id
   # uid=0(root) gid=0(root) ...
   ```

   On the first call Magisk asks on the phone screen whether to grant root to
   the shell: choose **Grant** and make it permanent.

**Keep the original `boot.img` and the patched image off the phone**, on the
server and in one more place. If anything goes wrong,
`fastboot flash boot boot.img` brings back the stock image.

### Turn off automatic system updates

An over-the-air update replaces the patched image and the phone silently
loses root, after which sim-proxy can no longer read SMS. In Developer
options turn off **Automatic system updates**. When you do want to update,
do it by hand and patch the new image again (step 3).

## 4. Power on automatically when power returns

This matters for power cuts: the server comes back by itself, but without
this step the phone stays off, showing the charging animation.

```bash
adb reboot bootloader
fastboot oem off-mode-charge 0
fastboot reboot
```

As a second layer you can add the Magisk module
[Magisk-Autoboot](https://github.com/anasfanani/Magisk-Autoboot).

> **Pixel-specific.** Pixels have `fastboot oem off-mode-charge`; many other
> phones do not and reject the command. On those, use the Magisk-Autoboot
> module as the main method and run the test below.

**What it reacts to:** `off-mode-charge 0` fires when power is *applied* to a
phone that is off. If you shut the phone down while the cable is already
plugged in, there is no such event and the phone stays off. That is
expected; cut and restore USB power from the server (see
[server-setup.md](server-setup.md#6-remote-control-of-phone-power-raspberry-pi)
for `uhubctl`) or press the power key.

**Test it properly:** shut the phone down (`adb shell su -c "svc power shutdown"`),
cut USB power or unplug the cable, wait about 30 seconds and restore power.
The phone should boot by itself in about 25 seconds.

## 5. Phone settings

### Google Messages as the default SMS app

Needed only for sending SMS with `/sms`. Install Google Messages if it is
missing, open it once and accept it as the default SMS app. Check:

```bash
adb shell cmd role get-role-holders android.app.role.SMS
# com.google.android.apps.messaging
```

### RCS chats off

Turn off RCS in Google Messages: Settings → RCS chats (called "Chat
features" in older versions) → off. sim-proxy reads the phone's SMS store.
RCS messages are kept inside Google Messages and never reach it, and neither
do MMS. Codes from banks and services are plain SMS and arrive either way,
but a message from a person whose phone supports RCS would silently go
missing. With RCS on, `/sms` to such a person may also go out as RCS and is
then reported as not confirmed.

### No screen lock and no SIM PIN

Each of them stops the phone from recovering on its own:

- **Screen lock:** after a reboot the phone's storage stays encrypted until
  the first unlock, and sim-proxy cannot read SMS. Set Settings → Security →
  Screen lock → **None** or **Swipe** (or
  `adb shell locksettings clear --old 1234` with your current PIN). Check:

  ```bash
  adb shell dumpsys lock_settings | grep -i credential
  # ... NONE
  ```

- **SIM PIN:** after a reboot the phone has no network until someone types
  the PIN. Turn it off in Settings → Security → SIM lock. Check after a
  reboot, without touching the phone:

  ```bash
  adb shell getprop gsm.sim.state
  # READY or LOADED
  ```

This is why the phone has to be kept somewhere physically safe: see the
security model in the [README](../README.md#security-model).

### Limit charging to 80%

A phone that is always on the charger lasts longer at 80%. Settings →
Battery → Charging optimization → **Limit to 80%**. Check with root:

```bash
adb shell su -c "cat /sys/devices/platform/google,charger/charge_stop_level"
# 80
```

The limiter does not drain the battery: above 80% it simply stops charging
(status "not charging") and waits for the level to drop by itself (about 1%
per hour). After that the phone stays between 77% and 80%.

> **Pixel-specific.** Both the setting and the file path are Pixel ones.
> Other phones may have a similar option under another name (Samsung calls it
> Protect battery) or none at all.

### Complete silence

No ringing, vibration or beeps:

```bash
adb shell cmd audio set-ringer-mode SILENT
adb shell cmd notification set_dnd none
adb shell settings put system vibrate_when_ringing 0
adb shell settings put system ring_vibration_intensity 0
adb shell settings put system notification_vibration_intensity 0
adb shell settings put system alarm_vibration_intensity 0
adb shell settings put system haptic_feedback_intensity 0
adb shell settings put secure charging_sounds_enabled 0
adb shell settings put secure charging_vibration_enabled 0
```

All of this survives reboots and does not affect forwarding: SMS and calls
still land in the databases sim-proxy reads. On recent Android versions
`settings put global zen_mode 2` no longer works directly; use
`cmd notification set_dnd` as above.

### Turn off what is not needed

```bash
adb shell su -c "svc bluetooth disable"
adb shell su -c "svc nfc disable"
adb shell settings put secure location_mode 0
adb shell settings put system screen_off_timeout 10000
adb shell settings put secure doze_tap_gesture 0          # tap to check phone
adb shell settings put secure doze_pulse_on_pick_up 0     # lift to check phone
```

## 6. Final checklist

Before moving on to [server-setup.md](server-setup.md), all of these should
pass:

```bash
adb devices                                                        # serial + "device"
adb shell su -c id                                                 # uid=0(root)
adb shell su -c 'content query --uri content://sms/inbox' | head -3   # rows or "No result found."
adb shell cmd role get-role-holders android.app.role.SMS           # com.google.android.apps.messaging
adb shell dumpsys lock_settings | grep -i credential                # NONE
adb shell getprop gsm.sim.state                                    # READY / LOADED
```

Write down the serial from `adb devices`: it goes into `.env` if more than
one phone is connected to the server.

## Useful ADB commands

```bash
adb shell su -c 'content query --uri content://sms/inbox'      # all SMS
adb shell su -c 'content query --uri content://call_log/calls' # call log
adb shell dumpsys battery                                      # battery
adb shell getprop gsm.operator.alpha                           # operator
adb shell getprop gsm.sim.state                                # SIM state
adb reboot                                                     # reboot
adb shell input keyevent KEYCODE_WAKEUP                        # wake the screen
adb shell screencap /sdcard/s.png && adb pull /sdcard/s.png    # screenshot
```

## Troubleshooting

- **`no permissions` in `adb devices` (Linux):** the udev rule is missing,
  see [server-setup.md](server-setup.md#1-adb-on-the-server).
- **`unauthorized`:** the "Allow USB debugging" prompt is waiting on the
  phone screen and needs a tap ("Always allow"). The server's key lives in
  `~/.android/adbkey*`; a new server or a fresh adb install means a new key
  and a new prompt.
- **`su: not found` or no root after a while:** the phone installed a system
  update and lost the patched image. Patch the new image again (step 3) and
  turn off automatic updates.
- **The phone did not power on after you shut it down by hand:** the cable
  was plugged in, so there was no "power applied" event. Cycle USB power or
  press the power key.
