# 🚀 Деплой Predict Bot на VPS

## Шаг 1: Подключение к серверу

```bash
ssh root@94.156.112.97
```

## Шаг 2: Установка необходимого ПО

```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git
```

## Шаг 3: Создание директории и загрузка файлов

```bash
mkdir -p /opt/predict_bot
cd /opt/predict_bot
```

## Шаг 4: Загрузка файлов (с локального компьютера)

На вашем Windows компьютере откройте PowerShell и выполните:

```powershell
# Копируем все файлы на сервер
scp -r "G:\Софты\Projects\Prediction fun\Predict_Bot\*" root@94.156.112.97:/opt/predict_bot/
```

Или через WinSCP:
1. Подключитесь к 94.156.112.97
2. Перейдите в /opt/predict_bot
3. Загрузите все файлы из проекта

## Шаг 5: Настройка на сервере

```bash
cd /opt/predict_bot

# Создаём виртуальное окружение
python3 -m venv venv
source venv/bin/activate

# Устанавливаем зависимости
pip install --upgrade pip
pip install -r requirements.txt
```

## Шаг 6: Настройка .env

```bash
nano /opt/predict_bot/.env
```

Убедитесь, что все переменные заполнены:
```
PREDICT_API_KEY=your_api_key
PRIVY_WALLET_PRIVATE_KEY=your_private_key
PREDICT_ACCOUNT_ADDRESS=0x...
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_ADMIN_ID=your_telegram_id
```

## Шаг 7: Тестовый запуск

```bash
cd /opt/predict_bot
source venv/bin/activate
python telegram_bot.py
```

Если работает - нажмите Ctrl+C и продолжайте.

## Шаг 8: Установка как системный сервис

```bash
# Копируем service файл
cp /opt/predict_bot/deploy/predict_bot.service /etc/systemd/system/

# Перезагружаем systemd
systemctl daemon-reload

# Включаем автозапуск
systemctl enable predict_bot

# Запускаем
systemctl start predict_bot

# Проверяем статус
systemctl status predict_bot
```

## Управление ботом

```bash
# Статус
systemctl status predict_bot

# Остановить
systemctl stop predict_bot

# Запустить
systemctl start predict_bot

# Перезапустить
systemctl restart predict_bot

# Логи (последние 100 строк)
tail -100 /var/log/predict_bot.log

# Логи в реальном времени
tail -f /var/log/predict_bot.log

# Логи ошибок
tail -f /var/log/predict_bot_error.log
```

## Обновление бота

```bash
# Останавливаем
systemctl stop predict_bot

# Загружаем новые файлы (с Windows)
# scp -r "G:\Софты\Projects\Prediction fun\Predict_Bot\*.py" root@94.156.112.97:/opt/predict_bot/

# Запускаем
systemctl start predict_bot
```

## Решение проблем

### Бот не запускается
```bash
# Смотрим логи
journalctl -u predict_bot -n 50

# Или
cat /var/log/predict_bot_error.log
```

### Нет интернета
```bash
ping google.com
```

### Проблемы с Python
```bash
cd /opt/predict_bot
source venv/bin/activate
python -c "import telegram; print('OK')"
```
