# -*- coding: utf-8 -*-
"""
Persistent State Manager
========================

Персистентное хранение состояния бота между перезагрузками.

Сохраняет:
- Активные задачи мониторинга (какие аккаунты запущены)
- Рынки для каждого аккаунта (market_ids)
- Информацию о SPLIT позициях (исходные суммы, текущие балансы)
- Историю исполнений ордеров для отслеживания разбалансировки

Формат файла bot_state.json:
{
    "version": 1,
    "last_saved": "2025-02-13T12:00:00",
    "accounts": {
        "0x...address...": {
            "name": "Main",
            "running": true,
            "markets": {
                "12345": {
                    "title": "Market Name",
                    "split_phase": 2,
                    "original_amount": 100.0,
                    "yes_position": 100.0,
                    "no_position": 100.0,
                    "yes_sold": 0.0,
                    "no_sold": 0.0,
                    "orders": {
                        "yes_sell": {"id": "...", "price": 0.65, "quantity": 100},
                        "no_sell": {"id": "...", "price": 0.35, "quantity": 100}
                    },
                    "created_at": "2025-02-10T10:00:00",
                    "last_check": "2025-02-13T12:00:00"
                }
            }
        }
    },
    "settings": {
        "error_notifications": true,
        "repositioning_notifications": true
    }
}
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict, field
from threading import Lock

logger = logging.getLogger(__name__)

# Путь к файлу состояния
STATE_FILE = os.path.join(os.path.dirname(__file__), 'bot_state.json')

# Версия формата состояния (для миграций)
STATE_VERSION = 1


@dataclass
class OrderState:
    """Состояние ордера"""
    id: str = ""
    price: float = 0.0
    quantity: float = 0.0
    original_quantity: float = 0.0
    filled: float = 0.0  # Исполнено


@dataclass
class MarketTaskState:
    """Состояние задачи мониторинга рынка"""
    market_id: int
    title: str = ""
    split_phase: int = 0  # 0=не начат, 1=токены куплены, 2=ордера выставлены
    
    # Исходная сумма SPLIT (для отслеживания)
    original_amount: float = 0.0
    
    # Текущие позиции
    yes_position: float = 0.0
    no_position: float = 0.0
    
    # Сколько уже продано (исполнено)
    yes_sold: float = 0.0
    no_sold: float = 0.0
    
    # Текущие ордера
    yes_order: Optional[OrderState] = None
    no_order: Optional[OrderState] = None
    
    # Временные метки
    created_at: str = ""
    last_check: str = ""
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
    
    def update_sold(self, yes_filled: float = 0.0, no_filled: float = 0.0):
        """Обновить количество проданных токенов"""
        self.yes_sold += yes_filled
        self.no_sold += no_filled
    
    def get_imbalance(self) -> float:
        """
        Получить дисбаланс между YES и NO
        
        Returns:
            Разница (положительная = больше продано YES)
        """
        return self.yes_sold - self.no_sold
    
    def is_balanced(self, threshold: float = 1.0) -> bool:
        """
        Проверить, сбалансированы ли позиции
        
        Args:
            threshold: Допустимый дисбаланс в токенах
            
        Returns:
            True если позиции сбалансированы
        """
        return abs(self.get_imbalance()) <= threshold
    
    def to_dict(self) -> Dict:
        """Конвертировать в словарь для JSON"""
        return {
            'market_id': self.market_id,
            'title': self.title,
            'split_phase': self.split_phase,
            'original_amount': self.original_amount,
            'yes_position': self.yes_position,
            'no_position': self.no_position,
            'yes_sold': self.yes_sold,
            'no_sold': self.no_sold,
            'yes_order': asdict(self.yes_order) if self.yes_order else None,
            'no_order': asdict(self.no_order) if self.no_order else None,
            'created_at': self.created_at,
            'last_check': self.last_check,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'MarketTaskState':
        """Восстановить из словаря"""
        yes_order = None
        no_order = None
        
        if data.get('yes_order'):
            yes_order = OrderState(**data['yes_order'])
        if data.get('no_order'):
            no_order = OrderState(**data['no_order'])
        
        return cls(
            market_id=data['market_id'],
            title=data.get('title', ''),
            split_phase=data.get('split_phase', 0),
            original_amount=data.get('original_amount', 0.0),
            yes_position=data.get('yes_position', 0.0),
            no_position=data.get('no_position', 0.0),
            yes_sold=data.get('yes_sold', 0.0),
            no_sold=data.get('no_sold', 0.0),
            yes_order=yes_order,
            no_order=no_order,
            created_at=data.get('created_at', ''),
            last_check=data.get('last_check', ''),
        )


@dataclass
class AccountTaskState:
    """Состояние аккаунта"""
    address: str
    name: str = ""
    running: bool = False
    markets: Dict[int, MarketTaskState] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        """Конвертировать в словарь для JSON"""
        return {
            'name': self.name,
            'running': self.running,
            'markets': {
                str(mid): market.to_dict()
                for mid, market in self.markets.items()
            }
        }
    
    @classmethod
    def from_dict(cls, address: str, data: Dict) -> 'AccountTaskState':
        """Восстановить из словаря"""
        markets = {}
        for mid_str, market_data in data.get('markets', {}).items():
            mid = int(mid_str)
            markets[mid] = MarketTaskState.from_dict(market_data)
        
        return cls(
            address=address,
            name=data.get('name', ''),
            running=data.get('running', False),
            markets=markets,
        )


class PersistentState:
    """
    Менеджер персистентного состояния
    
    Потокобезопасный, автоматически сохраняет изменения.
    """
    
    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self._lock = Lock()
        
        # Состояние
        self.accounts: Dict[str, AccountTaskState] = {}
        self.settings: Dict[str, Any] = {
            'error_notifications': True,
            'repositioning_notifications': True,
        }
        self.last_saved: str = ""
        
        # Загружаем при создании
        self.load()
    
    def load(self) -> bool:
        """
        Загрузить состояние из файла
        
        Returns:
            True если успешно загружено
        """
        with self._lock:
            if not os.path.exists(self.state_file):
                logger.info("📂 Файл состояния не существует, создаём новый")
                return False
            
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # Проверяем версию
                version = data.get('version', 1)
                if version != STATE_VERSION:
                    logger.warning(f"⚠️  Версия состояния {version} != {STATE_VERSION}, нужна миграция")
                    # TODO: миграция
                
                # Загружаем аккаунты
                self.accounts = {}
                for address, acc_data in data.get('accounts', {}).items():
                    self.accounts[address] = AccountTaskState.from_dict(address, acc_data)
                
                # Загружаем настройки
                self.settings = data.get('settings', self.settings)
                self.last_saved = data.get('last_saved', '')
                
                # Статистика
                total_markets = sum(len(acc.markets) for acc in self.accounts.values())
                running_accounts = sum(1 for acc in self.accounts.values() if acc.running)
                
                logger.info(f"✅ Загружено состояние: {len(self.accounts)} аккаунтов, "
                           f"{running_accounts} запущено, {total_markets} рынков")
                
                return True
                
            except Exception as e:
                logger.error(f"❌ Ошибка загрузки состояния: {e}")
                return False
    
    def save(self) -> bool:
        """
        Сохранить состояние в файл
        
        Returns:
            True если успешно сохранено
        """
        with self._lock:
            try:
                data = {
                    'version': STATE_VERSION,
                    'last_saved': datetime.now().isoformat(),
                    'accounts': {
                        address: acc.to_dict()
                        for address, acc in self.accounts.items()
                    },
                    'settings': self.settings,
                }
                
                # Сначала пишем во временный файл
                temp_file = self.state_file + '.tmp'
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                
                # Затем атомарно переименовываем
                os.replace(temp_file, self.state_file)
                
                self.last_saved = data['last_saved']
                logger.debug(f"💾 Состояние сохранено: {self.last_saved}")
                
                return True
                
            except Exception as e:
                logger.error(f"❌ Ошибка сохранения состояния: {e}")
                return False
    
    # =========================================================================
    # АККАУНТЫ
    # =========================================================================
    
    def get_account(self, address: str) -> Optional[AccountTaskState]:
        """Получить состояние аккаунта"""
        return self.accounts.get(address)
    
    def set_account_running(self, address: str, name: str, running: bool):
        """Установить статус запуска аккаунта"""
        if address not in self.accounts:
            self.accounts[address] = AccountTaskState(address=address, name=name)
        
        self.accounts[address].running = running
        self.accounts[address].name = name
        self.save()
    
    def get_running_accounts(self) -> List[str]:
        """Получить список адресов запущенных аккаунтов"""
        return [addr for addr, acc in self.accounts.items() if acc.running]
    
    # =========================================================================
    # РЫНКИ
    # =========================================================================
    
    def add_market(self, address: str, market_id: int, title: str = "", 
                   original_amount: float = 0.0) -> MarketTaskState:
        """
        Добавить или обновить рынок для мониторинга
        
        Args:
            address: Адрес аккаунта
            market_id: ID рынка
            title: Название рынка
            original_amount: Исходная сумма SPLIT
            
        Returns:
            Состояние рынка
        """
        if address not in self.accounts:
            self.accounts[address] = AccountTaskState(address=address)
        
        acc = self.accounts[address]
        
        if market_id not in acc.markets:
            acc.markets[market_id] = MarketTaskState(
                market_id=market_id,
                title=title,
                original_amount=original_amount,
            )
        else:
            # Обновляем существующий
            if title:
                acc.markets[market_id].title = title
            if original_amount > 0:
                acc.markets[market_id].original_amount = original_amount
        
        self.save()
        return acc.markets[market_id]
    
    def update_market(self, address: str, market_id: int, **kwargs) -> Optional[MarketTaskState]:
        """
        Обновить состояние рынка
        
        Args:
            address: Адрес аккаунта
            market_id: ID рынка
            **kwargs: Поля для обновления
            
        Returns:
            Обновлённое состояние или None
        """
        acc = self.accounts.get(address)
        if not acc or market_id not in acc.markets:
            return None
        
        market = acc.markets[market_id]
        
        for key, value in kwargs.items():
            if hasattr(market, key):
                setattr(market, key, value)
        
        market.last_check = datetime.now().isoformat()
        self.save()
        
        return market
    
    def remove_market(self, address: str, market_id: int):
        """Удалить рынок из мониторинга"""
        acc = self.accounts.get(address)
        if acc and market_id in acc.markets:
            del acc.markets[market_id]
            self.save()
    
    def get_account_markets(self, address: str) -> Dict[int, MarketTaskState]:
        """Получить все рынки аккаунта"""
        acc = self.accounts.get(address)
        return acc.markets if acc else {}
    
    # =========================================================================
    # ОТСЛЕЖИВАНИЕ ИСПОЛНЕНИЙ
    # =========================================================================
    
    def record_fill(self, address: str, market_id: int, 
                    yes_filled: float = 0.0, no_filled: float = 0.0):
        """
        Записать исполнение ордеров
        
        Args:
            address: Адрес аккаунта
            market_id: ID рынка
            yes_filled: Исполнено YES токенов
            no_filled: Исполнено NO токенов
        """
        acc = self.accounts.get(address)
        if not acc or market_id not in acc.markets:
            return
        
        market = acc.markets[market_id]
        market.update_sold(yes_filled, no_filled)
        market.last_check = datetime.now().isoformat()
        
        # Логируем дисбаланс
        imbalance = market.get_imbalance()
        if abs(imbalance) > 1.0:
            logger.warning(f"⚠️  Дисбаланс на рынке #{market_id}: "
                          f"YES sold={market.yes_sold:.2f}, NO sold={market.no_sold:.2f}, "
                          f"разница={imbalance:.2f}")
        
        self.save()
    
    def get_imbalanced_markets(self, address: str, threshold: float = 1.0) -> List[MarketTaskState]:
        """
        Получить рынки с дисбалансом
        
        Returns:
            Список рынков где YES sold != NO sold
        """
        acc = self.accounts.get(address)
        if not acc:
            return []
        
        return [m for m in acc.markets.values() if not m.is_balanced(threshold)]
    
    # =========================================================================
    # НАСТРОЙКИ
    # =========================================================================
    
    def get_setting(self, key: str, default: Any = None) -> Any:
        """Получить настройку"""
        return self.settings.get(key, default)
    
    def set_setting(self, key: str, value: Any):
        """Установить настройку"""
        self.settings[key] = value
        self.save()
    
    # =========================================================================
    # УТИЛИТЫ
    # =========================================================================
    
    def get_summary(self) -> Dict:
        """Получить сводку по состоянию"""
        total_markets = 0
        phase_2_markets = 0
        total_imbalance = 0.0
        
        for acc in self.accounts.values():
            for market in acc.markets.values():
                total_markets += 1
                if market.split_phase >= 2:
                    phase_2_markets += 1
                total_imbalance += abs(market.get_imbalance())
        
        return {
            'accounts': len(self.accounts),
            'running': len(self.get_running_accounts()),
            'total_markets': total_markets,
            'active_markets': phase_2_markets,
            'total_imbalance': total_imbalance,
            'last_saved': self.last_saved,
        }
    
    def clear_account(self, address: str):
        """Очистить данные аккаунта"""
        if address in self.accounts:
            del self.accounts[address]
            self.save()
    
    def clear_all(self):
        """Очистить всё состояние"""
        self.accounts = {}
        self.save()


# =============================================================================
# ГЛОБАЛЬНЫЙ ЭКЗЕМПЛЯР
# =============================================================================

# Создаём глобальный экземпляр для использования во всём приложении
persistent_state = PersistentState()


def get_state() -> PersistentState:
    """Получить глобальный экземпляр состояния"""
    return persistent_state
