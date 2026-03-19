# Predict.fun Trading Bot - SPLIT Strategy Points Farmer

🎯 **Бот для фарминга Predict Points (PP) на платформе [Predict.fun](https://predict.fun)**

## 📋 Описание

Этот бот реализует стратегию **SPLIT** для безрискового фарминга поинтов на prediction market платформе Predict.fun (BNB Chain).

### Как работает стратегия SPLIT:

1. **ФАЗА 1**: Покупаем одновременно YES и NO токены
   - YES + NO = 1.00 (при любом исходе получаем обратно $1)
   
2. **ФАЗА 2**: Выставляем SELL ордера на +1 цент от лучшего ASK
   - Ордера стоят в очереди и **НЕ исполняются** сразу
   - Фармим поинты за предоставление ликвидности
   
3. **РЕЗУЛЬТАТ**:
   - ✅ Независимо от исхода события выходим в 0 или небольшой плюс
   - ✅ Максимальный фарминг поинтов **БЕЗ РИСКА**!

## 💎 Система Predict Points (PP)

Согласно [документации](https://docs.predict.fun/the-basics/how-to-earn-points):

- **Лимитные ордера** = поинты (чем ближе к рынку = больше PP)
- **Лучший bid/ask** = МАКСИМУМ поинтов!
- **Ордера на обеих сторонах** (bid + ask) = бонусные поинты
- **Держать позиции** = дополнительные поинты
- **Maker Fee = 0%** (размещение ордеров БЕСПЛАТНО!)
- **Награды** распределяются каждые 7 дней

## 🚀 Установка

### 1. Клонировать репозиторий

```bash
cd "G:\Софты\Projects\Prediction fun"
cd Predict_Bot
```

### 2. Установить зависимости

```bash
pip install -r requirements.txt
```

### 3. Настроить конфигурацию

```bash
# Скопировать пример конфигурации
copy .env.example .env

# Отредактировать .env и указать свои ключи
notepad .env
```

## ⚙️ Конфигурация (.env)

```env
# API Key (получить в Discord: https://discord.gg/predictdotfun)
PREDICT_API_KEY=your_api_key_here

# Для EOA кошелька:
PRIVATE_KEY=your_private_key_here

# ИЛИ для Predict Account (Smart Wallet):
PRIVY_WALLET_PRIVATE_KEY=your_privy_wallet_private_key_here
PREDICT_ACCOUNT_ADDRESS=your_deposit_address_here

# Отступ от лучшего ASK (0.01 = 1 цент)
SPLIT_OFFSET=0.01
```

### Как получить API Key:

1. Присоединитесь к Discord: https://discord.gg/predictdotfun
2. Откройте тикет в поддержке
3. Запросите API key

### Как экспортировать Privy Wallet:

1. Перейдите в настройки аккаунта: https://predict.fun/account/settings
2. Экспортируйте приватный ключ Privy Wallet
3. Скопируйте Deposit Address (это ваш Predict Account Address)

## 📖 Использование

### Интерактивный режим (рекомендуется)

```bash
python predict_trader.py
```

Бот покажет список бинарных рынков и предложит выбрать один из них.

### Указать конкретный рынок

```bash
python predict_trader.py --market 123
```

### Изменить размер ордера

```bash
python predict_trader.py --market 123 --amount 10
```

### Изменить offset для SPLIT

```bash
python predict_trader.py --market 123 --offset 0.02
```

### Debug режим

```bash
python predict_trader.py --market 123 --debug
```

## 📊 Примерная оценка поинтов

Бот автоматически подсчитывает примерное количество заработанных поинтов:

```
📊 СТАТИСТИКА СЕССИИ
============================================================
   Длительность: 2:30:15
   Размещено ордеров: 24
   Общий объём: $120.00
   Время в книге: 2.5 часов
------------------------------------------------------------
   💎 ПРИМЕРНО ПОИНТОВ: 45.2 PP
============================================================
   ⚠️  Это приблизительная оценка!
   Точная формула PP не публикуется.
============================================================
```

## 📁 Структура проекта

```
Predict_Bot/
├── config.py           # Конфигурация и константы
├── predict_api.py      # REST API клиент для Predict.fun
├── predict_trader.py   # Основной бот со стратегией SPLIT
├── requirements.txt    # Зависимости Python
├── .env.example        # Пример конфигурации
└── README.md           # Документация
```

## 🔧 API Документация

- **REST API**: https://dev.predict.fun/
- **Docs**: https://docs.predict.fun/
- **Python SDK**: https://github.com/PredictDotFun/sdk-python
- **Alternative UI**: https://api.predict.fun/docs

## ⚠️ Важные замечания

### Лимиты платформы:

- Минимальный ордер: **$1 USDT**
- Максимум ордеров на рынок: **10** лимитных ордеров
- Rate limit: **240 запросов/минуту**

### Комиссии:

- **Maker Fee: 0%** (лимитные ордера БЕСПЛАТНО!)
- **Taker Fee: 0.018% - 2%** (зависит от цены)
- Формула: `Fee = Base Fee % × min(Price, 1-Price) × Shares`
- 10% скидка по реферальной программе

### Безопасность:

- ❌ НИКОГДА не делитесь приватным ключом
- ✅ Используйте отдельный кошелёк для бота
- ✅ Начните с малых сумм для тестирования
- ✅ На testnet API key не требуется

## 🔗 Полезные ссылки

- [Predict.fun](https://predict.fun) - Платформа
- [Документация](https://docs.predict.fun/) - Общая документация
- [API Docs](https://dev.predict.fun/) - Техническая документация
- [Discord](https://discord.gg/predictdotfun) - Сообщество
- [Python SDK](https://pypi.org/project/predict-sdk/) - PyPI пакет

## 📜 Лицензия

MIT License

---

**Disclaimer**: Это программное обеспечение предоставляется "как есть". Используйте на свой страх и риск. Автор не несёт ответственности за любые финансовые потери.
