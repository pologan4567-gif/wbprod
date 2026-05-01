#!/usr/bin/env python3
import os, logging, asyncio, json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import defaultdict

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wb")

TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_ID = int(os.environ["ALLOWED_USER_ID"])
WB_TOKEN = os.environ["WB_API_TOKEN"]
TZ       = ZoneInfo("Europe/Moscow")
WB_BASE  = "https://advert-api.wildberries.ru"
SCHED_FILE = Path("schedules.json")

STATUS = {
    -1: "🗑 Удалена", 4: "⏸ Готова", 7: "✅ Завершена",
     8: "🚫 Отменена", 9: "🟢 Активна", 11: "⏸ На паузе",
}
TYPE_MAP = {4:"Каталог", 5:"Карточка", 6:"Поиск", 7:"Главная", 8:"Авто"}
HIDDEN_STATUSES = {-1, 7, 8}

# pending actions: uid -> {"action": ..., "cid": ...}
pending = {}


# ─── Schedules persistence ────────────────────────────────────────────────────

def _default_sched():
    return {"on": 8, "off": 23, "enabled": True,
            "ctr_threshold": 0.0, "budget_alert": True}

def load_schedules() -> dict:
    if SCHED_FILE.exists():
        try:
            raw = json.loads(SCHED_FILE.read_text())
            return defaultdict(_default_sched, {int(k): v for k, v in raw.items()})
        except Exception as e:
            log.error("load_schedules: %s", e)
    return defaultdict(_default_sched)

def save_schedules():
    SCHED_FILE.write_text(json.dumps({str(k): v for k, v in schedules.items()}, ensure_ascii=False))

schedules = load_schedules()


# ─── WB API ──────────────────────────────────────────────────────────────────

def _h():
    return {"Authorization": WB_TOKEN}

async def wb_get_campaigns() -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(f"{WB_BASE}/adv/v1/promotion/count", headers=_h())
            log.info("count status=%s", r.status_code)
            if r.status_code != 200:
                return []
            all_ids = []
            for group in r.json().get("adverts", []):
                for adv in group.get("advert_list", []):
                    all_ids.append(adv["advertId"])
            if not all_ids:
                return []
            result = []
            for i in range(0, len(all_ids), 50):
                ids_str = ",".join(str(x) for x in all_ids[i:i+50])
                r2 = await c.get(f"{WB_BASE}/api/advert/v2/adverts",
                                 headers=_h(), params={"ids": ids_str})
                log.info("adverts status=%s body=%s", r2.status_code, r2.text[:500])
                if r2.status_code == 200:
                    d = r2.json()
                    result.extend(d if isinstance(d, list) else d.get("adverts", []))
            return result
        except Exception as e:
            log.error("wb_get_campaigns: %s", e)
    return []

async def wb_action(cid: int, action: str) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.get(f"{WB_BASE}/adv/v0/{action}", headers=_h(), params={"id": cid})
            log.info("action=%s id=%s status=%s", action, cid, r.status_code)
            return r.status_code == 200, r.text[:200]
        except Exception as e:
            return False, str(e)

async def wb_get_stats(cid: int) -> dict | None:
    """Статистика за сегодня"""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    async with httpx.AsyncClient(timeout=20) as c:
        try:
            r = await c.get(
                f"{WB_BASE}/adv/v2/fullstats",
                headers=_h(),
                params={"id": cid, "interval": f"{today};{today}"}
            )
            log.info("stats id=%s status=%s body=%s", cid, r.status_code, r.text[:300])
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            log.error("wb_get_stats: %s", e)
    return None

async def wb_get_bid(cid: int) -> int | None:
    """Текущая ставка кампании"""
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.get(f"{WB_BASE}/adv/v0/cpm", headers=_h(), params={"id": cid})
            log.info("bid id=%s status=%s body=%s", cid, r.status_code, r.text[:200])
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, list) and d:
                    return d[0].get("cpm")
                if isinstance(d, dict):
                    return d.get("cpm")
        except Exception as e:
            log.error("wb_get_bid: %s", e)
    return None

async def wb_set_bid(cid: int, bid: int, camp_type: int) -> tuple[bool, str]:
    """Установить ставку"""
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.post(
                f"{WB_BASE}/adv/v0/cpm",
                headers=_h(),
                json={"advertId": cid, "type": camp_type, "cpm": bid}
            )
            log.info("set_bid id=%s bid=%s status=%s", cid, bid, r.status_code)
            return r.status_code == 200, r.text[:200]
        except Exception as e:
            return False, str(e)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def get_name(c: dict, cid: int) -> str:
    for key in ("name", "campaignName", "advertName", "title"):
        val = c.get(key)
        if val and str(val).strip():
            return str(val).strip()[:35]
    return f"ID {cid}"

def auth(update: Update) -> bool:
    return update.effective_user.id == OWNER_ID

def main_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📋 Список кампаний", callback_data="list")]])


# ─── Keyboards ────────────────────────────────────────────────────────────────

def camp_kb(cid: int, st: int):
    sched = schedules[cid]
    rows = []
    if st in (4, 11):
        rows.append([InlineKeyboardButton("▶️ Запустить", callback_data=f"start:{cid}")])
    if st == 9:
        rows.append([InlineKeyboardButton("⏸ Пауза", callback_data=f"pause:{cid}")])
    rows.append([InlineKeyboardButton("📊 Статистика за сегодня", callback_data=f"stats:{cid}")])
    rows.append([InlineKeyboardButton("💰 Изменить ставку", callback_data=f"bid_ask:{cid}")])
    sched_icon = "✅" if sched["enabled"] else "⏸"
    rows.append([InlineKeyboardButton(
        f"{sched_icon} {sched['on']:02d}:00 вкл / {sched['off']:02d}:00 выкл — изменить",
        callback_data=f"sched_ask:{cid}"
    )])
    rows.append([InlineKeyboardButton("« Назад", callback_data="list")])
    return InlineKeyboardMarkup(rows)


# ─── Campaign card ────────────────────────────────────────────────────────────

async def show_camp(send, cid: int, camp: dict):
    st = camp.get("status", -1)
    sched = schedules[cid]
    s_icon = "✅" if sched["enabled"] else "⏸"
    name = get_name(camp, cid)
    bid = await wb_get_bid(cid)
    bid_str = f"{bid} ₽" if bid else "—"
    await send(
        f"<b>{name}</b>\n\n"
        f"🆔 ID: <code>{cid}</code>\n"
        f"📌 Статус: {STATUS.get(st, st)}\n"
        f"🏷 Тип: {TYPE_MAP.get(camp.get('type'), '—')}\n"
        f"💰 Ставка: <b>{bid_str}</b>\n\n"
        f"⏰ Расписание ({s_icon}):\n"
        f"  🟢 {sched['on']:02d}:00 МСК — включать\n"
        f"  🔴 {sched['off']:02d}:00 МСК — выключать",
        parse_mode="HTML",
        reply_markup=camp_kb(cid, st)
    )


# ─── List ─────────────────────────────────────────────────────────────────────

async def render_list(send):
    campaigns = await wb_get_campaigns()
    if not campaigns:
        await send("😕 Кампании не найдены или ошибка API.",
                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Повторить", callback_data="list")]]))
        return
    buttons = []
    for c in campaigns:
        st = c.get("status", -1)
        if st in HIDDEN_STATUSES:
            continue
        cid = c.get("advertId", 0)
        name = get_name(c, cid)
        buttons.append([InlineKeyboardButton(f"{STATUS.get(st,'?')}  {name}", callback_data=f"camp:{cid}:{st}")])
    if not buttons:
        await send("📭 Нет активных кампаний.",
                   reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить", callback_data="list")]]))
        return
    buttons.append([InlineKeyboardButton("🔄 Обновить", callback_data="list")])
    await send(f"<b>📋 Кампании: {len(buttons)-1}</b>",
               parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text(
        "👋 <b>WB Ad Manager</b>\n\nУправляю рекламными кампаниями Wildberries.",
        parse_mode="HTML", reply_markup=main_kb())

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    msg = await update.message.reply_text("⏳ Загружаю...")
    await render_list(msg.edit_text)

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    q = update.callback_query
    d = q.data
    uid = update.effective_user.id

    if d == "list":
        await q.answer()
        await q.edit_message_text("⏳ Загружаю...")
        await render_list(q.edit_message_text)

    elif d.startswith("camp:"):
        _, cid_s, _ = d.split(":")
        cid = int(cid_s)
        await q.answer()
        await q.edit_message_text("⏳ Загружаю...")
        camps = await wb_get_campaigns()
        camp = next((c for c in camps if c.get("advertId") == cid), None)
        if not camp:
            await q.edit_message_text("❌ Кампания не найдена.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data="list")]]))
            return
        await show_camp(q.edit_message_text, cid, camp)

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

    elif d.startswith("stats:"):
        cid = int(d.split(":")[1])
        await q.answer("⏳ Загружаю статистику...")
        stats = await wb_get_stats(cid)
        today = datetime.now(TZ).strftime("%d.%m.%Y")
        if stats:
            views  = stats.get("views", 0)
            clicks = stats.get("clicks", 0)
            spend  = stats.get("sum", 0)
            ctr    = round(clicks / views * 100, 2) if views else 0
            cpc    = round(spend / clicks, 2) if clicks else 0
            text = (
                f"📊 <b>Статистика за {today}</b>\n\n"
                f"👁 Показы: <b>{views:,}</b>\n"
                f"🖱 Клики: <b>{clicks:,}</b>\n"
                f"📈 CTR: <b>{ctr}%</b>\n"
                f"💰 Расход: <b>{spend} ₽</b>\n"
                f"💵 CPC: <b>{cpc} ₽</b>"
            )
        else:
            text = f"📊 Статистика за {today}\n\n😕 Данных нет или ошибка API"
        await q.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад", callback_data=f"camp:{cid}:0")]]))

    elif d.startswith("bid_ask:"):
        cid = int(d.split(":")[1])
        pending[uid] = {"action": "bid", "cid": cid}
        await q.answer()
        await q.edit_message_text(
            "💰 Введи новую ставку в рублях (целое число):\n\nПример: <code>150</code>",
            parse_mode="HTML")

    elif d.startswith("sched_ask:"):
        cid = int(d.split(":")[1])
        sched = schedules[cid]
        pending[uid] = {"action": "sched", "cid": cid}
        await q.answer()
        toggle = "⏸ Выключить" if sched["enabled"] else "▶️ Включить"
        await q.edit_message_text(
            f"⏰ <b>Расписание</b>\n\n"
            f"Сейчас: вкл <b>{sched['on']:02d}:00</b> / выкл <b>{sched['off']:02d}:00</b> МСК\n\n"
            f"Введи новое время:\n<code>ЧАС_ВКЛ ЧАС_ВЫКЛ</code>\n\nПример: <code>9 22</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle, callback_data=f"sched_toggle:{cid}")],
                [InlineKeyboardButton("« Назад", callback_data=f"camp:{cid}:0")],
            ]))

    elif d.startswith("sched_toggle:"):
        cid = int(d.split(":")[1])
        schedules[cid]["enabled"] = not schedules[cid]["enabled"]
        save_schedules()
        sched = schedules[cid]
        state = "✅ включено" if sched["enabled"] else "⏸ выключено"
        await q.answer(f"Расписание {state}")
        await q.edit_message_text(
            f"⏰ Расписание {state}\n\n"
            f"🟢 {sched['on']:02d}:00 МСК — включать\n"
            f"🔴 {sched['off']:02d}:00 МСК — выключать",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К кампании", callback_data=f"camp:{cid}:0")]]))


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    uid = update.effective_user.id
    if uid not in pending: return

    p = pending.pop(uid)
    action = p["action"]
    cid = p["cid"]
    text = update.message.text.strip()

    if action == "bid":
        try:
            bid = int(text)
            assert 50 <= bid <= 5000
        except:
            await update.message.reply_text("❌ Ставка должна быть числом от 50 до 5000 ₽")
            pending[uid] = p
            return
        # Получаем тип кампании
        camps = await wb_get_campaigns()
        camp = next((c for c in camps if c.get("advertId") == cid), None)
        camp_type = camp.get("type", 8) if camp else 8
        ok, msg = await wb_set_bid(cid, bid, camp_type)
        await update.message.reply_text(
            f"{'✅ Ставка установлена: ' + str(bid) + ' ₽' if ok else '⚠️ Ошибка: ' + msg}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К кампании", callback_data=f"camp:{cid}:0")]]))

    elif action == "sched":
        try:
            parts = text.split()
            on_h, off_h = int(parts[0]), int(parts[1])
            assert 0 <= on_h <= 23 and 0 <= off_h <= 23 and on_h != off_h
        except:
            await update.message.reply_text("❌ Формат: <code>9 22</code>", parse_mode="HTML")
            pending[uid] = p
            return
        schedules[cid]["on"] = on_h
        schedules[cid]["off"] = off_h
        save_schedules()
        await update.message.reply_text(
            f"✅ Расписание сохранено!\n🟢 {on_h:02d}:00 МСК — включать\n🔴 {off_h:02d}:00 МСК — выключать",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К кампании", callback_data=f"camp:{cid}:0")]]))


# ─── Auto scheduler ───────────────────────────────────────────────────────────

async def auto_scheduler(app: Application):
    last_report_day = -1
    while True:
        await asyncio.sleep(60)
        now = datetime.now(TZ)
        h, m = now.hour, now.minute
        if m != 0:
            continue

        # Авторасписание
        for cid, sched in list(schedules.items()):
            if not sched.get("enabled", True):
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

        # Дневной отчёт в 23:00
        if h == 23 and now.day != last_report_day:
            last_report_day = now.day
            camps = await wb_get_campaigns()
            active = [c for c in camps if c.get("status") in (9, 11)]
            if active:
                lines = [f"📊 <b>Дневной отчёт {now.strftime('%d.%m.%Y')}</b>\n"]
                total_spend = 0
                for camp in active:
                    cid = camp.get("advertId", 0)
                    stats = await wb_get_stats(cid)
                    name = get_name(camp, cid)
                    if stats:
                        spend = stats.get("sum", 0)
                        clicks = stats.get("clicks", 0)
                        views = stats.get("views", 0)
                        ctr = round(clicks / views * 100, 2) if views else 0
                        total_spend += spend
                        lines.append(f"▪️ <b>{name}</b>\n   👁 {views:,} | 🖱 {clicks:,} | CTR {ctr}% | 💰 {spend} ₽")
                    else:
                        lines.append(f"▪️ <b>{name}</b> — нет данных")
                lines.append(f"\n💳 <b>Итого расход: {total_spend} ₽</b>")
                await app.bot.send_message(OWNER_ID, "\n".join(lines), parse_mode="HTML")


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
