import asyncio
import sqlite3
import logging
import json
import os
import secrets
import string
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, CallbackQuery, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# ========== КОНФИГ ==========
BOT_TOKEN = "8573524597:AAG4J4zj2v1zXONgzN7mQ_IGeJOwLKD-8h4"
MASTER_ADMIN_IDS = [8484944484]
CHAT_INVITE_LINK = "https://t.me/+hhgkpuAQe2dkNzIy"
DB_PATH = os.path.join(os.path.dirname(__file__), "cult_flood.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def parse_time_input(time_str: str) -> Tuple[Optional[timedelta], str]:
    """
    Парсит ввод времени: 10m, 2h, 5d, 3M, 1y, 0/never
    Возвращает (timedelta, текст для отображения)
    """
    time_str = time_str.strip().lower()
    
    if time_str == '0' or time_str == 'never':
        return None, "бессрочно"
    
    match = re.match(r'^(\d+)([mhdMy])$', time_str)
    if not match:
        return None, "ошибка"
    
    value = int(match.group(1))
    unit = match.group(2)
    
    if unit == 'm':  # минуты
        return timedelta(minutes=value), f"{value} мин"
    elif unit == 'h':  # часы
        return timedelta(hours=value), f"{value} ч"
    elif unit == 'd':  # дни
        return timedelta(days=value), f"{value} дн"
    elif unit == 'M':  # месяцы (30 дней)
        return timedelta(days=value * 30), f"{value} мес"
    elif unit == 'y':  # годы (365 дней)
        return timedelta(days=value * 365), f"{value} г"
    
    return None, "ошибка"

def format_expires_text(expires_at: Optional[str]) -> str:
    """Форматирует срок для отображения"""
    if not expires_at:
        return "бессрочно"
    try:
        expires_date = datetime.fromisoformat(expires_at)
        if expires_date < datetime.now():
            return "❌ истёк"
        delta = expires_date - datetime.now()
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        
        if days > 0:
            if days >= 365:
                years = days // 365
                return f"{years} г"
            elif days >= 30:
                months = days // 30
                return f"{months} мес"
            else:
                return f"{days} дн"
        elif hours > 0:
            return f"{hours} ч"
        elif minutes > 0:
            return f"{minutes} мин"
        else:
            return "сегодня истекает"
    except:
        return "бессрочно"

# ========== БАЗА ДАННЫХ ==========
class DatabaseManager:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.conn = None
        self.cursor = None
        self.connect()
        self.setup()

    def connect(self):
        try:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.cursor = self.conn.cursor()
            logger.info(f"✅ БД подключена: {self.db_path}")
        except Exception as e:
            logger.error(f"❌ Ошибка БД: {e}")
            raise

    def setup(self):
        self.cursor.execute("PRAGMA journal_mode=WAL")
        self.cursor.execute("PRAGMA synchronous=NORMAL")
        
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_approved INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            created_at TEXT
        )''')
        
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            status TEXT DEFAULT 'open',
            created_at TEXT,
            updated_at TEXT
        )''')
        
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS ticket_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            user_id INTEGER,
            message TEXT,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT
        )''')
        
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            status TEXT DEFAULT 'pending',
            answers TEXT,
            created_at TEXT,
            reviewed_at TEXT,
            review_reason TEXT
        )''')
        
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT,
            original_id INTEGER,
            user_id INTEGER,
            username TEXT,
            data TEXT,
            closed_at TEXT
        )''')
        
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS blacklist (
            user_id INTEGER PRIMARY KEY,
            reason TEXT,
            banned_at TEXT
        )''')
        
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            level INTEGER DEFAULT 1,
            added_by INTEGER,
            added_at TEXT,
            expires_at TEXT
        )''')
        
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS admin_codes (
            code TEXT PRIMARY KEY,
            level INTEGER,
            expires_at TEXT,
            max_uses INTEGER DEFAULT 1,
            used INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT
        )''')
        
        for admin_id in MASTER_ADMIN_IDS:
            self.cursor.execute("INSERT OR IGNORE INTO admins (user_id, level, added_at) VALUES (?, 3, ?)",
                               (admin_id, datetime.now().isoformat()))
        
        self.conn.commit()
        logger.info("БД инициализирована")

    # ========== АДМИНЫ ==========
    def get_admin_level(self, user_id):
        self.cursor.execute("SELECT level, expires_at FROM admins WHERE user_id = ?", (user_id,))
        row = self.cursor.fetchone()
        if not row:
            return 0
        level, expires_at = row
        if expires_at and datetime.fromisoformat(expires_at) < datetime.now():
            self.cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            self.conn.commit()
            return 0
        return level
    
    def add_admin(self, user_id, level, added_by, delta: Optional[timedelta] = None):
        expires_at = None
        if delta:
            expires_at = (datetime.now() + delta).isoformat()
            logger.info(f"Добавлен админ {user_id} на {delta}")
        else:
            logger.info(f"Добавлен админ {user_id} бессрочно")
        
        self.cursor.execute("INSERT OR REPLACE INTO admins (user_id, level, added_by, added_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                           (user_id, level, added_by, datetime.now().isoformat(), expires_at))
        self.conn.commit()
        return True
    
    def remove_admin(self, user_id):
        self.cursor.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        self.conn.commit()
        return True
    
    def get_all_admins(self):
        self.cursor.execute("SELECT user_id, level, added_at, expires_at FROM admins ORDER BY level DESC")
        return self.cursor.fetchall()
    
    def upgrade_admin_level(self, user_id, new_level):
        current_level = self.get_admin_level(user_id)
        if new_level > current_level:
            self.cursor.execute("UPDATE admins SET level = ? WHERE user_id = ?", (new_level, user_id))
            self.conn.commit()
            return True
        return False
    
    # ========== КОДЫ ==========
    def generate_admin_code(self, level, delta: timedelta, max_uses, created_by) -> str:
        code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        expires_at = (datetime.now() + delta).isoformat()
        self.cursor.execute("INSERT INTO admin_codes (code, level, expires_at, max_uses, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (code, level, expires_at, max_uses, created_by, datetime.now().isoformat()))
        self.conn.commit()
        return code
    
    def use_admin_code(self, code, user_id) -> tuple:
        self.cursor.execute("SELECT level, expires_at, max_uses, used FROM admin_codes WHERE code = ?", (code,))
        row = self.cursor.fetchone()
        if not row:
            return False, "❌ Код не найден"
        code_level, expires_at, max_uses, used = row
        if datetime.fromisoformat(expires_at) < datetime.now():
            return False, "❌ Код просрочен"
        if used >= max_uses:
            return False, "❌ Код уже использован максимальное число раз"
        
        current_level = self.get_admin_level(user_id)
        
        if current_level == 0:
            self.add_admin(user_id, code_level, 0)
            self.cursor.execute("UPDATE admin_codes SET used = used + 1 WHERE code = ?", (code,))
            self.conn.commit()
            level_name = {1: "Модератор анкет", 2: "Модератор тикетов", 3: "Главный админ"}[code_level]
            return True, f"✅ Поздравляем! Вы стали {level_name}!\n\nВведите /admin для входа в панель"
        elif code_level > current_level:
            self.upgrade_admin_level(user_id, code_level)
            self.cursor.execute("UPDATE admin_codes SET used = used + 1 WHERE code = ?", (code,))
            self.conn.commit()
            level_name = {1: "Модератора анкет", 2: "Модератора тикетов", 3: "Главного админа"}[code_level]
            return True, f"✅ Поздравляем! Вы повышены до {level_name}!\n\nВведите /admin для входа в панель"
        else:
            return False, f"❌ Ваш текущий уровень ({current_level}) не ниже уровня кода ({code_level})"
    
    def get_all_codes(self):
        self.cursor.execute("SELECT code, level, expires_at, max_uses, used, created_at FROM admin_codes ORDER BY created_at DESC")
        return self.cursor.fetchall()
    
    def delete_code(self, code):
        self.cursor.execute("DELETE FROM admin_codes WHERE code = ?", (code,))
        self.conn.commit()
        return True
    
    # ========== ЧЁРНЫЙ СПИСОК ==========
    def is_banned(self, user_id):
        self.cursor.execute("SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,))
        return self.cursor.fetchone() is not None
    
    def add_to_blacklist(self, user_id, reason):
        self.cursor.execute("INSERT OR REPLACE INTO blacklist (user_id, reason, banned_at) VALUES (?, ?, ?)",
                           (user_id, reason, datetime.now().isoformat()))
        self.conn.commit()
        logger.info(f"Пользователь {user_id} добавлен в ЧС: {reason}")
    
    def remove_from_blacklist(self, user_id):
        self.cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
        self.conn.commit()
        logger.info(f"Пользователь {user_id} удалён из ЧС")
    
    def get_blacklist(self):
        self.cursor.execute("SELECT user_id, reason, banned_at FROM blacklist")
        return self.cursor.fetchall()
    
    # ========== ПОЛЬЗОВАТЕЛИ ==========
    def add_user(self, user_id, username):
        self.cursor.execute("INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
                           (user_id, username, datetime.now().isoformat()))
        self.conn.commit()
    
    def is_approved(self, user_id):
        self.cursor.execute("SELECT is_approved FROM users WHERE user_id = ?", (user_id,))
        row = self.cursor.fetchone()
        return row[0] == 1 if row else False
    
    def approve_user(self, user_id):
        self.cursor.execute("UPDATE users SET is_approved = 1 WHERE user_id = ?", (user_id,))
        self.conn.commit()
    
    def get_username(self, user_id):
        self.cursor.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
        row = self.cursor.fetchone()
        return row[0] if row else str(user_id)
    
    # ========== ТИКЕТЫ ==========
    def create_ticket(self, user_id) -> int:
        self.cursor.execute("INSERT INTO tickets (user_id, created_at, updated_at) VALUES (?, ?, ?)",
                           (user_id, datetime.now().isoformat(), datetime.now().isoformat()))
        self.conn.commit()
        return self.cursor.lastrowid
    
    def get_ticket(self, ticket_id):
        self.cursor.execute("SELECT id, user_id, status, created_at FROM tickets WHERE id = ?", (ticket_id,))
        return self.cursor.fetchone()
    
    def get_all_open_tickets(self):
        self.cursor.execute("SELECT id, user_id, created_at FROM tickets WHERE status = 'open' ORDER BY created_at ASC")
        return self.cursor.fetchall()
    
    def get_user_tickets(self, user_id):
        self.cursor.execute("SELECT id, status, created_at FROM tickets WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        return self.cursor.fetchall()
    
    def add_ticket_message(self, ticket_id, user_id, message, is_admin=False):
        self.cursor.execute("INSERT INTO ticket_messages (ticket_id, user_id, message, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                           (ticket_id, user_id, message, 1 if is_admin else 0, datetime.now().isoformat()))
        self.conn.commit()
        self.cursor.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (datetime.now().isoformat(), ticket_id))
        self.conn.commit()
    
    def get_ticket_messages(self, ticket_id):
        self.cursor.execute("SELECT id, user_id, message, is_admin, created_at FROM ticket_messages WHERE ticket_id = ? ORDER BY created_at ASC", 
                           (ticket_id,))
        return self.cursor.fetchall()
    
    def get_user_open_ticket_id(self, user_id):
        self.cursor.execute("SELECT id FROM tickets WHERE user_id = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1", (user_id,))
        row = self.cursor.fetchone()
        return row[0] if row else None
    
    def close_ticket(self, ticket_id):
        ticket = self.get_ticket(ticket_id)
        if not ticket:
            return False
        user_id = ticket[1]
        username = self.get_username(user_id)
        archive_data = {
            'ticket_id': ticket_id,
            'user_id': user_id,
            'username': username,
            'status': ticket[2],
            'created_at': ticket[3],
            'messages': self.get_ticket_messages(ticket_id)
        }
        self.cursor.execute("INSERT INTO archive (item_type, original_id, user_id, username, data, closed_at) VALUES (?, ?, ?, ?, ?, ?)",
                           ('ticket', ticket_id, user_id, username, json.dumps(archive_data, default=str), datetime.now().isoformat()))
        self.cursor.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        self.cursor.execute("DELETE FROM ticket_messages WHERE ticket_id = ?", (ticket_id,))
        self.conn.commit()
        return True
    
    def restore_ticket_from_archive(self, archive_id):
        self.cursor.execute("SELECT data FROM archive WHERE id = ? AND item_type = 'ticket'", (archive_id,))
        row = self.cursor.fetchone()
        if not row:
            return False
        data = json.loads(row[0])
        original_id = data.get('ticket_id')
        user_id = data.get('user_id')
        self.cursor.execute("INSERT INTO tickets (id, user_id, status, created_at, updated_at) VALUES (?, ?, 'open', ?, ?)",
                           (original_id, user_id, data.get('created_at'), datetime.now().isoformat()))
        for msg in data.get('messages', []):
            self.cursor.execute("INSERT INTO ticket_messages (ticket_id, user_id, message, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                               (original_id, msg[1], msg[2], msg[3], msg[4]))
        self.cursor.execute("DELETE FROM archive WHERE id = ?", (archive_id,))
        self.conn.commit()
        return True
    
    # ========== АНКЕТЫ ==========
    def create_application(self, user_id, username, answers):
        answers_json = json.dumps(answers, ensure_ascii=False)
        self.cursor.execute("INSERT INTO applications (user_id, username, answers, created_at) VALUES (?, ?, ?, ?)",
                           (user_id, username, answers_json, datetime.now().isoformat()))
        self.conn.commit()
        return self.cursor.lastrowid
    
    def get_pending_applications(self):
        self.cursor.execute("SELECT id, user_id, username, answers, created_at FROM applications WHERE status = 'pending' ORDER BY created_at ASC")
        return self.cursor.fetchall()
    
    def get_application(self, app_id):
        self.cursor.execute("SELECT id, user_id, username, answers, status, created_at FROM applications WHERE id = ?", (app_id,))
        return self.cursor.fetchone()
    
    def approve_application(self, app_id, user_id):
        self.cursor.execute("UPDATE applications SET status = 'approved', reviewed_at = ? WHERE id = ?", 
                           (datetime.now().isoformat(), app_id))
        self.conn.commit()
        self.approve_user(user_id)
    
    def reject_application(self, app_id, reason):
        self.cursor.execute("UPDATE applications SET status = 'rejected', reviewed_at = ?, review_reason = ? WHERE id = ?", 
                           (datetime.now().isoformat(), reason, app_id))
        self.conn.commit()
    
    def get_archive_tickets(self):
        self.cursor.execute("SELECT id, original_id, user_id, username, closed_at FROM archive WHERE item_type = 'ticket' ORDER BY closed_at DESC")
        return self.cursor.fetchall()
    
    def get_archive_item(self, archive_id):
        self.cursor.execute("SELECT id, item_type, original_id, user_id, username, data, closed_at FROM archive WHERE id = ?", (archive_id,))
        return self.cursor.fetchone()

db = DatabaseManager()

# ========== КНОПКИ ==========
def get_main_keyboard(user_id):
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="📩 Связь с админом"))
    builder.row(KeyboardButton(text="📝 Отправить анкету"))
    builder.row(KeyboardButton(text="📋 Мои обращения"))
    if db.get_admin_level(user_id) >= 1:
        builder.row(KeyboardButton(text="👑 Админ панель"))
    return builder.as_markup(resize_keyboard=True)

def get_admin_keyboard(user_id):
    level = db.get_admin_level(user_id)
    builder = ReplyKeyboardBuilder()
    
    if level >= 2:
        builder.row(KeyboardButton(text="📋 Тикеты (открытые)"))
        builder.row(KeyboardButton(text="📦 Архив"))
    if level >= 1:
        builder.row(KeyboardButton(text="📝 Анкеты (вступление)"))
    if level >= 3:
        builder.row(KeyboardButton(text="🚫 Чёрный список"))
        builder.row(KeyboardButton(text="👑 Управление админами"))
        builder.row(KeyboardButton(text="🎫 Коды для админки"))
    
    builder.row(KeyboardButton(text="🔙 Выйти в меню"))
    return builder.as_markup(resize_keyboard=True)

def get_ticket_actions_keyboard(ticket_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Ответить", callback_data=f"ticket_reply_{ticket_id}")
    builder.button(text="❌ Закрыть", callback_data=f"ticket_close_{ticket_id}")
    builder.button(text="🔙 Назад", callback_data="back_to_tickets")
    builder.adjust(1)
    return builder.as_markup()

def get_user_ticket_keyboard(ticket_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Ответить", callback_data=f"user_ticket_reply_{ticket_id}")
    builder.button(text="🔙 Назад", callback_data="back_to_my_tickets")
    builder.adjust(1)
    return builder.as_markup()

def get_archive_actions_keyboard(archive_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Восстановить", callback_data=f"archive_restore_{archive_id}")
    builder.button(text="🔙 Назад", callback_data="back_to_archive")
    builder.adjust(1)
    return builder.as_markup()

def get_application_actions_keyboard(app_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"app_approve_{app_id}")
    builder.button(text="❌ Отклонить", callback_data=f"app_reject_{app_id}")
    builder.adjust(2)
    return builder.as_markup()

def get_blacklist_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить в ЧС", callback_data="blacklist_add")
    builder.button(text="🔙 Назад", callback_data="back_to_admin")
    builder.adjust(2)
    return builder.as_markup()

def get_admin_manage_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить админа", callback_data="add_admin")
    builder.button(text="➖ Убрать админа", callback_data="remove_admin")
    builder.button(text="📋 Список админов", callback_data="list_admins")
    builder.button(text="🔙 Назад", callback_data="back_to_admin")
    builder.adjust(1)
    return builder.as_markup()

def get_codes_manage_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать код", callback_data="create_code")
    builder.button(text="📋 Список кодов", callback_data="list_codes")
    builder.button(text="🗑 Удалить код", callback_data="delete_code")
    builder.button(text="🔙 Назад", callback_data="back_to_admin")
    builder.adjust(1)
    return builder.as_markup()

# ========== FSM ==========
class TicketState(StatesGroup):
    waiting_for_message = State()

class AdminReplyState(StatesGroup):
    waiting_for_reply = State()

class UserReplyState(StatesGroup):
    waiting_for_reply = State()

class RejectReasonState(StatesGroup):
    waiting_for_reason = State()

class ApplicationState(StatesGroup):
    waiting_for_application = State()

class AddToBlacklistState(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_reason = State()

class AddAdminState(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_level = State()
    waiting_for_time = State()  # вместо waiting_for_days

class RemoveAdminState(StatesGroup):
    waiting_for_user_id = State()

class CreateCodeState(StatesGroup):
    waiting_for_level = State()
    waiting_for_time = State()
    waiting_for_max_uses = State()

class DeleteCodeState(StatesGroup):
    waiting_for_code = State()

class RedeemCodeState(StatesGroup):
    waiting_for_code = State()

# ========== БОТ ==========
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== ОСНОВНЫЕ КОМАНДЫ ==========
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or f"User{user_id}"
    db.add_user(user_id, username)
    
    if db.is_banned(user_id):
        await message.answer("🚫 **Вы в чёрном списке!**\n\nДля снятия бана обратитесь к @Coofw")
        return
    
    await message.answer(
        "👋 **Добро пожаловать в приёмную Cult Flood!**\n\n"
        "📌 **Связь с админом** — задать вопрос или решить проблему\n"
        "📝 **Отправить анкету** — подать заявку на вступление в чат\n"
        "📋 **Мои обращения** — посмотреть историю тикетов\n\n"
        "🎫 **Активировать код админа** — /redeem КОД\n\n"
        "После одобрения анкеты вы получите ссылку для входа!",
        reply_markup=get_main_keyboard(user_id)
    )

@dp.message(Command("redeem"))
async def redeem_code_start(message: Message, state: FSMContext):
    await state.set_state(RedeemCodeState.waiting_for_code)
    await message.answer(
        "🎫 **Активация кода администратора**\n\n"
        "Введите код, который вы получили:\n\n"
        "🔙 Отмена — /cancel"
    )

@dp.message(RedeemCodeState.waiting_for_code)
async def redeem_code_process(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    success, msg = db.use_admin_code(code, message.from_user.id)
    await state.clear()
    await message.answer(msg, reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(F.text == "🔙 Выйти в меню")
async def exit_admin(message: Message):
    if db.get_admin_level(message.from_user.id) < 1:
        return
    await message.answer("👋 Возврат в главное меню", reply_markup=get_main_keyboard(message.from_user.id))

# ========== СВЯЗЬ С АДМИНОМ ==========
@dp.message(F.text == "📩 Связь с админом")
async def contact_admin(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    if db.is_banned(user_id):
        await message.answer("🚫 Вы в чёрном списке! Обратитесь к @Coofw")
        return
    
    await state.set_state(TicketState.waiting_for_message)
    await message.answer(
        "📝 **Напишите ваше сообщение админу**\n\n"
        "Опишите вопрос или проблему. После отправки создастся тикет и админ ответит.\n\n"
        "🔙 Отмена — /cancel",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(TicketState.waiting_for_message)
async def send_ticket_message(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or f"User{user_id}"
    msg_text = message.text
    
    if msg_text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=get_main_keyboard(user_id))
        return
    
    if not msg_text:
        await message.answer("❌ Сообщение не может быть пустым")
        return
    
    ticket_id = db.create_ticket(user_id)
    db.add_ticket_message(ticket_id, user_id, msg_text)
    
    ticket_number = f"#{user_id}_{ticket_id}"
    
    await message.answer(
        f"✅ **Тикет {ticket_number} создан!**\n\n"
        f"Админ ответит в ближайшее время. Уведомление придёт сюда.\n\n"
        f"📋 **Мои обращения** — для просмотра истории\n\n"
        f"❌ Закрыть тикет: /close_ticket",
        reply_markup=get_main_keyboard(user_id)
    )
    
    admins = db.get_all_admins()
    for admin_id, level, _, _ in admins:
        if level >= 2:
            try:
                await bot.send_message(
                    admin_id,
                    f"🆕 **Новый тикет {ticket_number}**\n\n"
                    f"👤 Пользователь: @{username} (ID: {user_id})\n"
                    f"📝 Сообщение: {msg_text[:200]}\n\n"
                    f"📋 /admin — для входа в админ панель"
                )
            except:
                pass
    
    await state.clear()

@dp.message(Command("close_ticket"))
async def close_user_ticket(message: Message):
    user_id = message.from_user.id
    ticket_id = db.get_user_open_ticket_id(user_id)
    
    if not ticket_id:
        await message.answer("❌ У вас нет открытых тикетов")
        return
    
    db.close_ticket(ticket_id)
    await message.answer(f"✅ Тикет #{ticket_id} закрыт. Спасибо за обращение!")

# ========== ОТПРАВИТЬ АНКЕТУ ==========
@dp.message(F.text == "📝 Отправить анкету")
async def send_application_form(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    if db.is_banned(user_id):
        await message.answer("🚫 Вы в чёрном списке! Обратитесь к @Coofw")
        return
    
    if db.is_approved(user_id):
        await message.answer("✅ Вы уже одобрены! Можете заходить в чат по ссылке ниже.\n\n" + CHAT_INVITE_LINK)
        return
    
    pending = db.get_pending_applications()
    for app in pending:
        if app[1] == user_id:
            await message.answer("⏳ У вас уже есть отправленная анкета. Ожидайте решения админа.")
            return
    
    form = (
        "```\n"
        "📝 АНКЕТА ДЛЯ ВСТУПЛЕНИЯ\n"
        "═══════════════════════════\n\n"
        "1. Ваш @:\n"
        "2. Ваше имя (Не обязательно):\n"
        "3. Ваш возраст (Не обязательно):\n"
        "4. Почему хотите зайти к нам?:\n"
        "5. Роль и фандом:\n"
        "═══════════════════════════\n"
        "```"
    )
    
    await message.answer(
        "📝 **Для подачи заявки скопируйте шаблон ниже, заполните и отправьте одним сообщением:**\n\n"
        f"{form}\n\n"
        "✏️ Пример заполнения:\n"
        "```\n"
        "1. @durov\n"
        "2. Павел\n"
        "3. 38\n"
        "4. Хочу общаться с единомышленниками\n"
        "5. Участник, фандом: Аниме\n"
        "```\n\n"
        "После отправки заявка уйдёт на рассмотрение админу.\n\n"
        "🔙 Отмена — /cancel",
        parse_mode="Markdown"
    )
    
    await state.set_state(ApplicationState.waiting_for_application)

@dp.message(ApplicationState.waiting_for_application)
async def process_application(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or f"User{user_id}"
    text = message.text
    
    if text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено", reply_markup=get_main_keyboard(user_id))
        return
    
    lines = text.strip().split('\n')
    answers = {
        'telegram': '',
        'name': '',
        'age': '',
        'reason': '',
        'role_fandom': ''
    }
    
    for line in lines:
        line = line.strip()
        if line.startswith('1.'):
            answers['telegram'] = line[2:].strip()
        elif line.startswith('2.'):
            answers['name'] = line[2:].strip()
        elif line.startswith('3.'):
            answers['age'] = line[2:].strip()
        elif line.startswith('4.'):
            answers['reason'] = line[2:].strip()
        elif line.startswith('5.'):
            answers['role_fandom'] = line[2:].strip()
    
    app_id = db.create_application(user_id, username, answers)
    
    await state.clear()
    await message.answer(
        f"✅ **Анкета #{app_id} отправлена!**\n\n"
        f"Админ рассмотрит её в ближайшее время. Результат придёт сюда.",
        reply_markup=get_main_keyboard(user_id)
    )
    
    answers_text = (
        f"📝 **Новая анкета #{app_id}**\n\n"
        f"👤 Пользователь: @{username} (ID: {user_id})\n"
        f"📱 Telegram: {answers['telegram'] or '—'}\n"
        f"👤 Имя: {answers['name'] or '—'}\n"
        f"🎂 Возраст: {answers['age'] or '—'}\n"
        f"💭 Причина: {answers['reason'] or '—'}\n"
        f"🎭 Роль и фандом: {answers['role_fandom'] or '—'}\n\n"
        f"📋 /admin — для входа в админ панель"
    )
    
    admins = db.get_all_admins()
    for admin_id, level, _, _ in admins:
        if level >= 1:
            try:
                await bot.send_message(admin_id, answers_text)
            except:
                pass

# ========== МОИ ОБРАЩЕНИЯ ==========
@dp.message(F.text == "📋 Мои обращения")
async def my_tickets(message: Message):
    user_id = message.from_user.id
    tickets = db.get_user_tickets(user_id)
    
    if not tickets:
        await message.answer("📭 У вас нет обращений.\n\nНажмите **📩 Связь с админом** чтобы создать тикет.")
        return
    
    text = "📋 **Ваши обращения:**\n\n"
    builder = InlineKeyboardBuilder()
    
    for ticket in tickets:
        ticket_id, status, created_at = ticket
        status_emoji = "🟢" if status == "open" else "🔴"
        text += f"{status_emoji} Тикет #{ticket_id} | {created_at[:16]}\n"
        builder.button(text=f"Тикет #{ticket_id}", callback_data=f"my_ticket_{ticket_id}")
    
    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("my_ticket_"))
async def view_my_ticket(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[2])
    ticket = db.get_ticket(ticket_id)
    
    if not ticket:
        await call.answer("Тикет не найден", show_alert=True)
        return
    
    user_id = ticket[1]
    if user_id != call.from_user.id:
        await call.answer("Это не ваш тикет", show_alert=True)
        return
    
    messages = db.get_ticket_messages(ticket_id)
    
    text = f"📋 **Тикет #{ticket_id}**\n"
    text += f"📅 Создан: {ticket[3][:16]}\n"
    text += f"📊 Статус: {ticket[2]}\n\n"
    text += "💬 **Переписка:**\n"
    
    for msg in messages:
        sender = "👑 Админ" if msg[3] else "👤 Вы"
        text += f"\n[{sender}] {msg[4][:16]}\n{msg[2]}\n"
    
    if len(text) > 4000:
        text = text[:3900] + "\n... (обрезано)"
    
    await call.message.edit_text(text, reply_markup=get_user_ticket_keyboard(ticket_id))
    await call.answer()

@dp.callback_query(F.data.startswith("user_ticket_reply_"))
async def user_ticket_reply(call: CallbackQuery, state: FSMContext):
    ticket_id = int(call.data.split("_")[3])
    ticket = db.get_ticket(ticket_id)
    
    if not ticket or ticket[1] != call.from_user.id:
        await call.answer("Ошибка", show_alert=True)
        return
    
    await state.update_data(reply_ticket_id=ticket_id)
    await state.set_state(UserReplyState.waiting_for_reply)
    await call.message.answer(f"✏️ Введите ваше сообщение для тикета #{ticket_id}:")
    await call.answer()

@dp.message(UserReplyState.waiting_for_reply)
async def process_user_reply(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = data.get('reply_ticket_id')
    
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await message.answer("❌ Тикет не найден")
        await state.clear()
        return
    
    user_id = ticket[1]
    
    db.add_ticket_message(ticket_id, user_id, message.text, is_admin=False)
    
    admins = db.get_all_admins()
    for admin_id, level, _, _ in admins:
        if level >= 2:
            try:
                await bot.send_message(
                    admin_id,
                    f"💬 **Новое сообщение в тикете #{ticket_id}**\n\n"
                    f"👤 Пользователь: @{db.get_username(user_id)}\n"
                    f"📝 Сообщение: {message.text[:200]}\n\n"
                    f"📋 /admin → Тикеты (открытые)"
                )
            except:
                pass
    
    await message.answer(f"✅ Сообщение отправлено админу")
    await state.clear()

# ========== АДМИН ПАНЕЛЬ ==========
@dp.message(F.text == "👑 Админ панель")
async def admin_panel(message: Message):
    level = db.get_admin_level(message.from_user.id)
    if level < 1:
        return
    await message.answer("👑 **Админ панель**", reply_markup=get_admin_keyboard(message.from_user.id))

# ========== ТИКЕТЫ И АРХИВ (УРОВЕНЬ >=2) ==========
@dp.message(F.text == "📋 Тикеты (открытые)")
async def admin_tickets(message: Message):
    if db.get_admin_level(message.from_user.id) < 2:
        return
    
    tickets = db.get_all_open_tickets()
    
    if not tickets:
        await message.answer("📭 Нет открытых тикетов")
        return
    
    text = "📋 **Открытые тикеты:**\n\n"
    builder = InlineKeyboardBuilder()
    
    for ticket in tickets:
        ticket_id, user_id, created_at = ticket
        username = db.get_username(user_id)
        text += f"🆔 #{ticket_id} | @{username} | {created_at[:16]}\n"
        builder.button(text=f"#{ticket_id} @{username}", callback_data=f"view_ticket_{ticket_id}")
    
    builder.button(text="🔙 Назад", callback_data="back_to_admin")
    builder.adjust(1)
    
    await message.answer(text, reply_markup=builder.as_markup())

@dp.message(F.text == "📦 Архив")
async def admin_archive(message: Message):
    if db.get_admin_level(message.from_user.id) < 2:
        return
    
    archive_items = db.get_archive_tickets()
    
    if not archive_items:
        await message.answer("📦 Архив пуст")
        return
    
    text = "📦 **Архив тикетов:**\n\n"
    builder = InlineKeyboardBuilder()
    
    for item in archive_items:
        archive_id, original_id, user_id, username, closed_at = item
        text += f"🆔 #{original_id} | @{username} | {closed_at[:16]}\n"
        builder.button(text=f"#{original_id}", callback_data=f"view_archive_{archive_id}")
    
    builder.button(text="🔙 Назад", callback_data="back_to_admin")
    builder.adjust(1)
    
    await message.answer(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("view_ticket_"))
async def view_ticket(call: CallbackQuery):
    if db.get_admin_level(call.from_user.id) < 2:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    ticket_id = int(call.data.split("_")[2])
    ticket = db.get_ticket(ticket_id)
    
    if not ticket:
        await call.answer("Тикет не найден", show_alert=True)
        return
    
    user_id = ticket[1]
    username = db.get_username(user_id)
    
    messages = db.get_ticket_messages(ticket_id)
    
    text = f"📋 **Тикет #{ticket_id}**\n"
    text += f"👤 Пользователь: @{username} (ID: {user_id})\n"
    text += f"📅 Создан: {ticket[3][:16]}\n"
    text += f"📊 Статус: {ticket[2]}\n\n"
    text += "💬 **Переписка:**\n"
    
    for msg in messages:
        sender = "👑 Админ" if msg[3] else "👤 Пользователь"
        text += f"\n[{sender}] {msg[4][:16]}\n{msg[2]}\n"
    
    if len(text) > 4000:
        text = text[:3900] + "\n... (обрезано)"
    
    await call.message.edit_text(text, reply_markup=get_ticket_actions_keyboard(ticket_id))
    await call.answer()

@dp.callback_query(F.data.startswith("view_archive_"))
async def view_archive_item(call: CallbackQuery):
    if db.get_admin_level(call.from_user.id) < 2:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    archive_id = int(call.data.split("_")[2])
    item = db.get_archive_item(archive_id)
    
    if not item:
        await call.answer("Запись не найдена")
        return
    
    data = json.loads(item[5])
    messages = data.get('messages', [])
    
    text = f"📦 **Архивный тикет #{data.get('ticket_id')}**\n"
    text += f"👤 Пользователь: @{item[4]} (ID: {item[3]})\n"
    text += f"📅 Создан: {data.get('created_at', '—')[:16]}\n"
    text += f"📅 Закрыт: {item[6][:16]}\n\n"
    text += "💬 **Переписка:**\n"
    
    for msg in messages:
        sender = "👑 Админ" if msg[3] else "👤 Пользователь"
        text += f"\n[{sender}] {msg[4][:16]}\n{msg[2]}\n"
    
    if len(text) > 4000:
        text = text[:3900] + "\n... (обрезано)"
    
    await call.message.edit_text(text, reply_markup=get_archive_actions_keyboard(archive_id))
    await call.answer()

@dp.callback_query(F.data.startswith("ticket_reply_"))
async def ticket_reply(call: CallbackQuery, state: FSMContext):
    if db.get_admin_level(call.from_user.id) < 2:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    ticket_id = int(call.data.split("_")[2])
    await state.update_data(reply_ticket_id=ticket_id)
    await state.set_state(AdminReplyState.waiting_for_reply)
    await call.message.answer(f"✏️ Введите ответ для тикета #{ticket_id}:")
    await call.answer()

@dp.message(AdminReplyState.waiting_for_reply)
async def process_ticket_reply(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = data.get('reply_ticket_id')
    
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await message.answer("❌ Тикет не найден")
        await state.clear()
        return
    
    user_id = ticket[1]
    
    db.add_ticket_message(ticket_id, message.from_user.id, message.text, is_admin=True)
    
    await bot.send_message(
        user_id,
        f"📩 **Ответ админа** (тикет #{ticket_id})\n\n{message.text}\n\n"
        f"✏️ Чтобы ответить — нажмите **📋 Мои обращения** → выберите тикет → **💬 Ответить**"
    )
    
    await message.answer(f"✅ Ответ отправлен пользователю")
    await state.clear()

@dp.callback_query(F.data.startswith("ticket_close_"))
async def ticket_close(call: CallbackQuery):
    if db.get_admin_level(call.from_user.id) < 2:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    ticket_id = int(call.data.split("_")[2])
    ticket = db.get_ticket(ticket_id)
    
    if not ticket:
        await call.answer("Тикет не найден")
        return
    
    user_id = ticket[1]
    
    db.close_ticket(ticket_id)
    
    await call.answer("✅ Тикет закрыт и отправлен в архив")
    await call.message.edit_text(f"✅ Тикет #{ticket_id} закрыт и отправлен в архив")
    
    await bot.send_message(user_id, f"✅ Тикет #{ticket_id} закрыт администратором. Спасибо за обращение!")

@dp.callback_query(F.data.startswith("archive_restore_"))
async def archive_restore(call: CallbackQuery):
    if db.get_admin_level(call.from_user.id) < 3:
        await call.answer("Нет доступа (нужен уровень 3)", show_alert=True)
        return
    
    archive_id = int(call.data.split("_")[2])
    
    if db.restore_ticket_from_archive(archive_id):
        await call.answer("✅ Тикет восстановлен")
        await call.message.edit_text(f"✅ Тикет восстановлен из архива и снова активен.\n\n📋 /admin → Тикеты (открытые)")
    else:
        await call.answer("❌ Ошибка восстановления")

# ========== АНКЕТЫ (УРОВЕНЬ >=1) ==========
@dp.message(F.text == "📝 Анкеты (вступление)")
async def admin_applications(message: Message):
    if db.get_admin_level(message.from_user.id) < 1:
        return
    
    apps = db.get_pending_applications()
    
    if not apps:
        await message.answer("📭 Нет новых анкет")
        return
    
    text = "📝 **Анкеты на вступление:**\n\n"
    builder = InlineKeyboardBuilder()
    
    for app in apps:
        app_id, user_id, username, answers_json, created_at = app
        text += f"🆔 #{app_id} | @{username} | {created_at[:16]}\n"
        builder.button(text=f"#{app_id} @{username}", callback_data=f"view_app_{app_id}")
    
    builder.button(text="🔙 Назад", callback_data="back_to_admin")
    builder.adjust(1)
    
    await message.answer(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("view_app_"))
async def view_application(call: CallbackQuery):
    if db.get_admin_level(call.from_user.id) < 1:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    app_id = int(call.data.split("_")[2])
    app = db.get_application(app_id)
    
    if not app:
        await call.answer("Анкета не найдена")
        return
    
    user_id, username, answers_json, status, created_at = app[1], app[2], app[3], app[4], app[5]
    try:
        answers = json.loads(answers_json) if answers_json else {}
    except:
        answers = {}
    
    text = f"📝 **Анкета #{app_id}**\n\n"
    text += f"👤 Пользователь: @{username} (ID: {user_id})\n"
    text += f"📅 Создана: {created_at[:16]}\n"
    text += f"📊 Статус: {status}\n\n"
    text += f"📱 Telegram: {answers.get('telegram', '—')}\n"
    text += f"👤 Имя: {answers.get('name', '—')}\n"
    text += f"🎂 Возраст: {answers.get('age', '—')}\n"
    text += f"💭 Причина: {answers.get('reason', '—')}\n"
    text += f"🎭 Роль и фандом: {answers.get('role_fandom', '—')}"
    
    await call.message.edit_text(text, reply_markup=get_application_actions_keyboard(app_id))
    await call.answer()

@dp.callback_query(F.data.startswith("app_approve_"))
async def approve_application(call: CallbackQuery):
    if db.get_admin_level(call.from_user.id) < 1:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    app_id = int(call.data.split("_")[2])
    app = db.get_application(app_id)
    
    if not app:
        await call.answer("Анкета не найдена")
        return
    
    user_id, username = app[1], app[2]
    
    db.approve_application(app_id, user_id)
    
    await call.answer("✅ Анкета одобрена")
    await call.message.edit_text(f"✅ Анкета #{app_id} одобрена!\n\nПользователь @{username} получит ссылку.")
    
    await bot.send_message(
        user_id,
        f"🎉 **Ваша анкета одобрена!**\n\n"
        f"Вы можете заходить в флуд по ссылке:\n{CHAT_INVITE_LINK}\n\n"
        f"Добро пожаловать! 🎊"
    )

@dp.callback_query(F.data.startswith("app_reject_"))
async def reject_application_start(call: CallbackQuery, state: FSMContext):
    if db.get_admin_level(call.from_user.id) < 1:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    app_id = int(call.data.split("_")[2])
    await state.update_data(reject_app_id=app_id)
    await state.set_state(RejectReasonState.waiting_for_reason)
    await call.message.answer(f"✏️ Введите причину отклонения для анкеты #{app_id}:")
    await call.answer()

@dp.message(RejectReasonState.waiting_for_reason)
async def process_reject_application(message: Message, state: FSMContext):
    data = await state.get_data()
    app_id = data.get('reject_app_id')
    reason = message.text
    
    app = db.get_application(app_id)
    if not app:
        await message.answer("❌ Анкета не найдена")
        await state.clear()
        return
    
    user_id, username = app[1], app[2]
    
    db.reject_application(app_id, reason)
    
    await message.answer(f"✅ Анкета #{app_id} отклонена")
    await state.clear()
    
    await bot.send_message(
        user_id,
        f"❌ **Ваша анкета отклонена**\n\n"
        f"Причина: {reason}\n\n"
        f"По вопросам обращайтесь к @Coofw"
    )

# ========== ЧЁРНЫЙ СПИСОК (ТОЛЬКО УРОВЕНЬ 3) ==========
@dp.message(F.text == "🚫 Чёрный список")
async def admin_blacklist_view(message: Message):
    if db.get_admin_level(message.from_user.id) < 3:
        await message.answer("❌ Нет доступа. Только главные админы.")
        return
    
    blacklist = db.get_blacklist()
    
    if not blacklist:
        await message.answer("📭 Чёрный список пуст", reply_markup=get_blacklist_keyboard())
        return
    
    text = "🚫 **ЧЁРНЫЙ СПИСОК:**\n\n"
    builder = InlineKeyboardBuilder()
    
    for user_id, reason, banned_at in blacklist:
        username = db.get_username(user_id)
        text += f"🆔 {user_id} | @{username}\n📝 {reason}\n📅 {banned_at[:16]}\n\n"
        builder.button(text=f"❌ {user_id}", callback_data=f"blacklist_remove_{user_id}")
    
    builder.button(text="➕ Добавить в ЧС", callback_data="blacklist_add")
    builder.button(text="🔙 Назад", callback_data="back_to_admin")
    builder.adjust(2)
    
    await message.answer(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data == "blacklist_add")
async def blacklist_add_start(call: CallbackQuery, state: FSMContext):
    if db.get_admin_level(call.from_user.id) < 3:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    await state.set_state(AddToBlacklistState.waiting_for_user_id)
    await call.message.answer(
        "🚫 **Добавление в чёрный список**\n\n"
        "Введите ID пользователя (число):\n"
        "Пример: `123456789`\n\n"
        "🔙 Отмена — /cancel"
    )
    await call.answer()

@dp.message(AddToBlacklistState.waiting_for_user_id)
async def admin_add_blacklist_user_id(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        await state.update_data(ban_user_id=user_id)
        await state.set_state(AddToBlacklistState.waiting_for_reason)
        await message.answer(
            "✏️ Введите причину блокировки:\n"
            "Пример: `Спам в обращениях`\n\n"
            "🔙 Отмена — /cancel"
        )
    except ValueError:
        await message.answer("❌ Ошибка! Введите ЧИСЛО (ID пользователя).")

@dp.message(AddToBlacklistState.waiting_for_reason)
async def admin_add_blacklist_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get('ban_user_id')
    reason = message.text.strip()
    
    if db.is_banned(user_id):
        await message.answer(f"❌ Пользователь {user_id} уже в чёрном списке!")
        await state.clear()
        return
    
    db.add_to_blacklist(user_id, reason)
    username = db.get_username(user_id)
    
    await message.answer(
        f"✅ **Пользователь добавлен в ЧС!**\n\n"
        f"🆔 ID: {user_id}\n"
        f"👤 Username: @{username}\n"
        f"📝 Причина: {reason}\n\n"
        f"Пользователь получил уведомление о блокировке."
    )
    
    try:
        await bot.send_message(
            user_id,
            f"🚫 **Вы добавлены в чёрный список!**\n\n"
            f"Причина: {reason}\n\n"
            f"Вы не можете отправлять анкеты и создавать обращения.\n"
            f"По вопросам разблокировки обратитесь к @Coofw"
        )
    except:
        pass
    
    await state.clear()

@dp.callback_query(F.data.startswith("blacklist_remove_"))
async def blacklist_remove(call: CallbackQuery):
    if db.get_admin_level(call.from_user.id) < 3:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    user_id = int(call.data.split("_")[2])
    
    if not db.is_banned(user_id):
        await call.answer("Пользователь не в ЧС", show_alert=True)
        return
    
    db.remove_from_blacklist(user_id)
    username = db.get_username(user_id)
    
    await call.answer("✅ Пользователь удалён из ЧС", show_alert=True)
    await call.message.edit_text(f"✅ Пользователь {user_id} (@{username}) удалён из чёрного списка")
    
    try:
        await bot.send_message(
            user_id,
            f"✅ **Вы удалены из чёрного списка!**\n\n"
            f"Теперь вы снова можете отправлять анкеты и создавать обращения."
        )
    except:
        pass

# ========== УПРАВЛЕНИЕ АДМИНАМИ (ТОЛЬКО УРОВЕНЬ 3) ==========
@dp.message(F.text == "👑 Управление админами")
async def admin_manage(message: Message):
    if db.get_admin_level(message.from_user.id) < 3:
        return
    await message.answer("👑 **Управление админами**", reply_markup=get_admin_manage_keyboard())

@dp.callback_query(F.data == "add_admin")
async def add_admin_start(call: CallbackQuery, state: FSMContext):
    if db.get_admin_level(call.from_user.id) < 3:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    await state.set_state(AddAdminState.waiting_for_user_id)
    await call.message.answer("✏️ Введите ID пользователя:\n\n🔙 Отмена — /cancel")
    await call.answer()

@dp.message(AddAdminState.waiting_for_user_id)
async def add_admin_user_id(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        await state.update_data(admin_user_id=user_id)
        await state.set_state(AddAdminState.waiting_for_level)
        await message.answer(
            "✏️ Введите уровень админа:\n\n"
            "1 - Модератор анкет\n"
            "2 - Модератор тикетов\n"
            "3 - Главный админ\n\n"
            "🔙 Отмена — /cancel"
        )
    except ValueError:
        await message.answer("❌ Ошибка! Введите ЧИСЛО (ID пользователя).")

@dp.message(AddAdminState.waiting_for_level)
async def add_admin_level(message: Message, state: FSMContext):
    try:
        level = int(message.text.strip())
        if level not in [1, 2, 3]:
            await message.answer("❌ Уровень должен быть 1, 2 или 3")
            return
        await state.update_data(admin_level=level)
        await state.set_state(AddAdminState.waiting_for_time)
        await message.answer(
            "✏️ Введите срок действия:\n\n"
            "Примеры:\n"
            "• 30m — 30 минут\n"
            "• 2h — 2 часа\n"
            "• 5d — 5 дней\n"
            "• 3M — 3 месяца\n"
            "• 1y — 1 год\n"
            "• 0 или never — бессрочно\n\n"
            "🔙 Отмена — /cancel"
        )
    except ValueError:
        await message.answer("❌ Введите число (1, 2 или 3)")

@dp.message(AddAdminState.waiting_for_time)
async def add_admin_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    delta, delta_text = parse_time_input(time_str)
    
    if delta is None and time_str not in ['0', 'never']:
        await message.answer("❌ Неверный формат. Примеры: 30m, 2h, 5d, 3M, 1y, 0, never")
        return
    
    data = await state.get_data()
    user_id = data.get('admin_user_id')
    level = data.get('admin_level')
    
    db.add_admin(user_id, level, message.from_user.id, delta)
    
    level_name = {1: "Модератор анкет", 2: "Модератор тикетов", 3: "Главный админ"}[level]
    
    if delta:
        expires_text = f"на {delta_text}"
    else:
        expires_text = "бессрочно"
    
    await message.answer(f"✅ Админ {user_id} добавлен!\n\nУровень: {level_name}\nСрок: {expires_text}")
    await state.clear()
    
    try:
        await bot.send_message(
            user_id,
            f"👑 **Вы стали администратором!**\n\n"
            f"Ваш уровень: {level_name}\n"
            f"Срок: {expires_text}\n\n"
            f"Введите /admin для входа в панель."
        )
    except:
        pass

@dp.callback_query(F.data == "remove_admin")
async def remove_admin_start(call: CallbackQuery, state: FSMContext):
    if db.get_admin_level(call.from_user.id) < 3:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    await state.set_state(RemoveAdminState.waiting_for_user_id)
    await call.message.answer("✏️ Введите ID пользователя для удаления из админов:\n\n🔙 Отмена — /cancel")
    await call.answer()

@dp.message(RemoveAdminState.waiting_for_user_id)
async def remove_admin_user_id(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        
        if user_id in MASTER_ADMIN_IDS:
            await message.answer("❌ Нельзя удалить главного администратора!")
            await state.clear()
            return
        
        db.remove_admin(user_id)
        await message.answer(f"✅ Админ {user_id} удалён")
        await state.clear()
        
        try:
            await bot.send_message(user_id, f"❌ Ваши права администратора были отозваны.")
        except:
            pass
    except ValueError:
        await message.answer("❌ Ошибка! Введите ЧИСЛО (ID пользователя).")

@dp.callback_query(F.data == "list_admins")
async def list_admins(call: CallbackQuery):
    if db.get_admin_level(call.from_user.id) < 3:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    admins = db.get_all_admins()
    
    if not admins:
        await call.message.answer("📭 Нет администраторов")
        await call.answer()
        return
    
    text = "👑 **Список администраторов:**\n\n"
    for admin_id, level, added_at, expires_at in admins:
        level_name = {1: "Модератор анкет", 2: "Модератор тикетов", 3: "Главный админ"}.get(level, "Неизвестно")
        username = db.get_username(admin_id)
        expires_text = format_expires_text(expires_at)
        text += f"🆔 {admin_id} | @{username}\n   📊 {level_name}\n   📅 {expires_text}\n\n"
    
    await call.message.answer(text)
    await call.answer()

# ========== КОДЫ ДЛЯ АДМИНКИ (ТОЛЬКО УРОВЕНЬ 3) ==========
@dp.message(F.text == "🎫 Коды для админки")
async def admin_codes_menu(message: Message):
    if db.get_admin_level(message.from_user.id) < 3:
        return
    await message.answer("🎫 **Управление кодами для получения админки**\n\n"
                        "Пользователи могут активировать код командой /redeem КОД", 
                        reply_markup=get_codes_manage_keyboard())

@dp.callback_query(F.data == "create_code")
async def create_code_start(call: CallbackQuery, state: FSMContext):
    if db.get_admin_level(call.from_user.id) < 3:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    await state.set_state(CreateCodeState.waiting_for_level)
    await call.message.answer(
        "✏️ Введите уровень админа для кода:\n\n"
        "1 - Модератор анкет\n"
        "2 - Модератор тикетов\n"
        "3 - Главный админ\n\n"
        "🔙 Отмена — /cancel"
    )
    await call.answer()

@dp.message(CreateCodeState.waiting_for_level)
async def create_code_level(message: Message, state: FSMContext):
    try:
        level = int(message.text.strip())
        if level not in [1, 2, 3]:
            await message.answer("❌ Уровень должен быть 1, 2 или 3")
            return
        await state.update_data(code_level=level)
        await state.set_state(CreateCodeState.waiting_for_time)
        await message.answer(
            "✏️ Введите срок действия кода:\n\n"
            "Примеры:\n"
            "• 30m — 30 минут\n"
            "• 2h — 2 часа\n"
            "• 5d — 5 дней\n"
            "• 3M — 3 месяца\n"
            "• 1y — 1 год\n\n"
            "🔙 Отмена — /cancel"
        )
    except ValueError:
        await message.answer("❌ Введите число (1, 2 или 3)")

@dp.message(CreateCodeState.waiting_for_time)
async def create_code_time(message: Message, state: FSMContext):
    time_str = message.text.strip()
    delta, delta_text = parse_time_input(time_str)
    
    if delta is None:
        await message.answer("❌ Неверный формат. Примеры: 30m, 2h, 5d, 3M, 1y")
        return
    
    await state.update_data(code_delta=delta, code_delta_text=delta_text)
    await state.set_state(CreateCodeState.waiting_for_max_uses)
    await message.answer(
        "✏️ Введите максимальное количество использований:\n\n"
        "Пример: 1 (одноразовый), 10 (многоразовый)\n\n"
        "🔙 Отмена — /cancel"
    )

@dp.message(CreateCodeState.waiting_for_max_uses)
async def create_code_max_uses(message: Message, state: FSMContext):
    try:
        max_uses = int(message.text.strip())
        if max_uses <= 0:
            await message.answer("❌ Количество использований должно быть больше 0")
            return
        
        data = await state.get_data()
        level = data.get('code_level')
        delta = data.get('code_delta')
        delta_text = data.get('code_delta_text')
        
        code = db.generate_admin_code(level, delta, max_uses, message.from_user.id)
        
        level_name = {1: "Модератор анкет", 2: "Модератор тикетов", 3: "Главный админ"}[level]
        
        await message.answer(
            f"✅ **Код создан!**\n\n"
            f"🎫 Код: `{code}`\n"
            f"📊 Уровень: {level_name}\n"
            f"📅 Срок: {delta_text}\n"
            f"🔢 Использований: {max_uses}\n\n"
            f"Пользователи могут активировать код командой:\n"
            f"/redeem {code}",
            parse_mode="Markdown"
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите число")

@dp.callback_query(F.data == "list_codes")
async def list_codes(call: CallbackQuery):
    if db.get_admin_level(call.from_user.id) < 3:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    codes = db.get_all_codes()
    
    if not codes:
        await call.message.answer("📭 Нет созданных кодов")
        await call.answer()
        return
    
    text = "🎫 **Список кодов:**\n\n"
    for code, level, expires_at, max_uses, used, created_at in codes:
        level_name = {1: "Анкеты", 2: "Тикеты", 3: "Главный"}[level]
        expires_text = format_expires_text(expires_at)
        text += f"🔹 `{code}`\n   📊 {level_name} | 📅 {expires_text}\n   🔢 {used}/{max_uses}\n\n"
    
    await call.message.answer(text)
    await call.answer()

@dp.callback_query(F.data == "delete_code")
async def delete_code_start(call: CallbackQuery, state: FSMContext):
    if db.get_admin_level(call.from_user.id) < 3:
        await call.answer("Нет доступа", show_alert=True)
        return
    
    await state.set_state(DeleteCodeState.waiting_for_code)
    await call.message.answer("✏️ Введите код для удаления:\n\n🔙 Отмена — /cancel")
    await call.answer()

@dp.message(DeleteCodeState.waiting_for_code)
async def delete_code_process(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    
    if db.delete_code(code):
        await message.answer(f"✅ Код `{code}` удалён")
    else:
        await message.answer(f"❌ Код `{code}` не найден")
    
    await state.clear()

# ========== НАВИГАЦИЯ ==========
@dp.callback_query(F.data == "back_to_tickets")
async def back_to_tickets(call: CallbackQuery):
    await admin_tickets(call.message)

@dp.callback_query(F.data == "back_to_applications")
async def back_to_applications(call: CallbackQuery):
    await admin_applications(call.message)

@dp.callback_query(F.data == "back_to_archive")
async def back_to_archive(call: CallbackQuery):
    await admin_archive(call.message)

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(call: CallbackQuery):
    await call.message.delete()
    await admin_panel(call.message)

@dp.callback_query(F.data == "back_to_my_tickets")
async def back_to_my_tickets(call: CallbackQuery):
    await my_tickets(call.message)

@dp.message(Command("cancel"))
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if db.get_admin_level(message.from_user.id) >= 1:
        await admin_panel(message)
    else:
        await message.answer("❌ У вас нет доступа к админ-панели")

@dp.message(Command("my_level"))
async def cmd_my_level(message: Message):
    level = db.get_admin_level(message.from_user.id)
    if level == 0:
        await message.answer("❌ Вы не являетесь администратором")
    else:
        level_name = {1: "Модератор анкет", 2: "Модератор тикетов", 3: "Главный админ"}[level]
        await message.answer(f"👑 Ваш уровень: {level_name} (уровень {level})")

# ========== ЗАПУСК ==========
async def main():
    logger.info("✅ Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())