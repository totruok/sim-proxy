# sim-proxy — ТЗ для Claude Code

## Что это

Скрипт-демон для Mac (и Linux) который мониторит рутированный Android-телефон подключённый по USB через ADB, пересылает входящие SMS и пропущенные звонки в Telegram, и позволяет управлять телефоном через Telegram-команды. Телефон рутированный (Magisk), стоит на полке 24/7 как SMS-relay.

## Стек

- Python 3 (stdlib only, без внешних зависимостей)
- ADB для взаимодействия с телефоном
- Telegram Bot API (`urllib.request` для отправки, `getUpdates` long polling для приёма команд)
- LaunchAgent (macOS) или systemd (Linux) для автозапуска

## Конфигурация

Файл `.env`:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ADB_DEVICE_SERIAL=XXXXXXXXXXXXXX   # опционально, если один телефон — не нужен
POLL_INTERVAL=5                      # секунды между проверками
```

## Основная функциональность

### 1. Мониторинг SMS

- Каждые N секунд читает SMS inbox через ADB:
  ```
  adb shell su -c 'content query --uri content://sms/inbox'
  ```
- Запоминает ID последней обработанной SMS (хранит в файле `~/.sim-proxy/last_sms_id`)
- Новые SMS (с ID больше последнего) отправляет в Telegram:
  ```
  📩 SMS от +447123456789
  Ваш код подтверждения: 123456
  🕐 2026-04-04 18:30
  ```
- При первом запуске (файл last_sms_id не существует) — запомнить текущий макс ID, не флудить старыми сообщениями
- Timestamp из SMS (поле `date`, unix ms) конвертировать в читаемый формат

### 2. Мониторинг пропущенных звонков

- Читает call log через ADB:
  ```
  adb shell su -c 'content query --uri content://call_log/calls'
  ```
- Поле `type=3` — пропущенный звонок
- Новые пропущенные звонки отправляет в Telegram:
  ```
  📞 Пропущенный звонок от +447123456789
  🕐 2026-04-04 18:30
  ```
- Хранит ID последнего обработанного звонка в `~/.sim-proxy/last_call_id`

### 3. Health check

- При запуске скрипта отправляет в Telegram:
  ```
  ✅ sim-proxy запущен
  📱 Устройство: Pixel 9
  📞 Номер: +447700900123
  🔋 Батарея: 78%
  📶 Оператор: EE
  📡 Сигнал: LTE (-95 dBm, хороший)
  ```

### 4. Telegram-команды

Скрипт принимает команды через Telegram Bot API (`getUpdates` long polling). Long poll timeout = POLL_INTERVAL, это же заменяет sleep между циклами опроса.

| Команда | Описание |
|---|---|
| `/status` | Модель, номер SIM, батарея, оператор, сигнал (dBm + качество), uptime |
| `/sms +7XXX текст` | Отправить SMS через Google Messages (headless) |
| `/inbox [N]` | Показать последние N SMS (по умолчанию 5) |
| `/calls [N]` | Показать последние N звонков (по умолчанию 5) |
| `/ussd *100#` | Отправить USSD-запрос, попытаться захватить ответ из logcat |
| `/reboot` | Перезагрузить телефон |
| `/help` | Список команд |

Команды принимаются только от `TELEGRAM_CHAT_ID` из конфига.

### 5. Отправка SMS

**Важно:** `service call isms` из root-контекста на Android 14+ тихо не отправляет SMS (записывает в sent, но не доставляет). Рабочий способ — через Google Messages `NoConfirmationSmsSendService`:

```
am startservice -a android.intent.action.RESPOND_VIA_MESSAGE \
  -d "smsto:$NUMBER" \
  --es android.intent.extra.TEXT "$TEXT" \
  -n com.google.android.apps.messaging/.shared.NoConfirmationSmsSendService
```

Из-за проблем с кавычками через ADB→su→sh, текст SMS нельзя передавать напрямую. Решение — helper-скрипт на устройстве (`/data/local/tmp/sim-proxy-sendsms.sh`):

```sh
#!/system/bin/sh
NUMBER="$1"
shift
TEXT="$*"
am startservice -a android.intent.action.RESPOND_VIA_MESSAGE \
  -d "smsto:$NUMBER" --es android.intent.extra.TEXT "$TEXT" \
  -n com.google.android.apps.messaging/.shared.NoConfirmationSmsSendService
```

Скрипт деплоится на устройство при старте через `adb push`. Вызов: `adb shell su -c '/data/local/tmp/sim-proxy-sendsms.sh +7XXX слово1 слово2 слово3'` — `shift` + `$*` собирает все слова после номера в одну строку.

### 6. Уровень сигнала

Читать из `dumpsys telephony.registry`:
- Найти `primary=CellSignalStrength<Type>` (например `CellSignalStrengthLte`)
- Из секции `m<Type>=` извлечь `rsrp=` (в dBm)
- Качественная оценка по RSRP (AOSP thresholds):
  - >= -85: отличный
  - -85...-95: хороший
  - -95...-105: нормальный
  - -105...-115: слабый
  - < -115: очень слабый
- Показывать как: `LTE (-102 dBm, нормальный)`

**Не использовать** первый попавшийся `level=` — он может быть от неактивной сети (Cdma, Nr). Брать только из primary-типа.

### 7. Номер SIM

Определять через (по приоритету):
1. `adb shell su -c 'dumpsys iphonesubinfo'` → `Phone Number = +XXX`
2. `content query --uri content://telephony/siminfo` → поле `number`
3. `getprop gsm.sim.operator.numeric` (fallback, вернёт MCC/MNC)

### 8. Обработка ошибок

- Если ADB не видит телефон — отправить алерт в Telegram (через curl как fallback):
  ```
  🚨 Телефон не отвечает!
  Последний контакт: 2026-04-04 18:30
  ```
- Повторять попытки подключения каждые 60 секунд (при этом команды из Telegram продолжают обрабатываться)
- При reconnect — повторный health check
- Логировать всё в `~/.sim-proxy/sim-proxy.log` (RotatingFileHandler, 5MB × 3)
- Ошибки уровня ERROR+ дублировать в Telegram через кастомный logging handler

## Важные технические детали

### Парсинг `content query`

Формат: `Row: N key=value, key=value, ...`

- **Многострочные SMS**: body может содержать `\n`. Строки не начинающиеся с `Row: N` — продолжение предыдущей строки. Склеивать перед парсингом.
- **Запятые в body**: не делать split по `, `. Вместо этого найти все позиции `key=` через regex `(?:^|, )([a-zA-Z_]\w*)=`, извлечь значения между ними.
- Поле `date` в SMS — unix timestamp в **миллисекундах**.
- Поле `type` в call_log: 1=incoming, 2=outgoing, 3=missed.

### ADB кавычки

`su -c` через тройной уровень шеллов (Python subprocess → adb → device sh → su) ломает кавычки. Рабочие подходы:
- Для команд без пробелов в аргументах: `adb_cmd(["shell", "su", "-c", "command args"])`
- Для команд с пробелами (SMS текст): деплоить shell-скрипт на устройство и вызывать его
- Сортировка `--sort "date DESC"` не работает из-за escaping. Сортировку делать на стороне Python.

### Main loop

```
while running:
    process_commands(timeout=POLL_INTERVAL)  # long poll заменяет sleep
    if now < next_device_check: continue
    check_device → poll SMS/calls
    if disconnected: next_device_check = now + 60
```

Telegram `getUpdates` с `timeout=POLL_INTERVAL` — команды обрабатываются мгновенно, SMS опрашиваются каждые POLL_INTERVAL секунд.

## Структура проекта

```
sim-proxy/
├── sim-proxy-spec.md     # это ТЗ
├── README.md             # документация
├── .env.example          # шаблон конфигурации
├── sim_proxy.py          # основной скрипт (~800 строк)
├── install.sh            # интерактивный установщик
└── requirements.txt      # пустой (stdlib only)
```

## Установка

`install.sh` должен:
- Проверить что ADB и Python 3 доступны
- Проверить что телефон подключён
- Попросить ввести TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID
- Отправить тестовое сообщение в Telegram
- Создать `.env` и `~/.sim-proxy/`
- Настроить автозапуск:
  - macOS: LaunchAgent (`~/Library/LaunchAgents/com.sim-proxy.plist`), KeepAlive=true, PATH включает `/opt/homebrew/bin`
  - Linux: systemd user service (`~/.config/systemd/user/sim-proxy.service`), Restart=always

## Требования

- Python 3.6+
- ADB (Android SDK platform-tools)
- Рутированный Android (Magisk) с USB-отладкой
- Google Messages как SMS-приложение по умолчанию (для `/sms`)
- Telegram-бот

## Что НЕ нужно

- GUI
- Веб-интерфейс
- База данных (хватит flat files)
- Docker
- Внешние Python-зависимости
