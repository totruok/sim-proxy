# Настройка сервера с Telegram-ботом

Сервер — это машина, в которую воткнут телефон по USB: Linux (проверено на
Raspberry Pi / Debian) или macOS. Здесь живёт sim-proxy и Telegram-бот.

Телефон должен быть уже подготовлен по [android-setup.md](android-setup.md).

## 1. ADB на сервере

**Linux (Debian/Ubuntu/Raspberry Pi OS):**

```bash
sudo apt install adb
```

На Linux телефон без udev-правила будет виден как `no permissions`:

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", MODE="0660", GROUP="plugdev"' \
  | sudo tee /etc/udev/rules.d/51-android.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb
adb kill-server && adb devices
```

`18d1` — vendor ID Google (Pixel); для другого производителя возьми vendor
из `lsusb`. Пользователь должен состоять в группе `plugdev`.

**macOS:**

```bash
brew install android-platform-tools
```

Дальше добейся `adb devices` → `device`. Если `unauthorized` — подтверди
запрос на экране телефона (Always allow). Проверь рут: `adb shell su -c id`
→ `uid=0`. Запиши серийник — он пойдёт в `.env`.

## 2. Telegram-бот

- Новый бот: [@BotFather](https://t.me/BotFather) → `/newbot` → токен.
  Свой chat_id: спроси у [@userinfobot](https://t.me/userinfobot).
- Переиспользуешь существующего бота — **сначала погаси старого потребителя**:
  два поллера `getUpdates` на одном токене дерутся (Telegram отдаёт апдейты
  то одному, то другому), команды будут теряться. Найди и выключи старый
  сервис насовсем: `systemctl disable --now <старый-юнит>` (или
  `systemctl --user disable --now ...`).

## 3. Если api.telegram.org заблокирован (РФ)

sim-proxy ходит в Telegram через urllib и curl — оба уважают переменные
окружения `http_proxy`/`https_proxy`. Нужен любой локальный HTTP-прокси,
выходящий за пределы блокировок (например, xray VLESS+REALITY с
http-inbound на `127.0.0.1:10809`).

**До установки** проверь путь руками:

```bash
curl -x http://127.0.0.1:10809 "https://api.telegram.org/bot<ТОКЕН>/getMe"
# → {"ok":true,...}
```

Без работающего `getMe` дальше не иди.

Затем пропиши прокси в юнит sim-proxy через drop-in — он переживёт
пересоздание юнита инсталлером:

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

(curl понимает обе формы записи, urllib тоже; прописываем обе на всякий.)
На macOS аналогично добавь `EnvironmentVariables` в plist после install.sh.

При запуске install.sh в той же сессии экспортируй эти переменные —
его curl-проверка тоже должна ходить через прокси.

## 4. Установка sim-proxy

```bash
git clone git@github.com:totruok/sim-proxy.git && cd sim-proxy
cp .env.example .env   # заполни токен, chat_id, серийник
./install.sh           # на вопрос про существующий .env ответь n
```

install.sh проверит adb и python, отправит тестовое сообщение в Telegram,
создаст юнит (`systemd --user` на Linux, LaunchAgent на macOS) и запустит его.

Нюансы:

- **Неинтерактивный запуск:** `adb shell` внутри install.sh съедает stdin,
  поэтому `echo n | ./install.sh` не работает — используй `yes n | ./install.sh`.
- **Linux:** чтобы `systemd --user` юнит жил без активной ssh-сессии и
  переживал ребут:
  ```bash
  sudo loginctl enable-linger $USER
  ```

## 5. Проверка (обязательно, не на словах)

1. В Telegram пришёл health-check при старте: модель, номер SIM, батарея,
   оператор, сигнал dBm.
2. `/status` боту — отвечает в пределах POLL_INTERVAL.
3. **Реальная SMS по радиосети** долетает в Telegram: пошли с другого
   телефона, или без второго телефона — `/sms <номер этой же SIM> тест`
   (SMS-to-self: уйдёт с симки и вернётся на неё же входящей).
   Для исходящих на балансе должны быть деньги.
4. Логи чистые:
   ```bash
   journalctl --user -u sim-proxy -f
   tail -f ~/.sim-proxy/sim-proxy.log
   ```
5. Тест переподключения: выдерни-воткни USB. В Telegram придёт алерт
   «Телефон не отвечает», после переподключения — health-check. Авторизация
   заново не нужна.

## 6. Удалённое управление питанием телефона (Raspberry Pi)

Pi умеет программно рубить питание USB-портов — это удалённая замена
«выдернуть-воткнуть кабель» и единственный способ поднять телефон, который
лежит выключенный с воткнутым кабелем (см. семантику off-mode-charge в
android-setup.md):

```bash
sudo apt install uhubctl
sudo uhubctl -l 1-1 -a off && sudo uhubctl -l 2 -a off   # питание снято
sleep 15
sudo uhubctl -l 1-1 -a on && sudo uhubctl -l 2 -a on     # «подали питание»
# выключенный телефон начнёт грузиться сам (~25 сек до полной загрузки)
```

На Pi питание портов ганговое — выключаются все порты сразу; убедись, что
на USB не висит ничего критичного.

Этим же способом честно тестируется сценарий «отключили электричество»:
выключи телефон, сними питание, подожди, подай — телефон должен встать сам,
SIM зарегистрироваться (без SIM PIN!), sim-proxy прислать health-check.

## Troubleshooting

- **`unauthorized`** — нужен тап на экране телефона; ключ сервера в
  `~/.android/adbkey*` (свежая установка adb = новый ключ = новый запрос).
- **SMS задваиваются или команды теряются** — на токене живы два поллера
  getUpdates. Найди и погаси старого бота (см. п. 2).
- **Health-check есть, а `/sms` не отправляется** — проверь баланс SIM:
  ответ оператора придёт входящей SMS в этот же чат.
- **«Телефон не отвечает» и не переподключается** — проверь кабель/порт,
  затем передёрни питание USB через uhubctl (п. 6).
- **После ребута сервера бот молчит** — забыт `loginctl enable-linger`,
  юнит не стартовал без ssh-сессии.
