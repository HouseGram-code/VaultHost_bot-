"""
bot.py — HostBot v1.0 Beta
Telegram-бот для управления хостингом (приватные чаты только).

Запуск:
    BOT_TOKEN=<token> python bot.py
"""

import logging
import os
import threading
import time

import telebot
from apscheduler.schedulers.background import BackgroundScheduler
from telebot import types

from database import db
from docker_manager import docker_mgr

# ─── config ───────────────────────────────────────────────────────────────────

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
ADMIN_ID    = 5429363551
BOT_VERSION = "1.0 Бета"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
)
logger = logging.getLogger("hostbot")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

# ─── session / state ──────────────────────────────────────────────────────────

# Авторизованные пользователи (in-memory, сбрасывается при рестарте → нужен re-login)
_auth: set = set()

# Машина состояний: user_id → {"state": str, "data": dict}
_states: dict = {}
_states_lock = threading.Lock()

# Имена состояний
S_NONE                     = "none"
S_WAIT_NAME                = "wait_name"
S_WAIT_PASSWORD            = "wait_password"
S_WAIT_SERVER_NAME         = "wait_server_name"
S_WAIT_ZIP                 = "wait_zip"
S_WAIT_PIP                 = "wait_pip"
S_ADMIN_GIVE_CREDITS_WHO   = "adm_gc_who"
S_ADMIN_GIVE_CREDITS_AMT   = "adm_gc_amt"
S_ADMIN_GIVE_SERVER_WHO    = "adm_gs_who"
S_ADMIN_BAN_WHO            = "adm_ban_who"
S_ADMIN_UNBAN_WHO          = "adm_uban_who"


def _state(uid: int) -> dict:
    with _states_lock:
        return _states.get(uid, {"state": S_NONE, "data": {}})


def _set_state(uid: int, state: str, data: dict = None):
    with _states_lock:
        _states[uid] = {"state": state, "data": data or {}}


def _clear_state(uid: int):
    with _states_lock:
        _states.pop(uid, None)


def _is_authed(uid: int) -> bool:
    return uid in _auth


# ─── maintenance ──────────────────────────────────────────────────────────────

MAINTENANCE_MSG = (
    "🔧 <b>Технические работы</b>\n\n"
    "😔 Приносим искренние извинения за временные неудобства!\n\n"
    "⚡ Прямо сейчас наша команда проводит технические работы на серверах. "
    "Возможно, это вызвано повышенной нагрузкой или плановым обновлением системы.\n\n"
    "🙏 Мы прекрасно понимаем, как вам хочется воспользоваться нашими услугами — "
    "пожалуйста, немного подождите, мы стараемся завершить всё максимально быстро!\n\n"
    "⏰ Ориентировочное время восстановления: <b>скоро</b>\n\n"
    "💙 Благодарим вас за терпение и понимание!\n\n"
    "<i>— Команда HostBot</i>"
)


def _maintenance() -> bool:
    return db.get_setting("maintenance") == "1"


def _maintenance_guard(uid: int, cid: int) -> bool:
    """True → бот в тех.работе и юзер не админ → отправили сообщение."""
    if _maintenance() and uid != ADMIN_ID:
        bot.send_message(cid, MAINTENANCE_MSG)
        return True
    return False


# ─── misc helpers ─────────────────────────────────────────────────────────────

def _typing(cid: int, delay: float = 0.4):
    bot.send_chat_action(cid, "typing")
    time.sleep(delay)


def _is_private(msg: types.Message) -> bool:
    return msg.chat.type == "private"


def _auth_guard(uid: int, cid: int) -> bool:
    """True → пользователь не авторизован → отправили подсказку."""
    if not _is_authed(uid):
        bot.send_message(
            cid,
            "🔐 <b>Требуется авторизация</b>\n\nВведите /start для входа в систему.",
        )
        return True
    return False


def _ban_guard(uid: int, cid: int) -> bool:
    user = db.get_user(uid)
    if user and user["is_banned"]:
        bot.send_message(
            cid,
            "🚫 <b>Ваш аккаунт заблокирован.</b>\n\nОбратитесь к администратору.",
        )
        return True
    return False


STATUS_EMOJI = {
    "running":    "🟢",
    "stopped":    "🔴",
    "paused":     "🟡",
    "restarting": "🔄",
    "unknown":    "⚪",
}
STATUS_LABEL = {
    "running":    "Работает",
    "stopped":    "Остановлен",
    "paused":     "Пауза",
    "restarting": "Перезагрузка",
    "unknown":    "Неизвестно",
}


def _fmt_status(status: str) -> str:
    e = STATUS_EMOJI.get(status, "⚪")
    l = STATUS_LABEL.get(status, status)
    return f"{e} {l}"


# ─── keyboards ────────────────────────────────────────────────────────────────

def _kb_main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton("🖥 Серверы"),
        types.KeyboardButton("➕ Создать сервер"),
        types.KeyboardButton("👤 Профиль"),
        types.KeyboardButton("ℹ️ Версия"),
    )
    return kb


def _kb_server_manage(server_id: int):
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        types.InlineKeyboardButton("▶️ Запустить",     callback_data=f"s_start_{server_id}"),
        types.InlineKeyboardButton("⏹ Остановить",    callback_data=f"s_stop_{server_id}"),
    )
    mk.add(
        types.InlineKeyboardButton("🔄 Перезагрузить", callback_data=f"s_restart_{server_id}"),
    )
    mk.add(
        types.InlineKeyboardButton("📦 Загрузить ZIP", callback_data=f"s_zip_{server_id}"),
        types.InlineKeyboardButton("📥 pip install",  callback_data=f"s_pip_{server_id}"),
    )
    mk.add(types.InlineKeyboardButton("◀️ К списку серверов", callback_data="s_list"))
    return mk


# ─── texts ────────────────────────────────────────────────────────────────────

def _server_card(srv: dict) -> str:
    status = srv.get("status", "stopped")
    return (
        f"⚙️ <b>Управление сервером</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🖥 Имя:     <b>{srv['name']}</b>\n"
        f"📊 Статус:  <b>{_fmt_status(status)}</b>\n"
        f"🆔 ID:      <code>{srv['id']}</code>\n"
        f"📅 Создан:  {str(srv['created_at'])[:10]}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💾 Ресурсы: 50 МБ RAM · 0.25 vCPU"
    )


# ─── /start ───────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(msg: types.Message):
    if not _is_private(msg):
        bot.reply_to(msg, "❌ Бот работает только в личных сообщениях.")
        return
    uid, cid = msg.from_user.id, msg.chat.id
    if _maintenance_guard(uid, cid):
        return
    _typing(cid)

    user = db.get_user(uid)
    if user:
        if user["is_banned"]:
            bot.send_message(cid, "🚫 <b>Ваш аккаунт заблокирован.</b>")
            return
        bot.send_message(
            cid,
            f"👋 <b>С возвращением, {user['name']}!</b>\n\n"
            "🔐 Введите ваш <b>пароль</b> для входа:",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        _set_state(uid, S_WAIT_PASSWORD, {"existing": True})
    else:
        bot.send_message(
            cid,
            "🌟 <b>Добро пожаловать в HostBot!</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🚀 Ваш персональный хостинг прямо в Telegram\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📝 Давайте создадим аккаунт!\n\n"
            "👤 Введите ваше <b>имя</b> (2–32 символа):",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        _set_state(uid, S_WAIT_NAME)


# ─── /help ────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["help"])
def cmd_help(msg: types.Message):
    if not _is_private(msg):
        return
    uid, cid = msg.from_user.id, msg.chat.id
    if _maintenance_guard(uid, cid) or _auth_guard(uid, cid):
        return
    _send_commands(cid, uid)


def _send_commands(cid: int, uid: int):
    text = (
        "📋 <b>Список команд</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🖥 <b>Серверы</b>\n"
        "  /servers — Список ваших серверов\n"
        "  /create  — Создать новый сервер\n\n"
        "👤 <b>Аккаунт</b>\n"
        "  /profile — Ваш профиль и баланс\n"
        "  /version — Версия бота\n\n"
        "❓ <b>Помощь</b>\n"
        "  /help    — Эта справка\n"
        "  /cancel  — Отменить текущее действие\n"
    )
    if uid == ADMIN_ID:
        text += "\n🔑 <b>Администратор</b>\n  /admin — Панель управления\n"
    text += "\n━━━━━━━━━━━━━━━━━━━━━"
    bot.send_message(cid, text, reply_markup=_kb_main_menu())


# ─── /version ─────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["version"])
def cmd_version(msg: types.Message):
    if not _is_private(msg):
        return
    uid, cid = msg.from_user.id, msg.chat.id
    if _maintenance_guard(uid, cid):
        return
    _typing(cid, 0.3)
    bot.send_message(
        cid,
        f"🤖 <b>HostBot</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Версия:  <b>{BOT_VERSION}</b>\n"
        f"🔧 Статус:  <b>Бета-тестирование</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✨ Спасибо, что используете HostBot!",
    )


# ─── /cancel ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["cancel"])
def cmd_cancel(msg: types.Message):
    if not _is_private(msg):
        return
    uid, cid = msg.from_user.id, msg.chat.id
    _clear_state(uid)
    bot.send_message(cid, "❌ Действие отменено.", reply_markup=_kb_main_menu() if _is_authed(uid) else types.ReplyKeyboardRemove())


# ─── /profile ─────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["profile"])
def cmd_profile(msg: types.Message):
    if not _is_private(msg):
        return
    uid, cid = msg.from_user.id, msg.chat.id
    if _maintenance_guard(uid, cid) or _auth_guard(uid, cid) or _ban_guard(uid, cid):
        return
    _show_profile(cid, uid)


def _show_profile(cid: int, uid: int, message_id: int = None):
    user    = db.get_user(uid)
    servers = db.get_servers(uid)
    running = sum(1 for s in servers if s["status"] == "running")

    text = (
        f"👤 <b>Профиль пользователя</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID:       <code>{uid}</code>\n"
        f"👤 Имя:      <b>{user['name']}</b>\n"
        f"💰 Баланс:   <b>{user['credits']} кредитов</b>\n"
        f"🖥 Серверов: <b>{len(servers)}</b>  (запущено: {running})\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💡 <i>-1 кредит/час за каждый работающий сервер</i>"
    )
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(types.InlineKeyboardButton("❓ Частые вопросы", callback_data="faq_menu"))

    if message_id:
        try:
            bot.edit_message_text(text, cid, message_id, reply_markup=mk)
            return
        except Exception:
            pass
    bot.send_message(cid, text, reply_markup=mk)


# ─── /servers ─────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["servers"])
def cmd_servers(msg: types.Message):
    if not _is_private(msg):
        return
    uid, cid = msg.from_user.id, msg.chat.id
    if _maintenance_guard(uid, cid) or _auth_guard(uid, cid) or _ban_guard(uid, cid):
        return
    _typing(cid, 0.3)
    _show_server_list(cid, uid)


def _show_server_list(cid: int, uid: int, edit_mid: int = None):
    servers = db.get_servers(uid)
    mk = types.InlineKeyboardMarkup(row_width=1)

    if not servers:
        text = (
            "🖥 <b>Ваши серверы</b>\n\n"
            "📭 У вас пока нет серверов.\n"
            "Создайте первый — нажмите кнопку ниже!"
        )
        mk.add(types.InlineKeyboardButton("➕ Создать сервер", callback_data="new_server"))
    else:
        text = "🖥 <b>Ваши серверы</b>\n\n━━━━━━━━━━━━━━━━━━━━━\n"
        for s in servers:
            text += f"{STATUS_EMOJI.get(s['status'], '⚪')} <b>{s['name']}</b>\n"
            mk.add(
                types.InlineKeyboardButton(
                    f"⚙️  {s['name']}",
                    callback_data=f"s_manage_{s['id']}",
                )
            )
        text += "━━━━━━━━━━━━━━━━━━━━━"
        mk.add(types.InlineKeyboardButton("➕ Создать сервер", callback_data="new_server"))

    if edit_mid:
        try:
            bot.edit_message_text(text, cid, edit_mid, reply_markup=mk)
            return
        except Exception:
            pass
    bot.send_message(cid, text, reply_markup=mk)


# ─── /create ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["create"])
def cmd_create(msg: types.Message):
    if not _is_private(msg):
        return
    uid, cid = msg.from_user.id, msg.chat.id
    if _maintenance_guard(uid, cid) or _auth_guard(uid, cid) or _ban_guard(uid, cid):
        return
    user = db.get_user(uid)
    if user["credits"] <= 0:
        bot.send_message(cid, "❌ <b>Недостаточно кредитов</b> для создания сервера.")
        return
    _typing(cid, 0.3)
    bot.send_message(cid, "🖥 <b>Создание сервера</b>\n\n📝 Введите <b>название</b> сервера:")
    _set_state(uid, S_WAIT_SERVER_NAME)


# ─── /admin ───────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["admin"])
def cmd_admin(msg: types.Message):
    if not _is_private(msg):
        return
    uid, cid = msg.from_user.id, msg.chat.id
    if uid != ADMIN_ID:
        bot.send_message(cid, "❌ Нет доступа.")
        return
    _show_admin(cid)


def _show_admin(cid: int):
    maint = _maintenance()
    status_label = "🔴 Активны" if maint else "🟢 Не активны"
    btn_label    = "🔴 Выключить тех. работу" if maint else "🟢 Включить тех. работу"

    text = (
        f"🔑 <b>Панель администратора</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔧 Тех. работы: <b>{status_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        types.InlineKeyboardButton("💰 Выдать кредиты", callback_data="a_credits"),
        types.InlineKeyboardButton("🖥 Выдать сервер",  callback_data="a_server"),
    )
    mk.add(
        types.InlineKeyboardButton("🚫 Забанить",  callback_data="a_ban"),
        types.InlineKeyboardButton("✅ Разбанить", callback_data="a_unban"),
    )
    mk.add(types.InlineKeyboardButton(btn_label, callback_data="a_maint"))
    bot.send_message(cid, text, reply_markup=mk)


# ─── callback query router ────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call: types.CallbackQuery):
    uid  = call.from_user.id
    cid  = call.message.chat.id
    mid  = call.message.message_id
    data = call.data

    if _maintenance() and uid != ADMIN_ID:
        bot.answer_callback_query(call.id, "🔧 Ведутся технические работы", show_alert=True)
        return

    # ── FAQ ──────────────────────────────────────────────────────────────────
    if data == "faq_menu":
        bot.answer_callback_query(call.id)
        _show_faq_menu(cid, mid)
        return
    if data.startswith("faq_"):
        bot.answer_callback_query(call.id)
        _show_faq_item(cid, mid, data[4:], uid)
        return

    # ── server list ──────────────────────────────────────────────────────────
    if data == "s_list":
        bot.answer_callback_query(call.id)
        _show_server_list(cid, uid, edit_mid=mid)
        return
    if data == "new_server":
        bot.answer_callback_query(call.id)
        user = db.get_user(uid)
        if not user or user["credits"] <= 0:
            bot.send_message(cid, "❌ Недостаточно кредитов.")
            return
        bot.send_message(cid, "🖥 <b>Создание сервера</b>\n\n📝 Введите название сервера:")
        _set_state(uid, S_WAIT_SERVER_NAME)
        return

    # ── server management panel ──────────────────────────────────────────────
    if data.startswith("s_manage_"):
        server_id = int(data.split("_")[-1])
        bot.answer_callback_query(call.id)
        _show_manage(cid, uid, server_id, mid)
        return

    if data.startswith("s_start_"):
        server_id = int(data.split("_")[-1])
        bot.answer_callback_query(call.id, "⏳ Запускаю...")
        _do_server_action(cid, uid, server_id, "start")
        return
    if data.startswith("s_stop_"):
        server_id = int(data.split("_")[-1])
        bot.answer_callback_query(call.id, "⏳ Останавливаю...")
        _do_server_action(cid, uid, server_id, "stop")
        return
    if data.startswith("s_restart_"):
        server_id = int(data.split("_")[-1])
        bot.answer_callback_query(call.id, "⏳ Перезагружаю...")
        _do_server_action(cid, uid, server_id, "restart")
        return

    if data.startswith("s_zip_"):
        server_id = int(data.split("_")[-1])
        bot.answer_callback_query(call.id)
        bot.send_message(
            cid,
            "📦 <b>Загрузка ZIP</b>\n\nОтправьте ZIP-файл для загрузки на сервер.\n"
            "Он будет распакован в <code>/app</code>.",
        )
        _set_state(uid, S_WAIT_ZIP, {"server_id": server_id})
        return

    if data.startswith("s_pip_"):
        server_id = int(data.split("_")[-1])
        bot.answer_callback_query(call.id)
        bot.send_message(
            cid,
            "📥 <b>pip install</b>\n\n"
            "Введите название пакета (или несколько через пробел):\n"
            "<i>Пример: requests flask numpy==1.26.4</i>",
        )
        _set_state(uid, S_WAIT_PIP, {"server_id": server_id})
        return

    # ── admin ─────────────────────────────────────────────────────────────────
    if uid != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Нет доступа")
        return

    if data == "a_credits":
        bot.answer_callback_query(call.id)
        bot.send_message(cid, "💰 Введите <b>ID пользователя</b>, которому выдать кредиты:")
        _set_state(uid, S_ADMIN_GIVE_CREDITS_WHO)

    elif data == "a_server":
        bot.answer_callback_query(call.id)
        bot.send_message(cid, "🖥 Введите <b>ID пользователя</b>, которому выдать сервер:")
        _set_state(uid, S_ADMIN_GIVE_SERVER_WHO)

    elif data == "a_ban":
        bot.answer_callback_query(call.id)
        bot.send_message(cid, "🚫 Введите <b>ID пользователя</b> для блокировки:")
        _set_state(uid, S_ADMIN_BAN_WHO)

    elif data == "a_unban":
        bot.answer_callback_query(call.id)
        bot.send_message(cid, "✅ Введите <b>ID пользователя</b> для разблокировки:")
        _set_state(uid, S_ADMIN_UNBAN_WHO)

    elif data == "a_maint":
        new = "0" if _maintenance() else "1"
        db.set_setting("maintenance", new)
        label = "включены ✅" if new == "1" else "выключены ✅"
        bot.answer_callback_query(call.id, f"Тех. работы {label}", show_alert=True)
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass
        _show_admin(cid)


# ─── server management helpers ────────────────────────────────────────────────

def _show_manage(cid: int, uid: int, server_id: int, mid: int = None):
    srv = db.get_server(server_id)
    if not srv or srv["user_id"] != uid:
        bot.send_message(cid, "❌ Сервер не найден.")
        return
    # Sync real status from Docker
    if srv.get("container_id"):
        real = docker_mgr.status(srv["container_id"])
        if real != srv["status"]:
            db.update_server(server_id, status=real)
            srv["status"] = real

    text = _server_card(srv)
    mk   = _kb_server_manage(server_id)

    if mid:
        try:
            bot.edit_message_text(text, cid, mid, reply_markup=mk)
            return
        except Exception:
            pass
    bot.send_message(cid, text, reply_markup=mk)


def _do_server_action(cid: int, uid: int, server_id: int, action: str):
    srv = db.get_server(server_id)
    if not srv or srv["user_id"] != uid:
        bot.send_message(cid, "❌ Сервер не найден.")
        return

    user = db.get_user(uid)
    if action == "start" and user["credits"] <= 0:
        bot.send_message(cid, "❌ <b>Кредиты закончились.</b> Пополните баланс у администратора.")
        return

    action_msgs = {
        "start":   ("⏳ <b>Запускаю сервер…</b>",        "✅ Сервер <b>запущен</b>",        "running"),
        "stop":    ("⏳ <b>Останавливаю сервер…</b>",    "✅ Сервер <b>остановлен</b>",      "stopped"),
        "restart": ("🔄 <b>Перезагружаю сервер…</b>",   "✅ Сервер <b>перезагружен</b>",    "running"),
    }
    wait_text, ok_text, new_status = action_msgs[action]
    msg = bot.send_message(cid, wait_text)

    def _run():
        nonlocal srv
        # Provision container if missing
        if not srv.get("container_id") and action == "start":
            cid_docker, err = docker_mgr.provision(server_id, srv["name"])
            if err:
                bot.edit_message_text(f"❌ Ошибка создания контейнера:\n<code>{err}</code>", cid, msg.message_id)
                return
            db.update_server(server_id, container_id=cid_docker)
            srv = db.get_server(server_id)

        if not srv.get("container_id"):
            bot.edit_message_text("❌ Контейнер не создан. Сначала нажмите <b>Запустить</b>.", cid, msg.message_id)
            return

        fn = {"start": docker_mgr.start, "stop": docker_mgr.stop, "restart": docker_mgr.restart}[action]
        success, err = fn(srv["container_id"])

        if success:
            db.update_server(server_id, status=new_status)
            bot.edit_message_text(
                f"{ok_text}\n\n🖥 <b>{srv['name']}</b> — {_fmt_status(new_status)}",
                cid, msg.message_id,
            )
        else:
            bot.edit_message_text(
                f"❌ Ошибка операции:\n<code>{err}</code>",
                cid, msg.message_id,
            )

    threading.Thread(target=_run, daemon=True).start()


# ─── FAQ ──────────────────────────────────────────────────────────────────────

_FAQ = {
    "1": (
        "🆓 <b>Бесплатные серверы</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Бесплатные серверы могут быть <b>временно недоступны</b> "
        "в периоды высокой нагрузки на инфраструктуру.\n\n"
        "📊 Ресурсы распределяются между всеми пользователями, поэтому "
        "в пиковые часы скорость работы может снизиться.\n\n"
        "💡 <b>Совет:</b> Запускайте серверы в ночное время — "
        "нагрузка минимальна, скорость максимальна."
    ),
    "2": (
        "💳 <b>Платные тарифы — скоро!</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 Совсем скоро мы запустим платные тарифы с расширенными возможностями:\n\n"
        "  ✅ Гарантированные ресурсы (512 МБ — 2 ГБ RAM)\n"
        "  ✅ Выделенный CPU без ограничений\n"
        "  ✅ Приоритетная техподдержка\n"
        "  ✅ Автоматическое резервное копирование\n"
        "  ✅ Постоянный IP-адрес\n\n"
        "📬 Следите за обновлениями — анонс уже близко!"
    ),
    "3": (
        "💡 <b>Полезные советы</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔋 <b>Экономия кредитов:</b>\n"
        "Останавливайте серверы, когда они не нужны — "
        "кредиты списываются только за <i>работающие</i> серверы (-1/час).\n\n"
        "📦 <b>ZIP-загрузка:</b>\n"
        "Упакуйте весь проект в ZIP и загрузите одним файлом — "
        "он автоматически распакуется в <code>/app</code>.\n\n"
        "📥 <b>pip install:</b>\n"
        "Устанавливайте пакеты прямо из панели управления, "
        "не выходя из Telegram.\n\n"
        "💰 <b>Начальный баланс:</b>\n"
        "Каждый новый пользователь получает <b>25 стартовых кредитов</b>."
    ),
}


def _faq_menu_kb():
    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(
        types.InlineKeyboardButton("🆓 Бесплатные серверы",  callback_data="faq_1"),
        types.InlineKeyboardButton("💳 Платные тарифы",      callback_data="faq_2"),
        types.InlineKeyboardButton("💡 Советы и лайфхаки",  callback_data="faq_3"),
        types.InlineKeyboardButton("◀️ Назад в профиль",    callback_data="faq_profile"),
    )
    return mk


def _show_faq_menu(cid: int, mid: int):
    text = "❓ <b>Частые вопросы</b>\n\n━━━━━━━━━━━━━━━━━━━━━\nВыберите интересующую тему:"
    try:
        bot.edit_message_text(text, cid, mid, reply_markup=_faq_menu_kb())
    except Exception:
        bot.send_message(cid, text, reply_markup=_faq_menu_kb())


def _show_faq_item(cid: int, mid: int, key: str, uid: int):
    if key == "profile":
        _show_profile(cid, uid, message_id=mid)
        return
    text = _FAQ.get(key, "❓ Вопрос не найден.")
    mk = types.InlineKeyboardMarkup()
    mk.add(types.InlineKeyboardButton("◀️ Назад", callback_data="faq_menu"))
    try:
        bot.edit_message_text(text, cid, mid, reply_markup=mk)
    except Exception:
        bot.send_message(cid, text, reply_markup=mk)


# ─── universal text/document handler ─────────────────────────────────────────

@bot.message_handler(
    func=lambda m: True,
    content_types=["text", "document"],
)
def on_message(msg: types.Message):
    if not _is_private(msg):
        return
    uid, cid = msg.from_user.id, msg.chat.id

    if _maintenance_guard(uid, cid):
        return

    st = _state(uid)
    s  = st["state"]
    d  = st["data"]

    # ── ZIP upload ────────────────────────────────────────────────────────────
    if s == S_WAIT_ZIP:
        if msg.content_type == "document":
            _handle_zip_upload(msg, d.get("server_id"))
        else:
            bot.send_message(cid, "📦 Пожалуйста, отправьте ZIP-файл или /cancel для отмены.")
        return

    if msg.content_type != "text":
        return

    text = msg.text.strip()

    # ── Soft keyboard alias ───────────────────────────────────────────────────
    ALIASES = {
        "🖥 Серверы":        "servers",
        "➕ Создать сервер": "create",
        "👤 Профиль":        "profile",
        "ℹ️ Версия":         "version",
    }
    if text in ALIASES:
        msg.text = "/" + ALIASES[text]
        if ALIASES[text] == "servers":   cmd_servers(msg)
        elif ALIASES[text] == "create":  cmd_create(msg)
        elif ALIASES[text] == "profile": cmd_profile(msg)
        elif ALIASES[text] == "version": cmd_version(msg)
        return

    # ── State machine ─────────────────────────────────────────────────────────
    if s == S_WAIT_NAME:
        _handle_wait_name(msg, text)
    elif s == S_WAIT_PASSWORD:
        _handle_wait_password(msg, text, d)
    elif s == S_WAIT_SERVER_NAME:
        _handle_wait_server_name(msg, text)
    elif s == S_WAIT_PIP:
        _handle_pip(msg, text, d)

    # ── Admin states ──────────────────────────────────────────────────────────
    elif s == S_ADMIN_GIVE_CREDITS_WHO:
        if uid != ADMIN_ID:
            return
        try:
            tid = int(text)
            _set_state(uid, S_ADMIN_GIVE_CREDITS_AMT, {"tid": tid})
            tu = db.get_user(tid)
            name = tu["name"] if tu else f"<{tid}>"
            bot.send_message(cid, f"💰 Сколько кредитов выдать пользователю <b>{name}</b>?")
        except ValueError:
            bot.send_message(cid, "❌ Введите числовой ID пользователя.")

    elif s == S_ADMIN_GIVE_CREDITS_AMT:
        if uid != ADMIN_ID:
            return
        try:
            amount = int(text)
            tid    = d["tid"]
            tu     = db.get_user(tid)
            if not tu:
                bot.send_message(cid, "❌ Пользователь не найден.")
            else:
                db.update_credits(tid, amount)
                updated = db.get_user(tid)
                bot.send_message(
                    cid,
                    f"✅ <b>Кредиты выданы</b>\n\n"
                    f"👤 {tu['name']} (<code>{tid}</code>)\n"
                    f"➕ Начислено: <b>{amount} кр.</b>\n"
                    f"💰 Баланс:   <b>{updated['credits']} кр.</b>",
                )
                try:
                    bot.send_message(
                        tid,
                        f"🎁 <b>Вам начислены кредиты!</b>\n\n"
                        f"➕ <b>+{amount}</b> кредитов\n"
                        f"💰 Текущий баланс: <b>{updated['credits']} кр.</b>",
                    )
                except Exception:
                    pass
            _clear_state(uid)
        except ValueError:
            bot.send_message(cid, "❌ Введите число.")

    elif s == S_ADMIN_GIVE_SERVER_WHO:
        if uid != ADMIN_ID:
            return
        try:
            tid = int(text)
            tu  = db.get_user(tid)
            if not tu:
                bot.send_message(cid, "❌ Пользователь не найден.")
            else:
                sname = f"bonus_{tid}"
                sid   = db.create_server(tid, sname)
                bot.send_message(
                    cid,
                    f"✅ <b>Сервер выдан</b>\n\n"
                    f"👤 {tu['name']} (<code>{tid}</code>)\n"
                    f"🖥 Имя: <b>{sname}</b>  ID: {sid}",
                )
                try:
                    bot.send_message(
                        tid,
                        f"🎁 <b>Вам выдан бесплатный сервер!</b>\n\n"
                        f"🖥 Имя: <b>{sname}</b>\n"
                        f"Управляйте через /servers.",
                    )
                except Exception:
                    pass
            _clear_state(uid)
        except ValueError:
            bot.send_message(cid, "❌ Введите числовой ID.")

    elif s == S_ADMIN_BAN_WHO:
        if uid != ADMIN_ID:
            return
        try:
            tid = int(text)
            tu  = db.get_user(tid)
            if not tu:
                bot.send_message(cid, "❌ Пользователь не найден.")
            else:
                db.ban_user(tid)
                _auth.discard(tid)
                bot.send_message(
                    cid, f"✅ Пользователь <b>{tu['name']}</b> (<code>{tid}</code>) заблокирован."
                )
                try:
                    bot.send_message(tid, "🚫 <b>Ваш аккаунт заблокирован.</b>\nОбратитесь к администратору.")
                except Exception:
                    pass
            _clear_state(uid)
        except ValueError:
            bot.send_message(cid, "❌ Введите числовой ID.")

    elif s == S_ADMIN_UNBAN_WHO:
        if uid != ADMIN_ID:
            return
        try:
            tid = int(text)
            tu  = db.get_user(tid)
            if not tu:
                bot.send_message(cid, "❌ Пользователь не найден.")
            else:
                db.unban_user(tid)
                bot.send_message(
                    cid, f"✅ Пользователь <b>{tu['name']}</b> (<code>{tid}</code>) разблокирован."
                )
                try:
                    bot.send_message(tid, "✅ <b>Ваш аккаунт разблокирован!</b>\nВведите /start для входа.")
                except Exception:
                    pass
            _clear_state(uid)
        except ValueError:
            bot.send_message(cid, "❌ Введите числовой ID.")

    else:
        # Not in a state — prompt
        if _is_authed(uid):
            bot.send_message(
                cid,
                "❓ Непонятная команда. Используйте кнопки меню или /help.",
                reply_markup=_kb_main_menu(),
            )
        else:
            bot.send_message(cid, "👋 Введите /start для начала работы.")


# ─── state handlers ───────────────────────────────────────────────────────────

def _handle_wait_name(msg: types.Message, text: str):
    uid, cid = msg.from_user.id, msg.chat.id
    if len(text) < 2 or len(text) > 32:
        bot.send_message(cid, "❌ Имя должно быть от 2 до 32 символов. Попробуйте снова:")
        return
    _typing(cid, 0.3)
    bot.send_message(
        cid,
        f"👤 Имя: <b>{text}</b>\n\n"
        "🔐 Придумайте <b>пароль</b> (минимум 4 символа):",
    )
    _set_state(uid, S_WAIT_PASSWORD, {"name": text, "existing": False})


def _handle_wait_password(msg: types.Message, text: str, data: dict):
    uid, cid = msg.from_user.id, msg.chat.id

    if data.get("existing"):
        # Login
        user = db.get_user(uid)
        if user and db.check_password(uid, text):
            if user["is_banned"]:
                bot.send_message(cid, "🚫 <b>Ваш аккаунт заблокирован.</b>")
                _clear_state(uid)
                return
            _auth.add(uid)
            _clear_state(uid)
            _typing(cid, 0.4)
            bot.send_message(
                cid,
                f"✅ <b>Добро пожаловать, {user['name']}!</b>\n\n"
                f"💰 Баланс: <b>{user['credits']} кредитов</b>",
            )
            _send_commands(cid, uid)
        else:
            bot.send_message(cid, "❌ Неверный пароль. Попробуйте снова или /cancel:")
    else:
        # Register
        if len(text) < 4:
            bot.send_message(cid, "❌ Пароль должен содержать минимум 4 символа:")
            return
        name = data.get("name", "User")
        ok   = db.register_user(uid, name, text)
        if ok:
            _auth.add(uid)
            _clear_state(uid)
            _typing(cid, 0.5)
            bot.send_message(
                cid,
                f"🎉 <b>Аккаунт создан!</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Имя:    <b>{name}</b>\n"
                f"🆔 ID:     <code>{uid}</code>\n"
                f"💰 Баланс: <b>25 кредитов</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🚀 Вы готовы к работе!",
            )
            _send_commands(cid, uid)
        else:
            bot.send_message(cid, "❌ Ошибка регистрации. Введите /start снова.")
            _clear_state(uid)


def _handle_wait_server_name(msg: types.Message, text: str):
    uid, cid = msg.from_user.id, msg.chat.id
    if len(text) < 2 or len(text) > 32:
        bot.send_message(cid, "❌ Название должно быть от 2 до 32 символов. Попробуйте снова:")
        return
    _typing(cid, 0.5)
    server_id = db.create_server(uid, text)
    _clear_state(uid)

    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(types.InlineKeyboardButton("⚙️ Управление", callback_data=f"s_manage_{server_id}"))
    mk.add(types.InlineKeyboardButton("📋 Все серверы", callback_data="s_list"))

    bot.send_message(
        cid,
        f"✅ <b>Сервер создан!</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🖥 Имя:     <b>{text}</b>\n"
        f"📊 Статус:  🔴 Остановлен\n"
        f"💾 Ресурсы: 50 МБ RAM · 0.25 vCPU\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Нажмите <b>▶️ Запустить</b> в панели управления!",
        reply_markup=mk,
    )


def _handle_zip_upload(msg: types.Message, server_id: int):
    uid, cid = msg.from_user.id, msg.chat.id
    _clear_state(uid)

    if not msg.document or not (msg.document.file_name or "").lower().endswith(".zip"):
        bot.send_message(cid, "❌ Файл должен быть в формате ZIP.")
        return

    srv = db.get_server(server_id)
    if not srv or srv["user_id"] != uid:
        bot.send_message(cid, "❌ Сервер не найден.")
        return
    if not srv.get("container_id"):
        bot.send_message(cid, "❌ Сначала запустите сервер, чтобы создался контейнер.")
        return

    m = bot.send_message(cid, "⏳ <b>Загружаю файл…</b>")

    def _run():
        try:
            fi       = bot.get_file(msg.document.file_id)
            raw      = bot.download_file(fi.file_path)
            ok, res  = docker_mgr.upload_zip(srv["container_id"], raw)
            status   = "✅" if ok else "❌"
            bot.edit_message_text(f"{status} {res}", cid, m.message_id)
        except Exception as exc:
            bot.edit_message_text(f"❌ Ошибка: {exc}", cid, m.message_id)

    threading.Thread(target=_run, daemon=True).start()


def _handle_pip(msg: types.Message, text: str, data: dict):
    uid, cid = msg.from_user.id, msg.chat.id
    server_id = data.get("server_id")
    _clear_state(uid)

    srv = db.get_server(server_id)
    if not srv or srv["user_id"] != uid:
        bot.send_message(cid, "❌ Сервер не найден.")
        return
    if not srv.get("container_id"):
        bot.send_message(cid, "❌ Сначала запустите сервер.")
        return
    if srv["status"] != "running":
        bot.send_message(cid, "❌ Сервер должен быть <b>запущен</b> для установки пакетов.")
        return

    m = bot.send_message(cid, f"⏳ <b>pip install {text}…</b>")

    def _run():
        ok, res = docker_mgr.pip_install(srv["container_id"], text)
        try:
            bot.edit_message_text(res[:3800], cid, m.message_id)
        except Exception:
            bot.send_message(cid, res[:3800])

    threading.Thread(target=_run, daemon=True).start()


# ─── credit scheduler ─────────────────────────────────────────────────────────

def _deduct_credits():
    """Каждый час: -1 кредит за каждый работающий сервер."""
    try:
        for uid in db.get_all_user_ids():
            running = db.get_running_servers_count(uid)
            if running == 0:
                continue
            user = db.get_user(uid)
            if user["credits"] > 0:
                db.update_credits(uid, -running)
                new_bal = max(0, user["credits"] - running)
                if new_bal <= 5:
                    try:
                        bot.send_message(
                            uid,
                            f"⚠️ <b>Внимание! Низкий баланс</b>\n\n"
                            f"💰 Осталось кредитов: <b>{new_bal}</b>\n"
                            f"При нуле все серверы будут остановлены автоматически.\n"
                            f"Обратитесь к администратору для пополнения.",
                        )
                    except Exception:
                        pass
            else:
                # Нет кредитов — остановить все серверы
                for srv in db.get_servers(uid):
                    if srv["status"] == "running" and srv.get("container_id"):
                        docker_mgr.stop(srv["container_id"])
                        db.update_server(srv["id"], status="stopped")
                try:
                    bot.send_message(
                        uid,
                        "❌ <b>Кредиты закончились!</b>\n\n"
                        "Все ваши серверы автоматически остановлены.\n"
                        "Обратитесь к администратору для пополнения баланса.",
                    )
                except Exception:
                    pass
    except Exception:
        logger.exception("Ошибка в _deduct_credits")


# ─── entry point ──────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана!")

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(_deduct_credits, "interval", hours=1, id="credits")
    scheduler.start()
    logger.info("Планировщик кредитов запущен.")

    logger.info("HostBot %s запускается…", BOT_VERSION)
    bot.infinity_polling(timeout=60, long_polling_timeout=55)


if __name__ == "__main__":
    main()
