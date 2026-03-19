# Predict_Bot — установка и запуск на новом устройстве

## 1) Что нужно установить

- Python 3.10+ (рекомендуется 3.10–3.12)
- Git
- (Опционально) Node.js — только если будете использовать JS SDK отдельно

Официальные ссылки:
- Python SDK: https://github.com/PredictDotFun/sdk-python
- API/WS документация: https://dev.predict.fun/

## 2) Клонирование проекта

```bash
git clone <URL_ВАШЕГО_РЕПО>
cd Predict_Bot
```

## 3) Виртуальное окружение

### Windows (PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### Linux / macOS
```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 4) Установка зависимостей (включая SDK и WebSocket)

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

В проекте уже зафиксированы ключевые пакеты:
- `predict-sdk>=0.0.15` (официальный Python SDK)
- `web3>=6.0.0`
- `websockets>=12.0` (WebSocket клиент)
- `python-telegram-bot>=20.0`

Если ставите вручную минимальный набор:
```bash
pip install predict-sdk websockets web3 python-dotenv requests eth-account python-telegram-bot aiohttp
```

## 5) Настройка .env

Скопируйте шаблон:

### Windows
```powershell
Copy-Item .env.example .env
```

### Linux / macOS
```bash
cp .env.example .env
```

Заполните в `.env`:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_ID=...

PREDICT_API_KEY=...
PRIVATE_KEY=...

# Если используете Predict Account (smart wallet)
PRIVY_WALLET_PRIVATE_KEY=...
PREDICT_ACCOUNT_ADDRESS=...

CHAIN_ID=56
RPC_URL=https://bsc-dataseed.binance.org/
```

Важно:
- `PREDICT_API_KEY` для mainnet получают через Discord: https://discord.gg/predictdotfun
- Mainnet API: `https://api.predict.fun/`
- Testnet API (без API key): `https://api-testnet.predict.fun/`

## 6) Как работает WebSocket в этом боте

Бот использует `predict_ws.py` и подключается к:
- `wss://ws.predict.fun/ws`

Каналы:
- `predictOrderbook/{marketId}` — стакан
- `predictWalletEvents/{jwt}` — события кошелька/ордеров

Для `wallet events` нужен JWT. В проекте уже реализован flow:
1. `GET /v1/auth/message`
2. Подписать сообщение приватным ключом
3. `POST /v1/auth` -> получить JWT

Это делается автоматически кодом (`predict_api.py` + `predict_ws.py`) при корректных `.env` данных.

## 7) Проверка установки

```bash
python -m py_compile predict_trader.py
python -m py_compile telegram_bot.py
python -m py_compile predict_ws.py
```

Если без ошибок — окружение готово.

## 8) Запуск

### Telegram-бот (основной сценарий)
```bash
python telegram_bot.py
```

### CLI трейдер
```bash
python predict_trader.py
```

## 9) Проверка, что всё подключилось

В логах должны быть признаки:
- успешная инициализация API
- получение JWT (для auth endpoint)
- подключение к WebSocket
- подписка на `predictOrderbook/...`
- подписка на `predictWalletEvents/...`

## 10) Типовые проблемы

1. `401/403` от API:
- проверьте `PREDICT_API_KEY`
- убедитесь, что используете правильный endpoint (mainnet/testnet)

2. Не получает JWT:
- проверьте `PRIVATE_KEY`
- проверьте адрес/тип аккаунта (EOA vs Predict Account)

3. WebSocket не подключается:
- проверьте сеть/VPN/firewall
- проверьте доступ к `wss://ws.predict.fun/ws`

4. Ошибки пакетов:
```bash
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt --no-cache-dir
```

## 11) Обновление проекта

```bash
git pull
pip install -r requirements.txt
```

---

Если нужно, могу дополнительно добавить в эту инструкцию отдельный раздел «чистая установка на Windows Server/Linux VPS через systemd» с готовыми командами под ваш деплой в `deploy/predict_bot.service`.
