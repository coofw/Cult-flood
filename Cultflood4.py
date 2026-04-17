import asyncio
import sqlite3
import logging
import json
import os
from datetime import datetime
from typing import Optional, Dict, List

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, CallbackQuery, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# ========== КОНФИГ ==========
BOT_TOKEN = "8573524597:AAG4J4zj2v1zXONgzN7mQ_IGeJOwLKD-8h4"
ADMIN_IDS = [8484944484]
CHAT_INVITE_LINK = "https://t.me/+hhgkpuAQe2dkNzIy"
DB_PATH = os.path.join(os.path.dirname(__file__), "cult_flood.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        
        self.conn.commit()
        logger.info("БД инициализирована")

    def is_banned(self, user_id):
        self.cursor.execute("SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,))
        return self.cursor.fetchone() is not None
    
    def add_to_blacklist(self, user_id, reason):
        self.cursor.execute("INSERT OR REPLACE INTO blacklist (user_id, reason, banned_at) VALUES (?, ?, ?)",
                           (user_id, reason, datetime.now().isoformat()))
        self.conn.commit()
    
    def remove_from_blacklist(self, user_id):
        self.cursor.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
        self.conn.commit()
    
    def get_blacklist(self):
        self.cursor.execute("SELECT user_id, reason, banned_at FROM blacklist")
        return self.cursor.fetchall()
    
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
    if user_id in ADMIN_IDS:
        builder.row(KeyboardButton(text="👑 Админ панель"))
    return builder.as_markup(resize_keyboard=True)

def get_admin_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="📋 Тикеты (открытые)"))
    builder.row(KeyboardButton(text="📝 Анкеты (вступление)"))
    builder.row(KeyboardButton(text="📦 Архив"))
    builder.row(KeyboardButton(text="🚫 Чёрный список"))
    builder.row(KeyboardButton(text="🔙 Выйти в меню"))
    return builder.as_markup(resize_keyboard=True)

def get_ticket_actions_keyboard(ticket_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Ответить", callback_data=f"ticket_reply_{ticket_id}")
    builder.button(text="❌ Закрыть", callback_data=f"ticket_close_{ticket_id}")
    builder.button(text="🔙 Назад", callback_data="back_to_tickets")
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

def get_blacklist_actions_keyboard(user_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Разбанить", callback_data=f"unban_{user_id}")
    builder.button(text="🔙 Назад", callback_data="back_to_blacklist")
    return builder.as_markup()

# ========== FSM ==========
class TicketState(StatesGroup):
    waiting_for_message = State()

class AdminReplyState(StatesGroup):
    waiting_for_reply = State()

class RejectReasonState(StatesGroup):
    waiting_for_reason = State()

class ApplicationState(StatesGroup):
    waiting_for_application = State()

# ========== БОТ ==========
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

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
        "📝 **Отправить анкету** — подать заявку на вступление в чат\n\n"
        "После одобрения анкеты вы получите ссылку для входа!",
        reply_markup=get_main_keyboard(user_id)
    )

@dp.message(F.text == "🔙 Выйти в меню")
async def exit_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
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
        f"❌ Закрыть тикет: /close_ticket",
        reply_markup=get_main_keyboard(user_id)
    )
    
    for admin_id in ADMIN_IDS:
        await bot.send_message(
            admin_id,
            f"🆕 **Новый тикет {ticket_number}**\n\n"
            f"👤 Пользователь: @{username} (ID: {user_id})\n"
            f"📝 Сообщение: {msg_text[:200]}\n\n"
            f"📋 /admin — для входа в админ панель"
        )
    
    await state.clear()

@dp.message(Command("close_ticket"))
async def close_user_ticket(message: Message):
    user_id = message.from_user.id
    tickets = db.get_all_open_tickets()
    user_tickets = [t for t in tickets if t[1] == user_id]
    
    if not user_tickets:
        await message.answer("❌ У вас нет открытых тикетов")
        return
    
    ticket_id = user_tickets[0][0]
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
        "5. Роль и Фд:\n"
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
        "5. Обычный участник\n"
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
        'role': ''
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
            answers['role'] = line[2:].strip()
    
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
        f"🎭 Роль: {answers['role'] or '—'}\n\n"
        f"📋 /admin — для входа в админ панель"
    )
    
    for admin_id in ADMIN_IDS:
        await bot.send_message(admin_id, answers_text)

# ========== АДМИН ПАНЕЛЬ ==========
@dp.message(F.text == "👑 Админ панель")
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("👑 **Админ панель**", reply_markup=get_admin_keyboard())

# ========== ТИКЕТЫ ==========
@dp.message(F.text == "📋 Тикеты (открытые)")
async def admin_tickets(message: Message):
    if message.from_user.id not in ADMIN_IDS:
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

@dp.callback_query(F.data.startswith("view_ticket_"))
async def view_ticket(call: CallbackQuery):
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

@dp.callback_query(F.data.startswith("ticket_reply_"))
async def ticket_reply(call: CallbackQuery, state: FSMContext):
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
        f"✏️ Если хотите ответить — просто напишите сообщение сюда"
    )
    
    await message.answer(f"✅ Ответ отправлен пользователю")
    await state.clear()

@dp.callback_query(F.data.startswith("ticket_close_"))
async def ticket_close(call: CallbackQuery):
    ticket_id = int(call.data.split("_")[2])
    ticket = db.get_ticket(ticket_id)
    
    if not ticket:
        await call.answer("Тикет не найден")
        return
    
    user_id = ticket[1]
    username = db.get_username(user_id)
    
    db.close_ticket(ticket_id)
    
    await call.answer("✅ Тикет закрыт и отправлен в архив")
    await call.message.edit_text(f"✅ Тикет #{ticket_id} закрыт и отправлен в архив")
    
    await bot.send_message(user_id, f"✅ Тикет #{ticket_id} закрыт администратором. Спасибо за обращение!")

# ========== АНКЕТЫ ==========
@dp.message(F.text == "📝 Анкеты (вступление)")
async def admin_applications(message: Message):
    if message.from_user.id not in ADMIN_IDS:
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
    text += f"🎭 Роль: {answers.get('role', '—')}"
    
    await call.message.edit_text(text, reply_markup=get_application_actions_keyboard(app_id))
    await call.answer()

@dp.callback_query(F.data.startswith("app_approve_"))
async def approve_application(call: CallbackQuery):
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

# ========== АРХИВ ==========
@dp.message(F.text == "📦 Архив")
async def admin_archive(message: Message):
    if message.from_user.id not in ADMIN_IDS:
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

@dp.callback_query(F.data.startswith("view_archive_"))
async def view_archive_item(call: CallbackQuery):
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

@dp.callback_query(F.data.startswith("archive_restore_"))
async def archive_restore(call: CallbackQuery):
    archive_id = int(call.data.split("_")[2])
    
    if db.restore_ticket_from_archive(archive_id):
        await call.answer("✅ Тикет восстановлен")
        await call.message.edit_text(f"✅ Тикет восстановлен из архива и снова активен.\n\n📋 /admin → Тикеты (открытые)")
    else:
        await call.answer("❌ Ошибка восстановления")

# ========== ЧЁРНЫЙ СПИСОК ==========
@dp.message(F.text == "🚫 Чёрный список")
async def admin_blacklist(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    blacklist = db.get_blacklist()
    
    if not blacklist:
        await message.answer("🚫 Чёрный список пуст")
        return
    
    text = "🚫 **Чёрный список:**\n\n"
    builder = InlineKeyboardBuilder()
    
    for user_id, reason, banned_at in blacklist:
        text += f"🆔 {user_id} | {reason[:30]} | {banned_at[:16]}\n"
        builder.button(text=f"{user_id}", callback_data=f"view_blacklist_{user_id}")
    
    builder.button(text="🔙 Назад", callback_data="back_to_admin")
    builder.adjust(1)
    
    await message.answer(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("view_blacklist_"))
async def view_blacklist_item(call: CallbackQuery):
    user_id = int(call.data.split("_")[2])
    
    db.cursor.execute("SELECT reason, banned_at FROM blacklist WHERE user_id = ?", (user_id,))
    row = db.cursor.fetchone()
    
    if not row:
        await call.answer("Пользователь не в ЧС")
        return
    
    reason, banned_at = row
    
    text = f"🚫 **Пользователь в ЧС**\n\n"
    text += f"🆔 ID: {user_id}\n"
    text += f"📝 Причина: {reason}\n"
    text += f"📅 Забанен: {banned_at[:16]}"
    
    await call.message.edit_text(text, reply_markup=get_blacklist_actions_keyboard(user_id))
    await call.answer()

@dp.callback_query(F.data.startswith("unban_"))
async def unban_user(call: CallbackQuery):
    user_id = int(call.data.split("_")[1])
    
    db.remove_from_blacklist(user_id)
    
    await call.answer("✅ Пользователь разбанен")
    await call.message.edit_text(f"✅ Пользователь {user_id} удалён из чёрного списка")
    
    await bot.send_message(user_id, f"✅ Вы были разбанены администратором. Теперь можете снова отправлять заявки и обращаться в поддержку.")

# ========== НАВИГАЦИЯ ==========
@dp.callback_query(F.data == "back_to_tickets")
async def back_to_tickets(call: CallbackQuery):
    await admin_tickets(call.message)

@dp.callback_query(F.data == "back_to_applications")
async def back_to_applications(call: CallbackQuery):
    await admin_applications(call.message)

@dp.callback_query(F.data == "back_to_blacklist")
async def back_to_blacklist(call: CallbackQuery):
    await admin_blacklist(call.message)

@dp.callback_query(F.data == "back_to_archive")
async def back_to_archive(call: CallbackQuery):
    await admin_archive(call.message)

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(call: CallbackQuery):
    await call.message.delete()
    await admin_panel(call.message)

@dp.message(Command("cancel"))
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id in ADMIN_IDS:
        await admin_panel(message)

# ========== ЗАПУСК ==========
async def main():
    logger.info("✅ Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())