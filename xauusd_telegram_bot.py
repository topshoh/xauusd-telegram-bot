import os
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TASHKENT_TZ = ZoneInfo("Asia/Tashkent")

def is_market_closed(now_tashkent: datetime) -> bool:
    weekday = now_tashkent.weekday()
    hour = now_tashkent.hour
    if weekday == 5 and hour >= 2: return True
    if weekday == 6: return True
    if weekday == 0 and hour < 3: return True
    return False

MACRO_SUMMARY = (
    "Ставка ФРС / Уорш: инфляция 'too high', но без сигнала по ставке в июле, outlook улучшился (менее ястребиный)\n"
    "ADP (июнь): +98K — слабо, ниже прогноза 113-118K (поддержало золото)\n"
    "Инфляция (Nowcast июль): CPI 3.52% / Core 2.81% — разрыв уменьшился\n"
    "Доллар (DXY): ~101.3, стабильно/слегка слабее\n"
    "Доходность 10Y: ~4.49% — высокая, продолжает давить\n"
    "Вердикт: отскок после слабых данных по занятости и нейтрально-мягкой речи Уорша. Ждём NFP. Боковик с краткосрочным bias вверх"
)
MACRO_SUMMARY_DATE = "2 июля 2026 (утро)"

WARNING_TEXT = "Это не торговый сигнал и не прогноз цены — только живые данные и контекст для самостоятельного анализа."

def fetch_gold_price():
    for attempt in range(2):
        try:
            response = requests.get("https://api.gold-api.com/price/XAU", timeout=15)
            response.raise_for_status()
            data = response.json()
            price = data.get("price") or data.get("price_usd")
            if price is not None:
                return float(price)
        except:
            pass
    try:
        response = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/GC=F", timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        data = response.json()
        price = data.get("chart", {}).get("result", [{}])[0].get("meta", {}).get("regularMarketPrice")
        if price is not None:
            return float(price)
    except:
        pass
    return None

def build_message(price):
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    if price is None:
        price_line = "⚠️ Не удалось получить цену. Проверь вручную на tradingview.com"
    else:
        price_line = f"💰 XAU/USD: <b>${price:,.2f}</b>"
    message = f"<b>XAUUSD — сводка</b>\n{now}\n\n{price_line}\n\n<b>Макро-контекст</b> (срез на {MACRO_SUMMARY_DATE}):\n{MACRO_SUMMARY}\n\n<i>{WARNING_TEXT}</i>"
    return message

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    requests.post(url, json=payload, timeout=10)

def main():
    now_tashkent = datetime.now(TASHKENT_TZ)
    if is_market_closed(now_tashkent):
        print("Рынок закрыт. Сообщение не отправляется.")
        return
    price = fetch_gold_price()
    message = build_message(price)
    send_telegram_message(message)

if __name__ == "__main__":
    main()
