"""
StepRace Bot — работа с базой данных (SQLite).
Все функции синхронные; используются в хендлерах через run_in_executor при необходимости,
но для простоты и небольшой нагрузки (командное соревнование) вызываются напрямую — SQLite
с одним файлом и WAL-режимом спокойно справляется с такой нагрузкой.
"""

import sqlite3
import datetime
import os

DB_PATH = os.environ.get("STEPRACE_DB_PATH", "steprace.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS members (
        tg_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        joined_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS steps (
        member_id INTEGER NOT NULL,
        day_num INTEGER NOT NULL,
        value INTEGER NOT NULL,
        entered_by INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (member_id, day_num)
    );

    CREATE TABLE IF NOT EXISTS rivals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        avg_per_person REAL NOT NULL
    );
    """)
    conn.commit()
    conn.close()


# ---------- SETTINGS ----------

def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def get_start_date():
    """Возвращает datetime.date или None, если соревнование ещё не настроено."""
    v = get_setting("start_date")
    if not v:
        return None
    return datetime.date.fromisoformat(v)


def set_start_date(date_obj):
    set_setting("start_date", date_obj.isoformat())


def get_finish_date():
    v = get_setting("finish_date")
    if not v:
        return None
    return datetime.date.fromisoformat(v)


def set_finish_date(date_obj):
    set_setting("finish_date", date_obj.isoformat())


def is_finished():
    return get_setting("is_finished") == "1"


def mark_finished():
    set_setting("is_finished", "1")


# ---------- MEMBERS ----------

def add_member(tg_id, name, is_admin=False):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO members (tg_id, name, is_admin, joined_at) VALUES (?, ?, ?, ?)",
        (tg_id, name, 1 if is_admin else 0, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_member(tg_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM members WHERE tg_id=?", (tg_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_member_by_name(name):
    """Поиск участника по имени (без учёта регистра), для команды /for.
    Сравнение регистра делается в Python, а не в SQL: встроенная функция
    LOWER() в SQLite по умолчанию работает только с ASCII и не понимает
    кириллицу, поэтому 'Аня' и 'аня' не совпали бы при сравнении в самом запросе.
    """
    conn = get_conn()
    rows = conn.execute("SELECT * FROM members").fetchall()
    conn.close()
    target = name.strip().lower()
    for r in rows:
        if r["name"].strip().lower() == target:
            return dict(r)
    return None


def get_all_members():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM members ORDER BY joined_at ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def has_any_member():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) as c FROM members").fetchone()
    conn.close()
    return row["c"] > 0


def rename_member(tg_id, new_name):
    conn = get_conn()
    conn.execute("UPDATE members SET name=? WHERE tg_id=?", (new_name, tg_id))
    conn.commit()
    conn.close()


# ---------- STEPS ----------

def set_steps(member_id, day_num, value, entered_by):
    conn = get_conn()
    conn.execute(
        "INSERT INTO steps (member_id, day_num, value, entered_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(member_id, day_num) DO UPDATE SET "
        "value=excluded.value, entered_by=excluded.entered_by, updated_at=excluded.updated_at",
        (member_id, day_num, value, entered_by, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_steps(member_id, day_num):
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM steps WHERE member_id=? AND day_num=?", (member_id, day_num)
    ).fetchone()
    conn.close()
    return row["value"] if row else None


def get_steps_for_day(day_num):
    """Возвращает dict {member_id: value} за указанный день."""
    conn = get_conn()
    rows = conn.execute("SELECT member_id, value FROM steps WHERE day_num=?", (day_num,)).fetchall()
    conn.close()
    return {r["member_id"]: r["value"] for r in rows}


def get_all_steps():
    """Возвращает список словарей {member_id, day_num, value}."""
    conn = get_conn()
    rows = conn.execute("SELECT member_id, day_num, value FROM steps").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_member_total(member_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(value), 0) as total FROM steps WHERE member_id=?", (member_id,)
    ).fetchone()
    conn.close()
    return row["total"]


def get_member_streak(member_id, current_day):
    """Считает текущую серию дней подряд с введёнными данными, заканчивая current_day (включительно).
    Если за current_day данных ещё нет, отступает на день назад как точку отсчёта.
    """
    conn = get_conn()
    streak = 0
    day = current_day
    # если сегодня ещё не введено — серия считается по последнему введённому дню
    row = conn.execute(
        "SELECT value FROM steps WHERE member_id=? AND day_num=?", (member_id, day)
    ).fetchone()
    if row is None:
        day -= 1
    while day >= 1:
        row = conn.execute(
            "SELECT value FROM steps WHERE member_id=? AND day_num=?", (member_id, day)
        ).fetchone()
        if row is None:
            break
        streak += 1
        day -= 1
    conn.close()
    return streak


def get_member_days_with_data(member_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT day_num FROM steps WHERE member_id=? ORDER BY day_num", (member_id,)
    ).fetchall()
    conn.close()
    return [r["day_num"] for r in rows]


# ---------- RIVALS ----------

def add_rival(name, avg_per_person):
    conn = get_conn()
    conn.execute("INSERT INTO rivals (name, avg_per_person) VALUES (?, ?)", (name, avg_per_person))
    conn.commit()
    conn.close()


def get_rivals():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM rivals ORDER BY avg_per_person DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_rival(rival_id):
    conn = get_conn()
    conn.execute("DELETE FROM rivals WHERE id=?", (rival_id,))
    conn.commit()
    conn.close()


def update_rival_avg(rival_id, new_avg):
    conn = get_conn()
    conn.execute("UPDATE rivals SET avg_per_person=? WHERE id=?", (new_avg, rival_id))
    conn.commit()
    conn.close()
