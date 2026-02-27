# 🎯 Predict.fun SPLIT Farmer Bot

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge&logo=python&logoColor=white)
![BNB Chain](https://img.shields.io/badge/BNB_Chain-Mainnet-yellow?style=for-the-badge&logo=binance&logoColor=black)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![Telegram](https://img.shields.io/badge/Telegram-Bot-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)
![WebSocket](https://img.shields.io/badge/WebSocket-Real--time-orange?style=for-the-badge)

**Автоматический фарминг Predict Points (PP) на [Predict.fun](https://predict.fun) с нулевым риском**

[Установка](#-установка) · [Конфигурация](#%EF%B8%8F-конфигурация) · [Запуск](#-запуск) · [Стратегия](#-стратегия-split) · [FAQ](#-faq)

</div>

---

## ✨ Возможности

- ⚡ **Real-time мониторинг** через WebSocket — мгновенная реакция на изменения стакана
- 🤖 **Telegram-бот** — управление и уведомления прямо в Telegram
- 👥 **Мульти-аккаунт** — управление несколькими кошельками из одного места
- 📊 **Статистика сессии** — подсчёт заработанных PP в реальном времени
- 🔄 **Авто-репозиционирование** — ордера всегда на лучших позициях в стакане
- 🛡️ **Защита от риска** — стратегия SPLIT гарантирует возврат средств при любом исходе

---

## 🧠 Стратегия SPLIT

Стратегия основана на том, что `YES + NO = $1.00` при любом исходе события.

```
ФАЗА 1 ─── Покупаем YES + NO токены одновременно
               │
               │  YES + NO = $1.00 (нейтральная позиция)
               ▼
ФАЗА 2 ─── Выставляем SELL ордера на +1 цент от лучшего ASK
               │
               │  Ордера стоят в очереди ➜ Фармим PP за ликвидность
               ▼
ИТОГ   ─── Выходим в 0 или небольшой плюс + максимум Predict Points
```

### Почему это работает?

| Условие | Результат |
|---------|-----------|
| Ордера НЕ исполнились | Получаем PP за ликвидность, выходим в 0 |
| Один из ордеров исполнился | Получаем +$0.01 с продажи |
| Оба ордера исполнились | Выходим в безубыток |

> **Maker Fee = 0%** — размещение лимитных ордеров абсолютно бесплатно!

---

## 💎 Система Predict Points

Согласно [документации](https://docs.predict.fun/the-basics/how-to-earn-points):

- 📌 **Лимитные ордера** на обеих сторонах (bid + ask) — бонусные PP
- 📌 **Лучший bid/ask** в стакане — максимум PP
- 📌 **Чем ближе к рыночной цене** — тем больше PP
- 📌 **Держать позиции** на платформе — дополнительные PP
- 📌 **Награды раздаются каждые 7 дней**

---

## 🚀 Установка

### Требования

- Python 3.10–3.12
- Git

### Клонирование

```bash
git clone https://github.com/KIRSTa/predict-split-bot.git
cd predict-split-bot
```

### Виртуальное окружение

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
</details>

<details>
<summary>Linux / macOS</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate
```
</details>

### Зависимости

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

## ⚙️ Конфигурация

### 1. Создайте `.env` из шаблона

```bash
# Linux / macOS
cp .env.example .env

# Windows
copy .env.example .env
```

### 2. Заполните `.env`

```env
# ── Telegram ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=your_bot_token     # @BotFather
TELEGRAM_ADMIN_ID=your_telegram_id   # @userinfobot

# ── Predict.fun API ───────────────────────────────────────
PREDICT_API_KEY=your_api_key          # Discord → тикет в support

# ── Кошелёк ───────────────────────────────────────────────
# Вариант A — EOA кошелёк:
PRIVATE_KEY=0x...

# Вариант B — Predict Account (Smart Wallet):
PRIVY_WALLET_PRIVATE_KEY=0x...        # predict.fun/account/settings
PREDICT_ACCOUNT_ADDRESS=0x...         # Deposit Address

# ── Сеть ──────────────────────────────────────────────────
CHAIN_ID=56                           # 56 = BNB Mainnet, 97 = Testnet
RPC_URL=https://bsc-dataseed.binance.org/
```

### 3. Как получить API Key

1. Зайдите на Discord: [discord.gg/predictdotfun](https://discord.gg/predictdotfun)
2. Откройте тикет в поддержке
3. Запросите API key для торговли

> На **testnet** API key не нужен — можно тестировать сразу!

### 4. Мульти-аккаунт через `accounts.json`

```json
[
  {
    "name": "Main",
    "private_key": "0x...",
    "predict_account_address": "0x..."
  },
  {
    "name": "Secondary",
    "private_key": "0x...",
    "predict_account_address": "0x..."
  }
]
```

---

## 📖 Запуск

### Telegram-бот (рекомендуется)

```bash
python telegram_bot.py
```

Управляйте ботом прямо из Telegram:
- `/start` — главное меню
- Запуск/остановка фарминга по аккаунтам
- Уведомления о репозиционировании ордеров

### CLI режим

```bash
# Интерактивный выбор рынка
python predict_trader.py

# Указать конкретный рынок
python predict_trader.py --market 123

# Изменить размер ордера (USDT)
python predict_trader.py --market 123 --amount 10

# Изменить offset для SPLIT
python predict_trader.py --market 123 --offset 0.02

# Debug режим
python predict_trader.py --market 123 --debug
```

### Управление аккаунтами

```bash
python predict_trader.py --add-account     # Добавить аккаунт
python predict_trader.py --remove-account  # Удалить аккаунт
python predict_trader.py --list-accounts   # Список аккаунтов
```

---

## 📊 Пример вывода

```
📊 СТАТИСТИКА СЕССИИ
════════════════════════════════════════════════════════════
   Длительность:        2:30:15
   Размещено ордеров:   24
   Общий объём:         $120.00
   Время в книге:       2.5 ч
────────────────────────────────────────────────────────────
   💎 ПРИМЕРНО ПОИНТОВ: 45.2 PP
════════════════════════════════════════════════════════════
   ⚠️  Это приблизительная оценка!
   Точная формула PP не публикуется.
════════════════════════════════════════════════════════════
```

---

## 📁 Структура проекта

```
predict-split-bot/
├── 📄 predict_trader.py        # Основной бот — стратегия SPLIT
├── 📄 telegram_bot.py          # Telegram интерфейс управления
├── 📄 predict_api.py           # REST API клиент для Predict.fun
├── 📄 predict_ws.py            # WebSocket клиент (real-time)
├── 📄 find_binary_markets.py   # Поиск и фильтрация бинарных рынков
├── 📄 state.py                 # Менеджер состояния бота
├── 📄 config.py                # Конфигурация и константы
├── 📄 debug_api.py             # Утилиты для отладки API
├── 📄 requirements.txt         # Python зависимости
├── 📄 .env.example             # Шаблон конфигурации
├── 📄 accounts.json.example    # Шаблон мульти-аккаунт конфига
└── 📄 README.md
```

---

## ⚠️ Важные замечания

### Лимиты платформы

| Параметр | Значение |
|----------|----------|
| Минимальный ордер | $1 USDT |
| Макс. ордеров на рынок | 10 |
| Rate limit | 240 запросов/минуту |
| Maker Fee | **0%** ✅ |
| Taker Fee | 0.018% – 2% |

### Безопасность

> ⚠️ **КРИТИЧЕСКИ ВАЖНО**

- ❌ **НИКОГДА** не публикуйте `accounts.json` с реальными ключами
- ❌ **НИКОГДА** не добавляйте `.env` в git репозиторий
- ✅ Используйте отдельный кошелёк специально для бота
- ✅ Начинайте с **testnet** (CHAIN_ID=97) для тестирования
- ✅ Начинайте с минимальных сумм ($1–$5)

---

## 🔧 Устранение неполадок

**`401/403` от API** → Проверьте `PREDICT_API_KEY`, убедитесь что используете правильный endpoint (mainnet vs testnet)

**Не получает JWT** → Проверьте `PRIVATE_KEY` и тип аккаунта (EOA vs Predict Account)

**WebSocket не подключается** → Проверьте сеть/VPN, доступность `wss://ws.predict.fun/ws`

**Ошибки пакетов:**
```bash
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt --no-cache-dir
```

---

## 🔗 Полезные ссылки

| Ресурс | Ссылка |
|--------|--------|
| 🌐 Платформа | [predict.fun](https://predict.fun) |
| 📚 Документация | [docs.predict.fun](https://docs.predict.fun) |
| 🔌 API Docs | [dev.predict.fun](https://dev.predict.fun) |
| 🐍 Python SDK | [PyPI](https://pypi.org/project/predict-sdk/) · [GitHub](https://github.com/PredictDotFun/sdk-python) |
| 💬 Discord | [discord.gg/predictdotfun](https://discord.gg/predictdotfun) |

---

## 📜 Лицензия

MIT License — используйте свободно, но на свой страх и риск.

---

<div align="center">

**Disclaimer**: Программа предоставляется «как есть». Автор не несёт ответственности за финансовые потери. Используйте отдельный кошелёк и начинайте с малых сумм.

</div>
