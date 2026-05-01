#!/usr/bin/env python3
import os, logging, asyncio, json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import defaultdict

import httpx
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("wb")

TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_ID = int(os.environ["ALLOWED_USER_ID"])
WB_TOKEN = os.environ["WB_API_TOKEN"]
TZ = ZoneInfo("Europe/Moscow")
WB_BASE = "https://advert-api.wildberries.ru"
SCHED_FILE = Path("schedules.json")

STATUS = {
    -1: "🗑 Удалена", 4: "⏸ Готова", 7: "✅ Завершена",
    8: "🚫 Отменена", 9: "🟢 Активна", 11: "⏸ На паузе",
}
TYPE_MAP = {4: "Каталог", 5: "Карточка", 6: "Поиск", 7: "Главная", 8: "Авто"}
HIDDEN_STATUSES = {-1, 7, 8}

pending = {}

MENU_CAMPAIGNS = "📋 Кампании"
MENU_REPORT = "📊 Отчёт"
MENU_REFRESH = "🔄 Обновить"
MENU_SETTINGS = "⚙️ Настройки"


def _default_sched():
    return {"on": 8, "off": 23, "enabled": True, "ctr_threshold": 0.0, "budget_alert": True}


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
                ids_str = ",".join(str(x) for x in all_ids[i:i + 50])
                r2 = await c.get(f"{WB_BASE}/api/advert/v2/adverts", headers=_h(), params={"ids": ids_str})
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


def get_name(c: dict, cid: int) -> str:
    for key in ("name", "campaignName", "advertName", "title"):
        val = c.get(key)
        if val and str(val).strip():
            return str(val).strip()[:35]
    return f"ID {cid}"


def auth(update: Update) -> bool:
    return update.effective_user.id == OWNER_ID


def status_badge(st: int) -> str:
    return STATUS.get(st, f"Статус {st}")


def fmt_money(value) -> str:
    if value in (None, ""):
        return "—"
    return f"{value} ₽"


def app_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(MENU_CAMPAIGNS), KeyboardButton(MENU_REPORT)],
            [KeyboardButton(MENU_REFRESH), KeyboardButton(MENU_SETTINGS)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери раздел"
    )


def main_inline_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Открыть кампании", callback_data="list")],
        [InlineKeyboardButton("📊 Дневной отчёт", callback_data="daily_report")],
    ])


def camp_kb(cid: int, st: int):
    sched = schedules[cid]
    rows = []
    if st in (4, 11):
        rows.append([InlineKeyboardButton("▶️ Запустить", callback_data=f"confirm:start:{cid}")])
    if st == 9:
        rows.append([InlineKeyboardButton("⏸ Поставить на паузу", callback_data=f"confirm:pause:{cid}")])
    rows.append([InlineKeyboardButton("📊 Статистика за сегодня", callback_data=f"stats:{cid}")])
    rows.append([InlineKeyboardButton("💰 Изменить ставку", callback_data=f"bid_ask:{cid}")])
    sched_icon = "✅" if sched["enabled"] else "⏸"
    rows.append([InlineKeyboardButton(
        f"{sched_icon} Расписание {sched['on']:02d}:00–{sched['off']:02d}:00",
        callback_data=f"sched_ask:{cid}"
    )])
    rows.append([InlineKeyboardButton("« К списку кампаний", callback_data="list")])
    return InlineKeyboardMarkup(rows)


def confirm_kb(cid: int, action: str):
    label = "Да, запустить" if action == "start" else "Да, поставить на паузу"
    icon = "✅" if action == "start" else "⏸"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{icon} {label}", callback_data=f"do:{action}:{cid}")],
        [InlineKeyboardButton("↩️ Отмена", callback_data=f"camp:{cid}:0")],
    ])


def build_card_text(cid: int, camp: dict, bid: int | None) -> str:
    st = camp.get("status", -1)
    sched = schedules[cid]
    sched_state = "включено" if sched["enabled"] else "выключено"
    name = get_name(camp, cid)
    return (
        f"📦 <b>{name}</b>\n\n"
        f"🆔 ID: <code>{cid}</code>\n"
        f"📌 Статус: <b>{status_badge(st)}</b>\n"
        f"🏷 Тип: <b>{TYPE_MAP.get(camp.get('type'), '—')}</b>\n"
        f"💰 Ставка: <b>{fmt_money(bid)}</b>\n\n"
        f"⏰ Расписание: <b>{sched_state}</b>\n"
        f"🟢 Включать: <b>{sched['on']:02d}:00 МСК</b>\n"
        f"🔴 Выключать: <b>{sched['off']:02d}:00 МСК</b>"
    )


async def show_camp(send, cid: int, camp: dict):
    bid = await wb_get_bid(cid)
    await send(build_card_text(cid, camp, bid), parse_mode="HTML", reply_markup=camp_kb(cid, camp.get("status", -1)))


async def render_list(send):
    campaigns = await wb_get_campaigns()
    if not campaigns:
        await send(
            "😕 Не удалось получить кампании.\nПроверь токен WB или повтори позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Повторить", callback_data="list")]])
        )
        return
    buttons = []
    visible = 0
    for c in campaigns:
        st = c.get("status", -1)
        if st in HIDDEN_STATUSES:
            continue
        cid = c.get("advertId", 0)
        name = get_name(c, cid)
        short_name = name[:24] + "…" if len(name) > 24 else name
        buttons.append([
            InlineKeyboardButton(
                f"{status_badge(st)} • {short_name}",
                callback_data=f"camp:{cid}:{st}"
            )
        ])
        visible += 1
    if not buttons:
        await send(
            "📭 Сейчас нет активных или доступных кампаний.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить", callback_data="list")]])
        )
        return
    buttons.append([InlineKeyboardButton("🔄 Обновить список", callback_data="list")])
    await send(f"📋 <b>Кампании</b>\n\nНайдено: <b>{visible}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def render_daily_report(send):
    camps = await wb_get_campaigns()
    active = [c for c in camps if c.get("status") in (9, 11, 4)]
    today = datetime.now(TZ).strftime("%d.%m.%Y")
    if not active:
        await send(f"📊 <b>Дневной отчёт за {today}</b>\n\nНет кампаний для отчёта.", parse_mode="HTML")
        return
    lines = [f"📊 <b>Дневной отчёт за {today}</b>"]
    total_spend = 0
    for camp in active[:10]:
        cid = camp.get("advertId", 0)
        stats = await wb_get_stats(cid)
        name = get_name(camp, cid)
        if stats:
            spend = stats.get("sum", 0)
            clicks = stats.get("clicks", 0)
            views = stats.get("views", 0)
            ctr = round(clicks / views * 100, 2) if views else 0
            total_spend += spend
            lines.append(f"\n▪️ <b>{name}</b>\n👁 {views:,} | 🖱 {clicks:,} | CTR {ctr}% | 💰 {spend} ₽")
        else:
            lines.append(f"\n▪️ <b>{name}</b>\nНет данных за сегодня")
    lines.append(f"\n\n💳 <b>Итого расход: {total_spend} ₽</b>")
    await send("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 К кампаниям", callback_data="list")]]))


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    await update.message.reply_text(
        "👋 <b>WB Ad Manager</b>\n\n"
        "Удобное управление рекламой Wildberries прямо в Telegram.\n"
        "Выбери раздел в меню ниже или нажми кнопку.",
        parse_mode="HTML",
        reply_markup=app_menu()
    )
    await update.message.reply_text(
        "Главный экран готов.",
        reply_markup=main_inline_kb()
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    msg = await update.message.reply_text("⏳ Загружаю список кампаний...", reply_markup=app_menu())
    await render_list(msg.edit_text)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return
    q = update.callback_query
    d = q.data
    uid = update.effective_user.id

    if d == "list":
        await q.answer()
        await q.edit_message_text("⏳ Загружаю список кампаний...")
        await render_list(q.edit_message_text)

    elif d == "daily_report":
        await q.answer("⏳ Готовлю отчёт...")
        await q.edit_message_text("⏳ Собираю данные для отчёта...")
        await render_daily_report(q.edit_message_text)

    elif d.startswith("camp:"):
        _, cid_s, _ = d.split(":")
        cid = int(cid_s)
        await q.answer()
        await q.edit_message_text("⏳ Открываю карточку кампании...")
        camps = await wb_get_campaigns()
        camp = next((c for c in camps if c.get("advertId") == cid), None)
        if not camp:
            await q.edit_message_text(
                "❌ Кампания не найдена.\nВозможно, она уже завершена или недоступна.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад к списку", callback_data="list")]])
            )
            return
        await show_camp(q.edit_message_text, cid, camp)

    elif d.startswith("confirm:"):
        _, action, cid_s = d.split(":")
        cid = int(cid_s)
        camps = await wb_get_campaigns()
        camp = next((c for c in camps if c.get("advertId") == cid), None)
        name = get_name(camp, cid) if camp else f"ID {cid}"
        action_text = "запустить" if action == "start" else "поставить на паузу"
        await q.answer()
        await q.edit_message_text(
            f"⚠️ <b>Подтверждение действия</b>\n\n"
            f"Кампания: <b>{name}</b>\n"
            f"ID: <code>{cid}</code>\n\n"
            f"Ты точно хочешь <b>{action_text}</b> эту кампанию?",
            parse_mode="HTML",
            reply_markup=confirm_kb(cid, action)
        )

    elif d.startswith("do:"):
        _, action, cid_s = d.split(":")
        cid = int(cid_s)
        await q.answer("⏳ Выполняю...")
        ok, msg = await wb_action(cid, action)
        now = datetime.now(TZ).strftime("%H:%M")
        success_text = "🟢 Кампания запущена" if action == "start" else "⏸ Кампания поставлена на паузу"
        fail_text = "запуска" if action == "start" else "постановки на паузу"
        await q.edit_message_text(
            f"{success_text if ok else '⚠️ Ошибка ' + fail_text + ': ' + msg}\n\n"
            f"Время: <b>{now} МСК</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Вернуться к кампании", callback_data=f"camp:{cid}:0")],
                [InlineKeyboardButton("📋 К списку кампаний", callback_data="list")],
            ])
        )

    elif d.startswith("stats:"):
        cid = int(d.split(":")[1])
        await q.answer("⏳ Загружаю статистику...")
        stats = await wb_get_stats(cid)
        today = datetime.now(TZ).strftime("%d.%m.%Y")
        if stats:
            views = stats.get("views", 0)
            clicks = stats.get("clicks", 0)
            spend = stats.get("sum", 0)
            ctr = round(clicks / views * 100, 2) if views else 0
            cpc = round(spend / clicks, 2) if clicks else 0
            text = (
                f"📊 <b>Статистика за {today}</b>\n\n"
                f"👁 Показы: <b>{views:,}</b>\n"
                f"🖱 Клики: <b>{clicks:,}</b>\n"
                f"📈 CTR: <b>{ctr}%</b>\n"
                f"💰 Расход: <b>{spend} ₽</b>\n"
                f"💵 CPC: <b>{cpc} ₽</b>"
            )
        else:
            text = f"📊 <b>Статистика за {today}</b>\n\n😕 Данных нет или произошла ошибка API."
        await q.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К кампании", callback_data=f"camp:{cid}:0")]])
        )

    elif d.startswith("bid_ask:"):
        cid = int(d.split(":")[1])
        pending[uid] = {"action": "bid", "cid": cid}
        await q.answer()
        await q.edit_message_text(
            "💰 <b>Изменение ставки</b>\n\n"
            "Введи новую ставку в рублях целым числом.\n"
            "Пример: <code>150</code>",
            parse_mode="HTML"
        )

    elif d.startswith("sched_ask:"):
        cid = int(d.split(":")[1])
        sched = schedules[cid]
        pending[uid] = {"action": "sched", "cid": cid}
        await q.answer()
        toggle = "⏸ Выключить расписание" if sched["enabled"] else "▶️ Включить расписание"
        await q.edit_message_text(
            f"⏰ <b>Настройка расписания</b>\n\n"
            f"Сейчас: <b>{sched['on']:02d}:00</b> включение / <b>{sched['off']:02d}:00</b> выключение\n\n"
            f"Введи два часа через пробел:\n<code>9 22</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(toggle, callback_data=f"sched_toggle:{cid}")],
                [InlineKeyboardButton("« К кампании", callback_data=f"camp:{cid}:0")],
            ])
        )

    elif d.startswith("sched_toggle:"):
        cid = int(d.split(":")[1])
        schedules[cid]["enabled"] = not schedules[cid]["enabled"]
        save_schedules()
        sched = schedules[cid]
        state = "✅ включено" if sched["enabled"] else "⏸ выключено"
        await q.answer(f"Расписание: {state}")
        await q.edit_message_text(
            f"⏰ <b>Расписание {state}</b>\n\n"
            f"🟢 Включать: <b>{sched['on']:02d}:00 МСК</b>\n"
            f"🔴 Выключать: <b>{sched['off']:02d}:00 МСК</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К кампании", callback_data=f"camp:{cid}:0")]])
        )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update):
        return

    text = update.message.text.strip()
    uid = update.effective_user.id

    if text == MENU_CAMPAIGNS:
        msg = await update.message.reply_text("⏳ Загружаю список кампаний...", reply_markup=app_menu())
        await render_list(msg.edit_text)
        return
    if text == MENU_REPORT:
        msg = await update.message.reply_text("⏳ Собираю дневной отчёт...", reply_markup=app_menu())
        await render_daily_report(msg.edit_text)
        return
    if text == MENU_REFRESH:
        msg = await update.message.reply_text("🔄 Обновляю данные...", reply_markup=app_menu())
        await render_list(msg.edit_text)
        return
    if text == MENU_SETTINGS:
        await update.message.reply_text(
            "⚙️ <b>Настройки</b>\n\n"
            "Пока здесь только базовый экран.\n"
            "Следующим этапом сюда можно вынести фильтры, лимиты и уведомления.",
            parse_mode="HTML",
            reply_markup=app_menu()
        )
        return

    if uid not in pending:
        await update.message.reply_text(
            "Не понял действие. Используй меню ниже или кнопки в сообщениях.",
            reply_markup=app_menu()
        )
        return

    p = pending.pop(uid)
    action = p["action"]
    cid = p["cid"]

    if action == "bid":
        try:
            bid = int(text)
            assert 50 <= bid <= 5000
        except Exception:
            await update.message.reply_text("❌ Ставка должна быть целым числом от 50 до 5000 ₽", reply_markup=app_menu())
            pending[uid] = p
            return
        camps = await wb_get_campaigns()
        camp = next((c for c in camps if c.get("advertId") == cid), None)
        camp_type = camp.get("type", 8) if camp else 8
        ok, msg = await wb_set_bid(cid, bid, camp_type)
        await update.message.reply_text(
            f"{'✅ Ставка обновлена: ' + str(bid) + ' ₽' if ok else '⚠️ Ошибка изменения ставки: ' + msg}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К кампании", callback_data=f"camp:{cid}:0")]])
        )
        return

    if action == "sched":
        try:
            parts = text.split()
            on_h, off_h = int(parts[0]), int(parts[1])
            assert 0 <= on_h <= 23 and 0 <= off_h <= 23 and on_h != off_h
        except Exception:
            await update.message.reply_text("❌ Формат времени: <code>9 22</code>", parse_mode="HTML", reply_markup=app_menu())
            pending[uid] = p
            return
        schedules[cid]["on"] = on_h
        schedules[cid]["off"] = off_h
        save_schedules()
        await update.message.reply_text(
            f"✅ Расписание сохранено\n\n🟢 Включать: <b>{on_h:02d}:00 МСК</b>\n🔴 Выключать: <b>{off_h:02d}:00 МСК</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К кампании", callback_data=f"camp:{cid}:0")]])
        )


async def auto_scheduler(app: Application):
    last_report_day = -1
    while True:
        await asyncio.sleep(60)
        now = datetime.now(TZ)
        h, m = now.hour, now.minute
        if m != 0:
            continue

        for cid, sched in list(schedules.items()):
            if not sched.get("enabled", True):
                continue
            if h == sched["on"]:
                ok, _ = await wb_action(cid, "start")
                if ok:
                    await app.bot.send_message(
                        OWNER_ID,
                        f"🟢 Авторасписание\n\nКампания <code>{cid}</code> запущена в <b>{h:02d}:00 МСК</b>",
                        parse_mode="HTML"
                    )
            elif h == sched["off"]:
                ok, _ = await wb_action(cid, "pause")
                if ok:
                    await app.bot.send_message(
                        OWNER_ID,
                        f"🔴 Авторасписание\n\nКампания <code>{cid}</code> остановлена в <b>{h:02d}:00 МСК</b>",
                        parse_mode="HTML"
                    )

        if h == 23 and now.day != last_report_day:
            last_report_day = now.day
            camps = await wb_get_campaigns()
            active = [c for c in camps if c.get("status") in (9, 11)]
            if active:
                lines = [f"📊 <b>Дневной отчёт {now.strftime('%d.%m.%Y')}</b>"]
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
                        lines.append(f"\n▪️ <b>{name}</b>\n👁 {views:,} | 🖱 {clicks:,} | CTR {ctr}% | 💰 {spend} ₽")
                    else:
                        lines.append(f"\n▪️ <b>{name}</b>\nНет данных")
                lines.append(f"\n\n💳 <b>Итого расход: {total_spend} ₽</b>")
                await app.bot.send_message(OWNER_ID, "\n".join(lines), parse_mode="HTML")


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Главный экран"),
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
