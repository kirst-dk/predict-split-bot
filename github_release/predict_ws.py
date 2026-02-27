# -*- coding: utf-8 -*-
"""
Predict.fun WebSocket Client
=============================

Клиент для real-time подписок через WebSocket API predict.fun.

Каналы:
- predictOrderbook/{marketId} — обновления стакана в реальном времени
- predictWalletEvents/{jwt}   — события ордеров (fill, cancel и т.д.)
- assetPriceUpdate/{feedId}    — обновления цен активов

Документация: https://dev.predict.fun/general-information-1915499m0
"""

import json
import time
import asyncio
import logging
from typing import Optional, Dict, List, Callable, Any, Set
from datetime import datetime

logger = logging.getLogger(__name__)

# WebSocket URL
WS_URL = "wss://ws.predict.fun/ws"


class PredictWebSocket:
    """
    WebSocket клиент для predict.fun
    
    Подключается к wss://ws.predict.fun/ws, управляет подписками
    на orderbook и wallet events, обрабатывает heartbeats.
    
    Usage:
        ws = PredictWebSocket(api_key="...", jwt_token="...")
        ws.on_orderbook_update = my_orderbook_handler
        ws.on_wallet_event = my_wallet_handler
        await ws.connect()
        await ws.subscribe_orderbook(12345)
        await ws.subscribe_wallet_events()
    """
    
    def __init__(
        self,
        api_key: str = "",
        jwt_token: str = "",
        max_reconnect_attempts: int = 50,
        max_retry_interval: int = 30,
    ):
        self.api_key = api_key
        self.jwt_token = jwt_token
        self.max_reconnect_attempts = max_reconnect_attempts
        self.max_retry_interval = max_retry_interval
        
        # WebSocket connection
        self._ws = None
        self._connected = False
        self._running = False
        self._reconnect_attempts = 0
        
        # Request ID counter
        self._request_id = 0
        
        # Подписки: topic_name -> set of callbacks
        self._subscriptions: Dict[str, List[Callable]] = {}
        
        # Pending subscription requests: request_id -> topic_name
        self._pending_subs: Dict[int, str] = {}
        
        # Активные подписки на orderbook: market_id -> topic_name
        self._orderbook_subs: Dict[int, str] = {}
        
        # Wallet events topic
        self._wallet_topic: Optional[str] = None
        
        # Callbacks
        self.on_orderbook_update: Optional[Callable] = None  # (market_id, data) -> None
        self.on_wallet_event: Optional[Callable] = None      # (event_data) -> None
        self.on_connected: Optional[Callable] = None          # () -> None
        self.on_disconnected: Optional[Callable] = None       # () -> None
        
        # Heartbeat
        self._last_heartbeat: float = 0
        self._heartbeat_timeout = 30  # секунд без heartbeat = проблема
        
        # Stats
        self.messages_received = 0
        self.reconnections = 0
        self.last_message_time: Optional[datetime] = None
    
    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None
    
    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id
    
    async def connect(self):
        """Подключиться к WebSocket серверу"""
        try:
            import websockets
        except ImportError:
            logger.error("websockets не установлен! pip install websockets")
            return False
        
        url = WS_URL
        if self.api_key:
            url += f"?apiKey={self.api_key}"
        
        try:
            logger.info(f"🔌 Подключение к WebSocket: {WS_URL}...")
            self._ws = await websockets.connect(
                url,
                ping_interval=None,  # мы сами обрабатываем heartbeats
                ping_timeout=None,
                close_timeout=5,
                max_size=10 * 1024 * 1024,  # 10MB max message
            )
            self._connected = True
            self._running = True
            self._reconnect_attempts = 0
            self._last_heartbeat = time.time()
            
            logger.info("✅ WebSocket подключён!")
            
            if self.on_connected:
                await self._call_async(self.on_connected)
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к WebSocket: {e}")
            self._connected = False
            return False
    
    async def disconnect(self):
        """Отключиться от WebSocket"""
        self._running = False
        self._connected = False
        
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        
        self._subscriptions.clear()
        self._pending_subs.clear()
        self._orderbook_subs.clear()
        self._wallet_topic = None
        
        logger.info("🔌 WebSocket отключён")
    
    async def _reconnect(self):
        """Переподключение с экспоненциальным backoff"""
        self._connected = False
        
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        
        self._reconnect_attempts += 1
        self.reconnections += 1
        
        if self._reconnect_attempts > self.max_reconnect_attempts:
            logger.error(f"❌ Превышено максимальное количество попыток переподключения "
                        f"({self.max_reconnect_attempts})")
            self._running = False
            return False
        
        delay = min(2 ** self._reconnect_attempts, self.max_retry_interval)
        logger.warning(f"🔄 Переподключение через {delay} сек... "
                      f"(попытка {self._reconnect_attempts}/{self.max_reconnect_attempts})")
        
        await asyncio.sleep(delay)
        
        success = await self.connect()
        if success:
            # Перезаписываемся на все активные подписки
            await self._resubscribe_all()
        
        return success
    
    async def _resubscribe_all(self):
        """Переподписаться на все активные каналы после переподключения"""
        logger.info("🔄 Восстановление подписок...")
        
        # Сохраняем текущие подписки
        orderbook_markets = list(self._orderbook_subs.keys())
        had_wallet = self._wallet_topic is not None
        
        # Очищаем
        self._orderbook_subs.clear()
        self._wallet_topic = None
        old_subs = dict(self._subscriptions)
        self._subscriptions.clear()
        
        # Переподписываемся на orderbook
        for market_id in orderbook_markets:
            await self.subscribe_orderbook(market_id)
        
        # Переподписываемся на wallet events
        if had_wallet and self.jwt_token:
            await self.subscribe_wallet_events()
        
        logger.info(f"✅ Восстановлено подписок: {len(orderbook_markets)} orderbook" +
                    (", wallet events" if had_wallet else ""))
    
    async def _send(self, data: dict):
        """Отправить сообщение в WebSocket"""
        if not self._ws or not self._connected:
            logger.warning("WebSocket не подключён, сообщение игнорируется")
            return
        
        try:
            await self._ws.send(json.dumps(data))
        except Exception as e:
            logger.error(f"Ошибка отправки в WebSocket: {e}")
            self._connected = False
    
    async def subscribe_orderbook(self, market_id: int):
        """
        Подписаться на обновления стакана для рынка
        
        Topic: predictOrderbook/{marketId}
        """
        topic = f"predictOrderbook/{market_id}"
        
        if topic in self._subscriptions:
            logger.debug(f"Уже подписаны на {topic}")
            return
        
        request_id = self._next_request_id()
        self._pending_subs[request_id] = topic
        self._orderbook_subs[market_id] = topic
        self._subscriptions[topic] = []
        
        await self._send({
            "requestId": request_id,
            "method": "subscribe",
            "params": [topic],
        })
        
        logger.info(f"📊 Подписка на orderbook: market #{market_id}")
    
    async def unsubscribe_orderbook(self, market_id: int):
        """Отписаться от обновлений стакана"""
        topic = f"predictOrderbook/{market_id}"
        
        if topic not in self._subscriptions:
            return
        
        request_id = self._next_request_id()
        
        await self._send({
            "requestId": request_id,
            "method": "unsubscribe",
            "params": [topic],
        })
        
        self._subscriptions.pop(topic, None)
        self._orderbook_subs.pop(market_id, None)
        
        logger.info(f"📊 Отписка от orderbook: market #{market_id}")
    
    async def subscribe_wallet_events(self):
        """
        Подписаться на события кошелька (требуется JWT)
        
        Topic: predictWalletEvents/{jwt}
        
        Events:
        - orderAccepted — ордер принят
        - orderNotAccepted — ордер отклонён
        - orderExpired — ордер истёк
        - orderCancelled — ордер отменён
        - orderTransactionSubmitted — ордер сматчен, транзакция отправлена
        - orderTransactionSuccess — транзакция прошла
        - orderTransactionFailed — транзакция провалилась
        """
        if not self.jwt_token:
            logger.warning("JWT токен не установлен, wallet events недоступны")
            return
        
        topic = f"predictWalletEvents/{self.jwt_token}"
        self._wallet_topic = topic
        
        if topic in self._subscriptions:
            logger.debug(f"Уже подписаны на wallet events")
            return
        
        request_id = self._next_request_id()
        self._pending_subs[request_id] = topic
        self._subscriptions[topic] = []
        
        await self._send({
            "requestId": request_id,
            "method": "subscribe",
            "params": [topic],
        })
        
        logger.info("👛 Подписка на wallet events")
    
    async def unsubscribe_wallet_events(self):
        """Отписаться от событий кошелька"""
        if not self._wallet_topic:
            return
        
        request_id = self._next_request_id()
        
        await self._send({
            "requestId": request_id,
            "method": "unsubscribe",
            "params": [self._wallet_topic],
        })
        
        self._subscriptions.pop(self._wallet_topic, None)
        self._wallet_topic = None
        
        logger.info("👛 Отписка от wallet events")
    
    def update_jwt(self, new_jwt: str):
        """Обновить JWT токен (нужно перезаписаться на wallet events)"""
        self.jwt_token = new_jwt
    
    async def _handle_heartbeat(self, data):
        """Обработать heartbeat от сервера и ответить"""
        self._last_heartbeat = time.time()
        
        # Отправляем ответ с тем же timestamp
        await self._send({
            "method": "heartbeat",
            "data": data,
        })
        
        logger.debug(f"💓 Heartbeat: {data}")
    
    async def _handle_message(self, raw_message: str):
        """Обработать входящее сообщение"""
        try:
            parsed = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning(f"Невалидный JSON от WebSocket: {raw_message[:200]}")
            return
        
        self.messages_received += 1
        self.last_message_time = datetime.now()
        
        msg_type = parsed.get("type")
        
        # ===== Push Message (M) =====
        if msg_type == "M":
            topic = parsed.get("topic", "")
            data = parsed.get("data")
            
            # Heartbeat
            if topic == "heartbeat":
                await self._handle_heartbeat(data)
                return
            
            # Orderbook update
            if topic.startswith("predictOrderbook/"):
                try:
                    market_id = int(topic.split("/")[1])
                except (IndexError, ValueError):
                    logger.warning(f"Неверный topic для orderbook: {topic}")
                    return
                
                if self.on_orderbook_update:
                    await self._call_async(self.on_orderbook_update, market_id, data)
                return
            
            # Wallet events
            if topic.startswith("predictWalletEvents/"):
                if self.on_wallet_event:
                    await self._call_async(self.on_wallet_event, data)
                return
            
            # Asset price update
            if topic.startswith("assetPriceUpdate/"):
                # Пока не обрабатываем, но можно добавить
                return
            
            logger.debug(f"Неизвестный topic: {topic}")
        
        # ===== Request Response (R) =====
        elif msg_type == "R":
            request_id = parsed.get("requestId")
            success = parsed.get("success", False)
            
            topic_name = self._pending_subs.pop(request_id, None)
            
            if topic_name:
                if success:
                    logger.info(f"✅ Подписка подтверждена: {topic_name}")
                else:
                    error = parsed.get("error", {})
                    error_code = error.get("code", "unknown")
                    error_msg = error.get("message", "")
                    logger.error(f"❌ Ошибка подписки {topic_name}: [{error_code}] {error_msg}")
                    
                    # Убираем из подписок
                    self._subscriptions.pop(topic_name, None)
                    
                    # Если это orderbook — убираем из маппинга
                    for mid, t in list(self._orderbook_subs.items()):
                        if t == topic_name:
                            del self._orderbook_subs[mid]
                            break
                    
                    # Если это wallet — сбрасываем
                    if topic_name == self._wallet_topic:
                        self._wallet_topic = None
            else:
                if not success:
                    error = parsed.get("error", {})
                    logger.warning(f"Ответ с ошибкой (reqId={request_id}): {error}")
        
        else:
            logger.debug(f"Неизвестный тип сообщения: {msg_type}")
    
    async def _call_async(self, func: Callable, *args):
        """Вызвать callback (sync или async)"""
        try:
            result = func(*args)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.error(f"Ошибка в callback: {e}", exc_info=True)
    
    async def listen(self):
        """
        Основной цикл прослушивания WebSocket.
        
        Запускается как asyncio task. Обрабатывает все входящие сообщения,
        heartbeats, и переподключения.
        """
        self._running = True
        
        while self._running:
            # Подключаемся если нужно
            if not self._connected:
                success = await self._reconnect()
                if not success:
                    break
                continue
            
            try:
                import websockets
                
                # Читаем сообщения
                async for message in self._ws:
                    if not self._running:
                        break
                    
                    await self._handle_message(message)
                
                # Если цикл for завершился — соединение закрыто
                if self._running:
                    logger.warning("⚠️ WebSocket соединение закрыто сервером")
                    self._connected = False
                    
                    if self.on_disconnected:
                        await self._call_async(self.on_disconnected)
                        
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"⚠️ WebSocket закрыт: code={e.code}, reason={e.reason}")
                self._connected = False
                
                if self.on_disconnected:
                    await self._call_async(self.on_disconnected)
                    
            except Exception as e:
                logger.error(f"❌ Ошибка в WebSocket listener: {e}", exc_info=True)
                self._connected = False
                
                if self.on_disconnected:
                    await self._call_async(self.on_disconnected)
        
        await self.disconnect()
        logger.info("🛑 WebSocket listener остановлен")
    
    def get_stats(self) -> Dict:
        """Получить статистику WebSocket соединения"""
        return {
            "connected": self._connected,
            "messages_received": self.messages_received,
            "reconnections": self.reconnections,
            "subscribed_markets": list(self._orderbook_subs.keys()),
            "wallet_events_active": self._wallet_topic is not None,
            "last_message": self.last_message_time.strftime("%H:%M:%S") if self.last_message_time else None,
            "last_heartbeat_ago": f"{time.time() - self._last_heartbeat:.0f}s" if self._last_heartbeat else None,
        }
