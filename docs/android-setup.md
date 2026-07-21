# Настройка нового Android-телефона под sim-proxy

Как превратить Android-телефон в SMS/звонки-релей: телефон стоит на полке 24/7,
подключён к серверу по USB, всё управление — через ADB и Telegram.

Проверено на Pixel 6a (bluejay, Android 16) + Raspberry Pi. Команды с `fastboot`
и `adb` выполняются на сервере, к которому подключён телефон.

## Железо

| Вещь | Зачем |
|------|-------|
| Google Pixel 6a или новее (можно б/у) | Телефон-релей |
| SIM-карта с нужным номером | Приём SMS/звонков |
| USB-C кабель (данные, не только зарядка) | Подключение к серверу |

Почему Pixel: Google официально разрешает разблокировку bootloader, простое
рутование через Magisk, отличная документация сообщества, eSIM + nano-SIM.

## 1. Первоначальная настройка

1. Пройди онбординг **без Google-аккаунта** — он не нужен, все приложения
   ставятся через `adb install`.
2. Подключись к Wi-Fi, установи все обновления системы (повторяй до
   "Your system is up to date").
3. Включи Developer Options: Settings → About Phone → 7 тапов по Build number.
4. В Developer Options включи **USB Debugging** и **OEM Unlocking**.
   - Если OEM Unlocking серый (известная болезнь Pixel 6a):
     https://www.youtube.com/watch?v=4sgtc5XO9O0
5. Подключи к серверу, на телефоне подтверди "Allow USB debugging"
   с галкой **Always allow** (авторизация хранится на телефоне постоянно
   и переживает ребуты).
6. Проверь: `adb devices` → серийник и `device`.

## 2. Разблокировка bootloader

**⚠️ Все данные на телефоне будут стёрты.**

```bash
adb reboot bootloader
fastboot flashing unlock
# на телефоне кнопками громкости: "Unlock the bootloader" → кнопка питания
```

После вайпа: онбординг заново (снова без Google-аккаунта), Wi-Fi,
Developer Options, USB Debugging, "Always allow".

## 3. Рут через Magisk

Зачем рут: чтение SMS-базы (`content query --uri content://sms/inbox`) без рута
даёт Permission Denied; на телефоне не нужны никакие сторонние приложения —
всё делает сервер через ADB.

1. Скачай factory image своей модели и **точно того же Build number**:
   https://developers.google.com/android/images → распакуй, внутри
   `image-*.zip` → распакуй → найди `boot.img`.
2. Установи Magisk: скачай APK с https://github.com/topjohnwu/Magisk/releases,
   `adb install Magisk-vXX.X.apk`.
3. Пропатчи boot.img:
   ```bash
   adb push boot.img /sdcard/Download/
   # на телефоне: Magisk → Install → Select and Patch a File → boot.img
   adb pull /sdcard/Download/magisk_patched-XXXXX.img
   ```
4. Прошей:
   ```bash
   adb reboot bootloader
   fastboot flash boot magisk_patched-XXXXX.img
   fastboot reboot
   ```
5. Проверь: `adb shell su -c id` → `uid=0(root)`. При первом вызове Magisk
   спросит "Grant root access?" на экране — **Allow + Forever**.

**Сохрани оригинальный `boot.img` и пропатченный образ вне телефона**
(сервер + второе место). Если что-то сломается:
`fastboot flash boot boot.img` возвращает сток.

## 4. Автовключение при подаче питания

Критично для сценария «отключили электричество»: сервер перезагрузится сам,
а телефон без этого шага останется лежать выключенным с иконкой зарядки.

```bash
adb reboot bootloader
fastboot oem off-mode-charge 0
fastboot reboot
```

Опционально вторым слоем — Magisk-модуль autoboot:
https://github.com/anasfanani/Magisk-Autoboot

**Тонкость про семантику:** `off-mode-charge 0` срабатывает на событие
«на выключенный телефон *подали* питание». Если выключить телефон при уже
воткнутом кабеле — события нет, телефон останется лежать. Это нормально;
лечится передёргиванием питания USB-порта с сервера (см. server-setup.md,
uhubctl) или кнопкой.

**Честный тест:** выключи телефон (`adb shell su -c "svc power shutdown"`),
обесточь USB-порт (или выдерни кабель), подожди ~30 секунд, подай питание —
телефон должен загрузиться сам за ~25 секунд.

## 5. Настройки телефона

### SMS-приложение по умолчанию — Google Messages

Нужно для отправки SMS командой `/sms`. Проверка:

```bash
adb shell cmd role get-role-holders android.app.role.SMS
# → com.google.android.apps.messaging
```

### Без PIN экрана и без PIN на SIM

Оба PIN ломают автономность:

- **PIN экрана** — после ребута хранилище остаётся зашифрованным до первой
  разблокировки (BFU), sim-proxy не сможет читать SMS. Убери: Settings →
  Security → Screen lock → **Swipe** (или `adb shell locksettings clear --old "1234"`).
  Проверка: `adb shell dumpsys lock_settings | grep Credential` → `NONE`.
- **PIN на SIM** — после ребута не будет сети, пока PIN не введут. Убери:
  Settings → Security → SIM lock. Проверка — ребут: `adb shell getprop
  gsm.sim.state` должен дойти до `READY`/`LOADED` без рук.

### Лимит заряда 80%

Settings → Battery → Charging optimization → **Limit to 80%**.

Проверка через рут:
```bash
adb shell su -c "cat /sys/devices/platform/google,charger/charge_stop_level"
# → 80
```

Механика: лимитер не сливает батарею принудительно — если телефон заряжен
выше 80%, он перестаёт заряжать (status: not charging) и ждёт, пока заряд
сам стечёт (~1%/час). Дальше живёт в коридоре 77–80%.

### Полная тишина (не звонит, не вибрирует, не пищит)

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

Всё переживает ребут. На пересылку не влияет: SMS и звонки всё равно
попадают в базы, откуда их читает sim-proxy.

`settings put global zen_mode 2` напрямую не работает на новых Android —
только через `cmd notification set_dnd none`.

### Отключи лишнее

```bash
adb shell su -c "svc bluetooth disable"
adb shell su -c "svc nfc disable"
adb shell settings put secure location_mode 0
adb shell settings put system screen_off_timeout 10000
adb shell settings put secure doze_tap_gesture 0          # tap to check phone
adb shell settings put secure doze_pulse_on_pick_up 0     # lift to check phone
```

## Полезные ADB-команды

```bash
adb shell su -c 'content query --uri content://sms/inbox'      # все SMS
adb shell su -c 'content query --uri content://call_log/calls' # журнал звонков
adb shell dumpsys battery                                      # батарея
adb shell getprop gsm.operator.alpha                           # оператор
adb shell getprop gsm.sim.state                                # состояние SIM
adb reboot                                                     # перезагрузка
adb shell input keyevent KEYCODE_WAKEUP                        # разбудить экран
adb shell screencap /sdcard/s.png && adb pull /sdcard/s.png    # скриншот
```

## Troubleshooting

- **`no permissions` в adb devices (Linux)** — нет udev-правила, см.
  server-setup.md.
- **`unauthorized`** — на экране телефона висит запрос "Allow USB debugging" —
  нужен физический тап (Always allow). Ключи сервера живут в `~/.android/adbkey*`;
  новый сервер = новый ключ = новый запрос.
- **Телефон не включился сам после ручного выключения** — кабель был воткнут,
  события «подали питание» не было. Передёрни питание порта или включи кнопкой.
