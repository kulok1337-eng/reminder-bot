"""Проверка обновления базы старого формата (без потери задач и истории)."""

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DB = ROOT / "tests" / "old.db"
if DB.exists():
    DB.unlink()

now = 1_800_000_000
conn = sqlite3.connect(DB)
conn.executescript("""
CREATE TABLE employees(id INTEGER PRIMARY KEY, name TEXT, approved INTEGER DEFAULT 0);
CREATE TABLE reminders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, emp_id INTEGER, text TEXT, next_ts INTEGER,
    interval_min INTEGER, total INTEGER, sent INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
    tz TEXT DEFAULT 'spb');
CREATE TABLE log(ts INTEGER, rem_id INTEGER, ok INTEGER, info TEXT);
""")
conn.execute("INSERT INTO employees(id,name,approved) VALUES(2,'Старый сотрудник',1)")
conn.execute("""INSERT INTO reminders(emp_id,text,next_ts,interval_min,total,sent,status,tz)
                VALUES(2,'старая активная',?,30,3,1,'active','spb')""", (now,))
conn.execute("""INSERT INTO reminders(emp_id,text,next_ts,interval_min,total,sent,status,tz)
                VALUES(2,'старая выполненная',?,30,3,3,'done','spb')""", (now,))
conn.commit()
conn.close()

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ["DB_PATH"] = str(DB)

import bot  # noqa: E402

cols = {row["name"] for row in bot.db.execute("PRAGMA table_info(reminders)")}
for column in ("mode", "weekday", "day_minute", "series_no", "series_start", "reopen_ts"):
    assert column in cols, column

rows = bot.db.execute("SELECT * FROM reminders ORDER BY id").fetchall()
assert len(rows) == 2
assert rows[0]["mode"] == "once"
assert rows[0]["series_no"] == 1
assert rows[0]["series_start"] == now

done_history = bot.db.execute(
    "SELECT * FROM series_history WHERE rem_id=2").fetchone()
assert done_history is not None
assert done_history["status"] == "done"
assert done_history["text"] == "старая выполненная"

active_history = bot.db.execute(
    "SELECT * FROM series_history WHERE rem_id=1").fetchone()
assert active_history is None

print("OK  миграция старой базы: поля добавлены, задачи и история на месте")
