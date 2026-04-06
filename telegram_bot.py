# -*- coding: utf-8 -*-
"""
Predict.fun Telegram Bot
========================

Telegram-интерфейс для управления торговым ботом Predict.fun
"""

import os
import sys
import asyncio
import logging
from collections import deque
from datetime import datetime
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from dotenv import load_dotenv

# Фикс Unicode для Windows (CP1251 не поддерживает emoji)
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.request import HTTPXRequest
from telegram.helpers import escape_markdown

# Локальные модули
from config import (
    API_KEY, CHAIN_ID, ChainId,
    SPLIT_OFFSET, CHECK_INTERVAL, ASK_POSITION_OFFSET, REPOSITION_DELAY, AUTO_MARKET_EXIT_ON_FULL_FILL,
    AccountConfig, load_accounts, save_accounts, ACCOUNTS,
    USE_WEBSOCKET, WS_FALLBACK_INTERVAL, WS_MARKET_REFRESH_INTERVAL,
    REFERRAL_LINK, DISCONNECT_MESSAGE, MAX_USER_ACCOUNTS, USERS_DIR,
)
from predict_api import PredictAPI, wei_to_float
from predict_trader import PredictTrader
from predict_ws import PredictWebSocket
from state import get_state, PersistentState, MarketTaskState, OrderState
from user_manager import get_user_manager, UserManager

# Загружаем .env
load_dotenv()

# Telegram Bot Token
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_ADMIN_ID = os.getenv('TELEGRAM_ADMIN_ID', '')  # ID администратора (только он может управлять)

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
(
    STATE_WAITING_NAME,
    STATE_WAITING_PRIVATE_KEY,
    STATE_WAITING_ADDRESS,
    STATE_WAITING_MARKET_ID,
    STATE_WAITING_AMOUNT,
) = range(5)


# =============================================================================
# ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# =============================================================================

class BotState:
    """Глобальное состояние бота с персистентностью"""
    def __init__(self):
        self.traders: Dict[str, PredictTrader] = {}  # account_address -> trader
        self.running: Dict[str, bool] = {}  # account_address -> is_running
        self.tasks: Dict[str, asyncio.Task] = {}  # account_address -> monitoring task
        self.app: Optional[Application] = None  # Telegram application
        self.error_count: int = 0  # Счётчик ошибок
        self.last_error_time: Optional[datetime] = None
        self.last_restart_time: Optional[datetime] = None  # Время последней перезагрузки
        self.restart_job = None  # Планировщик перезагрузки
        
        # Персистентное состояние
        self._persistent = get_state()
        
        # Multi-user support
        self.account_owners: Dict[str, str] = {}  # address -> telegram_id владельца
        self.user_persistent_states: Dict[str, PersistentState] = {}  # user_id -> PersistentState
        
        # Лог событий по аккаунтам (для on-demand просмотра логов рефералов)
        self.account_event_logs: Dict[str, deque] = {}  # address -> deque of events
        self.MAX_EVENT_LOG_SIZE = 50  # Максимум событий на аккаунт
        
        # Восстанавливаем настройки из персистентного состояния
        self.error_notifications: bool = self._persistent.get_setting('error_notifications', True)
        self.repositioning_notifications: bool = self._persistent.get_setting('repositioning_notifications', True)
    
    @property
    def persistent(self) -> PersistentState:
        """Доступ к персистентному состоянию"""
        return self._persistent
    
    def save_settings(self):
        """Сохранить настройки в персистентное состояние"""
        self._persistent.set_setting('error_notifications', self.error_notifications)
        self._persistent.set_setting('repositioning_notifications', self.repositioning_notifications)
    
    def mark_account_running(self, address: str, name: str, running: bool, owner_id: str = None):
        """Отметить аккаунт как запущенный/остановленный (с персистентностью)"""
        self.running[address] = running
        ps = self.get_persistent_for_user(owner_id)
        ps.set_account_running(address, name, running)
    
    def get_accounts_to_restore(self) -> List[str]:
        """Получить список аккаунтов которые нужно восстановить"""
        return self._persistent.get_running_accounts()
    
    def get_persistent_for_user(self, user_id: str = None) -> PersistentState:
        """Получить PersistentState для пользователя (админ — основной, остальные — per-user)"""
        if not user_id or user_id == TELEGRAM_ADMIN_ID:
            return self._persistent
        if user_id not in self.user_persistent_states:
            um = get_user_manager()
            state_file = um.get_user_state_file(user_id)
            state_dir = os.path.dirname(state_file)
            os.makedirs(state_dir, exist_ok=True)
            self.user_persistent_states[user_id] = PersistentState(state_file)
        return self.user_persistent_states[user_id]
    
    def log_account_event(self, address: str, event_type: str, message: str):
        """Записать событие в лог аккаунта (для просмотра логов рефералов)"""
        if address not in self.account_event_logs:
            self.account_event_logs[address] = deque(maxlen=self.MAX_EVENT_LOG_SIZE)
        self.account_event_logs[address].append({
            'time': datetime.now().strftime('%H:%M:%S'),
            'type': event_type,
            'message': message,
        })
    
    def get_account_events(self, address: str, limit: int = 20) -> list:
        """Получить последние события аккаунта"""
        if address not in self.account_event_logs:
            return []
        events = list(self.account_event_logs[address])
        return events[-limit:]
    
    def get_user_events(self, user_id: str, limit: int = 20) -> list:
        """Получить последние события всех аккаунтов пользователя"""
        events = []
        for addr, owner in self.account_owners.items():
            if owner == user_id and addr in self.account_event_logs:
                for event in self.account_event_logs[addr]:
                    events.append(event)
        # Сортируем по времени и берём последние
        events.sort(key=lambda e: e['time'])
        return events[-limit:]
    
    def clear_event_logs(self):
        """Очистить все логи событий (вызывается каждые 24 часа)"""
        count = sum(len(q) for q in self.account_event_logs.values())
        self.account_event_logs.clear()
        logger.info(f"🗑 Очищено {count} событий из логов аккаунтов")
        
bot_state = BotState()

SETTINGS_ASK_POSITION_MIN = 1
SETTINGS_ASK_POSITION_MAX = 10
SETTINGS_REPOSITION_DELAY_MIN = 0
SETTINGS_REPOSITION_DELAY_MAX = 120
SETTINGS_ASK_STEP = 1
SETTINGS_DELAY_STEP = 5

# =============================================================================
# СИСТЕМА УВЕДОМЛЕНИЙ ОБ ОШИБКАХ
# =============================================================================

async def send_error_notification(error_message: str, error_type: str = "ERROR", chat_id: str = None):
    """Отправить уведомление об ошибке"""
    target_id = chat_id or TELEGRAM_ADMIN_ID
    if not target_id or not bot_state.app:
        logger.error(f"Cannot send error notification: {error_message}")
        return
    
    # Проверяем флаг уведомлений об ошибках
    if not bot_state.error_notifications:
        logger.warning(f"Error notification disabled: {error_message}")
        return
    
    try:
        bot_state.error_count += 1
        bot_state.last_error_time = datetime.now()
        
        # Ограничиваем длину сообщения
        if len(error_message) > 3000:
            error_message = error_message[:3000] + "..."
        
        text = (
            f"🚨 *{error_type}*\n\n"
            f"```\n{error_message}\n```\n\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"📊 Всего ошибок: {bot_state.error_count}"
        )
        
        await bot_state.app.bot.send_message(
            chat_id=int(target_id),
            text=text,
            parse_mode='Markdown',
            disable_notification=True  # Тихое уведомление
        )
    except Exception as e:
        logger.error(f"Failed to send error notification: {e}")


async def send_repositioning_notification(account_name: str, repositioned_orders: list,
                                          chat_id: str = None, reposition_delay: int = None,
                                          account_address: str = None):
    """Отправить уведомление о переставлении ордеров (тихое, не уводит бота вверх)"""
    target_id = chat_id or TELEGRAM_ADMIN_ID
    
    if not repositioned_orders:
        return
    
    # Логируем событие в буфер (всегда, независимо от флага уведомлений)
    if account_address:
        for order in repositioned_orders:
            market = order.get('market_name', 'Unknown')[:25]
            token = order.get('token', '?')
            old_cents = order.get('old_price', 0) * 100
            new_cents = order.get('new_price', 0) * 100
            direction = "↑" if new_cents > old_cents else "↓"
            bot_state.log_account_event(
                account_address, 'reposition',
                f"{direction} {market} {token}: {old_cents:.1f}→{new_cents:.1f}¢"
            )
    
    if not bot_state.repositioning_notifications:
        return
    
    try:
        lines = [f"🔄 *Переставление ордеров*", f"👤 Аккаунт: {account_name}", ""]
        
        for order in repositioned_orders:
            market = order.get('market_name', 'Unknown')
            token = order.get('token', '?')
            old_price = order.get('old_price', 0)
            new_price = order.get('new_price', 0)
            quantity = order.get('quantity', 0)  # Количество токенов
            is_delayed = order.get('delayed', False)
            
            # Конвертируем в центы для читабельности
            old_cents = old_price * 100
            new_cents = new_price * 100
            
            # Расчёт стоимости ордера
            order_value = quantity * new_price if quantity else 0
            
            direction = "📈" if new_price > old_price else "📉"
            lines.append(f"{direction} *{market[:25]}*")
            lines.append(f"   {token}: {old_cents:.1f}¢ → {new_cents:.1f}¢")
            if quantity > 0:
                lines.append(f"   📦 {quantity:.1f} shares (~${order_value:.2f})")
            if is_delayed:
                delay = REPOSITION_DELAY if reposition_delay is None else reposition_delay
                lines.append(f"   ⏳ Ожидание {delay}с перед новым ордером")
        
        lines.append(f"\n🕐 {datetime.now().strftime('%H:%M:%S')}")
        
        text = "\n".join(lines)
        
        # disable_notification=True - сообщение приходит без звука и не уводит чат вверх
        await bot_state.app.bot.send_message(
            chat_id=int(target_id),
            text=text,
            parse_mode='Markdown',
            disable_notification=True
        )
    except Exception as e:
        logger.error(f"Failed to send repositioning notification: {e}")


async def send_resolved_market_notification(account_name: str, resolved_results: list, chat_id: str = None):
    """Отправить уведомление о закрытии рынков (ГРОМКОЕ — это важное событие)"""
    target_id = chat_id or TELEGRAM_ADMIN_ID
    if not target_id or not bot_state.app:
        return
    
    if not resolved_results:
        return
    
    try:
        for res in resolved_results:
            market_title = res.get('market_title', '?')
            market_id = res.get('market_id', '?')
            cancelled = res.get('cancelled', 0)
            merged = res.get('merged', False)
            merge_amount = res.get('merge_amount', 0)
            balance_after = res.get('balance_after', 0)
            
            merge_status = f"✅ Merge выполнен: {merge_amount:.2f} токенов" if merged else "❌ Merge не удался"
            if merge_amount < 0.01:
                merge_status = "ℹ️ Merge не требуется (нет позиций)"
            
            text = (
                f"🏁 *РЫНОК ЗАКРЫТ (RESOLVED)*\n\n"
                f"👤 Аккаунт: {account_name}\n"
                f"📊 *{market_title}*\n"
                f"🆔 #{market_id}\n\n"
                f"📋 Отменено ордеров: {cancelled}\n"
                f"{merge_status}\n"
                f"💰 Баланс: ${balance_after:.2f} USDT\n\n"
                f"🕐 {datetime.now().strftime('%H:%M:%S')}"
            )
            
            await bot_state.app.bot.send_message(
                chat_id=int(target_id),
                text=text,
                parse_mode='Markdown',
                disable_notification=False,  # ГРОМКОЕ уведомление!
            )
    except Exception as e:
        logger.error(f"Failed to send resolved market notification: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок для Telegram бота"""
    from telegram.error import NetworkError, TimedOut, RetryAfter
    
    error = context.error
    error_type = type(error).__name__
    
    # Сетевые ошибки — просто логируем, библиотека сама переподключится
    if isinstance(error, (NetworkError, TimedOut)):
        logger.warning(f"Network error (will auto-retry): {error_type}: {error}")
        return  # Не отправляем уведомление, это временная ошибка
    
    # RetryAfter — Telegram просит подождать
    if isinstance(error, RetryAfter):
        logger.warning(f"Rate limited, retry after {error.retry_after} seconds")
        return
    
    logger.error(f"Exception while handling an update: {error}")
    
    # Формируем сообщение об ошибке
    error_msg = f"{error_type}: {error}"
    
    # Добавляем информацию об update если есть
    if update:
        if isinstance(update, Update):
            if update.effective_user:
                error_msg += f"\nUser: {update.effective_user.id}"
            if update.effective_message:
                error_msg += f"\nMessage: {update.effective_message.text[:100] if update.effective_message.text else 'N/A'}"
    
    await send_error_notification(error_msg, "TELEGRAM ERROR")


# =============================================================================
# ПРОВЕРКА ДОСТУПА
# =============================================================================

def admin_only(func):
    """Декоратор: только администратор может использовать команду"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        if TELEGRAM_ADMIN_ID and user_id != TELEGRAM_ADMIN_ID:
            await update.message.reply_text("⛔ Доступ запрещён")
            return
        return await func(update, context)
    return wrapper


def admin_only_callback(func):
    """Декоратор для callback queries"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        if TELEGRAM_ADMIN_ID and user_id != TELEGRAM_ADMIN_ID:
            await update.callback_query.answer("⛔ Доступ запрещён", show_alert=True)
            return
        return await func(update, context)
    return wrapper


def authorized_user(func):
    """Декоратор: админ или активный реферал"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        if is_admin_user(user_id):
            return await func(update, context)
        um = get_user_manager()
        if um.is_authorized(user_id):
            um.update_last_active(user_id)
            return await func(update, context)
        user = um.get_user(user_id)
        if user:
            if user.status == 'pending':
                await update.message.reply_text("⏳ Ваша заявка на рассмотрении. Ожидайте одобрения.")
                return
            if user.status == 'disabled':
                await update.message.reply_text(f"⛔ {DISCONNECT_MESSAGE}")
                return
        await update.message.reply_text("⛔ Доступ запрещён. Нажмите /start для регистрации.")
        return
    return wrapper


def authorized_user_callback(func):
    """Декоратор для callback queries: админ или активный реферал"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        if is_admin_user(user_id):
            return await func(update, context)
        um = get_user_manager()
        if um.is_authorized(user_id):
            um.update_last_active(user_id)
            return await func(update, context)
        await update.callback_query.answer("⛔ Доступ запрещён", show_alert=True)
        return
    return wrapper


# =============================================================================
# ХЕЛПЕРЫ МУЛЬТИ-ЮЗЕР
# =============================================================================

def is_admin_user(user_id: str) -> bool:
    """Проверить, является ли пользователь админом"""
    return bool(TELEGRAM_ADMIN_ID) and user_id == TELEGRAM_ADMIN_ID


def get_accounts_for_user(user_id: str) -> List[AccountConfig]:
    """Получить аккаунты пользователя (админ — из accounts.json, реферал — из users/<id>/accounts.json)"""
    if is_admin_user(user_id):
        return load_accounts()
    um = get_user_manager()
    return um.load_user_accounts(user_id)


def save_accounts_for_user(user_id: str, accounts: List[AccountConfig]) -> bool:
    """Сохранить аккаунты пользователя"""
    if is_admin_user(user_id):
        return save_accounts(accounts)
    um = get_user_manager()
    return um.save_user_accounts(user_id, accounts)


def md(text: str) -> str:
    """Экранировать текст для parse_mode='Markdown'."""
    return escape_markdown(str(text), version=1)


def get_user_trade_settings(user_id: str) -> Dict[str, Any]:
    """Получить торговые настройки пользователя (с fallback к config.py)."""
    ps = bot_state.get_persistent_for_user(user_id)

    ask_offset = int(ps.get_setting('ask_position_offset', ASK_POSITION_OFFSET))
    ask_offset = max(SETTINGS_ASK_POSITION_MIN, min(SETTINGS_ASK_POSITION_MAX, ask_offset))

    reposition_delay = int(ps.get_setting('reposition_delay', REPOSITION_DELAY))
    reposition_delay = max(SETTINGS_REPOSITION_DELAY_MIN, min(SETTINGS_REPOSITION_DELAY_MAX, reposition_delay))

    auto_market_exit_on_full_fill = bool(
        ps.get_setting('auto_market_exit_on_full_fill', AUTO_MARKET_EXIT_ON_FULL_FILL)
    )

    return {
        'ask_position_offset': ask_offset,
        'reposition_delay': reposition_delay,
        'auto_market_exit_on_full_fill': auto_market_exit_on_full_fill,
    }


def set_user_trade_setting(user_id: str, key: str, value: int) -> int:
    """Сохранить торговую настройку пользователя и вернуть итоговое значение."""
    if key == 'ask_position_offset':
        value = max(SETTINGS_ASK_POSITION_MIN, min(SETTINGS_ASK_POSITION_MAX, int(value)))
    elif key == 'reposition_delay':
        value = max(SETTINGS_REPOSITION_DELAY_MIN, min(SETTINGS_REPOSITION_DELAY_MAX, int(value)))
    else:
        return int(value)

    ps = bot_state.get_persistent_for_user(user_id)
    ps.set_setting(key, value)
    return value


def set_user_trade_toggle(user_id: str, key: str, value: bool) -> bool:
    """Сохранить булевую торговую настройку пользователя."""
    ps = bot_state.get_persistent_for_user(user_id)
    ps.set_setting(key, bool(value))
    return bool(value)


def apply_user_trade_settings_to_trader(trader: PredictTrader, user_id: str):
    """Применить торговые настройки пользователя к экземпляру трейдера."""
    settings = get_user_trade_settings(user_id)
    trader.ask_position_offset = settings['ask_position_offset']
    trader.reposition_delay = settings['reposition_delay']
    trader.auto_market_exit_on_full_fill = settings['auto_market_exit_on_full_fill']


def apply_user_trade_settings_to_running_traders(user_id: str) -> int:
    """Применить обновлённые настройки ко всем уже созданным трейдерам пользователя.
    Возвращает количество обновлённых трейдеров."""
    count = 0
    for acc in get_accounts_for_user(user_id):
        trader = bot_state.traders.get(acc.predict_account_address)
        if trader:
            apply_user_trade_settings_to_trader(trader, user_id)
            count += 1
    return count


def build_settings_view(user_id: str):
    """Текст и клавиатура меню настроек."""
    trade_settings = get_user_trade_settings(user_id)
    ask_offset = trade_settings['ask_position_offset']
    reposition_delay = trade_settings['reposition_delay']
    auto_market_exit = trade_settings['auto_market_exit_on_full_fill']

    repo_status = "✅ Вкл" if bot_state.repositioning_notifications else "❌ Выкл"
    err_status = "✅ Вкл" if bot_state.error_notifications else "❌ Выкл"

    text = (
        "⚙️ *Настройки*\n\n"
        f"🔔 Уведомления об ошибках: {err_status}\n"
        f"🔄 Уведомления о переставлении: {repo_status}\n\n"
        f"🎯 ASK offset: *{ask_offset}* тиков\n"
        "   _Позиция SELL относительно лучшего ASK_\n\n"
        f"⏳ Задержка после отмены: *{reposition_delay}* сек\n"
        "   _Пауза после отмены перед новым ордером_\n\n"
        f"⚡ Auto market-exit при 100% fill: *{'ВКЛ' if auto_market_exit else 'ВЫКЛ'}*\n"
        "   _Закрывать вторую сторону по рынку после полного fill первой_\n\n"
        f"📊 Всего ошибок: {bot_state.error_count}\n"
    )

    if bot_state.last_error_time:
        text += f"🕐 Последняя: {bot_state.last_error_time.strftime('%Y-%m-%d %H:%M:%S')}\n"

    keyboard = [
        [InlineKeyboardButton(
            f"{'❌' if bot_state.error_notifications else '✅'} Ошибки", callback_data="settings_toggle_errors"
        )],
        [InlineKeyboardButton(
            f"{'❌' if bot_state.repositioning_notifications else '✅'} Переставления", callback_data="settings_toggle_repo_notif"
        )],
        [InlineKeyboardButton(
            f"{'✅' if auto_market_exit else '❌'} Auto market-exit (100% fill)", callback_data="settings_toggle_market_exit"
        )],
        [
            InlineKeyboardButton("➖ ASK", callback_data="settings_ask_minus"),
            InlineKeyboardButton(f"{ask_offset}", callback_data="settings_noop"),
            InlineKeyboardButton("➕ ASK", callback_data="settings_ask_plus"),
        ],
        [
            InlineKeyboardButton("➖ DELAY", callback_data="settings_delay_minus"),
            InlineKeyboardButton(f"{reposition_delay}s", callback_data="settings_noop"),
            InlineKeyboardButton("➕ DELAY", callback_data="settings_delay_plus"),
        ],
        [InlineKeyboardButton("✅ Применить", callback_data="settings_apply")],
        [InlineKeyboardButton("🔄 Сбросить счётчик ошибок", callback_data="settings_reset_errors")],
        [InlineKeyboardButton("« Главное меню", callback_data="main_menu")]
    ]

    return text, keyboard


def get_all_running_accounts() -> list:
    """Получить все запущенные аккаунты со всех пользователей (acc, owner_id)"""
    result = []
    # Админ
    for acc in load_accounts():
        if bot_state.running.get(acc.predict_account_address, False):
            result.append((acc, TELEGRAM_ADMIN_ID))
    # Рефералы
    um = get_user_manager()
    for user in um.get_active_users():
        for acc in um.load_user_accounts(user.telegram_id):
            if bot_state.running.get(acc.predict_account_address, False):
                result.append((acc, user.telegram_id))
    return result


# =============================================================================
# ГЛАВНОЕ МЕНЮ
# =============================================================================

def get_reply_keyboard(user_id: str = None):
    """Постоянная клавиатура внизу экрана (админ видит кнопку Рефералы)"""
    buttons = [
        [KeyboardButton("📊 Статус"), KeyboardButton("💰 Баланс")],
        [KeyboardButton("👥 Аккаунты"), KeyboardButton("📈 Рынки")],
        [KeyboardButton("🔴 Закрыть"), KeyboardButton("🤖 Бот")],
        [KeyboardButton("⚙️ Настройки")],
    ]
    if user_id and is_admin_user(user_id):
        buttons.append([KeyboardButton("📋 Рефералы"), KeyboardButton("❓ Как работает?")])
    else:
        buttons.append([KeyboardButton("❓ Как работает?")])
    return ReplyKeyboardMarkup(
        buttons,
        resize_keyboard=True,
        is_persistent=True,
    )


def get_main_menu_keyboard():
    """Клавиатура главного меню (inline, используется для навигации назад)"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Назад", callback_data="main_menu")],
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start — регистрация / приветствие"""
    user_id = str(update.effective_user.id)
    
    # Админ → полное меню
    if is_admin_user(user_id):
        await update.message.reply_text(
            "🤖 *Predict.fun Trading Bot*\n\n"
            "Добро пожаловать! Используйте меню внизу для управления ботом.",
            reply_markup=get_reply_keyboard(user_id),
            parse_mode='Markdown'
        )
        return
    
    # Проверяем статус пользователя
    um = get_user_manager()
    user = um.get_user(user_id)
    
    if user and user.status == 'active':
        await update.message.reply_text(
            "🤖 *Predict.fun Trading Bot*\n\n"
            "Добро пожаловать! Используйте меню внизу для управления ботом.",
            reply_markup=get_reply_keyboard(user_id),
            parse_mode='Markdown'
        )
        return
    
    if user and user.status == 'pending':
        await update.message.reply_text(
            "⏳ *Заявка на рассмотрении*\n\n"
            "Администратор скоро рассмотрит вашу заявку.\n"
            "Вы получите уведомление.",
            parse_mode='Markdown'
        )
        return
    
    if user and user.status == 'disabled':
        await update.message.reply_text(
            f"⛔ {DISCONNECT_MESSAGE}",
            parse_mode='Markdown'
        )
        return
    
    # Новый пользователь → реферальная ссылка + кнопка "Готово"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Реферальная ссылка", url=REFERRAL_LINK)],
        [InlineKeyboardButton("✅ Готово", callback_data="register_done")],
    ])
    await update.message.reply_text(
        "👋 *Добро пожаловать!*\n\n"
        "Чтобы пользоваться ботом, нужно быть рефералом.\n\n"
        "1️⃣ Перейдите по реферальной ссылке\n"
        "2️⃣ Нажмите «Готово»",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )


@authorized_user
async def handle_any_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик любого текстового сообщения - обрабатывает кнопки Reply Keyboard"""
    text = update.message.text.strip()
    user_id = str(update.effective_user.id)
    
    # Обработка кнопок Reply Keyboard
    if text == "📊 Статус":
        await cmd_status(update, context)
        return
    elif text == "💰 Баланс":
        await cmd_balance(update, context)
        return
    elif text == "👥 Аккаунты":
        await cmd_accounts(update, context)
        return
    elif text == "📈 Рынки":
        await cmd_markets(update, context)
        return
    elif text == "🔴 Закрыть":
        # Показываем меню закрытия позиций
        accounts = get_accounts_for_user(user_id)
        if not accounts:
            await update.message.reply_text("📭 Нет аккаунтов", reply_markup=get_reply_keyboard(user_id))
            return
        keyboard = []
        for i, acc in enumerate(accounts):
            keyboard.append([InlineKeyboardButton(f"👤 {acc.name}", callback_data=f"close_acc_{i}")])
        await update.message.reply_text(
            "🔴 *Закрыть позиции*\n\nВыберите аккаунт:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return
    elif text == "🤖 Бот":
        # Показываем меню управления ботом
        accounts = get_accounts_for_user(user_id)
        running_count = sum(1 for acc in accounts if bot_state.running.get(acc.predict_account_address, False))
        total_count = len(accounts)
        
        last_restart = bot_state.last_restart_time
        if last_restart:
            last_str = last_restart.strftime('%Y-%m-%d %H:%M:%S')
        else:
            last_str = "Нет данных"
        
        keyboard = [
            [InlineKeyboardButton("▶️ Запустить всех", callback_data="start_all")],
            [InlineKeyboardButton("⏹ Остановить всех", callback_data="stop_all")],
            [InlineKeyboardButton("🔄 Перезагрузить", callback_data="restart_now")],
        ]
        await update.message.reply_text(
            f"🤖 *Управление ботом*\n\n"
            f"📊 Запущено: {running_count}/{total_count} аккаунтов\n"
            f"📅 Последняя перезагрузка: {last_str}\n"
            f"⏰ Автообновление: каждый час (git pull + restart)",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return
    elif text == "⚙️ Настройки":
        # Показываем меню настроек
        text_settings, keyboard = build_settings_view(user_id)
        await update.message.reply_text(
            text_settings,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return
    elif text == "❓ Как работает?":
        await cmd_help(update, context)
        return
    elif text == "📋 Рефералы" and is_admin_user(user_id):
        await cmd_referrals(update, context)
        return
    
    # Для любого другого текста - просто показываем подсказку
    await update.message.reply_text(
        "👆 Используйте меню внизу для управления ботом",
        reply_markup=get_reply_keyboard(user_id)
    )


@authorized_user_callback
async def callback_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в главное меню - просто удаляем inline сообщение"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "👆 Используйте меню внизу для управления ботом"
    )


@authorized_user_callback
async def callback_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок главного меню"""
    query = update.callback_query
    await query.answer()
    
    action = query.data.replace("menu_", "")
    
    if action == "status":
        await show_status(query)
    elif action == "balance":
        await show_balance(query)
    elif action == "accounts":
        await show_accounts(query)
    elif action == "markets":
        await show_markets(query, context)
    elif action == "split":
        await show_split(query)
    elif action == "start_bot":
        await show_start_bot(query, context)
    elif action == "stop_bot":
        await show_stop_bot(query)
    elif action == "restart":
        await show_restart(query, context)
    elif action == "settings":
        await show_settings(query)
    elif action == "close":
        await show_close_position(query, context)
    elif action == "help":
        await show_help(query)


async def callback_ignore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Игнорируем клик на заголовок категории"""
    query = update.callback_query
    await query.answer()  # Просто подтверждаем, ничего не делаем


# =============================================================================
# МЕНЮ: СТАТУС
# =============================================================================

async def show_status(query):
    """Показать статус"""
    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)
    
    text = "📊 *Статус бота*\n\n"
    text += f"🔗 Сеть: {'BNB Mainnet' if CHAIN_ID == ChainId.BNB_MAINNET else 'Testnet'}\n"
    text += f"🔑 API Key: {'✅' if API_KEY else '❌'}\n"
    text += f" Offset: {SPLIT_OFFSET*100:.1f}%\n\n"
    
    text += f"👥 *Аккаунты ({len(accounts)}):*\n"
    
    for acc in accounts:
        addr_short = f"{acc.predict_account_address[:6]}...{acc.predict_account_address[-4:]}"
        is_running = bot_state.running.get(acc.predict_account_address, False)
        status = "🟢 Работает" if is_running else "⚪ Остановлен"
        text += f"• {acc.name} ({addr_short}) - {status}\n"
    
    if not accounts:
        text += "📭 Нет аккаунтов\n"
    
    keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


# =============================================================================
# МЕНЮ: БАЛАНС  
# =============================================================================

async def show_balance(query):
    """Показать баланс"""
    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)
    
    if not accounts:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(
            "📭 Нет аккаунтов\n\nДобавьте аккаунт через меню Аккаунты",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    await query.edit_message_text("⏳ Получение балансов...")
    
    text = "💰 *Балансы аккаунтов*\n\n"
    
    for acc in accounts:
        addr_short = f"{acc.predict_account_address[:6]}...{acc.predict_account_address[-4:]}"
        try:
            user_id = str(query.from_user.id)
            trader = await asyncio.to_thread(get_or_create_trader, acc, user_id)
            if trader:
                if not trader.order_builder:
                    await asyncio.to_thread(trader.init_sdk)
                balance = await asyncio.to_thread(trader.get_usdt_balance)

                # Пытаемся получить реальные PP через /v1/account (нужен JWT)
                points_text = "н/д"
                if not trader.api.jwt_token:
                    await asyncio.to_thread(trader.authenticate)
                points_value = await asyncio.to_thread(trader.get_predict_points)
                if points_value is not None:
                    points_text = f"{points_value:,.1f} PP"
                else:
                    est_points = await asyncio.to_thread(trader.points.estimate_points)
                    points_text = f"~{est_points:,.1f} PP"

                text += f"👤 *{acc.name}* ({addr_short})\n"
                text += f"   💵 ${balance:.2f} USDT\n\n"
                text += f"   💎 {points_text}\n\n"
            else:
                text += f"👤 *{acc.name}* - ❌ ошибка\n\n"
        except Exception as e:
            text += f"👤 *{acc.name}* - ❌ {str(e)[:30]}\n\n"
    
    keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


# =============================================================================
# МЕНЮ: НАСТРОЙКИ
# =============================================================================

async def show_settings(query):
    """Показать настройки"""
    user_id = str(query.from_user.id)
    text, keyboard = build_settings_view(user_id)

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


@authorized_user_callback
async def callback_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик настроек"""
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    
    action = query.data.replace("settings_", "")

    if action == "noop":
        return
    
    if action == "toggle_repo_notif" or action == "toggle_repositioning":
        bot_state.repositioning_notifications = not bot_state.repositioning_notifications
        bot_state.save_settings()
        status = "включены" if bot_state.repositioning_notifications else "выключены"
        await query.answer(f"Уведомления о переставлении {status}")
        text, keyboard = build_settings_view(user_id)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    
    elif action == "toggle_errors":
        bot_state.error_notifications = not bot_state.error_notifications
        bot_state.save_settings()
        status = "включены" if bot_state.error_notifications else "выключены"
        await query.answer(f"Уведомления об ошибках {status}")
        text, keyboard = build_settings_view(user_id)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif action == "toggle_market_exit":
        settings = get_user_trade_settings(user_id)
        current = settings['auto_market_exit_on_full_fill']
        value = set_user_trade_toggle(user_id, 'auto_market_exit_on_full_fill', not current)
        apply_user_trade_settings_to_running_traders(user_id)
        await query.answer(
            "Auto market-exit: ВКЛ" if value else "Auto market-exit: ВЫКЛ"
        )
        text, keyboard = build_settings_view(user_id)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif action == "ask_minus" or action == "ask_plus":
        settings = get_user_trade_settings(user_id)
        current = settings['ask_position_offset']
        delta = -SETTINGS_ASK_STEP if action == "ask_minus" else SETTINGS_ASK_STEP
        value = set_user_trade_setting(user_id, 'ask_position_offset', current + delta)
        apply_user_trade_settings_to_running_traders(user_id)
        await query.answer(f"ASK_POSITION_OFFSET: {value}")
        text, keyboard = build_settings_view(user_id)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif action == "delay_minus" or action == "delay_plus":
        settings = get_user_trade_settings(user_id)
        current = settings['reposition_delay']
        delta = -SETTINGS_DELAY_STEP if action == "delay_minus" else SETTINGS_DELAY_STEP
        value = set_user_trade_setting(user_id, 'reposition_delay', current + delta)
        apply_user_trade_settings_to_running_traders(user_id)
        await query.answer(f"REPOSITION_DELAY: {value}с")
        text, keyboard = build_settings_view(user_id)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif action == "apply":
        count = apply_user_trade_settings_to_running_traders(user_id)
        if count > 0:
            await query.answer(f"✅ Настройки применены к {count} трейдерам!", show_alert=True)
        else:
            await query.answer("ℹ️ Нет активных трейдеров для обновления", show_alert=True)
        text, keyboard = build_settings_view(user_id)
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif action == "reset_errors":
        bot_state.error_count = 0
        bot_state.last_error_time = None
        await query.answer("Счётчик ошибок сброшен")
        await show_settings(query)


# =============================================================================
# РЕГИСТРАЦИЯ ПОЛЬЗОВАТЕЛЕЙ
# =============================================================================

async def callback_register_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь нажал 'Готово' после перехода по реф. ссылке"""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    username = query.from_user.username or ""
    first_name = query.from_user.first_name or ""

    um = get_user_manager()
    user = um.get_user(user_id)

    if user:
        if user.status == 'active':
            await query.edit_message_text("✅ Вы уже зарегистрированы! Нажмите /start")
            return
        if user.status == 'pending':
            await query.edit_message_text("⏳ Ваша заявка уже на рассмотрении.")
            return
        if user.status == 'disabled':
            await query.edit_message_text(f"⛔ {DISCONNECT_MESSAGE}")
            return

    # Создаём заявку
    um.add_pending_user(user_id, f"@{username}" if username else "", first_name)

    await query.edit_message_text(
        "✅ *Заявка отправлена!*\n\n"
        "Администратор рассмотрит вашу заявку.\n"
        "Вы получите уведомление о решении.",
        parse_mode='Markdown'
    )

    # Уведомляем админа
    if TELEGRAM_ADMIN_ID and bot_state.app:
        display_name = first_name or "Неизвестный"
        if username:
            display_name += f" (@{username})"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Подключить", callback_data=f"admin_approve_{user_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_{user_id}"),
            ]
        ])

        try:
            await bot_state.app.bot.send_message(
                chat_id=int(TELEGRAM_ADMIN_ID),
                text=(
                    f"🆕 *Новая заявка на подключение*\n\n"
                    f"👤 {display_name}\n"
                    f"🆔 ID: `{user_id}`\n"
                    f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                ),
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to notify admin about new user: {e}")


@admin_only_callback
async def callback_admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ одобряет заявку пользователя"""
    query = update.callback_query
    await query.answer()

    target_id = query.data.replace("admin_approve_", "")
    um = get_user_manager()

    if um.approve_user(target_id):
        user = um.get_user(target_id)
        await query.edit_message_text(
            f"✅ Пользователь {user.display_name} подключен!",
        )
        # Уведомляем пользователя
        try:
            await bot_state.app.bot.send_message(
                chat_id=int(target_id),
                text=(
                    "🎉 *Заявка одобрена!*\n\n"
                    "Добро пожаловать! Нажмите /start чтобы начать."
                ),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to notify approved user: {e}")
    else:
        await query.edit_message_text("❌ Пользователь не найден")


@admin_only_callback
async def callback_admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ отклоняет заявку"""
    query = update.callback_query
    await query.answer()

    target_id = query.data.replace("admin_reject_", "")
    um = get_user_manager()

    user = um.get_user(target_id)
    if user:
        um.delete_user(target_id)
        await query.edit_message_text(f"❌ Заявка {user.display_name} отклонена")
        try:
            await bot_state.app.bot.send_message(
                chat_id=int(target_id),
                text="❌ Ваша заявка отклонена.",
            )
        except Exception:
            pass
    else:
        await query.edit_message_text("❌ Пользователь не найден")


# =============================================================================
# АДМИН-ПАНЕЛЬ: РЕФЕРАЛЫ
# =============================================================================

@admin_only
async def cmd_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список рефералов (только для админа)"""
    um = get_user_manager()
    users = um.get_all_users()

    if not users:
        await update.message.reply_text(
            "📋 *Рефералы*\n\nПока нет рефералов.",
            parse_mode='Markdown'
        )
        return

    text = f"📋 *Рефералы ({len(users)})*\n\nВыберите для управления:"
    keyboard = []
    for user in users:
        status_emoji = {"active": "✅", "pending": "⏳", "disabled": "⛔"}.get(user.status, "❓")
        display = user.display_name
        keyboard.append([
            InlineKeyboardButton(f"{status_emoji} {display}", callback_data=f"ref_info_{user.telegram_id}")
        ])
    keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@admin_only_callback
async def callback_ref_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список рефералов (inline callback)"""
    query = update.callback_query
    await query.answer()

    um = get_user_manager()
    users = um.get_all_users()

    if not users:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(
            "📋 *Рефералы*\n\nПока нет рефералов.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return

    text = f"📋 *Рефералы ({len(users)})*\n\nВыберите для управления:"
    keyboard = []
    for user in users:
        status_emoji = {"active": "✅", "pending": "⏳", "disabled": "⛔"}.get(user.status, "❓")
        display = user.display_name
        keyboard.append([
            InlineKeyboardButton(f"{status_emoji} {display}", callback_data=f"ref_info_{user.telegram_id}")
        ])
    keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@admin_only_callback
async def callback_ref_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Детальная информация о реферале"""
    query = update.callback_query
    await query.answer()

    target_id = query.data.replace("ref_info_", "")
    um = get_user_manager()
    user = um.get_user(target_id)

    if not user:
        await query.edit_message_text("❌ Пользователь не найден")
        return

    status_text = {
        "active": "✅ Активен", "pending": "⏳ Ожидает", "disabled": "⛔ Отключен"
    }.get(user.status, "❓")

    accounts = um.load_user_accounts(target_id)
    running_count = sum(
        1 for acc in accounts
        if bot_state.running.get(acc.predict_account_address, False)
    )
    trade_settings = get_user_trade_settings(target_id)

    # Баланс
    balance_text = ""
    for acc in accounts:
        try:
            trader = PredictTrader(market_ids=None, monitor_mode=True, account=acc)
            if await asyncio.to_thread(trader.init_sdk):
                balance = await asyncio.to_thread(trader.get_usdt_balance)
                balance_text += f"\n   💵 {md(acc.name)}: ${balance:.2f}"
        except Exception:
            balance_text += f"\n   💵 {md(acc.name)}: ❌"

    safe_display_name = md(user.display_name)

    text = (
        f"👤 *{safe_display_name}*\n"
        f"🆔 `{user.telegram_id}`\n\n"
        f"📊 Статус: {status_text}\n"
        f"📅 Присоединился: {user.joined_at[:10] if user.joined_at else 'N/A'}\n"
        f"👥 Аккаунтов: {len(accounts)}\n"
        f"🟢 Запущено: {running_count}\n"
        f"🎯 ASK offset: {trade_settings['ask_position_offset']}\n"
        f"⏳ Задержка после отмены: {trade_settings['reposition_delay']}с"
    )
    if balance_text:
        text += f"\n{balance_text}"

    keyboard = []
    if user.status == 'active':
        keyboard.append([InlineKeyboardButton("⛔ Отключить", callback_data=f"ref_disable_{target_id}")])
    elif user.status == 'disabled':
        keyboard.append([InlineKeyboardButton("✅ Подключить", callback_data=f"ref_enable_{target_id}")])
    elif user.status == 'pending':
        keyboard.append([
            InlineKeyboardButton("✅ Одобрить", callback_data=f"admin_approve_{target_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_{target_id}"),
        ])

    keyboard.append([InlineKeyboardButton("� Логи", callback_data=f"ref_logs_{target_id}")])
    keyboard.append([InlineKeyboardButton("�🗑 Удалить", callback_data=f"ref_delete_{target_id}")])
    keyboard.append([InlineKeyboardButton("« К рефералам", callback_data="ref_list")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


@admin_only_callback
async def callback_ref_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр логов реферала (последние события переставления ордеров)"""
    query = update.callback_query
    await query.answer()

    target_id = query.data.replace("ref_logs_", "")
    um = get_user_manager()
    user = um.get_user(target_id)

    if not user:
        await query.edit_message_text("❌ Пользователь не найден")
        return

    events = bot_state.get_user_events(target_id, limit=30)

    safe_name = md(user.display_name)
    if not events:
        text = (
            f"📋 *Логи: {safe_name}*\n\n"
            "Нет событий. Логи появятся после старта бота "
            "и активных переставлений ордеров."
        )
    else:
        lines = [f"📋 *Логи: {safe_name}*", f"Последние {len(events)} событий:", ""]
        for event in events:
            time_str = event['time']
            msg = md(event['message'])
            if event['type'] == 'reposition':
                lines.append(f"🔄 `{time_str}` {msg}")
            elif event['type'] == 'error':
                lines.append(f"🚨 `{time_str}` {msg}")
            else:
                lines.append(f"ℹ️ `{time_str}` {msg}")
        text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"ref_logs_{target_id}")],
        [InlineKeyboardButton("« К рефералу", callback_data=f"ref_info_{target_id}")],
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


@admin_only_callback
async def callback_ref_disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отключить реферала"""
    query = update.callback_query
    await query.answer()

    target_id = query.data.replace("ref_disable_", "")
    um = get_user_manager()
    user = um.get_user(target_id)

    if not user:
        await query.edit_message_text("❌ Пользователь не найден")
        return

    # Останавливаем трейдеров пользователя
    user_accounts = um.load_user_accounts(target_id)
    for acc in user_accounts:
        if bot_state.running.get(acc.predict_account_address, False):
            await stop_monitoring(acc)

    um.disable_user(target_id)

    await query.edit_message_text(f"⛔ Пользователь {user.display_name} отключен")

    # Уведомляем пользователя
    try:
        await bot_state.app.bot.send_message(
            chat_id=int(target_id),
            text=f"⛔ {DISCONNECT_MESSAGE}",
        )
    except Exception:
        pass


@admin_only_callback
async def callback_ref_enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включить реферала обратно"""
    query = update.callback_query
    await query.answer()

    target_id = query.data.replace("ref_enable_", "")
    um = get_user_manager()

    if um.enable_user(target_id):
        user = um.get_user(target_id)
        await query.edit_message_text(f"✅ Пользователь {user.display_name} подключен обратно")
        try:
            await bot_state.app.bot.send_message(
                chat_id=int(target_id),
                text="✅ Ваш доступ к боту восстановлен! Нажмите /start",
            )
        except Exception:
            pass
    else:
        await query.edit_message_text("❌ Пользователь не найден")


@admin_only_callback
async def callback_ref_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления реферала"""
    query = update.callback_query
    await query.answer()

    target_id = query.data.replace("ref_delete_", "")
    um = get_user_manager()
    user = um.get_user(target_id)

    if not user:
        await query.edit_message_text("❌ Пользователь не найден")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"ref_confirm_delete_{target_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"ref_info_{target_id}"),
        ]
    ])

    await query.edit_message_text(
        f"⚠️ Удалить пользователя *{user.display_name}*?\n\n"
        "Все его данные будут утеряны!",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )


@admin_only_callback
async def callback_ref_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждённое удаление реферала"""
    query = update.callback_query
    await query.answer()

    target_id = query.data.replace("ref_confirm_delete_", "")
    um = get_user_manager()
    user = um.get_user(target_id)

    if not user:
        await query.edit_message_text("❌ Пользователь не найден")
        return

    # Останавливаем трейдеров
    user_accounts = um.load_user_accounts(target_id)
    for acc in user_accounts:
        if bot_state.running.get(acc.predict_account_address, False):
            await stop_monitoring(acc)

    display = user.display_name
    um.delete_user(target_id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("« К рефералам", callback_data="ref_list")]
    ])
    await query.edit_message_text(
        f"🗑 Пользователь {display} удалён",
        reply_markup=keyboard
    )


# =============================================================================
# МЕНЮ: ПОМОЩЬ
# =============================================================================

async def show_help(query):
    """Показать помощь"""
    text = (
        "📖 *Справка по боту*\n\n"
        "*Стратегия SPLIT:*\n"
        "1️⃣ Покупаем YES + NO токены одновременно\n"
        "2️⃣ Выставляем SELL ордера\n"
        "3️⃣ Фармим поинты пока ордера в книге\n"
        "4️⃣ При любом исходе выходим в 0 или +\n\n"
        "*Как начать:*\n"
        "1. Добавьте аккаунт: 👥 Аккаунты → Добавить\n"
        "2. Запустите мониторинг: ▶️ Запустить бота\n"
        "3. Делайте SPLIT: ⚡ SPLIT\n\n"
        "*Важно:*\n"
        "• Maker Fee = 0% (бесплатно!)\n"
        "• Поинты начисляются за ордера в книге\n"
        "• Ближе к рынку = больше поинтов"
    )
    
    keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


# =============================================================================
# МЕНЮ: АККАУНТЫ
# =============================================================================

async def show_accounts(query):
    """Показать список аккаунтов"""
    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)
    
    keyboard = []
    
    for i, acc in enumerate(accounts):
        addr_short = f"{acc.predict_account_address[:6]}...{acc.predict_account_address[-4:]}"
        is_running = bot_state.running.get(acc.predict_account_address, False)
        status = "🟢" if is_running else "⚪"
        keyboard.append([
            InlineKeyboardButton(f"{status} {acc.name} ({addr_short})", callback_data=f"acc_info_{i}")
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Добавить аккаунт", callback_data="acc_add")])
    keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
    
    text = f"👥 *Аккаунты ({len(accounts)})*\n\nВыберите аккаунт или добавьте новый:"
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


# =============================================================================
# МЕНЮ: РЫНКИ (портфолио)
# =============================================================================

async def show_markets(query, context):
    """Показать рынки из портфолио (с позициями/ордерами)"""
    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)
    
    if not accounts:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(
            "📭 Нет аккаунтов",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # Если несколько аккаунтов — показываем выбор
    if len(accounts) > 1:
        keyboard = []
        for i, acc in enumerate(accounts):
            keyboard.append([
                InlineKeyboardButton(f"👤 {acc.name}", callback_data=f"markets_acc_{i}")
            ])
        keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
        
        await query.edit_message_text(
            "📈 *Рынки (портфолио)*\n\nВыберите аккаунт:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return
    
    # Один аккаунт — сразу показываем рынки
    await show_markets_for_account(query, context, accounts[0])


async def show_markets_for_account(query, context, acc: AccountConfig):
    """Показать рынки конкретного аккаунта"""
    await query.edit_message_text(f"⏳ Загрузка портфолио {acc.name}...")
    
    try:
        trader = PredictTrader(
            market_ids=None,
            monitor_mode=True,
            account=acc
        )
        
        if not await asyncio.to_thread(trader.init_sdk):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Ошибка SDK", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        if not await asyncio.to_thread(trader.authenticate):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Ошибка аутентификации", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        # Загружаем рынки из портфолио (с ордерами и позициями)
        loaded_count = await asyncio.to_thread(trader.load_all_markets)
        
        if not loaded_count or not trader.markets:
            keyboard = [[InlineKeyboardButton("« К аккаунтам", callback_data="menu_markets")],
                        [InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text(f"📭 Нет рынков в портфолио ({acc.name})", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        # Загружаем ордера для детальной информации
        orders = await asyncio.to_thread(trader.api.get_open_orders)
        orders_by_market = {}
        for order in orders:
            if order.market_id not in orders_by_market:
                orders_by_market[order.market_id] = []
            orders_by_market[order.market_id].append(order)
        
        # Сохраняем данные для деталей рынка
        context.user_data['portfolio_markets'] = {}
        context.user_data['portfolio_orders'] = orders_by_market
        
        text = f"📈 *Портфолио ({loaded_count} рынков)*\n"
        text += f"👤 Аккаунт: {acc.name}\n\n"
        
        keyboard = []
        for market_id, state in list(trader.markets.items())[:20]:
            title = state.market.title[:30] if state.market else f"Market {market_id}"
            yes_pos = state.yes_position or 0
            no_pos = state.no_position or 0
            phase = state.split_phase or 0
            
            # Считаем ордера для этого рынка
            market_orders = orders_by_market.get(market_id, [])
            sell_orders = [o for o in market_orders if o.side == 1]  # SELL
            
            # Сохраняем данные
            context.user_data['portfolio_markets'][market_id] = {
                'title': state.market.title if state.market else f"Market {market_id}",
                'yes_pos': yes_pos,
                'no_pos': no_pos,
                'phase': phase,
                'orders': market_orders,
            }
            
            # Формируем текст кнопки
            phase_emoji = "🔴" if phase == 0 else "🟡" if phase == 1 else "🟢"
            orders_text = f"[{len(sell_orders)}]" if sell_orders else ""
            btn_text = f"{phase_emoji} #{market_id}: {title[:20]}.. {orders_text}"
            
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"market_info_{market_id}")])
        
        if loaded_count > 20:
            text += f"\n_Показаны первые 20 из {loaded_count} рынков_\n"
        
        text += "\n_Нажмите на рынок для детальной информации_"
        
        # Кнопка "Назад" зависит от количества аккаунтов
        accounts = load_accounts()
        if len(accounts) > 1:
            keyboard.append([InlineKeyboardButton("« К аккаунтам", callback_data="menu_markets")])
        keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        
    except Exception as e:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))


async def show_market_details(query, context, market_id: int):
    """Показать детальную информацию о рынке с SPLIT и ордерами"""
    market_data = context.user_data.get('portfolio_markets', {}).get(market_id)
    orders_by_market = context.user_data.get('portfolio_orders', {})
    
    if not market_data:
        keyboard = [[InlineKeyboardButton("« Назад", callback_data="menu_markets")]]
        await query.edit_message_text("❌ Данные рынка не найдены", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    title = market_data['title']
    yes_pos = market_data['yes_pos']
    no_pos = market_data['no_pos']
    phase = market_data['phase']
    market_orders = orders_by_market.get(market_id, [])
    
    # Фаза SPLIT
    phase_text = {
        0: "⚪ Не начат",
        1: "🟡 Токены куплены",
        2: "🟢 Ордера выставлены"
    }.get(phase, "❓ Неизвестно")
    
    text = f"📊 *Рынок #{market_id}*\n"
    text += f"📝 {title}\n\n"
    
    # SPLIT информация
    text += f"*⚡ SPLIT статус:*\n"
    text += f"   {phase_text}\n\n"
    
    # Позиции
    text += f"*💰 Позиции:*\n"
    text += f"   YES: {yes_pos:.2f}\n"
    text += f"   NO: {no_pos:.2f}\n"
    total_value = yes_pos + no_pos
    text += f"   _Всего: ~${total_value:.2f}_\n\n"
    
    # Ордера
    text += f"*📋 Ордера ({len(market_orders)}):*\n"
    if market_orders:
        for order in market_orders:
            side_text = "🟢 BUY" if order.side == 0 else "🔴 SELL"
            # Определяем YES/NO по token_id (упрощённо)
            token_text = "YES" if "yes" in str(order.token_id).lower() or order.side == 1 else "NO"
            price_cents = order.price_per_share * 100
            
            # Рассчитываем сумму ордера
            order_value = order.original_quantity * order.price_per_share
            
            text += f"   {side_text} {token_text} @ {price_cents:.1f}¢\n"
            text += f"   📦 Кол-во: {order.original_quantity:.2f} (~${order_value:.2f})\n"
            
            # Показываем заполнение если есть
            filled = order.original_quantity - order.quantity
            if filled > 0.01:
                text += f"   ✅ Заполнено: {filled:.2f}\n"
    else:
        text += "   _Нет открытых ордеров_\n"
    
    keyboard = [
        [InlineKeyboardButton("« К списку рынков", callback_data="menu_markets")],
        [InlineKeyboardButton("« Главное меню", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


# =============================================================================
# МЕНЮ: ЗАКРЫТИЕ ПОЗИЦИИ
# =============================================================================

async def show_close_position(query, context):
    """Показать меню закрытия позиции"""
    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)
    
    if not accounts:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(
            "📭 Нет аккаунтов\n\nДобавьте аккаунт через меню Аккаунты",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # Если несколько аккаунтов - показываем выбор
    if len(accounts) > 1:
        keyboard = []
        for i, acc in enumerate(accounts):
            keyboard.append([
                InlineKeyboardButton(f"👤 {acc.name}", callback_data=f"close_acc_{i}")
            ])
        keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
        
        await query.edit_message_text(
            "🔴 *Закрытие позиции*\n\nВыберите аккаунт:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return
    
    # Один аккаунт - сразу показываем рынки
    context.user_data['close_account_idx'] = 0
    await show_close_markets(query, context, accounts[0])


async def show_close_markets(query, context, acc: AccountConfig):
    """Показать рынки для закрытия"""
    await query.edit_message_text("⏳ Загрузка рынков с позициями...")
    
    try:
        trader = PredictTrader(
            market_ids=None,
            monitor_mode=True,
            account=acc
        )
        
        if not await asyncio.to_thread(trader.init_sdk):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Ошибка SDK", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        if not await asyncio.to_thread(trader.authenticate):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Ошибка аутентификации", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        # Загружаем рынки
        loaded_count = await asyncio.to_thread(trader.load_all_markets)
        
        if not loaded_count or not trader.markets:
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("📭 Нет рынков с позициями", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        # Сохраняем данные рынков для callback
        context.user_data['close_markets'] = {
            mid: {
                'title': state.market.title[:25] if state.market else f"Market {mid}",
                'yes': state.yes_position or 0,
                'no': state.no_position or 0,
                'phase': state.split_phase or 0,
            }
            for mid, state in trader.markets.items()
        }
        
        keyboard = []
        for market_id, state in trader.markets.items():
            title = state.market.title[:20] if state.market else f"#{market_id}"
            yes_pos = state.yes_position or 0
            no_pos = state.no_position or 0
            phase = state.split_phase or 0
            
            # Показываем только рынки с позициями или ордерами
            if yes_pos > 0.1 or no_pos > 0.1 or phase >= 1:
                phase_emoji = "🔴" if phase == 0 else "🟡" if phase == 1 else "🟢"
                btn_text = f"{phase_emoji} #{market_id}: {title}"
                if yes_pos > 0 or no_pos > 0:
                    btn_text += f" (Y:{yes_pos:.0f}/N:{no_pos:.0f})"
                keyboard.append([
                    InlineKeyboardButton(btn_text, callback_data=f"close_market_{market_id}")
                ])
        
        if not keyboard:
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("📭 Нет открытых позиций", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        keyboard.append([InlineKeyboardButton("🔴 Закрыть ВСЕ", callback_data="close_all_markets")])
        keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
        
        await query.edit_message_text(
            f"🔴 *Закрытие позиции - {acc.name}*\n\n"
            f"📊 Рынков: {len(trader.markets)}\n\n"
            "_Выберите рынок для закрытия (отмена ордеров + продажа токенов):_",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
    except Exception as e:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))


@authorized_user_callback
async def callback_close_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор аккаунта для закрытия"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    acc_idx = int(query.data.split('_')[-1])
    accounts = get_accounts_for_user(user_id)
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    context.user_data['close_account_idx'] = acc_idx
    await show_close_markets(query, context, accounts[acc_idx])


@authorized_user_callback
async def callback_close_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение закрытия рынка"""
    query = update.callback_query
    await query.answer()
    
    market_id = int(query.data.split('_')[-1])
    acc_idx = context.user_data.get('close_account_idx', 0)
    
    markets_data = context.user_data.get('close_markets', {})
    market_info = markets_data.get(market_id, {})
    
    title = market_info.get('title', f'Market #{market_id}')
    yes_pos = market_info.get('yes', 0)
    no_pos = market_info.get('no', 0)
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, закрыть", callback_data=f"close_confirm_{market_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data="menu_close")
        ]
    ]
    
    await query.edit_message_text(
        f"⚠️ *Подтверждение закрытия*\n\n"
        f"📊 Рынок: *#{market_id}*\n"
        f"📝 {title}\n\n"
        f"💰 Позиции:\n"
        f"   YES: {yes_pos:.2f}\n"
        f"   NO: {no_pos:.2f}\n\n"
        f"_Будут отменены все ордера и проданы все токены по рыночной цене._\n\n"
        f"*Вы уверены?*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_close_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнение закрытия позиции"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    market_id = int(query.data.split('_')[-1])
    acc_idx = context.user_data.get('close_account_idx', 0)
    
    accounts = get_accounts_for_user(user_id)
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    
    await query.edit_message_text(
        f"⏳ Закрываем позицию на рынке #{market_id}...\n\n"
        f"👤 Аккаунт: {acc.name}"
    )
    
    try:
        trader = PredictTrader(
            market_id=market_id,
            account=acc
        )
        
        if not await asyncio.to_thread(trader.init_sdk):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Ошибка SDK", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        if not await asyncio.to_thread(trader.authenticate):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Ошибка аутентификации", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        if not await asyncio.to_thread(trader.load_market, market_id):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Рынок не найден", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        # Выполняем закрытие
        result = await asyncio.to_thread(trader.close_position, market_id)
        
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        
        if result['success']:
            # Формируем сообщение о результате
            msg_parts = [
                f"✅ *Позиция закрыта!*\n\n"
                f"📊 Рынок: #{market_id}\n"
                f"👤 Аккаунт: {acc.name}\n\n"
                f"📋 Результат:\n"
                f"   Отменено ордеров: {result['cancelled_orders']}\n"
                f"   Продано YES: {result['sold_yes']:.2f}\n"
                f"   Продано NO: {result['sold_no']:.2f}\n"
                f"   Получено: ~${result['usdt_received']:.2f}"
            ]
            
            # Если есть дополнительное сообщение (например, о слишком маленькой позиции)
            if result.get('message') and '\n' in result['message']:
                extra_info = result['message'].split('\n', 1)[1] if '\n' in result['message'] else ''
                if extra_info:
                    msg_parts.append(f"\n\n⚠️ *Примечание:*\n{extra_info}")
            
            await query.edit_message_text(
                ''.join(msg_parts),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                f"⚠️ *Проблема при закрытии*\n\n"
                f"{result['message']}\n\n"
                f"Отменено: {result['cancelled_orders']} ордеров",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            
    except Exception as e:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))


@authorized_user_callback
async def callback_close_all_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение закрытия всех позиций"""
    query = update.callback_query
    await query.answer()
    
    acc_idx = context.user_data.get('close_account_idx', 0)
    markets_data = context.user_data.get('close_markets', {})
    
    total_markets = len(markets_data)
    total_yes = sum(m.get('yes', 0) for m in markets_data.values())
    total_no = sum(m.get('no', 0) for m in markets_data.values())
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, закрыть ВСЕ", callback_data="close_all_confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="menu_close")
        ]
    ]
    
    await query.edit_message_text(
        f"⚠️ *ВНИМАНИЕ! Закрытие ВСЕХ позиций*\n\n"
        f"📊 Рынков: {total_markets}\n"
        f"💰 Всего YES: {total_yes:.1f}\n"
        f"💰 Всего NO: {total_no:.1f}\n\n"
        f"_Все ордера будут отменены, все токены проданы по рыночной цене._\n\n"
        f"*Вы уверены?*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_close_all_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнение закрытия всех позиций"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    acc_idx = context.user_data.get('close_account_idx', 0)
    markets_data = context.user_data.get('close_markets', {})
    
    accounts = get_accounts_for_user(user_id)
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    market_ids = list(markets_data.keys())
    
    await query.edit_message_text(
        f"⏳ Закрываем {len(market_ids)} позиций...\n\n"
        f"👤 Аккаунт: {acc.name}"
    )
    
    try:
        trader = PredictTrader(
            market_ids=None,
            monitor_mode=True,
            account=acc
        )
        
        if not await asyncio.to_thread(trader.init_sdk):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Ошибка SDK", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        if not await asyncio.to_thread(trader.authenticate):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Ошибка аутентификации", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        # Закрываем все рынки по очереди
        total_cancelled = 0
        total_yes_sold = 0.0
        total_no_sold = 0.0
        total_usdt = 0.0
        errors = 0
        
        for i, market_id in enumerate(market_ids):
            await query.edit_message_text(
                f"⏳ Закрываем позиции...\n\n"
                f"📊 Прогресс: {i+1}/{len(market_ids)}\n"
                f"🔄 Рынок #{market_id}"
            )
            
            try:
                # Загружаем рынок
                if await asyncio.to_thread(trader.load_market, market_id):
                    result = await asyncio.to_thread(trader.close_position, market_id)
                    
                    total_cancelled += result.get('cancelled_orders', 0)
                    total_yes_sold += result.get('sold_yes', 0)
                    total_no_sold += result.get('sold_no', 0)
                    total_usdt += result.get('usdt_received', 0)
                else:
                    errors += 1
            except Exception as e:
                errors += 1
            
            await asyncio.sleep(0.5)  # Пауза между рынками
        
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        
        await query.edit_message_text(
            f"✅ *Все позиции закрыты!*\n\n"
            f"👤 Аккаунт: {acc.name}\n"
            f"📊 Обработано рынков: {len(market_ids)}\n\n"
            f"📋 Итого:\n"
            f"   Отменено ордеров: {total_cancelled}\n"
            f"   Продано YES: {total_yes_sold:.2f}\n"
            f"   Продано NO: {total_no_sold:.2f}\n"
            f"   Получено: ~${total_usdt:.2f}\n"
            f"   Ошибок: {errors}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        
    except Exception as e:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))


# =============================================================================
# МЕНЮ: ПЕРЕЗАГРУЗКА
# =============================================================================

async def show_restart(query, context):
    """Показать меню перезагрузки"""
    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)
    running_count = sum(1 for acc in accounts if bot_state.running.get(acc.predict_account_address, False))
    
    last_restart = "Никогда"
    if bot_state.last_restart_time:
        last_restart = bot_state.last_restart_time.strftime('%Y-%m-%d %H:%M:%S')
    
    keyboard = [
        [InlineKeyboardButton("🔄 Перезагрузить сейчас", callback_data="restart_now")],
        [InlineKeyboardButton("« Главное меню", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(
        f"🔄 *Перезагрузка бота*\n\n"
        f"📊 Запущено аккаунтов: {running_count}\n"
        f"🕐 Последняя перезагрузка: {last_restart}\n"
        f"⏰ Автообновление: каждый час (git pull + restart)\n\n"
        f"_Перезагрузка остановит все мониторинги и запустит их заново._",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_restart_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перезагрузить бота прямо сейчас"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text("⏳ Перезагрузка бота...")
    
    try:
        await restart_all_traders(context, manual=True)
        
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(
            f"✅ *Перезагрузка завершена!*\n\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    except Exception as e:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))


async def restart_all_traders(context, manual: bool = False):
    """Перезагрузить всех трейдеров (все пользователи)"""
    # Собираем все запущенные аккаунты со владельцами
    running_entries = []  # (acc, owner_id)
    all_running = get_all_running_accounts()
    for acc, owner_id in all_running:
        if bot_state.running.get(acc.predict_account_address, False):
            running_entries.append((acc, owner_id))
    
    # Останавливаем всех
    for acc, owner_id in running_entries:
        addr = acc.predict_account_address
        if addr in bot_state.tasks:
            bot_state.tasks[addr].cancel()
            try:
                await bot_state.tasks[addr]
            except asyncio.CancelledError:
                pass
            del bot_state.tasks[addr]
        bot_state.running[addr] = False
        if addr in bot_state.traders:
            del bot_state.traders[addr]
    
    await asyncio.sleep(2)  # Пауза между остановкой и запуском
    
    # Запускаем заново
    restarted = 0
    for acc, owner_id in running_entries:
        try:
            if await start_monitoring(acc, context, owner_id=owner_id):
                restarted += 1
        except Exception as e:
            logger.error(f"Failed to restart {acc.name}: {e}")
    
    bot_state.last_restart_time = datetime.now()
    
    # Отправляем уведомление админу
    if not manual:
        try:
            await bot_state.app.bot.send_message(
                chat_id=int(TELEGRAM_ADMIN_ID),
                text=(
                    f"🔄 *Автоматическая перезагрузка*\n\n"
                    f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"📊 Перезапущено: {restarted} аккаунтов\n\n"
                    f"_Следующая перезагрузка через 1 час_"
                ),
                parse_mode='Markdown',
                disable_notification=True  # Тихое уведомление
            )
        except Exception as e:
            logger.error(f"Failed to send restart notification: {e}")
    
    logger.info(f"Restarted {restarted} traders")
    return restarted


async def scheduled_restart(context):
    """Плановая перезагрузка (вызывается scheduler'ом)"""
    logger.info("Scheduled restart triggered")
    await restart_all_traders(context, manual=False)


async def scheduled_auto_update(context):
    """
    Автоматическое обновление из git + перезапуск (раз в час)
    
    1. Выполняет `git pull` для получения последних изменений
    2. Если есть изменения — перезапускает процесс
    3. Если нет изменений — просто перезагружает трейдеров
    """
    import subprocess
    
    logger.info("🔄 Scheduled auto-update triggered")
    
    try:
        # Определяем директорию проекта
        project_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Выполняем git pull
        git_result = subprocess.run(
            ['git', 'pull', '--ff-only'],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        
        git_output = git_result.stdout.strip()
        git_stderr = git_result.stderr.strip()
        
        logger.info(f"git pull output: {git_output}")
        if git_stderr:
            logger.info(f"git pull stderr: {git_stderr}")
        
        has_updates = git_result.returncode == 0 and 'Already up to date' not in git_output
        
        if has_updates:
            # Есть обновления — нужен полный перезапуск процесса
            logger.info("📦 Обнаружены обновления кода!")
            
            # Уведомляем администратора
            if TELEGRAM_ADMIN_ID and bot_state.app:
                try:
                    await bot_state.app.bot.send_message(
                        chat_id=int(TELEGRAM_ADMIN_ID),
                        text=(
                            f"📦 *Обновление кода*\n\n"
                            f"```\n{git_output[:500]}\n```\n\n"
                            f"🔄 Перезапуск бота...\n"
                            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        ),
                        parse_mode='Markdown',
                        disable_notification=False,  # Громкое уведомление
                    )
                except Exception as e:
                    logger.error(f"Failed to send update notification: {e}")
            
            # Обновляем зависимости если нужно
            try:
                pip_result = subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt', '--quiet'],
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if pip_result.returncode == 0:
                    logger.info("✅ Зависимости обновлены")
                else:
                    logger.warning(f"⚠️ pip install: {pip_result.stderr[:200]}")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось обновить зависимости: {e}")
            
            # Сохраняем состояние перед перезапуском
            bot_state.persistent.save()
            
            # Перезапускаем процесс (systemd или напрямую)
            logger.info("🔄 Перезапуск процесса...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
            
        else:
            # Нет обновлений — просто перезагружаем трейдеров
            logger.info("✅ Код актуален, перезагружаем трейдеров...")
            await restart_all_traders(context, manual=False)
            
            if TELEGRAM_ADMIN_ID and bot_state.app:
                try:
                    await bot_state.app.bot.send_message(
                        chat_id=int(TELEGRAM_ADMIN_ID),
                        text=(
                            f"🔄 *Плановая перезагрузка*\n\n"
                            f"📦 Код актуален (обновлений нет)\n"
                            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"_Следующая проверка через 1 час_"
                        ),
                        parse_mode='Markdown',
                        disable_notification=True,
                    )
                except Exception as e:
                    logger.error(f"Failed to send restart notification: {e}")
        
    except subprocess.TimeoutExpired:
        logger.error("git pull timeout!")
        await restart_all_traders(context, manual=False)
    except FileNotFoundError:
        logger.warning("git не найден, пропускаем обновление, просто перезагружаем трейдеров")
        await restart_all_traders(context, manual=False)
    except Exception as e:
        logger.error(f"Ошибка автообновления: {e}")
        # Даже при ошибке пробуем перезагрузить трейдеров
        try:
            await restart_all_traders(context, manual=False)
        except Exception as e2:
            logger.error(f"Ошибка перезагрузки после неудачного обновления: {e2}")


# =============================================================================
# МЕНЮ: SPLIT
# =============================================================================

async def show_split(query):
    """Показать меню SPLIT"""
    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)
    
    if not accounts:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(
            "📭 Нет аккаунтов\n\nДобавьте аккаунт через меню Аккаунты",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    keyboard = []
    for i, acc in enumerate(accounts):
        keyboard.append([
            InlineKeyboardButton(f"👤 {acc.name}", callback_data=f"split_acc_{i}")
        ])
    
    keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
    
    await query.edit_message_text(
        "⚡ *SPLIT*\n\nВыберите аккаунт для операции:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


# =============================================================================
# МЕНЮ: ЗАПУСК/ОСТАНОВКА БОТА
# =============================================================================

async def show_start_bot(query, context):
    """Показать меню запуска бота"""
    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)
    
    if not accounts:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(
            "📭 Добавьте аккаунт через меню Аккаунты",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    keyboard = []
    for i, acc in enumerate(accounts):
        is_running = bot_state.running.get(acc.predict_account_address, False)
        status = "🟢" if is_running else "⚪"
        keyboard.append([
            InlineKeyboardButton(f"{status} {acc.name}", callback_data=f"start_acc_{i}")
        ])
    
    keyboard.append([InlineKeyboardButton("▶️ Запустить все", callback_data="start_all")])
    keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
    
    await query.edit_message_text(
        "▶️ *Запуск мониторинга*\n\nВыберите аккаунт для запуска:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def show_stop_bot(query):
    """Показать меню остановки бота"""
    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)
    running_accounts = [acc for acc in accounts if bot_state.running.get(acc.predict_account_address, False)]
    
    if not running_accounts:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(
            "⚪ Нет запущенных аккаунтов",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    keyboard = []
    for i, acc in enumerate(accounts):
        if bot_state.running.get(acc.predict_account_address, False):
            keyboard.append([
                InlineKeyboardButton(f"🟢 {acc.name}", callback_data=f"stop_acc_{i}")
            ])
    
    keyboard.append([InlineKeyboardButton("⏹ Остановить все", callback_data="stop_all")])
    keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
    
    await query.edit_message_text(
        "⏹ *Остановка мониторинга*\n\nВыберите аккаунт для остановки:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


# =============================================================================
# КОМАНДЫ (оставляем для совместимости)
# =============================================================================

@authorized_user
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    await update.message.reply_text(
        "📖 *Как работает бот?*\n\n"
        "*Стратегия SPLIT:*\n"
        "1️⃣ Покупаем YES + NO токены одновременно\n"
        "2️⃣ Выставляем SELL ордера\n"
        "3️⃣ Фармим поинты пока ордера в книге\n"
        "4️⃣ При любом исходе выходим в 0 или +\n\n"
        "*Автоматизация:*\n"
        "🔄 Автообновление каждый час (git pull + restart)\n"
        "⚡ WebSocket мониторинг — мгновенная реакция\n"
        "📊 Автоматически переставляет ордера\n"
        "🏁 Авто-merge при закрытии рынка\n\n"
        "*Команды:*\n"
        "/markets — список рынков\n"
        "/refresh — обновить список рынков\n"
        "/split — запустить SPLIT\n"
        "/balance — баланс аккаунтов\n\n"
        "*Важно:*\n"
        "• Maker Fee = 0% (бесплатно!)\n"
        "• Поинты начисляются за ордера в книге\n"
        "• Ближе к рынку = больше поинтов",
        parse_mode='Markdown'
    )


@authorized_user
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /status - показать статус"""
    user_id = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id)
    
    text = "📊 *Статус бота*\n\n"
    text += f"🔗 Сеть: {'BNB Mainnet' if CHAIN_ID == ChainId.BNB_MAINNET else 'Testnet'}\n"
    text += f"🔑 API Key: {'✅' if API_KEY else '❌'}\n"
    text += f" Offset: {SPLIT_OFFSET*100:.1f}%\n\n"
    
    text += f"👥 *Аккаунты ({len(accounts)}):*\n"
    
    for acc in accounts:
        addr_short = f"{acc.predict_account_address[:6]}...{acc.predict_account_address[-4:]}"
        is_running = bot_state.running.get(acc.predict_account_address, False)
        status = "🟢 Работает" if is_running else "⚪ Остановлен"
        text += f"• {acc.name} ({addr_short}) - {status}\n"
    
    if not accounts:
        text += "📭 Нет аккаунтов\n"
    
    keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


@authorized_user
async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /accounts - управление аккаунтами"""
    user_id = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id)
    
    keyboard = []
    
    for i, acc in enumerate(accounts):
        addr_short = f"{acc.predict_account_address[:6]}...{acc.predict_account_address[-4:]}"
        keyboard.append([
            InlineKeyboardButton(f"👤 {acc.name} ({addr_short})", callback_data=f"acc_info_{i}")
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Добавить аккаунт", callback_data="acc_add")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = f"👥 *Аккаунты ({len(accounts)})*\n\nВыберите аккаунт или добавьте новый:"
    
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')


@authorized_user_callback
async def callback_account_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Информация об аккаунте"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    acc_idx = int(query.data.split('_')[-1])
    accounts = get_accounts_for_user(user_id)
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    addr_short = f"{acc.predict_account_address[:6]}...{acc.predict_account_address[-4:]}"
    is_running = bot_state.running.get(acc.predict_account_address, False)
    
    # Получаем баланс
    balance_text = "❓"
    try:
        trader = await asyncio.to_thread(get_or_create_trader, acc)
        if trader:
            balance = await asyncio.to_thread(trader.get_usdt_balance)
            balance_text = f"${balance:.2f}"
    except:
        pass
    
    text = f"👤 *{acc.name}*\n\n"
    text += f"📍 Адрес: `{acc.predict_account_address}`\n"
    text += f"💰 Баланс: {balance_text}\n"
    text += f"📊 Статус: {'🟢 Работает' if is_running else '⚪ Остановлен'}\n"
    
    keyboard = [
        [
            InlineKeyboardButton("▶️ Запустить" if not is_running else "⏹ Остановить", 
                               callback_data=f"acc_toggle_{acc_idx}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"acc_delete_{acc_idx}")
        ],
        [InlineKeyboardButton("« Назад", callback_data="acc_back")]
    ]
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


@authorized_user_callback
async def callback_account_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск/остановка аккаунта"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    acc_idx = int(query.data.split('_')[-1])
    accounts = get_accounts_for_user(user_id)
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    is_running = bot_state.running.get(acc.predict_account_address, False)
    
    if is_running:
        # Останавливаем
        await stop_monitoring(acc)
        await query.edit_message_text(f"⏹ Мониторинг для *{acc.name}* остановлен", parse_mode='Markdown')
    else:
        # Запускаем
        await query.edit_message_text(f"▶️ Запускаем мониторинг для *{acc.name}*...", parse_mode='Markdown')
        success = await start_monitoring(acc, context, owner_id=user_id)
        if success:
            await query.edit_message_text(f"✅ Мониторинг для *{acc.name}* запущен!", parse_mode='Markdown')
        else:
            await query.edit_message_text(f"❌ Ошибка запуска для *{acc.name}*", parse_mode='Markdown')


@authorized_user_callback
async def callback_account_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление аккаунта"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    acc_idx = int(query.data.split('_')[-1])
    accounts = get_accounts_for_user(user_id)
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"acc_confirm_delete_{acc_idx}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"acc_info_{acc_idx}")
        ]
    ]
    
    await query.edit_message_text(
        f"⚠️ Удалить аккаунт *{acc.name}*?\n\nЭто действие нельзя отменить!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_account_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    acc_idx = int(query.data.split('_')[-1])
    accounts = get_accounts_for_user(user_id)
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    
    # Останавливаем если работает
    await stop_monitoring(acc)
    
    # Удаляем
    accounts.pop(acc_idx)
    save_accounts_for_user(user_id, accounts)
    
    await query.edit_message_text(f"🗑 Аккаунт *{acc.name}* удалён", parse_mode='Markdown')


@authorized_user_callback
async def callback_add_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления аккаунта"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "➕ *Добавление аккаунта*\n\n"
        "Введите имя для аккаунта (например: Main, Account2):",
        parse_mode='Markdown'
    )
    
    return STATE_WAITING_NAME


async def handle_account_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение имени аккаунта"""
    name = update.message.text.strip()
    context.user_data['new_account_name'] = name
    
    await update.message.reply_text(
        f"✅ Имя: *{name}*\n\n"
        "Теперь введите *PRIVY\\_WALLET\\_PRIVATE\\_KEY*\n"
        "(экспортировать из https://predict.fun/account/settings):",
        parse_mode='Markdown'
    )
    
    return STATE_WAITING_PRIVATE_KEY


async def handle_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение приватного ключа"""
    private_key = update.message.text.strip()
    
    # Удаляем сообщение с ключом для безопасности
    try:
        await update.message.delete()
    except:
        pass
    
    if not private_key.startswith('0x'):
        private_key = '0x' + private_key
    
    context.user_data['new_account_key'] = private_key
    
    await update.message.reply_text(
        "✅ Ключ получен (сообщение удалено)\n\n"
        "Теперь введите *PREDICT\\_ACCOUNT\\_ADDRESS*\n"
        "(адрес вашего Predict Account):",
        parse_mode='Markdown'
    )
    
    return STATE_WAITING_ADDRESS


async def handle_account_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение адреса аккаунта"""
    address = update.message.text.strip()
    
    if not address.startswith('0x'):
        address = '0x' + address
    
    name = context.user_data.get('new_account_name', 'Account')
    private_key = context.user_data.get('new_account_key', '')
    
    # Создаём аккаунт
    new_account = AccountConfig(
        name=name,
        private_key=private_key,
        predict_account_address=address
    )
    
    # Сохраняем
    user_id = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id)
    
    # Лимит аккаунтов для рефералов
    if not is_admin_user(user_id) and len(accounts) >= MAX_USER_ACCOUNTS:
        await update.message.reply_text(f"❌ Максимум {MAX_USER_ACCOUNTS} аккаунт(ов).")
        context.user_data.clear()
        return ConversationHandler.END
    
    accounts.append(new_account)
    save_accounts_for_user(user_id, accounts)
    
    # Очищаем данные
    context.user_data.clear()
    
    await update.message.reply_text(
        f"✅ *Аккаунт добавлен!*\n\n"
        f"👤 Имя: {name}\n"
        f"📍 Адрес: `{address[:10]}...{address[-6:]}`\n\n"
        "Используйте /accounts для управления",
        parse_mode='Markdown'
    )
    
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена диалога"""
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено")
    return ConversationHandler.END


@authorized_user_callback
async def callback_back_to_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к списку аккаунтов"""
    query = update.callback_query
    await query.answer()
    
    # Эмулируем /accounts
    user_id = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id)
    
    keyboard = []
    for i, acc in enumerate(accounts):
        addr_short = f"{acc.predict_account_address[:6]}...{acc.predict_account_address[-4:]}"
        is_running = bot_state.running.get(acc.predict_account_address, False)
        status = "🟢" if is_running else "⚪"
        keyboard.append([
            InlineKeyboardButton(f"{status} {acc.name} ({addr_short})", callback_data=f"acc_info_{i}")
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Добавить аккаунт", callback_data="acc_add")])
    keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
    
    await query.edit_message_text(
        f"👥 *Аккаунты ({len(accounts)})*\n\nВыберите аккаунт или добавьте новый:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /balance - показать балансы"""
    user_id = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id)
    
    if not accounts:
        await update.message.reply_text("📭 Нет аккаунтов. Добавьте через /accounts")
        return
    
    msg = await update.message.reply_text("⏳ Загрузка балансов...")
    
    text = "💰 *Балансы*\n\n"
    
    for acc in accounts:
        try:
            trader = get_or_create_trader(acc, user_id)
            if trader and await asyncio.to_thread(trader.init_sdk):
                balance = await asyncio.to_thread(trader.get_usdt_balance)

                points_text = "н/д"
                if not trader.api.jwt_token:
                    await asyncio.to_thread(trader.authenticate)
                points_value = await asyncio.to_thread(trader.get_predict_points)
                if points_value is not None:
                    points_text = f"{points_value:,.1f} PP"
                else:
                    est_points = await asyncio.to_thread(trader.points.estimate_points)
                    points_text = f"~{est_points:,.1f} PP"

                text += f"• {md(acc.name)}: *${balance:.2f}* USDT\n"
                text += f"  💎 {md(points_text)}\n"
            else:
                text += f"• {md(acc.name)}: ❌ Ошибка\n"
        except Exception as e:
            text += f"• {md(acc.name)}: ❌ {md(str(e)[:30])}\n"
    
    await msg.edit_text(text, parse_mode='Markdown')


@authorized_user
async def cmd_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /markets - меню рынков: обзор и мои позиции"""
    user_id = str(update.effective_user.id)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌍 Обзор рынков", callback_data="browse_markets_all")],
        [InlineKeyboardButton("🔥 Boosted рынки", callback_data="browse_markets_boost")],
        [InlineKeyboardButton("🏆 Мульти-события (NegRisk)", callback_data="browse_events")],
        [InlineKeyboardButton("📊 Мои позиции", callback_data="my_positions")],
    ])
    await update.message.reply_text(
        "📈 *Рынки*\n\n"
        "🌍 *Обзор* — бинарные рынки по категориям с объёмами\n"
        "🔥 *Boosted* — рынки с повышенными PP\n"
        "🏆 *Мульти-события* — события с несколькими исходами (FIFA, NBA и т.д.)\n"
        "📊 *Мои позиции* — рынки, где есть ваши ордера/позиции",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )


# =============================================================================
# ОБЗОР РЫНКОВ (Browse Markets) - категории + объёмы
# =============================================================================

BROWSE_CATEGORY_NAMES = {
    'sports': '🏆 Спорт',
    'politics': '🏛️ Политика',
    'crypto': '💰 Крипто',
    'entertainment': '🎬 Развлечения',
    'science': '🔬 Наука',
    'economy': '📈 Экономика',
    'news': '📰 Новости',
    'culture': '🎭 Культура',
    '': '📋 Другое'
}

BROWSE_CATEGORY_ORDER = ['crypto', 'politics', 'economy', 'sports', 'entertainment', 'science', 'news', 'culture', '']


def _classify_category(slug: str) -> str:
    """Определить ключ категории по category_slug рынка."""
    cat = slug.lower() if slug else ''
    if 'sport' in cat:
        return 'sports'
    if 'politic' in cat:
        return 'politics'
    if 'crypto' in cat or 'bitcoin' in cat:
        return 'crypto'
    if 'entertain' in cat or 'movie' in cat or 'music' in cat:
        return 'entertainment'
    if 'science' in cat or 'tech' in cat:
        return 'science'
    if 'econom' in cat or 'finance' in cat:
        return 'economy'
    if 'news' in cat:
        return 'news'
    if 'culture' in cat or 'art' in cat:
        return 'culture'
    return ''


def _format_volume(vol: float) -> str:
    """Форматировать объём в человекочитаемый вид."""
    if vol >= 1_000_000:
        return f"${vol / 1_000_000:.1f}M"
    elif vol >= 1_000:
        return f"${vol / 1_000:.0f}K"
    else:
        return f"${vol:.0f}"


@authorized_user_callback
async def callback_browse_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск обзора рынков (все / boosted)"""
    query = update.callback_query
    await query.answer()

    boosted_only = query.data == "browse_markets_boost"
    await _load_browse_markets(query, context, boosted_only=boosted_only)


async def _load_browse_markets(query, context, boosted_only: bool):
    """Загрузить рынки для обзора с категориями и объёмами."""
    label = "🔥 Boosted" if boosted_only else "🌍 Все"
    await query.edit_message_text(f"⏳ Загрузка {label} рынков...")

    try:
        from predict_api import PredictAPI
        api = PredictAPI()

        if boosted_only:
            markets = await asyncio.to_thread(api.get_boosted_markets_for_split, 50)
        else:
            markets = await asyncio.to_thread(api.get_markets_for_split, 100)

        if not markets:
            toggle = "browse_markets_all" if boosted_only else "browse_markets_boost"
            toggle_text = "📋 Все рынки" if boosted_only else "🔥 Boosted рынки"
            keyboard = [
                [InlineKeyboardButton(toggle_text, callback_data=toggle)],
                [InlineKeyboardButton("« Назад", callback_data="cmd_markets_menu")],
            ]
            await query.edit_message_text(
                "📭 Нет подходящих рынков",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        # Загружаем объёмы
        await query.edit_message_text(f"⏳ Загрузка объёмов ({len(markets)} рынков)...")

        def _get_volumes(api_inst, ids):
            vols = {}
            for mid in ids:
                try:
                    stats = api_inst.get_market_stats(mid)
                    vols[mid] = stats.get('volume_total', 0)
                except Exception:
                    vols[mid] = 0
            return vols

        market_ids = [m.id for m in markets[:80]]
        volumes = await asyncio.to_thread(_get_volumes, api, market_ids)

        # Группируем по категориям
        categories: dict = {}
        for market in markets:
            cat_key = _classify_category(market.category_slug)
            categories.setdefault(cat_key, []).append(market)

        # Сортируем внутри каждой категории по объёму (убывание)
        for cat_key in categories:
            categories[cat_key].sort(key=lambda m: volumes.get(m.id, 0), reverse=True)

        # Формируем плоский список с заголовками категорий
        all_items = []
        for cat_key in BROWSE_CATEGORY_ORDER:
            if cat_key in categories:
                cat_display = BROWSE_CATEGORY_NAMES.get(cat_key, '📋 Другое')
                all_items.append({'type': 'category', 'name': cat_display, 'count': len(categories[cat_key])})
                for m in categories[cat_key]:
                    all_items.append({'type': 'market', 'market': m})

        # Статистика по категориям
        cat_summary = []
        for cat_key in BROWSE_CATEGORY_ORDER:
            if cat_key in categories:
                name = BROWSE_CATEGORY_NAMES.get(cat_key, 'Другое')
                cnt = len(categories[cat_key])
                total_vol = sum(volumes.get(m.id, 0) for m in categories[cat_key])
                cat_summary.append(f"{name}: {cnt} ({_format_volume(total_vol)})")

        context.user_data['browse_items'] = all_items
        context.user_data['browse_volumes'] = volumes
        context.user_data['browse_boosted'] = boosted_only
        context.user_data['browse_cat_summary'] = cat_summary
        context.user_data['browse_page'] = 0

        await _show_browse_page(query, context, 0)

    except Exception as e:
        keyboard = [[InlineKeyboardButton("« Назад", callback_data="cmd_markets_menu")]]
        await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_browse_page(query, context, page: int):
    """Показать страницу обзора рынков с категориями и объёмами."""
    ITEMS_PER_PAGE = 14

    all_items = context.user_data.get('browse_items', [])
    volumes = context.user_data.get('browse_volumes', {})
    boosted_only = context.user_data.get('browse_boosted', False)
    cat_summary = context.user_data.get('browse_cat_summary', [])

    total_markets = sum(1 for it in all_items if it['type'] == 'market')
    total_pages = max(1, (len(all_items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data['browse_page'] = page

    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = all_items[start:end]

    keyboard = []
    for item in page_items:
        if item['type'] == 'category':
            keyboard.append([
                InlineKeyboardButton(
                    f"━━ {item['name']} ({item['count']}) ━━",
                    callback_data="ignore"
                )
            ])
        else:
            market = item['market']
            display = market.question if market.question and market.question != market.title else market.title
            display = display[:35] if display else f"Market {market.id}"
            vol = _format_volume(volumes.get(market.id, 0))
            badge = "🔥" if market.is_boosted else ""
            keyboard.append([
                InlineKeyboardButton(
                    f"{badge}{display} 📊{vol}",
                    callback_data=f"browse_detail_{market.id}"
                )
            ])

    # Навигация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("« Назад", callback_data=f"browse_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ignore"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"browse_page_{page + 1}"))
    if nav:
        keyboard.append(nav)

    # Переключатель boost / all
    if boosted_only:
        keyboard.append([InlineKeyboardButton("📋 Показать все рынки", callback_data="browse_markets_all")])
    else:
        keyboard.append([InlineKeyboardButton("🔥 Только Boosted", callback_data="browse_markets_boost")])
    keyboard.append([InlineKeyboardButton("« Меню рынков", callback_data="cmd_markets_menu")])

    label = "🔥 Boosted" if boosted_only else "🌍 Все"
    # Краткая сводка категорий на первой странице
    summary_text = ""
    if page == 0 and cat_summary:
        summary_text = "\n".join(cat_summary) + "\n\n"

    await query.edit_message_text(
        f"📈 *Обзор рынков ({label})*\n\n"
        f"{summary_text}"
        f"📊 Всего: {total_markets} рынков | Стр. {page + 1}/{total_pages}\n"
        f"_Категории отсортированы по объёму ↓_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_browse_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение страницы обзора рынков."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split('_')[-1])
    await _show_browse_page(query, context, page)


@authorized_user_callback
async def callback_browse_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать детали рынка из обзора (объём, спред, boost, кнопка SPLIT)."""
    query = update.callback_query
    await query.answer()

    market_id = int(query.data.replace("browse_detail_", ""))
    
    # Ищем рынок в browse_items (бинарные)
    browse_items = context.user_data.get('browse_items', [])
    market = None
    vol = 0
    for item in browse_items:
        if item['type'] == 'market' and item['market'].id == market_id:
            market = item['market']
            volumes = context.user_data.get('browse_volumes', {})
            vol = volumes.get(market_id, 0)
            break

    # Ищем в event_sub_markets (мульти-событие)
    if not market:
        sub_markets = context.user_data.get('event_sub_markets', [])
        sub_volumes = context.user_data.get('event_sub_volumes', {})
        for m in sub_markets:
            if m.get('id') == market_id:
                # Создаём мини-объект для отображения
                market = type('M', (), {
                    'id': m.get('id', 0),
                    'title': m.get('title', ''),
                    'question': m.get('question', ''),
                    'category_slug': m.get('categorySlug', ''),
                    'is_boosted': m.get('isBoosted', False),
                    'is_neg_risk': m.get('isNegRisk', False),
                })()
                vol = sub_volumes.get(market_id, 0)
                break

    if not market:
        keyboard = [[InlineKeyboardButton("« Назад", callback_data=f"browse_page_{context.user_data.get('browse_page', 0)}")]]
        await query.edit_message_text("❌ Рынок не найден", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Загружаем ордербук для отображения спреда
    try:
        from predict_api import PredictAPI
        api = PredictAPI()
        ob = await asyncio.to_thread(api.get_orderbook, market_id)
        spread = ob.spread
        best_bid = ob.best_bid
        best_ask = ob.best_ask
        midpoint = ((best_bid or 0) + (best_ask or 0)) / 2 if best_bid and best_ask else None
    except Exception:
        spread = None
        best_bid = None
        best_ask = None
        midpoint = None

    # Формируем текст
    display_name = getattr(market, 'question', '') or ''
    if not display_name or display_name == getattr(market, 'title', ''):
        display_name = getattr(market, 'title', f'Market {market_id}')
    cat_key = _classify_category(getattr(market, 'category_slug', ''))
    cat_name = BROWSE_CATEGORY_NAMES.get(cat_key, '📋 Другое')

    is_neg_risk_market = getattr(market, 'is_neg_risk', False)

    lines = [
        f"📌 *{md(display_name)}*",
        f"",
        f"🆔 ID: `{market_id}`",
        f"📂 Категория: {cat_name}",
    ]
    if is_neg_risk_market:
        lines.append(f"📊 Объём: *{_format_volume(vol)}*")
        lines.append("🏆 *NegRisk* — мульти-исходный рынок")
    else:
        lines.append(f"📊 Объём: *{_format_volume(vol)}*")

    # Для суб-рынков мульти-события показываем вероятность
    sub_prices = context.user_data.get('event_sub_prices', {})
    last_price = sub_prices.get(market_id)
    if last_price is not None:
        lines.append(f"🎯 Вероятность: *{int(round(last_price * 100))}%*")

    if getattr(market, 'is_boosted', False):
        lines.append("🔥 *PP BOOST активен!*")
    if best_bid is not None and best_ask is not None:
        lines.append(f"")
        lines.append(f"💹 Best BID: {best_bid * 100:.1f}¢")
        lines.append(f"💹 Best ASK: {best_ask * 100:.1f}¢")
        if spread is not None:
            lines.append(f"📏 Спред: {spread * 100:.1f}¢")
        if midpoint is not None:
            lines.append(f"🎯 Midpoint: {midpoint * 100:.1f}¢")

    lines.append("")
    lines.append("_Нажмите SPLIT чтобы внести ликвидность_")

    # Кнопка SPLIT ведёт к выбору аккаунта
    back_data = f"browse_page_{context.user_data.get('browse_page', 0)}"
    if context.user_data.get('event_slug'):
        back_data = f"event_sub_page_{context.user_data.get('event_sub_page', 0)}"
    
    keyboard = [
        [InlineKeyboardButton(f"⚡ SPLIT #{market_id}", callback_data=f"browse_split_acc_{market_id}")],
        [InlineKeyboardButton("« Назад", callback_data=back_data)],
        [InlineKeyboardButton("« Меню рынков", callback_data="cmd_markets_menu")],
    ]

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_browse_split_acc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор аккаунта для SPLIT из обзора рынков."""
    query = update.callback_query
    await query.answer()

    market_id = int(query.data.replace("browse_split_acc_", ""))
    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)

    if not accounts:
        await query.edit_message_text("📭 Добавьте аккаунт через /accounts")
        return

    # Если один аккаунт — сразу переходим к выбору суммы
    if len(accounts) == 1:
        context.user_data['split_account_idx'] = 0
        context.user_data['split_market_id'] = market_id
        keyboard = [
            [
                InlineKeyboardButton("$5", callback_data="split_amount_5"),
                InlineKeyboardButton("$10", callback_data="split_amount_10"),
                InlineKeyboardButton("$25", callback_data="split_amount_25"),
            ],
            [
                InlineKeyboardButton("$50", callback_data="split_amount_50"),
                InlineKeyboardButton("$100", callback_data="split_amount_100"),
            ],
            [InlineKeyboardButton("✏️ Своя сумма", callback_data="split_custom_amount")],
            [InlineKeyboardButton("« Назад", callback_data=f"browse_detail_{market_id}")],
        ]
        await query.edit_message_text(
            f"⚡ *SPLIT — Рынок #{market_id}*\n"
            f"👤 Аккаунт: {accounts[0].name}\n\n"
            f"Выберите сумму:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return

    # Несколько аккаунтов — показываем выбор
    keyboard = []
    for i, acc in enumerate(accounts):
        addr_short = f"{acc.predict_account_address[:6]}...{acc.predict_account_address[-4:]}"
        keyboard.append([
            InlineKeyboardButton(
                f"👤 {acc.name} ({addr_short})",
                callback_data=f"browse_split_pick_{i}_{market_id}"
            )
        ])
    keyboard.append([InlineKeyboardButton("« Назад", callback_data=f"browse_detail_{market_id}")])

    await query.edit_message_text(
        f"⚡ *SPLIT — Рынок #{market_id}*\n\n"
        f"Выберите аккаунт:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_browse_split_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Аккаунт выбран из обзора — переходим к выбору суммы."""
    query = update.callback_query
    await query.answer()

    # Формат: browse_split_pick_{acc_idx}_{market_id}
    parts = query.data.replace("browse_split_pick_", "").split("_", 1)
    acc_idx = int(parts[0])
    market_id = int(parts[1])

    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)

    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return

    context.user_data['split_account_idx'] = acc_idx
    context.user_data['split_market_id'] = market_id

    acc = accounts[acc_idx]
    keyboard = [
        [
            InlineKeyboardButton("$5", callback_data="split_amount_5"),
            InlineKeyboardButton("$10", callback_data="split_amount_10"),
            InlineKeyboardButton("$25", callback_data="split_amount_25"),
        ],
        [
            InlineKeyboardButton("$50", callback_data="split_amount_50"),
            InlineKeyboardButton("$100", callback_data="split_amount_100"),
        ],
        [InlineKeyboardButton("✏️ Своя сумма", callback_data="split_custom_amount")],
        [InlineKeyboardButton("« Назад", callback_data=f"browse_split_acc_{market_id}")],
    ]

    await query.edit_message_text(
        f"⚡ *SPLIT — Рынок #{market_id}*\n"
        f"👤 Аккаунт: {acc.name}\n\n"
        f"Выберите сумму:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_cmd_markets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вернуться в меню рынков."""
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌍 Обзор рынков", callback_data="browse_markets_all")],
        [InlineKeyboardButton("🔥 Boosted рынки", callback_data="browse_markets_boost")],
        [InlineKeyboardButton("🏆 Мульти-события (NegRisk)", callback_data="browse_events")],
        [InlineKeyboardButton("📊 Мои позиции", callback_data="my_positions")],
    ])
    await query.edit_message_text(
        "📈 *Рынки*\n\n"
        "🌍 *Обзор* — бинарные рынки по категориям с объёмами\n"
        "🔥 *Boosted* — рынки с повышенными PP\n"
        "🏆 *Мульти-события* — события с несколькими исходами (FIFA, NBA и т.д.)\n"
        "📊 *Мои позиции* — рынки, где есть ваши ордера/позиции",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_my_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать мои позиции (старая логика cmd_markets)."""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)

    if not accounts:
        keyboard = [[InlineKeyboardButton("« Меню рынков", callback_data="cmd_markets_menu")]]
        await query.edit_message_text("📭 Нет аккаунтов", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await query.edit_message_text("⏳ Загрузка позиций...")

    total_markets = 0
    text = ""

    for acc in accounts:
        try:
            trader = get_or_create_trader(acc)
            if not trader:
                text += f"👤 *{acc.name}* - ❌ Ошибка создания трейдера\n\n"
                continue

            if not await asyncio.to_thread(trader.init_sdk):
                text += f"👤 *{acc.name}* - ❌ Ошибка SDK\n\n"
                continue

            if not await asyncio.to_thread(trader.authenticate):
                text += f"👤 *{acc.name}* - ❌ Ошибка аутентификации\n\n"
                continue

            await asyncio.to_thread(trader.load_all_markets)

            if not trader.markets:
                text += f"👤 *{acc.name}* - 📭 Нет рынков\n\n"
                continue

            all_orders = await asyncio.to_thread(trader.api.get_open_orders)

            orders_by_market = {}
            for order in all_orders:
                if order.market_id not in orders_by_market:
                    orders_by_market[order.market_id] = {'yes': [], 'no': []}
                if state := trader.markets.get(order.market_id):
                    if state.market and state.market.outcomes:
                        yes_token = None
                        no_token = None
                        for outcome in state.market.outcomes:
                            if outcome.is_yes:
                                yes_token = outcome.on_chain_id
                            elif outcome.is_no:
                                no_token = outcome.on_chain_id
                        if order.token_id == yes_token:
                            orders_by_market[order.market_id]['yes'].append(order)
                        elif order.token_id == no_token:
                            orders_by_market[order.market_id]['no'].append(order)

            acc_market_count = len(trader.markets)
            total_markets += acc_market_count
            text += f"👤 *{acc.name}* ({acc_market_count} рынков)\n\n"

            for market_id, state in trader.markets.items():
                title = state.market.title[:35] if state.market else f"#{market_id}"
                phase = state.split_phase
                phase_emoji = "⚪" if phase == 0 else "🟡" if phase == 1 else "🟢"

                text += f"{phase_emoji} *{title}*\n"
                text += f"   YES: {state.yes_position:.1f} | NO: {state.no_position:.1f}\n"

                market_orders = orders_by_market.get(market_id, {'yes': [], 'no': []})
                yes_orders = market_orders['yes']
                no_orders = market_orders['no']

                if yes_orders or no_orders:
                    orders_text = "   📋 Ордера: "
                    parts = []
                    for o in yes_orders:
                        qty = wei_to_float(o.maker_amount)
                        value = qty * o.price_per_share
                        parts.append(f"YES {qty:.1f}шт (${value:.1f})")
                    for o in no_orders:
                        qty = wei_to_float(o.maker_amount)
                        value = qty * o.price_per_share
                        parts.append(f"NO {qty:.1f}шт (${value:.1f})")
                    text += orders_text + ", ".join(parts) + "\n"

            text += "\n"

        except Exception as e:
            text += f"👤 *{acc.name}* - ❌ Ошибка: {e}\n\n"

    header = f"📊 *Мои позиции ({total_markets} рынков)*\n\n"
    text = header + text
    text += "_Фазы: ⚪0=нет токенов, 🟡1=есть токены, 🟢2=ордера выставлены_"

    if len(text) > 4000:
        text = text[:3990] + "..._"

    keyboard = [[InlineKeyboardButton("« Меню рынков", callback_data="cmd_markets_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


# =============================================================================
# МУЛЬТИ-СОБЫТИЯ (NegRisk Events) - FIFA, NBA, EPL и т.д.
# =============================================================================

@authorized_user_callback
async def callback_browse_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список NegRisk мульти-исходных событий."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("⏳ Загрузка мульти-событий...")

    try:
        from predict_api import PredictAPI
        api = PredictAPI()
        events = await asyncio.to_thread(api.get_multi_market_events)

        if not events:
            keyboard = [[InlineKeyboardButton("« Меню рынков", callback_data="cmd_markets_menu")]]
            await query.edit_message_text("📭 Нет доступных мульти-событий", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        # Сортируем по количеству рынков (больше рынков = интереснее)
        events.sort(key=lambda e: len(e.get('markets', [])), reverse=True)

        context.user_data['neg_risk_events'] = events
        context.user_data['events_page'] = 0

        await _show_events_page(query, context, 0)

    except Exception as e:
        keyboard = [[InlineKeyboardButton("« Меню рынков", callback_data="cmd_markets_menu")]]
        await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_events_page(query, context, page: int):
    """Показать страницу списка NegRisk событий."""
    EVENTS_PER_PAGE = 10

    events = context.user_data.get('neg_risk_events', [])
    total_pages = max(1, (len(events) + EVENTS_PER_PAGE - 1) // EVENTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data['events_page'] = page

    start = page * EVENTS_PER_PAGE
    end = start + EVENTS_PER_PAGE
    page_events = events[start:end]

    keyboard = []
    for ev in page_events:
        title = ev.get('title', ev.get('slug', 'Unknown'))[:38]
        markets_count = len(ev.get('markets', []))
        slug = ev.get('slug', '')
        yield_badge = "✅" if ev.get('isYieldBearing', False) else ""
        keyboard.append([
            InlineKeyboardButton(
                f"{yield_badge}🏆 {title} ({markets_count})",
                callback_data=f"event_{slug[:50]}"
            )
        ])

    # Навигация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("« Назад", callback_data=f"events_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ignore"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"events_page_{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("« Меню рынков", callback_data="cmd_markets_menu")])

    await query.edit_message_text(
        f"🏆 *Мульти-события*\n\n"
        f"Всего: {len(events)} событий | Стр. {page + 1}/{total_pages}\n"
        f"_Нажмите на событие чтобы увидеть исходы_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_events_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение страницы событий."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split('_')[-1])
    await _show_events_page(query, context, page)


@authorized_user_callback
async def callback_event_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать суб-рынки (исходы) конкретного события."""
    query = update.callback_query
    await query.answer()

    slug = query.data.replace("event_", "", 1)

    # Ищем событие в сохранённом списке
    events = context.user_data.get('neg_risk_events', [])
    event = None
    for ev in events:
        if ev.get('slug', '')[:50] == slug:
            event = ev
            break

    if not event:
        keyboard = [[InlineKeyboardButton("« К событиям", callback_data="browse_events")]]
        await query.edit_message_text("❌ Событие не найдено", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await query.edit_message_text("⏳ Загрузка исходов и вероятностей...")

    try:
        from predict_api import PredictAPI
        api = PredictAPI()

        # Загружаем полную категорию с рынками
        full_event = await asyncio.to_thread(api.get_category_by_slug, event.get('slug', ''))
        if not full_event:
            full_event = event

        sub_markets = full_event.get('markets', [])
        if not sub_markets:
            keyboard = [[InlineKeyboardButton("« К событиям", callback_data="browse_events")]]
            await query.edit_message_text("📭 Нет исходов в этом событии", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        # Фильтруем только торгуемые рынки (tradingStatus=OPEN)
        sub_markets = [m for m in sub_markets if m.get('tradingStatus') == 'OPEN']

        # Сортируем по questionIndex
        sub_markets.sort(key=lambda m: m.get('questionIndex', 0) or 0)

        # Загружаем объёмы и цены (probability) для суб-рынков
        def _get_sub_data(api_inst, markets):
            volumes = {}
            prices = {}
            for m in markets:
                mid = m.get('id', 0)
                stats = api_inst.get_market_stats(mid)
                volumes[mid] = stats.get('volume_total', 0)
                prices[mid] = api_inst.get_market_last_sale_price(mid)
            return volumes, prices

        volumes, prices = await asyncio.to_thread(_get_sub_data, api, sub_markets)

        context.user_data['event_sub_markets'] = sub_markets
        context.user_data['event_sub_volumes'] = volumes
        context.user_data['event_sub_prices'] = prices
        context.user_data['event_slug'] = event.get('slug', '')
        context.user_data['event_title'] = event.get('title', slug)
        context.user_data['event_sub_page'] = 0

        await _show_event_sub_page(query, context, 0)

    except Exception as e:
        keyboard = [[InlineKeyboardButton("« К событиям", callback_data="browse_events")]]
        await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))


async def _show_event_sub_page(query, context, page: int):
    """Показать страницу суб-рынков (исходов) внутри события."""
    ITEMS_PER_PAGE = 10

    sub_markets = context.user_data.get('event_sub_markets', [])
    volumes = context.user_data.get('event_sub_volumes', {})
    prices = context.user_data.get('event_sub_prices', {})
    event_title = context.user_data.get('event_title', 'Событие')

    total_pages = max(1, (len(sub_markets) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data['event_sub_page'] = page

    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = sub_markets[start:end]

    keyboard = []
    for m in page_items:
        mid = m.get('id', 0)
        title = m.get('title', f'#{mid}')[:24]
        vol = volumes.get(mid, 0)
        price = prices.get(mid)
        boosted = "🔥" if m.get('isBoosted', False) else ""

        # Процент (всегда показываем)
        if price is not None:
            pct_str = f" {int(round(price * 100))}%"
        else:
            pct_str = " —%"

        # Объём (только если > 0)
        vol_str = f" 📊{_format_volume(vol)}" if vol else ""

        keyboard.append([
            InlineKeyboardButton(
                f"{boosted}{title}{pct_str}{vol_str}",
                callback_data=f"browse_detail_{mid}"
            )
        ])

    # Навигация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("« Назад", callback_data=f"event_sub_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="ignore"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"event_sub_page_{page + 1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("« К событиям", callback_data="browse_events")])
    keyboard.append([InlineKeyboardButton("« Меню рынков", callback_data="cmd_markets_menu")])

    await query.edit_message_text(
        f"🏆 *{md(event_title)}*\n\n"
        f"📋 Исходов: {len(sub_markets)} | Стр. {page + 1}/{total_pages}\n"
        f"_Нажмите на исход для деталей и SPLIT_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_event_sub_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение страницы суб-рынков события."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split('_')[-1])
    await _show_event_sub_page(query, context, page)


@authorized_user
async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /refresh - принудительное обновление списка рынков"""
    user_id = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id)
    
    if not accounts:
        await update.message.reply_text("📭 Нет аккаунтов")
        return
    
    msg = await update.message.reply_text("🔄 Обновление списка рынков...")
    
    results = []
    for acc in accounts:
        addr = acc.predict_account_address
        if not bot_state.running.get(addr, False):
            continue
        
        trader = bot_state.traders.get(addr)
        if not trader:
            continue
        
        try:
            old_markets = set(trader.markets.keys())
            loaded = await asyncio.to_thread(trader.refresh_markets)
            new_markets = set(trader.markets.keys())
            
            added = new_markets - old_markets
            removed = old_markets - new_markets
            
            # Подписываемся/отписываемся от WS если есть ws клиент
            ws_client = getattr(trader, '_ws_client', None)
            
            status = f"👤 *{acc.name}*: {loaded} рынков"
            if added:
                status += f"\n   ➕ Новые: {', '.join(f'#{m}' for m in added)}"
            if removed:
                status += f"\n   ➖ Удалены: {', '.join(f'#{m}' for m in removed)}"
            if not added and not removed:
                status += " (без изменений)"
            results.append(status)
            
            # Синхронизируем с персистентным состоянием
            ps = bot_state.persistent
            for market_id, state in trader.markets.items():
                title = state.market.title if state.market else f"Market {market_id}"
                ps.add_market(addr, market_id, title=title)
            ps.save()
            
        except Exception as e:
            results.append(f"👤 *{acc.name}*: ❌ Ошибка: {e}")
    
    if not results:
        await msg.edit_text("⚠️ Нет запущенных аккаунтов для обновления")
        return
    
    text = "🔄 *Обновление рынков*\n\n" + "\n\n".join(results)
    text += f"\n\n🕐 {datetime.now().strftime('%H:%M:%S')}"
    await msg.edit_text(text, parse_mode='Markdown')


@authorized_user
async def cmd_split(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /split - запустить SPLIT"""
    user_id = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id)
    
    if not accounts:
        await update.message.reply_text("📭 Добавьте аккаунт через /accounts")
        return
    
    # Выбор аккаунта
    keyboard = []
    for i, acc in enumerate(accounts):
        addr_short = f"{acc.predict_account_address[:6]}...{acc.predict_account_address[-4:]}"
        keyboard.append([
            InlineKeyboardButton(f"👤 {acc.name} ({addr_short})", callback_data=f"split_acc_{i}")
        ])
    
    await update.message.reply_text(
        "🔄 *SPLIT*\n\nВыберите аккаунт:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_split_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор аккаунта для SPLIT - показываем выбор типа маркетов"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    acc_idx = int(query.data.split('_')[-1])
    accounts = get_accounts_for_user(user_id)
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return ConversationHandler.END
    
    context.user_data['split_account_idx'] = acc_idx
    acc = accounts[acc_idx]
    
    # Показываем выбор типа маркетов: Boost или Все
    keyboard = [
        [InlineKeyboardButton("🔥 Boost маркеты (больше PP)", callback_data="split_type_boost")],
        [InlineKeyboardButton("📋 Все маркеты", callback_data="split_type_all")],
        [InlineKeyboardButton("« Назад", callback_data="menu_split")],
    ]
    
    await query.edit_message_text(
        f"⚡ *SPLIT - {acc.name}*\n\n"
        f"Выберите тип маркетов:\n\n"
        f"🔥 *Boost* — маркеты с повышенным начислением PP\n"
        f"📋 *Все* — все подходящие маркеты для SPLIT",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    
    return ConversationHandler.END


async def _load_split_markets(query, context, boosted_only: bool):
    """Загрузить маркеты для SPLIT (общая логика для boost и all)"""
    acc_idx = context.user_data.get('split_account_idx', 0)
    accounts = load_accounts()
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    market_type_label = "🔥 Boost" if boosted_only else "📋 Все"
    
    await query.edit_message_text(
        f"⏳ Загрузка {market_type_label} рынков для SPLIT...", 
        parse_mode='Markdown'
    )
    
    try:
        from predict_api import PredictAPI
        api = PredictAPI()
        
        # Получаем рынки в зависимости от типа
        if boosted_only:
            markets = await asyncio.to_thread(api.get_boosted_markets_for_split, 50)
        else:
            markets = await asyncio.to_thread(api.get_markets_for_split, 100)
        
        if not markets:
            keyboard = [
                [InlineKeyboardButton("🔥 Boost маркеты", callback_data="split_type_boost")] if not boosted_only else [InlineKeyboardButton("📋 Все маркеты", callback_data="split_type_all")],
                [InlineKeyboardButton("« Главное меню", callback_data="main_menu")]
            ]
            no_market_text = "📭 Нет boosted маркетов с 2 исходами" if boosted_only else "📭 Нет подходящих рынков"
            await query.edit_message_text(no_market_text, reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        # Сохраняем рынки
        context.user_data['split_markets'] = {m.id: m for m in markets}
        context.user_data['split_boosted_only'] = boosted_only
        
        # Загружаем объемы (параллельно для скорости)
        await query.edit_message_text("⏳ Загрузка объемов...", parse_mode='Markdown')
        
        def get_volumes(api, market_ids):
            volumes = {}
            for mid in market_ids:
                try:
                    stats = api.get_market_stats(mid)
                    volumes[mid] = stats.get('volume_total', 0)
                except:
                    volumes[mid] = 0
            return volumes
        
        all_ids = [m.id for m in markets[:50]]  # Увеличили лимит
        volumes = await asyncio.to_thread(get_volumes, api, all_ids)
        
        # Группируем рынки по категориям
        CATEGORY_NAMES = {
            'sports': '🏆 Спорт',
            'politics': '🏛️ Политика', 
            'crypto': '💰 Крипто',
            'entertainment': '🎬 Развлечения',
            'science': '🔬 Наука',
            'economy': '📈 Экономика',
            'news': '📰 Новости',
            'culture': '🎭 Культура',
            '': '📋 Другое'
        }
        
        categories = {}
        for market in markets:
            cat = market.category_slug.lower() if market.category_slug else ''
            # Упрощаем категорию
            if 'sport' in cat:
                cat_key = 'sports'
            elif 'politic' in cat:
                cat_key = 'politics'
            elif 'crypto' in cat or 'bitcoin' in cat:
                cat_key = 'crypto'
            elif 'entertain' in cat or 'movie' in cat or 'music' in cat:
                cat_key = 'entertainment'
            elif 'science' in cat or 'tech' in cat:
                cat_key = 'science'
            elif 'econom' in cat or 'finance' in cat:
                cat_key = 'economy'
            else:
                cat_key = ''
            
            if cat_key not in categories:
                categories[cat_key] = []
            categories[cat_key].append(market)
        
        # Сортируем рынки внутри категорий по объёму
        for cat in categories:
            categories[cat].sort(key=lambda m: volumes.get(m.id, 0), reverse=True)
        
        # Формируем итоговый список с категориями
        # Порядок категорий
        cat_order = ['politics', 'sports', 'crypto', 'economy', 'entertainment', 'science', '']
        sorted_markets_with_cats = []
        
        for cat_key in cat_order:
            if cat_key in categories:
                cat_name = CATEGORY_NAMES.get(cat_key, '📋 Другое')
                # Добавляем маркер категории
                sorted_markets_with_cats.append({'type': 'category', 'name': cat_name, 'count': len(categories[cat_key])})
                for m in categories[cat_key]:
                    sorted_markets_with_cats.append({'type': 'market', 'market': m})
        
        # Сохраняем для пагинации
        context.user_data['split_all_items'] = sorted_markets_with_cats
        context.user_data['split_volumes'] = volumes
        context.user_data['split_page'] = 0
        context.user_data['split_categories'] = CATEGORY_NAMES
        
        # Показываем первую страницу
        await show_split_markets_page(query, context, 0)
        
        return
        
    except Exception as e:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))
        return


@authorized_user_callback
async def callback_split_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор типа маркетов для SPLIT: Boost или Все"""
    query = update.callback_query
    await query.answer()
    
    split_type = query.data.replace("split_type_", "")
    boosted_only = (split_type == "boost")
    
    await _load_split_markets(query, context, boosted_only=boosted_only)


async def show_split_markets_page(query, context, page: int):
    """Показать страницу рынков для SPLIT с категориями"""
    ITEMS_PER_PAGE = 12  # Увеличили чтобы вмещать заголовки категорий
    
    all_items = context.user_data.get('split_all_items', [])
    volumes = context.user_data.get('split_volumes', {})
    acc_idx = context.user_data.get('split_account_idx', 0)
    boosted_only = context.user_data.get('split_boosted_only', False)
    
    user_id = str(query.from_user.id)
    accounts = get_accounts_for_user(user_id)
    acc = accounts[acc_idx] if acc_idx < len(accounts) else None
    acc_name = acc.name if acc else "Unknown"
    
    # Считаем только рынки (не заголовки категорий)
    total_markets = sum(1 for item in all_items if item['type'] == 'market')
    total_pages = max(1, (len(all_items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    
    # Ограничиваем страницу
    page = max(0, min(page, total_pages - 1))
    context.user_data['split_page'] = page
    
    # Получаем элементы для текущей страницы
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_items = all_items[start_idx:end_idx]
    
    def format_volume(vol):
        if vol >= 1_000_000:
            return f"${vol/1_000_000:.1f}M"
        elif vol >= 1_000:
            return f"${vol/1_000:.0f}K"
        else:
            return f"${vol:.0f}"
    
    # Создаём кнопки
    keyboard = []
    current_category = None
    
    for item in page_items:
        if item['type'] == 'category':
            # Заголовок категории
            keyboard.append([
                InlineKeyboardButton(f"━━ {item['name']} ({item['count']}) ━━", callback_data="ignore")
            ])
            current_category = item['name']
        else:
            market = item['market']
            # question содержит полное описание события (например, "LoL: Gen.G vs BNK FEARX (BO5)")
            # title часто просто "Match Winner" — неинформативно
            display_name = market.question if market.question and market.question != market.title else market.title
            display_name = display_name[:35] if display_name else f"Market {market.id}"
            vol = format_volume(volumes.get(market.id, 0))
            boost_badge = "🔥 " if market.is_boosted else ""
            keyboard.append([
                InlineKeyboardButton(f"{boost_badge}{display_name} [{vol}]", callback_data=f"split_market_{market.id}")
            ])
    
    # Кнопки навигации
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("« Назад", callback_data=f"split_page_{page-1}"))
    nav_buttons.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="ignore"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд »", callback_data=f"split_page_{page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("✏️ Ввести ID вручную", callback_data="split_manual")])
    
    # Кнопка переключения типа маркетов
    if boosted_only:
        keyboard.append([InlineKeyboardButton("📋 Показать все маркеты", callback_data="split_type_all")])
    else:
        keyboard.append([InlineKeyboardButton("🔥 Показать Boost маркеты", callback_data="split_type_boost")])
    
    keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
    
    type_label = "🔥 Boost" if boosted_only else "📋 Все"
    
    await query.edit_message_text(
        f"⚡ *SPLIT - {acc_name}* ({type_label})\n\n"
        f"📊 Всего рынков: {total_markets}\n"
        f"📄 Страница {page+1} из {total_pages}\n\n"
        f"_Сгруппировано по категориям, отсортировано по объёму_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_split_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение страницы рынков в SPLIT"""
    query = update.callback_query
    await query.answer()
    
    page = int(query.data.split('_')[-1])
    await show_split_markets_page(query, context, page)


@authorized_user_callback
async def callback_market_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать детальную информацию о рынке"""
    query = update.callback_query
    await query.answer()
    
    market_id = int(query.data.split('_')[-1])
    await show_market_details(query, context, market_id)


@authorized_user_callback
async def callback_markets_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор аккаунта для просмотра рынков"""
    query = update.callback_query
    await query.answer()
    
    acc_idx = int(query.data.split('_')[-1])
    accounts = load_accounts()
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    await show_markets_for_account(query, context, acc)


@authorized_user_callback
async def callback_split_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор рынка для SPLIT"""
    query = update.callback_query
    await query.answer()
    
    market_id = int(query.data.split('_')[-1])
    context.user_data['split_market_id'] = market_id
    
    # Показываем кнопки выбора суммы
    keyboard = [
        [
            InlineKeyboardButton("$5", callback_data="split_amount_5"),
            InlineKeyboardButton("$10", callback_data="split_amount_10"),
            InlineKeyboardButton("$25", callback_data="split_amount_25"),
        ],
        [
            InlineKeyboardButton("$50", callback_data="split_amount_50"),
            InlineKeyboardButton("$100", callback_data="split_amount_100"),
        ],
        [InlineKeyboardButton("✏️ Своя сумма", callback_data="split_custom_amount")],
        [InlineKeyboardButton("« Назад", callback_data="menu_split")],
    ]
    
    await query.edit_message_text(
        f"⚡ *SPLIT - Рынок #{market_id}*\n\n"
        f"Выберите сумму:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_split_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнение SPLIT с выбранной суммой"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    amount = float(query.data.split('_')[-1])
    market_id = context.user_data.get('split_market_id')
    acc_idx = context.user_data.get('split_account_idx', 0)
    
    accounts = get_accounts_for_user(user_id)
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    
    await query.edit_message_text(
        f"⏳ Выполняем SPLIT...\n\n"
        f"👤 Аккаунт: {acc.name}\n"
        f"📊 Рынок: #{market_id}\n"
        f"💰 Сумма: ${amount:.2f}"
    )
    
    try:
        # Создаём трейдера
        trader = PredictTrader(
            market_id=market_id,
            order_amount=amount,
            account=acc
        )
        
        if not await asyncio.to_thread(trader.init_sdk):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Ошибка SDK", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        if not await asyncio.to_thread(trader.authenticate):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Ошибка аутентификации", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        if not await asyncio.to_thread(trader.set_approvals):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Ошибка разрешений", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        if not await asyncio.to_thread(trader.load_market, market_id):
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("❌ Рынок не найден", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        # Проверяем баланс
        balance = await asyncio.to_thread(trader.get_usdt_balance)
        if balance < amount:
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text(
                f"❌ Недостаточно баланса: ${balance:.2f} < ${amount:.2f}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        # Выполняем SPLIT
        await query.edit_message_text("⏳ Выполняем SPLIT транзакцию...")
        
        success = await asyncio.to_thread(trader.run_split_cycle)
        
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        
        if success:
            await query.edit_message_text(
                f"✅ *SPLIT выполнен!*\n\n"
                f"👤 Аккаунт: {acc.name}\n"
                f"📊 Рынок: #{market_id}\n"
                f"💰 Сумма: ${amount:.2f}\n\n"
                f"Токены куплены, ордера выставлены!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                "❌ SPLIT не удался. Проверьте логи.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
    except Exception as e:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))
        
        # Отправляем уведомление об ошибке
        await send_error_notification(
            f"Ошибка при выполнении SPLIT\n"
            f"Аккаунт: {acc.name}\n"
            f"Рынок: #{market_id}\n"
            f"Сумма: ${amount:.2f}\n\n"
            f"{type(e).__name__}: {e}",
            "SPLIT ERROR"
        )
    
    context.user_data.clear()


@authorized_user_callback  
async def callback_split_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной ввод ID рынка"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "🔄 *SPLIT*\n\n"
        "Введите ID рынка (например: 5052):\n\n"
        "_Или /cancel для отмены_",
        parse_mode='Markdown'
    )
    
    return STATE_WAITING_MARKET_ID


@authorized_user_callback
async def callback_split_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной ввод суммы"""
    query = update.callback_query
    await query.answer()
    
    market_id = context.user_data.get('split_market_id')
    
    await query.edit_message_text(
        f"⚡ *SPLIT - Рынок #{market_id}*\n\n"
        f"Введите сумму (например: 15.5):\n\n"
        "_Или /cancel для отмены_",
        parse_mode='Markdown'
    )
    
    return STATE_WAITING_AMOUNT


async def handle_market_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение ID рынка для SPLIT"""
    try:
        market_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введите число! Попробуйте ещё раз:")
        return STATE_WAITING_MARKET_ID
    
    context.user_data['split_market_id'] = market_id
    
    await update.message.reply_text(
        f"✅ Рынок: #{market_id}\n\n"
        f"Введите сумму SPLIT (например: 50 или 100):",
        parse_mode='Markdown'
    )
    
    return STATE_WAITING_AMOUNT


async def handle_split_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение суммы для SPLIT"""
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            await update.message.reply_text("❌ Сумма должна быть > 0! Введите сумму:")
            return STATE_WAITING_AMOUNT
    except ValueError:
        await update.message.reply_text("❌ Введите число! Например: 50 или 100")
        return STATE_WAITING_AMOUNT
    
    acc_idx = context.user_data.get('split_account_idx', 0)
    market_id = context.user_data.get('split_market_id')
    
    user_id_str = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id_str)
    if acc_idx >= len(accounts):
        await update.message.reply_text("❌ Аккаунт не найден")
        return ConversationHandler.END
    
    acc = accounts[acc_idx]
    
    msg = await update.message.reply_text(
        f"⏳ Выполняем SPLIT...\n\n"
        f"👤 Аккаунт: {acc.name}\n"
        f"📊 Рынок: #{market_id}\n"
        f"💰 Сумма: ${amount:.2f}"
    )
    
    try:
        # Создаём трейдера
        trader = PredictTrader(
            market_id=market_id,
            order_amount=amount,
            account=acc
        )
        
        if not await asyncio.to_thread(trader.init_sdk):
            await msg.edit_text("❌ Ошибка SDK")
            return ConversationHandler.END
        
        if not await asyncio.to_thread(trader.authenticate):
            await msg.edit_text("❌ Ошибка аутентификации")
            return ConversationHandler.END
        
        if not await asyncio.to_thread(trader.set_approvals):
            await msg.edit_text("❌ Ошибка разрешений")
            return ConversationHandler.END
        
        if not await asyncio.to_thread(trader.load_market, market_id):
            await msg.edit_text("❌ Рынок не найден")
            return ConversationHandler.END
        
        # Проверяем баланс
        balance = await asyncio.to_thread(trader.get_usdt_balance)
        if balance < amount:
            await msg.edit_text(f"❌ Недостаточно баланса: ${balance:.2f} < ${amount:.2f}")
            return ConversationHandler.END
        
        # Выполняем SPLIT
        await msg.edit_text("⏳ Выполняем SPLIT транзакцию...")
        
        success = await asyncio.to_thread(trader.run_split_cycle)
        
        if success:
            await msg.edit_text(
                f"✅ *SPLIT выполнен!*\n\n"
                f"👤 Аккаунт: {acc.name}\n"
                f"📊 Рынок: #{market_id}\n"
                f"💰 Сумма: ${amount:.2f}\n\n"
                f"Токены куплены, ордера выставлены!",
                parse_mode='Markdown'
            )
        else:
            await msg.edit_text("❌ SPLIT не удался. Проверьте логи.")
        
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")
    
    context.user_data.clear()
    return ConversationHandler.END


@authorized_user
async def cmd_start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start_bot - запустить мониторинг"""
    user_id = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id)
    
    if not accounts:
        await update.message.reply_text("📭 Добавьте аккаунт через /accounts")
        return
    
    keyboard = []
    for i, acc in enumerate(accounts):
        is_running = bot_state.running.get(acc.predict_account_address, False)
        status = "🟢" if is_running else "⚪"
        keyboard.append([
            InlineKeyboardButton(f"{status} {acc.name}", callback_data=f"start_acc_{i}")
        ])
    
    keyboard.append([InlineKeyboardButton("▶️ Запустить все", callback_data="start_all")])
    
    await update.message.reply_text(
        "▶️ *Запуск мониторинга*\n\nВыберите аккаунт:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_start_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск мониторинга для аккаунта"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    acc_idx = int(query.data.split('_')[-1])
    accounts = get_accounts_for_user(user_id)
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    
    await query.edit_message_text(f"⏳ Запуск мониторинга для *{acc.name}*...", parse_mode='Markdown')
    
    success = await start_monitoring(acc, context, owner_id=user_id)
    
    keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
    
    if success:
        await query.edit_message_text(
            f"✅ Мониторинг для *{acc.name}* запущен!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text(
            f"❌ Ошибка запуска для *{acc.name}*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )


@authorized_user_callback
async def callback_start_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск мониторинга для всех"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id)
    
    await query.edit_message_text("⏳ Запуск всех аккаунтов...")
    
    started = 0
    for acc in accounts:
        if await start_monitoring(acc, context, owner_id=user_id):
            started += 1
    
    keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
    await query.edit_message_text(
        f"✅ Запущено {started}/{len(accounts)} аккаунтов",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    


@authorized_user
async def cmd_stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stop_bot - остановить мониторинг"""
    user_id = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id)
    
    running_count = sum(1 for acc in accounts if bot_state.running.get(acc.predict_account_address, False))
    
    if running_count == 0:
        await update.message.reply_text("📭 Нет запущенных аккаунтов")
        return
    
    keyboard = []
    for i, acc in enumerate(accounts):
        is_running = bot_state.running.get(acc.predict_account_address, False)
        if is_running:
            keyboard.append([
                InlineKeyboardButton(f"🟢 {acc.name}", callback_data=f"stop_acc_{i}")
            ])
    
    keyboard.append([InlineKeyboardButton("⏹ Остановить все", callback_data="stop_all")])
    
    await update.message.reply_text(
        f"⏹ *Остановка мониторинга*\n\nЗапущено: {running_count}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_stop_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Остановка мониторинга для аккаунта"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    acc_idx = int(query.data.split('_')[-1])
    accounts = get_accounts_for_user(user_id)
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    await stop_monitoring(acc)
    
    keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
    await query.edit_message_text(
        f"⏹ Мониторинг для *{acc.name}* остановлен",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@authorized_user_callback
async def callback_stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Остановка всех"""
    query = update.callback_query
    await query.answer()
    
    user_id = str(update.effective_user.id)
    accounts = get_accounts_for_user(user_id)
    
    for acc in accounts:
        await stop_monitoring(acc)
    
    keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
    await query.edit_message_text(
        "⏹ Все аккаунты остановлены",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =============================================================================
# МОНИТОРИНГ
# =============================================================================

def get_or_create_trader(acc: AccountConfig, owner_id: str = None) -> Optional[PredictTrader]:
    """Получить или создать трейдера для аккаунта"""
    if acc.predict_account_address in bot_state.traders:
        trader = bot_state.traders[acc.predict_account_address]
        effective_owner = owner_id or bot_state.account_owners.get(acc.predict_account_address, TELEGRAM_ADMIN_ID)
        apply_user_trade_settings_to_trader(trader, effective_owner)
        return trader

    effective_owner = owner_id or bot_state.account_owners.get(acc.predict_account_address, TELEGRAM_ADMIN_ID)
    settings = get_user_trade_settings(effective_owner)
    
    trader = PredictTrader(
        market_ids=None,
        monitor_mode=True,
        account=acc,
        ask_position_offset=settings['ask_position_offset'],
        reposition_delay=settings['reposition_delay'],
    )
    
    bot_state.traders[acc.predict_account_address] = trader
    return trader


async def start_monitoring(acc: AccountConfig, context: ContextTypes.DEFAULT_TYPE, owner_id: str = None) -> bool:
    """Запустить мониторинг для аккаунта (с персистентностью)"""
    owner_id = owner_id or TELEGRAM_ADMIN_ID
    if bot_state.running.get(acc.predict_account_address, False):
        return True  # Уже запущен
    
    try:
        trader = get_or_create_trader(acc, owner_id=owner_id)
        apply_user_trade_settings_to_trader(trader, owner_id)
        
        # Запускаем синхронные методы в отдельном потоке
        if not await asyncio.to_thread(trader.init_sdk):
            return False
        
        if not await asyncio.to_thread(trader.authenticate):
            return False
        
        if not await asyncio.to_thread(trader.set_approvals):
            return False
        
        # Запускаем фоновую задачу (WebSocket или polling)
        if USE_WEBSOCKET:
            task = asyncio.create_task(monitoring_loop_ws(acc, trader, context))
            logger.info(f"⚡ Запуск WebSocket мониторинга для {acc.name}")
        else:
            task = asyncio.create_task(monitoring_loop(acc, trader, context))
            logger.info(f"🔄 Запуск polling мониторинга для {acc.name}")
        bot_state.tasks[acc.predict_account_address] = task
        
        # Сохраняем владельца аккаунта
        bot_state.account_owners[acc.predict_account_address] = owner_id
        
        # Сохраняем состояние (персистентно!)
        bot_state.mark_account_running(acc.predict_account_address, acc.name, True, owner_id=owner_id)
        
        logger.info(f"✅ Мониторинг запущен для {acc.name}, состояние сохранено")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка запуска мониторинга: {e}")
        return False


async def stop_monitoring(acc: AccountConfig):
    """Остановить мониторинг для аккаунта (с персистентностью)"""
    # Сохраняем состояние (персистентно!)
    bot_state.mark_account_running(acc.predict_account_address, acc.name, False)
    
    if acc.predict_account_address in bot_state.tasks:
        task = bot_state.tasks[acc.predict_account_address]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        del bot_state.tasks[acc.predict_account_address]
    
    logger.info(f"⏹ Мониторинг остановлен для {acc.name}, состояние сохранено")


async def _restore_monitoring(acc: AccountConfig, application: Application, owner_id: str = None) -> bool:
    """
    Восстановить мониторинг для аккаунта при старте бота (без context).
    
    Используется в post_init где нет пользовательского context.
    """
    owner_id = owner_id or TELEGRAM_ADMIN_ID
    addr = acc.predict_account_address
    bot_state.account_owners[addr] = owner_id
    
    if bot_state.running.get(addr, False):
        return True  # Уже запущен
    
    try:
        trader = get_or_create_trader(acc, owner_id=owner_id)
        apply_user_trade_settings_to_trader(trader, owner_id)
        
        if not await asyncio.to_thread(trader.init_sdk):
            return False
        
        if not await asyncio.to_thread(trader.authenticate):
            return False
        
        if not await asyncio.to_thread(trader.set_approvals):
            return False
        
        # Запускаем фоновую задачу (WebSocket или polling)
        if USE_WEBSOCKET:
            task = asyncio.create_task(monitoring_loop_ws(acc, trader, None))
        else:
            task = asyncio.create_task(monitoring_loop(acc, trader, None))
        bot_state.tasks[addr] = task
        bot_state.running[addr] = True
        
        return True
        
    except Exception as e:
        logger.error(f"Ошибка восстановления мониторинга {acc.name}: {e}")
        return False


async def monitoring_loop(acc: AccountConfig, trader: PredictTrader, context: ContextTypes.DEFAULT_TYPE):
    """Цикл мониторинга с персистентностью и отслеживанием исполнений"""
    cycle = 0
    consecutive_errors = 0
    addr = acc.predict_account_address
    owner_id = bot_state.account_owners.get(addr, TELEGRAM_ADMIN_ID)
    owner_chat_id = owner_id
    ps = bot_state.get_persistent_for_user(owner_id)
    
    # Запоминаем предыдущие позиции для детекции исполнений
    prev_positions: Dict[int, Dict[str, float]] = {}  # market_id -> {yes: x, no: x}
    
    while bot_state.running.get(addr, False):
        try:
            cycle += 1
            
            # Загружаем рынки каждые 10 циклов (в отдельном потоке)
            if cycle == 1 or cycle % 10 == 0:
                # Проверяем закрытые рынки ПЕРЕД перезагрузкой
                if trader.markets:
                    resolved = await asyncio.to_thread(trader.check_and_handle_resolved_markets)
                    if resolved:
                        await send_resolved_market_notification(acc.name, resolved, chat_id=owner_chat_id)
                        # Обновляем персистентное состояние
                        for res in resolved:
                            ps.update_market(addr, res['market_id'], split_phase=0,
                                           yes_position=0, no_position=0)
                
                await asyncio.to_thread(trader.load_all_markets)
                
                # Синхронизируем рынки с персистентным состоянием
                for market_id, state in trader.markets.items():
                    title = state.market.title if state.market else f"Market {market_id}"
                    ps.add_market(addr, market_id, title=title)
            
            if trader.markets:
                # Запоминаем позиции ДО цикла
                for market_id, state in trader.markets.items():
                    prev_positions[market_id] = {
                        'yes': state.yes_position,
                        'no': state.no_position,
                    }
                
                # Выполняем цикл мониторинга (в отдельном потоке)
                result = await asyncio.to_thread(trader.monitor_cycle)
                
                # Отслеживаем исполнения ордеров (сравниваем позиции до и после)
                for market_id, state in trader.markets.items():
                    prev = prev_positions.get(market_id, {'yes': 0, 'no': 0})
                    
                    yes_diff = prev['yes'] - state.yes_position
                    no_diff = prev['no'] - state.no_position
                    
                    # Если позиция уменьшилась — ордер исполнился
                    if yes_diff > 0.5 or no_diff > 0.5:
                        ps.record_fill(addr, market_id, 
                                      yes_filled=max(0, yes_diff),
                                      no_filled=max(0, no_diff))
                        
                        market_title = state.market.title[:25] if state.market else f"#{market_id}"
                        
                        # Проверяем дисбаланс
                        market_state = ps.get_account(addr)
                        if market_state:
                            ms = market_state.markets.get(market_id)
                            if ms and not ms.is_balanced(threshold=5.0):
                                imbalance = ms.get_imbalance()
                                await send_error_notification(
                                    f"⚠️ ДИСБАЛАНС на рынке {market_title}!\n"
                                    f"YES sold: {ms.yes_sold:.1f}\n"
                                    f"NO sold: {ms.no_sold:.1f}\n"
                                    f"Разница: {imbalance:.1f} токенов\n\n"
                                    f"Рекомендуется проверить позиции!",
                                    "FILL IMBALANCE",
                                    chat_id=owner_chat_id
                                )
                    
                    # Обновляем позиции в персистентном состоянии
                    ps.update_market(addr, market_id,
                                    split_phase=state.split_phase,
                                    yes_position=state.yes_position,
                                    no_position=state.no_position)
                
                # Логируем
                logger.info(f"[{acc.name}] Цикл #{cycle}: {result.get('message', 'OK')}")
                
                # Отправляем уведомление о переставлении ордеров
                repositioned = result.get('repositioned', [])
                if repositioned:
                    await send_repositioning_notification(
                        acc.name,
                        repositioned,
                        chat_id=owner_chat_id,
                        reposition_delay=trader.reposition_delay,
                        account_address=addr,
                    )
                
                # 🚨 Отправляем ГРОМКОЕ уведомление об исполнениях
                fills = result.get('fills_detected', [])
                if fills:
                    fills_text = f"🚨 *ОБНАРУЖЕНО ИСПОЛНЕНИЕ ОРДЕРОВ!*\n👤 {acc.name}\n\n"
                    for fill in fills:
                        fills_text += (
                            f"⚠️ *{fill.get('market_name', '?')}*\n"
                            f"   {fill['token']}: исполнено {fill['filled']:.2f} "
                            f"из {fill['original']:.2f} @ {fill['price']:.3f}\n"
                            f"   Осталось: {fill['remaining']:.2f}\n\n"
                        )
                    fills_text += f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                    
                    try:
                        if owner_chat_id and bot_state.app:
                            await bot_state.app.bot.send_message(
                                chat_id=int(owner_chat_id),
                                text=fills_text,
                                parse_mode='Markdown',
                                disable_notification=False,  # ГРОМКОЕ уведомление!
                            )
                    except Exception as e:
                        logger.error(f"Failed to send fill notification: {e}")
                
                # Сохраняем состояние каждые 5 циклов (не каждый, чтобы не нагружать диск)
                if cycle % 5 == 0:
                    ps.save()
            
            # Сбрасываем счётчик ошибок при успехе
            consecutive_errors = 0
            
            # Ждём
            await asyncio.sleep(CHECK_INTERVAL)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            consecutive_errors += 1
            error_msg = f"[{acc.name}] Ошибка в цикле #{cycle}: {type(e).__name__}: {e}"
            logger.error(error_msg)
            
            # Отправляем уведомление при первой ошибке или каждые 5 подряд
            if consecutive_errors == 1 or consecutive_errors % 5 == 0:
                await send_error_notification(
                    f"Аккаунт: {acc.name}\n"
                    f"Цикл: #{cycle}\n"
                    f"Ошибок подряд: {consecutive_errors}\n\n"
                    f"{type(e).__name__}: {e}",
                    "MONITORING ERROR",
                    chat_id=owner_chat_id
                )
            
            await asyncio.sleep(5)
    
    # Сохраняем финальное состояние
    ps.save()
    logger.info(f"[{acc.name}] Мониторинг (polling) остановлен, состояние сохранено")


async def monitoring_loop_ws(acc: AccountConfig, trader: PredictTrader, context: ContextTypes.DEFAULT_TYPE):
    """
    Цикл мониторинга через WebSocket — мгновенная реакция на изменения стакана.
    
    Подписывается на:
    - predictOrderbook/{marketId} — обновления стакана для каждого рынка
    - predictWalletEvents/{jwt}   — события ордеров (fill, cancel и т.д.)
    
    При изменении стакана мгновенно проверяет позицию ордеров и
    переставляет их если нужно. Fallback polling каждые WS_FALLBACK_INTERVAL сек.
    """
    addr = acc.predict_account_address
    owner_id = bot_state.account_owners.get(addr, TELEGRAM_ADMIN_ID)
    owner_chat_id = owner_id
    ps = bot_state.get_persistent_for_user(owner_id)
    
    # Запоминаем предыдущие позиции для детекции исполнений
    prev_positions: Dict[int, Dict[str, float]] = {}
    
    # Очередь событий для обработки
    event_queue: asyncio.Queue = asyncio.Queue()
    
    # Lock чтобы не делать одновременно 2 переставления
    reposition_lock = asyncio.Lock()
    
    # WebSocket клиент
    ws = PredictWebSocket(
        api_key=API_KEY,
        jwt_token=trader.api.jwt_token or "",
    )
    
    # --- CALLBACKS для WebSocket ---
    
    async def on_orderbook_update(market_id: int, data):
        """Получено обновление стакана — ставим в очередь на обработку"""
        await event_queue.put(('orderbook', market_id, data))
    
    async def on_wallet_event(data):
        """Получено событие кошелька (fill, cancel и т.д.)"""
        await event_queue.put(('wallet', None, data))
    
    ws.on_orderbook_update = on_orderbook_update
    ws.on_wallet_event = on_wallet_event
    
    # --- Обработка события orderbook ---
    
    async def handle_phase1_market(market_id: int):
        """Обработать рынок в Phase 1 — выставить SELL ордера"""
        if market_id not in trader.markets:
            return False
        state = trader.markets[market_id]
        if state.split_phase != 1:
            return False
        
        async with reposition_lock:
            try:
                trader._switch_to_market(market_id)
                result = await asyncio.to_thread(trader.strategy_split)
                orders_placed = result.get('orders_placed', 0)
                if orders_placed > 0:
                    state.split_phase = 2
                    ps.update_market(addr, market_id, split_phase=2)
                    logger.info(f"[{acc.name}] ✅ #{market_id}: выставлено {orders_placed} ордеров (Phase 1 → 2)")
                    return True
                else:
                    logger.warning(f"[{acc.name}] ⚠️ #{market_id} Phase 1: {result.get('message', 'ордера не создались')}")
            except Exception as e:
                logger.error(f"[{acc.name}] Phase 1 #{market_id}: {e}")
        return False

    async def handle_orderbook_event(market_id: int):
        """Мгновенная реакция на изменение стакана"""
        if market_id not in trader.markets:
            return
        
        state = trader.markets[market_id]
        
        # Phase 1 — пробуем выставить ордера при каждом обновлении стакана
        if state.split_phase == 1:
            await handle_phase1_market(market_id)
            return
        
        if state.split_phase < 1:
            return
        
        async with reposition_lock:
            try:
                # Запоминаем позиции ДО
                prev = {
                    'yes': state.yes_position,
                    'no': state.no_position,
                }
                
                # Выполняем проверку и переставление в отдельном потоке
                result = await asyncio.to_thread(
                    trader.check_and_reposition_market, market_id
                )
                
                market_name = state.market.title[:25] if state.market else f"#{market_id}"
                
                if result.get('updated', 0) > 0:
                    logger.info(f"[{acc.name}] ⚡ WS: переставлено {result['updated']} "
                               f"ордеров на {market_name}")
                    
                    # Уведомление о переставлении
                    repositioned = result.get('repositioned', [])
                    if repositioned:
                        enriched = [{
                            'market_name': market_name,
                            'market_id': market_id,
                            **r
                        } for r in repositioned]
                        await send_repositioning_notification(
                            acc.name,
                            enriched,
                            chat_id=owner_chat_id,
                            reposition_delay=trader.reposition_delay,
                            account_address=addr,
                        )
                
                # Проверяем fills
                fills = result.get('fills_detected', [])
                if fills:
                    fills_text = f"🚨 *ОБНАРУЖЕНО ИСПОЛНЕНИЕ ОРДЕРОВ!*\n👤 {acc.name}\n\n"
                    for fill in fills:
                        fills_text += (
                            f"⚠️ *{market_name}*\n"
                            f"   {fill['token']}: исполнено {fill['filled']:.2f} "
                            f"из {fill['original']:.2f} @ {fill['price']:.3f}\n"
                            f"   Осталось: {fill['remaining']:.2f}\n\n"
                        )
                    fills_text += f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                    
                    try:
                        if owner_chat_id and bot_state.app:
                            await bot_state.app.bot.send_message(
                                chat_id=int(owner_chat_id),
                                text=fills_text,
                                parse_mode='Markdown',
                                disable_notification=False,
                            )
                    except Exception as e:
                        logger.error(f"Failed to send fill notification: {e}")
                
                # Обновляем позиции после
                yes_diff = prev['yes'] - state.yes_position
                no_diff = prev['no'] - state.no_position
                
                if yes_diff > 0.5 or no_diff > 0.5:
                    ps.record_fill(addr, market_id,
                                  yes_filled=max(0, yes_diff),
                                  no_filled=max(0, no_diff))
                    
                    ms = ps.get_account(addr)
                    if ms:
                        mstate = ms.markets.get(market_id)
                        if mstate and not mstate.is_balanced(threshold=5.0):
                            imbalance = mstate.get_imbalance()
                            await send_error_notification(
                                f"⚠️ ДИСБАЛАНС на рынке {market_name}!\n"
                                f"YES sold: {mstate.yes_sold:.1f}\n"
                                f"NO sold: {mstate.no_sold:.1f}\n"
                                f"Разница: {imbalance:.1f} токенов\n\n"
                                f"Рекомендуется проверить позиции!",
                                "FILL IMBALANCE",
                                chat_id=owner_chat_id
                            )
                
                ps.update_market(addr, market_id,
                                split_phase=state.split_phase,
                                yes_position=state.yes_position,
                                no_position=state.no_position)
                
            except Exception as e:
                logger.error(f"[{acc.name}] Ошибка обработки WS orderbook для #{market_id}: {e}")
    
    # --- Обработка wallet events ---
    
    async def handle_wallet_event(data):
        """Обработка события от кошелька — все типы WS wallet events"""
        if not isinstance(data, dict):
            return
        
        event_type = data.get('type', '')
        details = data.get('details', {})
        order_id = data.get('orderId', '')
        
        outcome = details.get('outcome', '')
        market_question = details.get('marketQuestion', '')[:30] if details.get('marketQuestion') else ''
        quantity = details.get('quantity', '0')
        price = details.get('price', '0')
        
        # ===== orderAccepted — ордер принят в стакан =====
        if event_type == 'orderAccepted':
            logger.info(f"[{acc.name}] ✅ WS: ордер принят | {outcome} qty={quantity} @ {price} | {market_question}")
            return
        
        # ===== orderNotAccepted — ордер отклонён =====
        if event_type == 'orderNotAccepted':
            logger.warning(f"[{acc.name}] ❌ WS: ордер отклонён | {outcome} qty={quantity} @ {price} | {market_question}")
            try:
                if owner_chat_id and bot_state.app:
                    await bot_state.app.bot.send_message(
                        chat_id=int(owner_chat_id),
                        text=(
                            f"❌ *ОРДЕР ОТКЛОНЁН*\n"
                            f"👤 {acc.name}\n"
                            f"📊 {market_question}\n"
                            f"   {outcome}: qty={quantity} @ {price}\n"
                            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                        ),
                        parse_mode='Markdown',
                        disable_notification=True,
                    )
            except Exception as e:
                logger.error(f"Failed to send notification: {e}")
            return
        
        # ===== orderExpired — ордер истёк =====
        if event_type == 'orderExpired':
            logger.warning(f"[{acc.name}] ⏰ WS: ордер истёк | {outcome} qty={quantity} @ {price} | {market_question}")
            try:
                if owner_chat_id and bot_state.app:
                    await bot_state.app.bot.send_message(
                        chat_id=int(owner_chat_id),
                        text=(
                            f"⏰ *ОРДЕР ИСТЁК*\n"
                            f"👤 {acc.name}\n"
                            f"📊 {market_question}\n"
                            f"   {outcome}: qty={quantity} @ {price}\n"
                            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                        ),
                        parse_mode='Markdown',
                        disable_notification=False,
                    )
            except Exception as e:
                logger.error(f"Failed to send notification: {e}")
            # Ордер исчез — нужно проверить рынки и перевыставить
            for market_id in list(trader.markets.keys()):
                await handle_orderbook_event(market_id)
            return
        
        # ===== orderCancelled — ордер отменён =====
        if event_type == 'orderCancelled':
            logger.info(f"[{acc.name}] 🚫 WS: ордер отменён | {outcome} qty={quantity} @ {price} | {market_question}")
            # Проверяем рынки — возможно нужно перевыставить
            for market_id in list(trader.markets.keys()):
                await handle_orderbook_event(market_id)
            return
        
        # ===== orderTransactionSubmitted — ордер СМАТЧЕН, транзакция отправлена =====
        if event_type == 'orderTransactionSubmitted':
            logger.warning(f"[{acc.name}] 🚨 ОРДЕР СМАТЧЕН! | {outcome} qty={quantity} @ {price} | {market_question}")
            try:
                if owner_chat_id and bot_state.app:
                    await bot_state.app.bot.send_message(
                        chat_id=int(owner_chat_id),
                        text=(
                            f"⚡ *ОРДЕР СМАТЧЕН!*\n"
                            f"👤 {acc.name}\n"
                            f"📊 {market_question}\n"
                            f"   {outcome}: qty={quantity} @ {price}\n"
                            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                        ),
                        parse_mode='Markdown',
                        disable_notification=False,
                    )
            except Exception as e:
                logger.error(f"Failed to send match notification: {e}")
            # СРОЧНО проверяем все рынки
            for market_id in list(trader.markets.keys()):
                await handle_orderbook_event(market_id)
            return
        
        # ===== orderTransactionSuccess — транзакция прошла на блокчейне =====
        if event_type == 'orderTransactionSuccess':
            logger.info(f"[{acc.name}] ✅ WS: транзакция выполнена | {outcome} qty={quantity} @ {price} | {market_question}")
            try:
                if owner_chat_id and bot_state.app:
                    await bot_state.app.bot.send_message(
                        chat_id=int(owner_chat_id),
                        text=(
                            f"✅ *ТРАНЗАКЦИЯ ВЫПОЛНЕНА*\n"
                            f"👤 {acc.name}\n"
                            f"📊 {market_question}\n"
                            f"   {outcome}: qty={quantity} @ {price}\n"
                            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                        ),
                        parse_mode='Markdown',
                        disable_notification=True,
                    )
            except Exception as e:
                logger.error(f"Failed to send notification: {e}")
            # Обновляем состояние рынков после исполнения
            for market_id in list(trader.markets.keys()):
                await handle_orderbook_event(market_id)
            return
        
        # ===== orderTransactionFailed — транзакция провалилась =====
        if event_type == 'orderTransactionFailed':
            logger.error(f"[{acc.name}] ❌ WS: транзакция ПРОВАЛИЛАСЬ | {outcome} qty={quantity} @ {price} | {market_question}")
            try:
                if owner_chat_id and bot_state.app:
                    await bot_state.app.bot.send_message(
                        chat_id=int(owner_chat_id),
                        text=(
                            f"❌ *ТРАНЗАКЦИЯ ПРОВАЛИЛАСЬ!*\n"
                            f"👤 {acc.name}\n"
                            f"📊 {market_question}\n"
                            f"   {outcome}: qty={quantity} @ {price}\n\n"
                            f"⚠️ Проверьте позиции!\n"
                            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
                        ),
                        parse_mode='Markdown',
                        disable_notification=False,
                    )
            except Exception as e:
                logger.error(f"Failed to send notification: {e}")
            # Проверяем рынки
            for market_id in list(trader.markets.keys()):
                await handle_orderbook_event(market_id)
            return
        
        # ===== Неизвестный тип =====
        logger.info(f"[{acc.name}] 👛 WS wallet event (неизвестный): {event_type} | data={data}")
    
    # --- Основной цикл ---
    
    try:
        # 1. Начальная загрузка рынков
        await asyncio.to_thread(trader.load_all_markets)
        for market_id, state in trader.markets.items():
            title = state.market.title if state.market else f"Market {market_id}"
            ps.add_market(addr, market_id, title=title)
        
        # 2. Подключаем WebSocket
        connected = await ws.connect()
        if not connected:
            logger.error(f"[{acc.name}] Не удалось подключить WebSocket, переход на polling")
            # Fallback на обычный polling
            await monitoring_loop(acc, trader, context)
            return
        
        # 3. Подписываемся на все рынки
        for market_id in trader.markets:
            await ws.subscribe_orderbook(market_id)
            await asyncio.sleep(0.1)  # Не спамим подписками
        
        # 4. Подписываемся на wallet events
        if trader.api.jwt_token:
            ws.jwt_token = trader.api.jwt_token
            await ws.subscribe_wallet_events()
        
        # 5. Сразу обрабатываем Phase 1 рынки (выставляем SELL ордера)
        phase1_markets = [mid for mid, s in trader.markets.items() if s.split_phase == 1]
        if phase1_markets:
            logger.info(f"[{acc.name}] 🔄 Обнаружено {len(phase1_markets)} рынков в Phase 1, выставляем ордера...")
            for mid in phase1_markets:
                await handle_phase1_market(mid)
                await asyncio.sleep(0.5)
        
        # 6. Запускаем WebSocket listener как отдельную задачу
        ws_task = asyncio.create_task(ws.listen())
        
        # 7. Запускаем обработчик событий
        logger.info(f"[{acc.name}] ⚡ WebSocket мониторинг запущен! "
                    f"Рынков: {len(trader.markets)}, "
                    f"Обновление рынков: каждые {WS_MARKET_REFRESH_INTERVAL} сек, "
                    f"Fallback: каждые {WS_FALLBACK_INTERVAL} сек")
        
        fallback_cycle = 0
        last_fallback = asyncio.get_event_loop().time()
        last_market_reload = asyncio.get_event_loop().time()
        
        while bot_state.running.get(addr, False):
            try:
                # Ждём события из очереди (с таймаутом для fallback)
                try:
                    event = await asyncio.wait_for(
                        event_queue.get(),
                        timeout=min(5.0, WS_FALLBACK_INTERVAL)
                    )
                    
                    event_type, market_id, data = event
                    
                    if event_type == 'orderbook':
                        await handle_orderbook_event(market_id)
                    elif event_type == 'wallet':
                        await handle_wallet_event(data)
                        
                except asyncio.TimeoutError:
                    pass  # Таймаут — проверяем нужен ли fallback
                
                now = asyncio.get_event_loop().time()
                
                # Перезагружаем список рынков (обнаружение новых/удалённых)
                if now - last_market_reload > WS_MARKET_REFRESH_INTERVAL:
                    last_market_reload = now
                    
                    # Проверяем закрытые рынки ПЕРЕД перезагрузкой
                    if trader.markets:
                        async with reposition_lock:
                            resolved = await asyncio.to_thread(trader.check_and_handle_resolved_markets)
                        if resolved:
                            await send_resolved_market_notification(acc.name, resolved, chat_id=owner_chat_id)
                            for res in resolved:
                                mid = res['market_id']
                                ps.update_market(addr, mid, split_phase=0,
                                               yes_position=0, no_position=0)
                                # Отписываемся от закрытого рынка
                                await ws.unsubscribe_orderbook(mid)
                    
                    old_markets = set(trader.markets.keys())
                    await asyncio.to_thread(trader.refresh_markets)
                    new_markets = set(trader.markets.keys())
                    
                    # Подписываемся на новые рынки
                    for mid in new_markets - old_markets:
                        await ws.subscribe_orderbook(mid)
                        title = trader.markets[mid].market.title if trader.markets[mid].market else f"Market {mid}"
                        ps.add_market(addr, mid, title=title)
                        logger.info(f"[{acc.name}] ➕ Новый рынок: #{mid}")
                    
                    # Отписываемся от удалённых рынков
                    for mid in old_markets - new_markets:
                        await ws.unsubscribe_orderbook(mid)
                        logger.info(f"[{acc.name}] ➖ Рынок удалён: #{mid}")
                    
                    # Обрабатываем Phase 1 рынки (новые или после SPLIT)
                    phase1_markets = [mid for mid, s in trader.markets.items() if s.split_phase == 1]
                    for mid in phase1_markets:
                        await handle_phase1_market(mid)
                        await asyncio.sleep(0.5)
                
                # Fallback polling — страховочная проверка
                if now - last_fallback >= WS_FALLBACK_INTERVAL:
                    last_fallback = now
                    fallback_cycle += 1
                    
                    logger.info(f"[{acc.name}] 🔄 Fallback проверка #{fallback_cycle}")
                    
                    # Проверяем закрытые рынки каждые 5 fallback циклов
                    if fallback_cycle % 5 == 0 and trader.markets:
                        async with reposition_lock:
                            resolved = await asyncio.to_thread(trader.check_and_handle_resolved_markets)
                        if resolved:
                            await send_resolved_market_notification(acc.name, resolved, chat_id=owner_chat_id)
                            for res in resolved:
                                mid = res['market_id']
                                ps.update_market(addr, mid, split_phase=0,
                                               yes_position=0, no_position=0)
                                await ws.unsubscribe_orderbook(mid)
                    
                    async with reposition_lock:
                        try:
                            # Запоминаем позиции ДО
                            for market_id, state in trader.markets.items():
                                prev_positions[market_id] = {
                                    'yes': state.yes_position,
                                    'no': state.no_position,
                                }
                            
                            # Помечаем как fallback для менее громкого логирования
                            trader._is_fallback = True
                            result = await asyncio.to_thread(trader.monitor_cycle)
                            trader._is_fallback = False
                            
                            # Обработка результатов (так же как в обычном polling)
                            for market_id, state in trader.markets.items():
                                prev = prev_positions.get(market_id, {'yes': 0, 'no': 0})
                                yes_diff = prev['yes'] - state.yes_position
                                no_diff = prev['no'] - state.no_position
                                
                                if yes_diff > 0.5 or no_diff > 0.5:
                                    ps.record_fill(addr, market_id,
                                                  yes_filled=max(0, yes_diff),
                                                  no_filled=max(0, no_diff))
                                
                                ps.update_market(addr, market_id,
                                                split_phase=state.split_phase,
                                                yes_position=state.yes_position,
                                                no_position=state.no_position)
                            
                            repositioned = result.get('repositioned', [])
                            if repositioned:
                                await send_repositioning_notification(
                                    acc.name,
                                    repositioned,
                                    chat_id=owner_chat_id,
                                    reposition_delay=trader.reposition_delay,
                                    account_address=addr,
                                )
                            
                            if fallback_cycle % 5 == 0:
                                ps.save()
                                
                        except Exception as e:
                            logger.error(f"[{acc.name}] Ошибка fallback цикла: {e}")
                
                # Проверяем что WebSocket жив
                if not ws.connected and bot_state.running.get(addr, False):
                    logger.warning(f"[{acc.name}] ⚠️ WebSocket отключён, ожидаем reconnect...")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{acc.name}] Ошибка в WS цикле: {e}", exc_info=True)
                await asyncio.sleep(2)
        
        # Останавливаем WebSocket
        ws._running = False
        await ws.disconnect()
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
        
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"[{acc.name}] Критическая ошибка WS мониторинга: {e}", exc_info=True)
        await send_error_notification(
            f"Аккаунт: {acc.name}\n"
            f"WebSocket мониторинг упал!\n\n"
            f"{type(e).__name__}: {e}",
            "WS MONITORING CRASH",
            chat_id=owner_chat_id
        )
    finally:
        # Убеждаемся что WS закрыт
        try:
            await ws.disconnect()
        except Exception:
            pass
    
    ps.save()
    logger.info(f"[{acc.name}] WebSocket мониторинг остановлен, состояние сохранено")


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Запуск бота"""
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не установлен в .env!")
        print("   Получите токен у @BotFather и добавьте в .env:")
        print("   TELEGRAM_BOT_TOKEN=your_token_here")
        return
    
    print("🤖 Запуск Telegram бота...")
    
    # Настраиваем request с увеличенными таймаутами для устойчивости к сетевым ошибкам
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=30.0,
        write_timeout=30.0,
        connect_timeout=30.0,
        pool_timeout=30.0,
    )
    
    # Создаём приложение с кастомным request
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .get_updates_request(request)
        .build()
    )
    
    # Сохраняем app в глобальное состояние для отправки уведомлений
    bot_state.app = app
    
    # ConversationHandler для добавления аккаунта
    add_account_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_add_account, pattern="^acc_add$")],
        states={
            STATE_WAITING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_account_name)],
            STATE_WAITING_PRIVATE_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_private_key)],
            STATE_WAITING_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_account_address)],
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
    )
    
    # ConversationHandler для ручного ввода SPLIT
    split_manual_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_split_manual, pattern="^split_manual$")],
        states={
            STATE_WAITING_MARKET_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_market_id)],
            STATE_WAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_split_amount)],
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
    )
    
    # ConversationHandler для ввода своей суммы SPLIT
    split_custom_amount_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_split_custom_amount, pattern="^split_custom_amount$")],
        states={
            STATE_WAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_split_amount)],
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
    )
    
    # Регистрируем handlers
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('help', cmd_help))
    app.add_handler(CommandHandler('status', cmd_status))
    app.add_handler(CommandHandler('accounts', cmd_accounts))
    app.add_handler(CommandHandler('balance', cmd_balance))
    app.add_handler(CommandHandler('markets', cmd_markets))
    app.add_handler(CommandHandler('refresh', cmd_refresh))
    app.add_handler(CommandHandler('split', cmd_split))
    app.add_handler(CommandHandler('start_bot', cmd_start_bot))
    app.add_handler(CommandHandler('stop_bot', cmd_stop_bot))
    
    # Conversation handlers
    app.add_handler(add_account_conv)
    app.add_handler(split_manual_conv)
    app.add_handler(split_custom_amount_conv)
    
    # Main menu callbacks
    app.add_handler(CallbackQueryHandler(callback_main_menu, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(callback_menu_handler, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(callback_ignore, pattern="^ignore$"))
    
    # Split callbacks
    app.add_handler(CallbackQueryHandler(callback_split_account, pattern="^split_acc_"))
    app.add_handler(CallbackQueryHandler(callback_split_type, pattern="^split_type_"))
    app.add_handler(CallbackQueryHandler(callback_split_market, pattern="^split_market_"))
    app.add_handler(CallbackQueryHandler(callback_split_amount, pattern="^split_amount_"))
    
    # Callback handlers
    app.add_handler(CallbackQueryHandler(callback_account_info, pattern="^acc_info_"))
    app.add_handler(CallbackQueryHandler(callback_account_toggle, pattern="^acc_toggle_"))
    app.add_handler(CallbackQueryHandler(callback_account_delete, pattern="^acc_delete_"))
    app.add_handler(CallbackQueryHandler(callback_account_confirm_delete, pattern="^acc_confirm_delete_"))
    app.add_handler(CallbackQueryHandler(callback_back_to_accounts, pattern="^acc_back$"))
    app.add_handler(CallbackQueryHandler(callback_start_account, pattern="^start_acc_"))
    app.add_handler(CallbackQueryHandler(callback_start_all, pattern="^start_all$"))
    app.add_handler(CallbackQueryHandler(callback_stop_account, pattern="^stop_acc_"))
    app.add_handler(CallbackQueryHandler(callback_stop_all, pattern="^stop_all$"))
    
    # Close position callbacks
    app.add_handler(CallbackQueryHandler(callback_close_account, pattern="^close_acc_"))
    app.add_handler(CallbackQueryHandler(callback_close_market, pattern="^close_market_"))
    app.add_handler(CallbackQueryHandler(callback_close_confirm, pattern="^close_confirm_"))
    app.add_handler(CallbackQueryHandler(callback_close_all_markets, pattern="^close_all_markets$"))
    app.add_handler(CallbackQueryHandler(callback_close_all_confirm, pattern="^close_all_confirm$"))
    
    # Restart callbacks
    app.add_handler(CallbackQueryHandler(callback_restart_now, pattern="^restart_now$"))
    
    # Registration callbacks
    app.add_handler(CallbackQueryHandler(callback_register_done, pattern="^register_done$"))
    app.add_handler(CallbackQueryHandler(callback_admin_approve, pattern="^admin_approve_"))
    app.add_handler(CallbackQueryHandler(callback_admin_reject, pattern="^admin_reject_"))
    
    # Referral management callbacks (admin only)
    app.add_handler(CallbackQueryHandler(callback_ref_list, pattern="^ref_list$"))
    app.add_handler(CallbackQueryHandler(callback_ref_info, pattern="^ref_info_"))
    app.add_handler(CallbackQueryHandler(callback_ref_logs, pattern="^ref_logs_"))
    app.add_handler(CallbackQueryHandler(callback_ref_disable, pattern="^ref_disable_"))
    app.add_handler(CallbackQueryHandler(callback_ref_enable, pattern="^ref_enable_"))
    app.add_handler(CallbackQueryHandler(callback_ref_delete, pattern="^ref_delete_"))
    app.add_handler(CallbackQueryHandler(callback_ref_confirm_delete, pattern="^ref_confirm_delete_"))
    
    # Split pagination callbacks
    app.add_handler(CallbackQueryHandler(callback_split_page, pattern="^split_page_"))
    
    # Market info callback (для детального просмотра рынка)
    app.add_handler(CallbackQueryHandler(callback_market_info, pattern="^market_info_"))
    
    # Markets account selector callback
    app.add_handler(CallbackQueryHandler(callback_markets_account, pattern="^markets_acc_"))
    
    # Browse markets callbacks (обзор рынков с категориями и объёмами)
    app.add_handler(CallbackQueryHandler(callback_browse_markets, pattern="^browse_markets_"))
    app.add_handler(CallbackQueryHandler(callback_browse_page, pattern="^browse_page_"))
    app.add_handler(CallbackQueryHandler(callback_browse_detail, pattern="^browse_detail_"))
    app.add_handler(CallbackQueryHandler(callback_browse_split_acc, pattern="^browse_split_acc_"))
    app.add_handler(CallbackQueryHandler(callback_browse_split_pick, pattern="^browse_split_pick_"))
    app.add_handler(CallbackQueryHandler(callback_cmd_markets_menu, pattern="^cmd_markets_menu$"))
    app.add_handler(CallbackQueryHandler(callback_my_positions, pattern="^my_positions$"))
    
    # NegRisk events callbacks (мульти-исходные события)
    app.add_handler(CallbackQueryHandler(callback_browse_events, pattern="^browse_events$"))
    app.add_handler(CallbackQueryHandler(callback_events_page, pattern="^events_page_"))
    app.add_handler(CallbackQueryHandler(callback_event_sub_page, pattern="^event_sub_page_"))
    app.add_handler(CallbackQueryHandler(callback_event_detail, pattern="^event_"))
    
    # Settings callbacks
    app.add_handler(CallbackQueryHandler(callback_settings_handler, pattern="^settings_"))
    
    # Обработчик ЛЮБОГО текста - показывает главное меню (в конце!)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_any_text))
    
    # Глобальный обработчик ошибок
    app.add_error_handler(error_handler)
    
    # Callback при запуске - отправляем уведомление, настраиваем scheduler, восстанавливаем задачи
    async def post_init(application: Application) -> None:
        """Отправить уведомление при запуске бота, настроить scheduler и восстановить задачи"""
        # Настраиваем автоматическое обновление и перезагрузку каждые 1 час
        job_queue = application.job_queue
        if job_queue:
            # Автообновление из git + перезапуск каждый час (3600 секунд)
            bot_state.restart_job = job_queue.run_repeating(
                scheduled_auto_update,
                interval=3600,  # 1 час
                first=3600,  # Первый запуск через 1 час
                name='auto_update'
            )
            logger.info("Scheduled auto-update every 1 hour")
            
            # Очистка логов событий каждые 24 часа
            async def scheduled_clear_logs(context: ContextTypes.DEFAULT_TYPE):
                bot_state.clear_event_logs()
            
            job_queue.run_repeating(
                scheduled_clear_logs,
                interval=86400,  # 24 часа
                first=86400,
                name='clear_event_logs'
            )
            logger.info("Scheduled event log cleanup every 24 hours")
        
        bot_state.last_restart_time = datetime.now()
        
        # =====================================================================
        # ВОССТАНОВЛЕНИЕ ЗАДАЧ ИЗ ПЕРСИСТЕНТНОГО СОСТОЯНИЯ (МУЛЬТИ-ЮЗЕР)
        # =====================================================================
        
        # 1. Восстанавливаем аккаунты админа
        admin_accounts = load_accounts()
        accounts_by_addr = {acc.predict_account_address: acc for acc in admin_accounts}
        
        addresses_to_restore = bot_state.get_accounts_to_restore()
        restored_count = 0
        total_to_restore = len(addresses_to_restore)
        
        if addresses_to_restore:
            logger.info(f"🔄 Восстанавливаем {len(addresses_to_restore)} задач мониторинга (админ)...")
            
            for addr in addresses_to_restore:
                acc = accounts_by_addr.get(addr)
                if not acc:
                    logger.warning(f"⚠️  Аккаунт {addr[:10]}... не найден в accounts.json, пропускаем")
                    continue
                
                try:
                    success = await _restore_monitoring(acc, application, owner_id=TELEGRAM_ADMIN_ID)
                    if success:
                        restored_count += 1
                        logger.info(f"   ✅ {acc.name} — восстановлен")
                    else:
                        logger.error(f"   ❌ {acc.name} — не удалось восстановить")
                except Exception as e:
                    logger.error(f"   ❌ {acc.name} — ошибка: {e}")
        
        # 2. Восстанавливаем аккаунты рефералов
        active_users = []
        try:
            um = get_user_manager()
            active_users = [u for u in um.get_all_users() if u.status == 'active']
            
            for user_info in active_users:
                uid = user_info.telegram_id
                user_accounts = um.load_user_accounts(uid)
                if not user_accounts:
                    continue
                
                user_ps = bot_state.get_persistent_for_user(uid)
                user_addrs = user_ps.get_running_accounts()
                
                if not user_addrs:
                    continue
                
                user_accs_by_addr = {a.predict_account_address: a for a in user_accounts}
                total_to_restore += len(user_addrs)
                
                logger.info(f"🔄 Восстанавливаем {len(user_addrs)} задач для {user_info.display_name}...")
                
                for addr in user_addrs:
                    acc = user_accs_by_addr.get(addr)
                    if not acc:
                        continue
                    try:
                        success = await _restore_monitoring(acc, application, owner_id=uid)
                        if success:
                            restored_count += 1
                            logger.info(f"   ✅ {acc.name} ({user_info.display_name}) — восстановлен")
                        else:
                            logger.error(f"   ❌ {acc.name} ({user_info.display_name}) — не удалось")
                    except Exception as e:
                        logger.error(f"   ❌ {acc.name} ({user_info.display_name}) — ошибка: {e}")
        except Exception as e:
            logger.error(f"Ошибка восстановления рефералов: {e}")
        
        # Сводка восстановления
        restore_text = ""
        if total_to_restore > 0:
            restore_text = f"\n🔄 Восстановлено задач: {restored_count}/{total_to_restore}"
            
            # Информация о рынках из персистентного состояния
            ps = bot_state.persistent
            summary = ps.get_summary()
            if summary['total_markets'] > 0:
                restore_text += f"\n📊 Рынков в мониторинге: {summary['active_markets']}/{summary['total_markets']}"
            if summary['total_imbalance'] > 1.0:
                restore_text += f"\n⚠️ Общий дисбаланс: {summary['total_imbalance']:.1f} токенов"
        
        if TELEGRAM_ADMIN_ID:
            try:
                await application.bot.send_message(
                    chat_id=int(TELEGRAM_ADMIN_ID),
                    text=(
                        "✅ *Бот запущен!*\n\n"
                        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"👥 Аккаунтов (админ): {len(admin_accounts)}\n"
                        f"👤 Рефералов: {len(active_users) if active_users else 0}\n"
                        f"📡 Мониторинг: {'⚡ WebSocket (real-time)' if USE_WEBSOCKET else '🔄 Polling'}\n"
                        f"📬 Уведомления об ошибках: Включены\n"
                        f"🔄 Автообновление: каждый час (git pull + restart)"
                        f"{restore_text}"
                    ),
                    parse_mode='Markdown',
                    reply_markup=get_main_menu_keyboard()
                )
            except Exception as e:
                logger.error(f"Failed to send startup notification: {e}")
    
    app.post_init = post_init
    
    print("✅ Бот запущен!")
    print(f"   Admin ID: {TELEGRAM_ADMIN_ID or 'Не установлен (доступ всем)'}")
    print(f"   📡 Мониторинг: {'⚡ WebSocket (real-time)' if USE_WEBSOCKET else '🔄 Polling'}")
    print("   📬 Уведомления об ошибках: Включены")
    print("   🔄 Автообновление: каждый час (git pull + restart)")
    
    # Запускаем с настройками устойчивости к сетевым ошибкам
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,  # Игнорируем накопившиеся сообщения при старте
        poll_interval=1.0,  # Интервал между запросами
        timeout=30,  # Таймаут long polling
    )


if __name__ == '__main__':
    main()
