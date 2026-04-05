# sim-proxy

Скрипт для Mac/Linux: мониторит рутированный Android-телефон через ADB и пересылает SMS и пропущенные звонки в Telegram. Управление телефоном через Telegram-команды.

## Возможности

- Пересылка входящих SMS в Telegram (полный текст, включая многострочные)
- Уведомления о пропущенных звонках
- Отправка SMS через Telegram-команду (через Google Messages, headless)
- Health check при запуске (модель, номер SIM, батарея, оператор, сигнал dBm)
- Алерт при отключении телефона, ошибки скрипта в Telegram
- Автоматический перезапуск через LaunchAgent (macOS) или systemd (Linux)

## Telegram-команды

| Команда | Описание |
|---|---|
| `/status` | Статус телефона: модель, номер SIM, батарея, оператор, сигнал (dBm), uptime |
| `/sms +7XXX текст` | Отправить SMS с телефона |
| `/inbox [N]` | Последние N SMS (по умолчанию 5) |
| `/calls [N]` | Последние N звонков (по умолчанию 5) |
| `/ussd *100#` | USSD запрос (баланс, тариф и т.д.) |
| `/reboot` | Перезагрузить телефон |
| `/help` | Список команд |

## Требования

- Python 3.6+
- ADB (Android SDK platform-tools)
- Рутированный Android-телефон (Magisk) с USB-отладкой
- Google Messages как SMS-приложение по умолчанию (для отправки SMS через `/sms`)
- Telegram-бот (создать через [@BotFather](https://t.me/BotFather))

## Установка

```bash
git clone <repo-url> && cd sim-proxy
./install.sh
```

Скрипт `install.sh`:
1. Проверит наличие ADB и Python
2. Проверит подключение телефона
3. Запросит Telegram Bot Token и Chat ID
4. Отправит тестовое сообщение в Telegram
5. Настроит автозапуск

## Ручной запуск

```bash
cp .env.example .env
# Заполнить .env
python3 sim_proxy.py
```

## Управление

### macOS

```bash
# Остановить
launchctl unload ~/Library/LaunchAgents/com.sim-proxy.plist

# Запустить
launchctl load ~/Library/LaunchAgents/com.sim-proxy.plist
```

### Linux

```bash
systemctl --user status sim-proxy
systemctl --user stop sim-proxy
systemctl --user start sim-proxy
journalctl --user -u sim-proxy -f
```

## Логи

```bash
tail -f ~/.sim-proxy/sim-proxy.log
```

## Конфигурация (.env)

| Переменная | Описание | Обязательная |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather | Да |
| `TELEGRAM_CHAT_ID` | ID чата для уведомлений | Да |
| `ADB_DEVICE_SERIAL` | Серийный номер устройства (если несколько) | Нет |
| `POLL_INTERVAL` | Интервал опроса в секундах (по умолчанию 30) | Нет |
