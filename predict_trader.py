# -*- coding: utf-8 -*-
"""
Predict.fun Trading Bot - SPLIT Strategy Points Farmer
=======================================================

ОПТИМИЗИРОВАНО ДЛЯ МАКСИМИЗАЦИИ PREDICT POINTS (PP)!

СТРАТЕГИЯ SPLIT (БЕЗРИСКОВЫЙ ФАРМИНГ):
======================================
1. ФАЗА 1: Покупаем одновременно YES и NO токены через SPLIT
   - YES + NO = 1, поэтому при любом исходе получаем обратно $1
   
2. ФАЗА 2: Выставляем SELL ордера на 2-е место от лучшего ASK
   - Ордера стоят в очереди, НЕ исполняются сразу
   - Фармим поинты за предоставление ликвидности
   
3. ФАЗА 3 (АВТОМАТИЧЕСКАЯ): Постоянный мониторинг и переставление
   - Бот проверяет позицию ордеров каждый цикл
   - Если кто-то встал выше нас - переставляем на 2-е место
   - Если мы стали лучшим ASK (опасно!) - переставляем ниже
   - Цель: ВСЕГДА быть на 2-м месте (максимум поинтов, минимум риска)
   
4. РЕЗУЛЬТАТ:
   - Независимо от исхода события выходим в 0 или небольшой плюс
   - Максимальный фарминг поинтов БЕЗ РИСКА!

СИСТЕМА ПОИНТОВ (Predict Points - PP):
=====================================
- Лимитные ордера = поинты (чем ближе к рынку = больше поинтов)
- Лучший bid/ask = МАКСИМУМ поинтов!
- Ордера на обеих сторонах (bid + ask) = бонус
- Держать позиции = дополнительные поинты
- Maker Fee = 0% (размещение ордеров БЕСПЛАТНО!)
- Награды распределяются каждые 7 дней

Запуск:
    python predict_trader.py                    # Интерактивный режим
    python predict_trader.py --market 123       # Конкретный рынок
    python predict_trader.py --strategy split   # Стратегия SPLIT
    python predict_trader.py --amount 10        # Размер ордера $10
"""

import os
import sys
import time
import logging
import argparse
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Загружаем .env
load_dotenv()

# Локальные модули
from config import (
    API_KEY, PRIVATE_KEY, PRIVY_WALLET_PRIVATE_KEY, PREDICT_ACCOUNT_ADDRESS,
    CHAIN_ID, ChainId, RPC_URL,
    SPLIT_OFFSET, CHECK_INTERVAL, REPOSITION_COOLDOWN, ASK_POSITION_OFFSET,
    MIN_ORDER_USDT, MAX_ORDERS_PER_MARKET,
    validate_config, print_config,
    # Мульти-аккаунты
    AccountConfig, ACCOUNTS, load_accounts, save_accounts,
    add_account_interactive, remove_account_interactive, list_accounts
)
from predict_api import (
    PredictAPI, Market, Orderbook, Order, Position, Outcome,
    get_complement_price, wei_to_float, float_to_wei,
    calculate_taker_fee
)

# Predict SDK для создания ордеров
try:
    from predict_sdk import (
        OrderBuilder,
        ChainId as SDKChainId,
        Side,
        BuildOrderInput,
        LimitHelperInput,
        OrderBuilderOptions,
        CancelOrdersOptions,
        Order as SDKOrder,
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    print("⚠️  predict-sdk не установлен!")
    print("   Установите: pip install predict-sdk")


# =============================================================================
# ЛОГИРОВАНИЕ
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# =============================================================================
# КАЛЬКУЛЯТОР ПОИНТОВ
# =============================================================================

@dataclass
class PointsEstimator:
    """
    Оценщик заработка Predict Points (PP)
    
    ВАЖНО: Точная формула расчёта поинтов не публикуется,
    это приблизительная оценка на основе документации.
    """
    
    # Статистика сессии
    orders_placed: int = 0
    orders_filled: int = 0
    total_volume_usd: float = 0.0
    time_in_orderbook_seconds: float = 0.0
    session_start: datetime = field(default_factory=datetime.now)
    
    # Баллы за активность (примерные коэффициенты)
    POINTS_PER_ORDER = 1.0           # За каждый размещённый ордер
    POINTS_PER_DOLLAR_VOLUME = 0.1   # За каждый $ объёма
    POINTS_PER_HOUR_IN_BOOK = 5.0    # За каждый час в ордербуке
    POINTS_BONUS_BEST_PRICE = 2.0    # Бонус за лучшую цену
    POINTS_BONUS_BOTH_SIDES = 1.5    # Множитель за bid+ask
    
    def add_order(self, amount_usd: float, is_best_price: bool = False, has_both_sides: bool = False):
        """Добавить размещённый ордер"""
        self.orders_placed += 1
        self.total_volume_usd += amount_usd
    
    def add_time_in_book(self, seconds: float):
        """Добавить время нахождения ордера в книге"""
        self.time_in_orderbook_seconds += seconds
    
    def estimate_points(self) -> float:
        """
        Оценить количество заработанных поинтов
        
        ВАЖНО: Это приблизительная оценка!
        Реальная формула не публикуется Predict.
        """
        points = 0.0
        
        # Поинты за ордера
        points += self.orders_placed * self.POINTS_PER_ORDER
        
        # Поинты за объём
        points += self.total_volume_usd * self.POINTS_PER_DOLLAR_VOLUME
        
        # Поинты за время в книге
        hours = self.time_in_orderbook_seconds / 3600
        points += hours * self.POINTS_PER_HOUR_IN_BOOK
        
        return points
    
    def get_session_stats(self) -> Dict:
        """Получить статистику сессии"""
        session_duration = datetime.now() - self.session_start
        
        return {
            'session_duration': str(session_duration).split('.')[0],
            'orders_placed': self.orders_placed,
            'total_volume_usd': self.total_volume_usd,
            'time_in_book_hours': self.time_in_orderbook_seconds / 3600,
            'estimated_points': self.estimate_points(),
        }
    
    def print_stats(self):
        """Вывести статистику"""
        stats = self.get_session_stats()
        
        print("\n" + "="*60)
        print("📊 СТАТИСТИКА СЕССИИ")
        print("="*60)
        print(f"   Длительность: {stats['session_duration']}")
        print(f"   Размещено ордеров: {stats['orders_placed']}")
        print(f"   Общий объём: ${stats['total_volume_usd']:.2f}")
        print(f"   Время в книге: {stats['time_in_book_hours']:.2f} часов")
        print("-"*60)
        print(f"   💎 ПРИМЕРНО ПОИНТОВ: {stats['estimated_points']:.1f} PP")
        print("="*60)
        print("   ⚠️  Это приблизительная оценка!")
        print("   Точная формула PP не публикуется.")
        print("="*60 + "\n")


# =============================================================================
# СОСТОЯНИЕ РЫНКА (для мультирыночного режима)
# =============================================================================

@dataclass
class MarketState:
    """
    Состояние одного рынка для мультирыночного режима
    """
    market_id: int
    market: Optional[Market] = None
    orderbook: Optional[Orderbook] = None
    
    # Позиции
    yes_position: float = 0.0
    no_position: float = 0.0
    
    # Ордера (храним ID для отмены)
    yes_sell_order_id: Optional[str] = None
    no_sell_order_id: Optional[str] = None
    
    # Фаза SPLIT: 0=не начато, 1=куплены токены, 2=выставлены ордера
    split_phase: int = 0
    
    # Отложенное переставление: время последнего переставления (для cooldown)
    last_reposition_yes: Optional[datetime] = None
    last_reposition_no: Optional[datetime] = None
    
    # Статистика
    last_update: Optional[datetime] = None
    errors_count: int = 0
    
    def __str__(self):
        status = "🟢" if self.split_phase >= 2 else "🟡" if self.split_phase == 1 else "⚪"
        name = self.market.title[:30] + "..." if self.market and len(self.market.title) > 30 else (self.market.title if self.market else "Unknown")
        return f"{status} #{self.market_id}: {name}"


# =============================================================================
# ТРЕЙДЕР
# =============================================================================

class PredictTrader:
    """
    Торговый бот для Predict.fun
    
    Стратегия SPLIT:
    1. Покупает YES и NO токены одновременно
    2. Выставляет SELL ордера на +1 цент от лучшего ASK
    3. Фармит поинты пока ордера стоят в книге
    """
    
    # Флаг для отключения переставления ордеров (если API отмены не работает)
    ENABLE_ORDER_REPOSITIONING = True  # Включено: автопереставление ордеров на 2-е место
    
    def __init__(
        self,
        market_id: int = None,
        market_ids: List[int] = None,  # Список рынков для мультирыночного режима
        strategy: str = 'split',
        order_amount: float = None,  # Обязательно для SPLIT (нет default)
        split_offset: float = SPLIT_OFFSET,
        monitor_mode: bool = False,  # Режим мониторинга (не создавать новые SPLIT)
        account: AccountConfig = None,  # Конфиг аккаунта (для мульти-аккаунт режима)
    ):
        """
        Args:
            market_id: ID рынка (для одиночного режима)
            market_ids: Список ID рынков (для мультирыночного режима)
            strategy: Стратегия торговли ('split')
            order_amount: Размер ордера в USDT
            split_offset: Отступ от лучшего ASK для SPLIT (0.01 = 1 цент)
            monitor_mode: Только мониторинг существующих позиций
            account: Конфигурация аккаунта (если None - используется из .env)
        """
        # Сохраняем конфиг аккаунта
        self.account = account
        
        # Поддержка нескольких рынков
        self.market_ids: List[int] = []
        if market_ids:
            self.market_ids = market_ids
        elif market_id:
            self.market_ids = [market_id]
        
        # Для обратной совместимости
        self.market_id = self.market_ids[0] if self.market_ids else None
        
        self.strategy = strategy
        self.order_amount = max(MIN_ORDER_USDT, order_amount) if order_amount is not None else MIN_ORDER_USDT
        self.split_offset = split_offset
        self.monitor_mode = monitor_mode
        
        # API клиент (один на всех!)
        self.api = PredictAPI(api_key=API_KEY)
        
        # SDK для создания ордеров
        self.order_builder: Optional[OrderBuilder] = None
        self.wallet_address: Optional[str] = None
        
        # =====================================================================
        # МУЛЬТИРЫНОЧНЫЙ РЕЖИМ: состояние каждого рынка
        # =====================================================================
        self.markets: Dict[int, MarketState] = {}
        
        # Для обратной совместимости с одиночным режимом
        self.market: Optional[Market] = None
        self.orderbook: Optional[Orderbook] = None
        
        # Активные ордера (глобальные)
        self.active_orders: Dict[str, Order] = {}
        self.yes_sell_order_id: Optional[str] = None
        self.no_sell_order_id: Optional[str] = None
        
        # Позиции (глобальные, для обратной совместимости)
        self.yes_position: float = 0.0
        self.no_position: float = 0.0
        
        # Калькулятор поинтов
        self.points = PointsEstimator()
        
        # Состояние
        self.running = False
        self.cycles = 0
        self.last_order_time: Optional[datetime] = None
        
        # Фаза SPLIT (для обратной совместимости)
        self.split_phase = 0  # 0 = не начато, 1 = куплены токены, 2 = выставлены ордера
        
        # Лог
        account_name = account.name if account else 'Default'
        logger.info(f"✅ PredictTrader инициализирован [{account_name}]")
        logger.info(f"   Стратегия: {strategy}")
        logger.info(f"   Режим: {'мониторинг' if monitor_mode else 'активный'}")
        logger.info(f"   Рынков: {len(self.market_ids) if self.market_ids else 'авто'}")
        logger.info(f"   Размер ордера: ${self.order_amount:.2f}")
        logger.info(f"   SPLIT offset: {split_offset*100:.1f}%")
    
    # =========================================================================
    # ИНИЦИАЛИЗАЦИЯ
    # =========================================================================
    
    def init_sdk(self) -> bool:
        """
        Инициализировать Predict SDK
        
        Returns:
            True если успешно
        """
        if not SDK_AVAILABLE:
            logger.error("predict-sdk не установлен!")
            return False
        
        try:
            # Определяем chain
            chain_id = SDKChainId.BNB_MAINNET if CHAIN_ID == ChainId.BNB_MAINNET else SDKChainId.BNB_TESTNET
            
            # Получаем ключи из account или из .env (обратная совместимость)
            if self.account:
                private_key = self.account.private_key
                predict_account = self.account.predict_account_address
                logger.info(f"🔐 Используем аккаунт: {self.account.name}")
            else:
                private_key = PRIVY_WALLET_PRIVATE_KEY or PRIVATE_KEY
                predict_account = PREDICT_ACCOUNT_ADDRESS
                logger.info("🔐 Используем аккаунт из .env")
            
            # Проверяем тип кошелька
            if private_key and predict_account:
                # Predict Account (Smart Wallet)
                logger.info("   Тип: Predict Account (Smart Wallet)")
                self.order_builder = OrderBuilder.make(
                    chain_id,
                    private_key,
                    OrderBuilderOptions(predict_account=predict_account)
                )
                self.wallet_address = predict_account
            elif private_key:
                # EOA
                logger.info("   Тип: EOA кошелёк")
                self.order_builder = OrderBuilder.make(chain_id, private_key)
                # Получаем адрес из приватного ключа
                from eth_account import Account
                account = Account.from_key(private_key)
                self.wallet_address = account.address
            else:
                logger.error("Не настроен кошелёк!")
                return False
            
            logger.info(f"   Адрес: {self.wallet_address[:10]}...{self.wallet_address[-8:]}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка инициализации SDK: {e}")
            return False
    
    def authenticate(self) -> bool:
        """
        Аутентифицироваться в API
        
        Returns:
            True если успешно
        """
        try:
            # Получаем сообщение для подписи
            message = self.api.get_auth_message()
            logger.debug(f"Auth message: {message[:50]}...")
            
            # Получаем ключи из account или .env
            if self.account:
                private_key = self.account.private_key
                predict_account = self.account.predict_account_address
            else:
                private_key = PRIVY_WALLET_PRIVATE_KEY or PRIVATE_KEY
                predict_account = PREDICT_ACCOUNT_ADDRESS
            
            # Подписываем
            if predict_account:
                # Для Predict Account используем специальный метод
                signature = self.order_builder.sign_predict_account_message(message)
            else:
                # Для EOA - обычная подпись
                from eth_account import Account
                from eth_account.messages import encode_defunct
                
                account = Account.from_key(private_key)
                signable = encode_defunct(text=message)
                signed = account.sign_message(signable)
                signature = signed.signature.hex()
            
            # Получаем JWT
            jwt = self.api.get_jwt_token(self.wallet_address, message, signature)
            logger.info("✅ Аутентификация успешна")
            
            return True
            
        except Exception as e:
            logger.error(f"Ошибка аутентификации: {e}")
            return False
    
    def set_approvals(self) -> bool:
        """
        Установить разрешения для контрактов
        
        ВАЖНО: Нужно установить разрешения для ОБОИХ типов рынков:
        - is_yield_bearing=True (yield markets)
        - is_yield_bearing=False (non-yield markets)
        
        Это необходимо для split_positions и merge_positions операций.
        
        Returns:
            True если успешно
        """
        try:
            logger.info("🔄 Проверка разрешений...")
            
            # Устанавливаем разрешения для yield-bearing рынков
            result1 = self.order_builder.set_approvals(is_yield_bearing=True)
            
            # Устанавливаем разрешения для non-yield-bearing рынков
            result2 = self.order_builder.set_approvals(is_yield_bearing=False)
            
            all_success = result1.success and result2.success
            
            if all_success:
                logger.info("✅ Все разрешения установлены")
                return True
            else:
                logger.warning("⚠️  Некоторые разрешения не установлены")
                for result in [result1, result2]:
                    for tx in result.transactions:
                        if not tx.success:
                            logger.warning(f"   Failed: {tx.cause}")
                return True  # Продолжаем, возможно уже установлены
                
        except Exception as e:
            logger.error(f"Ошибка установки разрешений: {e}")
            return False
    
    def set_usdt_allowance_for_conditional_tokens(self, is_yield_bearing: bool, is_neg_risk: bool) -> bool:
        """
        Установить allowance USDT для conditional tokens контракта.
        
        ВАЖНО: SDK set_approvals НЕ устанавливает allowance для SPLIT операций!
        Нужно вручную одобрить USDT для conditional tokens контракта.
        
        Args:
            is_yield_bearing: Yield-bearing рынок
            is_neg_risk: NegRisk рынок
            
        Returns:
            True если успешно
        """
        try:
            contracts = self.order_builder.contracts
            usdt_contract = contracts.usdt
            
            # Выбираем правильный conditional tokens контракт
            if is_yield_bearing:
                if is_neg_risk:
                    ctf_address = contracts.yield_bearing_neg_risk_conditional_tokens.address
                else:
                    ctf_address = contracts.yield_bearing_conditional_tokens.address
            else:
                if is_neg_risk:
                    ctf_address = contracts.neg_risk_conditional_tokens.address
                else:
                    ctf_address = contracts.conditional_tokens.address
            
            # Проверяем текущий allowance
            current_allowance = usdt_contract.functions.allowance(
                self.wallet_address, ctf_address
            ).call()
            
            logger.debug(f"🔧 USDT allowance для CTF ({ctf_address[:10]}...): {wei_to_float(current_allowance):.2f}")
            
            # Если allowance достаточный (> 1M USDT), пропускаем
            if current_allowance > 10**24:  # > 1M USDT
                logger.debug(f"   ✅ Allowance уже установлен")
                return True
            
            # Устанавливаем максимальный allowance
            logger.info(f"🔄 Устанавливаем USDT allowance для conditional tokens...")
            
            max_allowance = 2**256 - 1  # MAX_UINT256
            
            # Если используем Predict Account, нужно вызвать через kernel
            if PREDICT_ACCOUNT_ADDRESS:
                # Используем SDK для отправки транзакции через Predict Account
                from predict_sdk import KERNEL_ABI
                from predict_sdk.order_builder import make_contract
                
                web3 = self.order_builder._web3
                
                # Кодируем вызов approve
                encoded_approve = usdt_contract.encode_abi(
                    abi_element_identifier="approve",
                    args=[ctf_address, max_allowance]
                )
                
                # Кодируем execution calldata для Kernel
                calldata = self.order_builder._encode_execution_calldata(
                    usdt_contract.address, encoded_approve, value=0
                )
                
                # Получаем kernel контракт
                kernel_contract = make_contract(web3, self.wallet_address, KERNEL_ABI)
                
                # Отправляем транзакцию
                result = self.order_builder._run_async(
                    self.order_builder._handle_transaction_async(
                        kernel_contract, "execute", 
                        self.order_builder._execution_mode, calldata
                    )
                )
                
                if result.success:
                    logger.info(f"   ✅ USDT allowance установлен")
                    return True
                else:
                    logger.error(f"   ❌ Ошибка: {result.cause}")
                    return False
            else:
                # EOA - прямой вызов approve
                from eth_account import Account
                account = Account.from_key(PRIVATE_KEY)
                
                web3 = self.order_builder._web3
                
                # Собираем транзакцию
                tx = usdt_contract.functions.approve(
                    ctf_address, max_allowance
                ).build_transaction({
                    'from': self.wallet_address,
                    'nonce': web3.eth.get_transaction_count(self.wallet_address),
                    'gas': 100000,
                    'gasPrice': web3.eth.gas_price,
                })
                
                # Подписываем и отправляем
                signed_tx = account.sign_transaction(tx)
                tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
                receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
                
                if receipt.status == 1:
                    logger.info(f"   ✅ USDT allowance установлен")
                    return True
                else:
                    logger.error(f"   ❌ Транзакция неуспешна")
                    return False
                    
        except Exception as e:
            logger.error(f"Ошибка установки USDT allowance: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def load_market(self, market_id: int) -> bool:
        """
        Загрузить информацию о рынке
        
        Args:
            market_id: ID рынка
            
        Returns:
            True если успешно
        """
        try:
            self.market = self.api.get_market_by_id(market_id)
            self.market_id = market_id
            
            if not self.market.is_binary:
                logger.error(f"Рынок #{market_id} не бинарный!")
                return False
            
            if self.market.status == 'RESOLVED':
                logger.error(f"Рынок #{market_id} уже завершён!")
                return False
            
            logger.info(f"✅ Загружен рынок #{market_id}")
            logger.info(f"   Название: {self.market.title[:50]}...")
            logger.info(f"   Статус: {self.market.status}")
            logger.info(f"   isNegRisk: {self.market.is_neg_risk}")
            logger.info(f"   isYieldBearing: {self.market.is_yield_bearing}")
            logger.info(f"   Комиссия: {self.market.fee_rate_bps} bps")
            logger.info(f"   Порог спреда для PP: {self.market.spread_threshold}")
            logger.info(f"   Мин. акций для PP: {self.market.share_threshold}")
            
            return True
            
        except Exception as e:
            logger.error(f"Ошибка загрузки рынка: {e}")
            return False
    
    def update_orderbook(self) -> bool:
        """
        Обновить ордербук
        
        Returns:
            True если успешно
        """
        try:
            self.orderbook = self.api.get_orderbook(self.market_id)
            return True
        except Exception as e:
            logger.error(f"Ошибка обновления ордербука: {e}")
            return False
    
    # =========================================================================
    # БАЛАНС И ПОЗИЦИИ
    # =========================================================================
    
    def get_usdt_balance(self) -> float:
        """Получить баланс USDT"""
        try:
            balance_wei = self.order_builder.balance_of("USDT")
            return wei_to_float(str(balance_wei))
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            return 0.0
    
    def update_positions(self):
        """
        Обновить позиции.
        
        ПРИОРИТЕТ: REST API (учитывает токены в ордерах), 
        fallback на blockchain (может показать 0 если токены залочены биржей).
        """
        # Сначала пробуем REST API — он учитывает ВСЕ токены включая залоченные в ордерах
        if self._update_positions_via_api():
            return
        
        # Fallback на blockchain
        self._update_positions_via_blockchain()
    
    def _update_positions_via_blockchain(self):
        """Обновить позиции напрямую из блокчейна через контракты.
        
        ВНИМАНИЕ: blockchain balanceOf() может возвращать 0 если токены 
        залочены биржей для SELL ордеров! Используйте как fallback.
        """
        try:
            if not self.market:
                logger.warning("Рынок не загружен, невозможно обновить позиции")
                return
            
            # Сбрасываем позиции перед загрузкой
            self.yes_position = 0.0
            self.no_position = 0.0
            
            # Получаем token IDs из рынка
            yes_token_id = int(self.market.yes_outcome.on_chain_id)
            no_token_id = int(self.market.no_outcome.on_chain_id)
            
            # Выбираем правильный контракт в зависимости от типа рынка
            contracts = self.order_builder.contracts
            
            # Определяем адрес для проверки баланса
            wallet_address = self.wallet_address
            
            # Для yield_bearing рынков используем yield_bearing_conditional_tokens
            # Для обычных рынков используем conditional_tokens
            if self.market.is_yield_bearing:
                if self.market.is_neg_risk:
                    ctf_contract = contracts.yield_bearing_neg_risk_conditional_tokens
                else:
                    ctf_contract = contracts.yield_bearing_conditional_tokens
            else:
                if self.market.is_neg_risk:
                    ctf_contract = contracts.neg_risk_conditional_tokens
                else:
                    ctf_contract = contracts.conditional_tokens
            
            # Получаем балансы напрямую через контракт
            yes_balance_wei = ctf_contract.functions.balanceOf(wallet_address, yes_token_id).call()
            no_balance_wei = ctf_contract.functions.balanceOf(wallet_address, no_token_id).call()
            
            self.yes_position = wei_to_float(yes_balance_wei)
            self.no_position = wei_to_float(no_balance_wei)
            
            logger.debug(f"Позиции (blockchain) market #{self.market_id}: YES={self.yes_position:.4f}, NO={self.no_position:.4f}")
            
        except Exception as e:
            logger.error(f"Ошибка обновления позиций через blockchain: {e}")
    
    def _update_positions_via_api(self) -> bool:
        """
        Обновить позиции через REST API.
        
        REST API учитывает ВСЕ токены — и свободные, и залоченные в ордерах.
        Это правильный источник данных для определения размера позиции.
        
        Returns:
            True если успешно, False если ошибка
        """
        try:
            positions = self.api.get_all_positions()
            
            self.yes_position = 0.0
            self.no_position = 0.0
            
            for pos in positions:
                if pos.market.id == self.market_id:
                    amount = wei_to_float(pos.amount)
                    if pos.outcome.is_yes:
                        self.yes_position = amount
                    elif pos.outcome.is_no:
                        self.no_position = amount
            
            logger.debug(f"Позиции (API) market #{self.market_id}: YES={self.yes_position:.4f}, NO={self.no_position:.4f}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка обновления позиций через API: {e}")
            return False
    
    # =========================================================================
    # СОЗДАНИЕ ОРДЕРОВ
    # =========================================================================
    
    def _convert_order_to_api_format(self, signed_order, order_hash: str) -> Dict:
        """
        Конвертирует SignedOrder из SDK в формат API
        
        SDK использует snake_case и enum объекты, а API ожидает camelCase и числа.
        """
        order_dict = signed_order.__dict__
        
        # Конвертируем в camelCase и правильные типы
        api_order = {
            'salt': order_dict['salt'],
            'maker': order_dict['maker'],
            'signer': order_dict['signer'],
            'taker': order_dict['taker'],
            'tokenId': order_dict['token_id'],
            'makerAmount': order_dict['maker_amount'],
            'takerAmount': order_dict['taker_amount'],
            'expiration': order_dict['expiration'],
            'nonce': order_dict['nonce'],
            'feeRateBps': order_dict['fee_rate_bps'],
            'side': order_dict['side'].value if hasattr(order_dict['side'], 'value') else int(order_dict['side']),
            'signatureType': order_dict['signature_type'].value if hasattr(order_dict['signature_type'], 'value') else int(order_dict['signature_type']),
            'signature': order_dict['signature'],
            'hash': order_hash,
        }
        
        return api_order
    
    def calculate_sell_prices(self) -> Tuple[Optional[float], Optional[float]]:
        """
        Вычислить цены для SELL ордеров (2-е место в стакане)
        
        Цена SELL должна быть выше best BID, иначе ордер исполнится мгновенно!
        
        Returns:
            Tuple (yes_sell_price, no_sell_price) или (None, None) при ошибке
        """
        if not self.orderbook or not self.market:
            return None, None
        
        precision = self.market.decimal_precision
        min_tick = 10 ** (-precision)  # 0.001 для precision=3
        
        # Получаем текущие лучшие ASK и BID цены
        yes_ask = self.orderbook.get_best_yes_ask()
        yes_bid = self.orderbook.best_bid  # best YES bid
        no_bid, no_ask = self.orderbook.get_no_prices(precision)
        
        if yes_ask is None or no_ask is None:
            return None, None
        
        # Ставим на минимальный шаг выше лучшего ASK (2-е место)
        yes_sell_price = round(yes_ask + min_tick, precision)
        no_sell_price = round(no_ask + min_tick, precision)
        
        # ВАЖНО: Цена SELL должна быть СТРОГО ВЫШЕ best BID!
        # Иначе ордер исполнится мгновенно (или частично)
        if yes_bid and yes_sell_price <= yes_bid:
            yes_sell_price = round(yes_bid + min_tick, precision)
            logger.info(f"   📌 YES sell price поднята выше bid: {yes_sell_price:.3f} (bid={yes_bid:.3f})")
            
        if no_bid and no_sell_price <= no_bid:
            no_sell_price = round(no_bid + min_tick, precision)
            logger.info(f"   📌 NO sell price поднята выше bid: {no_sell_price:.3f} (bid={no_bid:.3f})")
        
        # Проверяем, что цены валидны (< 0.999)
        max_price = 1.0 - min_tick
        yes_sell_price = min(yes_sell_price, max_price)
        no_sell_price = min(no_sell_price, max_price)
        
        return yes_sell_price, no_sell_price
    
    def create_limit_order(
        self,
        side: str,  # 'BUY' или 'SELL'
        token: str,  # 'YES' или 'NO'
        price: float,
        quantity: float,
        is_retry: bool = False,  # Флаг для предотвращения бесконечной рекурсии
        skip_guard: bool = False,  # Пропустить проверку безопасности (для закрытия позиций)
        skip_balance_check: bool = False,  # Пропустить проверку баланса blockchain (при репозиции)
    ) -> Optional[str]:
        """
        Создать лимитный ордер
        
        Args:
            side: 'BUY' или 'SELL'
            token: 'YES' или 'NO'
            price: Цена за акцию (0.01 - 0.99)
            quantity: Количество акций
            
        Returns:
            Order hash или None при ошибке
        """
        try:
            # Определяем outcome
            if token.upper() == 'YES':
                outcome = self.market.yes_outcome
            else:
                outcome = self.market.no_outcome
            
            if not outcome:
                logger.error(f"Не найден outcome для {token}")
                return None
            
            # Конвертируем side
            sdk_side = Side.BUY if side.upper() == 'BUY' else Side.SELL
            
            # Округляем цену до precision рынка (например, 3 знака = 0.001 шаг)
            # BUY - округляем ВВЕРХ (ceiling) чтобы гарантированно купить
            # SELL - округляем ВНИЗ (floor) чтобы гарантированно продать
            import math
            precision = self.market.decimal_precision  # Обычно 3 знака
            multiplier = 10 ** precision  # 1000 для precision=3
            if side.upper() == 'BUY':
                price = math.ceil(price * multiplier) / multiplier  # 0.2361 -> 0.237
            else:
                price = math.floor(price * multiplier) / multiplier  # 0.2369 -> 0.236
            
            # API требует минимум $0.9 value и makerAmount % 10^13 == 0
            # Минимум shares = ceil(0.9 / price)
            MIN_VALUE_USD = 0.9
            min_shares = int(MIN_VALUE_USD / price) + 1
            
            PRECISION = 10**13
            quantity_rounded = round(quantity, 5)
            
            # Для SELL ордеров: НЕ увеличиваем quantity до min_shares,
            # потому что мы ограничены количеством токенов
            # Для BUY: можно увеличить (у нас есть USDT)
            if quantity_rounded < min_shares:
                if side.upper() == 'SELL':
                    # Для SELL - отклоняем ордер если недостаточно токенов
                    logger.warning(f"⚠️  Количество {quantity_rounded:.4f} меньше минимума {min_shares} для цены {price:.2f}")
                    logger.warning(f"   Минимальный SELL ордер = {min_shares} шар × {price:.2f} = ${min_shares * price:.2f}")
                    return None
                else:
                    # Для BUY - увеличиваем до минимума
                    quantity_rounded = float(min_shares)
                    logger.debug(f"Увеличено quantity до {quantity_rounded} (min value ${MIN_VALUE_USD})")
            
            # Рассчитываем amounts через SDK
            # Используем float_to_wei для точной конвертации (исправление precision issue v0.0.12)
            price_wei = float_to_wei(price)
            quantity_wei = float_to_wei(quantity_rounded)
            
            amounts = self.order_builder.get_limit_order_amounts(
                LimitHelperInput(
                    side=sdk_side,
                    price_per_share_wei=price_wei,
                    quantity_wei=quantity_wei,
                )
            )
            
            # Проверяем, что amounts кратны PRECISION
            # Если нет - для SELL используем floor от quantity (чтобы не продать больше чем есть)
            # Для BUY - ceiling до min_shares
            if amounts.maker_amount % PRECISION != 0 or amounts.taker_amount % PRECISION != 0:
                if side.upper() == 'SELL':
                    # Для SELL: используем floor, но не меньше min_shares
                    quantity_adjusted = max(min_shares, math.floor(quantity_rounded))
                else:
                    # Для BUY: ceiling до min_shares
                    quantity_adjusted = max(min_shares, math.ceil(quantity_rounded))
                
                quantity_wei = float_to_wei(quantity_adjusted)
                
                amounts = self.order_builder.get_limit_order_amounts(
                    LimitHelperInput(
                        side=sdk_side,
                        price_per_share_wei=price_wei,
                        quantity_wei=quantity_wei,
                    )
                )
                quantity_rounded = float(quantity_adjusted)
                logger.info(f"   🔧 Adjusted quantity: {quantity_adjusted:.4f} (was {quantity:.4f})")
            
            # Используем amounts напрямую от SDK
            maker_amount = amounts.maker_amount
            taker_amount = amounts.taker_amount
            price_per_share = amounts.price_per_share
            
            # DEBUG: логируем amounts
            logger.info(f"   🔧 DEBUG: side={side}, quantity={quantity_rounded:.4f}, makerAmount={maker_amount}, takerAmount={taker_amount}")
            logger.info(f"   🔧 DEBUG: token={token}, token_id={outcome.on_chain_id[:20]}...")
            
            # Проверяем реальный баланс через правильный контракт перед SELL
            # ПРОПУСКАЕМ при skip_balance_check (репозиция) — blockchain может 
            # показать 0 пока токены возвращаются из эскроу биржи после отмены ордера
            if side.upper() == 'SELL' and not skip_balance_check:
                try:
                    # Выбираем правильный контракт для проверки баланса
                    contracts = self.order_builder.contracts
                    token_id_int = int(outcome.on_chain_id)
                    
                    if self.market.is_yield_bearing:
                        if self.market.is_neg_risk:
                            ctf_contract = contracts.yield_bearing_neg_risk_conditional_tokens
                        else:
                            ctf_contract = contracts.yield_bearing_conditional_tokens
                    else:
                        if self.market.is_neg_risk:
                            ctf_contract = contracts.neg_risk_conditional_tokens
                        else:
                            ctf_contract = contracts.conditional_tokens
                    
                    real_balance_wei = ctf_contract.functions.balanceOf(self.wallet_address, token_id_int).call()
                    real_balance_float = real_balance_wei / 10**18 if real_balance_wei else 0
                    logger.info(f"   🔧 DEBUG: blockchain balance({token}) = {real_balance_float:.4f}")
                    
                    # Если баланс 0, выходим с понятной ошибкой
                    if real_balance_float < quantity_rounded:
                        logger.warning(f"   ⚠️ Недостаточно токенов! Нужно {quantity_rounded:.4f}, есть {real_balance_float:.4f}")
                        return None
                        
                except Exception as e:
                    logger.warning(f"   ⚠️ Не удалось получить баланс: {e}")
            
            if maker_amount == 0 or taker_amount == 0:
                logger.error(f"Сумма слишком мала")
                return None
            
            # Строим ордер с amounts от SDK
            order = self.order_builder.build_order(
                "LIMIT",
                BuildOrderInput(
                    side=sdk_side,
                    token_id=outcome.on_chain_id,
                    maker_amount=str(maker_amount),
                    taker_amount=str(taker_amount),
                    fee_rate_bps=self.market.fee_rate_bps,
                )
            )
            
            # Подписываем
            typed_data = self.order_builder.build_typed_data(
                order,
                is_neg_risk=self.market.is_neg_risk,
                is_yield_bearing=self.market.is_yield_bearing,
            )
            
            signed_order = self.order_builder.sign_typed_data_order(typed_data)
            order_hash = self.order_builder.build_typed_data_hash(typed_data)
            
            # Конвертируем в формат API (camelCase, числа вместо enum)
            api_order = self._convert_order_to_api_format(signed_order, order_hash)
            
            # DEBUG: логируем maker адрес
            logger.info(f"   🔧 DEBUG: maker={api_order.get('maker', 'N/A')}")
            
            # ====================================================================
            # 🛡️ ФИНАЛЬНАЯ ПРОВЕРКА: для SELL ордеров перечитываем ордербук
            # и убеждаемся что наша цена НЕ пересекает спред
            # (Пропускается при закрытии позиций — там мы ХОТИМ продать в bid)
            # ====================================================================
            if side.upper() == 'SELL' and not skip_guard:
                try:
                    if self.update_orderbook():
                        min_tick_guard = 10 ** (-self.market.decimal_precision)
                        if token.upper() == 'YES':
                            best_bid_guard = self.orderbook.best_bid
                        else:
                            no_bid_guard, _ = self.orderbook.get_no_prices(self.market.decimal_precision)
                            best_bid_guard = no_bid_guard
                        
                        if best_bid_guard is not None and price <= best_bid_guard:
                            logger.error(f"   🛡️ БЛОКИРОВКА! SELL {token} @ {price:.4f} <= BID {best_bid_guard:.4f}")
                            logger.error(f"   🛡️ Ордер НЕ отправлен — пересёк бы спред и мгновенно исполнился!")
                            return None
                        
                        if best_bid_guard is not None and price <= best_bid_guard + min_tick_guard:
                            logger.warning(f"   🛡️ ПРЕДУПРЕЖДЕНИЕ: SELL {token} @ {price:.4f} "
                                          f"очень близко к BID {best_bid_guard:.4f} (разница < 1 tick)")
                except Exception as guard_err:
                    logger.warning(f"   ⚠️ Guard check error: {guard_err}")
            
            # Формируем body запроса
            # Используем пересчитанный pricePerShare для округлённых amounts
            order_data = {
                'order': api_order,
                'pricePerShare': str(price_per_share),
                'strategy': 'LIMIT',
            }
            
            # DEBUG: логируем что отправляем
            logger.debug(f"Order data: {order_data}")
            
            result = self.api.create_order(order_data)
            
            logger.info(f"✅ Ордер создан: {side} {token} @ {price:.4f} x {quantity_rounded:.5f}")
            logger.info(f"   Hash: {order_hash[:20]}...")
            
            # Обновляем статистику поинтов
            self.points.add_order(price * quantity_rounded)
            
            return order_hash
            
        except Exception as e:
            error_str = str(e)
            
            # Проверяем, есть ли информация о доступном количестве (только если retry ещё не было)
            if 'Insufficient shares' in error_str and 'amountAvailable' in error_str and not is_retry:
                # Парсим amountAvailable из ошибки
                import re
                import json
                
                # Ищем JSON в ошибке
                json_match = re.search(r'\{.*\}', error_str)
                if json_match:
                    try:
                        error_data = json.loads(json_match.group())
                        error_info = error_data.get('error', {})
                        amount_available = int(error_info.get('amountAvailable', '0'))
                        
                        if amount_available > 0:
                            available_float = amount_available / 10**18
                            logger.warning(f"⚠️  Доступно только {available_float:.4f}, запрашивали {quantity:.4f}")
                            
                            # ЗАЩИТА: не создаём мизерные ордера!
                            # Если доступно < 20% от запрошенного — это аномалия, НЕ делаем retry
                            # (иначе создадим ордер на 1 share вместо 82)
                            ratio = available_float / quantity if quantity > 0 else 0
                            if ratio < 0.20:
                                logger.error(f"🛑 Доступно {available_float:.4f} = {ratio*100:.1f}% от {quantity:.4f} — слишком мало, retry ОТМЕНЁН")
                                logger.error(f"   Возможна ошибка синхронизации. Проверьте позиции вручную!")
                                return None
                            
                            # Один retry с реальным доступным количеством
                            new_quantity = available_float * 0.90  # 90% от доступного
                            if new_quantity >= 0.5 and new_quantity < quantity:
                                logger.info(f"🔄 Retry с {new_quantity:.4f} ({ratio*100:.0f}% от исходного)...")
                                # Рекурсивный вызов с флагом is_retry=True
                                return self.create_limit_order(
                                    token=token,
                                    side=side,
                                    price=price,
                                    quantity=new_quantity,
                                    is_retry=True,
                                    skip_guard=skip_guard,
                                )
                    except (json.JSONDecodeError, KeyError) as parse_error:
                        logger.debug(f"Не удалось распарсить ошибку: {parse_error}")
            
            logger.error(f"Ошибка создания ордера: {e}")
            return None
    
    def cancel_order(self, order_id: str) -> bool:
        """
        Отменить ордер через API
        
        Args:
            order_id: ID ордера (не хеш!)
        """
        try:
            result = self.api.remove_orders([order_id])
            logger.info(f"✅ Ордер отменён: {order_id}")
            return True
        except Exception as e:
            logger.warning(f"API отмена не удалась: {e}")
            # Ордер мог уже исполниться или быть отменён
            return False
    
    def cancel_all_orders_sdk(self, orders_to_cancel: list) -> int:
        """
        Отменить ордера через SDK (on-chain)
        
        Args:
            orders_to_cancel: Список SDKOrder объектов
            
        Returns:
            Количество отменённых ордеров
        """
        if not orders_to_cancel:
            return 0
            
        try:
            result = self.order_builder.cancel_orders(
                orders=orders_to_cancel,
                options=CancelOrdersOptions(
                    is_neg_risk=self.market.is_neg_risk,
                    is_yield_bearing=self.market.is_yield_bearing,
                ),
            )
            
            if result.success:
                logger.info(f"✅ Отменено {len(orders_to_cancel)} ордеров через SDK")
                return len(orders_to_cancel)
            else:
                logger.error(f"Ошибка отмены через SDK: {result.cause if hasattr(result, 'cause') else 'Unknown'}")
                return 0
                
        except Exception as e:
            logger.error(f"Ошибка SDK cancel_orders: {e}")
            return 0
    
    def cancel_all_orders(self) -> int:
        """
        Отменить все ордера на рынке
        
        Пробует сначала через API, затем через SDK если не получилось.
        """
        try:
            orders = self.api.get_open_orders()
            
            # Фильтруем по рынку
            market_orders = [o for o in orders if o.market_id == self.market_id]
            
            if not market_orders:
                return 0
            
            # Используем id для отмены, не hash!
            order_ids = [o.id for o in market_orders]
            
            # Пробуем через API
            try:
                self.api.remove_orders(order_ids)
                logger.info(f"✅ Отменено {len(order_ids)} ордеров через API")
                return len(order_ids)
            except Exception as api_error:
                logger.warning(f"API отмена не удалась: {api_error}")
                # Ордера могли уже исполниться
                return 0
            
        except Exception as e:
            logger.error(f"Ошибка отмены ордеров: {e}")
            return 0
    
    def close_position(self, market_id: int = None) -> Dict:
        """
        Закрыть позицию на рынке: отменить ордера и продать токены
        
        Args:
            market_id: ID рынка (если None, использует текущий self.market_id)
            
        Returns:
            Dict с результатами: {
                'success': bool,
                'cancelled_orders': int,
                'sold_yes': float,
                'sold_no': float,
                'usdt_received': float,
                'message': str
            }
        """
        result = {
            'success': False,
            'cancelled_orders': 0,
            'sold_yes': 0.0,
            'sold_no': 0.0,
            'usdt_received': 0.0,
            'message': ''
        }
        
        target_market_id = market_id or self.market_id
        
        if not target_market_id:
            result['message'] = 'Не указан ID рынка'
            return result
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🔴 ЗАКРЫТИЕ ПОЗИЦИИ НА РЫНКЕ #{target_market_id}")
        logger.info(f"{'='*60}")
        
        try:
            # Если это другой рынок - загружаем его
            if market_id and market_id != self.market_id:
                if not self.load_market(market_id):
                    result['message'] = f'Не удалось загрузить рынок #{market_id}'
                    return result
            
            # 1. Отменяем все ордера на рынке
            logger.info("📋 Шаг 1: Отмена всех ордеров...")
            cancelled = self.cancel_all_orders()
            result['cancelled_orders'] = cancelled
            logger.info(f"   Отменено ордеров: {cancelled}")
            
            time.sleep(0.5)  # Пауза для синхронизации
            
            # 2. Обновляем позиции
            logger.info("📋 Шаг 2: Обновление позиций...")
            self.update_positions()
            
            yes_balance = self.yes_position
            no_balance = self.no_position
            logger.info(f"   YES: {yes_balance:.4f} | NO: {no_balance:.4f}")
            
            # 3. Продаём токены (market sell)
            total_usdt = 0.0
            
            # Минимальная стоимость ордера на Predict.fun
            MIN_ORDER_USD = 0.95
            
            # Продаём YES токены
            if yes_balance >= 0.5:
                logger.info(f"📋 Шаг 3a: Продажа {yes_balance:.2f} YES токенов...")
                
                # Получаем лучший bid для YES
                if not self.update_orderbook():
                    logger.warning("   Не удалось обновить ордербук")
                
                best_bid = self.orderbook.best_bid if self.orderbook else None
                
                if best_bid and best_bid > 0.01:
                    # Продаём по market price (чуть ниже best bid для гарантии исполнения)
                    sell_price = best_bid - 0.001
                    sell_price = max(0.01, sell_price)
                    
                    # Проверяем минимальную стоимость ордера
                    order_value = yes_balance * sell_price
                    if order_value < MIN_ORDER_USD:
                        min_tokens_needed = int(MIN_ORDER_USD / sell_price) + 1
                        logger.warning(f"   ⚠️ Позиция слишком маленькая для продажи!")
                        logger.warning(f"   Стоимость: ${order_value:.2f} < минимум ${MIN_ORDER_USD}")
                        logger.warning(f"   Нужно минимум {min_tokens_needed} токенов @ {sell_price:.3f}")
                        result['message'] += f"\nYES: ${order_value:.2f} < мин. ${MIN_ORDER_USD} (нужно {min_tokens_needed} шт.)"
                    else:
                        # Создаём SELL ордер (skip_guard: закрытие позиции — мы хотим продать)
                        order_hash = self.create_limit_order(
                            side='SELL',
                            token='YES',
                            price=sell_price,
                            quantity=yes_balance,
                            skip_guard=True,
                        )
                        
                        if order_hash:
                            result['sold_yes'] = yes_balance
                            total_usdt += yes_balance * sell_price
                            logger.info(f"   ✅ Продано YES @ {sell_price:.3f} = ${yes_balance * sell_price:.2f}")
                        else:
                            logger.warning(f"   ⚠️ Не удалось продать YES")
                else:
                    logger.warning(f"   ⚠️ Нет bid для YES, пропускаем")
            
            time.sleep(0.3)
            
            # Продаём NO токены
            if no_balance >= 0.5:
                logger.info(f"📋 Шаг 3b: Продажа {no_balance:.2f} NO токенов...")
                
                # Для NO нужно вычислить цену из YES orderbook
                # NO bid = 1 - YES ask
                if not self.update_orderbook():
                    logger.warning("   Не удалось обновить ордербук")
                
                precision = self.market.decimal_precision if self.market else 3
                best_no_bid = self.orderbook.get_best_no_bid(precision) if self.orderbook else None
                
                if best_no_bid and best_no_bid > 0.01:
                    # Продаём по market price
                    sell_price = best_no_bid - 0.001
                    sell_price = max(0.01, sell_price)
                    
                    # Проверяем минимальную стоимость ордера
                    order_value = no_balance * sell_price
                    if order_value < MIN_ORDER_USD:
                        min_tokens_needed = int(MIN_ORDER_USD / sell_price) + 1
                        logger.warning(f"   ⚠️ Позиция слишком маленькая для продажи!")
                        logger.warning(f"   Стоимость: ${order_value:.2f} < минимум ${MIN_ORDER_USD}")
                        logger.warning(f"   Нужно минимум {min_tokens_needed} токенов @ {sell_price:.3f}")
                        result['message'] += f"\nNO: ${order_value:.2f} < мин. ${MIN_ORDER_USD} (нужно {min_tokens_needed} шт.)"
                    else:
                        order_hash = self.create_limit_order(
                            side='SELL',
                            token='NO',
                            price=sell_price,
                            quantity=no_balance,
                            skip_guard=True,
                        )
                        
                        if order_hash:
                            result['sold_no'] = no_balance
                            total_usdt += no_balance * sell_price
                            logger.info(f"   ✅ Продано NO @ {sell_price:.3f} = ${no_balance * sell_price:.2f}")
                        else:
                            logger.warning(f"   ⚠️ Не удалось продать NO")
                else:
                    logger.warning(f"   ⚠️ Нет bid для NO, пропускаем")
            
            result['usdt_received'] = total_usdt
            result['success'] = True
            result['message'] = f'Позиция закрыта. Отменено {cancelled} ордеров, продано YES={result["sold_yes"]:.1f}, NO={result["sold_no"]:.1f}'
            
            logger.info(f"\n✅ ПОЗИЦИЯ ЗАКРЫТА!")
            logger.info(f"   Отменено ордеров: {cancelled}")
            logger.info(f"   Продано YES: {result['sold_yes']:.2f}")
            logger.info(f"   Продано NO: {result['sold_no']:.2f}")
            logger.info(f"   Получено ~${total_usdt:.2f} USDT")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Ошибка закрытия позиции: {e}")
            result['message'] = f'Ошибка: {e}'
            return result
    
    # =========================================================================
    # SPLIT POSITIONS (ПОКУПКА YES + NO)
    # =========================================================================
    
    def split_to_positions(self, amount_usdt: float) -> bool:
        """
        Конвертировать USDT в YES + NO токены (SPLIT)
        
        Это основная операция ФАЗЫ 1 - одна транзакция создаёт
        равное количество YES и NO токенов из USDT.
        
        Args:
            amount_usdt: Сумма в USDT для конвертации
            
        Returns:
            True если успешно
        """
        try:
            if not self.market:
                logger.error("Рынок не загружен!")
                return False
            
            # Проверяем баланс
            balance = self.get_usdt_balance()
            if balance < amount_usdt:
                logger.error(f"Недостаточно USDT: ${balance:.2f} < ${amount_usdt:.2f}")
                return False
            
            # ВАЖНО: Проверяем/устанавливаем USDT allowance для conditional tokens
            # SDK set_approvals НЕ устанавливает это! Нужно делать вручную.
            if not self.set_usdt_allowance_for_conditional_tokens(
                is_yield_bearing=self.market.is_yield_bearing,
                is_neg_risk=self.market.is_neg_risk
            ):
                logger.error("Не удалось установить USDT allowance")
                return False
            
            # Используем float_to_wei для точной конвертации (исправление precision issue v0.0.12)
            amount_wei = float_to_wei(amount_usdt)
            
            logger.info(f"🔄 SPLIT: ${amount_usdt:.2f} USDT → YES + NO токены...")
            
            result = self.order_builder.split_positions(
                condition_id=self.market.condition_id,
                amount=amount_wei,
                is_neg_risk=self.market.is_neg_risk,
                is_yield_bearing=self.market.is_yield_bearing,
            )
            
            # DEBUG: смотрим что вернулось
            logger.info(f"🔧 SPLIT result type: {type(result)}")
            logger.info(f"🔧 SPLIT result: {result}")
            if hasattr(result, '__dict__'):
                logger.info(f"🔧 SPLIT result.__dict__: {result.__dict__}")
            
            # Проверяем результат
            if hasattr(result, 'success') and result.success:
                tx_hash = 'N/A'
                if hasattr(result, 'receipt') and hasattr(result.receipt, 'transactionHash'):
                    tx_hash = result.receipt.transactionHash.hex()[:20] + '...'
                logger.info(f"✅ SPLIT выполнен!")
                logger.info(f"   TX: {tx_hash}")
                logger.info(f"   Получено: ~{amount_usdt:.2f} YES + ~{amount_usdt:.2f} NO")
                
                # Обновляем позиции
                self.update_positions()
                return True
            elif hasattr(result, 'receipt'):
                # TransactionSuccess без явного success
                tx_hash = 'N/A'
                if hasattr(result.receipt, 'transactionHash'):
                    tx_hash = result.receipt.transactionHash.hex() if hasattr(result.receipt.transactionHash, 'hex') else str(result.receipt.transactionHash)
                    tx_hash = tx_hash[:20] + '...'
                logger.info(f"✅ SPLIT выполнен!")
                logger.info(f"   TX: {tx_hash}")
                self.update_positions()
                return True
            else:
                cause = result.cause if hasattr(result, 'cause') else str(result)
                logger.error(f"Ошибка SPLIT: {cause}")
                return False
                
        except Exception as e:
            logger.error(f"Ошибка split_positions: {e}")
            return False
    
    # =========================================================================
    # MERGE POSITIONS (СЛИЯНИЕ ПОЗИЦИЙ)
    # =========================================================================
    
    def merge_positions(self, amount: float = None) -> bool:
        """
        Слить YES + NO токены обратно в USDT
        
        Это ключевая функция для безрискового выхода из SPLIT!
        При равном количестве YES и NO получаем обратно полную сумму в USDT.
        
        Args:
            amount: Количество для слияния (в токенах). Если None - всё доступное
            
        Returns:
            True если успешно
        """
        try:
            if not self.market:
                logger.error("Рынок не загружен!")
                return False
            
            # Обновляем позиции
            self.update_positions()
            
            # Определяем количество для merge
            # Можем слить только минимум из YES и NO
            max_merge = min(self.yes_position, self.no_position)
            
            if max_merge < 0.01:
                logger.warning("Нет позиций для слияния")
                return False
            
            merge_amount = amount if amount and amount <= max_merge else max_merge
            # Используем float_to_wei для точной конвертации (исправление precision issue v0.0.12)
            merge_amount_wei = float_to_wei(merge_amount)
            
            logger.info(f"🔄 Слияние позиций: {merge_amount:.4f} токенов...")
            logger.info(f"   YES: {self.yes_position:.4f}")
            logger.info(f"   NO:  {self.no_position:.4f}")
            logger.info(f"   Возврат ≈ ${merge_amount:.2f} USDT")
            
            # Получаем condition_id из рынка
            condition_id = self.market.condition_id
            
            if not condition_id:
                logger.error("Не найден condition_id!")
                return False
            
            # Вызываем merge через SDK
            result = self.order_builder.merge_positions(
                condition_id=condition_id,
                amount=merge_amount_wei,
                is_neg_risk=self.market.is_neg_risk,
                is_yield_bearing=self.market.is_yield_bearing,
            )
            
            # Проверяем результат - SDK возвращает TransactionSuccess или TransactionFailure
            if hasattr(result, 'success') and result.success:
                tx_hash = 'N/A'
                if hasattr(result, 'receipt') and hasattr(result.receipt, 'transactionHash'):
                    tx_hash = result.receipt.transactionHash.hex()[:20] + '...'
                logger.info(f"✅ Позиции объединены!")
                logger.info(f"   TX: {tx_hash}")
                
                # Обновляем позиции
                self.update_positions()
                return True
            elif hasattr(result, 'success') and not result.success:
                cause = result.cause if hasattr(result, 'cause') else 'Unknown'
                logger.error(f"Ошибка merge: {cause}")
                return False
            else:
                # Неожиданный формат результата - проверяем по типу
                # TransactionSuccess имеет receipt, TransactionFailure имеет cause
                if hasattr(result, 'receipt'):
                    logger.info(f"✅ Позиции объединены!")
                    self.update_positions()
                    return True
                else:
                    logger.error(f"Ошибка merge: неожиданный результат - {result}")
                    return False
                
        except Exception as e:
            logger.error(f"Ошибка слияния позиций: {e}")
            return False
    
    def exit_split_safe(self) -> bool:
        """
        Безопасный выход из SPLIT стратегии
        
        1. Отменяет все ордера
        2. Сливает YES+NO позиции в USDT
        
        Returns:
            True если успешно
        """
        logger.info("🚪 Безопасный выход из SPLIT...")
        
        # Шаг 1: Отменяем все ордера
        cancelled = self.cancel_all_orders()
        logger.info(f"   Отменено ордеров: {cancelled}")
        
        # Шаг 2: Сливаем позиции
        merged = self.merge_positions()
        
        if merged:
            logger.info("✅ Успешный выход из SPLIT!")
            # Показываем новый баланс
            balance = self.get_usdt_balance()
            logger.info(f"💰 Баланс: ${balance:.2f} USDT")
            return True
        else:
            logger.warning("⚠️  Не удалось слить позиции")
            logger.info("   Позиции остались на аккаунте")
            return False
    
    def handle_resolved_market(self, market_id: int, old_state) -> Dict:
        """
        Обработка закрытого (RESOLVED) рынка: отмена ордеров + merge позиций.
        
        Вызывается когда рынок переходит в статус RESOLVED.
        Переключает контекст на старое состояние рынка, отменяет ордера,
        сливает YES+NO → USDT и возвращает результат.
        
        Args:
            market_id: ID закрытого рынка
            old_state: MarketState рынка (из self.markets до удаления)
            
        Returns:
            Dict с результатами: {
                'market_id': int,
                'market_title': str,
                'cancelled': int,
                'merged': bool,
                'merge_amount': float,
                'balance_after': float,
            }
        """
        result = {
            'market_id': market_id,
            'market_title': old_state.market.title[:50] if old_state.market else f'#{market_id}',
            'cancelled': 0,
            'merged': False,
            'merge_amount': 0.0,
            'balance_after': 0.0,
        }
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🏁 РЫНОК #{market_id} ЗАКРЫТ (RESOLVED)!")
        logger.info(f"   {result['market_title']}")
        logger.info(f"{'='*60}")
        
        # Сохраняем текущий контекст
        saved_market = self.market
        saved_orderbook = self.orderbook
        saved_market_id = self.market_id
        saved_yes_pos = self.yes_position
        saved_no_pos = self.no_position
        saved_phase = self.split_phase
        
        try:
            # Переключаем контекст на закрытый рынок
            self.market = old_state.market
            self.orderbook = old_state.orderbook
            self.market_id = market_id
            self.yes_position = old_state.yes_position
            self.no_position = old_state.no_position
            self.split_phase = old_state.split_phase
            
            # Шаг 1: Отменяем все ордера на этом рынке
            logger.info("\n📋 Шаг 1: Отмена ордеров...")
            cancelled = self.cancel_all_orders()
            result['cancelled'] = cancelled
            logger.info(f"   Отменено ордеров: {cancelled}")
            
            # Шаг 2: Обновляем позиции (чтобы узнать актуальные балансы)
            self.update_positions()
            
            # Шаг 3: Выполняем merge (YES + NO → USDT)
            logger.info("\n🔄 Шаг 2: Merge позиций (YES + NO → USDT)...")
            max_merge = min(self.yes_position, self.no_position)
            
            if max_merge >= 0.01:
                result['merge_amount'] = max_merge
                merged = self.merge_positions()
                result['merged'] = merged
                
                if merged:
                    logger.info(f"   ✅ Merge выполнен! Слито: {max_merge:.4f} токенов")
                else:
                    logger.warning(f"   ⚠️ Merge не удался")
            else:
                logger.info("   ℹ️ Нет позиций для merge (уже слиты или дисбаланс)")
                # Проверяем оставшиеся позиции
                if self.yes_position > 0.01 or self.no_position > 0.01:
                    logger.info(f"   Остаток YES: {self.yes_position:.4f}, NO: {self.no_position:.4f}")
            
            # Показываем итоговый баланс
            balance = self.get_usdt_balance()
            result['balance_after'] = balance
            logger.info(f"\n💰 Баланс после: ${balance:.2f} USDT")
            logger.info(f"{'='*60}\n")
            
        except Exception as e:
            logger.error(f"❌ Ошибка обработки закрытого рынка #{market_id}: {e}")
        finally:
            # Восстанавливаем контекст
            self.market = saved_market
            self.orderbook = saved_orderbook
            self.market_id = saved_market_id
            self.yes_position = saved_yes_pos
            self.no_position = saved_no_pos
            self.split_phase = saved_phase
        
        return result
    
    def check_and_handle_resolved_markets(self) -> List[Dict]:
        """
        Проверить все текущие рынки на статус RESOLVED.
        
        Для каждого закрытого рынка: отменяет ордера + merge.
        Убирает обработанные рынки из self.markets и self.market_ids.
        
        Returns:
            Список результатов обработки (по одному Dict на рынок)
        """
        resolved_results = []
        resolved_ids = []
        
        for market_id, state in list(self.markets.items()):
            try:
                # Проверяем актуальный статус рынка
                market = self.api.get_market_by_id(market_id)
                if market and market.status == 'RESOLVED':
                    resolved_ids.append(market_id)
                    
                    # Обновляем market объект в state (для condition_id)
                    state.market = market
                    
                    result = self.handle_resolved_market(market_id, state)
                    resolved_results.append(result)
            except Exception as e:
                logger.error(f"Ошибка проверки статуса рынка #{market_id}: {e}")
        
        # Удаляем обработанные рынки
        for mid in resolved_ids:
            if mid in self.markets:
                del self.markets[mid]
            if mid in self.market_ids:
                self.market_ids.remove(mid)
            logger.info(f"🗑 Рынок #{mid} удалён из мониторинга")
        
        return resolved_results
    
    # =========================================================================
    # СТРАТЕГИЯ SPLIT
    # =========================================================================
    
    def strategy_split(self) -> Dict:
        """
        Стратегия SPLIT - безрисковый фарминг поинтов
        
        ФАЗА 1: Покупаем YES и NO токены
        ФАЗА 2: Выставляем SELL ордера на +offset от лучшего ASK
        
        Returns:
            Результат выполнения
        """
        result = {
            'success': False,
            'phase': self.split_phase,
            'message': '',
            'orders_placed': 0,
        }
        
        if not self.update_orderbook():
            result['message'] = 'Не удалось обновить ордербук'
            return result
        
        # Получаем цены из стаканов
        # YES стакан приходит напрямую от API
        # NO стакан вычисляется как complement от YES:
        #   NO ASK = 1 - YES BID (кто покупает YES = продаёт NO)
        #   NO BID = 1 - YES ASK (кто продаёт YES = покупает NO)
        precision = self.market.decimal_precision
        
        yes_ask = self.orderbook.get_best_yes_ask()  # Лучший YES SELL
        yes_bid = self.orderbook.get_best_yes_bid()  # Лучший YES BUY
        
        if not yes_ask or not yes_bid:
            result['message'] = 'Нет данных о ценах'
            return result
        
        # Цены NO из стакана
        no_bid = self.orderbook.get_best_no_bid(precision)  # Лучший NO BUY = 1 - YES ASK
        no_ask = self.orderbook.get_best_no_ask(precision)  # Лучший NO SELL = 1 - YES BID
        
        if no_ask is None:
            no_ask = round(1.0 - yes_bid, precision)  # Fallback
        if no_bid is None:
            no_bid = round(1.0 - yes_ask, precision)  # Fallback
        
        logger.info(f"📊 Цены: YES bid={yes_bid:.3f} ask={yes_ask:.3f} | NO bid={no_bid:.3f} ask={no_ask:.3f}")
        
        # =====================================================================
        # ФАЗА 1: Покупка токенов через SPLIT (USDT → YES + NO)
        # =====================================================================
        
        if self.split_phase == 0:
            logger.info("🔄 ФАЗА 1: SPLIT - конвертация USDT в YES + NO токены...")
            
            balance = self.get_usdt_balance()
            
            # Используем заданный размер ордера
            required = self.order_amount
            
            if balance < required:
                result['message'] = f'Недостаточно баланса: ${balance:.2f} < ${required:.2f}'
                return result
            
            # Используем SDK split_positions - одна транзакция!
            split_success = self.split_to_positions(required)
            
            if not split_success:
                result['message'] = 'Не удалось выполнить SPLIT'
                return result
            
            self.split_phase = 1
            result['success'] = True
            result['phase'] = 1
            result['message'] = f'SPLIT выполнен! Получено ~{required:.2f} YES + ~{required:.2f} NO'
            result['orders_placed'] = 0  # SPLIT - это не ордер, а транзакция
            
            return result
        
        # =====================================================================
        # ФАЗА 2: Выставление SELL ордеров
        # =====================================================================
        
        elif self.split_phase >= 1:
            # Проверяем позиции
            self.update_positions()
            
            # ИСПРАВЛЕНИЕ: используем AND вместо OR!
            # Если ОДНА сторона продана (fill) — нужно создать ордер для ДРУГОЙ стороны.
            # Блокируем только если ОБЕИХ позиций нет (нечего продавать).
            if self.yes_position < 0.1 and self.no_position < 0.1:
                logger.info("⏳ Нет позиций для выставления ордеров (обе стороны < 0.1)")
                result['message'] = 'Нет позиций для ордеров'
                return result
            
            # Если одна из сторон < 0.1, логируем предупреждение (не блокируем!)
            if self.yes_position < 0.1:
                logger.warning(f"⚠️ YES позиция почти пуста ({self.yes_position:.4f}) — ордер будет только для NO")
            if self.no_position < 0.1:
                logger.warning(f"⚠️ NO позиция почти пуста ({self.no_position:.4f}) — ордер будет только для YES")
            
            # Инициализируем флаги существования ордеров
            has_yes_order = False
            has_no_order = False
            
            # ВАЖНО: Проверяем, есть ли уже открытые ордера
            # Если оба ордера (YES и NO) уже есть - ждём исполнения
            # Если только 1 - нужно создать недостающий!
            try:
                existing_orders = self.api.get_open_orders()
                logger.info(f"🔍 Всего открытых ордеров: {len(existing_orders)}")
                
                # Фильтруем по market_id и только SELL ордера (side=1)
                market_orders = [o for o in existing_orders if o.market_id == self.market_id and o.side == 1]
                logger.info(f"🔍 SELL ордеров для рынка {self.market_id}: {len(market_orders)}")
                
                # Проверяем, какие ордера уже есть
                yes_token_id = self.market.yes_outcome.on_chain_id
                no_token_id = self.market.no_outcome.on_chain_id
                
                has_yes_order = any(o.token_id == yes_token_id for o in market_orders)
                has_no_order = any(o.token_id == no_token_id for o in market_orders)
                
                logger.info(f"   YES ордер: {'✅ есть' if has_yes_order else '❌ нет'}")
                logger.info(f"   NO ордер: {'✅ есть' if has_no_order else '❌ нет'}")
                
                # Если оба ордера есть - переходим к фазе проверки/обновления
                if has_yes_order and has_no_order:
                    logger.info(f"📋 Оба ордера на месте, проверяем позицию...")
                    
                    # Обновляем время для статистики поинтов
                    if self.last_order_time:
                        elapsed = (datetime.now() - self.last_order_time).total_seconds()
                        self.points.add_time_in_book(elapsed)
                    self.last_order_time = datetime.now()
                    
                    self.split_phase = 2
                    result['success'] = True
                    result['phase'] = 2
                    result['message'] = f'Ожидание исполнения {len(market_orders)} ордеров'
                    return result
                    
                # Если есть только часть ордеров - нужно создать недостающие
                if has_yes_order or has_no_order:
                    logger.info(f"⚠️  Есть только часть ордеров, создаём недостающие...")
                    # Не выходим, продолжаем к созданию ордеров
                    # has_yes_order и has_no_order уже определены выше
                    
            except Exception as e:
                logger.warning(f"Не удалось проверить ордера: {e}")
                has_yes_order = False
                has_no_order = False
            
            logger.info("🔄 ФАЗА 2: Выставление SELL ордеров...")
            
            # НЕ отменяем существующие ордера если создаём недостающие!
            # Отменяем только если нет ни одного ордера (начинаем сначала)
            if not has_yes_order and not has_no_order:
                cancelled = self.cancel_all_orders()
                if cancelled > 0:
                    logger.info(f"🗑️ Отменено {cancelled} старых ордеров")
                    # Сбрасываем хеши отменённых ордеров
                    self.yes_sell_order_id = None
                    self.no_sell_order_id = None
                    
                    # Небольшая пауза для синхронизации API
                    import time
                    time.sleep(0.5)
            
            # Рассчитываем цены для SELL
            # ВАЖНО: ставим на 2-е место, НЕ на 1-е!
            # yes_ask и no_ask - это текущие лучшие цены (чужие ордера)
            # Мы ставим на минимальный шаг выше них (0.1 цента = 0.001)
            precision = self.market.decimal_precision  # Обычно 3 знака
            min_tick = 10 ** (-precision)  # Минимальный шаг цены (0.001 для precision=3)
            
            # ВАЖНО: всегда используем min_tick для минимального отступа
            # Это 0.001 = 0.1 цента - минимальный шаг на бирже
            # НЕ используем split_offset здесь - он слишком большой!
            
            yes_sell_price = round(yes_ask + min_tick, precision)
            no_sell_price = round(no_ask + min_tick, precision)
            
            # Проверяем, что цены валидны (< 0.999 для precision=3)
            max_price = 1.0 - min_tick  # 0.999 для precision=3
            yes_sell_price = min(yes_sell_price, max_price)
            no_sell_price = min(no_sell_price, max_price)
            
            logger.info(f"📈 Цены SELL (2-е место): YES @ {yes_sell_price:.3f}, NO @ {no_sell_price:.3f}")
            logger.info(f"   (Лучшие ASK: YES={yes_ask:.3f}, NO={no_ask:.3f})")
            
            orders_placed = 0
            
            # ВАЖНО: используем order_amount как целевое количество для продажи
            # При SPLIT мы получаем ТОЧНО order_amount токенов YES и NO
            # Поэтому используем order_amount, проверяя что позиция достаточна
            sell_quantity_target = self.order_amount
            
            # Выставляем SELL YES (только если ордера ещё нет!)
            if not has_yes_order and self.yes_position > 0:
                # Используем order_amount если позиция достаточна, иначе всю позицию
                # Это исправляет ошибку когда ордер создавался на меньшую сумму
                if self.yes_position >= sell_quantity_target:
                    sell_quantity = sell_quantity_target
                else:
                    sell_quantity = self.yes_position
                sell_quantity = int(sell_quantity * 100000) / 100000
                
                logger.info(f"   YES: позиция={self.yes_position:.4f}, order_amount={self.order_amount:.4f}, sell_quantity={sell_quantity:.4f}")
                
                if sell_quantity >= 0.5:  # Минимум 0.5 токена
                    yes_sell = self.create_limit_order(
                        side='SELL',
                        token='YES',
                        price=yes_sell_price,
                        quantity=sell_quantity,
                    )
                    
                    if yes_sell:
                        self.yes_sell_order_id = yes_sell
                        orders_placed += 1
                    else:
                        logger.error(f"   ❌ YES ордер НЕ создан!")
                else:
                    logger.warning(f"   ⚠️ YES: quantity {sell_quantity:.4f} < 0.5, пропускаем")
            elif has_yes_order:
                logger.info(f"   YES: ордер уже существует ✅")
                orders_placed += 1  # Считаем существующий ордер
            else:
                logger.warning(f"   ⚠️ YES: нет позиции ({self.yes_position:.4f})")
            
            # Выставляем SELL NO (только если ордера ещё нет!)
            if not has_no_order and self.no_position > 0:
                # Используем order_amount если позиция достаточна, иначе всю позицию
                if self.no_position >= sell_quantity_target:
                    sell_quantity = sell_quantity_target
                else:
                    sell_quantity = self.no_position
                sell_quantity = int(sell_quantity * 100000) / 100000
                
                logger.info(f"   NO: позиция={self.no_position:.4f}, order_amount={self.order_amount:.4f}, sell_quantity={sell_quantity:.4f}")
                
                if sell_quantity >= 0.5:
                    no_sell = self.create_limit_order(
                        side='SELL',
                        token='NO',
                        price=no_sell_price,
                        quantity=sell_quantity,
                    )
                    
                    if no_sell:
                        self.no_sell_order_id = no_sell
                        orders_placed += 1
                    else:
                        logger.error(f"   ❌ NO ордер НЕ создан!")
                else:
                    logger.warning(f"   ⚠️ NO: quantity {sell_quantity:.4f} < 0.5, пропускаем")
            elif has_no_order:
                logger.info(f"   NO: ордер уже существует ✅")
                orders_placed += 1  # Считаем существующий ордер
            else:
                logger.warning(f"   ⚠️ NO: нет позиции ({self.no_position:.4f})")
            
            self.split_phase = 2
            result['success'] = orders_placed > 0
            result['phase'] = 2
            result['message'] = f'Выставлено {orders_placed} SELL ордеров'
            result['orders_placed'] = orders_placed
            
            return result
        
        return result
    
    def _safe_sell_price(self, token: str, target_price: float) -> Optional[float]:
        """
        Проверить что цена SELL ордера БЕЗОПАСНА (не пересечёт спред).
        
        КРИТИЧЕСКИ ВАЖНО: Перед созданием ордера ВСЕГДА перечитываем ордербук
        и проверяем что наша цена СТРОГО ВЫШЕ лучшего BID.
        Если цена опасна — поднимаем выше или отказываемся.
        
        Returns:
            Безопасная цена или None если невозможно
        """
        # Перечитываем ордербук ПРЯМО ПЕРЕД размещением
        if not self.update_orderbook():
            logger.error(f"   ❌ Не удалось обновить ордербук для проверки {token}")
            return None
        
        precision = self.market.decimal_precision
        min_tick = 10 ** (-precision)
        max_price = 1.0 - min_tick
        
        if token.upper() == 'YES':
            best_bid = self.orderbook.best_bid
            best_ask = self.orderbook.best_ask
        else:
            # NO orderbook: инвертировано относительно YES
            # get_no_prices() возвращает (no_buy, no_sell)
            #   no_buy = 1 - yes_best_bid = лучший NO ASK (цена покупки NO = цена продажи для маркет-мейкера)
            #   no_sell = 1 - yes_best_ask = лучший NO BID (цена продажи NO = цена покупки для маркет-мейкера)
            no_buy, no_sell = self.orderbook.get_no_prices(precision)
            best_bid = no_sell  # NO best BID = 1 - YES best ASK (самая высокая цена покупки NO)
            best_ask = no_buy   # NO best ASK = 1 - YES best BID (самая низкая цена продажи NO)
        
        if best_bid is None:
            # Нет покупателей — безопасно
            return round(min(target_price, max_price), precision)
        
        # === ГЛАВНАЯ ПРОВЕРКА: наша SELL цена ДОЛЖНА быть СТРОГО ВЫШЕ best_bid ===
        if target_price <= best_bid + min_tick / 2:
            # ОПАСНО! Наш ордер мог бы исполниться мгновенно!
            safe_price = best_bid + min_tick * 2  # Минимум 2 тика выше bid
            logger.warning(f"   ⛔ {token}: цена {target_price:.{precision}f} слишком близко к BID {best_bid:.{precision}f}!")
            logger.warning(f"   ⛔ {token}: поднимаем до {safe_price:.{precision}f}")
            target_price = safe_price
        
        # === Доп. проверка: не быть лучшим ASK ===
        if best_ask is not None and abs(target_price - best_ask) < min_tick / 2:
            # Мы были бы лучшим ASK — опасно!
            target_price = best_ask + min_tick
            logger.warning(f"   ⚠️ {token}: стали бы лучшим ASK, сдвигаем на {target_price:.{precision}f}")
        
        target_price = round(min(target_price, max_price), precision)
        
        # Финальная проверка
        if target_price <= best_bid:
            logger.error(f"   ❌ {token}: НЕВОЗМОЖНО разместить безопасно! target={target_price:.{precision}f} <= bid={best_bid:.{precision}f}")
            return None
        
        return target_price
    
    def _create_safe_sell_order(self, token: str, target_price: float, result: Dict,
                               known_quantity: float = None) -> Optional[str]:
        """
        Создать SELL ордер с полной проверкой безопасности.
        
        1. Проверяет достаточность позиции
        2. Валидирует цену через _safe_sell_price
        3. Создаёт ордер только если всё безопасно
        
        Args:
            known_quantity: Если указано — используем это количество вместо 
                          чтения позиций (для репозиции после отмены ордера).
        
        Returns:
            Order hash или None
        """
        precision = self.market.decimal_precision
        
        if known_quantity is not None:
            # При репозиции мы ЗНАЕМ количество из отменённого ордера —
            # не нужно перечитывать позиции (blockchain может показать 0
            # пока токены возвращаются из эскроу биржи)
            sell_quantity = known_quantity
            logger.debug(f"   {token}: используем known_quantity={sell_quantity:.4f}")
        else:
            # Обновляем позиции через API
            self.update_positions()
            
            if token.upper() == 'YES':
                sell_quantity = self.yes_position
            else:
                sell_quantity = self.no_position
        
        sell_quantity = int(sell_quantity * 100000) / 100000
        
        if sell_quantity < 0.5:
            logger.info(f"   {token}: недостаточно токенов ({sell_quantity:.4f})")
            return None
        
        # Проверяем безопасность цены (перечитывает ордербук!)
        safe_price = self._safe_sell_price(token, target_price)
        if safe_price is None:
            logger.error(f"   ❌ {token}: не удалось найти безопасную цену")
            return None
        
        # Создаём ордер (skip_balance_check при known_quantity — 
        # blockchain может показать 0 для залоченных токенов)
        new_hash = self.create_limit_order(
            side='SELL',
            token=token,
            price=safe_price,
            quantity=sell_quantity,
            skip_balance_check=(known_quantity is not None),
        )
        
        if new_hash:
            if token.upper() == 'YES':
                self.yes_sell_order_id = new_hash
            else:
                self.no_sell_order_id = new_hash
            logger.info(f"   ✅ {token} ордер создан @ {safe_price:.{precision}f} ({sell_quantity:.2f} shares)")
        
        return new_hash
    
    def _process_side_order(self, token: str, order, result: Dict) -> bool:
        """
        Обработать один ордер (YES или NO): проверка fill, позиции, переставление.
        
        Returns:
            True если ордер обработан (обновлён или создан)
        """
        precision = self.market.decimal_precision
        min_tick = 10 ** (-precision)
        max_price = 1.0 - min_tick
        is_yes = token.upper() == 'YES'
        ms = self.markets.get(self.market_id)  # MarketState для cooldown
        
        # =================================================================
        # 1. ПРОВЕРКА ЧАСТИЧНОГО ИСПОЛНЕНИЯ
        # =================================================================
        if order and order.is_partially_filled:
            filled = order.filled_quantity
            logger.warning(f"   🚨 {token}: ЧАСТИЧНО ИСПОЛНЕН! Filled={filled:.4f}, "
                          f"original={order.original_quantity:.4f}")
            result['fills_detected'].append({
                'token': token,
                'filled': filled,
                'original': order.original_quantity,
                'remaining': order.quantity,
                'price': order.price_per_share,
            })
            # СРОЧНО ОТМЕНЯЕМ остаток!
            self.cancel_order(order.id)
            time.sleep(0.5)
            order = None  # Пересоздадим ниже
        
        # =================================================================
        # 2. ЕСЛИ ОРДЕРА НЕТ — создаём новый СРАЗУ
        # =================================================================
        if order is None:
            position = self.yes_position if is_yes else self.no_position
            if position >= 0.5:
                logger.info(f"   ⚠️ {token}: ордера нет, создаём...")
                
                if is_yes:
                    best_ask = self.orderbook.get_best_yes_ask()
                else:
                    best_ask = self.orderbook.get_best_no_ask(precision)
                
                if best_ask:
                    target_price = best_ask + min_tick * ASK_POSITION_OFFSET
                else:
                    target_price = 0.50
                
                target_price = round(min(target_price, max_price), precision)
                
                new_hash = self._create_safe_sell_order(token, target_price, result)
                if new_hash:
                    result[f'{"yes" if is_yes else "no"}_updated'] = True
                    result['updated'] += 1
                return True
            else:
                logger.debug(f"   {token}: нет позиции ({position:.2f})")
                return False
        
        # =================================================================
        # 3. ОРДЕР СУЩЕСТВУЕТ — проверяем нашу позицию в стакане
        # =================================================================
        our_price = order.price_per_share
        
        if is_yes:
            # Ищем лучший YES ASK который НЕ наш
            best_other_ask = None
            for level in self.orderbook.asks:
                if abs(level.price - our_price) > min_tick / 2:
                    best_other_ask = level.price
                    break
            
            we_are_best = (self.orderbook.best_ask is not None and 
                          abs(our_price - self.orderbook.best_ask) < min_tick / 2)
        else:
            # Ищем лучший NO ASK который НЕ наш
            no_asks = self.orderbook.get_no_asks(precision)
            best_other_ask = None
            for level in no_asks:
                if abs(level.price - our_price) > min_tick / 2:
                    best_other_ask = level.price
                    break
            
            current_best_no_ask = self.orderbook.get_best_no_ask(precision)
            we_are_best = (current_best_no_ask is not None and 
                          abs(our_price - current_best_no_ask) < min_tick / 2)
        
        # =================================================================
        # 3a. МЫ ЕДИНСТВЕННЫЕ В СТАКАНЕ
        # =================================================================
        if best_other_ask is None:
            if is_yes:
                if self.orderbook.best_bid:
                    target_price = self.orderbook.best_bid + min_tick * 3
                else:
                    target_price = our_price
            else:
                no_bid, _ = self.orderbook.get_no_prices(precision)
                if no_bid:
                    target_price = no_bid + min_tick * 3
                else:
                    target_price = our_price
            logger.info(f"   {token}: единственный ASK, target={target_price:.{precision}f}")
            # Не переставляем если мы одни — некуда деваться
            return False
        
        # =================================================================
        # 3b. 🚨 МЫ ЛУЧШИЙ ASK — КРИТИЧЕСКАЯ ОПАСНОСТЬ!
        # =================================================================
        if we_are_best:
            # Cooldown: проверяем, не слишком ли часто переставляем
            if ms:
                last_repo = ms.last_reposition_yes if is_yes else ms.last_reposition_no
                if last_repo:
                    elapsed = (datetime.now() - last_repo).total_seconds()
                    # Для критической ситуации (мы лучший ASK) — cooldown в 2 раза короче
                    critical_cooldown = max(REPOSITION_COOLDOWN / 2, 2)
                    if elapsed < critical_cooldown:
                        logger.info(f"   ⏳ {token}: МЫ ЛУЧШИЙ ASK, но cooldown {critical_cooldown:.0f}с "
                                   f"(прошло {elapsed:.1f}с). Ждём.")
                        return False
            
            logger.warning(f"   🚨 {token}: МЫ ЛУЧШИЙ ASK ({our_price:.{precision}f})! ОТМЕНА + ПЕРЕСТАВЛЕНИЕ!")
            # Запоминаем количество из ордера ДО отмены
            reposition_quantity = order.quantity if hasattr(order, 'quantity') else order.original_quantity
            # СНАЧАЛА ОТМЕНЯЕМ
            self.cancel_order(order.id)
            time.sleep(0.5)
            
            # СРАЗУ ставим новый ордер на 2-е место (безопасность через _safe_sell_price)
            # Передаём known_quantity — blockchain может показать 0 для залоченных токенов
            new_target = round(min(best_other_ask + min_tick * ASK_POSITION_OFFSET, max_price), precision)
            new_hash = self._create_safe_sell_order(token, new_target, result,
                                                    known_quantity=reposition_quantity)
            
            # Запоминаем время переставления
            if ms:
                if is_yes:
                    ms.last_reposition_yes = datetime.now()
                else:
                    ms.last_reposition_no = datetime.now()
            
            if new_hash:
                logger.info(f"   ✅ {token}: переставлен на {new_target:.{precision}f} (было {our_price:.{precision}f})")
            else:
                logger.warning(f"   ⚠️ {token}: не удалось переставить, будет создан в следующем цикле")
            
            result['repositioned'].append({
                'token': token,
                'old_price': our_price,
                'new_price': new_target,
                'quantity': self.yes_position if is_yes else self.no_position,
                'delayed': False,
            })
            result['updated'] += 1
            return True
        
        # =================================================================
        # 3c. НОРМАЛЬНАЯ СИТУАЦИЯ — проверяем, на 2-м ли мы месте
        # =================================================================
        # Мы не лучший ASK. best_other_ask — это лучший чужой ASK (перед нами).
        # Целевая: best_other_ask + N ticks (стать ЗА ним с отступом ASK_POSITION_OFFSET)
        target_price = best_other_ask + min_tick * ASK_POSITION_OFFSET
        target_price = round(min(target_price, max_price), precision)
        
        price_diff = abs(our_price - target_price)
        
        if price_diff > min_tick / 2:
            # Cooldown: не переставляем чаще чем раз в REPOSITION_COOLDOWN секунд
            if ms:
                last_repo = ms.last_reposition_yes if is_yes else ms.last_reposition_no
                if last_repo:
                    elapsed = (datetime.now() - last_repo).total_seconds()
                    if elapsed < REPOSITION_COOLDOWN:
                        logger.info(f"   ⏳ {token}: нужно переставить "
                                   f"({our_price:.{precision}f} → {target_price:.{precision}f}), "
                                   f"но cooldown {REPOSITION_COOLDOWN}с (прошло {elapsed:.1f}с)")
                        return False
            
            reason = f"цена {our_price:.{precision}f} != целевая {target_price:.{precision}f}"
            logger.info(f"   🔄 {token}: переставляем ({reason})")
            
            # Запоминаем количество из ордера ДО отмены
            reposition_quantity = order.quantity if hasattr(order, 'quantity') else order.original_quantity
            
            if self.cancel_order(order.id):
                time.sleep(0.5)
                
                # СРАЗУ ставим новый ордер (безопасность через _safe_sell_price)
                # Передаём known_quantity — blockchain может показать 0 для залоченных токенов
                new_hash = self._create_safe_sell_order(token, target_price, result,
                                                        known_quantity=reposition_quantity)
                
                # Запоминаем время переставления
                if ms:
                    if is_yes:
                        ms.last_reposition_yes = datetime.now()
                    else:
                        ms.last_reposition_no = datetime.now()
                
                if new_hash:
                    logger.info(f"   ✅ {token}: переставлен на {target_price:.{precision}f} (было {our_price:.{precision}f})")
                else:
                    logger.warning(f"   ⚠️ {token}: не удалось переставить, будет создан в следующем цикле")
                
                result['repositioned'].append({
                    'token': token,
                    'old_price': our_price,
                    'new_price': target_price,
                    'quantity': self.yes_position if is_yes else self.no_position,
                    'delayed': False,
                })
            return True
        else:
            logger.debug(f"   {token} OK: цена {our_price:.{precision}f}, целевая {target_price:.{precision}f}")
            return False
    
    def check_and_update_split_orders(self) -> Dict:
        """
        Проверить и переставить SPLIT ордера чтобы ВСЕГДА быть на 2-м месте от лучшего ASK.
        
        МНОГОУРОВНЕВАЯ ЗАЩИТА ОТ ИСПОЛНЕНИЯ:
        ===================================
        1. Частичное исполнение → мгновенная отмена + уведомление
        2. Мы лучший ASK → мгновенная отмена + новый ордер (cooldown REPOSITION_COOLDOWN/2 сек)
        3. Цена не на 2-м месте → отмена + новый ордер (cooldown REPOSITION_COOLDOWN сек)
        4. Перед созданием ордера → перечитываем ордербук и проверяем цену
        5. Цена SELL ВСЕГДА строго выше best BID (никогда не пересечёт спред)
        6. Не становимся лучшим ASK при создании
        
        Returns:
            Результат проверки {'updated': int, 'message': str}
        """
        result = {
            'updated': 0,
            'message': '',
            'yes_updated': False,
            'no_updated': False,
            'repositioned': [],
            'fills_detected': [],
        }
        
        if self.split_phase < 2:
            return result
        
        if not self.ENABLE_ORDER_REPOSITIONING:
            result['message'] = 'Переставление отключено, ордера на месте'
            return result
        
        if not self.update_orderbook():
            result['message'] = 'Не удалось обновить ордербук'
            return result
        
        # Получаем текущие открытые ордера
        try:
            orders = self.api.get_open_orders()
            market_orders = [o for o in orders if o.market_id == self.market_id and o.side == 1]
        except Exception as e:
            logger.error(f"Ошибка получения ордеров: {e}")
            result['message'] = f'Ошибка получения ордеров: {e}'
            return result
        
        if not market_orders:
            logger.warning("⚠️ Нет открытых SELL ордеров!")
            
            # Обновляем позиции, чтобы узнать что осталось
            self.update_positions()
            
            has_yes = self.yes_position >= 0.5
            has_no = self.no_position >= 0.5
            
            if not has_yes and not has_no:
                # Обе стороны проданы — нечего делать
                logger.warning("   Обе стороны проданы, позиций нет.")
                self.split_phase = 0
                result['message'] = 'Все ордера исполнились, позиций нет'
                return result
            
            # Есть токены — СРАЗУ создаём ордера для оставшихся позиций
            logger.info(f"   Позиции: YES={self.yes_position:.2f}, NO={self.no_position:.2f}")
            logger.info(f"   Пересоздаём ордера для оставшихся позиций...")
            
            precision = self.market.decimal_precision
            min_tick = 10 ** (-precision)
            
            if has_yes:
                yes_ask = self.orderbook.get_best_yes_ask()
                target = (yes_ask + min_tick) if yes_ask else 0.50
                target = round(min(target, 1.0 - min_tick), precision)
                new_hash = self._create_safe_sell_order('YES', target, result)
                if new_hash:
                    result['updated'] += 1
                    result['yes_updated'] = True
                    logger.info(f"   ✅ YES ордер пересоздан @ {target}")
            
            if has_no:
                self.update_orderbook()  # Обновляем стакан перед NO
                no_ask = self.orderbook.get_best_no_ask(precision)
                target = (no_ask + min_tick) if no_ask else 0.50
                target = round(min(target, 1.0 - min_tick), precision)
                new_hash = self._create_safe_sell_order('NO', target, result)
                if new_hash:
                    result['updated'] += 1
                    result['no_updated'] = True
                    logger.info(f"   ✅ NO ордер пересоздан @ {target}")
            
            if result['updated'] > 0:
                self.split_phase = 2
                result['message'] = f'Пересоздано {result["updated"]} ордеров для оставшихся позиций'
            else:
                self.split_phase = 1
                result['message'] = 'Не удалось пересоздать ордера'
            return result
        
        # Разделяем ордера на YES и NO
        yes_order = None
        no_order = None
        
        yes_token_id = self.market.yes_outcome.on_chain_id
        no_token_id = self.market.no_outcome.on_chain_id
        
        for order in market_orders:
            if order.token_id == yes_token_id:
                yes_order = order
            elif order.token_id == no_token_id:
                no_order = order
        
        # Обновляем позиции
        self.update_positions()
        
        # Обрабатываем YES
        self._process_side_order('YES', yes_order, result)
        
        # Небольшая пауза между сторонами для стабильности
        time.sleep(0.1)
        
        # Обновляем ордербук перед проверкой NO (мог измениться после YES)
        self.update_orderbook()
        
        # Обрабатываем NO
        self._process_side_order('NO', no_order, result)
        
        # Статистика поинтов
        if self.last_order_time:
            elapsed = (datetime.now() - self.last_order_time).total_seconds()
            self.points.add_time_in_book(elapsed)
        self.last_order_time = datetime.now()
        
        if result['updated'] > 0:
            result['message'] = f"Переставлено {result['updated']} ордеров"
        else:
            result['message'] = "Ордера на месте, переставление не требуется"
        
        return result
    
    # =========================================================================
    # ГЛАВНЫЙ ЦИКЛ
    # =========================================================================
    
    def update_cycle(self) -> Dict:
        """
        Один цикл обновления
        
        Returns:
            Результат цикла
        """
        self.cycles += 1
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🔄 ЦИКЛ #{self.cycles}")
        logger.info(f"{'='*60}")
        
        if self.strategy == 'split':
            result = self.strategy_split()
            
            # Если фаза 2 - проверяем и переставляем ордера для оптимальной позиции
            if self.split_phase >= 2:
                logger.info("📊 Проверка позиции ордеров...")
                update_result = self.check_and_update_split_orders()
                
                if update_result['updated'] > 0:
                    logger.info(f"🔄 {update_result['message']}")
                else:
                    logger.info(f"✅ {update_result['message']}")
                
                # Показываем текущее состояние
                self._log_current_state()
            
            return result
        else:
            logger.warning(f"Неизвестная стратегия: {self.strategy}")
            return {'success': False, 'message': 'Unknown strategy'}
    
    def _log_current_state(self):
        """Вывести текущее состояние ордеров и позиций"""
        try:
            orders = self.api.get_open_orders()
            market_orders = [o for o in orders if o.market_id == self.market_id and o.side == 1]
            
            yes_token_id = self.market.yes_outcome.on_chain_id
            no_token_id = self.market.no_outcome.on_chain_id
            
            yes_order = None
            no_order = None
            
            for order in market_orders:
                if order.token_id == yes_token_id:
                    yes_order = order
                elif order.token_id == no_token_id:
                    no_order = order
            
            # Получаем текущие лучшие цены
            yes_best_ask = self.orderbook.best_ask if self.orderbook else None
            no_buy, no_best_ask = self.orderbook.get_no_prices(self.market.decimal_precision) if self.orderbook else (None, None)
            
            logger.info(f"📈 Состояние:")
            logger.info(f"   ✅ Выставлено {len(market_orders)} SELL ордеров")
            
            if yes_order:
                position_str = "🥇 1-е (ОПАСНО!)" if yes_best_ask and abs(yes_order.price_per_share - yes_best_ask) < 0.002 else "🥈 2-е+"
                logger.info(f"   YES SELL @ {yes_order.price_per_share:.3f} ({position_str} место) | Best ASK: {yes_best_ask:.3f if yes_best_ask else 'N/A'}")
            else:
                logger.info(f"   YES: нет ордера")
            
            if no_order:
                position_str = "🥇 1-е (ОПАСНО!)" if no_best_ask and abs(no_order.price_per_share - no_best_ask) < 0.002 else "🥈 2-е+"
                logger.info(f"   NO SELL @ {no_order.price_per_share:.3f} ({position_str} место) | Best ASK: {no_best_ask:.3f if no_best_ask else 'N/A'}")
            else:
                logger.info(f"   NO: нет ордера")
            
            # Примерные поинты
            logger.info(f"   💎 Примерно поинтов: {self.points.estimate_points():.1f} PP")
            
        except Exception as e:
            logger.debug(f"Ошибка вывода состояния: {e}")
    
    # =========================================================================
    # МУЛЬТИРЫНОЧНЫЙ РЕЖИМ
    # =========================================================================
    
    def discover_active_markets(self) -> List[int]:
        """
        Автоматически найти рынки где у нас есть позиции или ордера
        
        Returns:
            Список ID рынков с активными позициями/ордерами
        """
        active_market_ids = set()
        
        try:
            # 1. Получаем все открытые ордера
            logger.info("🔍 Поиск рынков с открытыми ордерами...")
            orders = self.api.get_open_orders()
            for order in orders:
                if order.market_id and order.side == 1:  # SELL ордера
                    active_market_ids.add(order.market_id)
            
            logger.info(f"   Найдено {len(active_market_ids)} рынков с ордерами")
            
            # 2. Получаем все позиции
            logger.info("🔍 Поиск рынков с позициями...")
            positions = self.api.get_all_positions()  # Используем get_all_positions
            for pos in positions:
                if pos.market and pos.market.id and float(pos.amount) > 0.5:  # Минимальный размер позиции
                    active_market_ids.add(pos.market.id)
            
            logger.info(f"   Всего найдено {len(active_market_ids)} активных рынков")
            
        except Exception as e:
            logger.error(f"Ошибка поиска рынков: {e}")
        
        return list(active_market_ids)
    
    def load_market_state(self, market_id: int) -> Optional[MarketState]:
        """
        Загрузить состояние рынка
        
        Args:
            market_id: ID рынка
            
        Returns:
            MarketState или None при ошибке
        """
        try:
            # Загружаем данные рынка
            market = self.api.get_market_by_id(market_id)  # Исправлено: get_market_by_id
            if not market:
                logger.warning(f"   ⚠️  Рынок #{market_id} не найден")
                return None
            
            # Проверяем статус
            if market.status not in ['REGISTERED', 'ACTIVE', 'OPEN']:
                logger.warning(f"   ⚠️  Рынок #{market_id} не активен: {market.status}")
                return None
            
            # Создаём состояние
            state = MarketState(
                market_id=market_id,
                market=market,
            )
            
            # Загружаем ордербук (принимает market_id)
            orderbook = self.api.get_orderbook(market_id)
            state.orderbook = orderbook
            
            # Загружаем позиции для этого рынка
            positions = self.api.get_all_positions()  # Исправлено
            for pos in positions:
                if pos.market and pos.market.id == market_id:
                    # Используем index_set вместо имени, т.к. имена могут быть разные (Yes/No, Up/Down, etc)
                    if pos.outcome and pos.outcome.index_set == 1:  # Первый исход (YES/Up)
                        state.yes_position = wei_to_float(pos.amount)  # Конвертируем из wei
                    elif pos.outcome and pos.outcome.index_set == 2:  # Второй исход (NO/Down)
                        state.no_position = wei_to_float(pos.amount)  # Конвертируем из wei
            
            # Проверяем ордера
            orders = self.api.get_open_orders()
            for order in orders:
                if order.market_id == market_id and order.side == 1:  # SELL
                    if order.token_id == market.yes_outcome.on_chain_id:
                        state.yes_sell_order_id = order.id
                    elif order.token_id == market.no_outcome.on_chain_id:
                        state.no_sell_order_id = order.id
            
            # Определяем фазу
            # Фаза 2 если есть ХОТЯ БЫ ОДИН ордер (YES или NO)
            # Это позволяет мониторить и переставлять частичные позиции
            if state.yes_sell_order_id or state.no_sell_order_id:
                state.split_phase = 2
            elif state.yes_position > 0.5 or state.no_position > 0.5:
                state.split_phase = 1
            else:
                state.split_phase = 0
            
            state.last_update = datetime.now()
            
            logger.info(f"   ✅ #{market_id}: {market.title[:40]}...")
            logger.info(f"      YES: {state.yes_position:.2f} | NO: {state.no_position:.2f} | Фаза: {state.split_phase}")
            
            return state
            
        except Exception as e:
            logger.error(f"Ошибка загрузки рынка #{market_id}: {e}")
            return None
    
    def load_all_markets(self) -> int:
        """
        Загрузить все рынки (из списка или авто-обнаружение)
        
        Returns:
            Количество загруженных рынков
        """
        # Если список пустой - авто-обнаружение
        if not self.market_ids:
            self.market_ids = self.discover_active_markets()
        
        if not self.market_ids:
            logger.warning("⚠️  Не найдено активных рынков")
            return 0
        
        logger.info(f"\n📊 Загрузка {len(self.market_ids)} рынков...")
        
        loaded = 0
        for market_id in self.market_ids:
            state = self.load_market_state(market_id)
            if state:
                self.markets[market_id] = state
                loaded += 1
        
        # Для обратной совместимости - устанавливаем первый рынок как основной
        if self.markets:
            first_id = list(self.markets.keys())[0]
            first_state = self.markets[first_id]
            self.market_id = first_id
            self.market = first_state.market
            self.orderbook = first_state.orderbook
            self.yes_position = first_state.yes_position
            self.no_position = first_state.no_position
            self.split_phase = first_state.split_phase
        
        logger.info(f"\n✅ Загружено {loaded}/{len(self.market_ids)} рынков")
        return loaded
    
    def refresh_markets(self) -> int:
        """
        Принудительное обновление списка рынков.
        Перезапускает discover_active_markets для обнаружения новых рынков
        и удаления неактивных.
        
        Returns:
            Количество загруженных рынков
        """
        logger.info("🔄 Принудительное обновление списка рынков...")
        
        old_ids = set(self.market_ids) if self.market_ids else set()
        
        # Заново обнаруживаем рынки
        self.market_ids = self.discover_active_markets()
        
        new_ids = set(self.market_ids) if self.market_ids else set()
        
        added = new_ids - old_ids
        removed = old_ids - new_ids
        
        if added:
            logger.info(f"   ➕ Новые рынки: {added}")
        if removed:
            logger.info(f"   ➖ Удалённые рынки: {removed}")
            # Убираем удалённые рынки из self.markets
            for mid in removed:
                if mid in self.markets:
                    del self.markets[mid]
        
        if not added and not removed:
            logger.info("   ℹ️ Изменений нет")
        
        # Перезагружаем состояния
        return self.load_all_markets()
    
    def update_market_state(self, market_id: int) -> bool:
        """
        Обновить состояние конкретного рынка
        
        Args:
            market_id: ID рынка
            
        Returns:
            True если успешно
        """
        if market_id not in self.markets:
            return False
        
        state = self.markets[market_id]
        
        try:
            # Обновляем ордербук (принимает market_id)
            orderbook = self.api.get_orderbook(market_id)
            state.orderbook = orderbook
            
            # Обновляем позиции
            positions = self.api.get_all_positions()  # Исправлено
            state.yes_position = 0.0
            state.no_position = 0.0
            for pos in positions:
                if pos.market and pos.market.id == market_id:
                    # Используем index_set вместо имени, т.к. имена могут быть разные (Yes/No, Up/Down, etc)
                    if pos.outcome and pos.outcome.index_set == 1:  # Первый исход (YES/Up)
                        state.yes_position = wei_to_float(pos.amount)  # Конвертируем из wei
                    elif pos.outcome and pos.outcome.index_set == 2:  # Второй исход (NO/Down)
                        state.no_position = wei_to_float(pos.amount)  # Конвертируем из wei
            
            # Обновляем ордера
            orders = self.api.get_open_orders()
            state.yes_sell_order_id = None
            state.no_sell_order_id = None
            for order in orders:
                if order.market_id == market_id and order.side == 1:
                    if state.market and order.token_id == state.market.yes_outcome.on_chain_id:
                        state.yes_sell_order_id = order.id
                    elif state.market and order.token_id == state.market.no_outcome.on_chain_id:
                        state.no_sell_order_id = order.id
            
            # Обновляем фазу
            # Фаза 2 если есть ХОТЯ БЫ ОДИН ордер (YES или NO)
            # Это позволяет мониторить и переставлять частичные позиции
            if state.yes_sell_order_id or state.no_sell_order_id:
                state.split_phase = 2
            elif state.yes_position > 0.5 or state.no_position > 0.5:
                state.split_phase = 1
            else:
                state.split_phase = 0
            
            state.last_update = datetime.now()
            return True
            
        except Exception as e:
            logger.error(f"Ошибка обновления рынка #{market_id}: {e}")
            state.errors_count += 1
            return False
    
    def _switch_to_market(self, market_id: int):
        """
        Переключить контекст трейдера на указанный рынок
        
        Args:
            market_id: ID рынка
        """
        if market_id not in self.markets:
            raise ValueError(f"Рынок #{market_id} не найден")
        
        state = self.markets[market_id]
        
        # Обновляем состояние рынка перед переключением
        self.update_market_state(market_id)
        
        # Переключаем контекст
        self.market = state.market
        self.orderbook = state.orderbook
        self.market_id = market_id
        self.yes_position = state.yes_position
        self.no_position = state.no_position
        self.split_phase = state.split_phase
    
    def check_and_reposition_market(self, market_id: int) -> Dict:
        """
        Проверить и переставить ордера на конкретном рынке
        
        Args:
            market_id: ID рынка
            
        Returns:
            Результат проверки
        """
        result = {'updated': 0, 'message': '', 'market_id': market_id}
        
        if market_id not in self.markets:
            result['message'] = f'Рынок #{market_id} не найден'
            return result
        
        state = self.markets[market_id]
        
        if state.split_phase < 2:
            result['message'] = f'Фаза {state.split_phase} - ордера не выставлены'
            return result
        
        if not self.ENABLE_ORDER_REPOSITIONING:
            result['message'] = 'Переставление отключено'
            return result
        
        # Обновляем состояние рынка
        if not self.update_market_state(market_id):
            result['message'] = 'Ошибка обновления'
            return result
        
        # Временно переключаем контекст на этот рынок
        old_market = self.market
        old_orderbook = self.orderbook
        old_market_id = self.market_id
        old_yes_pos = self.yes_position
        old_no_pos = self.no_position
        old_phase = self.split_phase
        
        try:
            self.market = state.market
            self.orderbook = state.orderbook
            self.market_id = market_id
            self.yes_position = state.yes_position
            self.no_position = state.no_position
            self.split_phase = state.split_phase
            
            # Вызываем существующую логику проверки
            check_result = self.check_and_update_split_orders()
            
            result['updated'] = check_result.get('updated', 0)
            result['message'] = check_result.get('message', '')
            result['repositioned'] = check_result.get('repositioned', [])
            result['fills_detected'] = check_result.get('fills_detected', [])
            
            # Обновляем состояние после изменений
            self.update_market_state(market_id)
            
        finally:
            # Восстанавливаем контекст
            self.market = old_market
            self.orderbook = old_orderbook
            self.market_id = old_market_id
            self.yes_position = old_yes_pos
            self.no_position = old_no_pos
            self.split_phase = old_phase
        
        return result
    
    def monitor_cycle(self) -> Dict:
        """
        Цикл мониторинга всех рынков
        
        Returns:
            Результат цикла
        """
        self.cycles += 1
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🔄 ЦИКЛ МОНИТОРИНГА #{self.cycles}{' (fallback)' if getattr(self, '_is_fallback', False) else ''}")
        logger.info(f"{'='*60}")
        
        total_updated = 0
        total_errors = 0
        orders_placed = 0
        all_repositioned = []  # Собираем информацию о всех переставленных ордерах
        all_fills = []  # Собираем обнаруженные исполнения
        
        for market_id, state in self.markets.items():
            market_name = state.market.title[:30] if state.market else f"#{market_id}"
            
            # Рынки в Фазе 1 (есть токены, нет ордеров) - нужно выставить ордера
            if state.split_phase == 1:
                logger.info(f"\n📊 {market_name}...")
                logger.info(f"   🔄 Фаза 1 - выставляем SELL ордера...")
                
                try:
                    # Переключаем контекст на этот рынок
                    self._switch_to_market(market_id)
                    
                    # Вызываем strategy_split для выставления ордеров
                    result = self.strategy_split()
                    
                    if result.get('success') or result.get('phase') == 2:
                        state.split_phase = 2
                        orders_placed += result.get('orders_placed', 0)
                        logger.info(f"   ✅ Ордера выставлены!")
                    elif result.get('orders_placed', 0) > 0:
                        orders_placed += result['orders_placed']
                        logger.info(f"   ⚠️ Частично выставлено: {result['orders_placed']} ордеров")
                    else:
                        logger.warning(f"   ⚠️ {result.get('message', 'Не удалось выставить ордера')}")
                        
                except Exception as e:
                    logger.error(f"   ❌ Ошибка: {e}")
                    total_errors += 1
                
                time.sleep(0.5)
                continue
            
            # Пропускаем рынки в Фазе 0 (нет токенов)
            if state.split_phase < 1:
                continue
            
            logger.info(f"\n📊 {market_name}...")
            
            try:
                result = self.check_and_reposition_market(market_id)
                
                if result['updated'] > 0:
                    logger.info(f"   🔄 Переставлено: {result['updated']}")
                    total_updated += result['updated']
                    
                    # Собираем информацию о переставленных ордерах для уведомлений
                    for repo_info in result.get('repositioned', []):
                        all_repositioned.append({
                            'market_name': market_name,
                            'market_id': market_id,
                            **repo_info
                        })
                    
                    # Собираем информацию об исполнениях
                    for fill_info in result.get('fills_detected', []):
                        all_fills.append({
                            'market_name': market_name,
                            'market_id': market_id,
                            **fill_info
                        })
                else:
                    # Показываем краткую информацию о ценах
                    if state.orderbook:
                        precision = state.market.decimal_precision if state.market else 3
                        yes_ask = state.orderbook.best_ask
                        no_bid, no_ask = state.orderbook.get_no_prices(precision)
                        logger.info(f"   ✅ OK | YES ask={yes_ask:.3f} | NO ask={no_ask:.3f}")
                    else:
                        logger.info(f"   ✅ OK")
                        
            except Exception as e:
                logger.error(f"   ❌ Ошибка: {e}")
                total_errors += 1
            
            # Небольшая пауза между рынками
            time.sleep(0.5)
        
        return {
            'success': total_errors == 0,
            'message': f'Обновлено {total_updated} ордеров на {len(self.markets)} рынках',
            'updated': total_updated,
            'errors': total_errors,
            'repositioned': all_repositioned,  # Информация о переставленных ордерах
            'fills_detected': all_fills,  # Обнаруженные исполнения
        }
    
    def start_monitor(self):
        """Запустить бота в режиме мониторинга нескольких рынков"""
        print_config()
        
        if not validate_config():
            logger.error("Ошибка конфигурации!")
            return
        
        logger.info("🚀 Запуск Predict.fun Monitor Bot...")
        
        if not self.init_sdk():
            return
        
        if not self.authenticate():
            return
        
        if not self.set_approvals():
            return
        
        # Загружаем все рынки
        loaded = self.load_all_markets()
        if loaded == 0:
            logger.error("Не удалось загрузить ни одного рынка!")
            return
        
        # Проверяем баланс
        balance = self.get_usdt_balance()
        logger.info(f"💰 Баланс: ${balance:.2f} USDT")
        
        self.running = True
        
        # Выводим список рынков
        print(f"""
    ╔════════════════════════════════════════════════════════════╗
    ║     🔍 РЕЖИМ МОНИТОРИНГА                                   ║
    ║     Рынков: {len(self.markets):<5}                                         ║
    ║                                                            ║
    ║     Нажмите Ctrl+C для остановки                          ║
    ╚════════════════════════════════════════════════════════════╝
        """)
        
        logger.info("\n📋 Активные рынки:")
        for market_id, state in self.markets.items():
            status = "🟢" if state.split_phase >= 2 else "🟡" if state.split_phase == 1 else "⚪"
            name = state.market.title[:40] if state.market else "Unknown"
            logger.info(f"   {status} #{market_id}: {name}")
        
        try:
            while self.running:
                try:
                    result = self.monitor_cycle()
                    
                    if result.get('success'):
                        logger.info(f"\n✅ {result.get('message', 'OK')}")
                    else:
                        logger.warning(f"\n⚠️  {result.get('message', 'Unknown')}")
                    
                    logger.info(f"⏳ Следующий цикл через {CHECK_INTERVAL} сек...")
                    time.sleep(CHECK_INTERVAL)
                    
                except Exception as e:
                    logger.error(f"Ошибка в цикле: {e}")
                    time.sleep(5)
        
        except KeyboardInterrupt:
            logger.info("\n🛑 Получен сигнал остановки...")
        
        finally:
            self.stop(safe_exit=False)  # В режиме мониторинга не делаем merge
    
    def run_split_cycle(self) -> bool:
        """
        Выполнить один цикл SPLIT на текущем рынке:
        1. SPLIT USDT в YES + NO токены
        2. Выставить SELL ордера
        
        Returns:
            True если успешно
        """
        try:
            # Сбрасываем позиции перед проверкой нового рынка
            self.yes_position = 0.0
            self.no_position = 0.0
            
            # Обновляем ордербук
            self.update_orderbook()
            
            # Обновляем позиции для ТЕКУЩЕГО рынка
            self.update_positions()
            logger.info(f"📊 Позиции на рынке #{self.market_id}: YES={self.yes_position:.2f}, NO={self.no_position:.2f}")
            
            # Проверяем, есть ли уже позиции на этом рынке
            has_positions = self.yes_position > 0.5 or self.no_position > 0.5
            
            # Определяем сколько нужно докупить
            # Если позиции есть, но меньше order_amount - докупаем разницу
            current_position = max(self.yes_position, self.no_position)
            need_to_buy = self.order_amount - current_position
            
            if not has_positions:
                # ФАЗА 1: SPLIT - позиций нет, покупаем полную сумму
                logger.info("🔄 ФАЗА 1: SPLIT - конвертация USDT в YES + NO токены...")
                
                # Логируем token IDs для отладки
                yes_token_id = int(self.market.yes_outcome.on_chain_id)
                no_token_id = int(self.market.no_outcome.on_chain_id)
                logger.info(f"   Market ID: {self.market_id}")
                logger.info(f"   YES token ID: {yes_token_id}")
                logger.info(f"   NO token ID: {no_token_id}")
                logger.info(f"   Condition ID: {self.market.condition_id}")
                
                if not self.split_to_positions(self.order_amount):
                    logger.error("❌ Ошибка SPLIT!")
                    return False
                    
                logger.info(f"✅ SPLIT выполнен! Получено ~{self.order_amount:.2f} YES + ~{self.order_amount:.2f} NO")
                
                # Ждём синхронизации блокчейна
                logger.info("⏳ Ожидание синхронизации блокчейна (3 сек)...")
                time.sleep(3)
                
                # Проверяем баланс напрямую через правильный контракт
                # (update_positions теперь использует правильный контракт)
                self.update_positions()
                yes_balance = self.yes_position
                no_balance = self.no_position
                
                logger.info(f"📊 Баланс из блокчейна (SDK): YES={yes_balance:.4f}, NO={no_balance:.4f}")
                
                if yes_balance < 0.5:
                    logger.warning("⚠️  Токены не обнаружены! Возможно SPLIT не выполнился.")
                    logger.warning(f"   Проверьте транзакцию в Explorer")
                    # Всё равно пытаемся продолжить с установленными вручную значениями
                    self.yes_position = self.order_amount
                    self.no_position = self.order_amount
                else:
                    self.yes_position = yes_balance
                    self.no_position = no_balance
                    
            elif need_to_buy >= 1.0:
                # Позиции есть, но меньше чем нужно - докупаем разницу
                logger.info(f"ℹ️  Позиции: YES={self.yes_position:.2f}, NO={self.no_position:.2f}")
                logger.info(f"🔄 Докупаем ещё ${need_to_buy:.2f} до целевой суммы ${self.order_amount:.2f}...")
                
                if not self.split_to_positions(need_to_buy):
                    logger.warning("⚠️  Не удалось докупить, продолжаем с текущими позициями")
                else:
                    logger.info(f"✅ Докуплено ~{need_to_buy:.2f} токенов")
                    time.sleep(3)
                    self.update_positions()
                    
            else:
                logger.info(f"ℹ️  Уже есть достаточно позиций: YES={self.yes_position:.2f}, NO={self.no_position:.2f}")
            
            # ФАЗА 2: Выставление SELL ордеров
            logger.info("🔄 ФАЗА 2: Выставление SELL ордеров...")
            
            # Обновляем ордербук
            self.update_orderbook()
            
            # Вычисляем цены для 2-го места
            yes_sell_price, no_sell_price = self.calculate_sell_prices()
            
            if not yes_sell_price or not no_sell_price:
                logger.error("❌ Не удалось вычислить цены!")
                return False
            
            logger.info(f"📈 Цены SELL (2-е место): YES @ {yes_sell_price}, NO @ {no_sell_price}")
            
            orders_created = 0
            
            # YES ордер - используем ВСЮ позицию
            if self.yes_position >= 0.5:
                # Используем всю позицию для SELL (чтобы не оставлять "хвосты")
                sell_quantity = self.yes_position
                sell_quantity = int(sell_quantity * 100000) / 100000
                
                yes_hash = self.create_limit_order(
                    side='SELL',
                    token='YES',
                    price=yes_sell_price,
                    quantity=sell_quantity,
                )
                if yes_hash:
                    self.yes_sell_order_id = yes_hash  # Будет обновлено на реальный ID позже
                    orders_created += 1
            
            # NO ордер - используем ВСЮ позицию
            if self.no_position >= 0.5:
                sell_quantity = self.no_position
                sell_quantity = int(sell_quantity * 100000) / 100000
                
                no_hash = self.create_limit_order(
                    side='SELL',
                    token='NO',
                    price=no_sell_price,
                    quantity=sell_quantity,
                )
                if no_hash:
                    self.no_sell_order_id = no_hash  # Будет обновлено на реальный ID позже
                    orders_created += 1
            
            if orders_created > 0:
                logger.info(f"✅ Выставлено {orders_created} SELL ордеров")
                return True
            else:
                logger.warning("⚠️  Ордера не созданы")
                return False
                
        except Exception as e:
            logger.error(f"Ошибка в run_split_cycle: {e}")
            return False

    def start(self):
        """Запустить бота"""
        print_config()
        
        if not validate_config():
            logger.error("Ошибка конфигурации!")
            return
        
        # Инициализация
        logger.info("🚀 Запуск Predict.fun Trading Bot...")
        
        if not self.init_sdk():
            return
        
        if not self.authenticate():
            return
        
        if not self.set_approvals():
            return
        
        if not self.load_market(self.market_id):
            return
        
        # Проверяем баланс
        balance = self.get_usdt_balance()
        logger.info(f"💰 Баланс: ${balance:.2f} USDT")
        
        if balance < self.order_amount * 2:
            logger.error(f"Недостаточно баланса! Нужно минимум ${self.order_amount * 2:.2f}")
            return
        
        self.running = True
        
        print(f"""
    ╔════════════════════════════════════════════════════════════╗
    ║     🚀 БОТ ЗАПУЩЕН!                                       ║
    ║     Стратегия: {self.strategy.upper():<20}                    ║
    ║     Рынок: #{self.market_id:<10}                              ║
    ║     Размер ордера: ${self.order_amount:<10.2f}                     ║
    ║                                                            ║
    ║     Нажмите Ctrl+C для остановки                          ║
    ╚════════════════════════════════════════════════════════════╝
        """)
        
        try:
            while self.running:
                try:
                    result = self.update_cycle()
                    
                    # Логируем результат
                    if result.get('success'):
                        logger.info(f"✅ {result.get('message', 'OK')}")
                    else:
                        logger.warning(f"⚠️  {result.get('message', 'Unknown')}")
                    
                    # Пауза между циклами
                    logger.info(f"⏳ Следующий цикл через {CHECK_INTERVAL} сек...")
                    time.sleep(CHECK_INTERVAL)
                    
                except Exception as e:
                    logger.error(f"Ошибка в цикле: {e}")
                    time.sleep(5)
        
        except KeyboardInterrupt:
            logger.info("\n🛑 Получен сигнал остановки...")
        
        finally:
            self.stop()
    
    def stop(self, safe_exit: bool = True):
        """
        Остановить бота
        
        Args:
            safe_exit: Если True - выполняет безопасный выход (merge позиций)
        """
        self.running = False
        
        logger.info("🛑 Остановка бота...")
        
        if safe_exit and self.split_phase > 0:
            # Безопасный выход из SPLIT
            self.exit_split_safe()
        else:
            # Просто отменяем ордера
            cancelled = self.cancel_all_orders()
            logger.info(f"   Отменено ордеров: {cancelled}")
        
        # Выводим статистику поинтов
        self.points.print_stats()
        
        logger.info("👋 Бот остановлен")


# =============================================================================
# ПОИСК РЫНКОВ
# =============================================================================

# Категории рынков для удобной классификации
MARKET_CATEGORIES = {
    'crypto': {
        'name': '🪙 КРИПТО',
        'keywords': ['btc', 'eth', 'bnb', 'sol', 'xrp', 'bitcoin', 'ethereum', 'crypto', '/usd'],
        'description': 'Криптовалюты и крипто-рынки'
    },
    'politics': {
        'name': '🏛️ ПОЛИТИКА',
        'keywords': ['trump', 'biden', 'election', 'president', 'congress', 'senate', 'government', 'shutdown', 'tariff', 'greenland', 'supreme court'],
        'description': 'Политические события'
    },
    'finance': {
        'name': '💰 ФИНАНСЫ',
        'keywords': ['fed', 'rate', 'nvidia', 'tesla', 'apple', 'stock', 'market cap', 'gold', 'silver', 'aapl', 'nvda', 'tsla'],
        'description': 'Фондовый рынок и экономика'
    },
    'sports': {
        'name': '⚽ СПОРТ',
        'keywords': ['ufc', 'nfl', 'nba', 'nhl', 'super bowl', 'championship', 'playoffs', 'game', 'match'],
        'description': 'Спортивные события'
    },
    'entertainment': {
        'name': '🎬 РАЗВЛЕЧЕНИЯ',
        'keywords': ['oscars', 'academy', 'movie', 'film', 'gta', 'game awards', 'album', 'song', 'grammy'],
        'description': 'Кино, игры, музыка'
    },
    'tech': {
        'name': '💻 ТЕХНОЛОГИИ',
        'keywords': ['ai', 'openai', 'chatgpt', 'google', 'meta', 'microsoft', 'spacex', 'launch', 'token', 'metamask'],
        'description': 'Технологии и стартапы'
    },
    'world': {
        'name': '🌍 МИР',
        'keywords': ['iran', 'russia', 'china', 'ukraine', 'war', 'peace', 'leader', 'khamenei'],
        'description': 'Мировые события'
    },
    'other': {
        'name': '📦 ДРУГОЕ',
        'keywords': [],
        'description': 'Прочие рынки'
    }
}


def classify_market(title: str) -> str:
    """
    Определить категорию рынка по названию
    
    Args:
        title: Название рынка
        
    Returns:
        Код категории
    """
    title_lower = title.lower()
    
    for cat_code, cat_info in MARKET_CATEGORIES.items():
        if cat_code == 'other':
            continue
        for keyword in cat_info['keywords']:
            if keyword in title_lower:
                return cat_code
    
    return 'other'


def format_volume(volume: float) -> str:
    """Форматировать объём в читаемый вид"""
    if volume >= 1_000_000:
        return f"${volume/1_000_000:.1f}M"
    elif volume >= 1_000:
        return f"${volume/1_000:.0f}K"
    elif volume > 0:
        return f"${volume:.0f}"
    else:
        return "-"


def find_binary_markets(with_stats: bool = True) -> List[Market]:
    """
    Найти бинарные рынки с категориями и статистикой
    
    Args:
        with_stats: Загружать статистику (объём) для каждого рынка
    
    Returns:
        Список бинарных рынков
    """
    api = PredictAPI()
    
    print("\n" + "="*85)
    print("🔍 ПОИСК РЫНКОВ ДЛЯ SPLIT СТРАТЕГИИ")
    print("="*85)
    
    print("⏳ Загрузка рынков...")
    markets = api.get_binary_markets(max_markets=50)
    
    if not markets:
        print("❌ Рынки не найдены")
        return []
    
    # Загружаем статистику
    market_stats = {}
    if with_stats:
        print("⏳ Загрузка статистики...")
        for m in markets:
            stats = api.get_market_stats(m.id)
            market_stats[m.id] = stats
    
    # Классифицируем рынки
    categorized = {}
    for m in markets:
        cat = classify_market(m.title)
        if cat not in categorized:
            categorized[cat] = []
        categorized[cat].append(m)
    
    print(f"✅ Найдено {len(markets)} рынков в {len(categorized)} категориях\n")
    
    # Выводим по категориям
    market_index = 1
    market_map = {}  # индекс -> market
    
    for cat_code in ['crypto', 'finance', 'politics', 'tech', 'entertainment', 'sports', 'world', 'other']:
        if cat_code not in categorized:
            continue
        
        cat_info = MARKET_CATEGORIES[cat_code]
        cat_markets = categorized[cat_code]
        
        # Сортируем по объёму (по убыванию)
        if with_stats:
            cat_markets.sort(key=lambda x: market_stats.get(x.id, {}).get('volume_total', 0), reverse=True)
        
        print(f"\n{cat_info['name']} ({len(cat_markets)})")
        print("-"*85)
        print(f"{'#':<4} {'ID':<6} {'Название':<45} {'Объём':>12} {'Ликв.':>10}")
        print("-"*85)
        
        for m in cat_markets:
            title = m.title[:43] + ".." if len(m.title) > 45 else m.title
            
            if with_stats and m.id in market_stats:
                stats = market_stats[m.id]
                vol = format_volume(stats['volume_total'])
                liq = format_volume(stats['liquidity'])
            else:
                vol = "-"
                liq = "-"
            
            print(f"{market_index:<4} {m.id:<6} {title:<45} {vol:>12} {liq:>10}")
            market_map[market_index] = m
            market_index += 1
    
    print("-"*85)
    print(f"💡 Рекомендация: выбирайте рынки с высоким объёмом для лучших поинтов")
    
    # Сохраняем map для select_market_interactive
    find_binary_markets._market_map = market_map
    find_binary_markets._markets = markets
    
    return markets


def select_market_interactive() -> Optional[int]:
    """
    Интерактивный выбор рынка
    
    Returns:
        ID выбранного рынка или None
    """
    markets = find_binary_markets(with_stats=True)
    
    if not markets:
        print("❌ Рынки не найдены")
        return None
    
    # Получаем map из find_binary_markets
    market_map = getattr(find_binary_markets, '_market_map', {})
    
    # Выбор
    while True:
        try:
            choice = input(f"\n📌 Введите номер (1-{len(markets)}) или ID рынка, 'q' для выхода: ").strip()
            
            if choice.lower() == 'q':
                return None
            
            num = int(choice)
            
            # Номер в списке
            if num in market_map:
                selected = market_map[num]
                print(f"\n✅ Выбран рынок #{selected.id}: {selected.title[:50]}...")
                return selected.id
            
            # Может быть ID рынка
            for m in markets:
                if m.id == num:
                    print(f"\n✅ Выбран рынок #{m.id}: {m.title[:50]}...")
                    return m.id
            
            print("❌ Неверный выбор")
            
        except ValueError:
            print("❌ Введите число")
        except KeyboardInterrupt:
            return None


def input_with_timeout(prompt: str, timeout: float = 5.0) -> Optional[str]:
    """
    Запрос ввода с таймаутом (для Windows)
    
    Args:
        prompt: Текст запроса
        timeout: Таймаут в секундах
        
    Returns:
        Введённая строка или None при таймауте
    """
    import sys
    import msvcrt
    
    print(prompt, end='', flush=True)
    
    start_time = time.time()
    input_chars = []
    
    while True:
        # Проверяем таймаут
        if time.time() - start_time > timeout:
            print()  # Новая строка
            return None
        
        # Проверяем, есть ли ввод
        if msvcrt.kbhit():
            char = msvcrt.getwch()
            
            # Enter
            if char in ('\r', '\n'):
                print()
                return ''.join(input_chars)
            
            # Backspace
            elif char == '\b':
                if input_chars:
                    input_chars.pop()
                    print('\b \b', end='', flush=True)
            
            # Обычный символ
            else:
                input_chars.append(char)
                print(char, end='', flush=True)
        
        # Небольшая пауза чтобы не грузить CPU
        time.sleep(0.05)


# =============================================================================
# ТОЧКА ВХОДА
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Predict.fun Trading Bot - SPLIT Strategy Points Farmer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:

  # Стандартный запуск (Монитор + SPLIT по запросу)
  python predict_trader.py

  # Конкретный рынок для SPLIT
  python predict_trader.py --market 123

  # С кастомным размером ордера
  python predict_trader.py --amount 20

  # Изменить offset для SPLIT
  python predict_trader.py --offset 0.02

  # Слить позиции (merge YES+NO -> USDT)
  python predict_trader.py --market 123 --merge

  # Выйти из SPLIT (отменить ордера + merge)
  python predict_trader.py --market 123 --exit

РЕЖИМ РАБОТЫ:
=============
  По умолчанию бот работает в режиме МОНИТОР:
  - Автоматически находит все рынки с позициями/ордерами
  - Проверяет позицию ордеров и переставляет на 2-е место
  
  После каждого цикла мониторинга спрашивает:
  "Запустить SPLIT на новом рынке? [1] - Да (5 сек)"
  
  Если нажать 1 - откроется выбор рынка для SPLIT
  Если подождать 5 сек - продолжит мониторинг

СТРАТЕГИЯ SPLIT:
================
  1. ФАЗА 1: Покупаем YES и NO токены одновременно
  2. ФАЗА 2: Выставляем SELL на +offset от лучшего ASK
  3. Ордера стоят в книге, фармят поинты
  4. При любом исходе выходим в 0 или плюс

СИСТЕМА ПОИНТОВ:
================
  - Лимитные ордера ближе к рынку = больше PP
  - Лучший bid/ask = максимум PP
  - Maker Fee = 0% (бесплатно!)
  - Награды каждые 7 дней

УПРАВЛЕНИЕ АККАУНТАМИ:
======================
  python predict_trader.py --add-account      # Добавить новый аккаунт
  python predict_trader.py --remove-account   # Удалить аккаунт
  python predict_trader.py --list-accounts    # Показать список аккаунтов
  python predict_trader.py --account 1        # Запустить с конкретным аккаунтом
        """
    )
    
    # Аргументы для управления аккаунтами
    parser.add_argument(
        '--add-account',
        action='store_true',
        help='Интерактивно добавить новый аккаунт'
    )
    
    parser.add_argument(
        '--remove-account',
        action='store_true',
        help='Удалить аккаунт'
    )
    
    parser.add_argument(
        '--list-accounts',
        action='store_true',
        help='Показать список аккаунтов'
    )
    
    parser.add_argument(
        '--account',
        type=int,
        default=None,
        help='Номер аккаунта для запуска (1, 2, ...)'
    )
    
    parser.add_argument(
        '--market', '-m',
        type=int,
        required=False,
        default=None,
        help='ID рынка для SPLIT (пропустить выбор)'
    )
    
    parser.add_argument(
        '--strategy', '-s',
        type=str,
        choices=['split'],
        default='split',
        help='Стратегия торговли (по умолчанию: split)'
    )
    
    parser.add_argument(
        '--amount', '-a',
        type=float,
        required=False,  # Обязательно для SPLIT, не нужно для мониторинга
        help='Размер ордера в USDT (обязательно для SPLIT)'
    )
    
    parser.add_argument(
        '--offset', '-o',
        type=float,
        default=SPLIT_OFFSET,
        help=f'Offset от лучшего ASK для SPLIT (по умолчанию: {SPLIT_OFFSET})'
    )
    
    parser.add_argument(
        '--merge',
        action='store_true',
        help='Слить YES+NO позиции в USDT (не запускает бота)'
    )
    
    parser.add_argument(
        '--exit',
        action='store_true',
        help='Безопасный выход: отменить ордера и слить позиции'
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Включить debug логирование'
    )
    
    args = parser.parse_args()
    
    # Настройка логирования
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Заголовок
    print("""
    ╔════════════════════════════════════════════════════════════╗
    ║     Predict.fun Trading Bot                                ║
    ║     SPLIT Strategy - Points Farmer                         ║
    ║     https://predict.fun                                    ║
    ╚════════════════════════════════════════════════════════════╝
    """)
    
    # =========================================================================
    # УПРАВЛЕНИЕ АККАУНТАМИ (интерактивное меню при запуске)
    # =========================================================================
    
    # Загружаем аккаунты
    accounts = load_accounts()
    
    # Показываем текущие аккаунты
    print("=" * 60)
    print(f"📋 АККАУНТЫ ({len(accounts)})")
    print("=" * 60)
    
    if accounts:
        for i, acc in enumerate(accounts):
            print(f"   [{i+1}] {acc}")
    else:
        print("   📭 Нет настроенных аккаунтов")
    
    print("-" * 60)
    print("   [+] Добавить новый аккаунт")
    if accounts:
        print("   [-] Удалить аккаунт")
        print("   [Enter] Продолжить с выбором аккаунта")
    print("=" * 60)
    
    # Спрашиваем действие
    action = input("\n   Ваш выбор: ").strip().lower()
    
    if action == '+':
        new_account = add_account_interactive()
        if new_account:
            accounts = load_accounts()  # Перезагружаем список
        print()  # Пустая строка
        
    elif action == '-' and accounts:
        remove_account_interactive()
        accounts = load_accounts()  # Перезагружаем список
        if not accounts:
            print("\n⚠️  Нет аккаунтов! Добавьте хотя бы один.")
            return
        print()
    
    # Выбор аккаунта для работы
    selected_account = None
    
    if args.account:
        # Указан конкретный аккаунт через аргумент
        if 1 <= args.account <= len(accounts):
            selected_account = accounts[args.account - 1]
            print(f"📌 Выбран аккаунт: {selected_account}")
        else:
            print(f"❌ Аккаунт #{args.account} не найден!")
            print(f"   Доступно аккаунтов: {len(accounts)}")
            return
    elif len(accounts) == 0:
        print("\n❌ Нет аккаунтов! Перезапустите и добавьте аккаунт.")
        return
    elif len(accounts) == 1:
        selected_account = accounts[0]
        print(f"\n📌 Используется аккаунт: {selected_account}")
    else:
        # Несколько аккаунтов - спрашиваем какой использовать
        print("\n📌 Выберите аккаунт для работы:")
        for i, acc in enumerate(accounts):
            print(f"   [{i+1}] {acc}")
        
        try:
            choice = input("\n   Номер аккаунта: ").strip()
            if choice and int(choice) >= 1 and int(choice) <= len(accounts):
                selected_account = accounts[int(choice) - 1]
                print(f"\n✅ Выбран: {selected_account}")
            else:
                print("⚠️  Используется первый аккаунт")
                selected_account = accounts[0]
        except (ValueError, IndexError):
            print("⚠️  Используется первый аккаунт")
            selected_account = accounts[0]
    
    # =========================================================================
    # РЕЖИМ MERGE/EXIT (специальные команды)
    # =========================================================================
    if args.merge or args.exit:
        market_id = args.market
        if market_id is None:
            market_id = select_market_interactive()
            if market_id is None:
                print("\n👋 Выход")
                return
        
        try:
            trader = PredictTrader(
                market_id=market_id,
                strategy=args.strategy,
                order_amount=args.amount,
                split_offset=args.offset,
                account=selected_account,
            )
            
            if not trader.init_sdk():
                return
            if not trader.authenticate():
                return
            if not trader.load_market(market_id):
                return
            
            if args.merge:
                print("\n🔄 Режим MERGE: слияние позиций...")
                trader.merge_positions()
            elif args.exit:
                print("\n🚪 Режим EXIT: безопасный выход...")
                trader.exit_split_safe()
                
        except Exception as e:
            logger.exception(f"Ошибка: {e}")
        return
    
    # =========================================================================
    # ОСНОВНОЙ РЕЖИМ: МОНИТОР + SPLIT ПО ЗАПРОСУ
    # =========================================================================
    print("\n🚀 Запуск бота в режиме МОНИТОР + SPLIT...")
    print("   - Автопоиск рынков с открытыми ордерами/позициями")
    print("   - После каждого цикла можно запустить SPLIT на новый рынок")
    print("   - Нажмите Ctrl+C для выхода\n")
    
    # Создаём трейдера в режиме мониторинга
    try:
        trader = PredictTrader(
            market_ids=None,  # Авто-поиск
            strategy=args.strategy,
            order_amount=args.amount,
            split_offset=args.offset,
            monitor_mode=True,
            account=selected_account,  # Выбранный аккаунт
        )
        
        print_config()
        
        if not validate_config():
            logger.error("Ошибка конфигурации!")
            return
        
        if not trader.init_sdk():
            return
        
        if not trader.authenticate():
            return
        
        if not trader.set_approvals():
            return
        
        cycle_count = 0
        
        while True:
            try:
                # =============================================================
                # ФАЗА 1: МОНИТОРИНГ СУЩЕСТВУЮЩИХ РЫНКОВ
                # =============================================================
                cycle_count += 1
                
                # Загружаем/обновляем рынки
                if cycle_count == 1 or cycle_count % 10 == 0:  # Полная перезагрузка каждые 10 циклов
                    trader.load_all_markets()
                
                if trader.markets:
                    print(f"\n{'='*60}")
                    print(f"🔄 ЦИКЛ МОНИТОРИНГА #{cycle_count}")
                    print(f"   Активных рынков: {len(trader.markets)}")
                    print(f"{'='*60}")
                    
                    # Проверяем и переставляем ордера
                    result = trader.monitor_cycle()
                    
                    if result.get('success'):
                        logger.info(f"✅ {result.get('message', 'OK')}")
                    else:
                        logger.warning(f"⚠️  {result.get('message', 'Unknown')}")
                else:
                    print(f"\n{'='*60}")
                    print(f"📭 Нет активных рынков с ордерами")
                    print(f"{'='*60}")
                
                # =============================================================
                # ФАЗА 2: ЗАПРОС НА SPLIT
                # =============================================================
                print(f"\n{'─'*60}")
                print("💡 Запустить SPLIT на новом рынке? [1] - Да")
                
                user_input = input_with_timeout("   Ваш выбор (5 сек): ", timeout=5.0)
                
                if user_input and user_input.strip() == '1':
                    # Пользователь хочет SPLIT
                    print("\n" + "="*60)
                    print("🎯 РЕЖИМ SPLIT: Выбор рынка")
                    print("="*60)
                    
                    # Выбираем рынок
                    market_id = args.market if args.market else select_market_interactive()
                    
                    if market_id:
                        # Выполняем SPLIT
                        print(f"\n🔄 Запуск SPLIT на рынке #{market_id}...")
                        
                        # Временно переключаемся на один рынок
                        trader.market_id = market_id
                        
                        if trader.load_market(market_id):
                            # Проверяем баланс
                            balance = trader.get_usdt_balance()
                            logger.info(f"💰 Баланс: ${balance:.2f} USDT")
                            
                            if balance >= trader.order_amount:
                                # Выполняем один цикл SPLIT
                                success = trader.run_split_cycle()
                                
                                if success:
                                    print(f"\n✅ SPLIT завершён! Рынок #{market_id} добавлен в мониторинг.")
                                    
                                    # Добавляем в список мониторинга если ещё нет
                                    if market_id not in trader.markets:
                                        trader.load_market_state(market_id)
                                else:
                                    print(f"\n⚠️  SPLIT не завершён полностью")
                            else:
                                print(f"\n❌ Недостаточно баланса: ${balance:.2f} < ${trader.order_amount}")
                        else:
                            print(f"\n❌ Не удалось загрузить рынок #{market_id}")
                    else:
                        print("\n↩️  Отмена выбора рынка")
                    
                    print(f"\n{'='*60}")
                    print("🔙 Возврат в режим МОНИТОРИНГА...")
                    print(f"{'='*60}")
                    
                else:
                    # Таймаут - продолжаем мониторинг
                    logger.info(f"⏳ Следующий цикл через {CHECK_INTERVAL} сек...")
                    time.sleep(CHECK_INTERVAL)
                    
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"Ошибка в цикле: {e}")
                time.sleep(5)
                
    except KeyboardInterrupt:
        print("\n\n👋 Получен сигнал остановки...")
        print("   Ордера остаются активными, позиции сохранены.")
        print("   Используйте --exit для безопасного выхода.")
    except Exception as e:
        logger.exception(f"Критическая ошибка: {e}")


if __name__ == "__main__":
    main()
