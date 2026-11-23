"""
Проверки еженедельного режима без обращения к Telegram.

Запуск из каталога проекта:
    python tests/test_weekly.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("ADMIN_IDS", "1")
TEST_DB = ROOT / "tests" / "test.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DB_PATH"] = str(TEST_DB)

import bot  # noqa: E402

# В тестах не зависим от пакета tzdata (на Windows его нужно ставить отдельно).
MOSCOW = timezone(timedelta(hours=3))
bot.tz_of = lambda key: MOSCOW


def ts_of(year, month, day, hour, minute):
    return int(datetime(year, month, day, hour, minute, tzinfo=MOSCOW).timestamp())


def reset_db():
    bot.db.execute("DELETE FROM reminders")
    bot.db.execute("DELETE FROM series_history")
    bot.db.execute("DELETE FROM employees")
    bot.db.execute("DELETE FROM log")
    bot.db.commit()
    bot.db.execute("INSERT OR REPLACE INTO employees(id,name,approved) VALUES(2,'Тест',1)")
    bot.db.commit()


def insert_weekly(start_ts, total=5, interval=30, emp=2, text="тест"):
    cur = bot.db.execute("""
        INSERT INTO reminders
            (emp_id, text, next_ts, interval_min, total, sent, status, tz,
             mode, weekday, day_minute, series_no, series_start, reopen_ts, created_ts)
        VALUES (?, ?, ?, ?, ?, 0, 'active', 'spb', 'weekly', 0, ?, 1, ?, NULL, ?)
    """, (emp, text, start_ts, interval, total, 19 * 60, start_ts, start_ts))
    bot.db.commit()
    return cur.lastrowid


def insert_once(start_ts, total=2, interval=30):
    cur = bot.db.execute("""
        INSERT INTO reminders
            (emp_id, text, next_ts, interval_min, total, sent, status, tz,
             mode, weekday, day_minute, series_no, series_start, reopen_ts, created_ts)
        VALUES (2, 'разовая', ?, ?, ?, 0, 'active', 'spb', 'once', NULL, NULL, 1, ?, NULL, ?)
    """, (start_ts, interval, total, start_ts, start_ts))
    bot.db.commit()
    return cur.lastrowid


def get_rem(rid):
    return bot.db.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone()


def history(rid):
    return bot.db.execute(
        "SELECT * FROM series_history WHERE rem_id=? ORDER BY series_no", (rid,)).fetchall()


async def scenario_weekly_full_cycle():
    reset_db()
    start = ts_of(2026, 11, 23, 19, 0)  # понедельник
    rid = insert_weekly(start)
    sent = []

    async def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))

    original = bot.bot.send_message
    bot.bot.send_message = fake_send
    try:
        for i in range(5):
            await bot.scheduler_tick(start + i * 1800)

        r = get_rem(rid)
        assert r["sent"] == 5, r["sent"]
        assert r["status"] == "exhausted", r["status"]
        assert r["reopen_ts"] == start + 7 * 86400, r["reopen_ts"]
        employee_msgs = [x for x in sent if x[0] == 2]
        assert len(employee_msgs) == 5, len(employee_msgs)
        for i, (_, text, _) in enumerate(employee_msgs):
            assert f"сообщение {i + 1} из 5" in text

        h = history(rid)
        assert len(h) == 1 and h[0]["status"] == "exhausted", h[0]["status"]
        assert h[0]["sent"] == 5

        # Следующий понедельник: серия №2 должна начаться сама.
        await bot.scheduler_tick(start + 7 * 86400)
        r = get_rem(rid)
        assert r["series_no"] == 2, r["series_no"]
        assert r["sent"] == 1, r["sent"]
        assert r["status"] == "active", r["status"]
        assert r["series_start"] == start + 7 * 86400
        employee_msgs = [x for x in sent if x[0] == 2]
        assert len(employee_msgs) == 6
        assert "серия 2" in employee_msgs[-1][1]
        print("OK  полный недельный цикл: 5 отправок -> exhausted -> новая серия")
    finally:
        bot.bot.send_message = original


async def scenario_done_closes_current_series_only():
    reset_db()
    start = ts_of(2026, 11, 30, 19, 0)  # следующий понедельник
    rid = insert_weekly(start)
    sent = []

    async def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))

    original = bot.bot.send_message
    bot.bot.send_message = fake_send
    try:
        await bot.scheduler_tick(start)
        r = get_rem(rid)
        assert r["sent"] == 1

        reopen = bot.mark_series_done(r, start + 60)
        r = get_rem(rid)
        assert r["status"] == "done"
        assert reopen == start + 7 * 86400
        assert r["reopen_ts"] == reopen
        h = history(rid)
        assert len(h) == 1 and h[0]["status"] == "done"

        # В следующий понедельник задача снова активна, серия №2.
        await bot.scheduler_tick(reopen)
        r = get_rem(rid)
        assert r["series_no"] == 2
        assert r["sent"] == 1
        assert r["status"] == "active"
        employee_msgs = [x for x in sent if x[0] == 2]
        assert "серия 2" in employee_msgs[-1][1]
        print("OK  «Сделано» закрывает только текущую серию, следующая стартует сама")
    finally:
        bot.bot.send_message = original


async def scenario_once_still_terminal():
    reset_db()
    start = ts_of(2026, 12, 1, 10, 0)
    rid = insert_once(start, total=2, interval=30)
    sent = []

    async def fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))

    original = bot.bot.send_message
    bot.bot.send_message = fake_send
    try:
        await bot.scheduler_tick(start)
        await bot.scheduler_tick(start + 1800)
        r = get_rem(rid)
        assert r["status"] == "exhausted", r["status"]
        assert r["reopen_ts"] is None
        assert len([x for x in sent if x[0] == 2]) == 2
        h = history(rid)
        assert len(h) == 1 and h[0]["status"] == "exhausted"
        print("OK  разовый режим не затронут")
    finally:
        bot.bot.send_message = original


class FakeUser:
    id = 2
    full_name = "Тест"


class FakeMessage:
    def __init__(self):
        self.edits = []
        self.answers = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.from_user = FakeUser()
        self.message = FakeMessage()
        self.alerts = []

    async def answer(self, text=None, **kwargs):
        self.alerts.append(text)


async def scenario_stale_button_does_not_close_new_series():
    reset_db()
    start = ts_of(2026, 12, 7, 19, 0)
    rid = insert_weekly(start)

    async def fake_send(chat_id, text, **kwargs):
        return None

    original = bot.bot.send_message
    bot.bot.send_message = fake_send
    try:
        await bot.scheduler_tick(start)
        bot.mark_series_done(get_rem(rid), start + 60)
        await bot.scheduler_tick(start + 7 * 86400)  # открылась серия №2
        r = get_rem(rid)
        assert r["series_no"] == 2 and r["status"] == "active"

        stale = FakeCallback(f"done:{rid}:1")
        await bot.emp_btn(stale)
        r = get_rem(rid)
        assert r["status"] == "active", "старая кнопка не должна закрывать новую серию"
        assert any("уже завершена" in (a or "") for a in stale.alerts)

        current = FakeCallback(f"done:{rid}:2")
        await bot.emp_btn(current)
        assert get_rem(rid)["status"] == "done"
        print("OK  кнопка прошлой серии не закрывает новую")
    finally:
        bot.bot.send_message = original


async def scenario_done_after_last_send_updates_history():
    reset_db()
    start = ts_of(2026, 12, 14, 19, 0)
    rid = insert_weekly(start, total=2, interval=30)

    async def fake_send(chat_id, text, **kwargs):
        return None

    original = bot.bot.send_message
    bot.bot.send_message = fake_send
    try:
        await bot.scheduler_tick(start)
        await bot.scheduler_tick(start + 1800)
        r = get_rem(rid)
        assert r["status"] == "exhausted"
        assert history(rid)[0]["status"] == "exhausted"

        callback = FakeCallback(f"done:{rid}:1")
        await bot.emp_btn(callback)
        r = get_rem(rid)
        assert r["status"] == "done"
        assert r["reopen_ts"] == start + 7 * 86400
        assert history(rid)[0]["status"] == "done", "история должна обновиться на «выполнено»"
        print("OK  «Сделано» после последней отправки обновляет историю")
    finally:
        bot.bot.send_message = original


def scenario_helpers():
    # 23.11.2026 — понедельник. В 18:00 ближайший понедельник 19:00 — сегодня.
    now = ts_of(2026, 11, 23, 18, 0)
    assert bot.weekly_first_ts(0, 19 * 60, "spb", now_ts=now) == ts_of(2026, 11, 23, 19, 0)
    # В 20:00 сегодняшний слот уже прошёл — следующий понедельник.
    now = ts_of(2026, 11, 23, 20, 0)
    assert bot.weekly_first_ts(0, 19 * 60, "spb", now_ts=now) == ts_of(2026, 11, 30, 19, 0)
    # Вторник -> ближайший понедельник.
    now = ts_of(2026, 11, 24, 12, 0)
    assert bot.weekly_first_ts(0, 19 * 60, "spb", now_ts=now) == ts_of(2026, 11, 30, 19, 0)

    assert bot.next_week_ts(ts_of(2026, 11, 23, 19, 0), "spb") == ts_of(2026, 11, 30, 19, 0)

    assert bot.weekly_capacity_error(
        {"mode": "weekly", "day_minute": 19 * 60, "iv": 30, "total": 5}) is None
    error = bot.weekly_capacity_error(
        {"mode": "weekly", "day_minute": 19 * 60, "iv": 30, "total": 11})
    assert error and "максимум отправок — <b>10</b>" in error, error

    assert bot.parse_hm("19:00") == 19 * 60
    assert bot.parse_hm("9.30") == 9 * 60 + 30
    assert bot.parse_hm("1900") == 19 * 60
    assert bot.parse_hm("25:00") is None
    print("OK  расчёт ближайшего дня, недели и вместимости серии")


async def main():
    scenario_helpers()
    await scenario_weekly_full_cycle()
    await scenario_done_closes_current_series_only()
    await scenario_stale_button_does_not_close_new_series()
    await scenario_done_after_last_send_updates_history()
    await scenario_once_still_terminal()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
