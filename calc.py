"""
StepRace Bot — расчёты и форматирование.
"""

import datetime
import db

TARGET_STEPS = 10000


def today_msk():
    """Текущая дата по МСК (UTC+3), без внешних зависимостей от tz базы данных ОС."""
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).date()


def day_number_for_date(date_obj, start_date):
    return (date_obj - start_date).days + 1


def date_for_day_number(day_num, start_date):
    return start_date + datetime.timedelta(days=day_num - 1)


def current_day_number(start_date):
    d = day_number_for_date(today_msk(), start_date)
    return max(d, 1)


def format_num(n):
    n = int(round(n or 0))
    return f"{n:,}".replace(",", " ")


MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def format_date_ru(date_obj):
    return f"{date_obj.day} {MONTHS_RU[date_obj.month - 1]} {date_obj.year}"


# ---------- AGGREGATE CALCULATIONS ----------

def day_summary(day_num):
    """Возвращает (total_steps, contributors_count) за день day_num по всем участникам."""
    steps_map = db.get_steps_for_day(day_num)
    total = sum(steps_map.values())
    contributors = len(steps_map)
    return total, contributors


def team_total_and_avg(start_date):
    """Возвращает (total_steps_all_time, member_count, elapsed_days, cumulative_avg_per_person)."""
    members = db.get_all_members()
    member_count = len(members)
    all_steps = db.get_all_steps()
    total = sum(s["value"] for s in all_steps)
    elapsed = current_day_number(start_date)
    cum_avg = (total / member_count / elapsed) if member_count > 0 and elapsed > 0 else 0
    return total, member_count, elapsed, cum_avg


def leadership_goal(start_date):
    """Возвращает (rival_dict или None, needed_per_person_per_day)."""
    rivals = db.get_rivals()
    if not rivals:
        return None, None
    leader = rivals[0]  # уже отсортированы по avg_per_person DESC
    _, _, _, cum_avg = team_total_and_avg(start_date)
    needed = (leader["avg_per_person"] + 1) - cum_avg
    return leader, needed


def members_hit_target(day_num):
    """Возвращает (hit_list, miss_list, nodata_list) — списки словарей участников за день."""
    members = db.get_all_members()
    steps_map = db.get_steps_for_day(day_num)
    hit, miss, nodata = [], [], []
    for m in members:
        v = steps_map.get(m["tg_id"])
        if v is None:
            nodata.append(m)
        elif v >= TARGET_STEPS:
            hit.append(m)
        else:
            miss.append(m)
    return hit, miss, nodata


def ranking():
    """Возвращает список (member, total) отсортированный по total убыв."""
    members = db.get_all_members()
    result = []
    for m in members:
        total = db.get_member_total(m["tg_id"])
        result.append((m, total))
    result.sort(key=lambda x: x[1], reverse=True)
    return result


def standings(start_date):
    """Турнирная таблица: наша команда + соперники, отсортированы по среднему/чел/день убыв."""
    _, _, _, cum_avg = team_total_and_avg(start_date)
    our_name = db.get_setting("team_name", "Наша команда")
    rows = [{"name": our_name, "avg": cum_avg, "is_us": True, "id": None}]
    for r in db.get_rivals():
        rows.append({"name": r["name"], "avg": r["avg_per_person"], "is_us": False, "id": r["id"]})
    rows.sort(key=lambda x: x["avg"], reverse=True)
    return rows
