"""
xauusd_alerts_bot.py

Третий Telegram-бот в этой системе. Делает две вещи:

1. Предупреждает за 1 час и за 30 минут до выхода важных экономических
   событий по USD (CPI, PCE, NFP, заседания ФРС и т.д.) — с контекстом
   для самостоятельного решения, не готовым сигналом.

2. Следит за резкими движениями цены золота вне расписания (сравнивает
   с ценой из предыдущего запуска) и шлёт внеплановый алерт, если
   движение превышает порог.

Предназначен для запуска через GitHub Actions каждые ~15-30 минут.

ВАЖНО — что этот скрипт умеет и не умеет:
- Календарь событий берётся через JBlanked Calendar API (живые данные)
- Cleveland Fed Nowcasting для CPI/PCE — статичные цифры, обновляются
  вручную (как MACRO_SUMMARY в почасовом боте), не парсятся автоматически
  в этой версии
- Это НЕ торговый сигнал. Видно явно в каждом сообщении.
"""

import os
import json
import base64
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# --- Настройки из переменных окружения ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN_FOR_STATE")  # для хранения состояния между запусками
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")  # автоматически доступно в GitHub Actions

STATE_FILE_PATH = "alerts_state.json"  # хранится в самом репозитории

TASHKENT_TZ = ZoneInfo("Asia/Tashkent")

# --- Cleveland Fed Inflation Nowcasting — обновляется вручную при "обнови дашборд" ---
NOWCAST_DATE = "26 июня 2026"
NOWCAST_DATA = {
    "CPI": 3.96,
    "Core CPI": 2.85,
    "PCE": 3.90,
    "Core PCE": 3.43,
}

# --- Текущий контекст рынка — обновляется вручную, синхронно с дашбордом ---
CURRENT_CONTEXT = (
    "Доминирующий фактор: ФРС держит ястребиный тон, рынок закладывает ~80% "
    "вероятность повышения ставки в декабре. Managed Money (хедж-фонды) уверенно "
    "в лонге по золоту, банки (Swap Dealers) держат экстремальный шорт — риск "
    "short squeeze при росте цены. Неделя в целом: золото в боковике после "
    "4 недель снижения."
)

# --- Какие события считаем важными для золота (фильтр по названию) ---
HIGH_IMPACT_KEYWORDS = [
    "CPI", "PCE", "Non-Farm", "NFP", "Federal Funds Rate", "FOMC",
    "Unemployment Rate", "Interest Rate Decision", "GDP",
]

PRICE_SPIKE_THRESHOLD_PERCENT = 0.4  # порог для алерта на резкое движение


# ============================================================
# Хранение состояния между запусками (через коммит в репозиторий)
# ============================================================

def load_state() -> dict:
    """
    Загружает состояние (последняя известная цена, список уже отправленных
    предупреждений) из файла в репозитории. Если файла нет — возвращает
    пустое состояние.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("GITHUB_TOKEN_FOR_STATE не задан — состояние не сохраняется между запусками.")
        return {"last_price": None, "last_price_time": None, "sent_warnings": []}

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 404:
            print("Файл состояния пока не существует — создаём с нуля.")
            return {"last_price": None, "last_price_time": None, "sent_warnings": [], "_sha": None}
        response.raise_for_status()
        data = response.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        state = json.loads(content)
        state["_sha"] = data["sha"]
        return state
    except Exception as exc:
        print(f"Не удалось загрузить состояние: {exc}")
        return {"last_price": None, "last_price_time": None, "sent_warnings": [], "_sha": None}


def save_state(state: dict) -> None:
    """Сохраняет состояние обратно в репозиторий через GitHub API."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return

    sha = state.pop("_sha", None)
    content_str = json.dumps(state, ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "message": "Обновление состояния алерт-бота (автоматический коммит)",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    try:
        response = requests.put(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        print("Состояние успешно сохранено.")
    except Exception as exc:
        print(f"Не удалось сохранить состояние: {exc}")


# ============================================================
# Проверка выходных (рынок закрыт)
# ============================================================

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


# ============================================================
# Получение цены золота (тот же подход, что в почасовом боте)
# ============================================================

def fetch_gold_price() -> float | None:
    for attempt in range(2):
        try:
            response = requests.get("https://api.gold-api.com/price/XAU", timeout=15)
            response.raise_for_status()
            data = response.json()
            price = data.get("price") or data.get("price_usd")
            if price is not None:
                return float(price)
        except Exception as exc:
            print(f"Попытка {attempt + 1}, gold-api.com: {exc}")

    try:
        response = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        data = response.json()
        price = (
            data.get("chart", {}).get("result", [{}])[0]
            .get("meta", {}).get("regularMarketPrice")
        )
        if price is not None:
            return float(price)
    except Exception as exc:
        print(f"Резервный источник тоже не сработал: {exc}")

    return None


# ============================================================
# Получение календаря событий через JBlanked API
# ============================================================

def fetch_calendar_events() -> list[dict]:
    """
    Возвращает список известных предстоящих важных событий.

    ВАЖНО: после трёх неудачных попыток найти надёжный бесплатный API
    календаря (JBlanked — лимит 1 запрос/день, Finnhub — экономический
    календарь требует платной подписки, Trading Economics — гостевой
    доступ полностью отключён), календарь ведётся вручную в этом списке.

    Обновляется при каждом "обнови дашборд" — туда же, где обновляются
    макрофакторы. Здесь нужно вписывать точную дату/время в UTC и
    форекаст/предыдущее значение, которые видно на дашборде в разделе
    "Календарь ближайших событий".
    """
    print(f"[ЛОГ] Используется ручной список событий ({len(KNOWN_EVENTS)} событий).")
    now_utc = datetime.now(timezone.utc)

    parsed = []
    for event in KNOWN_EVENTS:
        event_dt_utc = event["date_utc"]
        # пропускаем события, которые уже прошли больше суток назад —
        # не нужно их каждый раз заново парсить и проверять
        if (now_utc - event_dt_utc).total_seconds() > 86400:
            continue
        parsed.append({
            "id": event["id"],
            "name": event["name"],
            "date_et": event_dt_utc,  # имя поля сохранено для совместимости, значение в UTC
            "forecast": event.get("forecast"),
            "previous": event.get("previous"),
            "impact": "High",
        })

    return parsed


# --- РУЧНОЙ КАЛЕНДАРЬ СОБЫТИЙ ---
# Обновляется при каждом "обнови дашборд". Дата и время — в UTC.
# Если событие прошло больше суток назад — само пропустится в коде выше,
# можно не удалять вручную, но лучше чистить раз в несколько недель.
KNOWN_EVENTS = [
    {
        "id": "2026-06-30-pmi",
        "name": "PMI производства США (июнь)",
        "date_utc": datetime(2026, 6, 30, 14, 0, tzinfo=timezone.utc),
        "forecast": "49.0",
        "previous": "48.5",
    },
    {
        "id": "2026-07-03-nfp",
        "name": "Non-Farm Payrolls (июнь)",
        "date_utc": datetime(2026, 7, 3, 12, 30, tzinfo=timezone.utc),
        "forecast": "180K",
        "previous": "199K",
    },
    # Добавляй новые события сюда же по тому же образцу.
]


# ============================================================
# Построение объяснения события (5-пунктовый школьный стандарт)
# ============================================================

EVENT_EXPLANATIONS = {
    "cpi": (
        "<b>Перевод названия:</b> Consumer Price Index — \"Индекс потребительских цен\".\n"
        "<b>Кто считает и как:</b> Bureau of Labor Statistics (BLS) — государственное "
        "статистическое агентство США. Раз в месяц сравнивают цены на корзину товаров "
        "и услуг с прошлым месяцем.\n"
        "<b>Как влияет на золото:</b> Высокий CPI → ФРС вероятнее поднимет ставку → "
        "облигации становятся выгоднее золота → цена золота падает. Низкий CPI → наоборот."
    ),
    "pce": (
        "<b>Перевод названия:</b> Personal Consumption Expenditures — \"Расходы на личное "
        "потребление\".\n"
        "<b>Кто считает и как:</b> Bureau of Economic Analysis (BEA). Похож на CPI, но "
        "именно этот показатель ФРС использует как главный ориентир по инфляции.\n"
        "<b>Как влияет на золото:</b> Та же логика, что у CPI — но именно PCE для ФРС "
        "важнее, поэтому реакция рынка на этот отчёт часто сильнее."
    ),
    "non-farm": (
        "<b>Перевод названия:</b> Non-Farm Payrolls — \"Число занятых вне сельского "
        "хозяйства\".\n"
        "<b>Кто считает и как:</b> BLS, раз в месяц, первая пятница месяца. Считают, "
        "сколько новых рабочих мест появилось за месяц.\n"
        "<b>Как влияет на золото:</b> Сильный рынок труда → экономика выглядит крепкой "
        "→ ФРС спокойнее насчёт повышения ставки → может быть слегка медвежьим для "
        "золота. Слабые цифры — наоборот, поддержка для золота."
    ),
    "federal funds rate": (
        "<b>Перевод названия:</b> \"Ставка федеральных фондов\" — решение ФРС по "
        "процентной ставке.\n"
        "<b>Кто считает и как:</b> FOMC (комитет ФРС), голосование 8 раз в год.\n"
        "<b>Как влияет на золото:</b> Повышение ставки делает облигации привлекательнее "
        "золота (золото не платит процентов) → цена падает. Снижение — наоборот."
    ),
}


def get_explanation(event_name: str) -> str:
    name_lower = event_name.lower()
    for key, text in EVENT_EXPLANATIONS.items():
        if key in name_lower:
            return text
    return (
        "<b>Важное событие по доллару США</b> — может вызвать волатильность в золоте, "
        "так как золото торгуется в долларах и реагирует на ожидания по ставке ФРС."
    )


def get_nowcast_block(event_name: str) -> str:
    """Если событие про CPI/PCE — добавляем блок с Cleveland Fed nowcast."""
    name_lower = event_name.lower()
    if "cpi" not in name_lower and "pce" not in name_lower:
        return ""

    lines = [f"\n<b>📊 Cleveland Fed Nowcast (срез на {NOWCAST_DATE}):</b>"]
    for key, value in NOWCAST_DATA.items():
        lines.append(f"  {key}: {value}%")
    lines.append(
        "\n<i>Nowcast — это ежедневная предварительная оценка инфляции от "
        "Резервного банка Кливленда, основанная на ценах на нефть и бензин. "
        "Если nowcast выше официального прогноза рынка — шанс на \"горячий\" "
        "сюрприз (медвежье для золота). Если ниже — шанс на \"холодный\" "
        "сюрприз (бычье для золота). Это не гарантия, лишь обоснованная оценка.</i>"
    )
    return "\n".join(lines)


def build_warning_message(event: dict, minutes_before: int) -> str:
    name = event["name"]
    forecast = event.get("forecast", "н/д")
    previous = event.get("previous", "н/д")

    explanation = get_explanation(name)
    nowcast_block = get_nowcast_block(name)

    time_label = "1 ЧАС" if minutes_before >= 45 else "30 МИНУТ"

    message = (
        f"⏰ <b>До выхода данных: {time_label}</b>\n\n"
        f"<b>{name}</b>\n\n"
        f"Прогноз: {forecast} | Предыдущее значение: {previous}\n\n"
        f"{explanation}\n"
        f"{nowcast_block}\n\n"
        f"<b>Условный сценарий для золота:</b>\n"
        f"• Если факт выйдет ВЫШЕ прогноза → вероятнее движение ВНИЗ\n"
        f"• Если факт выйдет НИЖЕ прогноза → вероятнее движение ВВЕРХ\n"
        f"(точное направление узнаем только в момент публикации факта)\n\n"
        f"<b>Текущий контекст рынка:</b>\n{CURRENT_CONTEXT}\n\n"
        f"<i>Это не торговый сигнал, а контекст для твоего собственного решения. "
        f"Окончательное направление будет видно только по факту реакции рынка "
        f"в первые минуты после релиза.</i>"
    )
    return message


def build_spike_alert_message(old_price: float, new_price: float, minutes_elapsed: float) -> str:
    change_pct = (new_price - old_price) / old_price * 100
    direction = "ВЫРОСЛА 📈" if change_pct > 0 else "УПАЛА 📉"

    message = (
        f"⚡ <b>Резкое движение цены золота</b>\n\n"
        f"Цена {direction} на {abs(change_pct):.2f}% за последние {minutes_elapsed:.0f} минут\n\n"
        f"Было: ${old_price:,.2f}\n"
        f"Сейчас: ${new_price:,.2f}\n\n"
        f"<b>Простыми словами:</b> Такое движение за такой короткий промежуток "
        f"времени нетипично резкое для золота — обычно это означает выход "
        f"неожиданных новостей, крупную институциональную сделку, или реакцию "
        f"на геополитическое событие. Стоит проверить новости прямо сейчас.\n\n"
        f"<i>Это не торговый сигнал — просто уведомление о нетипичной активности.</i>"
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


# ============================================================
# Основная логика
# ============================================================

def main() -> None:
    now_tashkent = datetime.now(TASHKENT_TZ)

    # ВРЕМЕННО ОТКЛЮЧЕНО ДЛЯ ТЕСТИРОВАНИЯ — вернуть обратно после проверки!
    # if is_market_closed(now_tashkent):
    #     print(f"Рынок закрыт (сейчас {now_tashkent.strftime('%A %H:%M')} по Ташкенту). Алерты не отправляются.")
    #     return
    print(f"[ТЕСТ] Проверка выходных временно отключена. Сейчас {now_tashkent.strftime('%A %H:%M')} по Ташкенту.")

    state = load_state()
    now_utc = datetime.now(timezone.utc)

    # --- 1. Проверка предстоящих событий ---
    events = fetch_calendar_events()
    print(f"[ЛОГ] Получено {len(events)} подходящих событий от JBlanked API.")
    for e in events:
        mins = (e["date_et"].astimezone(timezone.utc) - now_utc).total_seconds() / 60
        print(f"  - {e['name']} | через {mins:.0f} мин | прогноз={e.get('forecast')} пред={e.get('previous')}")

    sent_warnings = set(state.get("sent_warnings", []))

    for event in events:
        event_dt_utc = event["date_et"].astimezone(timezone.utc)
        minutes_until = (event_dt_utc - now_utc).total_seconds() / 60

        for target_minutes, warning_key_suffix in [(60, "60m"), (30, "30m")]:
            warning_key = f"{event['id']}_{warning_key_suffix}"
            # Срабатывает, если событие в пределах окна (с запасом в 7 минут
            # на случай если запуск не попал точно на цель из-за интервала между запусками)
            if (target_minutes - 7) <= minutes_until <= (target_minutes + 7):
                if warning_key not in sent_warnings:
                    message = build_warning_message(event, minutes_until)
                    send_telegram_message(message)
                    sent_warnings.add(warning_key)

    # очищаем старые записи (события больше суток назад) чтобы файл не рос бесконечно
    state["sent_warnings"] = list(sent_warnings)[-200:]

    # --- 2. Проверка резкого движения цены ---
    current_price = fetch_gold_price()
    print(f"[ЛОГ] Текущая цена золота: {current_price}")
    if current_price is not None:
        last_price = state.get("last_price")
        last_price_time_str = state.get("last_price_time")

        if last_price is not None and last_price_time_str:
            last_price_time = datetime.fromisoformat(last_price_time_str)
            minutes_elapsed = (now_utc - last_price_time).total_seconds() / 60
            change_pct = abs((current_price - last_price) / last_price * 100)
            print(f"[ЛОГ] Предыдущая цена: {last_price} ({minutes_elapsed:.1f} мин назад), изменение: {change_pct:.3f}% (порог: {PRICE_SPIKE_THRESHOLD_PERCENT}%)")

            if change_pct >= PRICE_SPIKE_THRESHOLD_PERCENT and minutes_elapsed <= 35:
                message = build_spike_alert_message(last_price, current_price, minutes_elapsed)
                send_telegram_message(message)
        else:
            print("[ЛОГ] Нет предыдущей цены для сравнения (первый запуск).")

        state["last_price"] = current_price
        state["last_price_time"] = now_utc.isoformat()

    save_state(state)


if __name__ == "__main__":
    main()
