import os
import requests
from datetime import datetime

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def get_last_commit():
    url = "https://api.github.com/repos/topshoh/xauusd-telegram-bot/commits/main"
    r = requests.get(url)
    data = r.json()
    return data["sha"], data["commit"]["message"], data["commit"]["author"]["date"]

def get_dashboard_meta():
    url = "https://api.github.com/repos/topshoh/xauusd-telegram-bot/contents/dashboard.html"
    r = requests.get(url)
    content = requests.get(r.json()["download_url"]).text
    for line in content.split("\n"):
        if "📅 Последнее полное обновление дашборда" in line:
            return line.strip()
    return "Дата не найдена"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    requests.post(url, json=payload)

if __name__ == "__main__":
    sha, msg, date = get_last_commit()
    meta = get_dashboard_meta()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    text = f"""
🔔 <b>Дашборд XAUUSD обновлён</b>

📅 Время: {now}
📦 Коммит: {sha[:7]}
📝 Сообщение: {msg}

📊 {meta}

🌐 Открыть дашборд:
<a href="https://topshoh.github.io/xauusd-telegram-bot/dashboard.html">https://topshoh.github.io/xauusd-telegram-bot/dashboard.html</a>

⚠️ Это не финансовый совет.
"""
    send_telegram(text)
    print("✅ Уведомление отправлено")
