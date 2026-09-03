import os
import requests
from datetime import datetime

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def get_last_commit():
    url = "https://api.github.com/repos/topshoh/xauusd-telegram-bot/commits/main"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]["sha"], data[0]["commit"]["message"], data[0]["commit"]["author"]["date"]
        return "unknown", "unknown", "unknown"
    except Exception as e:
        print(f"Error getting commit: {e}")
        return "unknown", "unknown", "unknown"

def get_dashboard_meta():
    url = "https://api.github.com/repos/topshoh/xauusd-telegram-bot/contents/dashboard.html"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        content = requests.get(r.json()["download_url"], timeout=10).text
        for line in content.split("\n"):
            if "📅 Последнее полное обновление дашборда" in line:
                return line.strip()
        return "📅 Дата обновления не найдена"
    except Exception as e:
        print(f"Error getting dashboard meta: {e}")
        return "📅 Дата обновления не найдена"

def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("❌ TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не установлены")
        return False
    
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("✅ Уведомление отправлено")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        print(f"Response: {r.text if 'r' in locals() else 'No response'}")
        return False

if __name__ == "__main__":
    sha, msg, date = get_last_commit()
    meta = get_dashboard_meta()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    text = f"""
🔔 <b>Дашборд XAUUSD обновлён</b>

📅 Время: {now}
📦 Коммит: {sha[:7] if sha != 'unknown' else 'unknown'}
📝 Сообщение: {msg}

📊 {meta}

🌐 Открыть дашборд:
<a href="https://topshoh.github.io/xauusd-telegram-bot/dashboard.html">https://topshoh.github.io/xauusd-telegram-bot/dashboard.html</a>

⚠️ Это не финансовый совет.
"""
    send_telegram(text)
