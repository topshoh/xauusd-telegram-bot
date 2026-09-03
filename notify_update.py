import os
import requests

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_test_message():
    print(f"🔍 TELEGRAM_TOKEN: {TOKEN[:5]}...{TOKEN[-5:] if TOKEN else 'НЕТ'}")
    print(f"🔍 TELEGRAM_CHAT_ID: {CHAT_ID}")
    
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": "🔔 Это ТЕСТОВОЕ сообщение от GitHub Actions через @gold_info_view_bot. Если вы это видите — всё работает!",
        "parse_mode": "HTML"
    }
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"📤 Status code: {r.status_code}")
        print(f"📤 Response: {r.text}")
        if r.status_code == 200:
            print("✅ Уведомление отправлено")
        else:
            print("❌ Ошибка при отправке")
    except Exception as e:
        print(f"❌ Исключение: {e}")

if __name__ == "__main__":
    send_test_message()
