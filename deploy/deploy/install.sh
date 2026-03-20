#!/bin/bash
# =============================================================================
# Скрипт установки Predict Bot на сервер
# =============================================================================

set -e

echo "🚀 Установка Predict Bot..."

# Проверяем Python
if ! command -v python3 &> /dev/null; then
    echo "📦 Установка Python3..."
    sudo apt update
    sudo apt install -y python3 python3-pip python3-venv
fi

# Создаём директорию
BOT_DIR="/opt/predict_bot"
sudo mkdir -p $BOT_DIR
sudo chown $USER:$USER $BOT_DIR

# Копируем файлы (если запускаем локально)
echo "📁 Директория бота: $BOT_DIR"

# Создаём виртуальное окружение
echo "🐍 Создание виртуального окружения..."
cd $BOT_DIR
python3 -m venv venv
source venv/bin/activate

# Устанавливаем зависимости
echo "📦 Установка зависимостей..."
pip install --upgrade pip
pip install -r requirements.txt

echo "✅ Установка завершена!"
echo ""
echo "Следующие шаги:"
echo "1. Скопируйте .env файл: nano $BOT_DIR/.env"
echo "2. Установите сервис: sudo cp $BOT_DIR/deploy/predict_bot.service /etc/systemd/system/"
echo "3. Запустите: sudo systemctl enable predict_bot && sudo systemctl start predict_bot"
