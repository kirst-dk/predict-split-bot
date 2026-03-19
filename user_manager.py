# -*- coding: utf-8 -*-
"""
User Manager — Multi-User / Referral System
=============================================

Управление пользователями (рефералами) бота.

Структура данных:
    users/
    ├── users.json              # Реестр всех пользователей
    ├── <telegram_id>/          # Папка данных пользователя
    │   ├── accounts.json       # Аккаунты Predict.fun
    │   └── bot_state.json      # Состояние бота пользователя

Статусы пользователей:
    - pending:  Ожидает одобрения администратора
    - active:   Активный пользователь
    - disabled: Отключён администратором
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, Optional, List
from dataclasses import dataclass, asdict, field
from threading import Lock

from config import AccountConfig

logger = logging.getLogger(__name__)

# Пути
USERS_DIR = os.path.join(os.path.dirname(__file__), 'users')
USERS_FILE = os.path.join(USERS_DIR, 'users.json')


@dataclass
class UserInfo:
    """Информация о пользователе (реферале)"""
    telegram_id: str
    username: str = ""
    first_name: str = ""
    status: str = "pending"  # pending / active / disabled
    joined_at: str = ""
    approved_at: str = ""
    disabled_at: str = ""
    accounts_count: int = 0
    last_active: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'UserInfo':
        known_fields = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    @property
    def display_name(self) -> str:
        parts = []
        if self.first_name:
            parts.append(self.first_name)
        if self.username:
            parts.append(f"({self.username})")
        if not parts:
            parts.append(f"ID:{self.telegram_id}")
        return " ".join(parts)


class UserManager:
    """
    Менеджер пользователей.

    Потокобезопасный, автоматически сохраняет изменения.
    """

    def __init__(self):
        self._lock = Lock()
        self.users: Dict[str, UserInfo] = {}
        self._ensure_dirs()
        self.load()

    # =========================================================================
    # Файловые операции
    # =========================================================================

    def _ensure_dirs(self):
        os.makedirs(USERS_DIR, exist_ok=True)

    def _ensure_user_dir(self, telegram_id: str):
        user_dir = self.get_user_data_dir(telegram_id)
        os.makedirs(user_dir, exist_ok=True)

    def load(self) -> bool:
        """Загрузить реестр пользователей из users.json"""
        with self._lock:
            if not os.path.exists(USERS_FILE):
                return False
            try:
                with open(USERS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.users = {}
                for uid, udata in data.items():
                    self.users[uid] = UserInfo.from_dict(udata)
                logger.info(f"Loaded {len(self.users)} users")
                return True
            except Exception as e:
                logger.error(f"Error loading users.json: {e}")
                return False

    def save(self) -> bool:
        """Сохранить реестр пользователей в users.json"""
        with self._lock:
            try:
                data = {uid: user.to_dict() for uid, user in self.users.items()}
                temp = USERS_FILE + '.tmp'
                with open(temp, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(temp, USERS_FILE)
                return True
            except Exception as e:
                logger.error(f"Error saving users.json: {e}")
                return False

    # =========================================================================
    # CRUD пользователей
    # =========================================================================

    def get_user(self, telegram_id: str) -> Optional[UserInfo]:
        """Получить информацию о пользователе"""
        return self.users.get(telegram_id)

    def add_pending_user(self, telegram_id: str, username: str = "",
                          first_name: str = "") -> UserInfo:
        """Добавить нового пользователя со статусом pending"""
        user = UserInfo(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            status="pending",
            joined_at=datetime.now().isoformat(),
        )
        self.users[telegram_id] = user
        self.save()
        logger.info(f"New pending user: {user.display_name} ({telegram_id})")
        return user

    def approve_user(self, telegram_id: str) -> bool:
        """Одобрить пользователя (pending → active)"""
        user = self.users.get(telegram_id)
        if not user:
            return False
        user.status = "active"
        user.approved_at = datetime.now().isoformat()
        self._ensure_user_dir(telegram_id)
        self.save()
        logger.info(f"User approved: {user.display_name}")
        return True

    def disable_user(self, telegram_id: str) -> bool:
        """Отключить пользователя (active → disabled)"""
        user = self.users.get(telegram_id)
        if not user:
            return False
        user.status = "disabled"
        user.disabled_at = datetime.now().isoformat()
        self.save()
        logger.info(f"User disabled: {user.display_name}")
        return True

    def enable_user(self, telegram_id: str) -> bool:
        """Включить пользователя обратно (disabled → active)"""
        user = self.users.get(telegram_id)
        if not user:
            return False
        user.status = "active"
        user.disabled_at = ""
        self.save()
        logger.info(f"User re-enabled: {user.display_name}")
        return True

    def delete_user(self, telegram_id: str) -> bool:
        """Удалить пользователя из реестра"""
        if telegram_id not in self.users:
            return False
        user = self.users.pop(telegram_id)
        self.save()
        logger.info(f"User deleted: {user.display_name}")
        return True

    # =========================================================================
    # Проверки
    # =========================================================================

    def is_authorized(self, telegram_id: str) -> bool:
        """Проверить, является ли пользователь активным"""
        user = self.users.get(telegram_id)
        return user is not None and user.status == "active"

    def update_last_active(self, telegram_id: str):
        """Обновить время последней активности (не сохраняет на диск каждый раз)"""
        user = self.users.get(telegram_id)
        if user:
            user.last_active = datetime.now().isoformat()

    # =========================================================================
    # Списки пользователей
    # =========================================================================

    def get_all_users(self) -> List[UserInfo]:
        return list(self.users.values())

    def get_active_users(self) -> List[UserInfo]:
        return [u for u in self.users.values() if u.status == "active"]

    def get_pending_users(self) -> List[UserInfo]:
        return [u for u in self.users.values() if u.status == "pending"]

    def get_disabled_users(self) -> List[UserInfo]:
        return [u for u in self.users.values() if u.status == "disabled"]

    # =========================================================================
    # Пути
    # =========================================================================

    def get_user_data_dir(self, telegram_id: str) -> str:
        """Путь к папке данных пользователя"""
        return os.path.join(USERS_DIR, telegram_id)

    def get_user_state_file(self, telegram_id: str) -> str:
        """Путь к bot_state.json пользователя"""
        return os.path.join(self.get_user_data_dir(telegram_id), 'bot_state.json')

    # =========================================================================
    # Per-user аккаунты
    # =========================================================================

    def load_user_accounts(self, telegram_id: str) -> List[AccountConfig]:
        """Загрузить аккаунты конкретного пользователя"""
        user_dir = self.get_user_data_dir(telegram_id)
        accounts_file = os.path.join(user_dir, 'accounts.json')

        if not os.path.exists(accounts_file):
            return []

        try:
            with open(accounts_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return [
                AccountConfig(
                    name=acc.get('name', f'Account {i+1}'),
                    private_key=acc.get('private_key', ''),
                    predict_account_address=acc.get('predict_account_address', '')
                )
                for i, acc in enumerate(data)
            ]
        except Exception as e:
            logger.error(f"Error loading accounts for user {telegram_id}: {e}")
            return []

    def save_user_accounts(self, telegram_id: str,
                            accounts: List[AccountConfig]) -> bool:
        """Сохранить аккаунты пользователя"""
        self._ensure_user_dir(telegram_id)
        accounts_file = os.path.join(
            self.get_user_data_dir(telegram_id), 'accounts.json'
        )

        try:
            data = [
                {
                    'name': acc.name,
                    'private_key': acc.private_key,
                    'predict_account_address': acc.predict_account_address,
                }
                for acc in accounts
            ]
            with open(accounts_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # Обновляем счётчик аккаунтов
            user = self.users.get(telegram_id)
            if user:
                user.accounts_count = len(accounts)
                self.save()

            return True
        except Exception as e:
            logger.error(f"Error saving accounts for user {telegram_id}: {e}")
            return False


# =============================================================================
# Глобальный экземпляр
# =============================================================================

_user_manager: Optional[UserManager] = None


def get_user_manager() -> UserManager:
    """Получить глобальный экземпляр UserManager"""
    global _user_manager
    if _user_manager is None:
        _user_manager = UserManager()
    return _user_manager
