import os
import requests

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("❌ Секреты не переданы")
        return False
    
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"📤 Статус: {r.status_code}")
        print(f"📤 Ответ: {r.text}")  # <-- теперь видно реальный ответ
        
        if r.status_code == 200 and r.json().get("ok"):
            print("✅ Уведомление отправлено")
            return True
        else:
            print("❌ Ошибка от Telegram")
            return False
    except Exception as e:
        print(f"❌ Исключение: {e}")
        return False

if __name__ == "__main__":
    send_telegram("🔔 Тестовое сообщение от GitHub Actions через @gold_info_view_bot")
