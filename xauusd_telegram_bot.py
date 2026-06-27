"""
xauusd_telegram_bot.py

Скрипт берёт живую цену золота (gold-api.com) и отправляет короткую
сводку в Telegram-чат. Предназначен для запуска по расписанию через
GitHub Actions — то есть работает даже когда твой компьютер выключен
и дашборд в браузере закрыт.

Что НЕ делает этот скрипт (важно понимать):
- Не ищет свежие новости/макрофакторы сам — для этого нужен я (Claude) в
  чате. Этот скрипт отправляет только живую цену + последний срез
  макро-вывода, который ты вручную обновляешь в этом файле, когда просишь
  меня "обнови дашборд".
- Не даёт торговых сигналов и не предсказывает цену — см. WARNING_TEXT ниже.
"""

import os
import requests
from datetime import datetime, timezone

# --- Настройки из переменных окружения (задаются в GitHub Secrets, см. README) ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# --- Этот блок обновляешь вручную, когда просишь меня "обнови дашборд" ---
# Просто попроси меня в чате: "обнови macro_summary для телеграм-бота" —
# и я перепишу текст ниже под актуальную картину.
MACRO_SUMMARY = (
    "Ставка ФРС: ястребиный тон, вероятность повышения в декабре ~80% (давит на золото)\n"
    "Инфляция (PCE): 4.1%, в рамках ожиданий, 2-й день отскока (поддержало золото)\n"
    "Доллар (DXY): ослаб от годовых максимумов (поддержало золото)\n"
    "Геополитика (Иран): инцидент в Ормузском проливе несмотря на перемирие (неопределённость)\n"
    "Неделя в целом: 4-е подряд недельное снижение (-3%)\n"
    "Вердикт: боковик, не чёткий long и не чёткий short"
)
MACRO_SUMMARY_DATE = "27 июня 2026 (вечер)"

WARNING_TEXT = (
    "Это не торговый сигнал и не прогноз цены — только живые данные и "
    "контекст для самостоятельного анализа."
)


def fetch_gold_price() -> float | None:
    """
    Получить текущую цену золота. Сначала пробует основной источник
    (gold-api.com), при сбое — резервный (Yahoo Finance, без API-ключа).
    Делает до 2 попыток на каждый источник, чтобы единичный сетевой
    сбой не оставлял сообщение без цены.
    """
    # --- Источник 1: gold-api.com ---
    for attempt in range(2):
        try:
            response = requests.get(
                "https://api.gold-api.com/price/XAU", timeout=15
            )
            response.raise_for_status()
            data = response.json()
            price = data.get("price") or data.get("price_usd")
            if price is not None:
                return float(price)
            print(f"gold-api.com вернул ответ без цены: {data}")
        except Exception as exc:
            print(f"Попытка {attempt + 1}, gold-api.com: {exc}")

    # --- Источник 2 (резервный): Yahoo Finance, фьючерс на золото (GC=F) ---
    try:
        response = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        data = response.json()
        price = (
            data.get("chart", {})
            .get("result", [{}])[0]
            .get("meta", {})
            .get("regularMarketPrice")
        )
        if price is not None:
            return float(price)
        print(f"Yahoo Finance вернул ответ без цены: {data}")
    except Exception as exc:
        print(f"Резервный источник (Yahoo Finance) тоже не сработал: {exc}")

    return None


def build_message(price: float | None) -> str:
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    if price is None:
        price_line = (
            "⚠️ Не удалось получить цену из обоих источников "
            "(gold-api.com и Yahoo Finance). Проверь цену вручную, "
            "например на tradingview.com или investing.com."
        )
    else:
        price_line = f"💰 XAU/USD: <b>${price:,.2f}</b>"

    message = (
        f"<b>XAUUSD — сводка</b>\n"
        f"{now}\n\n"
        f"{price_line}\n\n"
        f"<b>Макро-контекст</b> (срез на {MACRO_SUMMARY_DATE}):\n"
        f"{MACRO_SUMMARY}\n\n"
        f"<i>{WARNING_TEXT}</i>"
    )
    return message


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
    print("Сообщение отправлено успешно.")


def main() -> None:
    price = fetch_gold_price()
    message = build_message(price)
    send_telegram_message(message)


if __name__ == "__main__":
    main()
