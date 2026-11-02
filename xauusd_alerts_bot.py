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
import time
import json
import base64
import html
import xml.etree.ElementTree as ET
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

PRICE_SPIKE_THRESHOLD_PERCENT = 0.4  # (устарело, оставлено для совместимости)

# Окна для поиска резких движений: (минут, порог в %, пауза между повторами в минутах, подпись)
# Сравниваем текущую цену с ценой из истории внутри окна, а не только с прошлым запуском —
# GitHub запускает бота неровно, и медленное падение на несколько % раньше проходило незамеченным.
MOVE_WINDOWS = [
    (30, 0.5, 60, "30 минут"),
    (60, 0.9, 90, "час"),
    (240, 1.6, 240, "4 часа"),
    (1440, 2.5, 720, "сутки"),
]
HISTORY_KEEP_MINUTES = 1440 + 360


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
# Получение календаря событий (ручной список)
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
        "id": "2026-11-06-nfp",
        "name": "Non-Farm Payrolls (октябрь)",
        "date_utc": datetime(2026, 11, 6, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "+29K (сентябрь), безработица 4.2%",
    },
    {
        "id": "2026-11-10-cpi",
        "name": "CPI (октябрь)",
        "date_utc": datetime(2026, 11, 10, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "3.7% г/г (сентябрь), core 2.6%",
    },
    {
        "id": "2026-11-18-fomc-minutes",
        "name": "Минутки FOMC (протокол предыдущего заседания)",
        "date_utc": datetime(2026, 11, 18, 19, 0, tzinfo=timezone.utc),
        "forecast": "—",
        "previous": "—",
    },
    {
        "id": "2026-11-25-pce",
        "name": "PCE (октябрь)",
        "date_utc": datetime(2026, 11, 25, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2026-12-04-nfp",
        "name": "Non-Farm Payrolls (ноябрь)",
        "date_utc": datetime(2026, 12, 4, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2026-12-09-fomc",
        "name": "FOMC — решение по ставке (8-9 декабря)",
        "date_utc": datetime(2026, 12, 9, 19, 0, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "3.75-4.00% (удержание 28 окт, голосование 10-2; повышение +25 б.п. 16 сент)",
    },
    {
        "id": "2026-12-10-cpi",
        "name": "CPI (ноябрь)",
        "date_utc": datetime(2026, 12, 10, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2026-12-30-fomc-minutes",
        "name": "Минутки FOMC (протокол предыдущего заседания)",
        "date_utc": datetime(2026, 12, 30, 19, 0, tzinfo=timezone.utc),
        "forecast": "—",
        "previous": "—",
    },
    {
        "id": "2027-01-08-nfp",
        "name": "Non-Farm Payrolls (декабрь)",
        "date_utc": datetime(2027, 1, 8, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-01-13-cpi",
        "name": "CPI (декабрь)",
        "date_utc": datetime(2027, 1, 13, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-01-27-fomc",
        "name": "FOMC — решение по ставке (26-27 января)",
        "date_utc": datetime(2027, 1, 27, 19, 0, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-02-05-nfp",
        "name": "Non-Farm Payrolls (январь)",
        "date_utc": datetime(2027, 2, 5, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-02-11-cpi",
        "name": "CPI (январь)",
        "date_utc": datetime(2027, 2, 11, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-02-17-fomc-minutes",
        "name": "Минутки FOMC (протокол предыдущего заседания)",
        "date_utc": datetime(2027, 2, 17, 19, 0, tzinfo=timezone.utc),
        "forecast": "—",
        "previous": "—",
    },
    {
        "id": "2027-03-05-nfp",
        "name": "Non-Farm Payrolls (февраль)",
        "date_utc": datetime(2027, 3, 5, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-03-10-cpi",
        "name": "CPI (февраль)",
        "date_utc": datetime(2027, 3, 10, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-03-17-fomc",
        "name": "FOMC — решение по ставке (16-17 марта)",
        "date_utc": datetime(2027, 3, 17, 18, 0, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-04-02-nfp",
        "name": "Non-Farm Payrolls (март)",
        "date_utc": datetime(2027, 4, 2, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-04-07-fomc-minutes",
        "name": "Минутки FOMC (протокол предыдущего заседания)",
        "date_utc": datetime(2027, 4, 7, 18, 0, tzinfo=timezone.utc),
        "forecast": "—",
        "previous": "—",
    },
    {
        "id": "2027-04-13-cpi",
        "name": "CPI (март)",
        "date_utc": datetime(2027, 4, 13, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-04-28-fomc",
        "name": "FOMC — решение по ставке (27-28 апреля)",
        "date_utc": datetime(2027, 4, 28, 18, 0, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-05-07-nfp",
        "name": "Non-Farm Payrolls (апрель)",
        "date_utc": datetime(2027, 5, 7, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-05-12-cpi",
        "name": "CPI (апрель)",
        "date_utc": datetime(2027, 5, 12, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-05-19-fomc-minutes",
        "name": "Минутки FOMC (протокол предыдущего заседания)",
        "date_utc": datetime(2027, 5, 19, 18, 0, tzinfo=timezone.utc),
        "forecast": "—",
        "previous": "—",
    },
    {
        "id": "2027-06-04-nfp",
        "name": "Non-Farm Payrolls (май)",
        "date_utc": datetime(2027, 6, 4, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-06-09-fomc",
        "name": "FOMC — решение по ставке (8-9 июня)",
        "date_utc": datetime(2027, 6, 9, 18, 0, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-06-10-cpi",
        "name": "CPI (май)",
        "date_utc": datetime(2027, 6, 10, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-06-30-fomc-minutes",
        "name": "Минутки FOMC (протокол предыдущего заседания)",
        "date_utc": datetime(2027, 6, 30, 18, 0, tzinfo=timezone.utc),
        "forecast": "—",
        "previous": "—",
    },
    {
        "id": "2027-07-02-nfp",
        "name": "Non-Farm Payrolls (июнь)",
        "date_utc": datetime(2027, 7, 2, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-07-14-cpi",
        "name": "CPI (июнь)",
        "date_utc": datetime(2027, 7, 14, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-07-28-fomc",
        "name": "FOMC — решение по ставке (27-28 июля)",
        "date_utc": datetime(2027, 7, 28, 18, 0, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-08-06-nfp",
        "name": "Non-Farm Payrolls (июль)",
        "date_utc": datetime(2027, 8, 6, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-08-11-cpi",
        "name": "CPI (июль)",
        "date_utc": datetime(2027, 8, 11, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-08-18-fomc-minutes",
        "name": "Минутки FOMC (протокол предыдущего заседания)",
        "date_utc": datetime(2027, 8, 18, 18, 0, tzinfo=timezone.utc),
        "forecast": "—",
        "previous": "—",
    },
    {
        "id": "2027-09-03-nfp",
        "name": "Non-Farm Payrolls (август)",
        "date_utc": datetime(2027, 9, 3, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-09-14-cpi",
        "name": "CPI (август)",
        "date_utc": datetime(2027, 9, 14, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-09-15-fomc",
        "name": "FOMC — решение по ставке (14-15 сентября)",
        "date_utc": datetime(2027, 9, 15, 18, 0, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-10-06-fomc-minutes",
        "name": "Минутки FOMC (протокол предыдущего заседания)",
        "date_utc": datetime(2027, 10, 6, 18, 0, tzinfo=timezone.utc),
        "forecast": "—",
        "previous": "—",
    },
    {
        "id": "2027-10-08-nfp",
        "name": "Non-Farm Payrolls (сентябрь)",
        "date_utc": datetime(2027, 10, 8, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-10-14-cpi",
        "name": "CPI (сентябрь)",
        "date_utc": datetime(2027, 10, 14, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-10-27-fomc",
        "name": "FOMC — решение по ставке (26-27 октября)",
        "date_utc": datetime(2027, 10, 27, 18, 0, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-11-05-nfp",
        "name": "Non-Farm Payrolls (октябрь)",
        "date_utc": datetime(2027, 11, 5, 12, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-11-10-cpi",
        "name": "CPI (октябрь)",
        "date_utc": datetime(2027, 11, 10, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-11-17-fomc-minutes",
        "name": "Минутки FOMC (протокол предыдущего заседания)",
        "date_utc": datetime(2027, 11, 17, 19, 0, tzinfo=timezone.utc),
        "forecast": "—",
        "previous": "—",
    },
    {
        "id": "2027-12-03-nfp",
        "name": "Non-Farm Payrolls (ноябрь)",
        "date_utc": datetime(2027, 12, 3, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-12-08-fomc",
        "name": "FOMC — решение по ставке (7-8 декабря)",
        "date_utc": datetime(2027, 12, 8, 19, 0, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-12-10-cpi",
        "name": "CPI (ноябрь)",
        "date_utc": datetime(2027, 12, 10, 13, 30, tzinfo=timezone.utc),
        "forecast": "уточнить ближе к дате",
        "previous": "—",
    },
    {
        "id": "2027-12-29-fomc-minutes",
        "name": "Минутки FOMC (протокол предыдущего заседания)",
        "date_utc": datetime(2027, 12, 29, 19, 0, tzinfo=timezone.utc),
        "forecast": "—",
        "previous": "—",
    },
    # Календарь внесён до конца 2027 г. (BLS, ФРС). Даты PCE (BEA) на 2027 пока не опубликованы — добавить позже.
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


# ============================================================
# Состояние рынка: S&P 500 и VIX (Yahoo Finance, бесплатно, без ключа)
# ============================================================
STRESS_SP500_DROP = -1.2    # падение S&P 500 за день, %
STRESS_VIX_JUMP = 10.0      # рост VIX за день, %
STRESS_VIX_LEVEL = 30.0     # или уровень VIX выше этого
STRESS_MAX_AGE_MIN = 360    # данные старше 6 часов (биржа закрыта) — сигнал не подаём


def _yahoo_quote(symbol: str):
    """(текущая цена, закрытие предыдущей сессии, возраст данных в минутах) или (None, None, None)."""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"range": "1d", "interval": "1d"},
            timeout=15, headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None or not prev:
            return None, None, None
        ts = meta.get("regularMarketTime")
        age_min = (datetime.now(timezone.utc).timestamp() - ts) / 60 if ts else None
        return float(price), float(prev), age_min
    except Exception as exc:
        print(f"[WARN] Yahoo {symbol}: {exc}")
        return None, None, None


def fetch_market_context():
    """{'sp': (цена, изм%), 'vix': (уровень, изм%)} или None, если данных нет."""
    sp, sp_prev, sp_age = _yahoo_quote("%5EGSPC")
    vx, vx_prev, vx_age = _yahoo_quote("%5EVIX")
    ctx = {}
    if sp is not None:
        ctx["sp"] = (sp, (sp - sp_prev) / sp_prev * 100)
    if vx is not None:
        ctx["vix"] = (vx, (vx - vx_prev) / vx_prev * 100)
    ages = [a for a in (sp_age, vx_age) if a is not None]
    if ctx and ages:
        ctx["age_min"] = min(ages)   # насколько свежие данные (минуты с последней сделки)
    return ctx or None


def vix_zone(v: float) -> str:
    if v < 15:
        return "🟢 спокойно"
    if v < 20:
        return "🟢 норма"
    if v < 25:
        return "🟡 тревожно"
    if v < 30:
        return "🟠 высокий страх"
    if v < 40:
        return "🔴 паника"
    return "🔴 экстремальная паника"


def sp_zone(pct: float) -> str:
    if pct > -0.5:
        return "🟢 обычные колебания"
    if pct > -1.2:
        return "🟡 небольшое снижение"
    if pct > -2.0:
        return "🟠 заметное падение"
    if pct > -3.0:
        return "🔴 сильное падение"
    return "🔴 обвал"


SCALE_TEXT = (
    "<b>Шкала для ориентира:</b>\n"
    "VIX («индекс страха»): до 15 — спокойно · 15–20 — норма · 20–25 — тревожно · "
    "25–30 — высокий страх · 30–40 — паника · выше 40 — экстремальная паника.\n"
    "S&P 500 за день: до −0.5% — обычные колебания · −0.5…−1.2% — небольшое снижение · "
    "−1.2…−2% — заметное падение (наш порог тревоги) · −2…−3% — сильное падение · "
    "ниже −3% — обвал.\n\n"
)

VIGILANCE_TEXT = (
    "👀 <b>Будьте бдительны.</b> В такие дни золото может пойти и вверх (если люди бегут в «тихую "
    "гавань»), и вниз (если крупные игроки продают всё подряд, чтобы закрыть убытки). Заранее "
    "угадать, что именно случится, нельзя — ни «покупать», ни «продавать» бот не рекомендует. "
    "Не спешите, не рискуйте деньгами, потерю которых не можете себе позволить, и не используйте "
    "кредитное плечо.\n\n"
)


def context_line(ctx) -> str:
    if not ctx:
        return ""
    parts = []
    if "sp" in ctx:
        parts.append(f"S&P 500 {ctx['sp'][1]:+.2f}% за день — {sp_zone(ctx['sp'][1])}")
    if "vix" in ctx:
        parts.append(f"VIX {ctx['vix'][0]:.1f} ({ctx['vix'][1]:+.1f}%) — {vix_zone(ctx['vix'][0])}")
    title = "Рынок сейчас" if ctx.get("age_min", 0) <= STRESS_MAX_AGE_MIN else "Рынок (на закрытие предыдущей сессии)"
    return f"📊 <b>{title}:</b>\n• " + "\n• ".join(parts) + "\n\n"


def stress_triggered(ctx) -> bool:
    if not ctx:
        return False
    if ctx.get("age_min", 0) > STRESS_MAX_AGE_MIN:
        print(f"[ЛОГ] Данные по S&P/VIX устарели ({ctx['age_min']:.0f} мин) — биржа закрыта, стресс-сигнал пропущен.")
        return False
    if "sp" in ctx and ctx["sp"][1] <= STRESS_SP500_DROP:
        return True
    if "vix" in ctx and (ctx["vix"][1] >= STRESS_VIX_JUMP or ctx["vix"][0] >= STRESS_VIX_LEVEL):
        return True
    return False


def build_stress_message(ctx, gold_change_pct) -> str:
    msg = "🌩 <b>Рынок под давлением</b>\n\n" + context_line(ctx)
    if gold_change_pct is not None:
        msg += f"🥇 Золото за сутки: {gold_change_pct:+.2f}%\n\n"
    if gold_change_pct is not None and gold_change_pct <= -1.0:
        msg += ("<b>Золото падает вместе с акциями.</b> Обычно они движутся по-разному. Когда падает "
                "всё сразу, это часто значит, что продают то, что продаётся легко (в том числе "
                "золото), чтобы закрыть убытки в других активах. Такая фаза обычно короткая, но по "
                "ней нельзя предсказать дно.\n\n")
    else:
        msg += ("<b>Простыми словами:</b> акции падают или растёт «индекс страха» VIX. Золото пока "
                "держится, но в такие дни его тоже могут продавать, чтобы покрыть убытки в других "
                "активах.\n\n")
    msg += VIGILANCE_TEXT + SCALE_TEXT
    msg += "<i>Это не торговый сигнал и не финансовый совет — просто уведомление о состоянии рынка.</i>"
    return msg


def fetch_gold_headlines(limit: int = 3) -> list[str]:
    """Свежие заголовки про золото (Google News RSS, бесплатно, без ключа)."""
    try:
        r = requests.get(
            "https://news.google.com/rss/search",
            params={"q": "gold price when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"},
            timeout=12, headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if title:
                out.append(html.escape(title))
            if len(out) >= limit:
                break
        return out
    except Exception as exc:
        print(f"[WARN] Заголовки новостей не получены: {exc}")
        return []


def build_move_alert_message(old_price: float, new_price: float, label: str, headlines: list[str], ctx=None) -> str:
    change_pct = (new_price - old_price) / old_price * 100
    direction = "ВЫРОСЛА 📈" if change_pct > 0 else "УПАЛА 📉"
    message = (
        f"⚡ <b>Резкое движение цены золота</b>\n\n"
        f"Цена {direction} на {abs(change_pct):.2f}% за последние {label}\n\n"
        f"Было: ${old_price:,.2f}\n"
        f"Сейчас: ${new_price:,.2f}\n\n"
        + context_line(ctx)
    )
    if headlines:
        message += "<b>Что пишут сейчас (заголовки, не проверено мной):</b>\n"
        message += "\n".join(f"• {h}" for h in headlines) + "\n\n"
    message += (
        "<b>Простыми словами:</b> такое движение для золота нетипично. Чаще всего за ним стоят "
        "неожиданные новости, распродажа всего рынка (когда продают и золото, чтобы закрыть "
        "убытки в других активах) или смена ожиданий по ставке ФРС. "
        "Напишите мне в чат «почему упало золото» — разберу причину по свежим данным.\n\n"
        "<i>Это не торговый сигнал — просто уведомление о нетипичной активности.</i>"
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

# ============================================================
# «ЧТО ИЗМЕНИЛОСЬ ПОСЛЕ ВЫХОДА ДАННЫХ» (NFP / CPI / PCE)
# ============================================================
POST_EVENT_KINDS = {"nfp": "nfp", "cpi": "cpi", "pce": "pce"}
PRE_SNAPSHOT_WINDOW_MIN = 120      # снимок «до» делаем за 2 часа до выхода
POST_FIRST_CHECK_MIN = 2           # первая проверка через 2 минуты после выхода
POST_GIVE_UP_HOURS = 48            # после 48 часов перестаём ждать данные в источниках


def _event_kind(event):
    eid = event.get("id", "")
    for suffix, kind in POST_EVENT_KINDS.items():
        if eid.endswith("-" + suffix):
            return kind
    return None


def handle_post_release(state: dict, now_utc: datetime) -> None:
    """Снимок до выхода → отчёт об изменениях после выхода. Любая ошибка не ломает остальные алерты."""
    if not os.environ.get("FRED_API_KEY"):
        # В workflow алертов ещё не прописаны ключи FRED/BLS — без них снимок данных пустой.
        print("[ЛОГ] Отчёт после выхода данных отключён: нет FRED_API_KEY в окружении workflow алертов.")
        return
    try:
        import xauusd_numeric_bot as nb
    except Exception as exc:
        print(f"[WARN] Отчёт после выхода данных недоступен (импорт): {exc}")
        return
    snaps = state.setdefault("snapshots", {})
    done = state.setdefault("post_done", {})
    tries = state.setdefault("post_tries", {})

    for event in KNOWN_EVENTS:
        kind = _event_kind(event)
        if not kind:
            continue
        eid, when = event["id"], event["date_utc"]
        minutes_to = (when - now_utc).total_seconds() / 60

        # 1) снимок ДО выхода
        if 0 < minutes_to <= PRE_SNAPSHOT_WINDOW_MIN and eid not in snaps:
            try:
                snaps[eid] = nb.compute_snapshot()
                print(f"[ЛОГ] Сохранён снимок «до» для {eid}")
            except Exception as exc:
                print(f"[WARN] Снимок «до» для {eid} не получен: {exc}")
            continue

        # 2) отчёт ПОСЛЕ выхода
        minutes_after = -minutes_to
        if minutes_after < POST_FIRST_CHECK_MIN or eid in done:
            continue
        if minutes_after > POST_GIVE_UP_HOURS * 60:
            if eid not in done:
                done[eid] = "gave_up"
                send_telegram_message(
                    f"ℹ️ После выхода «{event['name']}» новые данные в бесплатных источниках так и не "
                    f"появились за {POST_GIVE_UP_HOURS} часов. Проверьте вручную или напишите мне «обнови дашборд».")
            continue
        last_try = tries.get(eid)
        gap = 10 if minutes_after <= 180 else 120   # первые 3 часа — при каждом запуске (~15 мин), потом раз в 2 часа
        if last_try:
            try:
                if (now_utc - datetime.fromisoformat(last_try)).total_seconds() / 60 < gap:
                    continue
            except Exception:
                pass
        tries[eid] = now_utc.isoformat()
        try:
            after = nb.compute_snapshot()
        except Exception as exc:
            print(f"[WARN] Снимок «после» для {eid} не получен: {exc}")
            continue
        before = snaps.get(eid)
        if before is not None and not nb.release_detected(kind, before, after):
            print(f"[ЛОГ] {eid}: новых данных в источниках пока нет, повторю позже.")
            continue
        send_telegram_message(nb.build_change_report(event["name"], before, after))
        done[eid] = now_utc.isoformat()
        state["digest_base"] = after   # не дублируем то же изменение в еженедельном отчёте

    # чистим старое, чтобы файл состояния не рос
    cutoff = now_utc - timedelta(days=10)
    for d in (snaps, done, tries):
        for k in list(d.keys()):
            ev = next((e for e in KNOWN_EVENTS if e["id"] == k), None)
            if ev and ev["date_utc"] < cutoff:
                d.pop(k, None)


# ============================================================
# ИЗМЕНЕНИЯ ВНУТРИ НЕДЕЛИ: если вердикт фактора, общий уклон или отчёт COT изменились
# ============================================================
DIGEST_CHECK_EVERY_MIN = 180   # собираем данные раз в 3 часа (не при каждом запуске)


def _verdict_sig(snap) -> dict:
    return {n: f["verdict"] for n, f in snap["factors"].items()}


def handle_weekly_changes(state: dict, now_utc: datetime) -> None:
    """Раз в ~3 часа сравнивает снимок с последним сообщённым. Если изменение (вердикт фактора,
    уклон или новый отчёт COT) подтвердилось двумя проверками подряд — шлёт «что изменилось»."""
    if not os.environ.get("FRED_API_KEY"):
        print("[ЛОГ] Отчёт об изменениях внутри недели отключён: нет FRED_API_KEY в окружении.")
        return
    last_check = state.get("digest_last_check")
    if last_check:
        try:
            if (now_utc - datetime.fromisoformat(last_check)).total_seconds() / 60 < DIGEST_CHECK_EVERY_MIN:
                return
        except Exception:
            pass
    try:
        import xauusd_numeric_bot as nb
        snap = nb.compute_snapshot()
    except Exception as exc:
        print(f"[WARN] Снимок для отчёта об изменениях не получен: {exc}")
        return
    state["digest_last_check"] = now_utc.isoformat()
    if not snap.get("factors"):
        return
    base = state.get("digest_base")
    if not base:
        state["digest_base"] = snap
        return

    names = {n for n, v in _verdict_sig(snap).items() if base["factors"].get(n, {}).get("verdict") != v}
    cot_changed = bool(snap["raw"].get("cot_date")) and snap["raw"].get("cot_date") != base["raw"].get("cot_date")
    if cot_changed:
        names.add("Хедж-фонды")
    tilt_changed = (snap["bullish"] - snap["bearish"] > 0) != (base["bullish"] - base["bearish"] > 0) \
        or (snap["bullish"] - snap["bearish"] < 0) != (base["bullish"] - base["bearish"] < 0)
    if not names and not tilt_changed:
        state.pop("digest_pending", None)
        return

    sig = json.dumps([sorted(names), _verdict_sig(snap), snap["raw"].get("cot_date")], sort_keys=True, ensure_ascii=False)
    if state.get("digest_pending") != sig:
        state["digest_pending"] = sig     # подтверждаем следующей проверкой, чтобы не ловить «мигание»
        return
    send_telegram_message(nb.build_change_report(
        "", base, snap, names=names,
        title="🔔 <b>За время с прошлого сообщения изменились данные из сводки:</b>"))
    state["digest_base"] = snap
    state.pop("digest_pending", None)


def send_demo_messages() -> None:
    """Тестовый режим: при ручном запуске (Run workflow) шлёт примеры сообщений с пометкой ТЕСТ.
    Состояние бота не меняется, реальных алертов не создаётся."""
    tag = "🧪 <b>ТЕСТ — так будет выглядеть сообщение (цифры примерные)</b>\n\n"
    demo_ctx = {"sp": (7580.07, -1.82), "vix": (24.6, 12.9)}
    demo_event = {
        "id": "demo", "name": "Non-Farm Payrolls (октябрь)",
        "date_utc": datetime.now(timezone.utc) + timedelta(minutes=30),
        "forecast": "уточнить ближе к дате", "previous": "+29K (сентябрь), безработица 4.2%",
    }
    messages = [
        tag + "<b>1/4. Предупреждение о событии (за 30 минут):</b>\n\n" + build_warning_message(demo_event, 30),
        tag + "<b>2/4. Резкое движение цены золота:</b>\n\n" + build_move_alert_message(
            3992.0, 3899.0, "4 часа",
            ["Gold falls to one-year low as Fed hike bets build (пример заголовка)"], demo_ctx),
        tag + "<b>3/4. Рынок под давлением:</b>\n\n" + build_stress_message(demo_ctx, -2.3),
    ]
    try:
        import xauusd_numeric_bot as nb
        bf = {"price": 3990.0, "bullish": 3, "bearish": 1, "raw": {},
              "factors": {"Рабочие места": {"verdict": "neutral", "text": "+29К, мало — слабая экономика"},
                          "Доллар": {"verdict": "neutral", "text": "почти без изменений (121.4)"}}}
        af = {"price": 3935.0, "bullish": 3, "bearish": 2, "raw": {},
              "factors": {"Рабочие места": {"verdict": "bearish", "text": "+185К, много — сильная экономика, ФРС жёстче"},
                          "Доллар": {"verdict": "bearish", "text": "вырос на 0.4% (121.9)"}}}
        messages.append(tag + "<b>4/4. Что изменилось после выхода данных (пример):</b>\n\n"
                        + nb.build_change_report("Non-Farm Payrolls (октябрь)", bf, af))
    except Exception as exc:
        print(f"[WARN] Демо отчёта после выхода не собрано: {exc}")
    for m in messages:
        send_telegram_message(m)
        time.sleep(1.5)


def main() -> None:
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("Ручной запуск (Run workflow) — отправляю тестовые сообщения.")
        send_demo_messages()
        return
    now_tashkent = datetime.now(TASHKENT_TZ)

    if is_market_closed(now_tashkent):
        print(f"Рынок закрыт (сейчас {now_tashkent.strftime('%A %H:%M')} по Ташкенту). Алерты не отправляются.")
        return

    state = load_state()
    now_utc = datetime.now(timezone.utc)

    # --- 1. Проверка предстоящих событий ---
    events = fetch_calendar_events()
    print(f"[ЛОГ] Получено {len(events)} подходящих событий из календаря.")
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

    # --- 1б. Отчёт «что изменилось после выхода данных» ---
    handle_post_release(state, now_utc)
    handle_weekly_changes(state, now_utc)

    # --- 2. Проверка резкого движения цены (несколько окон по истории цен) ---
    current_price = fetch_gold_price()
    parsed = []
    print(f"[ЛОГ] Текущая цена золота: {current_price}")
    if current_price is not None:
        history = state.get("price_history") or []
        if not history and state.get("last_price") and state.get("last_price_time"):
            history = [[state["last_price_time"], state["last_price"]]]
        parsed = []
        for t, pr in history:
            try:
                parsed.append((datetime.fromisoformat(t), float(pr)))
            except Exception:
                continue
        parsed = [(t, pr) for t, pr in parsed if (now_utc - t).total_seconds() / 60 <= HISTORY_KEEP_MINUTES]

        sent_moves = state.get("sent_moves") or {}
        for minutes, threshold, cooldown, label in MOVE_WINDOWS:
            # опорная цена: самая старая точка, которая не старше окна (+25% запаса) и не моложе половины окна
            candidates = [(t, pr) for t, pr in parsed
                          if minutes * 0.5 <= (now_utc - t).total_seconds() / 60 <= minutes * 1.25]
            if not candidates:
                continue
            ref_t, ref_price = min(candidates, key=lambda x: x[0])
            change_pct = (current_price - ref_price) / ref_price * 100
            print(f"[ЛОГ] Окно {label}: было {ref_price} → {current_price}, {change_pct:+.2f}% (порог {threshold}%)")
            if abs(change_pct) >= threshold:
                dir_key = "up" if change_pct > 0 else "down"
                # не засыпаем сообщениями: одно движение — один алерт (общая пауза 2 часа
                # на направление + своя пауза у каждого окна)
                skip = False
                for key, pause in ((f"{label}_{dir_key}", cooldown), (f"any_{dir_key}", 120)):
                    last_sent = sent_moves.get(key)
                    if last_sent:
                        try:
                            if (now_utc - datetime.fromisoformat(last_sent)).total_seconds() / 60 < pause:
                                skip = True
                        except Exception:
                            pass
                if skip:
                    continue
                headlines = fetch_gold_headlines()
                send_telegram_message(build_move_alert_message(ref_price, current_price, label, headlines, fetch_market_context()))
                sent_moves[f"{label}_{dir_key}"] = now_utc.isoformat()
                sent_moves[f"any_{dir_key}"] = now_utc.isoformat()
                break  # одного сообщения за запуск достаточно

        parsed.append((now_utc, current_price))
        state["price_history"] = [[t.isoformat(), pr] for t, pr in parsed][-200:]
        state["sent_moves"] = sent_moves
        state["last_price"] = current_price
        state["last_price_time"] = now_utc.isoformat()

    # --- 3. Рынок под давлением (S&P 500 / VIX), не чаще раза в сутки ---
    stress_key = f"stress_{now_utc.strftime('%Y-%m-%d')}"
    sent_moves = state.get("sent_moves") or {}
    if stress_key not in sent_moves:
        ctx = fetch_market_context()
        if stress_triggered(ctx):
            gold_change = None
            if current_price is not None:
                for t, pr in sorted(parsed):
                    age = (now_utc - t).total_seconds() / 60
                    if 600 <= age <= 1800:
                        gold_change = (current_price - pr) / pr * 100
                        break
            send_telegram_message(build_stress_message(ctx, gold_change))
            sent_moves[stress_key] = now_utc.isoformat()
    # чистим старые ключи, чтобы файл не рос
    state["sent_moves"] = {k: v for k, v in sent_moves.items()
                           if not k.startswith("stress_") or k == stress_key or k >= f"stress_{(now_utc - timedelta(days=3)).strftime('%Y-%m-%d')}"}

    save_state(state)


if __name__ == "__main__":
    main()
