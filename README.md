# Predict.fun Telegram Bot — SPLIT Strategy Points Farmer

🎯 **Telegram-бот для фарминга Predict Points (PP) на платформе [Predict.fun](https://predict.fun)**

Управляй ботом прямо из Telegram — без командной строки. Поддерживает несколько пользователей и несколько Predict.fun аккаунтов одновременно.

## 📋 Как работает стратегия SPLIT

1. **ФАЗА 1** — Покупаем одновременно YES и NO токены
   - YES + NO = 1.00 → при любом исходе получаем обратно $1

2. **ФАЗА 2** — Выставляем SELL ордера на +1 цент от лучшего ASK
   - Ордера стоят в книге и **не исполняются** сразу
   - Фармим поинты за предоставление ликвидности

3. **Результат**
   - ✅ Выходим в ноль или небольшой плюс при любом исходе
   - ✅ Максимальный фарминг поинтов **без риска**

## 💎 Система Predict Points (PP)

Согласно [документации](https://docs.predict.fun/the-basics/how-to-earn-points):

- **Лимитные ордера** — чем ближе к рынку, тем больше PP
- **Лучший bid/ask** — максимум поинтов
- **Ордера на обеих сторонах** (bid + ask) — бонусные поинты
- **Maker Fee = 0%** — размещение ордеров бесплатно
- **Награды** распределяются каждые 7 дней

## ✨ Возможности бота

- **Telegram-интерфейс** — запуск, остановка и мониторинг через бота
- **Мульти-аккаунт** — несколько Predict.fun аккаунтов, работают параллельно
- **Мульти-пользователь** — система приглашений: вы одобряете доступ через Telegram
- **WebSocket** — получение данных стакана в реальном времени
- **Авто-репозиционирование** — ордера поддерживаются у лучшего ASK
- **Мониторинг** — статус, баланс и активные ордера прямо в чате

## 🚀 Установка

### 1. Клонировать репозиторий

```bash
git clone https://github.com/kirst-dk/predict-split-bot.git
cd predict-split-bot
```

### 2. Создать виртуальное окружение и установить зависимости

```bash
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Настроить конфигурацию

```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

Откройте `.env` и заполните все значения (см. раздел ниже).

### 4. Запустить бота

```bash
python telegram_bot.py
```

## ⚙️ Конфигурация (.env)

```env
# ── Telegram ──────────────────────────────────────────────
# Получить токен у @BotFather
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here

# Ваш Telegram ID — только вы сможете управлять ботом
# Узнать ID: напишите @userinfobot
TELEGRAM_ADMIN_ID=your_telegram_admin_id_here

# ── Predict.fun API ───────────────────────────────────────
# API Key (получить в Discord: https://discord.gg/predictdotfun)
PREDICT_API_KEY=your_api_key_here

# Для Predict Account (Smart Wallet — рекомендуется):
PRIVY_WALLET_PRIVATE_KEY=your_privy_wallet_private_key_here
PREDICT_ACCOUNT_ADDRESS=your_predict_account_address_here

# Или для обычного EOA кошелька:
# PRIVATE_KEY=your_private_key_here

# ── Сеть ──────────────────────────────────────────────────
# BNB Mainnet = 56, BNB Testnet = 97
CHAIN_ID=56
```

### Как получить Telegram Bot Token:
1. Откройте [@BotFather](https://t.me/BotFather) в Telegram
2. Отправьте `/newbot` и следуйте инструкциям
3. Скопируйте полученный токен

### Как получить Telegram Admin ID:
Напишите [@userinfobot](https://t.me/userinfobot) — он пришлёт ваш числовой ID.

### Как получить API Key:
1. Зайдите в Discord: https://discord.gg/predictdotfun
2. Откройте тикет в поддержке и запросите API key

### Как экспортировать Privy Wallet:
1. Зайдите в настройки: https://predict.fun/account/settings
2. Экспортируйте приватный ключ Privy Wallet
3. Скопируйте Deposit Address — это ваш `PREDICT_ACCOUNT_ADDRESS`

## 📖 Использование

### Основной режим — Telegram Bot

```bash
python telegram_bot.py
```

После запуска откройте бота в Telegram и нажмите `/start`.

**Команды администратора:**
- `/start` — главное меню
- Управление пользователями (одобрение/отключение)
- Добавление и переключение Predict.fun аккаунтов
- Запуск/остановка торговли по рынкам
- Просмотр баланса и активных ордеров

**Для рефералов (после одобрения):**
- Добавляют свои аккаунты и торгуют самостоятельно

### Консольный режим (дополнительно)

Если нужно запустить без Telegram:

```bash
# Интерактивный выбор рынка
python predict_trader.py

# Указать конкретный рынок
python predict_trader.py --market 123

# Указать размер ордера
python predict_trader.py --market 123 --amount 10

# Изменить offset для SPLIT
python predict_trader.py --market 123 --offset 0.02
```

## 📁 Структура проекта

```
predict-split-bot/
├── telegram_bot.py         # Telegram-бот (основной запуск)
├── predict_trader.py       # Движок стратегии SPLIT
├── predict_api.py          # REST API клиент Predict.fun
├── predict_ws.py           # WebSocket клиент (real-time данные)
├── user_manager.py         # Управление пользователями / доступом
├── state.py                # Персистентное состояние бота
├── config.py               # Конфигурация и константы
├── find_binary_markets.py  # Поиск бинарных рынков
├── requirements.txt        # Зависимости Python
├── .env.example            # Шаблон конфигурации
├── accounts.json.example   # Шаблон файла аккаунтов
├── INSTRUCTION_RU.md       # Детальная инструкция по установке
└── deploy/                 # Скрипты для деплоя (Linux systemd)
```

## 🔧 API Документация

- **REST API**: https://dev.predict.fun/
- **Docs**: https://docs.predict.fun/
- **Python SDK**: https://github.com/PredictDotFun/sdk-python

## ⚠️ Важные замечания

### Лимиты платформы:

- Минимальный ордер: **$1 USDT**
- Максимум ордеров на рынок: **10** лимитных ордеров
- Rate limit: **240 запросов/минуту**

### Комиссии:

- **Maker Fee: 0%** (лимитные ордера бесплатно)
- **Taker Fee: 0.018% – 2%** (зависит от цены)
- 10% скидка по реферальной программе

### Безопасность:

- ❌ Никогда не публикуйте `.env` и `accounts.json`
- ✅ Используйте отдельный кошелёк для бота
- ✅ Начните с малых сумм для тестирования
- ✅ На testnet API key не требуется

## 🔗 Полезные ссылки

- [Predict.fun](https://predict.fun) — Платформа
- [Документация](https://docs.predict.fun/) — Общая документация
- [API Docs](https://dev.predict.fun/) — Техническая документация
- [Discord](https://discord.gg/predictdotfun) — Сообщество
- [Python SDK](https://pypi.org/project/predict-sdk/) — PyPI пакет

## 📜 Лицензия

MIT License

---

**Disclaimer**: Программное обеспечение предоставляется «как есть». Используйте на свой страх и риск. Автор не несёт ответственности за финансовые потери.
