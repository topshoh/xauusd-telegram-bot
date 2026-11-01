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
