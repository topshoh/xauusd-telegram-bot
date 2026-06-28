"""
xauusd_cot_telegram_bot.py

Отправляет в Telegram еженедельную сводку по отчёту CFTC COT
(позиции крупных игроков на рынке золота). Предназначен для запуска
по пятницам через GitHub Actions, отдельно от ежечасного бота с ценой.

ВАЖНО: данные ниже (COT_DATA) — это не живой автоматический парсинг.
CFTC публикует сырой CSV без понятных названий колонок (все товары
в одном файле), надёжно парсить его на лету рискованно — легко тихо
сдвинуться на колонку и показывать неверные цифры. Поэтому цифры
обновляются вручную, когда ты просишь меня в чате "обнови дашборд"
(это автоматически включает и COT) — после чего нужно заново загрузить
этот файл на GitHub.
"""

import os
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# --- Часы, когда рынок золота закрыт (см. подробности в xauusd_telegram_bot.py) ---
TASHKENT_TZ = ZoneInfo("Asia/Tashkent")


def is_market_closed(now_tashkent: datetime) -> bool:
    """Закрыт: с субботы 02:00 до понедельника 03:00 по Ташкенту."""
    weekday = now_tashkent.weekday()
    hour = now_tashkent.hour
    if weekday == 5 and hour >= 2:
        return True
    if weekday == 6:
        return True
    if weekday == 0 and hour < 3:
        return True
    return False

# --- Обновляется вручную при каждом новом отчёте CFTC (по пятницам) ---
# Формат: (название, long, short, net, вердикт, пояснение)
COT_DATA = [
    {
        "name": "Producer/Merchant",
        "subtitle": "Золотодобытчики, хеджируют бизнес",
        "long": 13643,
        "short": 35041,
        "net": -21398,
        "verdict": "🟢 Без паники",
    },
    {
        "name": "Swap Dealers",
        "subtitle": "В основном банки",
        "long": 41702,
        "short": 217086,
        "net": -175384,
        "verdict": "🔴 Экстремально много шортов",
    },
    {
        "name": "Managed Money",
        "subtitle": "Хедж-фонды, \"умные деньги\"",
        "long": 123011,
        "short": 27118,
        "net": 95893,
        "verdict": "🟢 Без паники",
    },
]
COT_REPORT_DATE = "16 июня 2026 (опубликовано 19 июня)"

COT_TAKEAWAY = (
    "Банки держат экстремальный шорт — потенциальный риск short squeeze "
    "(резкий рост, если короткие позиции начнут закрываться). Управляемые "
    "деньги (хедж-фонды) уверенно в лонге, но не на экстремальном уровне."
)

WARNING_TEXT = (
    "Это официальные данные CFTC, а не прогноз. Источник: "
    "cftc.gov/dea/futures/other_sf.htm"
)


def format_number(n: int) -> str:
    sign = "+" if n > 0 else ""
    return f"{sign}{n:,}"


def build_message() -> str:
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y")

    lines = [
        "<b>XAUUSD — еженедельный отчёт CFTC COT</b>",
        f"Отправлено: {now}",
        f"Данные отчёта на: {COT_REPORT_DATE}",
        "",
    ]

    for group in COT_DATA:
        lines.append(f"<b>{group['name']}</b> ({group['subtitle']})")
        lines.append(
            f"Long: {group['long']:,} · Short: {group['short']:,} · "
            f"Net: {format_number(group['net'])}"
        )
        lines.append(f"{group['verdict']}")
        lines.append("")

    lines.append(f"<b>Итог:</b> {COT_TAKEAWAY}")
    lines.append("")
    lines.append(f"<i>{WARNING_TEXT}</i>")

    return "\n".join(lines)


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    response = requests.post(url, json=payload, timeout=10)
    response.raise_for_status()
    print("COT-сообщение отправлено успешно.")


def main() -> None:
    now_tashkent = datetime.now(TASHKENT_TZ)

    if is_market_closed(now_tashkent):
        print(
            f"Рынок золота закрыт (сейчас {now_tashkent.strftime('%A %H:%M')} "
            f"по Ташкенту). COT-сообщение не отправляется."
        )
        return

    message = build_message()
    send_telegram_message(message)


if __name__ == "__main__":
    main()
