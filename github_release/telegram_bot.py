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
from datetime import datetime
from typing import Optional, Dict, List
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

# Локальные модули
from config import (
    API_KEY, CHAIN_ID, ChainId,
    SPLIT_OFFSET, CHECK_INTERVAL,
    AccountConfig, load_accounts, save_accounts, ACCOUNTS,
    USE_WEBSOCKET, WS_FALLBACK_INTERVAL, WS_MARKET_REFRESH_INTERVAL,
)
from predict_api import PredictAPI, wei_to_float
from predict_trader import PredictTrader
from predict_ws import PredictWebSocket
from state import get_state, PersistentState, MarketTaskState, OrderState

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
    
    def mark_account_running(self, address: str, name: str, running: bool):
        """Отметить аккаунт как запущенный/остановленный (с персистентностью)"""
        self.running[address] = running
        self._persistent.set_account_running(address, name, running)
    
    def get_accounts_to_restore(self) -> List[str]:
        """Получить список аккаунтов которые нужно восстановить"""
        return self._persistent.get_running_accounts()
        
bot_state = BotState()


# =============================================================================
# СИСТЕМА УВЕДОМЛЕНИЙ ОБ ОШИБКАХ
# =============================================================================

async def send_error_notification(error_message: str, error_type: str = "ERROR"):
    """Отправить уведомление об ошибке администратору"""
    if not TELEGRAM_ADMIN_ID or not bot_state.app:
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
            chat_id=int(TELEGRAM_ADMIN_ID),
            text=text,
            parse_mode='Markdown',
            disable_notification=True  # Тихое уведомление
        )
    except Exception as e:
        logger.error(f"Failed to send error notification: {e}")


async def send_repositioning_notification(account_name: str, repositioned_orders: list):
    """Отправить уведомление о переставлении ордеров (тихое, не уводит бота вверх)"""
    if not bot_state.repositioning_notifications:
        return
    
    if not TELEGRAM_ADMIN_ID or not bot_state.app:
        return
    
    if not repositioned_orders:
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
                from config import REPOSITION_COOLDOWN
                lines.append(f"   ⏳ Cooldown между переставлениями: {REPOSITION_COOLDOWN} сек")
        
        lines.append(f"\n🕐 {datetime.now().strftime('%H:%M:%S')}")
        
        text = "\n".join(lines)
        
        # disable_notification=True - сообщение приходит без звука и не уводит чат вверх
        await bot_state.app.bot.send_message(
            chat_id=int(TELEGRAM_ADMIN_ID),
            text=text,
            parse_mode='Markdown',
            disable_notification=True
        )
    except Exception as e:
        logger.error(f"Failed to send repositioning notification: {e}")


async def send_resolved_market_notification(account_name: str, resolved_results: list):
    """Отправить уведомление о закрытии рынков (ГРОМКОЕ — это важное событие)"""
    if not TELEGRAM_ADMIN_ID or not bot_state.app:
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
                chat_id=int(TELEGRAM_ADMIN_ID),
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


# =============================================================================
# ГЛАВНОЕ МЕНЮ
# =============================================================================

def get_reply_keyboard():
    """Постоянная клавиатура внизу экрана (всплывающее меню)"""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Статус"), KeyboardButton("💰 Баланс")],
            [KeyboardButton("👥 Аккаунты"), KeyboardButton("📈 Рынки")],
            [KeyboardButton("⚡ SPLIT"), KeyboardButton("🔴 Закрыть")],
            [KeyboardButton("🤖 Бот"), KeyboardButton("⚙️ Настройки")],
            [KeyboardButton("❓ Как работает?")],
        ],
        resize_keyboard=True,  # Уменьшить размер кнопок
        is_persistent=True,  # Всегда показывать
    )


def get_main_menu_keyboard():
    """Клавиатура главного меню (inline, используется для навигации назад)"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Назад", callback_data="main_menu")],
    ])


@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    await update.message.reply_text(
        "🤖 *Predict.fun Trading Bot*\n\n"
        "Добро пожаловать! Используйте меню внизу для управления ботом.",
        reply_markup=get_reply_keyboard(),
        parse_mode='Markdown'
    )


@admin_only
async def handle_any_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик любого текстового сообщения - обрабатывает кнопки Reply Keyboard"""
    text = update.message.text.strip()
    
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
    elif text == "⚡ SPLIT":
        await cmd_split(update, context)
        return
    elif text == "🔴 Закрыть":
        # Показываем меню закрытия позиций
        accounts = load_accounts()
        if not accounts:
            await update.message.reply_text("📭 Нет аккаунтов", reply_markup=get_reply_keyboard())
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
        running_count = sum(1 for v in bot_state.running.values() if v)
        total_count = len(load_accounts())
        
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
        keyboard = [
            [InlineKeyboardButton(
                f"{'✅' if bot_state.error_notifications else '❌'} Уведомления об ошибках",
                callback_data="settings_toggle_errors"
            )],
            [InlineKeyboardButton(
                f"{'✅' if bot_state.repositioning_notifications else '❌'} Уведомления о репозиционировании",
                callback_data="settings_toggle_repositioning"
            )],
        ]
        await update.message.reply_text(
            "⚙️ *Настройки*\n\nНажмите чтобы переключить:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return
    elif text == "❓ Как работает?":
        await cmd_help(update, context)
        return
    
    # Для любого другого текста - просто показываем подсказку
    await update.message.reply_text(
        "👆 Используйте меню внизу для управления ботом",
        reply_markup=get_reply_keyboard()
    )


@admin_only_callback
async def callback_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в главное меню - просто удаляем inline сообщение"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "👆 Используйте меню внизу для управления ботом"
    )


@admin_only_callback
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
    accounts = load_accounts()
    
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
    accounts = load_accounts()
    
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
            trader = await asyncio.to_thread(get_or_create_trader, acc)
            if trader:
                if not trader.order_builder:
                    await asyncio.to_thread(trader.init_sdk)
                balance = await asyncio.to_thread(trader.get_usdt_balance)
                text += f"👤 *{acc.name}* ({addr_short})\n"
                text += f"   💵 ${balance:.2f} USDT\n\n"
            else:
                text += f"👤 *{acc.name}* - ❌ ошибка\n\n"
        except Exception as e:
            text += f"👤 *{acc.name}* - ❌ {str(e)[:30]}\n\n"
    
    keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


# =============================================================================
# МЕНЮ: ПОМОЩЬ
# =============================================================================

# =============================================================================
# МЕНЮ: НАСТРОЙКИ
# =============================================================================

async def show_settings(query):
    """Показать настройки"""
    repo_status = "✅ Вкл" if bot_state.repositioning_notifications else "❌ Выкл"
    
    text = (
        "⚙️ *Настройки*\n\n"
        f"🔔 Уведомления о переставлении: {repo_status}\n"
        "   _Уведомляет когда ордера переставляются_\n\n"
        f"📊 Всего ошибок: {bot_state.error_count}\n"
    )
    
    if bot_state.last_error_time:
        text += f"🕐 Последняя: {bot_state.last_error_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    
    keyboard = [
        [InlineKeyboardButton(
            f"{'🔕 Выключить' if bot_state.repositioning_notifications else '🔔 Включить'} уведомления о переставлении",
            callback_data="settings_toggle_repo_notif"
        )],
        [InlineKeyboardButton("🔄 Сбросить счётчик ошибок", callback_data="settings_reset_errors")],
        [InlineKeyboardButton("« Главное меню", callback_data="main_menu")]
    ]
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


@admin_only_callback
async def callback_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик настроек"""
    query = update.callback_query
    await query.answer()
    
    action = query.data.replace("settings_", "")
    
    if action == "toggle_repo_notif" or action == "toggle_repositioning":
        bot_state.repositioning_notifications = not bot_state.repositioning_notifications
        status = "включены" if bot_state.repositioning_notifications else "выключены"
        await query.answer(f"Уведомления о переставлении {status}")
        # Показываем обновлённое меню настроек
        keyboard = [
            [InlineKeyboardButton(
                f"{'✅' if bot_state.error_notifications else '❌'} Уведомления об ошибках",
                callback_data="settings_toggle_errors"
            )],
            [InlineKeyboardButton(
                f"{'✅' if bot_state.repositioning_notifications else '❌'} Уведомления о репозиционировании",
                callback_data="settings_toggle_repositioning"
            )],
        ]
        await query.edit_message_text(
            "⚙️ *Настройки*\n\nНажмите чтобы переключить:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif action == "toggle_errors":
        bot_state.error_notifications = not bot_state.error_notifications
        status = "включены" if bot_state.error_notifications else "выключены"
        await query.answer(f"Уведомления об ошибках {status}")
        # Показываем обновлённое меню настроек
        keyboard = [
            [InlineKeyboardButton(
                f"{'✅' if bot_state.error_notifications else '❌'} Уведомления об ошибках",
                callback_data="settings_toggle_errors"
            )],
            [InlineKeyboardButton(
                f"{'✅' if bot_state.repositioning_notifications else '❌'} Уведомления о репозиционировании",
                callback_data="settings_toggle_repositioning"
            )],
        ]
        await query.edit_message_text(
            "⚙️ *Настройки*\n\nНажмите чтобы переключить:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif action == "reset_errors":
        bot_state.error_count = 0
        bot_state.last_error_time = None
        await query.answer("Счётчик ошибок сброшен")
        await show_settings(query)


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
    accounts = load_accounts()
    
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
    accounts = load_accounts()
    
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
    accounts = load_accounts()
    
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


@admin_only_callback
async def callback_close_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор аккаунта для закрытия"""
    query = update.callback_query
    await query.answer()
    
    acc_idx = int(query.data.split('_')[-1])
    accounts = load_accounts()
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    context.user_data['close_account_idx'] = acc_idx
    await show_close_markets(query, context, accounts[acc_idx])


@admin_only_callback
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


@admin_only_callback
async def callback_close_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнение закрытия позиции"""
    query = update.callback_query
    await query.answer()
    
    market_id = int(query.data.split('_')[-1])
    acc_idx = context.user_data.get('close_account_idx', 0)
    
    accounts = load_accounts()
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


@admin_only_callback
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


@admin_only_callback
async def callback_close_all_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнение закрытия всех позиций"""
    query = update.callback_query
    await query.answer()
    
    acc_idx = context.user_data.get('close_account_idx', 0)
    markets_data = context.user_data.get('close_markets', {})
    
    accounts = load_accounts()
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
    accounts = load_accounts()
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


@admin_only_callback
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
    """Перезагрузить всех трейдеров"""
    accounts = load_accounts()
    
    # Сохраняем список запущенных аккаунтов
    running_accounts = [acc for acc in accounts if bot_state.running.get(acc.predict_account_address, False)]
    
    # Останавливаем всех
    for acc in running_accounts:
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
    for acc in running_accounts:
        try:
            if await start_monitoring(acc, context):
                restarted += 1
        except Exception as e:
            logger.error(f"Failed to restart {acc.name}: {e}")
    
    bot_state.last_restart_time = datetime.now()
    
    # Отправляем уведомление
    if not manual:
        await bot_state.app.bot.send_message(
            chat_id=int(TELEGRAM_ADMIN_ID),
            text=(
                f"🔄 *Автоматическая перезагрузка*\n\n"
                f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"📊 Перезапущено: {restarted} аккаунтов\n\n"
                f"_Следующая перезагрузка через 3 часа_"
            ),
            parse_mode='Markdown',
            disable_notification=True  # Тихое уведомление
        )
    
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
    accounts = load_accounts()
    
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
    accounts = load_accounts()
    
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
    accounts = load_accounts()
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

@admin_only
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


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /status - показать статус"""
    accounts = load_accounts()
    
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


@admin_only
async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /accounts - управление аккаунтами"""
    accounts = load_accounts()
    
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


@admin_only_callback
async def callback_account_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Информация об аккаунте"""
    query = update.callback_query
    await query.answer()
    
    acc_idx = int(query.data.split('_')[-1])
    accounts = load_accounts()
    
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


@admin_only_callback
async def callback_account_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск/остановка аккаунта"""
    query = update.callback_query
    await query.answer()
    
    acc_idx = int(query.data.split('_')[-1])
    accounts = load_accounts()
    
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
        success = await start_monitoring(acc, context)
        if success:
            await query.edit_message_text(f"✅ Мониторинг для *{acc.name}* запущен!", parse_mode='Markdown')
        else:
            await query.edit_message_text(f"❌ Ошибка запуска для *{acc.name}*", parse_mode='Markdown')


@admin_only_callback
async def callback_account_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаление аккаунта"""
    query = update.callback_query
    await query.answer()
    
    acc_idx = int(query.data.split('_')[-1])
    accounts = load_accounts()
    
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


@admin_only_callback
async def callback_account_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления"""
    query = update.callback_query
    await query.answer()
    
    acc_idx = int(query.data.split('_')[-1])
    accounts = load_accounts()
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    
    # Останавливаем если работает
    await stop_monitoring(acc)
    
    # Удаляем
    accounts.pop(acc_idx)
    save_accounts(accounts)
    
    await query.edit_message_text(f"🗑 Аккаунт *{acc.name}* удалён", parse_mode='Markdown')


@admin_only_callback
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
    accounts = load_accounts()
    accounts.append(new_account)
    save_accounts(accounts)
    
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


@admin_only_callback
async def callback_back_to_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к списку аккаунтов"""
    query = update.callback_query
    await query.answer()
    
    # Эмулируем /accounts
    accounts = load_accounts()
    
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


@admin_only
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /balance - показать балансы"""
    accounts = load_accounts()
    
    if not accounts:
        await update.message.reply_text("📭 Нет аккаунтов. Добавьте через /accounts")
        return
    
    msg = await update.message.reply_text("⏳ Загрузка балансов...")
    
    text = "💰 *Балансы*\n\n"
    
    for acc in accounts:
        try:
            trader = get_or_create_trader(acc)
            if trader and await asyncio.to_thread(trader.init_sdk):
                balance = await asyncio.to_thread(trader.get_usdt_balance)
                text += f"• {acc.name}: *${balance:.2f}* USDT\n"
            else:
                text += f"• {acc.name}: ❌ Ошибка\n"
        except Exception as e:
            text += f"• {acc.name}: ❌ {str(e)[:30]}\n"
    
    await msg.edit_text(text, parse_mode='Markdown')


@admin_only
async def cmd_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /markets - активные рынки для ВСЕХ аккаунтов"""
    accounts = load_accounts()
    
    if not accounts:
        await update.message.reply_text("📭 Нет аккаунтов")
        return
    
    msg = await update.message.reply_text("⏳ Загрузка рынков...")
    
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
            
            # Загружаем рынки
            await asyncio.to_thread(trader.load_all_markets)
            
            if not trader.markets:
                text += f"👤 *{acc.name}* - 📭 Нет рынков\n\n"
                continue
            
            # Получаем открытые ордера
            all_orders = await asyncio.to_thread(trader.api.get_open_orders)
            
            # Группируем ордера по market_id
            orders_by_market = {}
            for order in all_orders:
                if order.market_id not in orders_by_market:
                    orders_by_market[order.market_id] = {'yes': [], 'no': []}
                
                # Определяем YES/NO по token_id (on_chain_id из outcomes)
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
                
                # Показываем ордера если есть
                market_orders = orders_by_market.get(market_id, {'yes': [], 'no': []})
                yes_orders = market_orders['yes']
                no_orders = market_orders['no']
                
                if yes_orders or no_orders:
                    orders_text = "   📋 Ордера: "
                    parts = []
                    for o in yes_orders:
                        # maker_amount в wei (1e18)
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
    
    header = f"📊 *Активные рынки ({total_markets})*\n\n"
    text = header + text
    text += "_Фазы: ⚪0=нет токенов, 🟡1=есть токены, 🟢2=ордера выставлены_"
    
    # Telegram лимит 4096 символов
    if len(text) > 4000:
        text = text[:3990] + "..._"
    
    await msg.edit_text(text, parse_mode='Markdown')


@admin_only
async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /refresh - принудительное обновление списка рынков"""
    accounts = load_accounts()
    
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


@admin_only
async def cmd_split(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /split - запустить SPLIT"""
    accounts = load_accounts()
    
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


@admin_only_callback
async def callback_split_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор аккаунта для SPLIT - загружаем ВСЕ подходящие рынки"""
    query = update.callback_query
    await query.answer()
    
    acc_idx = int(query.data.split('_')[-1])
    accounts = load_accounts()
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return ConversationHandler.END
    
    context.user_data['split_account_idx'] = acc_idx
    acc = accounts[acc_idx]
    
    await query.edit_message_text("⏳ Загрузка рынков для SPLIT...", parse_mode='Markdown')
    
    try:
        from predict_api import PredictAPI
        api = PredictAPI()
        
        # Получаем все рынки подходящие для SPLIT
        markets = await asyncio.to_thread(api.get_markets_for_split, 100)
        
        if not markets:
            keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
            await query.edit_message_text("📭 Нет подходящих рынков", reply_markup=InlineKeyboardMarkup(keyboard))
            return ConversationHandler.END
        
        # Сохраняем рынки
        context.user_data['split_markets'] = {m.id: m for m in markets}
        
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
        
        return ConversationHandler.END
        
    except Exception as e:
        keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
        await query.edit_message_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END


async def show_split_markets_page(query, context, page: int):
    """Показать страницу рынков для SPLIT с категориями"""
    ITEMS_PER_PAGE = 12  # Увеличили чтобы вмещать заголовки категорий
    
    all_items = context.user_data.get('split_all_items', [])
    volumes = context.user_data.get('split_volumes', {})
    acc_idx = context.user_data.get('split_account_idx', 0)
    
    accounts = load_accounts()
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
            title = market.title[:23] if market.title else f"Market {market.id}"
            vol = format_volume(volumes.get(market.id, 0))
            keyboard.append([
                InlineKeyboardButton(f"#{market.id}: {title}.. [{vol}]", callback_data=f"split_market_{market.id}")
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
    keyboard.append([InlineKeyboardButton("« Главное меню", callback_data="main_menu")])
    
    await query.edit_message_text(
        f"⚡ *SPLIT - {acc_name}*\n\n"
        f"📊 Всего рынков: {total_markets}\n"
        f"📄 Страница {page+1} из {total_pages}\n\n"
        f"_Сгруппировано по категориям, отсортировано по объёму_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


@admin_only_callback
async def callback_split_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение страницы рынков в SPLIT"""
    query = update.callback_query
    await query.answer()
    
    page = int(query.data.split('_')[-1])
    await show_split_markets_page(query, context, page)


@admin_only_callback
async def callback_market_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать детальную информацию о рынке"""
    query = update.callback_query
    await query.answer()
    
    market_id = int(query.data.split('_')[-1])
    await show_market_details(query, context, market_id)


@admin_only_callback
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


@admin_only_callback
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


@admin_only_callback
async def callback_split_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнение SPLIT с выбранной суммой"""
    query = update.callback_query
    await query.answer()
    
    amount = float(query.data.split('_')[-1])
    market_id = context.user_data.get('split_market_id')
    acc_idx = context.user_data.get('split_account_idx', 0)
    
    accounts = load_accounts()
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


@admin_only_callback  
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


@admin_only_callback
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
    
    accounts = load_accounts()
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


@admin_only
async def cmd_start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start_bot - запустить мониторинг"""
    accounts = load_accounts()
    
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


@admin_only_callback
async def callback_start_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск мониторинга для аккаунта"""
    query = update.callback_query
    await query.answer()
    
    acc_idx = int(query.data.split('_')[-1])
    accounts = load_accounts()
    
    if acc_idx >= len(accounts):
        await query.edit_message_text("❌ Аккаунт не найден")
        return
    
    acc = accounts[acc_idx]
    
    await query.edit_message_text(f"⏳ Запуск мониторинга для *{acc.name}*...", parse_mode='Markdown')
    
    success = await start_monitoring(acc, context)
    
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


@admin_only_callback
async def callback_start_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск мониторинга для всех"""
    query = update.callback_query
    await query.answer()
    
    accounts = load_accounts()
    
    await query.edit_message_text("⏳ Запуск всех аккаунтов...")
    
    started = 0
    for acc in accounts:
        if await start_monitoring(acc, context):
            started += 1
    
    keyboard = [[InlineKeyboardButton("« Главное меню", callback_data="main_menu")]]
    await query.edit_message_text(
        f"✅ Запущено {started}/{len(accounts)} аккаунтов",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    


@admin_only
async def cmd_stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stop_bot - остановить мониторинг"""
    accounts = load_accounts()
    
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


@admin_only_callback
async def callback_stop_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Остановка мониторинга для аккаунта"""
    query = update.callback_query
    await query.answer()
    
    acc_idx = int(query.data.split('_')[-1])
    accounts = load_accounts()
    
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


@admin_only_callback
async def callback_stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Остановка всех"""
    query = update.callback_query
    await query.answer()
    
    accounts = load_accounts()
    
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

def get_or_create_trader(acc: AccountConfig) -> Optional[PredictTrader]:
    """Получить или создать трейдера для аккаунта"""
    if acc.predict_account_address in bot_state.traders:
        return bot_state.traders[acc.predict_account_address]
    
    trader = PredictTrader(
        market_ids=None,
        monitor_mode=True,
        account=acc
    )
    
    bot_state.traders[acc.predict_account_address] = trader
    return trader


async def start_monitoring(acc: AccountConfig, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Запустить мониторинг для аккаунта (с персистентностью)"""
    if bot_state.running.get(acc.predict_account_address, False):
        return True  # Уже запущен
    
    try:
        trader = get_or_create_trader(acc)
        
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
        
        # Сохраняем состояние (персистентно!)
        bot_state.mark_account_running(acc.predict_account_address, acc.name, True)
        
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


async def _restore_monitoring(acc: AccountConfig, application: Application) -> bool:
    """
    Восстановить мониторинг для аккаунта при старте бота (без context).
    
    Используется в post_init где нет пользовательского context.
    """
    addr = acc.predict_account_address
    
    if bot_state.running.get(addr, False):
        return True  # Уже запущен
    
    try:
        trader = get_or_create_trader(acc)
        
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
    ps = bot_state.persistent
    
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
                        await send_resolved_market_notification(acc.name, resolved)
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
                                    "FILL IMBALANCE"
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
                    await send_repositioning_notification(acc.name, repositioned)
                
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
                        if TELEGRAM_ADMIN_ID and bot_state.app:
                            await bot_state.app.bot.send_message(
                                chat_id=int(TELEGRAM_ADMIN_ID),
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
                    "MONITORING ERROR"
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
    ps = bot_state.persistent
    
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
    
    async def handle_orderbook_event(market_id: int):
        """Мгновенная реакция на изменение стакана"""
        if market_id not in trader.markets:
            return
        
        state = trader.markets[market_id]
        if state.split_phase < 2:
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
                        await send_repositioning_notification(acc.name, enriched)
                
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
                        if TELEGRAM_ADMIN_ID and bot_state.app:
                            await bot_state.app.bot.send_message(
                                chat_id=int(TELEGRAM_ADMIN_ID),
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
                                "FILL IMBALANCE"
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
                if TELEGRAM_ADMIN_ID and bot_state.app:
                    await bot_state.app.bot.send_message(
                        chat_id=int(TELEGRAM_ADMIN_ID),
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
                if TELEGRAM_ADMIN_ID and bot_state.app:
                    await bot_state.app.bot.send_message(
                        chat_id=int(TELEGRAM_ADMIN_ID),
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
                if TELEGRAM_ADMIN_ID and bot_state.app:
                    await bot_state.app.bot.send_message(
                        chat_id=int(TELEGRAM_ADMIN_ID),
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
                if TELEGRAM_ADMIN_ID and bot_state.app:
                    await bot_state.app.bot.send_message(
                        chat_id=int(TELEGRAM_ADMIN_ID),
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
                if TELEGRAM_ADMIN_ID and bot_state.app:
                    await bot_state.app.bot.send_message(
                        chat_id=int(TELEGRAM_ADMIN_ID),
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
        
        # 5. Запускаем WebSocket listener как отдельную задачу
        ws_task = asyncio.create_task(ws.listen())
        
        # 6. Запускаем обработчик событий
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
                            await send_resolved_market_notification(acc.name, resolved)
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
                            await send_resolved_market_notification(acc.name, resolved)
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
                                await send_repositioning_notification(acc.name, repositioned)
                            
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
            "WS MONITORING CRASH"
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
    
    # Split pagination callbacks
    app.add_handler(CallbackQueryHandler(callback_split_page, pattern="^split_page_"))
    
    # Market info callback (для детального просмотра рынка)
    app.add_handler(CallbackQueryHandler(callback_market_info, pattern="^market_info_"))
    
    # Markets account selector callback
    app.add_handler(CallbackQueryHandler(callback_markets_account, pattern="^markets_acc_"))
    
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
        
        bot_state.last_restart_time = datetime.now()
        
        # =====================================================================
        # ВОССТАНОВЛЕНИЕ ЗАДАЧ ИЗ ПЕРСИСТЕНТНОГО СОСТОЯНИЯ
        # =====================================================================
        accounts = load_accounts()
        accounts_by_addr = {acc.predict_account_address: acc for acc in accounts}
        
        # Получаем адреса аккаунтов, которые были запущены до перезагрузки
        addresses_to_restore = bot_state.get_accounts_to_restore()
        restored_count = 0
        
        if addresses_to_restore:
            logger.info(f"🔄 Восстанавливаем {len(addresses_to_restore)} задач мониторинга...")
            
            for addr in addresses_to_restore:
                acc = accounts_by_addr.get(addr)
                if not acc:
                    logger.warning(f"⚠️  Аккаунт {addr[:10]}... не найден в accounts.json, пропускаем")
                    continue
                
                try:
                    # Создаём фиктивный context для start_monitoring
                    success = await _restore_monitoring(acc, application)
                    if success:
                        restored_count += 1
                        logger.info(f"   ✅ {acc.name} — восстановлен")
                    else:
                        logger.error(f"   ❌ {acc.name} — не удалось восстановить")
                except Exception as e:
                    logger.error(f"   ❌ {acc.name} — ошибка: {e}")
        
        # Сводка восстановления
        restore_text = ""
        if addresses_to_restore:
            restore_text = f"\n🔄 Восстановлено задач: {restored_count}/{len(addresses_to_restore)}"
            
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
                        f"👥 Аккаунтов: {len(accounts)}\n"
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
