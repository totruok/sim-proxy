# sim-proxy — ТЗ для Claude Code

## Что это

Скрипт для Mac (и Linux) который мониторит Android-телефон подключённый по USB через ADB, читает входящие SMS и пересылает их в Telegram. Телефон рутированный (Magisk), стоит на полке 24/7 как SMS-relay.

## Стек

- Bash или Python 3 (что удобнее)
- ADB для взаимодействия с телефоном
- Telegram Bot API для отправки сообщений
- Cron или systemd timer для запуска

## Конфигурация

Файл `.env` или `config.yaml`:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
ADB_DEVICE_SERIAL=XXXXXXXXXXXXXX   # опционально, если один телефон — не нужен
POLL_INTERVAL=30                     # секунды между проверками
```

## Основная функциональность

### 1. Мониторинг SMS (MVP)

- Каждые N секунд читает SMS inbox через ADB:
  ```
  adb shell su -c 'content query --uri content://sms/inbox'
  ```
- Запоминает ID последней обработанной SMS (хранит в файле `~/.sim-proxy/last_sms_id`)
- Новые SMS (с ID больше последнего) отправляет в Telegram в формате:
  ```
  📩 SMS от +447123456789
  Ваш код подтверждения: 123456
  🕐 2026-04-04 18:30
  ```
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
  📱 Устройство: Pixel 6a
  🔋 Батарея: 78%
  📶 Оператор: EE
  📡 Сигнал: LTE
  ```
- Раз в 24 часа отправляет heartbeat:
  ```
  💓 sim-proxy жив
  🔋 Батарея: 80%
  ⏱ Uptime: 3д 14ч
  ```

### 4. Обработка ошибок

- Если ADB не видит телефон (`adb devices` пустой) — отправить алерт в Telegram (через curl напрямую, без ADB):
  ```
  🚨 Телефон не отвечает!
  Последний контакт: 2026-04-04 18:30
  ```
- Повторять попытки подключения каждые 60 секунд
- Логировать всё в файл `~/.sim-proxy/sim-proxy.log`

## Команды для получения данных через ADB

```bash
# Все SMS
adb shell su -c 'content query --uri content://sms/inbox'

# Call log
adb shell su -c 'content query --uri content://call_log/calls'

# Батарея
adb shell dumpsys battery

# Оператор
adb shell getprop gsm.operator.alpha

# Состояние SIM
adb shell getprop gsm.sim.state

# Позвонить
adb shell am start -a android.intent.action.CALL -d tel:+447XXXXXXXXX

# Отправить SMS (через ADB input, не идеально)
# Лучше через intent или приложение

# Перезагрузить
adb reboot
```

## Структура проекта

```
sim-proxy/
├── README.md
├── .env.example
├── sim_proxy.py          # основной скрипт
├── install.sh            # установка зависимостей + настройка cron/launchd
└── requirements.txt      # если Python
```

## Установка

`install.sh` должен:
- Проверить что ADB доступен
- Проверить что телефон подключён
- Попросить ввести TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID
- Создать `.env`
- Создать `~/.sim-proxy/` директорию
- Настроить автозапуск:
  - macOS: LaunchAgent (plist)
  - Linux: systemd service

## Важные моменты

- Кавычки в ADB командах: `su -c` через тройной уровень шеллов ломает кавычки. Проверенный рабочий формат:
  ```
  adb shell su -c 'content query --uri content://sms/inbox'
  ```
  Сортировка с `--sort "date DESC"` не работает из-за escaping. Сортировку делать на стороне скрипта.

- Парсинг вывода `content query`: формат `Row: N key=value, key=value, ...`. Парсить через regex.

- Поле `date` в SMS — unix timestamp в **миллисекундах**.

- Поле `type` в call_log: 1=incoming, 2=outgoing, 3=missed.

- Скрипт должен работать и на macOS и на Linux без изменений.

## Что НЕ нужно

- GUI
- Веб-интерфейс
- База данных (хватит flat files)
- Docker
- Отправка SMS (пока)
