"""
Telegram-бот напоминаний для руководителя.

Два режима задачи:
  • once   — обычное напоминание с фиксированной датой и повторами;
  • weekly — еженедельная серия: выбранный день недели, время первой отправки,
             интервал и количество отправок внутри дня. Серия повторяется
             каждую неделю, пока задачу не отменят.

Кнопка «Сделано» закрывает только ТЕКУЩУЮ серию. Следующая серия начнётся
в ближайший выбранный день недели по расписанию.
"""

import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ["ADMIN_IDS"].replace(" ", "").split(",") if x}
SNOOZE_MIN = int(os.environ.get("SNOOZE_MIN", "30"))
DB_PATH = os.environ.get("DB_PATH", "data/reminders.db")
MIN_INTERVAL = 5
TICK_SEC = int(os.environ.get("SCHEDULER_TICK", "20"))
# Если бот был выключен и серия опоздала сильнее этого порога, неделя
# пропускается, а следующая серия ставится на ближайший выбранный день.
WEEKLY_LATE_GRACE_H = int(os.environ.get("WEEKLY_LATE_GRACE_H", "6"))
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

# ---------- часовые пояса ----------
TZS = {
    "spb": ("Санкт-Петербург", "Europe/Moscow"),
    "nor": ("Норильск", "Asia/Krasnoyarsk"),
}
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
# Винительный падеж для фраз «в понедельник», «в среду», «в пятницу».
WEEKDAYS_IN = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]


def tz_of(key):
    return ZoneInfo(TZS.get(key or "spb", TZS["spb"])[1])


def tz_name(key):
    return TZS.get(key or "spb", TZS["spb"])[0]


def fmt(ts, key="spb"):
    return datetime.fromtimestamp(ts, tz_of(key)).strftime("%d.%m.%Y %H:%M")


def fmt_hm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def esc(s):
    return html.escape(str(s))


# ---------- база ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS employees(
    id INTEGER PRIMARY KEY,
    name TEXT,
    approved INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reminders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_id INTEGER,
    text TEXT,
    next_ts INTEGER,
    interval_min INTEGER,
    total INTEGER,
    sent INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    tz TEXT DEFAULT 'spb'
);
CREATE TABLE IF NOT EXISTS log(
    ts INTEGER,
    rem_id INTEGER,
    ok INTEGER,
    info TEXT
);
CREATE TABLE IF NOT EXISTS series_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rem_id INTEGER NOT NULL,
    series_no INTEGER NOT NULL DEFAULT 1,
    emp_id INTEGER,
    text TEXT,
    tz TEXT,
    mode TEXT,
    weekday INTEGER,
    day_minute INTEGER,
    planned_ts INTEGER,
    ended_ts INTEGER,
    sent INTEGER,
    total INTEGER,
    status TEXT,
    note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_series_history_unique
    ON series_history(rem_id, series_no);
""")


def ensure_column(table, column, ddl):
    """Добавляет колонку в старую базу (миграция без потери данных)."""
    cols = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


# Поля, появившиеся вместе с еженедельным режимом.
ensure_column("reminders", "tz", "tz TEXT DEFAULT 'spb'")
ensure_column("reminders", "mode", "mode TEXT DEFAULT 'once'")
ensure_column("reminders", "weekday", "weekday INTEGER")
ensure_column("reminders", "day_minute", "day_minute INTEGER")
ensure_column("reminders", "series_no", "series_no INTEGER DEFAULT 1")
ensure_column("reminders", "series_start", "series_start INTEGER")
ensure_column("reminders", "reopen_ts", "reopen_ts INTEGER")
ensure_column("reminders", "created_ts", "created_ts INTEGER")
ensure_column("series_history", "weekday", "weekday INTEGER")
ensure_column("series_history", "day_minute", "day_minute INTEGER")

db.execute("UPDATE reminders SET mode='once' WHERE mode IS NULL")
db.execute("UPDATE reminders SET series_no=1 WHERE series_no IS NULL")
db.execute("UPDATE reminders SET series_start=COALESCE(series_start, next_ts) WHERE series_start IS NULL")
db.execute("UPDATE reminders SET tz='spb' WHERE tz IS NULL")
db.execute("UPDATE reminders SET created_ts=COALESCE(created_ts, next_ts) WHERE created_ts IS NULL")

# Старые завершённые задачи попадают в общую историю серий.
db.execute("""
INSERT OR IGNORE INTO series_history
    (rem_id, series_no, emp_id, text, tz, mode, weekday, day_minute,
     planned_ts, ended_ts, sent, total, status, note)
SELECT id, 1, emp_id, text, tz, 'once', NULL, NULL,
       COALESCE(series_start, next_ts), COALESCE(next_ts, 0),
       sent, total, status, NULL
FROM reminders
WHERE mode = 'once' AND status IN ('done', 'cancelled', 'failed', 'exhausted')
""")
db.commit()

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


def is_admin(uid):
    return uid in ADMINS


def kb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


BTN_NEW, BTN_ACT, BTN_HIST, BTN_EMP, BTN_ADD, BTN_CANCEL, BTN_MY = (
    "➕ Новое напоминание", "📋 Активные", "🗂 История", "👥 Сотрудники",
    "🙋 Добавить сотрудника", "✖️ Отмена", "📌 Мои задачи",
)
MENU_TEXTS = [BTN_NEW, BTN_ACT, BTN_HIST, BTN_EMP, BTN_ADD, BTN_CANCEL, BTN_MY]
ADMIN_MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_ACT)],
    [KeyboardButton(text=BTN_HIST), KeyboardButton(text=BTN_EMP)],
    [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)
EMP_MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_MY)]], resize_keyboard=True)
NOT_MENU = (F.text, ~F.text.in_(MENU_TEXTS), ~F.text.startswith("/"))

IV_KB = kb([[("1 раз", "iv:0")], [("Каждые 30 мин", "iv:30"), ("Каждый час", "iv:60")],
            [("Каждые 2 часа", "iv:120"), ("Каждый день", "iv:1440")], [("Свой интервал", "iv:x")]])
IV_WEEK_KB = kb([[("1 раз", "iv:0")], [("Каждые 15 мин", "iv:15"), ("Каждые 30 мин", "iv:30")],
                 [("Каждый час", "iv:60"), ("Каждые 2 часа", "iv:120")],
                 [("Свой интервал", "iv:x")]])
CNT_KB = kb([[(str(n), f"ct:{n}") for n in (2, 3, 5, 10)]])
CNT_WEEK_KB = kb([[(str(n), f"ct:{n}") for n in (2, 3, 5, 10)]])
TZ_KB = kb([[("Санкт-Петербург", "tz:spb")], [("Норильск", "tz:nor")]])
MODE_KB = kb([[("1 раз / обычное напоминание", "md:once")], [("🔄 Каждую неделю", "md:weekly")]])
WEEKDAY_KB = kb([[(WEEKDAYS[0], "wd:0"), (WEEKDAYS[1], "wd:1")],
                 [(WEEKDAYS[2], "wd:2"), (WEEKDAYS[3], "wd:3")],
                 [(WEEKDAYS[4], "wd:4"), (WEEKDAYS[5], "wd:5")],
                 [(WEEKDAYS[6], "wd:6")]])
WEEK_TIME_KB = kb([[("09:00", "wt:0900"), ("12:00", "wt:1200"), ("15:00", "wt:1500")],
                   [("18:00", "wt:1800"), ("19:00", "wt:1900"), ("20:00", "wt:2000")]])


class New(StatesGroup):
    text = State()
    tz = State()
    mode = State()
    when = State()
    weekday = State()
    wtime = State()
    interval = State()
    count = State()


class AddEmp(StatesGroup):
    data = State()


class Report(StatesGroup):
    wait = State()


# ---------- расчёты расписания ----------
def weekly_first_ts(weekday, day_minute, tz_key, now_ts=None):
    """Ближайший выбранный день недели и время в будущем (в часовом поясе задачи)."""
    tz = tz_of(tz_key)
    now = datetime.fromtimestamp(time.time() if now_ts is None else now_ts, tz)
    target = now.replace(hour=day_minute // 60, minute=day_minute % 60, second=0, microsecond=0)
    target += timedelta(days=(weekday - now.weekday()) % 7)
    if target <= now:
        target += timedelta(days=7)
    return int(target.timestamp())


def next_week_ts(ts, tz_key):
    """Ровно через неделю по местному календарю (без ошибок на переходе времени)."""
    return int((datetime.fromtimestamp(ts, tz_of(tz_key)) + timedelta(days=7)).timestamp())


def catchup_start(start_ts, tz_key, now_ts):
    """Если серия опоздала не сильно — стартуем сразу, иначе переносим на следующую неделю."""
    if now_ts - start_ts <= WEEKLY_LATE_GRACE_H * 3600:
        return start_ts
    nxt = start_ts
    guard = 0
    while nxt <= now_ts and guard < 1000:
        nxt = next_week_ts(nxt, tz_key)
        guard += 1
    return nxt


def weekly_capacity_error(d):
    """Проверяет, что все отправки серии помещаются в выбранный день до 23:59."""
    if d.get("mode") != "weekly" or d.get("iv", 0) <= 0:
        return None
    last_minute = d["day_minute"] + (d["total"] - 1) * d["iv"]
    if last_minute < 24 * 60:
        return None
    max_total = (23 * 60 + 59 - d["day_minute"]) // d["iv"] + 1
    return (f"⚠️ Последняя отправка вышла бы за пределы дня (после 23:59).\n"
            f"При времени {fmt_hm(d['day_minute'])} и интервале {d['iv']} мин "
            f"максимум отправок — <b>{max_total}</b>.\n"
            f"Введите количество ещё раз или измените интервал.")


def parse_hm(text):
    s = text.strip().replace(".", ":").replace(" ", "")
    match = re.fullmatch(r"(\d{1,2}):?(\d{2})", s)
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return hours * 60 + minutes


def schedule_line(r):
    tz_key = r["tz"] or "spb"
    if r["mode"] == "weekly" and r["weekday"] is not None:
        return f"🔄 еженедельно · {WEEKDAYS_SHORT[r['weekday']]} {fmt_hm(r['day_minute'])}"
    return f"📅 {fmt(r['next_ts'], tz_key)}"


# ---------- запись истории серий ----------
def record_series(r, status, ended_ts=None, note=None, sent=None):
    """Сохраняет/обновляет итог серии. Одна серия = одна строка истории."""
    now = int(time.time()) if ended_ts is None else int(ended_ts)
    sent_count = r["sent"] if sent is None else sent
    db.execute("""
        INSERT INTO series_history
            (rem_id, series_no, emp_id, text, tz, mode, weekday, day_minute,
             planned_ts, ended_ts, sent, total, status, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rem_id, series_no) DO UPDATE SET
            status=excluded.status,
            ended_ts=excluded.ended_ts,
            sent=excluded.sent,
            total=excluded.total,
            note=excluded.note
    """, (r["id"], r["series_no"], r["emp_id"], r["text"], r["tz"] or "spb",
          r["mode"] or "once", r["weekday"], r["day_minute"],
          r["series_start"] or r["next_ts"], now,
          sent_count, r["total"], status, (note or "")[:300]))


def mark_series_done(r, now):
    """«Сделано» закрывает только текущую серию. Возвращает старт следующей серии."""
    reopen = None
    if r["mode"] == "weekly":
        reopen = next_week_ts(r["series_start"] or r["next_ts"], r["tz"] or "spb")
    db.execute("UPDATE reminders SET status='done', reopen_ts=? WHERE id=?", (reopen, r["id"]))
    record_series(r, "done", ended_ts=now)
    db.commit()
    return reopen


def mark_series_failed(r, now, error):
    db.execute("UPDATE reminders SET status='failed', reopen_ts=NULL WHERE id=?", (r["id"],))
    record_series(r, "failed", ended_ts=now, note=str(error))
    db.execute("INSERT INTO log VALUES(?,?,0,?)", (now, r["id"], str(error)[:200]))
    db.commit()


def snooze_reminder(r, now):
    nxt = now + SNOOZE_MIN * 60
    db.execute("UPDATE reminders SET next_ts=? WHERE id=? AND status='active'", (nxt, r["id"]))
    db.commit()
    return nxt


async def notify_admins(text):
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logging.exception("Не удалось отправить уведомление администратору %s", admin_id)


# ---------- старт и регистрация ----------
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    if is_admin(uid):
        return await m.answer(
            "👋 <b>Привет, администратор!</b>\n\n"
            "Здесь вы ставите задачи сотрудникам, а я напоминаю о них в нужное время, "
            "пока человек не подтвердит выполнение.\n\n"
            "Можно создать разовое напоминание или <b>еженедельную серию</b> "
            "(например, каждый понедельник в 19:00 с повторами каждые 30 минут).\n\n"
            "Пользуйтесь кнопками внизу.", reply_markup=ADMIN_MENU)
    row = db.execute("SELECT approved, name FROM employees WHERE id=?", (uid,)).fetchone()
    if row and row["approved"]:
        return await m.answer(
            f"👋 Привет, <b>{esc(row['name'])}</b>!\n\n"
            "Сюда будут приходить задачи от руководителя. Нажмите «Сделано», когда выполните.",
            reply_markup=EMP_MENU)
    name = m.from_user.full_name
    db.execute("INSERT OR IGNORE INTO employees(id,name,approved) VALUES(?,?,0)", (uid, name))
    db.commit()
    await m.answer("⏳ Заявка отправлена руководителю. Как только её одобрят, я напишу.")
    for a in ADMINS:
        try:
            await bot.send_message(
                a, f"🆕 Новый пользователь: <b>{esc(name)}</b> (id <code>{uid}</code>)",
                reply_markup=kb([[("✅ Одобрить", f"ap:{uid}"), ("🚫 Отклонить", f"rj:{uid}")]]))
        except Exception:
            pass


@dp.callback_query(F.data.startswith(("ap:", "rj:")))
async def approve(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    act, uid = c.data.split(":")
    uid = int(uid)
    if act == "ap":
        db.execute("UPDATE employees SET approved=1 WHERE id=?", (uid,))
        db.commit()
        await c.message.edit_text(f"✅ Сотрудник <code>{uid}</code> одобрен.")
        try:
            await bot.send_message(uid, "✅ Доступ одобрен! Теперь я буду присылать вам задачи.",
                                   reply_markup=EMP_MENU)
        except Exception:
            pass
    else:
        db.execute("DELETE FROM employees WHERE id=?", (uid,))
        db.commit()
        await c.message.edit_text(f"🚫 Заявка <code>{uid}</code> отклонена.")


@dp.message(F.text == BTN_CANCEL)
async def cancel_btn(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Окей, отменил.", reply_markup=ADMIN_MENU if is_admin(m.from_user.id) else EMP_MENU)


# ---------- сотрудники ----------
@dp.message(F.text == BTN_ADD)
async def addemp_btn(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    await state.set_state(AddEmp.data)
    await m.answer("🙋 Пришлите Telegram ID и имя через пробел.\nНапример: <code>1117238943 Ваня</code>")


@dp.message(AddEmp.data, *NOT_MENU)
async def addemp_data(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    p = m.text.split(maxsplit=1)
    if len(p) < 2 or not p[0].isdigit():
        return await m.answer("Формат: ID и имя, например <code>1117238943 Ваня</code>")
    db.execute("INSERT OR REPLACE INTO employees(id,name,approved) VALUES(?,?,1)", (int(p[0]), p[1]))
    db.commit()
    await state.clear()
    await m.answer(f"✅ <b>{esc(p[1])}</b> добавлен.\n"
                   "Человеку нужно один раз нажать Start у бота, иначе Telegram не даст мне ему писать.",
                   reply_markup=ADMIN_MENU)


@dp.message(Command("addemp"))
async def addemp(m: Message):
    if not is_admin(m.from_user.id):
        return
    p = m.text.split(maxsplit=2)
    if len(p) < 3 or not p[1].isdigit():
        return await m.answer("Формат: /addemp 123456789 Иван Петров")
    db.execute("INSERT OR REPLACE INTO employees(id,name,approved) VALUES(?,?,1)", (int(p[1]), p[2]))
    db.commit()
    await m.answer("✅ Сотрудник добавлен.")


@dp.message(Command("employees"))
@dp.message(F.text == BTN_EMP)
async def emps(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    rows = db.execute("SELECT * FROM employees WHERE approved=1").fetchall()
    if rows:
        await m.answer("👥 <b>Сотрудники</b>\n\n" +
                       "\n".join(f"• {esc(r['name'])} — <code>{r['id']}</code>" for r in rows))
    else:
        await m.answer("Сотрудников пока нет. Нажмите «Добавить сотрудника».")


# ---------- создание напоминания ----------
@dp.message(Command("new"))
@dp.message(F.text == BTN_NEW)
async def new(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    rows = db.execute("SELECT * FROM employees WHERE approved=1").fetchall()
    if not rows:
        return await m.answer("Сначала добавьте сотрудника кнопкой «Добавить сотрудника».")
    await state.clear()
    await m.answer("👤 <b>Шаг 1.</b> Кому напомнить?",
                   reply_markup=kb([[(r["name"], f"to:{r['id']}")] for r in rows]))


@dp.callback_query(F.data.startswith("to:"))
async def pick(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    await state.update_data(emp=int(c.data[3:]))
    await state.set_state(New.text)
    await c.message.answer("✍️ <b>Шаг 2.</b> Напишите текст напоминания — он уйдёт сотруднику без изменений.")
    await c.answer()


@dp.message(New.text, *NOT_MENU)
async def s_text(m: Message, state: FSMContext):
    await state.update_data(text=m.text)
    await state.set_state(New.tz)
    await m.answer("🌍 <b>Шаг 3.</b> В каком часовом поясе указываете время?", reply_markup=TZ_KB)


@dp.callback_query(F.data.startswith("tz:"), New.tz)
async def s_tz(c: CallbackQuery, state: FSMContext):
    key = c.data[3:]
    await state.update_data(tz=key)
    await state.set_state(New.mode)
    await c.message.answer(
        f"🗓 <b>Шаг 4.</b> Какой режим напоминания? (время: {tz_name(key)})",
        reply_markup=MODE_KB)
    await c.answer()


@dp.callback_query(F.data.startswith("md:"), New.mode)
async def s_mode(c: CallbackQuery, state: FSMContext):
    mode = c.data[3:]
    await state.update_data(mode=mode)
    if mode == "weekly":
        await state.set_state(New.weekday)
        await c.message.answer(
            "📅 <b>Шаг 5.</b> В какой день недели отправлять напоминания?\n\n"
            "Выбранная схема будет повторяться каждую неделю.", reply_markup=WEEKDAY_KB)
    else:
        await state.set_state(New.when)
        await c.message.answer(
            "🕒 <b>Шаг 5.</b> Когда отправить первое сообщение?\n"
            "Выберите кнопку или введите <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>.",
            reply_markup=kb([[("Через 5 минут", "wh:5"), ("Через 1 час", "wh:60")],
                             [("Завтра в 10:00", "wh:tm10")]]))
    await c.answer()


@dp.callback_query(F.data.startswith("wd:"), New.weekday)
async def s_weekday(c: CallbackQuery, state: FSMContext):
    weekday = int(c.data[3:])
    await state.update_data(weekday=weekday)
    await state.set_state(New.wtime)
    await c.message.answer(
        f"🕒 <b>Шаг 6.</b> Во сколько начинать в {WEEKDAYS_IN[weekday]}?\n"
        "Введите время в формате <code>ЧЧ:ММ</code>, например <code>19:00</code>, "
        "или выберите кнопку.", reply_markup=WEEK_TIME_KB)
    await c.answer()


async def ask_interval(target: Message, state: FSMContext):
    d = await state.get_data()
    await state.set_state(New.interval)
    if d.get("mode") == "weekly":
        await target.answer(
            f"🔁 <b>Шаг 7.</b> Сколько раз напомнить в {WEEKDAYS_IN[d['weekday']]} "
            "и с каким интервалом?\nВыберите интервал между отправками.",
            reply_markup=IV_WEEK_KB)
    else:
        await target.answer("🔁 <b>Шаг 6.</b> Как часто повторять?", reply_markup=IV_KB)


async def set_week_time(target: Message, state: FSMContext, day_minute):
    await state.update_data(day_minute=day_minute)
    await ask_interval(target, state)


@dp.callback_query(F.data.startswith("wt:"), New.wtime)
async def s_wtime_btn(c: CallbackQuery, state: FSMContext):
    value = c.data[3:]
    day_minute = int(value[:2]) * 60 + int(value[2:])
    await c.answer()
    await set_week_time(c.message, state, day_minute)


@dp.message(New.wtime, *NOT_MENU)
async def s_wtime(m: Message, state: FSMContext):
    day_minute = parse_hm(m.text)
    if day_minute is None:
        return await m.answer("Не понял время. Пример: <code>19:00</code>")
    await set_week_time(m, state, day_minute)


async def after_when(target: Message, state: FSMContext, ts: int):
    d = await state.get_data()
    await state.update_data(ts=ts)
    await state.set_state(New.interval)
    await target.answer(
        f"✅ Первая отправка: <b>{fmt(ts, d['tz'])}</b> ({tz_name(d['tz'])}).\n\n"
        "🔁 <b>Шаг 6.</b> Как часто повторять?", reply_markup=IV_KB)


@dp.callback_query(F.data.startswith("wh:"), New.when)
async def s_when_btn(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    value = c.data[3:]
    if value == "tm10":
        t = datetime.now(tz_of(d["tz"])).replace(hour=10, minute=0, second=0, microsecond=0)
        ts = int(t.timestamp()) + 86400
    else:
        ts = int(time.time()) + int(value) * 60
    await c.answer()
    await after_when(c.message, state, ts)


@dp.message(New.when, *NOT_MENU)
async def s_when(m: Message, state: FSMContext):
    d = await state.get_data()
    try:
        ts = int(datetime.strptime(m.text.strip(), "%d.%m.%Y %H:%M")
                 .replace(tzinfo=tz_of(d["tz"])).timestamp())
    except ValueError:
        return await m.answer("Не понял формат. Пример: <code>28.10.2026 10:00</code>")
    if ts < time.time():
        return await m.answer("Это время уже прошло. Введите время в будущем.")
    await after_when(m, state, ts)


async def ask_count(target: Message, state: FSMContext):
    d = await state.get_data()
    await state.set_state(New.count)
    if d.get("mode") == "weekly":
        question = (f"🔢 <b>Шаг 8.</b> Сколько отправок в {WEEKDAYS_IN[d['weekday']]} "
                    "(включая первую)?")
        markup = CNT_WEEK_KB
    else:
        question = "🔢 <b>Шаг 7.</b> Сколько всего отправок, включая первую?"
        markup = CNT_KB
    await target.answer(question, reply_markup=markup)


@dp.callback_query(F.data.startswith("iv:"), New.interval)
async def s_iv_btn(c: CallbackQuery, state: FSMContext):
    value = c.data[3:]
    await c.answer()
    if value == "x":
        return await c.message.answer(f"Введите интервал в минутах (не меньше {MIN_INTERVAL}).")
    iv = int(value)
    await state.update_data(iv=iv)
    if iv == 0:
        await state.update_data(total=1)
        return await card(c.message, state)
    await ask_count(c.message, state)


@dp.message(New.interval, *NOT_MENU)
async def s_int(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("Введите число минут.")
    iv = int(m.text)
    if iv != 0 and iv < MIN_INTERVAL:
        return await m.answer(f"Минимальный интервал — {MIN_INTERVAL} минут.")
    await state.update_data(iv=iv)
    if iv == 0:
        await state.update_data(total=1)
        return await card(m, state)
    await ask_count(m, state)


async def accept_count(target: Message, state: FSMContext, total: int):
    d = await state.get_data()
    d["total"] = total
    error = weekly_capacity_error(d)
    if error:
        await state.set_state(New.count)
        return await target.answer(error, reply_markup=CNT_WEEK_KB)
    await state.update_data(total=total)
    await card(target, state)


@dp.callback_query(F.data.startswith("ct:"), New.count)
async def s_cnt_btn(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await accept_count(c.message, state, int(c.data[3:]))


@dp.message(New.count, *NOT_MENU)
async def s_cnt(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not 1 <= int(m.text) <= 50:
        return await m.answer("Число от 1 до 50.")
    await accept_count(m, state, int(m.text))


async def card(target: Message, state: FSMContext):
    d = await state.get_data()
    emp_row = db.execute("SELECT name FROM employees WHERE id=?", (d["emp"],)).fetchone()
    emp = emp_row["name"] if emp_row else "сотрудник"
    tz_key = d["tz"]

    if d.get("mode") == "weekly":
        ts = weekly_first_ts(d["weekday"], d["day_minute"], tz_key)
        await state.update_data(ts=ts)
        times = "\n".join(f"   • {fmt_hm(d['day_minute'] + i * d['iv'])}"
                          for i in range(d["total"]))
        text = (f"📝 <b>Проверьте напоминание</b>\n\n"
                f"👤 Кому: <b>{esc(emp)}</b>\n"
                f"💬 Текст: {esc(d['text'])}\n"
                f"🌍 Часовой пояс: {tz_name(tz_key)}\n"
                f"🔄 Режим: <b>еженедельно</b>\n"
                f"📅 День: <b>{WEEKDAYS[d['weekday']]}</b>, первая серия {fmt(ts, tz_key)}\n"
                f"⏰ Отправки в этот день ({d['total']}):\n{times}\n\n"
                f"📆 Далее — каждую неделю в этот же день и время.\n\n"
                f"✅ «Сделано» закроет только текущую серию. "
                f"В {WEEKDAYS_IN[d['weekday']]} всё начнётся заново.\n"
                f"⏰ «Отложить» сдвигает на {SNOOZE_MIN} мин.")
    else:
        times = "\n".join(f"   {i + 1}. {fmt(d['ts'] + i * d['iv'] * 60, tz_key)}"
                          for i in range(d["total"]))
        text = (f"📝 <b>Проверьте напоминание</b>\n\n"
                f"👤 Кому: <b>{esc(emp)}</b>\n"
                f"💬 Текст: {esc(d['text'])}\n"
                f"🌍 Часовой пояс: {tz_name(tz_key)}\n"
                f"📆 Отправки ({d['total']}):\n{times}\n\n"
                f"✅ Остановится, когда сотрудник нажмёт «Сделано».\n"
                f"⏰ «Отложить» сдвигает на {SNOOZE_MIN} мин.")

    await state.set_state(None)
    await target.answer(text, reply_markup=kb([[("✅ Подтвердить", "ok"), ("✖️ Отменить", "no")]]))


@dp.callback_query(F.data.in_({"ok", "no"}))
async def confirm(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    d = await state.get_data()
    if c.data == "no" or "emp" not in d:
        await state.clear()
        return await c.message.edit_text("Отменено.")

    mode = d.get("mode", "once")
    ts = d["ts"]
    if mode == "weekly":
        # Если пока заполняли карточку, время прошло — ставим на следующую неделю.
        if ts <= int(time.time()):
            ts = weekly_first_ts(d["weekday"], d["day_minute"], d["tz"])
    cur = db.execute("""
        INSERT INTO reminders
            (emp_id, text, next_ts, interval_min, total, sent, status, tz,
             mode, weekday, day_minute, series_no, series_start, reopen_ts, created_ts)
        VALUES (?, ?, ?, ?, ?, 0, 'active', ?, ?, ?, ?, 1, ?, NULL, ?)
    """, (d["emp"], d["text"], ts, d["iv"], d["total"], d["tz"], mode,
          d.get("weekday"), d.get("day_minute"), ts, int(time.time())))
    db.commit()
    await state.clear()

    if mode == "weekly":
        tail = (f"\n🔄 Далее каждую неделю: {WEEKDAYS[d['weekday']]}, "
                f"{fmt_hm(d['day_minute'])}.")
    else:
        tail = ""
    await c.message.edit_text(
        f"🎉 Готово! Напоминание №{cur.lastrowid} создано и поставлено в расписание."
        f"\n📅 Первая отправка: {fmt(ts, d['tz'])}{tail}")


# ---------- списки ----------
@dp.message(Command("list"))
@dp.message(F.text == BTN_ACT)
async def lst(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    rows = db.execute("""
        SELECT r.*, e.name
        FROM reminders r JOIN employees e ON e.id = r.emp_id
        WHERE r.status IN ('active', 'exhausted')
           OR (r.mode = 'weekly' AND r.status = 'done' AND r.reopen_ts IS NOT NULL)
        ORDER BY r.id
    """).fetchall()
    if not rows:
        return await m.answer("Активных задач нет. Все спокойно.")

    for r in rows:
        tz_key = r["tz"] or "spb"
        lines = [f"📌 <b>№{r['id']}</b> · {esc(r['name'])}",
                 esc(r["text"]),
                 schedule_line(r)]
        if r["mode"] == "weekly":
            lines.append(f"Серия {r['series_no']} · отправлено {r['sent']} из {r['total']}")
            if r["status"] == "done":
                lines.append(f"✅ Серия выполнена. Следующая серия: {fmt(r['reopen_ts'], tz_key)}")
            elif r["status"] == "exhausted":
                lines.append("⚠️ Отправки серии закончились, выполнение не подтверждено.")
                if r["reopen_ts"]:
                    lines.append(f"🔄 Следующая серия: {fmt(r['reopen_ts'], tz_key)}")
            else:
                lines.append(f"⏭ Следующая отправка: {fmt(r['next_ts'], tz_key)}")
        else:
            if r["status"] == "exhausted":
                lines.append("⚠️ Отправки закончились, выполнение не подтверждено.")
            else:
                lines.append(f"⏭ Следующая отправка: {fmt(r['next_ts'], tz_key)}")

        await m.answer("\n".join(lines),
                       reply_markup=kb([[("🗑 Отменить", f"cx:{r['id']}")]]))


@dp.message(F.text == BTN_HIST)
async def hist(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    rows = db.execute("""
        SELECT h.*, COALESCE(e.name, '—') AS name
        FROM series_history h
        LEFT JOIN employees e ON e.id = h.emp_id
        ORDER BY h.ended_ts DESC, h.id DESC
        LIMIT 15
    """).fetchall()
    if not rows:
        return await m.answer("История пока пуста.")

    icons = {"done": "✅ выполнено", "exhausted": "⚠️ отправки закончились",
             "cancelled": "🗑 отменено", "failed": "❌ не доставлено"}
    parts = []
    for r in rows:
        tz_key = r["tz"] or "spb"
        if r["mode"] == "weekly" and r["weekday"] is not None:
            sched = f"еженедельно · {WEEKDAYS_SHORT[r['weekday']]} {fmt_hm(r['day_minute'])}"
        else:
            sched = "однократно"
        parts.append(
            f"№{r['rem_id']} · {esc(r['name'])} · {sched}\n"
            f"«{esc(str(r['text'])[:60])}» — {icons.get(r['status'], r['status'])} · "
            f"{fmt(r['ended_ts'], tz_key)}")
    await m.answer("🗂 <b>Последние серии</b>\n\n" + "\n\n".join(parts))


@dp.callback_query(F.data.startswith("cx:"))
async def cancel(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    rid = int(c.data[3:])
    r = db.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
    if not r or r["status"] not in ("active", "exhausted", "done"):
        return await c.answer("Задача уже закрыта", show_alert=True)

    now = int(time.time())
    if r["status"] != "done":
        record_series(r, "cancelled", ended_ts=now, note="Отменено руководителем")
    db.execute("UPDATE reminders SET status='cancelled', reopen_ts=NULL WHERE id=?", (rid,))
    db.commit()
    await c.message.edit_text("🗑 Напоминание отменено.")


@dp.message(F.text == BTN_MY)
async def my(m: Message, state: FSMContext):
    await state.clear()
    rows = db.execute("""
        SELECT * FROM reminders
        WHERE emp_id = ? AND status IN ('active', 'exhausted')
        ORDER BY id
    """, (m.from_user.id,)).fetchall()
    if not rows:
        return await m.answer("🎉 Активных задач нет!")
    for r in rows:
        tz_key = r["tz"] or "spb"
        text = f"📌 <b>Задача №{r['id']}</b>\n{esc(r['text'])}\n{schedule_line(r)}"
        if r["mode"] == "weekly":
            text += f"\nСерия {r['series_no']} · отправлено {r['sent']} из {r['total']}"
        buttons = [[("✅ Сделано", f"done:{r['id']}:{r['series_no']}")]]
        if r["status"] == "active":
            buttons.append([(f"⏰ Отложить на {SNOOZE_MIN} мин", f"snz:{r['id']}:{r['series_no']}")])
        await m.answer(text, reply_markup=kb(buttons))


# ---------- кнопки сотрудника ----------
@dp.callback_query(F.data.startswith(("done:", "snz:")))
async def emp_btn(c: CallbackQuery):
    parts = c.data.split(":")
    act, rid = parts[0], int(parts[1])
    r = db.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
    if not r or r["emp_id"] != c.from_user.id:
        return await c.answer("Это не ваша задача", show_alert=True)

    # Кнопка из прошлой недели не должна закрывать новую серию.
    if len(parts) > 2 and int(parts[2]) != r["series_no"]:
        return await c.answer(
            "⏳ Эта серия уже завершена. Актуальную задачу смотрите в «Мои задачи».",
            show_alert=True)
    if r["status"] in ("done", "cancelled", "failed"):
        return await c.answer("Задача уже закрыта", show_alert=True)

    tz_key = r["tz"] or "spb"
    now = int(time.time())

    if act == "done":
        reopen = mark_series_done(r, now)
        await c.message.edit_text(f"✅ <b>Выполнено</b>\n\n{esc(r['text'])}")
        await c.message.answer(
            "Отлично, молодец! 🙌\nХотите приложить отчёт (фото или комментарий)?",
            reply_markup=kb([[("📎 Прислать отчёт", f"rp:{r['id']}:{r['series_no']}"),
                              ("Без отчёта", f"nr:{r['id']}:{r['series_no']}")]]))
        await c.answer("Принято!")

        series = f" (серия {r['series_no']})" if r["mode"] == "weekly" else ""
        tail = ""
        if r["mode"] == "weekly" and reopen:
            tail = (f"\n🔄 Следующая серия начнётся автоматически: "
                    f"{fmt(reopen, tz_key)}")
        await notify_admins(
            f"✅ <b>{esc(c.from_user.full_name)}</b> выполнил(а) задачу №{r['id']}{series}:\n"
            f"«{esc(r['text'])}»{tail}")
    else:
        if r["status"] != "active":
            return await c.answer("Больше отправок не запланировано", show_alert=True)
        nxt = snooze_reminder(r, now)
        await c.message.edit_text(
            f"⏰ <b>Отложено на {SNOOZE_MIN} мин</b>\n\n{esc(r['text'])}\n\n"
            f"⏭ Новое время: {fmt(nxt, tz_key)}")
        await c.answer("Напомню позже")


@dp.callback_query(F.data.startswith(("rp:", "nr:")))
async def report_choice(c: CallbackQuery, state: FSMContext):
    parts = c.data.split(":")
    act, rid = parts[0], int(parts[1])
    series_no = int(parts[2]) if len(parts) > 2 else None
    r = db.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()
    if not r or r["emp_id"] != c.from_user.id:
        return await c.answer("Это не ваша задача", show_alert=True)
    if act == "nr":
        await c.message.edit_text("Хорошо, без отчёта. Спасибо! 👍")
        return await c.answer()
    await state.set_state(Report.wait)
    await state.update_data(rid=r["id"], series_no=series_no or r["series_no"])
    await c.message.edit_text("📎 Пришлите фото, файл или текст одним сообщением — "
                              "я передам руководителю.")
    await c.answer()


@dp.message(Report.wait)
async def report_msg(m: Message, state: FSMContext):
    d = await state.get_data()
    r = db.execute("SELECT * FROM reminders WHERE id=?", (d.get("rid", 0),)).fetchone()
    await state.clear()
    for a in ADMINS:
        try:
            if r:
                series = (f" (серия {d.get('series_no', r['series_no'])})"
                          if r["mode"] == "weekly" else "")
                caption = (f"📎 Отчёт от <b>{esc(m.from_user.full_name)}</b> по задаче "
                           f"№{r['id']}{series} «{esc(r['text'])}»:")
            else:
                caption = f"📎 Отчёт от <b>{esc(m.from_user.full_name)}</b>:"
            await bot.send_message(a, caption)
            await m.copy_to(a)
        except Exception:
            logging.exception("Не удалось передать отчёт руководителю %s", a)
    await m.answer("✅ Отчёт отправлен руководителю. Спасибо!")


# ---------- планировщик ----------
def reopen_weekly(now):
    """Возвращает в работу серии, ожидающие следующей недели."""
    rows = db.execute("""
        SELECT * FROM reminders
        WHERE mode='weekly' AND status IN ('done', 'exhausted')
          AND reopen_ts IS NOT NULL AND reopen_ts <= ?
    """, (now,)).fetchall()
    for r in rows:
        tz_key = r["tz"] or "spb"
        start = catchup_start(r["reopen_ts"], tz_key, now)
        db.execute("""
            UPDATE reminders
            SET status='active', sent=0, series_no=series_no+1,
                series_start=?, next_ts=?, reopen_ts=NULL
            WHERE id=?
        """, (start, start, r["id"]))
    if rows:
        db.commit()
    return rows


async def rollover_stale(now):
    """Если активная серия не успела завершиться до следующей недели — начинаем новую."""
    rows = db.execute("""
        SELECT * FROM reminders
        WHERE mode='weekly' AND status='active' AND series_start IS NOT NULL
    """).fetchall()
    for r in rows:
        tz_key = r["tz"] or "spb"
        boundary = next_week_ts(r["series_start"], tz_key)
        if now < boundary:
            continue
        record_series(r, "exhausted", ended_ts=now,
                      note="Серия не завершилась до следующей недели")
        start = catchup_start(boundary, tz_key, now)
        db.execute("""
            UPDATE reminders
            SET sent=0, series_no=series_no+1, series_start=?, next_ts=?,
                reopen_ts=NULL, status='active'
            WHERE id=?
        """, (start, start, r["id"]))
        db.commit()
        await notify_admins(
            f"⚠️ Задача №{r['id']}: предыдущая серия не завершилась до следующей недели. "
            f"Начинаю новую серию: {fmt(start, tz_key)}.")


async def send_due(r, now):
    n = r["sent"] + 1
    total = r["total"]
    tz_key = r["tz"] or "spb"

    step = max(r["interval_min"], 0) * 60
    if step:
        # Следующий слот строго после now: держим ровные 19:00, 19:30, 20:00...
        nxt = r["next_ts"] + ((now - r["next_ts"]) // step + 1) * step
    else:
        nxt = now
    last = n >= total

    buttons = [[("✅ Сделано", f"done:{r['id']}:{r['series_no']}")]]
    if not last:
        buttons.append([(f"⏰ Отложить на {SNOOZE_MIN} мин",
                         f"snz:{r['id']}:{r['series_no']}")])

    try:
        await bot.send_message(
            r["emp_id"],
            f"🔔 <b>Напоминание</b>\n\n{esc(r['text'])}\n\n"
            f"📌 Задача №{r['id']} · сообщение {n} из {r['total']}"
            + (f"\n🔄 {WEEKDAYS[r['weekday']]} · серия {r['series_no']}"
               if r["mode"] == "weekly" and r["weekday"] is not None else ""),
            reply_markup=kb(buttons))
    except Exception as e:
        mark_series_failed(r, now, e)
        await notify_admins(
            f"❌ Задача №{r['id']} остановлена: не удалось доставить сообщение ({esc(e)}). "
            "Возможно, сотрудник не нажал Start у бота.")
        return

    db.execute("INSERT INTO log VALUES(?,?,1,'sent')", (now, r["id"]))

    if last and r["mode"] == "weekly":
        reopen = next_week_ts(r["series_start"] or r["next_ts"], tz_key)
        db.execute("""
            UPDATE reminders
            SET sent=?, status='exhausted', next_ts=?, reopen_ts=?
            WHERE id=?
        """, (n, nxt, reopen, r["id"]))
        db.commit()
        record_series(r, "exhausted", ended_ts=now, sent=n)
        await notify_admins(
            f"⚠️ Задача №{r['id']} (серия {r['series_no']}): отправки закончились, "
            f"выполнение не подтверждено.\n🔄 Следующая серия: {fmt(reopen, tz_key)}")
    elif last:
        db.execute("""
            UPDATE reminders SET sent=?, status='exhausted', next_ts=? WHERE id=?
        """, (n, nxt, r["id"]))
        db.commit()
        record_series(r, "exhausted", ended_ts=now, sent=n)
        await notify_admins(
            f"⚠️ Задача №{r['id']}: отправки закончились, выполнение не подтверждено.")
    else:
        db.execute("""
            UPDATE reminders SET sent=?, next_ts=?, status='active' WHERE id=?
        """, (n, nxt, r["id"]))
        db.commit()


async def scheduler_tick(now):
    reopen_weekly(now)
    await rollover_stale(now)
    rows = db.execute("""
        SELECT * FROM reminders
        WHERE status='active' AND next_ts <= ?
        ORDER BY next_ts
    """, (now,)).fetchall()
    for r in rows:
        await send_due(r, now)


async def scheduler():
    while True:
        try:
            await scheduler_tick(int(time.time()))
        except Exception:
            logging.exception("scheduler")
        await asyncio.sleep(TICK_SEC)


async def main():
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
