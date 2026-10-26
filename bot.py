import asyncio, os, sqlite3, time, logging, html
from datetime import datetime
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
                           ReplyKeyboardMarkup, KeyboardButton)

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ["ADMIN_IDS"].replace(" ", "").split(",") if x}
SNOOZE_MIN = int(os.environ.get("SNOOZE_MIN", "30"))
DB_PATH = os.environ.get("DB_PATH", "data/reminders.db")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
MIN_INTERVAL = 5

# Часовые пояса на выбор
TZS = {
    "spb": ("Санкт-Петербург", "Europe/Moscow"),
    "nor": ("Норильск", "Asia/Krasnoyarsk"),
}
def tz_of(key): return ZoneInfo(TZS.get(key, TZS["spb"])[1])
def tz_name(key): return TZS.get(key, TZS["spb"])[0]
def fmt(ts, key="spb"): return datetime.fromtimestamp(ts, tz_of(key)).strftime("%d.%m.%Y %H:%M")
def esc(s): return html.escape(str(s))

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS employees(id INTEGER PRIMARY KEY, name TEXT, approved INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reminders(
 id INTEGER PRIMARY KEY AUTOINCREMENT, emp_id INTEGER, text TEXT, next_ts INTEGER,
 interval_min INTEGER, total INTEGER, sent INTEGER DEFAULT 0, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS log(ts INTEGER, rem_id INTEGER, ok INTEGER, info TEXT);
""")
try: db.execute("ALTER TABLE reminders ADD COLUMN tz TEXT DEFAULT 'spb'")
except sqlite3.OperationalError: pass
db.commit()

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

def is_admin(uid): return uid in ADMINS
def kb(rows): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows])

BTN_NEW, BTN_ACT, BTN_HIST, BTN_EMP, BTN_ADD, BTN_CANCEL, BTN_MY = (
    "➕ Новое напоминание", "📋 Активные", "🗂 История", "👥 Сотрудники", "🙋 Добавить сотрудника", "✖️ Отмена", "📌 Мои задачи")
MENU_TEXTS = [BTN_NEW, BTN_ACT, BTN_HIST, BTN_EMP, BTN_ADD, BTN_CANCEL, BTN_MY]
ADMIN_MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_ACT)],
    [KeyboardButton(text=BTN_HIST), KeyboardButton(text=BTN_EMP)],
    [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)
EMP_MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_MY)]], resize_keyboard=True)
NOT_MENU = (F.text, ~F.text.in_(MENU_TEXTS), ~F.text.startswith("/"))

IV_KB = kb([[("1 раз", "iv:0")], [("Каждые 30 мин", "iv:30"), ("Каждый час", "iv:60")],
            [("Каждые 2 часа", "iv:120"), ("Каждый день", "iv:1440")], [("Свой интервал", "iv:x")]])
CNT_KB = kb([[(str(n), f"ct:{n}") for n in (2, 3, 5, 10)]])
TZ_KB = kb([[("Санкт-Петербург", "tz:spb")], [("Норильск", "tz:nor")]])

class New(StatesGroup):
    text = State(); tz = State(); when = State(); interval = State(); count = State()
class AddEmp(StatesGroup):
    data = State()
class Report(StatesGroup):
    wait = State()

# ---------- старт и регистрация ----------
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    if is_admin(uid):
        return await m.answer("👋 <b>Привет, администратор!</b>\n\nЗдесь вы ставите задачи сотрудникам, а я напоминаю о них в нужное время, пока человек не подтвердит выполнение.\n\nПользуйтесь кнопками внизу.", reply_markup=ADMIN_MENU)
    row = db.execute("SELECT approved, name FROM employees WHERE id=?", (uid,)).fetchone()
    if row and row["approved"]:
        return await m.answer(f"👋 Привет, <b>{esc(row['name'])}</b>!\n\nСюда будут приходить задачи от руководителя. Нажмите «Сделано», когда выполните.", reply_markup=EMP_MENU)
    name = m.from_user.full_name
    db.execute("INSERT OR IGNORE INTO employees(id,name,approved) VALUES(?,?,0)", (uid, name)); db.commit()
    await m.answer("⏳ Заявка отправлена руководителю. Как только её одобрят, я напишу.")
    for a in ADMINS:
        try: await bot.send_message(a, f"🆕 Новый пользователь: <b>{esc(name)}</b> (id <code>{uid}</code>)",
                                    reply_markup=kb([[("✅ Одобрить", f"ap:{uid}"), ("🚫 Отклонить", f"rj:{uid}")]]))
        except Exception: pass

@dp.callback_query(F.data.startswith(("ap:", "rj:")))
async def approve(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа", show_alert=True)
    act, uid = c.data.split(":"); uid = int(uid)
    if act == "ap":
        db.execute("UPDATE employees SET approved=1 WHERE id=?", (uid,)); db.commit()
        await c.message.edit_text(f"✅ Сотрудник <code>{uid}</code> одобрен.")
        try: await bot.send_message(uid, "✅ Доступ одобрен! Теперь я буду присылать вам задачи.", reply_markup=EMP_MENU)
        except Exception: pass
    else:
        db.execute("DELETE FROM employees WHERE id=?", (uid,)); db.commit()
        await c.message.edit_text(f"🚫 Заявка <code>{uid}</code> отклонена.")

@dp.message(F.text == BTN_CANCEL)
async def cancel_btn(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Окей, отменил.", reply_markup=ADMIN_MENU if is_admin(m.from_user.id) else EMP_MENU)

# ---------- сотрудники ----------
@dp.message(F.text == BTN_ADD)
async def addemp_btn(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear(); await state.set_state(AddEmp.data)
    await m.answer("🙋 Пришлите Telegram ID и имя через пробел.\nНапример: <code>1117238943 Ваня</code>")

@dp.message(AddEmp.data, *NOT_MENU)
async def addemp_data(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    p = m.text.split(maxsplit=1)
    if len(p) < 2 or not p[0].isdigit(): return await m.answer("Формат: ID и имя, например <code>1117238943 Ваня</code>")
    db.execute("INSERT OR REPLACE INTO employees(id,name,approved) VALUES(?,?,1)", (int(p[0]), p[1])); db.commit()
    await state.clear()
    await m.answer(f"✅ <b>{esc(p[1])}</b> добавлен.\nЧеловеку нужно один раз нажать Start у бота, иначе Telegram не даст мне ему писать.", reply_markup=ADMIN_MENU)

@dp.message(Command("addemp"))
async def addemp(m: Message):
    if not is_admin(m.from_user.id): return
    p = m.text.split(maxsplit=2)
    if len(p) < 3 or not p[1].isdigit(): return await m.answer("Формат: /addemp 123456789 Иван Петров")
    db.execute("INSERT OR REPLACE INTO employees(id,name,approved) VALUES(?,?,1)", (int(p[1]), p[2])); db.commit()
    await m.answer("✅ Сотрудник добавлен.")

@dp.message(Command("employees"))
@dp.message(F.text == BTN_EMP)
async def emps(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear()
    rows = db.execute("SELECT * FROM employees WHERE approved=1").fetchall()
    await m.answer("👥 <b>Сотрудники</b>\n\n" + "\n".join(f"• {esc(r['name'])} — <code>{r['id']}</code>" for r in rows)
                   if rows else "Сотрудников пока нет. Нажмите «Добавить сотрудника».")

# ---------- создание напоминания ----------
@dp.message(Command("new"))
@dp.message(F.text == BTN_NEW)
async def new(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    rows = db.execute("SELECT * FROM employees WHERE approved=1").fetchall()
    if not rows: return await m.answer("Сначала добавьте сотрудника кнопкой «Добавить сотрудника».")
    await state.clear()
    await m.answer("👤 <b>Шаг 1.</b> Кому напомнить?", reply_markup=kb([[(r["name"], f"to:{r['id']}")] for r in rows]))

@dp.callback_query(F.data.startswith("to:"))
async def pick(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа", show_alert=True)
    await state.update_data(emp=int(c.data[3:])); await state.set_state(New.text)
    await c.message.answer("✍️ <b>Шаг 2.</b> Напишите текст напоминания — он уйдёт сотруднику без изменений."); await c.answer()

@dp.message(New.text, *NOT_MENU)
async def s_text(m: Message, state: FSMContext):
    await state.update_data(text=m.text); await state.set_state(New.tz)
    await m.answer("🌍 <b>Шаг 3.</b> В каком часовом поясе указываете время?", reply_markup=TZ_KB)

@dp.callback_query(F.data.startswith("tz:"), New.tz)
async def s_tz(c: CallbackQuery, state: FSMContext):
    key = c.data[3:]; await state.update_data(tz=key); await state.set_state(New.when)
    await c.message.answer(f"🕒 <b>Шаг 4.</b> Когда отправить первое сообщение?\nВыберите кнопку или введите <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (время: {tz_name(key)}).",
                           reply_markup=kb([[("Через 5 минут", "wh:5"), ("Через 1 час", "wh:60")], [("Завтра в 10:00", "wh:tm10")]]))
    await c.answer()

async def after_when(target: Message, state: FSMContext, ts: int):
    d = await state.get_data()
    await state.update_data(ts=ts); await state.set_state(New.interval)
    await target.answer(f"✅ Первая отправка: <b>{fmt(ts, d['tz'])}</b> ({tz_name(d['tz'])}).\n\n🔁 <b>Шаг 5.</b> Как часто повторять?", reply_markup=IV_KB)

@dp.callback_query(F.data.startswith("wh:"), New.when)
async def s_when_btn(c: CallbackQuery, state: FSMContext):
    d = await state.get_data(); v = c.data[3:]
    if v == "tm10":
        t = datetime.now(tz_of(d["tz"])).replace(hour=10, minute=0, second=0, microsecond=0)
        ts = int(t.timestamp()) + 86400
    else:
        ts = int(time.time()) + int(v) * 60
    await c.answer(); await after_when(c.message, state, ts)

@dp.message(New.when, *NOT_MENU)
async def s_when(m: Message, state: FSMContext):
    d = await state.get_data()
    try: ts = int(datetime.strptime(m.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=tz_of(d["tz"])).timestamp())
    except ValueError: return await m.answer("Не понял формат. Пример: <code>28.10.2026 10:00</code>")
    if ts < time.time(): return await m.answer("Это время уже прошло. Введите время в будущем.")
    await after_when(m, state, ts)

@dp.callback_query(F.data.startswith("iv:"), New.interval)
async def s_iv_btn(c: CallbackQuery, state: FSMContext):
    v = c.data[3:]; await c.answer()
    if v == "x": return await c.message.answer(f"Введите интервал в минутах (не меньше {MIN_INTERVAL}).")
    iv = int(v); await state.update_data(iv=iv)
    if iv == 0:
        await state.update_data(total=1); return await card(c.message, state)
    await state.set_state(New.count); await c.message.answer("🔢 <b>Шаг 6.</b> Сколько всего отправок, включая первую?", reply_markup=CNT_KB)

@dp.message(New.interval, *NOT_MENU)
async def s_int(m: Message, state: FSMContext):
    if not m.text.strip().isdigit(): return await m.answer("Введите число минут.")
    iv = int(m.text)
    if iv != 0 and iv < MIN_INTERVAL: return await m.answer(f"Минимальный интервал — {MIN_INTERVAL} минут.")
    await state.update_data(iv=iv)
    if iv == 0:
        await state.update_data(total=1); return await card(m, state)
    await state.set_state(New.count); await m.answer("🔢 <b>Шаг 6.</b> Сколько всего отправок, включая первую?", reply_markup=CNT_KB)

@dp.callback_query(F.data.startswith("ct:"), New.count)
async def s_cnt_btn(c: CallbackQuery, state: FSMContext):
    await state.update_data(total=int(c.data[3:])); await c.answer(); await card(c.message, state)

@dp.message(New.count, *NOT_MENU)
async def s_cnt(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not 1 <= int(m.text) <= 50: return await m.answer("Число от 1 до 50.")
    await state.update_data(total=int(m.text)); await card(m, state)

async def card(m: Message, state: FSMContext):
    d = await state.get_data()
    emp = db.execute("SELECT name FROM employees WHERE id=?", (d["emp"],)).fetchone()["name"]
    times = "\n".join(f"   {i+1}. {fmt(d['ts'] + i * d['iv'] * 60, d['tz'])}" for i in range(d["total"]))
    await state.set_state(None)
    await m.answer(f"📝 <b>Проверьте напоминание</b>\n\n👤 Кому: <b>{esc(emp)}</b>\n💬 Текст: {esc(d['text'])}\n"
                   f"🌍 Часовой пояс: {tz_name(d['tz'])}\n📆 Отправки ({d['total']}):\n{times}\n\n"
                   f"✅ Остановится, когда сотрудник нажмёт «Сделано».\n⏰ «Отложить» сдвигает на {SNOOZE_MIN} мин.",
                   reply_markup=kb([[("✅ Подтвердить", "ok"), ("✖️ Отменить", "no")]]))

@dp.callback_query(F.data.in_({"ok", "no"}))
async def confirm(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа", show_alert=True)
    d = await state.get_data()
    if c.data == "no" or "emp" not in d:
        await state.clear(); return await c.message.edit_text("Отменено.")
    cur = db.execute("INSERT INTO reminders(emp_id,text,next_ts,interval_min,total,tz) VALUES(?,?,?,?,?,?)",
                     (d["emp"], d["text"], d["ts"], d["iv"], d["total"], d["tz"])); db.commit()
    await state.clear(); await c.message.edit_text(f"🎉 Готово! Напоминание №{cur.lastrowid} создано и поставлено в расписание.")

# ---------- списки ----------
@dp.message(Command("list"))
@dp.message(F.text == BTN_ACT)
async def lst(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear()
    rows = db.execute("SELECT r.*, e.name FROM reminders r JOIN employees e ON e.id=r.emp_id WHERE status IN ('active','exhausted')").fetchall()
    if not rows: return await m.answer("Активных задач нет. Все спокойно.")
    for r in rows:
        extra = "\n⚠️ Отправки закончились, выполнение не подтверждено" if r["status"] == "exhausted" else f"\n⏭ Следующая: {fmt(r['next_ts'], r['tz'])}"
        await m.answer(f"📌 <b>№{r['id']}</b> · {esc(r['name'])}\n{esc(r['text'])}\nОтправлено {r['sent']} из {r['total']}{extra}",
                       reply_markup=kb([[("🗑 Отменить", f"cx:{r['id']}")]]))

@dp.message(F.text == BTN_HIST)
async def hist(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): return
    await state.clear()
    rows = db.execute("SELECT r.*, e.name FROM reminders r JOIN employees e ON e.id=r.emp_id WHERE status IN ('done','cancelled','failed') ORDER BY r.id DESC LIMIT 10").fetchall()
    icon = {"done": "✅ выполнено", "cancelled": "🗑 отменено", "failed": "❌ не доставлено"}
    await m.answer("🗂 <b>Последние задачи</b>\n\n" + "\n".join(f"№{r['id']} · {esc(r['name'])} · {esc(r['text'])[:40]} — {icon[r['status']]}" for r in rows) if rows else "История пока пуста.")

@dp.callback_query(F.data.startswith("cx:"))
async def cancel(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа", show_alert=True)
    db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND status IN ('active','exhausted')", (int(c.data[3:]),)); db.commit()
    await c.message.edit_text("🗑 Напоминание отменено.")

@dp.message(F.text == BTN_MY)
async def my(m: Message):
    rows = db.execute("SELECT * FROM reminders WHERE emp_id=? AND status IN ('active','exhausted')", (m.from_user.id,)).fetchall()
    if not rows: return await m.answer("🎉 Активных задач нет!")
    for r in rows:
        await m.answer(f"📌 <b>Задача №{r['id']}</b>\n{esc(r['text'])}", reply_markup=kb([[("✅ Сделано", f"done:{r['id']}")]]))

# ---------- кнопки сотрудника ----------
@dp.callback_query(F.data.startswith(("done:", "snz:")))
async def emp_btn(c: CallbackQuery):
    act, rid = c.data.split(":"); r = db.execute("SELECT * FROM reminders WHERE id=?", (int(rid),)).fetchone()
    if not r or r["emp_id"] != c.from_user.id: return await c.answer("Это не ваша задача", show_alert=True)
    if r["status"] in ("done", "cancelled"): return await c.answer("Задача уже закрыта", show_alert=True)
    if act == "done":
        db.execute("UPDATE reminders SET status='done' WHERE id=?", (r["id"],)); db.commit()
        await c.message.edit_text(f"✅ <b>Выполнено</b>\n\n{esc(r['text'])}")
        await c.message.answer("Отлично, молодец! 🙌\nХотите приложить отчёт (фото или комментарий)?",
                               reply_markup=kb([[("📎 Прислать отчёт", f"rp:{r['id']}"), ("Без отчёта", f"nr:{r['id']}")]]))
        await c.answer("Принято!")
        for a in ADMINS:
            try: await bot.send_message(a, f"✅ <b>{esc(c.from_user.full_name)}</b> выполнил(а) задачу №{r['id']}:\n«{esc(r['text'])}»")
            except Exception: pass
    else:
        if r["status"] != "active": return await c.answer("Больше отправок не запланировано", show_alert=True)
        db.execute("UPDATE reminders SET next_ts=? WHERE id=?", (int(time.time()) + SNOOZE_MIN * 60, r["id"])); db.commit()
        await c.message.edit_text(f"⏰ <b>Отложено на {SNOOZE_MIN} мин</b>\n\n{esc(r['text'])}")
        await c.answer("Напомню позже")

@dp.callback_query(F.data.startswith(("rp:", "nr:")))
async def report_choice(c: CallbackQuery, state: FSMContext):
    act, rid = c.data.split(":"); r = db.execute("SELECT * FROM reminders WHERE id=?", (int(rid),)).fetchone()
    if not r or r["emp_id"] != c.from_user.id: return await c.answer("Это не ваша задача", show_alert=True)
    if act == "nr":
        await c.message.edit_text("Хорошо, без отчёта. Спасибо! 👍"); return await c.answer()
    await state.set_state(Report.wait); await state.update_data(rid=r["id"])
    await c.message.edit_text("📎 Пришлите фото, файл или текст одним сообщением — я передам руководителю."); await c.answer()

@dp.message(Report.wait)
async def report_msg(m: Message, state: FSMContext):
    d = await state.get_data(); r = db.execute("SELECT * FROM reminders WHERE id=?", (d.get("rid", 0),)).fetchone()
    await state.clear()
    for a in ADMINS:
        try:
            await bot.send_message(a, f"📎 Отчёт от <b>{esc(m.from_user.full_name)}</b> по задаче №{r['id']} «{esc(r['text'])}»:")
            await m.copy_to(a)
        except Exception: pass
    await m.answer("✅ Отчёт отправлен руководителю. Спасибо!")

# ---------- планировщик ----------
async def scheduler():
    while True:
        try:
            now = int(time.time())
            for r in db.execute("SELECT * FROM reminders WHERE status='active' AND next_ts<=?", (now,)).fetchall():
                n = r["sent"] + 1
                nxt = now + r["interval_min"] * 60  # просроченные не копятся: одна отправка
                db.execute("UPDATE reminders SET sent=?, next_ts=?, status=? WHERE id=?",
                           (n, nxt, "exhausted" if n >= r["total"] else "active", r["id"])); db.commit()
                try:
                    await bot.send_message(r["emp_id"], f"🔔 <b>Напоминание</b>\n\n{esc(r['text'])}\n\n📌 Задача №{r['id']} · сообщение {n} из {r['total']}",
                                           reply_markup=kb([[("✅ Сделано", f"done:{r['id']}"), (f"⏰ Отложить на {SNOOZE_MIN} мин", f"snz:{r['id']}")]]))
                    db.execute("INSERT INTO log VALUES(?,?,1,'sent')", (now, r["id"]))
                except Exception as e:
                    db.execute("UPDATE reminders SET status='failed' WHERE id=?", (r["id"],))
                    db.execute("INSERT INTO log VALUES(?,?,0,?)", (now, r["id"], str(e)[:200]))
                    for a in ADMINS:
                        try: await bot.send_message(a, f"❌ Задача №{r['id']} остановлена: не удалось доставить сообщение ({esc(e)}). Возможно, сотрудник не нажал Start у бота.")
                        except Exception: pass
                    db.commit(); continue
                db.commit()
                if n >= r["total"]:
                    for a in ADMINS:
                        try: await bot.send_message(a, f"⚠️ Задача №{r['id']}: отправки закончились, выполнение не подтверждено.")
                        except Exception: pass
        except Exception:
            logging.exception("scheduler")
        await asyncio.sleep(20)

async def main():
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
