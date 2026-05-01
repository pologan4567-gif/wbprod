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
WB_BASE   = "https://advert-api.wildberries.ru"

STATUS = {
    -1: "🗑 Удалена", 4: "⏸ Готова", 7: "✅ Завершена",
     8: "🚫 Отменена", 9: "🟢 Активна", 11: "⏸ На паузе",
}
TYPE_MAP = {4:"Каталог", 5:"Карточка", 6:"Поиск", 7:"Главная", 8:"Авто"}

schedules = defaultdict(lambda: {"on": 8, "off": 23, "enabled": True})
pending_schedule = {}


def _h():
    return {"Authorization": WB_TOKEN}


async def wb_get_campaigns() -> list[dict]:
    """
    Шаг 1: GET /adv/v1/promotion/count  — получаем сгруппированный список ID
    Шаг 2: GET /api/advert/v2/adverts?ids=...  — получаем детали кампаний
    """
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            # Шаг 1
            r = await c.get(f"{WB_BASE}/adv/v1/promotion/count", headers=_h())
            log.info("promotion/count status=%s body=%s", r.status_code, r.text[:300])
            if r.status_code != 200:
                return []
            data = r.json()
            all_ids = []
            for group in data.get("adverts", []):
                for adv in group.get("advert_list", []):
                    all_ids.append(adv["advertId"])
            log.info("Found %d campaign IDs", len(all_ids))
            if not all_ids:
                return []
            # Шаг 2: запрашиваем детали порциями по 50
            result = []
            for i in range(0, len(all_ids), 50):
                chunk = all_ids[i:i+50]
                ids_str = ",".join(str(x) for x in chunk)
                r2 = await c.get(
                    f"{WB_BASE}/api/advert/v2/adverts",
                    headers=_h(),
                    params={"ids": ids_str}
                )
                log.info("advert/v2/adverts status=%s", r2.status_code)
                if r2.status_code == 200:
                    d = r2.json()
                    if isinstance(d, list):
                        result.extend(d)
                    elif isinstance(d, dict):
                        result.extend(d.get("adverts", []))
            return result
        except Exception as e:
            log.error("wb_get_campaigns: %s", e)
    return []


async def wb_action(campaign_id: int, action: str) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.get(
                f"{WB_BASE}/adv/v0/{action}",
                headers=_h(),
                params={"id": campaign_id}
            )
            log.info("wb_action %s id=%s status=%s body=%s", action, campaign_id, r.status_code, r.text[:100])
            return r.status_code == 200, r.text[:100]
        except Exception as e:
            log.error("wb_action %s: %s", action, e)
            return False, str(e)


def auth(update: Update) -> bool:
    return update.effective_user.id == OWNER_ID


def main_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Список кампаний", callback_data="list")]])


def camp_kb(cid: int, st: int):
    sched = schedules[cid]
    icon = "✅" if sched["enabled"] else "⏸"
    rows = []
    if st in (4, 11):
        rows.append([InlineKeyboardButton("▶️ Запустить", callback_data=f"start:{cid}")])
    if st == 9:
        rows.append([InlineKeyboardButton("⏸ Пауза", callback_data=f"pause:{cid}")])
    rows.append([InlineKeyboardButton(
        f"{icon} Расписание: {sched['on']:02d}:00–{sched['off']:02d}:00",
        callback_data=f"sched_menu:{cid}"
    )])
    rows.append([InlineKeyboardButton("« Назад", callback_data="list")])
    return InlineKeyboardMarkup(rows)


def sched_kb(cid: int):
    sched = schedules[cid]
    toggle = "⏸ Выключить" if sched["enabled"] else "▶️ Включить"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🕐 Изменить время", callback_data=f"sched_edit:{cid}")],
        [InlineKeyboardButton(toggle + " расписание", callback_data=f"sched_toggle:{cid}")],
        [InlineKeyboardButton("« К кампании", callback_data=f"camp:{cid}:0")],
    ])


async def render_list(send):
    campaigns = await wb_get_campaigns()
    if not campaigns:
        await send("😕 Кампании не найдены или ошибка API.\n\nПроверь WB_API_TOKEN.",
                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Повторить", callback_data="list")]]))
        return
    buttons = []
    for c in campaigns:
        cid = c.get("advertId", 0)
        name = c.get("name", f"Кампания {cid}")[:35]
        st = c.get("status", -1)
        buttons.append([InlineKeyboardButton(f"{STATUS.get(st,'?')}  {name}", callback_data=f"camp:{cid}:{st}")])
    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="list")])
    await send(f"<b>📋 Найдено кампаний: {len(campaigns)}</b>",
               parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text("👋 <b>WB Ad Manager</b>\n\nУправляю рекламными кампаниями Wildberries.",
                                    parse_mode="HTML", reply_markup=main_kb())


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    msg = await update.message.reply_text("⏳ Загружаю...")
    await render_list(msg.edit_text)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    q = update.callback_query
    d = q.data

    if d == "list":
        await q.answer()
        await q.edit_message_text("⏳ Загружаю...")
        await render_list(q.edit_message_text)

    elif d.startswith("camp:"):
        _, cid_s, st_s = d.split(":")
        cid, st = int(cid_s), int(st_s)
        await q.answer()
        camps = await wb_get_campaigns()
        camp = next((c for c in camps if c.get("advertId") == cid), None)
        if not camp:
            await q.edit_message_text("❌ Кампания не найдена.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="list")]]))
            return
        real_st = camp.get("status", st)
        sched = schedules[cid]
        s_icon = "✅ активно" if sched["enabled"] else "⏸ выключено"
        await q.edit_message_text(
            f"<b>{camp.get('name', cid)}</b>\n\n"
            f"🆔 ID: <code>{cid}</code>\n"
            f"📌 Статус: {STATUS.get(real_st, real_st)}\n"
            f"🏷 Тип: {TYPE_MAP.get(camp.get('type'), '—')}\n\n"
            f"⏰ <b>Расписание</b> ({s_icon}):\n"
            f"  🟢 {sched['on']:02d}:00 МСК — включать\n"
            f"  🔴 {sched['off']:02d}:00 МСК — выключать",
            parse_mode="HTML", reply_markup=camp_kb(cid, real_st)
        )

    elif d.startswith("start:"):
        cid = int(d.split(":")[1])
        await q.answer("⏳")
        ok, msg = await wb_action(cid, "start")
        now = datetime.now(TZ).strftime("%H:%M")
        await q.edit_message_text(
            f"{'🟢 Реклама запущена' if ok else '⚠️ Ошибка: ' + msg} в {now} МСК",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="list")]]))

    elif d.startswith("pause:"):
        cid = int(d.split(":")[1])
        await q.answer("⏳")
        ok, msg = await wb_action(cid, "pause")
        now = datetime.now(TZ).strftime("%H:%M")
        await q.edit_message_text(
            f"{'🔴 Реклама остановлена' if ok else '⚠️ Ошибка: ' + msg} в {now} МСК",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="list")]]))

    elif d.startswith("sched_menu:"):
        cid = int(d.split(":")[1])
        sched = schedules[cid]
        await q.answer()
        await q.edit_message_text(
            f"⏰ <b>Расписание</b> кампании <code>{cid}</code>\n\n"
            f"🟢 {sched['on']:02d}:00 МСК — включать\n"
            f"🔴 {sched['off']:02d}:00 МСК — выключать",
            parse_mode="HTML", reply_markup=sched_kb(cid))

    elif d.startswith("sched_toggle:"):
        cid = int(d.split(":")[1])
        schedules[cid]["enabled"] = not schedules[cid]["enabled"]
        sched = schedules[cid]
        await q.answer("✅ Включено" if sched["enabled"] else "⏸ Выключено")
        await q.edit_message_text(
            f"⏰ <b>Расписание</b> кампании <code>{cid}</code>\n\n"
            f"🟢 {sched['on']:02d}:00 МСК — включать\n"
            f"🔴 {sched['off']:02d}:00 МСК — выключать",
            parse_mode="HTML", reply_markup=sched_kb(cid))

    elif d.startswith("sched_edit:"):
        cid = int(d.split(":")[1])
        pending_schedule[update.effective_user.id] = cid
        await q.answer()
        await q.edit_message_text(
            "✏️ Введи время: <code>ЧАС_ВКЛ ЧАС_ВЫКЛ</code>\n\nПример: <code>9 22</code>",
            parse_mode="HTML")


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
        f"✅ Готово!\n🟢 {on_h:02d}:00 МСК — включать\n🔴 {off_h:02d}:00 МСК — выключать",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К списку", callback_data="list")]]))


async def auto_scheduler(app: Application):
    while True:
        await asyncio.sleep(60)
        now = datetime.now(TZ)
        if now.minute != 0:
            continue
        h = now.hour
        for cid, sched in list(schedules.items()):
            if not sched["enabled"]:
                continue
            if h == sched["on"]:
                ok, _ = await wb_action(cid, "start")
                if ok:
                    await app.bot.send_message(OWNER_ID,
                        f"🟢 Авторасписание: кампания <code>{cid}</code> запущена в {h:02d}:00 МСК",
                        parse_mode="HTML")
            elif h == sched["off"]:
                ok, _ = await wb_action(cid, "pause")
                if ok:
                    await app.bot.send_message(OWNER_ID,
                        f"🔴 Авторасписание: кампания <code>{cid}</code> остановлена в {h:02d}:00 МСК",
                        parse_mode="HTML")


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("list", "Список кампаний"),
    ])
    asyncio.create_task(auto_scheduler(app))


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
