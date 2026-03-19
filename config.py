# -*- coding: utf-8 -*-
"""
Predict.fun Bot Configuration
=============================

Конфигурация для бота Predict.fun
"""

import os
import json
from dotenv import load_dotenv, set_key
from enum import IntEnum
from dataclasses import dataclass
from typing import List, Optional

# Загружаем переменные окружения
load_dotenv()


@dataclass
class AccountConfig:
    """Конфигурация одного аккаунта"""
    name: str
    private_key: str  # PRIVATE_KEY или PRIVY_WALLET_PRIVATE_KEY
    predict_account_address: str  # Predict Account адрес
    
    def __str__(self):
        addr = f"{self.predict_account_address[:6]}...{self.predict_account_address[-4:]}" if self.predict_account_address else '???'
        return f"{self.name} ({addr})"


class ChainId(IntEnum):
    """Поддерживаемые сети"""
    BNB_MAINNET = 56
    BNB_TESTNET = 97


# =============================================================================
# API НАСТРОЙКИ
# =============================================================================

# Predict.fun API
API_KEY = os.getenv('PREDICT_API_KEY', '')
BASE_URL_MAINNET = "https://api.predict.fun"
BASE_URL_TESTNET = "https://api-testnet.predict.fun"

# Выбор сети
CHAIN_ID = int(os.getenv('CHAIN_ID', '56'))
BASE_URL = BASE_URL_MAINNET if CHAIN_ID == ChainId.BNB_MAINNET else BASE_URL_TESTNET

# Кошелёк
PRIVATE_KEY = os.getenv('PRIVATE_KEY', '')
PRIVY_WALLET_PRIVATE_KEY = os.getenv('PRIVY_WALLET_PRIVATE_KEY', '')
PREDICT_ACCOUNT_ADDRESS = os.getenv('PREDICT_ACCOUNT_ADDRESS', '')

# RPC URL
RPC_URL = os.getenv('RPC_URL', 'https://bsc-dataseed.binance.org/')


# =============================================================================
# МУЛЬТИ-АККАУНТЫ
# =============================================================================

# Путь к файлу с аккаунтами
ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), 'accounts.json')


def load_accounts() -> List[AccountConfig]:
    """
    Загрузить аккаунты из accounts.json
    
    Формат файла:
    [
        {
            "name": "Main",
            "private_key": "0x...",
            "predict_account_address": "0x..."
        },
        ...
    ]
    """
    accounts = []
    
    # Пробуем загрузить из JSON файла
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for i, acc in enumerate(data):
                    accounts.append(AccountConfig(
                        name=acc.get('name', f'Account {i+1}'),
                        private_key=acc.get('private_key', ''),
                        predict_account_address=acc.get('predict_account_address', '')
                    ))
        except Exception as e:
            print(f"⚠️  Ошибка загрузки accounts.json: {e}")
    
    # Если нет JSON, используем .env (обратная совместимость)
    if not accounts and PRIVY_WALLET_PRIVATE_KEY and PREDICT_ACCOUNT_ADDRESS:
        accounts.append(AccountConfig(
            name='Main',
            private_key=PRIVY_WALLET_PRIVATE_KEY,
            predict_account_address=PREDICT_ACCOUNT_ADDRESS
        ))
    
    return accounts


def save_accounts(accounts: List[AccountConfig]) -> bool:
    """Сохранить аккаунты в accounts.json"""
    try:
        data = []
        for acc in accounts:
            data.append({
                'name': acc.name,
                'private_key': acc.private_key,
                'predict_account_address': acc.predict_account_address
            })
        
        with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        return True
    except Exception as e:
        print(f"❌ Ошибка сохранения accounts.json: {e}")
        return False


def add_account_interactive() -> Optional[AccountConfig]:
    """
    Интерактивное добавление нового аккаунта
    
    Returns:
        AccountConfig или None если отмена
    """
    print("\n" + "="*60)
    print("➕ ДОБАВЛЕНИЕ НОВОГО АККАУНТА")
    print("="*60)
    
    # Имя аккаунта
    name = input("   Имя аккаунта (например 'Main'): ").strip()
    if not name:
        name = f"Account {len(load_accounts()) + 1}"
    
    # Private Key
    print("\n   Введите PRIVY_WALLET_PRIVATE_KEY")
    print("   (экспортировать из https://predict.fun/account/settings)")
    private_key = input("   Private Key (0x...): ").strip()
    
    if not private_key:
        print("❌ Private Key обязателен!")
        return None
    
    if not private_key.startswith('0x'):
        private_key = '0x' + private_key
    
    # Predict Account Address
    print("\n   Введите PREDICT_ACCOUNT_ADDRESS")
    print("   (адрес вашего Predict Account / Smart Wallet)")
    address = input("   Address (0x...): ").strip()
    
    if not address:
        print("❌ Address обязателен!")
        return None
    
    if not address.startswith('0x'):
        address = '0x' + address
    
    # Создаём конфиг
    account = AccountConfig(
        name=name,
        private_key=private_key,
        predict_account_address=address
    )
    
    # Сохраняем
    accounts = load_accounts()
    accounts.append(account)
    
    if save_accounts(accounts):
        print(f"\n✅ Аккаунт '{name}' добавлен!")
        print(f"   Адрес: {address[:10]}...{address[-6:]}")
        return account
    
    return None


def remove_account_interactive() -> bool:
    """Интерактивное удаление аккаунта"""
    accounts = load_accounts()
    
    if not accounts:
        print("📭 Нет аккаунтов для удаления")
        return False
    
    print("\n" + "="*60)
    print("➖ УДАЛЕНИЕ АККАУНТА")
    print("="*60)
    
    for i, acc in enumerate(accounts):
        print(f"   [{i+1}] {acc}")
    
    print(f"   [0] Отмена")
    
    try:
        choice = int(input("\n   Выберите аккаунт: ").strip())
        if choice == 0:
            return False
        if 1 <= choice <= len(accounts):
            removed = accounts.pop(choice - 1)
            if save_accounts(accounts):
                print(f"\n✅ Аккаунт '{removed.name}' удалён!")
                return True
    except ValueError:
        pass
    
    print("❌ Неверный выбор")
    return False


def list_accounts():
    """Показать список аккаунтов"""
    accounts = load_accounts()
    
    print("\n" + "="*60)
    print(f"📋 АККАУНТЫ ({len(accounts)})")
    print("="*60)
    
    if not accounts:
        print("   📭 Нет настроенных аккаунтов")
        print("   Используйте --add-account для добавления")
    else:
        for i, acc in enumerate(accounts):
            print(f"   [{i+1}] {acc}")
    
    print("="*60)


# Загружаем аккаунты при импорте
ACCOUNTS = load_accounts()


# =============================================================================
# MULTI-USER / REFERRAL SYSTEM
# =============================================================================

# Папка данных пользователей
USERS_DIR = os.path.join(os.path.dirname(__file__), 'users')

# Реферальная ссылка (показывается новым пользователям)
REFERRAL_LINK = os.getenv('REFERRAL_LINK', 'https://predict.fun?ref=4295F')

# Сообщение при отключении пользователя
DISCONNECT_MESSAGE = os.getenv(
    'DISCONNECT_MESSAGE',
    'Вы отключены от бота из-за несоблюдения условий.'
)

# Максимум аккаунтов Predict.fun для одного реферала
MAX_USER_ACCOUNTS = int(os.getenv('MAX_USER_ACCOUNTS', '1'))


# =============================================================================
# ЛИМИТЫ ПЛАТФОРМЫ
# =============================================================================

# Минимальный размер ордера
MIN_ORDER_USDT = 1.0

# Максимум лимитных ордеров на рынок
MAX_ORDERS_PER_MARKET = 10

# Ограничение запросов: 240 запросов в минуту
RATE_LIMIT = 240


# =============================================================================
# КОМИССИИ
# =============================================================================

# Maker Fee = 0% (бесплатно!)
MAKER_FEE_BPS = 0

# Taker Fee: 0.018% - 2% в зависимости от цены
# Fee = Base Fee % × min(Price, 1 - Price) × Shares
# При 10% скидке по реферальной программе: × 0.9
TAKER_BASE_FEE_BPS = 200  # 2%
FEE_DISCOUNT = 0.1  # 10% скидка по рефералу


# =============================================================================
# НАСТРОЙКИ СТРАТЕГИИ SPLIT
# =============================================================================

# Отступ от лучшего ASK при выставлении SELL ордера
# 0.01 = 1 цент (1% от $1)
SPLIT_OFFSET = float(os.getenv('SPLIT_OFFSET', '0.01'))

# Минимальный размер ордера для SPLIT стратегии
SPLIT_MIN_ORDER_SIZE = 1.0


# =============================================================================
# WEBSOCKET
# =============================================================================

# WebSocket URL для real-time подписок
WS_URL = os.getenv('WS_URL', 'wss://ws.predict.fun/ws')

# Включить WebSocket мониторинг (вместо polling)
# WebSocket реагирует на изменения стакана мгновенно (<1 сек)
# Polling проверяет каждые CHECK_INTERVAL секунд
USE_WEBSOCKET = os.getenv('USE_WEBSOCKET', 'true').lower() in ('true', '1', 'yes')


# =============================================================================
# ИНТЕРВАЛЫ
# =============================================================================

# Интервал проверки между циклами (секунды)
# Чем меньше — тем быстрее обнаруживаем опасную позицию в стакане
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '7'))

# Интервал для агрессивных стратегий (ещё быстрее)
CHECK_INTERVAL_AGGRESSIVE = 5

# Задержка ПОСЛЕ отмены ордера перед созданием нового (секунды)
# Защита от быстрого исполнения при резких изменениях стакана
# После отмены бот ЖДЁТ N секунд, пока ситуация в стакане не стабилизируется
REPOSITION_DELAY = int(os.getenv('REPOSITION_DELAY', '15'))

# Автозакрытие второй стороны по рынку при 100% fill первой стороны
# true  = сразу закрываем остаток по текущему BID (быстро снимаем риск перекоса)
# false = не закрываем по рынку (более мягкий режим)
AUTO_MARKET_EXIT_ON_FULL_FILL = os.getenv('AUTO_MARKET_EXIT_ON_FULL_FILL', 'true').lower() in ('true', '1', 'yes')

# Позиция ордера в стакане (считая от лучшего чужого ASK)
# 1 = сразу за лучшим ASK (2-е место) — больше поинтов, но больше риск филла
# 2 = за 2-м уровнем (3-е место) — безопаснее, но меньше поинтов
# 3 = за 3-м уровнем (4-е место) — макс. безопасность, но меньше всего поинтов
# Бот ищет N-й реальный ценовой уровень в стакане и ставит на 1 тик выше
# Это гарантирует ОДИНАКОВУЮ позицию в стакане на ВСЕХ рынках
# Бот отменяет ордер при отклонении >= 1 тик в ЛЮБОМ направлении
# (как ближе к best ASK, так и дальше от него)
ASK_POSITION_OFFSET = int(os.getenv('ASK_POSITION_OFFSET', '2'))

# Интервал обновления списка рынков (обнаружение новых/закрытых)
# U predict.fun нет WS-канала для этого, поэтому поллим через REST API
WS_MARKET_REFRESH_INTERVAL = int(os.getenv('WS_MARKET_REFRESH_INTERVAL', '30'))

# Интервал fallback-проверки при WebSocket режиме (секунды)
# WebSocket обрабатывает 99% событий мгновенно, fallback нужен редко
WS_FALLBACK_INTERVAL = int(os.getenv('WS_FALLBACK_INTERVAL', '300'))


# =============================================================================
# СИСТЕМА ПОИНТОВ (Predict Points - PP)
# =============================================================================

"""
Как заработать Predict Points (PP):

1. РАЗМЕЩАТЬ ЛИМИТНЫЕ ОРДЕРА (главное!)
   - Предоставлять ликвидность на рынках
   - Минимальное количество акций (зависит от рынка)
   - Спред bid-ask должен быть ниже порога (зависит от рынка)
   - Ордера на обеих сторонах (bid и ask) = больше поинтов
   - Ордера ближе к рыночной цене = больше поинтов
   - Лучший ask и лучший bid = максимум поинтов!

2. ДЕРЖАТЬ ПОЗИЦИИ
   - Поддерживать значимые позиции на платформе
   - Позиции в разных рынках

3. НАГРАДЫ
   - Распределение каждые 7 дней
   - Таблица лидеров обновляется каждую неделю
   - Расчёт занимает 2-3 дня после окончания недели

ВАЖНО: Maker Fee = 0%, поэтому размещение лимитных ордеров БЕСПЛАТНО!
"""

# Минимальный спред для активации поинтов (зависит от рынка)
# Проверять spreadThreshold в данных рынка
POINTS_MAX_SPREAD = 0.10  # 10% - типичный порог

# Минимальное количество акций для поинтов (зависит от рынка)
# Проверять shareThreshold в данных рынка
POINTS_MIN_SHARES = 100


# =============================================================================
# ЛОГИРОВАНИЕ
# =============================================================================

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'


def validate_config() -> bool:
    """
    Проверить обязательные настройки конфигурации
    
    Returns:
        True если конфигурация валидна
    """
    errors = []
    
    if CHAIN_ID == ChainId.BNB_MAINNET:
        if not API_KEY:
            errors.append("PREDICT_API_KEY обязателен для Mainnet")
    
    # Проверяем что есть хотя бы один аккаунт
    if not ACCOUNTS:
        # Проверяем старый формат .env
        if not PRIVATE_KEY and not PRIVY_WALLET_PRIVATE_KEY:
            errors.append("Нет настроенных аккаунтов! Используйте --add-account")
        elif PRIVY_WALLET_PRIVATE_KEY and not PREDICT_ACCOUNT_ADDRESS:
            errors.append("PREDICT_ACCOUNT_ADDRESS обязателен при использовании Privy Wallet")
    
    if errors:
        print("❌ Ошибки конфигурации:")
        for error in errors:
            print(f"   - {error}")
        return False
    
    return True


def print_config():
    """Вывести текущую конфигурацию (без секретов)"""
    print("\n" + "="*60)
    print("⚙️  КОНФИГУРАЦИЯ PREDICT.FUN BOT")
    print("="*60)
    print(f"   Сеть: {'BNB Mainnet' if CHAIN_ID == ChainId.BNB_MAINNET else 'BNB Testnet'}")
    print(f"   API URL: {BASE_URL}")
    print(f"   API Key: {'✅ Установлен' if API_KEY else '❌ Не установлен'}")
    print(f"   SPLIT offset: {SPLIT_OFFSET*100:.1f}%")
    if USE_WEBSOCKET:
        print(f"   Мониторинг: ⚡ WebSocket (real-time)")
        print(f"   WebSocket URL: {WS_URL}")
        print(f"   Обновление рынков: каждые {WS_MARKET_REFRESH_INTERVAL} сек")
        print(f"   Fallback интервал: {WS_FALLBACK_INTERVAL} сек")
    else:
        print(f"   Мониторинг: 🔄 Polling (каждые {CHECK_INTERVAL} сек)")
    print(f"   Задержка после отмены: {REPOSITION_DELAY} сек")
    print(f"   Auto market-exit on full fill: {'ON' if AUTO_MARKET_EXIT_ON_FULL_FILL else 'OFF'}")
    
    # Показываем аккаунты
    accounts = load_accounts()  # Перезагружаем для актуальности
    print(f"\n   📊 Аккаунтов: {len(accounts)}")
    for acc in accounts:
        print(f"   • {acc}")
    
    print("="*60 + "\n")
