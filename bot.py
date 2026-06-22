"""
StepRace Bot — командный трекер шагов в Telegram.

Запуск:
    export TELEGRAM_BOT_TOKEN="ваш_токен_от_BotFather"
    python bot.py

Зависимости: python-telegram-bot==21.6, apscheduler, pytz
"""

import logging
import os
import datetime
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import db
import calc

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
MSK_TZ = pytz.timezone("Europe/Moscow")

# Простое in-memory состояние диалога "жду дату старта от админа" / "жду имя при регистрации"
# Ключ: tg_id пользователя, значение: строка-состояние.
PENDING = {}


# ---------- HELPERS ----------

def is_registered(tg_id):
    return db.get_member(tg_id) is not None


def require_setup_done():
    return db.get_start_date() is not None


async def send_help(update: Update):
    text = (
        "👟 *StepRace* — командный трекер шагов\n\n"
        "*Ввод шагов:*\n"
        "Просто отправь число — запишутся шаги за сегодня.\n"
        "`/day 3 9500` — записать/исправить шаги за день №3.\n"
        "`/for Аня 8000` — записать шаги за сегодня другому участнику (по имени).\n"
        "`/for Аня 3 8000` — то же самое, но за конкретный день.\n\n"
        "*Просмотр:*\n"
        "/today — что внесено сегодня по всей команде.\n"
        "/stats — общая статистика и рейтинг.\n"
        "/streak — у кого сколько дней подряд.\n"
        "/rivals — турнирная таблица с соперниками.\n\n"
        "*Прочее:*\n"
        "/rename Новое\\_имя — изменить своё имя в рейтинге.\n"
        "/help — это сообщение.\n"
    )
    if db.get_member(update.effective_user.id) and db.get_member(update.effective_user.id)["is_admin"]:
        text += (
            "\n*Команды администратора:*\n"
            "/addrival Название 8500 — добавить соперника со средним числом шагов/чел/день.\n"
            "/addrivalbulk Название 1200000 15 — добавить соперника по общей сумме шагов и числу людей "
            "(среднее посчитается автоматически).\n"
            "/delrival — список соперников с кнопками удаления.\n"
            "/setfinish 2026-07-20 — задать дату финиша соревнования.\n"
            "/finish — подвести итоги и завершить соревнование.\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------- START / REGISTRATION ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    existing = db.get_member(tg_id)
    if existing:
        await update.message.reply_text(
            f"Привет, {existing['name']}! Ты уже зарегистрирован(а). /help — список команд."
        )
        return

    if not db.has_any_member():
        # Первый пользователь — становится админом, сначала просим имя, потом дату старта
        PENDING[tg_id] = "awaiting_name_admin"
        await update.message.reply_text(
            "Привет! Ты первый, кто запускает бота — будешь администратором соревнования 🛠\n\n"
            "Как тебя записать в рейтинге? Напиши имя."
        )
    else:
        PENDING[tg_id] = "awaiting_name"
        await update.message.reply_text(
            "Привет! Присоединяешься к командному соревнованию StepRace 👟\n\n"
            "Как тебя записать в рейтинге? Напиши имя."
        )


async def handle_pending_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Обрабатывает текстовые ответы в режиме ожидания (имя, дата старта).
    Возвращает True, если сообщение было обработано как часть такого диалога.
    """
    tg_id = update.effective_user.id
    state = PENDING.get(tg_id)
    if state is None:
        return False

    if state == "awaiting_name_admin":
        name = text.strip()
        if not name:
            await update.message.reply_text("Имя не может быть пустым. Напиши имя.")
            return True
        db.add_member(tg_id, name, is_admin=True)
        PENDING[tg_id] = "awaiting_start_date"
        await update.message.reply_text(
            f"Отлично, {name}! Теперь укажи дату старта соревнования в формате ГГГГ-ММ-ДД "
            "(например, 2026-06-22). Можно также написать «сегодня»."
        )
        return True

    if state == "awaiting_start_date":
        raw = text.strip().lower()
        if raw == "сегодня":
            date_obj = calc.today_msk()
        else:
            try:
                date_obj = datetime.date.fromisoformat(raw)
            except ValueError:
                await update.message.reply_text(
                    "Не понял дату. Формат: ГГГГ-ММ-ДД (например, 2026-06-22), или напиши «сегодня»."
                )
                return True
        db.set_start_date(date_obj)
        db.set_setting("team_name", "Наша команда")
        del PENDING[tg_id]
        await update.message.reply_text(
            f"Дата старта установлена: {calc.format_date_ru(date_obj)} (день №1).\n\n"
            "Соревнование запущено! Теперь просто отправляй число шагов в чат каждый день.\n"
            "Когда остальные участники напишут боту /start, они тоже присоединятся.\n\n"
            "/help — все команды."
        )
        return True

    if state == "awaiting_name":
        name = text.strip()
        if not name:
            await update.message.reply_text("Имя не может быть пустым. Напиши имя.")
            return True
        db.add_member(tg_id, name, is_admin=False)
        del PENDING[tg_id]
        await update.message.reply_text(
            f"Готово, {name}! Ты в команде 🎉\n\nПросто отправляй число шагов каждый день. /help — все команды."
        )
        return True

    return False


# ---------- STEP ENTRY ----------

def steps_confirmation_keyboard(member_id, day_num):
    """Кнопки быстрой правки значения после ввода (пресеты)."""
    presets = [5000, 8000, 10000, 12000, 15000]
    row = [
        InlineKeyboardButton(calc.format_num(p), callback_data=f"preset:{member_id}:{day_num}:{p}")
        for p in presets
    ]
    return InlineKeyboardMarkup([row[:3], row[3:]])


async def record_steps(update: Update, target_member, day_num, value, entered_by_id):
    start_date = db.get_start_date()
    db.set_steps(target_member["tg_id"], day_num, value, entered_by_id)
    date_obj = calc.date_for_day_number(day_num, start_date)

    who_part = ""
    if target_member["tg_id"] != entered_by_id:
        who_part = f" (внёс {db.get_member(entered_by_id)['name']})"

    text = (
        f"✅ Сохранено: {target_member['name']} — {calc.format_num(value)} шагов "
        f"за день №{day_num} ({calc.format_date_ru(date_obj)}){who_part}."
    )
    if value >= calc.TARGET_STEPS:
        text += "\n🎯 Норма 10 000 достигнута!"

    await update.message.reply_text(
        text, reply_markup=steps_confirmation_keyboard(target_member["tg_id"], day_num)
    )


async def cmd_plain_number(update: Update, context: ContextTypes.DEFAULT_TYPE, value: int):
    tg_id = update.effective_user.id
    member = db.get_member(tg_id)
    if not member:
        await update.message.reply_text("Сначала зарегистрируйся: /start")
        return
    start_date = db.get_start_date()
    if start_date is None:
        await update.message.reply_text("Соревнование пока не настроено. Попроси админа выполнить /start.")
        return
    if value < 0:
        await update.message.reply_text("Число шагов не может быть отрицательным.")
        return
    day_num = calc.current_day_number(start_date)
    await record_steps(update, member, day_num, value, tg_id)


async def cmd_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    member = db.get_member(tg_id)
    if not member:
        await update.message.reply_text("Сначала зарегистрируйся: /start")
        return
    start_date = db.get_start_date()
    if start_date is None:
        await update.message.reply_text("Соревнование пока не настроено.")
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Использование: /day НомерДня Шаги — например, /day 3 9500")
        return
    try:
        day_num = int(args[0])
        value = int(args[1])
    except ValueError:
        await update.message.reply_text("Номер дня и шаги должны быть числами. Пример: /day 3 9500")
        return
    if day_num < 1:
        await update.message.reply_text("Номер дня должен быть от 1 и больше.")
        return
    today_num = calc.current_day_number(start_date)
    if day_num > today_num:
        await update.message.reply_text("Нельзя вносить данные за день, который ещё не наступил.")
        return
    if value < 0:
        await update.message.reply_text("Число шагов не может быть отрицательным.")
        return
    await record_steps(update, member, day_num, value, tg_id)


async def cmd_for(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    member = db.get_member(tg_id)
    if not member:
        await update.message.reply_text("Сначала зарегистрируйся: /start")
        return
    start_date = db.get_start_date()
    if start_date is None:
        await update.message.reply_text("Соревнование пока не настроено.")
        return
    args = context.args
    if len(args) == 2:
        name, steps_str = args
        day_num = calc.current_day_number(start_date)
    elif len(args) == 3:
        name, day_str, steps_str = args
        try:
            day_num = int(day_str)
        except ValueError:
            await update.message.reply_text(
                "Использование: /for Имя Шаги — или: /for Имя НомерДня Шаги"
            )
            return
    else:
        await update.message.reply_text(
            "Использование: /for Имя Шаги — или: /for Имя НомерДня Шаги\n"
            "Например: /for Аня 8000  или  /for Аня 3 8000"
        )
        return

    try:
        value = int(steps_str)
    except ValueError:
        await update.message.reply_text("Количество шагов должно быть числом.")
        return
    if value < 0:
        await update.message.reply_text("Число шагов не может быть отрицательным.")
        return
    today_num = calc.current_day_number(start_date)
    if day_num < 1 or day_num > today_num:
        await update.message.reply_text("Указан некорректный номер дня.")
        return

    target = db.get_member_by_name(name)
    if not target:
        await update.message.reply_text(
            f"Не нашёл участника «{name}». Проверь, что он уже выполнил /start, и имя написано верно."
        )
        return

    await record_steps(update, target, day_num, value, tg_id)


async def callback_preset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, member_id_str, day_num_str, value_str = query.data.split(":")
    member_id = int(member_id_str)
    day_num = int(day_num_str)
    value = int(value_str)

    member = db.get_member(member_id)
    if not member:
        await query.edit_message_text("Участник не найден.")
        return

    tg_id = update.effective_user.id
    start_date = db.get_start_date()
    db.set_steps(member_id, day_num, value, tg_id)
    date_obj = calc.date_for_day_number(day_num, start_date)
    text = (
        f"✅ Сохранено: {member['name']} — {calc.format_num(value)} шагов "
        f"за день №{day_num} ({calc.format_date_ru(date_obj)})."
    )
    if value >= calc.TARGET_STEPS:
        text += "\n🎯 Норма 10 000 достигнута!"
    await query.edit_message_text(text)


# ---------- VIEWS: /today /stats /streak ----------

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_date = db.get_start_date()
    if start_date is None:
        await update.message.reply_text("Соревнование пока не настроено.")
        return
    day_num = calc.current_day_number(start_date)
    date_obj = calc.date_for_day_number(day_num, start_date)
    total, contributors = calc.day_summary(day_num)
    hit, miss, nodata = calc.members_hit_target(day_num)

    lines = [f"📅 День №{day_num} · {calc.format_date_ru(date_obj)}\n"]
    lines.append(f"Шагов команды за сегодня: {calc.format_num(total)}")
    if contributors > 0:
        lines.append(f"Среднее за день / чел: {calc.format_num(total / contributors)}")
    lines.append(f"\n🎯 Достигли 10 000: {len(hit)} из {len(hit) + len(miss) + len(nodata)}")

    if hit:
        lines.append("\n✅ Достигли:")
        for m in hit:
            v = db.get_steps(m["tg_id"], day_num)
            lines.append(f"  {m['name']} — {calc.format_num(v)}")
    if miss:
        lines.append("\n🔸 Внесли, но меньше 10 000:")
        for m in miss:
            v = db.get_steps(m["tg_id"], day_num)
            lines.append(f"  {m['name']} — {calc.format_num(v)}")
    if nodata:
        lines.append("\n⬜️ Пока не внесли:")
        for m in nodata:
            lines.append(f"  {m['name']}")

    await update.message.reply_text("\n".join(lines))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_date = db.get_start_date()
    if start_date is None:
        await update.message.reply_text("Соревнование пока не настроено.")
        return
    total, member_count, elapsed, cum_avg = calc.team_total_and_avg(start_date)
    team_name = db.get_setting("team_name", "Наша команда")

    lines = [f"📊 Статистика «{team_name}»\n"]
    lines.append(f"Идёт день №{elapsed} ({calc.format_date_ru(calc.today_msk())})")
    lines.append(f"Участников: {member_count}")
    lines.append(f"Всего шагов за соревнование: {calc.format_num(total)}")
    lines.append(f"Накопительное среднее / чел / день: {calc.format_num(cum_avg)}")

    leader, needed = calc.leadership_goal(start_date)
    if leader:
        if needed and needed > 0:
            lines.append(
                f"\n🎯 Чтобы обогнать лидера «{leader['name']}», нужно ещё "
                f"{calc.format_num(needed)} шагов / чел / день."
            )
        else:
            lines.append(f"\n🏆 Вы опережаете лидера среди соперников «{leader['name']}»!")

    lines.append("\n🏅 Рейтинг участников:")
    rank = calc.ranking()
    medals = ["🥇", "🥈", "🥉"]
    for i, (m, mtotal) in enumerate(rank):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {m['name']} — {calc.format_num(mtotal)}")

    await update.message.reply_text("\n".join(lines))


async def cmd_streak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_date = db.get_start_date()
    if start_date is None:
        await update.message.reply_text("Соревнование пока не настроено.")
        return
    day_num = calc.current_day_number(start_date)
    members = db.get_all_members()
    rows = []
    for m in members:
        streak = db.get_member_streak(m["tg_id"], day_num)
        rows.append((m, streak))
    rows.sort(key=lambda x: x[1], reverse=True)

    lines = ["🔥 Серии дней подряд с внесёнными данными:\n"]
    for m, streak in rows:
        if streak >= 3:
            lines.append(f"{m['name']} — {streak} дней подряд 🔥")
        elif streak > 0:
            lines.append(f"{m['name']} — {streak} дн.")
        else:
            lines.append(f"{m['name']} — серия прервана")
    await update.message.reply_text("\n".join(lines))


# ---------- RIVALS ----------

async def cmd_rivals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_date = db.get_start_date()
    if start_date is None:
        await update.message.reply_text("Соревнование пока не настроено.")
        return
    rows = calc.standings(start_date)
    lines = ["🏁 Турнирная таблица (среднее шагов / чел / день):\n"]
    for i, r in enumerate(rows):
        tag = " (мы)" if r["is_us"] else ""
        lines.append(f"{i + 1}. {r['name']}{tag} — {calc.format_num(r['avg'])}")
    if len(rows) == 1:
        lines.append("\nСоперников пока нет — добавит админ через /addrival.")
    await update.message.reply_text("\n".join(lines))


def require_admin(tg_id):
    member = db.get_member(tg_id)
    return member is not None and member["is_admin"] == 1


async def cmd_addrival(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not require_admin(tg_id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /addrival Название 8500\n(среднее число шагов на человека в день)"
        )
        return
    try:
        avg = float(args[-1])
    except ValueError:
        await update.message.reply_text("Последним аргументом должно быть число (среднее шагов/чел/день).")
        return
    name = " ".join(args[:-1])
    if not name:
        await update.message.reply_text("Укажи название команды-соперника.")
        return
    db.add_rival(name, avg)
    await update.message.reply_text(f"Добавлено: «{name}» — {calc.format_num(avg)} шагов/чел/день.")


async def cmd_addrivalbulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавить соперника по общей сумме шагов и числу участников — среднее посчитается само."""
    tg_id = update.effective_user.id
    if not require_admin(tg_id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "Использование: /addrivalbulk Название ОбщаяСумма КоличествоУчастников\n"
            "Например: /addrivalbulk Орлы 1200000 15\n"
            "Среднее посчитается автоматически (сумма ÷ людей). Если у тебя уже есть число дней "
            "соревнования и хочешь среднее в день — раздели сумму на число дней заранее."
        )
        return
    try:
        people = int(args[-1])
        total_steps = float(args[-2])
    except ValueError:
        await update.message.reply_text(
            "Последние два аргумента должны быть числами: общая сумма шагов и число участников."
        )
        return
    if people <= 0:
        await update.message.reply_text("Количество участников должно быть больше нуля.")
        return
    name = " ".join(args[:-2])
    if not name:
        await update.message.reply_text("Укажи название команды-соперника.")
        return
    avg = total_steps / people
    db.add_rival(name, avg)
    await update.message.reply_text(
        f"Добавлено: «{name}» — {calc.format_num(total_steps)} шагов ÷ {people} чел. "
        f"= {calc.format_num(avg)} шагов/чел в среднем."
    )


async def cmd_delrival(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not require_admin(tg_id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    rivals = db.get_rivals()
    if not rivals:
        await update.message.reply_text("Соперников пока нет.")
        return
    buttons = [
        [InlineKeyboardButton(f"🗑 {r['name']}", callback_data=f"delrival:{r['id']}")]
        for r in rivals
    ]
    await update.message.reply_text(
        "Выбери, кого удалить:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def callback_delrival(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_id = update.effective_user.id
    if not require_admin(tg_id):
        await query.edit_message_text("Эта команда доступна только администратору.")
        return
    rival_id = int(query.data.split(":")[1])
    db.delete_rival(rival_id)
    await query.edit_message_text("Удалено.")


# ---------- RENAME / FINISH ----------

async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    member = db.get_member(tg_id)
    if not member:
        await update.message.reply_text("Сначала зарегистрируйся: /start")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /rename Новое имя")
        return
    new_name = " ".join(args).strip()
    if not new_name:
        await update.message.reply_text("Имя не может быть пустым.")
        return
    db.rename_member(tg_id, new_name)
    await update.message.reply_text(f"Готово, теперь ты в рейтинге как «{new_name}».")


async def cmd_setfinish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not require_admin(tg_id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Использование: /setfinish 2026-07-20")
        return
    try:
        date_obj = datetime.date.fromisoformat(args[0])
    except ValueError:
        await update.message.reply_text("Формат даты: ГГГГ-ММ-ДД, например 2026-07-20.")
        return
    db.set_finish_date(date_obj)
    await update.message.reply_text(f"Дата финиша установлена: {calc.format_date_ru(date_obj)}.")


async def cmd_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not require_admin(tg_id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return
    start_date = db.get_start_date()
    if start_date is None:
        await update.message.reply_text("Соревнование пока не настроено.")
        return

    total, member_count, elapsed, cum_avg = calc.team_total_and_avg(start_date)
    team_name = db.get_setting("team_name", "Наша команда")
    rows = calc.standings(start_date)
    our_place = next(i for i, r in enumerate(rows) if r["is_us"]) + 1

    lines = [f"🏁 Итоги соревнования «{team_name}»\n"]
    lines.append(f"Дней пройдено: {elapsed}")
    lines.append(f"Всего шагов командой: {calc.format_num(total)}")
    lines.append(f"Среднее / чел / день: {calc.format_num(cum_avg)}")
    lines.append(f"\nМесто в турнирной таблице: {our_place} из {len(rows)}")

    if our_place == 1 and len(rows) > 1:
        lines.append("🏆 Поздравляем — вы обогнали всех соперников!")
    elif len(rows) > 1:
        leader = rows[0]
        lines.append(f"Лидер турнирной таблицы: «{leader['name']}» — {calc.format_num(leader['avg'])}.")

    lines.append("\n🏅 Финальный рейтинг участников:")
    rank = calc.ranking()
    medals = ["🥇", "🥈", "🥉"]
    for i, (m, mtotal) in enumerate(rank):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} {m['name']} — {calc.format_num(mtotal)}")

    db.mark_finished()
    await update.message.reply_text("\n".join(lines))


# ---------- MESSAGE ROUTER ----------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    tg_id = update.effective_user.id

    # Сначала проверяем, не находится ли пользователь в процессе регистрации/настройки
    handled = await handle_pending_text(update, context, text)
    if handled:
        return

    # Если это просто число — записываем шаги за сегодня
    cleaned = text.replace(" ", "").replace("\u00a0", "")
    if cleaned.lstrip("-").isdigit():
        value = int(cleaned)
        await cmd_plain_number(update, context, value)
        return

    await update.message.reply_text(
        "Не понял сообщение 🤔 Отправь число шагов, или набери /help, чтобы увидеть список команд."
    )


# ---------- REMINDERS ----------

async def send_reminders(app: Application):
    """Отправляет напоминание всем, кто не внёс шаги за сегодняшний день. Вызывается планировщиком."""
    start_date = db.get_start_date()
    if start_date is None or db.is_finished():
        return
    day_num = calc.current_day_number(start_date)
    _, _, nodata = calc.members_hit_target(day_num)
    for m in nodata:
        try:
            await app.bot.send_message(
                chat_id=m["tg_id"],
                text=(
                    f"☀️ Доброе утро! Не забудь внести шаги за день №{day_num} — "
                    "просто отправь число в этот чат."
                ),
            )
        except Exception as e:
            logger.warning(f"Не удалось отправить напоминание {m['tg_id']}: {e}")


# ---------- HEALTH CHECK SERVER (для бесплатных хостингов типа Render Web Service) ----------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("StepRace bot is running.".encode("utf-8"))

    def log_message(self, format, *args):
        pass  # не шумим в логах health-check'ами


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health-check сервер запущен на порту {port}.")


# ---------- MAIN ----------

async def _on_startup(app: Application):
    """Выполняется уже внутри работающего event loop python-telegram-bot,
    поэтому scheduler можно безопасно стартовать здесь (на Python 3.14
    AsyncIOScheduler.start() требует наличие активного running loop)."""
    scheduler = AsyncIOScheduler(timezone=MSK_TZ)
    scheduler.add_job(
        lambda: app.create_task(send_reminders(app)),
        CronTrigger(hour=8, minute=50, timezone=MSK_TZ),
    )
    scheduler.start()
    logger.info("Планировщик напоминаний запущен (08:50 МСК).")


def main():
    if not TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Установи переменную окружения с токеном от @BotFather."
        )

    db.init_db()
    start_health_server()

    app = Application.builder().token(TOKEN).post_init(_on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", lambda u, c: send_help(u)))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("for", cmd_for))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("streak", cmd_streak))
    app.add_handler(CommandHandler("rivals", cmd_rivals))
    app.add_handler(CommandHandler("addrival", cmd_addrival))
    app.add_handler(CommandHandler("addrivalbulk", cmd_addrivalbulk))
    app.add_handler(CommandHandler("delrival", cmd_delrival))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("setfinish", cmd_setfinish))
    app.add_handler(CommandHandler("finish", cmd_finish))

    app.add_handler(CallbackQueryHandler(callback_preset, pattern=r"^preset:"))
    app.add_handler(CallbackQueryHandler(callback_delrival, pattern=r"^delrival:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("StepRace bot запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
