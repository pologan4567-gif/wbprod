#!/usr/bin/env python3
"""
Запускается GitHub Actions cron — включает или останавливает все кампании из CAMPAIGN_IDS.
Использование: python cron_action.py start|stop
"""
import os, sys, httpx
from datetime import datetime
from zoneinfo import ZoneInfo

WB_TOKEN   = os.environ["WB_API_TOKEN"]
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_USER    = os.environ["ALLOWED_USER_ID"]
# CAMPAIGN_IDS — список ID через запятую: "12345,67890,11111"
RAW_IDS    = os.environ.get("CAMPAIGN_IDS", "")
CAMPAIGN_IDS = [int(x.strip()) for x in RAW_IDS.split(",") if x.strip().isdigit()]
TZ         = ZoneInfo("Europe/Moscow")
ACTION     = sys.argv[1] if len(sys.argv) > 1 else "start"


def tg(text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        httpx.post(url, json={"chat_id": TG_USER, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"TG error: {e}")


def wb_call(cid: int, action: str) -> tuple[bool, str]:
    url = f"https://advert-api.wb.ru/adv/v0/{action}?id={cid}"
    r = httpx.get(url, headers={"Authorization": WB_TOKEN}, timeout=15)
    return r.status_code == 200, r.text[:100]


if __name__ == "__main__":
    if not CAMPAIGN_IDS:
        print("ERROR: CAMPAIGN_IDS is empty. Set secret CAMPAIGN_IDS=12345,67890")
        sys.exit(1)

    now   = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    icon  = "🟢" if ACTION == "start" else "🔴"
    verb  = "запущена" if ACTION == "start" else "остановлена"

    results = []
    errors  = []
    for cid in CAMPAIGN_IDS:
        ok, msg = wb_call(cid, ACTION)
        if ok:
            results.append(cid)
            print(f"[OK] {ACTION} campaign {cid}")
        else:
            errors.append(f"{cid}: {msg}")
            print(f"[ERR] campaign {cid}: {msg}", file=sys.stderr)

    # Собрать уведомление
    lines = [f"{icon} <b>Реклама {verb}</b> в {now} МСК"]
    if results:
        ids_str = ", ".join(f"<code>{i}</code>" for i in results)
        lines.append(f"✅ Успешно: {ids_str}")
    if errors:
        lines.append(f"⚠️ Ошибки:\n" + "\n".join(errors))

    tg("\n".join(lines))

    if errors and not results:
        sys.exit(1)
