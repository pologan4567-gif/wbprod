#!/usr/bin/env python3
import os, logging, asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wb")

TG_TOKEN  = os.environ["TELEGRAM_TOKEN"]
OWNER_ID  = int(os.environ["ALLOWED_USER_ID"])
WB_TOKEN  = os.environ["WB_API_TOKEN"]
TZ        = ZoneInfo("Europe/Moscow")

# Оба хоста — пробуем оба
WB_HOSTS = [
    "https://advert-api.wildberries.ru",
    "https://advert-api.wb.ru",
]

STATUS = {
    -1: "🗑 Удалена", 4: "⏸ Готова", 7: "✅ Завершена",
     8: "🚫 Отменена", 9: "🟢 Активна", 11: "⏸ На паузе",
}

schedules = defaultdict(lambda: {"on": 8, "off": 23, "enabled": True})
pending_schedule = {}


def _headers():
    return {"Authorization": WB_TOKEN, "Content-Type": "application/json"}


async def wb_get_campaigns() -> list[dict]:
    """Пробует оба хоста WB API."""
    for host in WB_HOSTS:
        async with httpx.AsyncClient(timeout=20) as c:
            try:
                log.info("Trying host: %s", host)
                r = await c.get(f"{host}/adv/v2/promotion/count", headers=_headers())
                log.info("count status=%s body=%s", r.status_code, r.text[:200])
                if r.status_code != 200:
                    continue
                data = r.json()
                all_ids = []
                for group in data.get("adverts", []):
                    for adv in group.get("advert_list", []):
                        all_ids.append(adv["advertId"])
                if not all_ids:
                    log.info("No campaign IDs found in response")
                    return []
                r2 = await c.post(
                    f"{host}/adv/v2/promotion/adverts",
                    headers=_headers(),
                    json=all_ids[:50],
                    timeout=20,
                )
                log.info("adverts status=%s", r2.status_code)
                if r2.status_code == 200:
                    return r2.json() or []
            except Exception as e:
                log.error("Host %s failed: %s", host, e)
                continue
    return []


async def wb_action(campaign_id: int, action: str) -> tuple[bool, str]:
    for host in WB_HOSTS:
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                r = await c.get(f"{host}/adv/v0/{action}?id={campaign_id}", headers=_headers())
                if r.status_code == 200:
                    return True, r.text[:200]
            except Exception as e:
                log.error("wb_action %s host %s: %s", action, host, e)
    return False, "Все хосты недоступны"


def auth(update: Update) -> bool:
    return update.effective_user.id == OWNER_ID


def main_menu_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Список кампаний", callback_data="list"),
    ]])


def campaign_keyboard(cid: int, raw_status: int):
    sched = schedules[cid]
    sched_icon = "✅" if sched["enabled"] else "⏸"
    rows = []
    if raw_status in (4, 11):
        rows.append([InlineKeyboardButton("▶️ Запустить", callback_data=f"start:{cid}")])
    if raw_status == 9:
        rows.append([InlineKeyboardButton("⏸ Пауза", callback_data=f"pause:{cid}")])
    rows.append([InlineKeyboardButton(
        f"{sched_icon} Расписание: {sched['on']:02d}:00 – {sched['off']:02d}:00",
        callback_data=f"sched_menu:{cid}"
    )])
    rows.append([InlineKeyboardButton("« Назад", callback_data="list")])
    return InlineKeyboardMarkup(rows)


def schedule_keyboard(cid: int):
    sched = schedules[cid]
    toggle = "⏸ Выключить расписание" if sched["enabled"] else "▶️ Включить расписание"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🕐 Изменить время", callback_data=f"sched_edit:{cid}")],
        [InlineKeyboardButton(toggle, callback_data=f"sched_toggle:{cid}")],
        [InlineKeyboardButton("« К кампании", callback_data=f"camp:{cid}:0")],
    ])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text(
        "👋 <b>WB Ad Manager</b>\n\nУправляю рекламными кампаниями Wildberries.",
        parse_mode="HTML", reply_markup=main_menu_keyboard()
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    msg = await update.message.reply_text("⏳ Загружаю список кампаний...")
    campaigns = await wb_get_campaigns()
    if not campaigns:
        await msg.edit_text(
            "😕 Кампании не найдены.\nВозможно API временно недоступен — попробуй через минуту.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Повторить", callback_data="list")]])
        )
        return
    buttons = []
    for c in campaigns:
        cid = c.get("advertId", 0)
        name = c.get("name", f"Кампания {cid}")[:35]
        st = c.get("status", -1)
        buttons.append([InlineKeyboardButton(f"{STATUS.get(st,'?')}  {name}", callback_data=f"camp:{cid}:{st}")])
    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="list")])
    await msg.edit_text(
        f"<b>📋 Найдено кампаний: {len(campaigns)}</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    query = update.callback_query
    data = query.data

    if data == "list":
        await query.answer()
        await query.edit_message_text("⏳ Загружаю...")
        campaigns = await wb_get_campaigns()
        if not campaigns:
            await query.edit_message_text(
                "😕 Кампании не найдены.\nПопробуй через минуту.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Повторить", callback_data="list")]])
            )
            return
        buttons = []
        for c in campaigns:
            cid = c.get("advertId", 0)
            name = c.get("name", f"Кампания {cid}")[:35]
            st = c.get("status", -1)
            buttons.append([InlineKeyboardButton(f"{STATUS.get(st,'?')}  {name}", callback_data=f"camp:{cid}:{st}")])
        buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="list")])
        await query.edit_message_text(
            f"<b>📋 Найдено кампаний: {len(campaigns)}</b>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("camp:"):
        parts = data.split(":")
        cid, st = int(parts[1]), int(parts[2])
        await query.answer()
        campaigns = await wb_get_campaigns()
        camp = next((c for c in campaigns if c.get("advertId") == cid), None)
        if not camp:
            await query.edit_message_text("❌ Кампания не найдена.")
            return
        real_st = camp.get("status", st)
        name = camp.get("name", f"Кампания {cid}")
        sched = schedules[cid]
        s_icon = "✅ активно" if sched["enabled"] else "⏸ выключено"
        type_map = {4:"Каталог",5:"Карточка",6:"Поиск",7:"Главная",8:"Авто"}
        await query.edit_message_text(
            f"<b>{name}</b>\n\n🆔 ID: <code>{cid}</code>\n📌 Статус: {STATUS.get(real_st, real_st)}\n"
            f"🏷 Тип: {type_map.get(camp.get('type',''), '—')}\n\n"
            f"⏰ <b>Расписание</b> ({s_icon}):\n  🟢 {sched['on']:02d}:00 МСК — включать\n  🔴 {sched['off']:02d}:00 МСК — выключать",
            parse_mode="HTML", reply_markup=campaign_keyboard(cid, real_st)
        )

    elif data.startswith("start:"):
        cid = int(data.split(":")[1])
        await query.answer("⏳")
        ok, msg = await wb_action(cid, "start")
        now = datetime.now(TZ).strftime("%H:%M")
        await query.edit_message_text(
            f"{'🟢 Реклама запущена' if ok else '⚠️ Ошибка: '+msg} в {now} МСК",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="list")]])
        )

    elif data.startswith("pause:"):
        cid = int(data.split(":")[1])
        await query.answer("⏳")
        ok, msg = await wb_action(cid, "pause")
        now = datetime.now(TZ).strftime("%H:%M")
        await query.edit_message_text(
            f"{'🔴 Реклама остановлена' if ok else '⚠️ Ошибка: '+msg} в {now} МСК",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="list")]])
        )

    elif data.startswith("sched_menu:"):
        cid = int(data.split(":")[1])
        sched = schedules[cid]
        await query.answer()
        await query.edit_message_text(
            f"⏰ <b>Расписание</b> кампании <code>{cid}</code>\n\n"
            f"🟢 Включать: <b>{sched['on']:02d}:00</b> МСК\n🔴 Выключать: <b>{sched['off']:02d}:00</b> МСК",
            parse_mode="HTML", reply_markup=schedule_keyboard(cid)
        )

    elif data.startswith("sched_toggle:"):
        cid = int(data.split(":")[1])
        schedules[cid]["enabled"] = not schedules[cid]["enabled"]
        sched = schedules[cid]
        await query.answer("Расписание " + ("включено" if sched["enabled"] else "выключено"))
        await query.edit_message_text(
            f"⏰ <b>Расписание</b> кампании <code>{cid}</code>\n\n"
            f"🟢 Включать: <b>{sched['on']:02d}:00</b> МСК\n🔴 Выключать: <b>{sched['off']:02d}:00</b> МСК",
            parse_mode="HTML", reply_markup=schedule_keyboard(cid)
        )

    elif data.startswith("sched_edit:"):
        cid = int(data.split(":")[1])
        pending_schedule[update.effective_user.id] = cid
        await query.answer()
        await query.edit_message_text(
            "✏️ Введи время в формате: <code>ЧАС_ВКЛ ЧАС_ВЫКЛ</code>\n\nПример: <code>9 22</code>",
            parse_mode="HTML"
        )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    uid = update.effective_user.id
    if uid not in pending_schedule: return
    cid = pending_schedule.pop(uid)
    try:
        parts = update.message.text.strip().split()
        on_h, off_h = int(parts[0]), int(parts[1])
        assert 0 <= on_h <= 23 and 0 <= off_h <= 23 and on_h != off_h
    except:
        await update.message.reply_text("❌ Неверный формат. Пример: <code>9 22</code>", parse_mode="HTML")
        pending_schedule[uid] = cid
        return
    schedules[cid]["on"] = on_h
    schedules[cid]["off"] = off_h
    await update.message.reply_text(
        f"✅ Расписание обновлено!\n🟢 {on_h:02d}:00 МСК — включать\n🔴 {off_h:02d}:00 МСК — выключать",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К списку", callback_data="list")]])
    )


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("list", "Список кампаний"),
    ])


def main():
    app = Application.builder().token(TG_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
