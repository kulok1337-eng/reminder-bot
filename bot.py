import asyncio, os, sqlite3, time, logging
from datetime import datetime
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ["ADMIN_IDS"].replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
SNOOZE_MIN = int(os.environ.get("SNOOZE_MIN", "30"))
DB_PATH = os.environ.get("DB_PATH", "reminders.db")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS employees(id INTEGER PRIMARY KEY, name TEXT, approved INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reminders(
 id INTEGER PRIMARY KEY AUTOINCREMENT, emp_id INTEGER, text TEXT, next_ts INTEGER,
 interval_min INTEGER, total INTEGER, sent INTEGER DEFAULT 0, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS log(ts INTEGER, rem_id INTEGER, ok INTEGER, info TEXT);
""")
db.commit()

bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def fmt(ts): return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")
def is_admin(uid): return uid in ADMINS
def kb(rows): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows])

MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Новое напоминание"), KeyboardButton(text="Активные")],
    [KeyboardButton(text="Сотрудники"), KeyboardButton(text="Добавить сотрудника")],
    [KeyboardButton(text="Отмена")]], resize_keyboard=True)
MIN_INTERVAL = 5

class New(StatesGroup):
    text = State(); when = State(); interval = State(); count = State()

IV_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Один раз", callback_data="iv:0")],
    [InlineKeyboardButton(text="Каждые 30 мин", callback_data="iv:30"), InlineKeyboardButton(text="Каждый час", callback_data="iv:60")],
    [InlineKeyboardButton(text="Каждые 2 часа", callback_data="iv:120"), InlineKeyboardButton(text="Каждый день", callback_data="iv:1440")],
    [InlineKeyboardButton(text="Свой интервал", callback_data="iv:x")]])
CNT_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=str(n), callback_data=f"ct:{n}") for n in (2, 3, 5, 10)]])

class AddEmp(StatesGroup):
    data = State()

# ---------- регистрация ----------
@dp.message(CommandStart())
async def start(m: Message):
    uid = m.from_user.id
    if is_admin(uid):
        await m.answer("Вы администратор. Пользуйтесь кнопками внизу.", reply_markup=MENU)
        return
    row = db.execute("SELECT approved FROM employees WHERE id=?", (uid,)).fetchone()
    if row and row["approved"]:
        await m.answer("Вы подключены. Напоминания будут приходить сюда."); return
    name = m.from_user.full_name
    db.execute("INSERT OR IGNORE INTO employees(id,name,approved) VALUES(?,?,0)", (uid, name)); db.commit()
    await m.answer("Заявка отправлена администратору.")
    for a in ADMINS:
        try:
            await bot.send_message(a, f"Новый пользователь: {name} (id {uid})", reply_markup=kb([[("Одобрить", f"ap:{uid}"), ("Отклонить", f"rj:{uid}")]]))
        except Exception: pass

@dp.callback_query(F.data.startswith(("ap:", "rj:")))
async def approve(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа", show_alert=True)
    act, uid = c.data.split(":"); uid = int(uid)
    if act == "ap":
        db.execute("UPDATE employees SET approved=1 WHERE id=?", (uid,)); db.commit()
        await c.message.edit_text(f"Сотрудник {uid} одобрен.")
        try: await bot.send_message(uid, "Доступ одобрен. Напоминания будут приходить сюда.")
        except Exception: pass
    else:
        db.execute("DELETE FROM employees WHERE id=?", (uid,)); db.commit()
        await c.message.edit_text(f"Сотрудник {uid} отклонён.")

@dp.message(F.text == "Добавить сотрудника")
async def addemp_btn(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear(); await state.set_state(AddEmp.data)
    await m.answer("Пришлите Telegram ID и имя через пробел, например: 1117238943 Ваня")

@dp.message(AddEmp.data)
async def addemp_data(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    p = m.text.split(maxsplit=1)
    if len(p) < 2 or not p[0].isdigit(): return await m.answer("Формат: ID и имя, например: 1117238943 Ваня")
    db.execute("INSERT OR REPLACE INTO employees(id,name,approved) VALUES(?,?,1)", (int(p[0]), p[1])); db.commit()
    await state.clear()
    await m.answer("Сотрудник добавлен. Он должен один раз нажать Start у бота, иначе Telegram не даст писать ему.", reply_markup=MENU)

@dp.message(F.text == "Отмена")
async def cancel_btn(m: Message, state: FSMContext):
    await state.clear(); await m.answer("Отменено.", reply_markup=MENU if is_admin(m.from_user.id) else None)

@dp.message(Command("addemp"))
async def addemp(m: Message):
    if not is_admin(m.from_user.id): return
    p = m.text.split(maxsplit=2)
    if len(p) < 3 or not p[1].isdigit(): return await m.answer("Формат: /addemp 123456789 Иван Петров")
    db.execute("INSERT OR REPLACE INTO employees(id,name,approved) VALUES(?,?,1)", (int(p[1]), p[2])); db.commit()
    await m.answer("Сотрудник добавлен. Он должен один раз нажать /start у бота, иначе Telegram не даст писать ему.")

@dp.message(Command("employees"))
@dp.message(F.text == "Сотрудники")
async def emps(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear()
    rows = db.execute("SELECT * FROM employees WHERE approved=1").fetchall()
    await m.answer("\n".join(f"{r['name']} — {r['id']}" for r in rows) or "Сотрудников нет. Нажмите «Добавить сотрудника».")

# ---------- создание ----------
@dp.message(Command("new"))
@dp.message(F.text == "Новое напоминание")
async def new(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    rows = db.execute("SELECT * FROM employees WHERE approved=1").fetchall()
    if not rows: return await m.answer("Сначала добавьте сотрудника кнопкой «Добавить сотрудника».")
    await state.clear()
    await m.answer("Кому напомнить?", reply_markup=kb([[(r["name"], f"to:{r['id']}")] for r in rows]))

@dp.callback_query(F.data.startswith("to:"))
async def pick(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа", show_alert=True)
    await state.update_data(emp=int(c.data[3:])); await state.set_state(New.text)
    await c.message.answer("Введите точный текст напоминания."); await c.answer()

@dp.message(New.text)
async def s_text(m: Message, state: FSMContext):
    await state.update_data(text=m.text); await state.set_state(New.when)
    await m.answer(f"Когда отправить первое сообщение? Выберите кнопку или введите ДД.ММ.ГГГГ ЧЧ:ММ (часовой пояс {TZ.key}).",
                   reply_markup=kb([[("Через 5 минут", "wh:5"), ("Через 1 час", "wh:60")], [("Завтра в 10:00", "wh:tm10")]]))

@dp.callback_query(F.data.startswith("wh:"), New.when)
async def s_when_btn(c: CallbackQuery, state: FSMContext):
    v = c.data[3:]
    if v == "tm10":
        t = datetime.now(TZ).replace(hour=10, minute=0, second=0, microsecond=0)
        ts = int(t.timestamp()) + 86400
    else:
        ts = int(time.time()) + int(v) * 60
    await state.update_data(ts=ts); await state.set_state(New.interval)
    await c.message.answer(f"Первая отправка: {fmt(ts)}.\nКак часто повторять?", reply_markup=IV_KB); await c.answer()

@dp.message(New.when)
async def s_when(m: Message, state: FSMContext):
    try: ts = int(datetime.strptime(m.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=TZ).timestamp())
    except ValueError: return await m.answer("Не понял формат. Пример: 28.10.2026 10:00")
    if ts < time.time(): return await m.answer("Это время уже прошло. Введите будущее.")
    await state.update_data(ts=ts); await state.set_state(New.interval)
    await m.answer("Как часто повторять?", reply_markup=IV_KB)

@dp.callback_query(F.data.startswith("iv:"), New.interval)
async def s_iv_btn(c: CallbackQuery, state: FSMContext):
    v = c.data[3:]
    if v == "x":
        await c.message.answer(f"Введите интервал в минутах (не меньше {MIN_INTERVAL})."); return await c.answer()
    iv = int(v); await state.update_data(iv=iv); await c.answer()
    if iv == 0:
        await state.update_data(total=1); return await card(c.message, state)
    await state.set_state(New.count); await c.message.answer("Сколько всего отправок, включая первую?", reply_markup=CNT_KB)

@dp.message(New.interval)
async def s_int(m: Message, state: FSMContext):
    if not m.text.strip().isdigit(): return await m.answer("Введите число минут.")
    iv = int(m.text)
    if iv != 0 and iv < MIN_INTERVAL: return await m.answer(f"Минимальный интервал — {MIN_INTERVAL} минут.")
    await state.update_data(iv=iv)
    if iv == 0:
        await state.update_data(total=1); return await card(m, state)
    await state.set_state(New.count); await m.answer("Сколько всего отправок, включая первую?", reply_markup=CNT_KB)

@dp.callback_query(F.data.startswith("ct:"), New.count)
async def s_cnt_btn(c: CallbackQuery, state: FSMContext):
    await state.update_data(total=int(c.data[3:])); await c.answer(); await card(c.message, state)

@dp.message(New.count)
async def s_cnt(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not 1 <= int(m.text) <= 50: return await m.answer("Число от 1 до 50.")
    await state.update_data(total=int(m.text)); await card(m, state)

async def card(m: Message, state: FSMContext):
    d = await state.get_data()
    emp = db.execute("SELECT name FROM employees WHERE id=?", (d["emp"],)).fetchone()["name"]
    times = ", ".join(fmt(d["ts"] + i * d["iv"] * 60) for i in range(d["total"]))
    await state.set_state(None)
    await m.answer(f"Новое напоминание\n\nСотрудник: {emp}\nТекст: {d['text']}\nОтправки ({d['total']}): {times}\nПосле «Готово» — остановить.\nОткладывание: {SNOOZE_MIN} мин.",
                   reply_markup=kb([[("Подтвердить", "ok"), ("Отменить", "no")]]))

@dp.callback_query(F.data.in_({"ok", "no"}))
async def confirm(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа", show_alert=True)
    d = await state.get_data()
    if c.data == "no" or "emp" not in d:
        await state.clear(); return await c.message.edit_text("Отменено.")
    cur = db.execute("INSERT INTO reminders(emp_id,text,next_ts,interval_min,total) VALUES(?,?,?,?,?)",
                     (d["emp"], d["text"], d["ts"], d["iv"], d["total"])); db.commit()
    await state.clear(); await c.message.edit_text(f"Напоминание №{cur.lastrowid} создано.")

@dp.message(Command("list"))
@dp.message(F.text == "Активные")
async def lst(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear()
    rows = db.execute("SELECT r.*, e.name FROM reminders r JOIN employees e ON e.id=r.emp_id WHERE status='active'").fetchall()
    if not rows: return await m.answer("Активных нет.")
    for r in rows:
        await m.answer(f"№{r['id']} {r['name']}: {r['text']}\nОтправлено {r['sent']} из {r['total']}, следующая: {fmt(r['next_ts'])}",
                       reply_markup=kb([[("Отменить", f"cx:{r['id']}")]]))

@dp.callback_query(F.data.startswith("cx:"))
async def cancel(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа", show_alert=True)
    db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND status='active'", (int(c.data[3:]),)); db.commit()
    await c.message.edit_text("Напоминание отменено.")

# ---------- кнопки сотрудника ----------
@dp.callback_query(F.data.startswith(("done:", "snz:")))
async def emp_btn(c: CallbackQuery):
    act, rid = c.data.split(":"); r = db.execute("SELECT * FROM reminders WHERE id=?", (int(rid),)).fetchone()
    if not r or r["emp_id"] != c.from_user.id: return await c.answer("Это не ваша задача", show_alert=True)
    if r["status"] in ("done", "cancelled"): return await c.answer("Задача уже закрыта", show_alert=True)
    if act == "done":
        db.execute("UPDATE reminders SET status='done' WHERE id=?", (r["id"],)); db.commit()
        await c.message.edit_reply_markup(reply_markup=None); await c.answer("Отмечено")
        for a in ADMINS:
            try: await bot.send_message(a, f"{c.from_user.full_name} отметил задачу №{r['id']} «{r['text']}» как выполненную.")
            except Exception: pass
    else:
        if r["status"] != "active": return await c.answer("Больше отправок не запланировано", show_alert=True)
        db.execute("UPDATE reminders SET next_ts=? WHERE id=?", (int(time.time()) + SNOOZE_MIN * 60, r["id"])); db.commit()
        await c.message.edit_reply_markup(reply_markup=None); await c.answer(f"Отложено на {SNOOZE_MIN} мин")

# ---------- планировщик ----------
async def scheduler():
    while True:
        try:
            now = int(time.time())
            for r in db.execute("SELECT * FROM reminders WHERE status='active' AND next_ts<=?", (now,)).fetchall():
                n = r["sent"] + 1
                nxt = now + r["interval_min"] * 60  # просроченные не копятся: одна отправка
                # резервируем событие до отправки, чтобы не было дублей
                db.execute("UPDATE reminders SET sent=?, next_ts=?, status=? WHERE id=?",
                           (n, nxt, "exhausted" if n >= r["total"] else "active", r["id"])); db.commit()
                try:
                    await bot.send_message(r["emp_id"], f"{r['text']}\n\nНапоминание {n} из {r['total']}\nЗадача №{r['id']}",
                                           reply_markup=kb([[("Готово", f"done:{r['id']}"), ("Отложить", f"snz:{r['id']}")]]))
                    db.execute("INSERT INTO log VALUES(?,?,1,'sent')", (now, r["id"]))
                except Exception as e:
                    db.execute("UPDATE reminders SET status='failed' WHERE id=?", (r["id"],))
                    db.execute("INSERT INTO log VALUES(?,?,0,?)", (now, r["id"], str(e)[:200]))
                    for a in ADMINS:
                        try: await bot.send_message(a, f"Задача №{r['id']} остановлена: не удалось доставить сообщение ({e}). Возможно, сотрудник не нажал Start у бота.")
                        except Exception: pass
                    db.commit(); continue
                db.commit()
                if n >= r["total"]:
                    for a in ADMINS:
                        try: await bot.send_message(a, f"Задача №{r['id']}: лимит отправок исчерпан, выполнение не подтверждено.")
                        except Exception: pass
        except Exception:
            logging.exception("scheduler")
        await asyncio.sleep(20)

# «Готово» после исчерпания лимита тоже должно работать
async def main():
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
