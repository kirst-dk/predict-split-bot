# -*- coding: utf-8 -*-
"""
Predict.fun REST API Client
===========================

Клиент для работы с REST API Predict.fun
Документация: https://dev.predict.fun/
"""

import logging
import time
import requests
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_DOWN

from config import (
    API_KEY, BASE_URL, CHAIN_ID, ChainId,
    RATE_LIMIT, MIN_ORDER_USDT
)

logger = logging.getLogger(__name__)


# =============================================================================
# СТРУКТУРЫ ДАННЫХ
# =============================================================================

@dataclass
class Outcome:
    """Исход рынка (YES или NO)"""
    name: str
    index_set: int  # 1 = YES, 2 = NO
    on_chain_id: str
    status: str = "PENDING"  # WON, LOST, PENDING
    
    @property
    def is_yes(self) -> bool:
        return self.index_set == 1 or self.name.upper() == "YES"
    
    @property
    def is_no(self) -> bool:
        return self.index_set == 2 or self.name.upper() == "NO"


@dataclass
class Market:
    """Информация о рынке"""
    id: int
    title: str
    question: str
    status: str  # REGISTERED, OPEN, RESOLVED
    is_neg_risk: bool  # Winner-takes-all рынки
    is_yield_bearing: bool
    fee_rate_bps: int  # Комиссия в basis points
    condition_id: str
    outcomes: List[Outcome]
    spread_threshold: float = 0.0  # Порог спреда для поинтов
    share_threshold: float = 0.0  # Минимум акций для поинтов
    decimal_precision: int = 2  # Точность цены (2 = $0.01)
    category_slug: str = ""
    image_url: str = ""
    description: str = ""
    # Тип рынка: DEFAULT, SPORTS_MATCH, CRYPTO_UP_DOWN, TWEET_COUNT, SPORTS_TEAM_MATCH
    market_variant: str = "DEFAULT"
    
    @property
    def is_binary(self) -> bool:
        """Проверить, что это бинарный рынок (ровно 2 исхода)"""
        return len(self.outcomes) == 2
    
    @property
    def is_good_for_split(self) -> bool:
        """
        Проверить, что рынок подходит для SPLIT стратегии.
        
        ПОДХОДЯТ:
        - Рынки с вопросом (?) в названии
        - Рынки начинающиеся с "Will", "Is", "Does", "Can", "Has", "Are", "Do"
        - CRYPTO_UP_DOWN (BTC/BNB Up or Down) - отличные рынки!
        
        НЕ ПОДХОДЯТ:
        - SPORTS_MATCH - суб-рынки спортивных матчей
        - SPORTS_TEAM_MATCH - матчи команда vs команда  
        - TWEET_COUNT - количество твитов
        - NegRisk рынки - winner-takes-all, не работает SPLIT
        - Суб-рынки мульти-исходных событий:
          * Имена: "Paul Thomas Anderson", "Vegas Golden Knights"
          * Суммы: ">$6B", "$700M"
          * Даты: "September 30, 2026?"
          * Проценты: "50 bps decrease"
        """
        # NegRisk рынки НЕ подходят для SPLIT
        if self.is_neg_risk:
            return False
        
        # Должно быть 2 исхода
        if len(self.outcomes) != 2:
            return False
        
        # CRYPTO_UP_DOWN - всегда хорошие рынки
        if self.market_variant == 'CRYPTO_UP_DOWN':
            return True
        
        # SPORTS_MATCH и SPORTS_TEAM_MATCH - исключаем
        if self.market_variant in ('SPORTS_MATCH', 'SPORTS_TEAM_MATCH'):
            return False
        
        # TWEET_COUNT - исключаем
        if self.market_variant == 'TWEET_COUNT':
            return False
        
        # DEFAULT рынки - проверяем дополнительно
        names = [o.name.upper() for o in self.outcomes]
        
        # Исходы должны быть YES/NO или UP/DOWN
        valid_pairs = [{'YES', 'NO'}, {'UP', 'DOWN'}]
        if set(names) not in valid_pairs:
            return False
        
        # Исключаем суб-рынки (короткие названия - это части multi-outcome рынков)
        # Признаки суб-рынков: "Carlos Alcaraz", "Vegas Golden Knights", "$700M", "June 30, 2026"
        title = self.title
        title_lower = title.lower()
        
        # ГЛАВНОЕ ПРАВИЛО: полноценный рынок должен содержать вопрос (?)
        # или начинаться с вопросительных слов
        question_starters = ['will ', 'is ', 'does ', 'can ', 'has ', 'are ', 'do ']
        has_question_mark = '?' in title
        starts_with_question = any(title_lower.startswith(q) for q in question_starters)
        
        # CRYPTO_UP_DOWN рынки имеют особый формат "BTC/USD Up or Down on Feb 1"
        is_crypto_format = '/usd' in title_lower and ('up or down' in title_lower or 'up/down' in title_lower)
        
        # Если это не вопрос и не крипто-формат - это суб-рынок
        if not has_question_mark and not starts_with_question and not is_crypto_format:
            return False
        
        # Дополнительные проверки для коротких названий
        if len(title) < 30:
            # Если начинается с $ или > - денежная сумма/порог
            if title.startswith('$') or title.startswith('>'):
                return False
            
            # Если содержит "bps" - процентная ставка
            if 'bps' in title_lower:
                return False
            
            # Даты без полноценного вопроса - суб-рынки
            months = ['january', 'february', 'march', 'april', 'may', 'june',
                     'july', 'august', 'september', 'october', 'november', 'december']
            if any(m in title_lower for m in months) and not starts_with_question:
                return False
        
        return True
    
    @property
    def is_true_yes_no(self) -> bool:
        """
        Проверить, что это настоящий YES/NO рынок.
        DEPRECATED: используйте is_good_for_split для SPLIT стратегии
        """
        return self.is_good_for_split
    
    @property
    def yes_outcome(self) -> Optional[Outcome]:
        """Получить YES исход"""
        for o in self.outcomes:
            if o.is_yes:
                return o
        return self.outcomes[0] if self.outcomes else None
    
    @property
    def no_outcome(self) -> Optional[Outcome]:
        """Получить NO исход"""
        for o in self.outcomes:
            if o.is_no:
                return o
        return self.outcomes[1] if len(self.outcomes) > 1 else None


@dataclass
class OrderbookLevel:
    """Уровень в ордербуке [price, quantity]"""
    price: float
    quantity: float


@dataclass
class Orderbook:
    """Ордербук рынка"""
    market_id: int
    update_timestamp_ms: int
    asks: List[OrderbookLevel]  # Продажи YES (лучший ASK первый)
    bids: List[OrderbookLevel]  # Покупки YES (лучший BID первый)
    
    @property
    def best_ask(self) -> Optional[float]:
        """Лучшая цена продажи YES"""
        return self.asks[0].price if self.asks else None
    
    @property
    def best_bid(self) -> Optional[float]:
        """Лучшая цена покупки YES"""
        return self.bids[0].price if self.bids else None
    
    @property
    def spread(self) -> Optional[float]:
        """Спред между лучшим bid и ask"""
        if self.best_ask and self.best_bid:
            return self.best_ask - self.best_bid
        return None
    
    def get_no_prices(self, decimal_precision: int = 2) -> Tuple[Optional[float], Optional[float]]:
        """
        Получить цены для NO исхода (complement от YES).
        
        Returns:
            (no_buy_price, no_sell_price)
            no_buy_price = цена покупки NO = 1 - yes_bid
            no_sell_price = цена продажи NO = 1 - yes_ask
        """
        factor = 10 ** decimal_precision
        
        no_buy = None
        no_sell = None
        
        if self.best_bid:
            # Покупка NO = продажа YES = complement от лучшего YES bid
            no_buy = (factor - round(self.best_bid * factor)) / factor
        
        if self.best_ask:
            # Продажа NO = покупка YES = complement от лучшего YES ask
            no_sell = (factor - round(self.best_ask * factor)) / factor
        
        return no_buy, no_sell
    
    def get_no_asks(self, decimal_precision: int = 3) -> List['OrderbookLevel']:
        """
        Получить полный NO ASK ордербук (отсортированный по цене, лучший первый).
        
        NO ASK = complement от YES BID
        Согласно документации: noAsks = yesBids.map(([p, q]) => [getComplement(p), q])
        
        Returns:
            Список OrderbookLevel для NO SELL ордеров, отсортированный по возрастанию цены
        """
        factor = 10 ** decimal_precision
        no_asks = []
        
        for level in self.bids:  # YES bids → NO asks
            no_price = (factor - round(level.price * factor)) / factor
            no_asks.append(OrderbookLevel(price=no_price, quantity=level.quantity))
        
        # Сортируем по цене (лучший/низший ASK первый)
        no_asks.sort(key=lambda x: x.price)
        
        return no_asks
    
    def get_best_no_ask(self, decimal_precision: int = 3) -> Optional[float]:
        """
        Получить лучшую (самую низкую) цену NO ASK.
        
        Returns:
            Лучшая цена продажи NO или None
        """
        no_asks = self.get_no_asks(decimal_precision)
        return no_asks[0].price if no_asks else None
    
    def get_no_bids(self, decimal_precision: int = 3) -> List['OrderbookLevel']:
        """
        Получить полный NO BID ордербук (отсортированный по цене, лучший первый).
        
        NO BID = complement от YES ASK
        Согласно документации: noBids = yesAsks.map(([p, q]) => [getComplement(p), q])
        
        Returns:
            Список OrderbookLevel для NO BUY ордеров, отсортированный по убыванию цены
        """
        factor = 10 ** decimal_precision
        no_bids = []
        
        for level in self.asks:  # YES asks → NO bids
            no_price = (factor - round(level.price * factor)) / factor
            no_bids.append(OrderbookLevel(price=no_price, quantity=level.quantity))
        
        # Сортируем по цене (лучший/высший BID первый)
        no_bids.sort(key=lambda x: x.price, reverse=True)
        
        return no_bids
    
    def get_best_no_bid(self, decimal_precision: int = 3) -> Optional[float]:
        """
        Получить лучшую (самую высокую) цену NO BID.
        
        Returns:
            Лучшая цена покупки NO или None
        """
        no_bids = self.get_no_bids(decimal_precision)
        return no_bids[0].price if no_bids else None
    
    def get_yes_asks(self) -> List['OrderbookLevel']:
        """
        Получить полный YES ASK ордербук.
        
        YES ASK приходит напрямую из API (self.asks).
        
        Returns:
            Список OrderbookLevel для YES SELL ордеров, отсортированный по возрастанию цены
        """
        return self.asks
    
    def get_best_yes_ask(self) -> Optional[float]:
        """
        Получить лучшую (самую низкую) цену YES ASK.
        Эквивалентно self.best_ask, но для консистентности API.
        
        Returns:
            Лучшая цена продажи YES или None
        """
        return self.best_ask
    
    def get_yes_bids(self) -> List['OrderbookLevel']:
        """
        Получить полный YES BID ордербук.
        
        YES BID приходит напрямую из API (self.bids).
        
        Returns:
            Список OrderbookLevel для YES BUY ордеров, отсортированный по убыванию цены
        """
        return self.bids
    
    def get_best_yes_bid(self) -> Optional[float]:
        """
        Получить лучшую (самую высокую) цену YES BID.
        Эквивалентно self.best_bid, но для консистентности API.
        
        Returns:
            Лучшая цена покупки YES или None
        """
        return self.best_bid


@dataclass
class Order:
    """Ордер"""
    id: str  # ID ордера (используется для отмены)
    order_hash: str
    market_id: int
    side: int  # 0 = BUY, 1 = SELL
    token_id: str
    maker_amount: str
    taker_amount: str
    price_per_share: float
    status: str  # OPEN, FILLED, CANCELLED, EXPIRED
    amount_filled: str = "0"
    amount: str = "0"  # Общий объём ордера (из API wrapper)
    is_neg_risk: bool = False
    is_yield_bearing: bool = False
    strategy: str = "LIMIT"
    reward_earning_rate: float = 0.0  # Скорость заработка поинтов
    
    @property
    def original_quantity(self) -> float:
        """Исходное количество токенов (для SELL = makerAmount в токенах)"""
        try:
            if self.side == 1:  # SELL: makerAmount = токены
                return int(self.maker_amount) / 10**18
            else:  # BUY: takerAmount = токены
                return int(self.taker_amount) / 10**18
        except (ValueError, ZeroDivisionError):
            return 0.0
    
    @property
    def filled_quantity(self) -> float:
        """Количество уже исполненных токенов"""
        try:
            amt_filled = int(self.amount_filled)
            if amt_filled == 0:
                return 0.0
            # amount_filled из API — это сумма в wei (USDT или токены)
            # Для SELL: filled в USDT, поэтому пересчитываем в токены через цену
            if self.side == 1 and self.price_per_share > 0:  # SELL
                return (amt_filled / 10**18) / self.price_per_share
            else:  # BUY
                return amt_filled / 10**18
        except (ValueError, ZeroDivisionError):
            return 0.0
    
    @property
    def quantity(self) -> float:
        """Оставшееся количество токенов (original - filled)"""
        remaining = self.original_quantity - self.filled_quantity
        return max(0.0, remaining)
    
    @property
    def is_partially_filled(self) -> bool:
        """Ордер частично исполнен?"""
        try:
            return int(self.amount_filled) > 0 and self.status == "OPEN"
        except ValueError:
            return False


@dataclass
class Position:
    """Позиция пользователя"""
    id: str
    market: Market
    outcome: Outcome
    amount: str  # Количество акций (в wei)
    value_usd: str  # Стоимость в USDT


# =============================================================================
# API CLIENT
# =============================================================================

class PredictAPI:
    """
    REST API клиент для Predict.fun
    
    Документация: https://dev.predict.fun/
    """
    
    def __init__(self, api_key: str = None, jwt_token: str = None):
        """
        Args:
            api_key: API ключ (обязателен для Mainnet)
            jwt_token: JWT токен для аутентифицированных запросов
        """
        self.api_key = api_key or API_KEY
        self.jwt_token = jwt_token
        self.base_url = BASE_URL
        
        # Rate limiting
        self._request_times: List[float] = []
        self._rate_limit = RATE_LIMIT
        
        # Session для переиспользования соединений
        self.session = requests.Session()
        self._update_headers()
    
    def _update_headers(self):
        """Обновить заголовки сессии"""
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        
        if self.api_key and CHAIN_ID == ChainId.BNB_MAINNET:
            headers['x-api-key'] = self.api_key
        
        if self.jwt_token:
            headers['Authorization'] = f'Bearer {self.jwt_token}'
        
        self.session.headers.update(headers)
    
    def set_jwt_token(self, token: str):
        """Установить JWT токен для аутентификации"""
        self.jwt_token = token
        self._update_headers()
    
    def _check_rate_limit(self):
        """Проверить и соблюдать rate limit"""
        now = time.time()
        minute_ago = now - 60
        
        # Удаляем старые запросы
        self._request_times = [t for t in self._request_times if t > minute_ago]
        
        if len(self._request_times) >= self._rate_limit:
            # Ждём до освобождения слота
            sleep_time = self._request_times[0] - minute_ago + 0.1
            logger.debug(f"Rate limit reached, sleeping {sleep_time:.2f}s")
            time.sleep(sleep_time)
            self._request_times = self._request_times[1:]
        
        self._request_times.append(now)
    
    def _request(
        self,
        method: str,
        endpoint: str,
        params: Dict = None,
        json_data: Dict = None,
        require_auth: bool = False
    ) -> Dict:
        """
        Выполнить HTTP запрос к API
        
        Args:
            method: HTTP метод (GET, POST)
            endpoint: Endpoint API (например, /v1/markets)
            params: Query параметры
            json_data: JSON body для POST
            require_auth: Требуется ли JWT токен
            
        Returns:
            Ответ API в виде словаря
        """
        if require_auth and not self.jwt_token:
            raise ValueError("JWT token required for this endpoint")
        
        self._check_rate_limit()
        
        url = f"{self.base_url}{endpoint}"
        
        try:
            if method.upper() == 'GET':
                response = self.session.get(url, params=params, timeout=10)
            elif method.upper() == 'POST':
                response = self.session.post(url, params=params, json=json_data, timeout=10)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            # Логируем ответ при ошибке и бросаем исключение с JSON
            if response.status_code >= 400:
                error_text = response.text
                logger.error(f"API Error {response.status_code}: {error_text}")
                # Бросаем исключение с полным текстом ответа (для парсинга в вызывающем коде)
                raise Exception(f"API Error {response.status_code}: {error_text}")
            
            data = response.json()
            
            if not data.get('success', False):
                error = data.get('error', data.get('message', 'Unknown error'))
                logger.error(f"API error: {error}")
                raise Exception(f"API error: {error}")
            
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            raise
    
    # =========================================================================
    # АУТЕНТИФИКАЦИЯ
    # =========================================================================
    
    def get_auth_message(self) -> str:
        """
        Получить сообщение для подписи (шаг 1 аутентификации)
        
        Returns:
            Сообщение для подписи
        """
        data = self._request('GET', '/v1/auth/message')
        return data['data']['message']
    
    def get_jwt_token(self, signer_address: str, message: str, signature: str) -> str:
        """
        Получить JWT токен (шаг 2 аутентификации)
        
        Args:
            signer_address: Адрес кошелька
            message: Сообщение (из get_auth_message)
            signature: Подпись сообщения
            
        Returns:
            JWT токен
        """
        json_data = {
            'signer': signer_address,
            'message': message,
            'signature': signature,
        }
        
        data = self._request('POST', '/v1/auth', json_data=json_data)
        token = data['data']['token']
        
        # Автоматически устанавливаем токен
        self.set_jwt_token(token)
        
        return token
    
    # =========================================================================
    # РЫНКИ
    # =========================================================================
    
    def get_markets(
        self,
        first: int = 50,
        after: str = None,
        status: str = None
    ) -> Tuple[List[Market], Optional[str]]:
        """
        Получить список рынков
        
        Args:
            first: Количество рынков (пагинация)
            after: Курсор для следующей страницы
            status: Фильтр по статусу: 'OPEN' или 'RESOLVED'
            
        Returns:
            (список рынков, курсор для следующей страницы)
        """
        params = {'first': first}
        if after:
            params['after'] = after
        if status:
            params['status'] = status
        
        data = self._request('GET', '/v1/markets', params=params)
        
        markets = []
        for m in data.get('data', []):
            outcomes = []
            for o in m.get('outcomes', []):
                outcomes.append(Outcome(
                    name=o.get('name', ''),
                    index_set=o.get('indexSet', 0),
                    on_chain_id=o.get('onChainId', ''),
                    status=o.get('status', 'PENDING'),
                ))
            
            markets.append(Market(
                id=m.get('id', 0),
                title=m.get('title', ''),
                question=m.get('question', ''),
                status=m.get('status', ''),
                is_neg_risk=m.get('isNegRisk', False),
                is_yield_bearing=m.get('isYieldBearing', True),
                fee_rate_bps=m.get('feeRateBps', 0),
                condition_id=m.get('conditionId', ''),
                outcomes=outcomes,
                spread_threshold=m.get('spreadThreshold', 0),
                share_threshold=m.get('shareThreshold', 0),
                decimal_precision=m.get('decimalPrecision', 2),
                category_slug=m.get('categorySlug', ''),
                image_url=m.get('imageUrl', ''),
                description=m.get('description', ''),
                market_variant=m.get('marketVariant', 'DEFAULT'),
            ))
        
        cursor = data.get('cursor')
        return markets, cursor
    
    def get_market_by_id(self, market_id: int) -> Market:
        """
        Получить информацию о конкретном рынке
        
        Args:
            market_id: ID рынка
            
        Returns:
            Market объект
        """
        data = self._request('GET', f'/v1/markets/{market_id}')
        m = data['data']
        
        outcomes = []
        for o in m.get('outcomes', []):
            outcomes.append(Outcome(
                name=o.get('name', ''),
                index_set=o.get('indexSet', 0),
                on_chain_id=o.get('onChainId', ''),
                status=o.get('status', 'PENDING'),
            ))
        
        return Market(
            id=m.get('id', 0),
            title=m.get('title', ''),
            question=m.get('question', ''),
            status=m.get('status', ''),
            is_neg_risk=m.get('isNegRisk', False),
            is_yield_bearing=m.get('isYieldBearing', True),
            fee_rate_bps=m.get('feeRateBps', 0),
            condition_id=m.get('conditionId', ''),
            outcomes=outcomes,
            spread_threshold=m.get('spreadThreshold', 0),
            share_threshold=m.get('shareThreshold', 0),
            decimal_precision=m.get('decimalPrecision', 2),
            category_slug=m.get('categorySlug', ''),
            image_url=m.get('imageUrl', ''),
            description=m.get('description', ''),
            market_variant=m.get('marketVariant', 'DEFAULT'),
        )
    
    def get_markets_for_split(
        self, 
        max_markets: int = 100,
    ) -> List[Market]:
        """
        Получить рынки подходящие для SPLIT стратегии
        
        Включает:
        - CRYPTO_UP_DOWN рынки (BTC/BNB Up or Down) - отличные!
        - DEFAULT рынки без NegRisk с вопросами (Will X happen?)
        
        Исключает:
        - SPORTS_MATCH, SPORTS_TEAM_MATCH - спортивные суб-рынки
        - TWEET_COUNT - твиты
        - NegRisk рынки - winner-takes-all
        - Суб-рынки мульти-исходных событий (Carlos Alcaraz, $700M)
        
        Args:
            max_markets: Максимальное количество рынков
            
        Returns:
            Список рынков подходящих для SPLIT
        """
        good_markets = []
        cursor = None
        
        while len(good_markets) < max_markets:
            # Запрашиваем только ОТКРЫТЫЕ рынки
            markets, cursor = self.get_markets(first=100, after=cursor, status='OPEN')
            
            for market in markets:
                # Проверяем что подходит для SPLIT
                if market.is_good_for_split:
                    good_markets.append(market)
                    if len(good_markets) >= max_markets:
                        break
            
            if not cursor:
                break
        
        return good_markets
    
    def get_market_stats(self, market_id: int) -> Dict:
        """
        Получить статистику рынка (объём, ликвидность)
        
        Args:
            market_id: ID рынка
            
        Returns:
            {'volume_total': float, 'volume_24h': float, 'liquidity': float}
        """
        try:
            data = self._request('GET', f'/v1/markets/{market_id}/stats')
            stats = data.get('data', {})
            return {
                'volume_total': stats.get('volumeTotalUsd', 0),
                'volume_24h': stats.get('volume24hUsd', 0),
                'liquidity': stats.get('totalLiquidityUsd', 0),
            }
        except Exception:
            return {'volume_total': 0, 'volume_24h': 0, 'liquidity': 0}
    
    def get_binary_markets(
        self, 
        max_markets: int = 100,
        include_neg_risk: bool = False
    ) -> List[Market]:
        """
        DEPRECATED: используйте get_markets_for_split() для SPLIT стратегии
        
        Получить бинарные YES/NO рынки (старый метод)
        """
        # Используем новый метод
        return self.get_markets_for_split(max_markets=max_markets)
    
    # =========================================================================
    # ОРДЕРБУК
    # =========================================================================
    
    def get_orderbook(self, market_id: int) -> Orderbook:
        """
        Получить ордербук для рынка
        
        ВАЖНО: Ордербук хранит цены для YES исхода!
        Для получения цен NO используйте метод get_no_prices()
        
        Args:
            market_id: ID рынка
            
        Returns:
            Orderbook объект
        """
        data = self._request('GET', f'/v1/markets/{market_id}/orderbook')
        ob = data['data']
        
        asks = []
        for level in ob.get('asks', []):
            if isinstance(level, list) and len(level) >= 2:
                asks.append(OrderbookLevel(
                    price=float(level[0]),
                    quantity=float(level[1])
                ))
        
        bids = []
        for level in ob.get('bids', []):
            if isinstance(level, list) and len(level) >= 2:
                bids.append(OrderbookLevel(
                    price=float(level[0]),
                    quantity=float(level[1])
                ))
        
        return Orderbook(
            market_id=ob.get('marketId', market_id),
            update_timestamp_ms=ob.get('updateTimestampMs', 0),
            asks=asks,
            bids=bids,
        )
    
    # =========================================================================
    # ОРДЕРА
    # =========================================================================
    
    def get_orders(
        self,
        first: int = 50,
        after: str = None,
        status: str = None
    ) -> Tuple[List[Order], Optional[str]]:
        """
        Получить список своих ордеров
        
        Args:
            first: Количество (пагинация)
            after: Курсор
            status: OPEN, FILLED
            
        Returns:
            (список ордеров, курсор)
        """
        params = {'first': first}
        if after:
            params['after'] = after
        if status:
            params['status'] = status
        
        data = self._request('GET', '/v1/orders', params=params, require_auth=True)
        
        orders = []
        for o in data.get('data', []):
            order_data = o.get('order', {})
            
            # Вычисляем price_per_share из maker/taker amounts
            maker_amount = int(order_data.get('makerAmount', '0'))
            taker_amount = int(order_data.get('takerAmount', '0'))
            side = order_data.get('side', 0)
            
            # makerAmount = то, что отдаём (токены для SELL, USDT для BUY)
            # takerAmount = то, что получаем (USDT для SELL, токены для BUY)
            # price = USDT / токены
            #
            # Для SELL: makerAmount = токены, takerAmount = USDT
            #   price = takerAmount / makerAmount
            # Для BUY: makerAmount = USDT, takerAmount = токены
            #   price = makerAmount / takerAmount
            if side == 1 and maker_amount > 0:  # SELL
                price_per_share = taker_amount / maker_amount
            elif side == 0 and taker_amount > 0:  # BUY
                price_per_share = maker_amount / taker_amount
            else:
                price_per_share = 0.0
            
            orders.append(Order(
                id=o.get('id', ''),  # ID ордера для отмены
                order_hash=order_data.get('hash', ''),
                market_id=o.get('marketId', 0),
                side=side,
                token_id=order_data.get('tokenId', ''),
                maker_amount=order_data.get('makerAmount', '0'),
                taker_amount=order_data.get('takerAmount', '0'),
                price_per_share=price_per_share,
                status=o.get('status', ''),
                amount_filled=o.get('amountFilled', '0'),
                amount=o.get('amount', '0'),
                is_neg_risk=o.get('isNegRisk', False),
                is_yield_bearing=o.get('isYieldBearing', True),
                strategy=o.get('strategy', 'LIMIT'),
                reward_earning_rate=o.get('rewardEarningRate', 0.0),
            ))
        
        cursor = data.get('cursor')
        return orders, cursor
    
    def get_open_orders(self) -> List[Order]:
        """Получить все открытые ордера"""
        orders = []
        cursor = None
        
        while True:
            batch, cursor = self.get_orders(first=50, after=cursor, status='OPEN')
            orders.extend(batch)
            
            if not cursor:
                break
        
        return orders
    
    def create_order(self, order_data: Dict) -> Dict:
        """
        Создать ордер
        
        Args:
            order_data: Данные ордера (см. SDK для построения)
            
        Returns:
            Ответ API с orderId и orderHash
        """
        data = self._request(
            'POST',
            '/v1/orders',
            json_data={'data': order_data},
            require_auth=True
        )
        return data['data']
    
    def remove_orders(self, order_ids: List[str]) -> Dict:
        """
        Удалить ордера из ордербука
        
        Args:
            order_ids: Список ID ордеров для удаления (не хешей!)
            
        Returns:
            Результат операции с полями removed и noop
        """
        data = self._request(
            'POST',
            '/v1/orders/remove',
            json_data={'data': {'ids': order_ids}},
            require_auth=True
        )
        return data
    
    # =========================================================================
    # ПОЗИЦИИ
    # =========================================================================
    
    def get_positions(
        self,
        first: int = 50,
        after: str = None
    ) -> Tuple[List[Position], Optional[str]]:
        """
        Получить свои позиции
        
        Args:
            first: Количество (пагинация)
            after: Курсор
            
        Returns:
            (список позиций, курсор)
        """
        params = {'first': first}
        if after:
            params['after'] = after
        
        data = self._request('GET', '/v1/positions', params=params, require_auth=True)
        
        positions = []
        for p in data.get('data', []):
            # Парсим маркет
            m = p.get('market', {})
            outcomes = []
            for o in m.get('outcomes', []):
                outcomes.append(Outcome(
                    name=o.get('name', ''),
                    index_set=o.get('indexSet', 0),
                    on_chain_id=o.get('onChainId', ''),
                    status=o.get('status', 'PENDING'),
                ))
            
            market = Market(
                id=m.get('id', 0),
                title=m.get('title', ''),
                question=m.get('question', ''),
                status=m.get('status', ''),
                is_neg_risk=m.get('isNegRisk', False),
                is_yield_bearing=m.get('isYieldBearing', True),
                fee_rate_bps=m.get('feeRateBps', 0),
                condition_id=m.get('conditionId', ''),
                outcomes=outcomes,
                spread_threshold=m.get('spreadThreshold', 0),
                share_threshold=m.get('shareThreshold', 0),
                decimal_precision=m.get('decimalPrecision', 2),
            )
            
            # Парсим исход позиции
            o = p.get('outcome', {})
            outcome = Outcome(
                name=o.get('name', ''),
                index_set=o.get('indexSet', 0),
                on_chain_id=o.get('onChainId', ''),
                status=o.get('status', 'PENDING'),
            )
            
            positions.append(Position(
                id=p.get('id', ''),
                market=market,
                outcome=outcome,
                amount=p.get('amount', '0'),
                value_usd=p.get('valueUsd', '0'),
            ))
        
        cursor = data.get('cursor')
        return positions, cursor
    
    def get_all_positions(self) -> List[Position]:
        """Получить все позиции"""
        positions = []
        cursor = None
        
        while True:
            batch, cursor = self.get_positions(first=50, after=cursor)
            positions.extend(batch)
            
            if not cursor:
                break
        
        return positions
    
    # =========================================================================
    # АККАУНТ
    # =========================================================================
    
    def get_account(self) -> Dict:
        """
        Получить информацию об аккаунте
        
        Returns:
            Данные аккаунта
        """
        data = self._request('GET', '/v1/account', require_auth=True)
        return data['data']


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def calculate_taker_fee(
    price: float,
    shares: float,
    fee_rate_bps: int = 200,
    has_discount: bool = False
) -> float:
    """
    Рассчитать комиссию Taker
    
    Fee = Base Fee % × min(Price, 1 - Price) × Shares
    
    Args:
        price: Цена за акцию
        shares: Количество акций
        fee_rate_bps: Базовая комиссия в basis points (200 = 2%)
        has_discount: Есть ли 10% скидка по рефералу
        
    Returns:
        Комиссия в USDT
    """
    base_fee_percent = fee_rate_bps / 10000
    min_price = min(price, 1 - price)
    fee = base_fee_percent * min_price * shares
    
    if has_discount:
        fee *= 0.9  # 10% скидка
    
    return fee


def get_complement_price(price: float, decimal_precision: int = 2) -> float:
    """
    Получить complement цену (для NO от YES и наоборот)
    
    NO_price = 1 - YES_price (с учётом точности)
    
    Args:
        price: Исходная цена
        decimal_precision: Точность (2 = 0.01)
        
    Returns:
        Complement цена
    """
    factor = 10 ** decimal_precision
    return (factor - round(price * factor)) / factor


def wei_to_float(wei_str: str) -> float:
    """Конвертировать wei строку в float (18 decimals)"""
    try:
        return int(wei_str) / 10**18
    except (ValueError, TypeError):
        return 0.0


def float_to_wei(amount: float, precision: int = 10**18) -> int:
    """
    Конвертировать float в wei с использованием Decimal для точности.
    
    Исправлено в v0.0.12 SDK - избегает IEEE 754 floating-point ошибок
    путём конвертации через строку и использования Decimal модуля.
    
    Args:
        amount: Значение для конвертации (например, 0.46 для цены)
        precision: Множитель точности (по умолчанию 10**18 для wei)
        
    Returns:
        Значение в wei как integer
        
    Example:
        >>> float_to_wei(0.46)
        460000000000000000  # Correct! (not 460000000000000001)
        >>> float_to_wei(0.421031)
        421031000000000000  # Correct! (not 421030999999999936)
    """
    d = Decimal(str(amount)) * Decimal(precision)
    return int(d.quantize(Decimal("1"), rounding=ROUND_DOWN))


# =============================================================================
# ТЕСТЫ
# =============================================================================

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    
    print("🔍 Тестирование Predict.fun API Client...")
    print(f"   Base URL: {BASE_URL}")
    print(f"   API Key: {'✅' if API_KEY else '❌'}")
    
    api = PredictAPI()
    
    try:
        # Тест получения рынков
        print("\n📊 Загрузка рынков...")
        markets, cursor = api.get_markets(first=10)
        print(f"   Загружено: {len(markets)} рынков")
        
        # Фильтруем бинарные
        binary = [m for m in markets if m.is_binary]
        print(f"   Бинарных: {len(binary)}")
        
        if binary:
            market = binary[0]
            print(f"\n📈 Пример рынка:")
            print(f"   ID: {market.id}")
            print(f"   Название: {market.title[:50]}...")
            print(f"   Статус: {market.status}")
            print(f"   isNegRisk: {market.is_neg_risk}")
            print(f"   isYieldBearing: {market.is_yield_bearing}")
            print(f"   Комиссия: {market.fee_rate_bps} bps")
            print(f"   Порог спреда для поинтов: {market.spread_threshold}")
            print(f"   Мин. акций для поинтов: {market.share_threshold}")
            
            if market.yes_outcome:
                print(f"   YES token: {market.yes_outcome.on_chain_id[:20]}...")
            if market.no_outcome:
                print(f"   NO token: {market.no_outcome.on_chain_id[:20]}...")
            
            # Тест ордербука
            print(f"\n📖 Ордербук для рынка #{market.id}...")
            ob = api.get_orderbook(market.id)
            print(f"   Лучший YES BID: {ob.best_bid}")
            print(f"   Лучший YES ASK: {ob.best_ask}")
            print(f"   Спред: {ob.spread}")
            
            no_buy, no_sell = ob.get_no_prices(market.decimal_precision)
            print(f"   NO Buy (= 1 - YES Bid): {no_buy}")
            print(f"   NO Sell (= 1 - YES Ask): {no_sell}")
        
        print("\n✅ Тесты пройдены!")
        
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
