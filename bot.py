#!/usr/bin/env python3
"""
WB Ad Manager Bot v2
• Показывает список всех рекламных кампаний с реальными статусами
• Управление каждой кампанией: старт / пауза / расписание
• Расписание хранится в памяти (сбрасывается при рестарте — для персистентности используй SQLite)
• Работает через GitHub Actions cron (polling не нужен — бот отвечает на webhook или polling)
"""

import os, logging, asyncio, json
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

import httpx
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("wb")

# ── Config ─────────────────────────────────────────────────────────────────────
TG_TOKEN  = os.environ["TELEGRAM_TOKEN"]
OWNER_ID  = int(os.environ["ALLOWED_USER_ID"])
WB_TOKEN  = os.environ["WB_API_TOKEN"]
TZ        = ZoneInfo("Europe/Moscow")
WB_BASE   = "https://advert-api.wb.ru"

# ── Conversation states ────────────────────────────────────────────────────────
SET_ON, SET_OFF = range(2)

# ── In-memory schedule store: {campaign_id: {"on": 8, "off": 23, "enabled": True}} ──
schedules: dict[int, dict] = defaultdict(lambda: {"on": 8, "off": 23, "enabled": True})
# Временно хранит campaign_id пока пользователь вводит время
pending_schedule: dict[int, int] = {}  # user_id -> campaign_id

# ── WB status map ──────────────────────────────────────────────────────────────
STATUS = {
    -1: "🗑 Удалена",
     4: "⏸ Готова",
     7: "✅ Завершена",
     8: "🚫 Отменена",
     9: "🟢 Активна",
    11: "⏸ На паузе",
}

# ── WB API helpers ─────────────────────────────────────────────────────────────

def _headers():
    return {"Authorization": WB_TOKEN, "Content-Type": "application/json"}


async def wb_get_campaigns() -> list[dict]:
    """Получить все кампании аккаунта."""
    url = f"{WB_BASE}/adv/v2/promotion/count"
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.get(url, headers=_headers())
            if r.status_code != 200:
                log.error("count error: %s %s", r.status_code, r.text[:200])
                return []
            data = r.json()
            all_ids = []
            for group in data.get("adverts", []):
                for adv in group.get("advert_list", []):
                    all_ids.append(adv["advertId"])
            if not all_ids:
                return []
            # Получить детали по ID
            r2 = await c.post(
                f"{WB_BASE}/adv/v2/promotion/adverts",
                headers=_headers(),
                json=all_ids[:50],  # API принимает до 50
                timeout=15,
            )
            if r2.status_code != 200:
                return []
            return r2.json() or []
        except Exception as e:
            log.error("wb_get_campaigns: %s", e)
            return []


async def wb_action(campaign_id: int, action: str) -> tuple[bool, str]:
    """action: start | pause"""
    url = f"{WB_BASE}/adv/v0/{action}?id={campaign_id}"
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.get(url, headers=_headers())
            return r.status_code == 200, r.text[:200]
        except Exception as e:
            return False, str(e)

# ── Auth ───────────────────────────────────────────────────────────────────────

def auth(update: Update) -> bool:
    return update.effective_user.id == OWNER_ID

# ── Keyboards ──────────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Список кампаний", callback_data="list"),
        InlineKeyboardButton("🔄 Обновить", callback_data="list"),
    ]])


def campaign_keyboard(cid: int, raw_status: int):
    sched = schedules[cid]
    sched_icon = "✅" if sched["enabled"] else "⏸"
    can_start = raw_status in (4, 11)
    can_pause  = raw_status == 9

    rows = []
    if can_start:
        rows.append([InlineKeyboardButton("▶️ Запустить", callback_data=f"start:{cid}")])
    if can_pause:
        rows.append([InlineKeyboardButton("⏸ Пауза", callback_data=f"pause:{cid}")])

    rows.append([
        InlineKeyboardButton(
            f"{sched_icon} Расписание: {sched['on']:02d}:00 – {sched['off']:02d}:00",
            callback_data=f"sched_menu:{cid}"
        )
    ])
    rows.append([InlineKeyboardButton("« Назад", callback_data="list")])
    return InlineKeyboardMarkup(rows)


def schedule_keyboard(cid: int):
    sched = schedules[cid]
    toggle_label = "⏸ Выключить расписание" if sched["enabled"] else "▶️ Включить расписание"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🕐 Изменить время", callback_data=f"sched_edit:{cid}")],
        [InlineKeyboardButton(toggle_label,        callback_data=f"sched_toggle:{cid}")],
        [InlineKeyboardButton("« К кампании",      callback_data=f"camp:{cid}")],
    ])

# ── Handlers ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    text = (
        "👋 <b>WB Ad Manager</b>\n\n"
        "Управляю рекламными кампаниями Wildberries.\n"
        "Нажми кнопку ниже чтобы увидеть все кампании."
    )
    await update.message.reply_text(text, parse_mode="HTML",
                                    reply_markup=main_menu_keyboard())


async def show_campaign_list(query, ctx):
    """Показать список всех кампаний."""
    await query.answer()
    await query.edit_message_text("⏳ Загружаю список кампаний...", parse_mode="HTML")

    campaigns = await wb_get_campaigns()
    if not campaigns:
        await query.edit_message_text(
            "😕 Кампании не найдены или ошибка API.\n\nПроверь WB_API_TOKEN.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Попробовать снова", callback_data="list")
            ]])
        )
        return

    lines = ["<b>📋 Рекламные кампании</b>\n"]
    buttons = []
    for c in campaigns:
        cid   = c.get("advertId", 0)
        name  = c.get("name", f"Кампания {cid}")[:32]
        st    = c.get("status", -1)
        label = STATUS.get(st, f"Статус {st}")
        lines.append(f"{label}  <code>{name}</code>")
        buttons.append([InlineKeyboardButton(
            f"{label}  {name}", callback_data=f"camp:{cid}:{st}"
        )])

    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="list")])
    text = "\n".join(lines)
    await query.edit_message_text(text, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(buttons))


async def show_campaign(query, cid: int, raw_status: int, ctx):
    """Показать карточку одной кампании."""
    await query.answer()
    campaigns = await wb_get_campaigns()
    camp = next((c for c in campaigns if c.get("advertId") == cid), None)

    if not camp:
        await query.edit_message_text("❌ Кампания не найдена.", parse_mode="HTML")
        return

    st     = camp.get("status", raw_status)
    name   = camp.get("name", f"Кампания {cid}")
    tp     = camp.get("type", "")
    sched  = schedules[cid]
    s_icon = "✅ активно" if sched["enabled"] else "⏸ выключено"

    type_map = {4: "Каталог", 5: "Карточка", 6: "Поиск", 7: "Реклама главной", 8: "Автоматическая"}
    type_name = type_map.get(tp, f"Тип {tp}")

    text = (
        f"<b>{name}</b>\n\n"
        f"🆔 ID: <code>{cid}</code>\n"
        f"📌 Статус: {STATUS.get(st, st)}\n"
        f"🏷 Тип: {type_name}\n\n"
        f"⏰ <b>Расписание</b> ({s_icon}):\n"
        f"  🟢 Включать в {sched['on']:02d}:00 МСК\n"
        f"  🔴 Выключать в {sched['off']:02d}:00 МСК"
    )
    await query.edit_message_text(text, parse_mode="HTML",
                                  reply_markup=campaign_keyboard(cid, st))


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    query = update.callback_query
    data  = query.data

    # ── Список кампаний ─────────────────────────────────────────────────────
    if data == "list":
        await show_campaign_list(query, ctx)

    # ── Карточка кампании ───────────────────────────────────────────────────
    elif data.startswith("camp:"):
        parts = data.split(":")
        cid = int(parts[1])
        st  = int(parts[2]) if len(parts) > 2 else 9
        await show_campaign(query, cid, st, ctx)

    # ── Запустить кампанию ──────────────────────────────────────────────────
    elif data.startswith("start:"):
        cid = int(data.split(":")[1])
        await query.answer("⏳ Запускаю...")
        ok, msg = await wb_action(cid, "start")
        now = datetime.now(TZ).strftime("%H:%M")
        if ok:
            await query.edit_message_text(
                f"🟢 <b>Реклама запущена</b> в {now} МСК\n🆔 Кампания: <code>{cid}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏸ Поставить на паузу", callback_data=f"pause:{cid}")],
                    [InlineKeyboardButton("« Назад к списку",     callback_data="list")],
                ])
            )
        else:
            await query.edit_message_text(
                f"⚠️ Ошибка запуска\nОтвет API: <code>{msg}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("« Назад", callback_data="list")
                ]])
            )

    # ── Поставить на паузу ──────────────────────────────────────────────────
    elif data.startswith("pause:"):
        cid = int(data.split(":")[1])
        await query.answer("⏳ Останавливаю...")
        ok, msg = await wb_action(cid, "pause")
        now = datetime.now(TZ).strftime("%H:%M")
        if ok:
            await query.edit_message_text(
                f"🔴 <b>Реклама остановлена</b> в {now} МСК\n🆔 Кампания: <code>{cid}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ Запустить снова", callback_data=f"start:{cid}")],
                    [InlineKeyboardButton("« Назад к списку",  callback_data="list")],
                ])
            )
        else:
            await query.edit_message_text(
                f"⚠️ Ошибка остановки\nОтвет API: <code>{msg}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("« Назад", callback_data="list")
                ]])
            )

    # ── Меню расписания кампании ────────────────────────────────────────────
    elif data.startswith("sched_menu:"):
        cid   = int(data.split(":")[1])
        sched = schedules[cid]
        s_icon = "✅ активно" if sched["enabled"] else "⏸ выключено"
        await query.answer()
        await query.edit_message_text(
            f"⏰ <b>Расписание кампании</b> <code>{cid}</code>\n\n"
            f"Статус: {s_icon}\n"
            f"🟢 Включать: <b>{sched['on']:02d}:00</b> МСК\n"
            f"🔴 Выключать: <b>{sched['off']:02d}:00</b> МСК\n\n"
            f"GitHub Actions запустит рекламу ровно по расписанию.",
            parse_mode="HTML",
            reply_markup=schedule_keyboard(cid),
        )

    # ── Включить/выключить расписание ───────────────────────────────────────
    elif data.startswith("sched_toggle:"):
        cid = int(data.split(":")[1])
        schedules[cid]["enabled"] = not schedules[cid]["enabled"]
        status = "✅ активировано" if schedules[cid]["enabled"] else "⏸ приостановлено"
        await query.answer(f"Расписание {status}")
        # Обновить меню расписания
        sched = schedules[cid]
        s_icon = "✅ активно" if sched["enabled"] else "⏸ выключено"
        await query.edit_message_text(
            f"⏰ <b>Расписание кампании</b> <code>{cid}</code>\n\n"
            f"Статус: {s_icon}\n"
            f"🟢 Включать: <b>{sched['on']:02d}:00</b> МСК\n"
            f"🔴 Выключать: <b>{sched['off']:02d}:00</b> МСК",
            parse_mode="HTML",
            reply_markup=schedule_keyboard(cid),
        )

    # ── Начать редактирование расписания ────────────────────────────────────
    elif data.startswith("sched_edit:"):
        cid = int(data.split(":")[1])
        pending_schedule[update.effective_user.id] = cid
        await query.answer()
        await query.edit_message_text(
            f"✏️ <b>Изменить расписание</b>\n\n"
            f"Введи два числа через пробел:\n"
            f"<code>ЧАС_ВКЛ ЧАС_ВЫКЛ</code>\n\n"
            f"Например: <code>9 22</code> — включать в 9:00, выключать в 22:00 МСК\n\n"
            f"Или напиши /cancel чтобы отменить.",
            parse_mode="HTML",
        )
        return SET_ON  # начало ConversationHandler — но мы обрабатываем через MessageHandler ниже


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    pending_schedule.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "❌ Отменено.", reply_markup=main_menu_keyboard()
    )


async def handle_schedule_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода нового расписания."""
    if not auth(update): return
    uid = update.effective_user.id
    if uid not in pending_schedule:
        return  # не ждём ввода

    cid = pending_schedule.pop(uid)
    text = update.message.text.strip()

    try:
        parts = text.split()
        on_h  = int(parts[0])
        off_h = int(parts[1])
        assert 0 <= on_h <= 23 and 0 <= off_h <= 23 and on_h != off_h
    except Exception:
        await update.message.reply_text(
            "❌ Неверный формат. Введи два числа 0-23, например: <code>9 22</code>\n"
            "Попробуй ещё раз или /cancel",
            parse_mode="HTML",
        )
        pending_schedule[uid] = cid  # вернуть в ожидание
        return

    schedules[cid]["on"]  = on_h
    schedules[cid]["off"] = off_h

    await update.message.reply_text(
        f"✅ <b>Расписание обновлено!</b>\n\n"
        f"🆔 Кампания: <code>{cid}</code>\n"
        f"🟢 Включать: <b>{on_h:02d}:00</b> МСК\n"
        f"🔴 Выключать: <b>{off_h:02d}:00</b> МСК\n\n"
        f"Не забудь обновить cron в GitHub Actions если нужно!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("« К списку кампаний", callback_data="list")
        ]])
    )


# ── Main ───────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",  "Главное меню"),
        BotCommand("list",   "Список кампаний"),
        BotCommand("cancel", "Отменить ввод"),
        BotCommand("help",   "Справка"),
    ])


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    msg = await update.message.reply_text("⏳ Загружаю список кампаний...")
    campaigns = await wb_get_campaigns()
    if not campaigns:
        await msg.edit_text(
            "😕 Кампании не найдены или ошибка API.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Попробовать снова", callback_data="list")
            ]])
        )
        return

    buttons = []
    for c in campaigns:
        cid  = c.get("advertId", 0)
        name = c.get("name", f"Кампания {cid}")[:32]
        st   = c.get("status", -1)
        label = STATUS.get(st, f"?{st}")
        buttons.append([InlineKeyboardButton(
            f"{label}  {name}", callback_data=f"camp:{cid}:{st}"
        )])
    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="list")])

    await msg.edit_text(
        f"<b>📋 Найдено кампаний: {len(campaigns)}</b>\nВыбери кампанию:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text(
        "<b>📖 Справка WB Ad Manager</b>\n\n"
        "/start — главное меню\n"
        "/list — список всех кампаний\n"
        "/cancel — отменить текущий ввод\n\n"
        "<b>Через кнопки:</b>\n"
        "• Нажми на кампанию → увидишь статус и кнопки управления\n"
        "• ▶️ Запустить / ⏸ Пауза — мгновенное действие\n"
        "• ⏰ Расписание — настройка авто-включения/выключения\n\n"
        "<b>Авто-расписание работает через GitHub Actions</b>\n"
        "Кампании включаются/выключаются по cron даже когда ты не в боте.",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


def main():
    app = (
        Application.builder()
        .token(TG_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_schedule_input))

    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
