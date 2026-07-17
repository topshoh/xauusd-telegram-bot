"""
xauusd_numeric_bot.py
======================
Полностью автономный бот: тянет ТОЛЬКО реальные цифры из настоящих
бесплатных API (без ИИ в контуре, без риска выдумать данные) и шлёт
сводку в Telegram. Рассчитан на запуск по расписанию через GitHub Actions
(cron), но можно запускать и вручную для проверки.

ЧТО ЭТОТ БОТ УМЕЕТ САМ, БЕЗ ЧЕЛОВЕКА И БЕЗ ИИ:
  - Живая цена золота (gold-api.com)
  - Реальная (TIPS) доходность 10-летних облигаций (FRED)
  - Широкий индекс доллара (FRED, DTWEXBGS — честный аналог DXY, не сам DXY)
  - Последние факт/прогноз по CPI, NFP (BLS), PCE (FRED PCEPI)
  - Последний отчёт COT по золоту (CFTC, Socrata API)
  - Автоматическая классификация каждого фактора на Бычий/Медвежий/
    Нейтральный на основе реального сравнения с предыдущей точкой
    (не выдуманных чисел — см. функции classify_*)

ЧЕГО ЭТОТ БОТ НЕ ДЕЛАЕТ (осознанно, чтобы не выдумывать):
  - Не считает "вероятность решения ФРС" (нет бесплатного API у CME FedWatch)
  - Не даёт сигналов BUY/SELL и не прогнозирует цену — только счёт факторов
    ("уклон") как контекст, явно помечено что это не рекомендация

НУЖНЫЕ СЕКРЕТЫ (задаются в GitHub Secrets репозитория, см. ниже список):
  TELEGRAM_BOT_TOKEN   - токен бота от @BotFather
  TELEGRAM_CHAT_ID     - куда слать (личный чат или канал)
  FRED_API_KEY         - бесплатный ключ https://fred.stlouisfed.org/docs/api/api_key.html
  BLS_API_KEY          - бесплатный ключ https://www.bls.gov/developers/home.htm (не обязателен,
                          но без ключа лимит намного ниже и без него часто падает)

ИСТОРИЯ ИЗМЕНЕНИЙ:
  14.07.2026 — убран BEA_API_KEY (PCE теперь через FRED, надёжнее и без
  риска перепутать номер строки в таблице), добавлен DXY-прокси, добавлена
  классификация факторов на бычьи/медвежьи/нейтральные с реальными
  сравнениями вместо единого текстового сообщения по каждому показателю.

ВАЖНО ПРО ЭТОТ ФАЙЛ:
  Я (Claude) не могу протестировать этот скрипт "живьём" в этом чате — у
  меня нет ваших API-ключей и нет доступа к Telegram. Структура и
  эндпоинты проверены по документации и актуальному веб-поиску на момент
  написания, но перед боевым использованием обязательно прогоните хотя бы
  один раз вручную (`python xauusd_numeric_bot.py`) и проверьте, что
  сообщение реально пришло и цифры выглядят разумно.
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta

# ============================================================
# КОНФИГ / СЕКРЕТЫ (берутся из переменных окружения — GitHub Secrets)
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
FRED_API_KEY = os.environ.get("FRED_API_KEY")
BLS_API_KEY = os.environ.get("BLS_API_KEY", "")  # опционален, но лучше иметь

TASHKENT_TZ = timezone(timedelta(hours=5))

# ============================================================
# КАЛЕНДАРЬ ДЛЯ БОЛЬШИХ НАПОМИНАНИЙ (заглавными буквами в Telegram)
# Обновлять вручную по мере появления новых подтверждённых дат.
# ============================================================
REMINDER_EVENTS = [
    {"date": "2026-07-14", "label": "CPI за июнь (BLS)"},
    {"date": "2026-07-28", "label": "FOMC — решение по ставке, день 1"},
    {"date": "2026-07-29", "label": "FOMC — решение + пресс-конференция"},
    {"date": "2026-07-30", "label": "PCE за июнь (BEA) — самое важное событие месяца"},
]


def _is_market_closed_at(dt) -> bool:
    """Чистая функция без побочных эффектов — принимает конкретный момент
    времени, а не всегда 'сейчас'. Нужна, чтобы переиспользовать логику
    выходных для расчёта 'первый открытый день месяца' (см.
    build_token_hygiene_reminder), а не только для гейта в main()."""
    wd, hr = dt.weekday(), dt.hour  # 0=Пн ... 5=Сб, 6=Вс
    if wd == 5 and hr >= 2:
        return True
    if wd == 6:
        return True
    if wd == 0 and hr < 3:
        return True
    return False


def is_market_closed() -> bool:
    """Рынок золота закрыт с субботы 02:00 до понедельника 03:00 по Ташкенту
    (тот же паттерн, что и в старых ботах проекта — см. project_context.md)."""
    now = datetime.now(TASHKENT_TZ)
    return _is_market_closed_at(now)


# ============================================================
# 1. ЦЕНА ЗОЛОТА (gold-api.com — уже проверенный источник в проекте)
# ============================================================
def fetch_gold_price(max_retries=3, delay_seconds=5):
    """С повторными попытками — единичный сетевой сбой (например, разовый
    DNS-глюк у раннера GitHub Actions, как было 12.07.2026) не должен
    оставлять сводку без цены."""
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get("https://api.gold-api.com/price/XAU", timeout=15)
            r.raise_for_status()
            data = r.json()
            price = data.get("price") or data.get("price_usd")
            if price is not None:
                return float(price)
            last_error = "в ответе API нет поля price"
        except Exception as e:
            last_error = e
        print(f"[WARN] Попытка {attempt}/{max_retries} получить цену золота не удалась: {last_error}")
        if attempt < max_retries:
            time.sleep(delay_seconds)
    print(f"[WARN] Все {max_retries} попытки получить цену золота не удались. Последняя ошибка: {last_error}")
    return None


# ============================================================
# 2. ДОХОДНОСТИ ОБЛИГАЦИЙ И ДОЛЛАР (FRED — официальный API ФРБ Сент-Луиса)
#    DGS10     = номинальная доходность 10-летних Treasury
#    DFII10    = реальная доходность 10-летних TIPS
#    DTWEXBGS  = широкий индекс доллара ФРС (не то же самое, что ICE DXY,
#                который смотрят трейдеры, но честный бесплатный аналог —
#                называем его прямо так, без вранья про "это DXY")
# ============================================================
def fetch_fred_series(series_id: str, lookback_points: int = 22):
    """Возвращает (текущее_значение, дата, значение_около_месяца_назад).
    lookback_points=22 — примерно месяц торговых дней назад для дневных
    рядов (DGS10, DFII10); для месячных рядов (DTWEXBGS) это может
    захватить больше одного периода, но нам достаточно "предыдущая
    доступная точка" для сравнения направления."""
    if not FRED_API_KEY:
        print(f"[WARN] FRED_API_KEY не задан, пропускаю {series_id}")
        return None, None, None
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": lookback_points,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        obs = [o for o in r.json().get("observations", []) if o.get("value") != "."]
        if not obs:
            return None, None, None
        latest_val = float(obs[0]["value"])
        latest_date = obs[0]["date"]
        prev_val = float(obs[-1]["value"]) if len(obs) > 1 else None
        return latest_val, latest_date, prev_val
    except Exception as e:
        print(f"[WARN] Не удалось получить {series_id} из FRED: {e}")
        return None, None, None


def fetch_fred_yoy(series_id: str, n_points: int = 13):
    """Год к году — берём точку сейчас и точку 12 периодов назад (для
    месячных рядов вроде PCEPI это ровно 12 месяцев) и считаем % сами."""
    if not FRED_API_KEY:
        print(f"[WARN] FRED_API_KEY не задан, пропускаю {series_id}")
        return None
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": n_points,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        obs = [o for o in r.json().get("observations", []) if o.get("value") != "."]
        if len(obs) < n_points:
            print(f"[WARN] Недостаточно точек для YoY по {series_id}: получено {len(obs)}, нужно {n_points}")
            return None
        latest = float(obs[0]["value"])
        year_ago = float(obs[n_points - 1]["value"])
        yoy = (latest - year_ago) / year_ago * 100
        return round(yoy, 2), obs[0]["date"]
    except Exception as e:
        print(f"[WARN] Не удалось получить YoY для {series_id} из FRED: {e}")
        return None


# ============================================================
# 3. CPI / NFP (BLS — Bureau of Labor Statistics, официальный API)
#    Series IDs: CUUR0000SA0 = CPI-U (headline), CES0000000001 = Total Nonfarm Employment
#    NFP тут отдаёт УРОВЕНЬ занятости, не месячное изменение напрямую —
#    для изменения нужно брать разницу двух последних точек (сделано ниже).
# ============================================================
def fetch_bls_series(series_id: str, n_points: int = 2):
    url = f"https://api.bls.gov/publicAPI/v2/timeseries/data/{series_id}"
    payload = {"seriesid": [series_id], "latest": "true" if n_points == 1 else "false"}
    if not n_points == 1:
        payload["startyear"] = str(datetime.now().year - 1)
        payload["endyear"] = str(datetime.now().year)
    if BLS_API_KEY:
        payload["registrationkey"] = BLS_API_KEY
    try:
        r = requests.post(url, json=payload, timeout=20)
        r.raise_for_status()
        result = r.json()
        if result.get("status") != "REQUEST_SUCCEEDED":
            print(f"[WARN] BLS API вернул ошибку для {series_id}: {result.get('message')}")
            return []
        series = result["Results"]["series"][0]["data"]
        return series[:n_points]
    except Exception as e:
        print(f"[WARN] Не удалось получить {series_id} из BLS: {e}")
        return []


def fetch_cpi_yoy():
    """CPI год к году — берём текущую и точку 12 месяцев назад и считаем % сами,
    так как BLS отдаёт индекс, а не готовый % изменения. Дополнительно
    считаем CPI год к году МЕСЯЦ НАЗАД (точки [1] и [13]) — чтобы можно
    было показать "было X%, стало Y%" для сравнения."""
    points = fetch_bls_series("CUUR0000SA0", n_points=14)
    if len(points) < 14:
        return None
    latest = float(points[0]["value"])
    year_ago = float(points[12]["value"])
    yoy = (latest - year_ago) / year_ago * 100

    prev_month = float(points[1]["value"])
    prev_year_ago = float(points[13]["value"])
    prev_yoy = (prev_month - prev_year_ago) / prev_year_ago * 100

    period_label = f"{points[0]['periodName']} {points[0]['year']}"
    return round(yoy, 2), period_label, round(prev_yoy, 2)


def fetch_nfp_change():
    """NFP — изменение занятости за последний месяц, в формате "+57K".

    ОКОНЧАТЕЛЬНО ПОДТВЕРЖДЕНО живым тестом 09.07.2026 (см. debug-лог):
    latest=158984.0, prev=158927.0, diff=57.0 — ровно совпадает с реальным
    известным значением "+57K". BLS-серия CES0000000001 действительно
    в тысячах человек, как и предполагалось изначально. Разница берётся
    как есть, без умножения и без деления."""
    points = fetch_bls_series("CES0000000001", n_points=2)
    if len(points) < 2:
        return None
    latest = float(points[0]["value"])
    prev = float(points[1]["value"])
    change_thousands = round(latest - prev)
    period_label = f"{points[0]['periodName']} {points[0]['year']}"
    return change_thousands, period_label


# ============================================================
# 4. PCE (BEA — Bureau of Economic Analysis, официальный API)
# ============================================================
def fetch_pce_yoy():
    """PCE год к году — headline (общий).

    ИСПРАВЛЕНО: раньше брал таблицу BEA T20804 с LineNumber=1, предполагая,
    что это headline-агрегат — но реальный тестовый прогон 13.07.2026 дал
    -4.02%, что явно неправдоподобно (настоящий headline PCE положительный,
    около +4%). Судя по всему, LineNumber=1 в этой таблице — не общий
    показатель, а конкретная подкатегория (например, товары длительного
    пользования, где дефляция — обычное дело, отсюда и минус). Вместо того
    чтобы гадать правильный номер строки, переключился на FRED — у них есть
    прямой, однозначный ряд PCEPI (тот же показатель ФРС, что публикует
    BEA, просто уже без риска перепутать категорию), и FRED уже проверенно
    работает в этом боте для доходностей."""
    return fetch_fred_yoy("PCEPI")


def fetch_dxy_proxy():
    """Индекс доллара — через FRED (DTWEXBGS, Nominal Broad U.S. Dollar
    Index). ВАЖНО: это НЕ тот же самый ICE DXY, который обычно смотрят
    трейдеры (у него нет бесплатного API) — это отдельный, тоже официальный
    и реальный индекс от ФРС, но с другой корзиной валют. Называем его
    честно "широкий индекс доллара (ФРС)", а не "DXY", чтобы не повторить
    ошибку с "выдачей за то, чем это не является". Обновляется раз в
    рабочий день."""
    latest, date, prev = fetch_fred_series("DTWEXBGS", lookback_points=10)
    return latest, date, prev


# ============================================================
# 5. COT ПО ЗОЛОТУ (CFTC Socrata API — бесплатно, без ключа)
#    Disaggregated Futures Only report, отфильтрован по золоту (COMEX)
# ============================================================
def fetch_cot_gold():
    url = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
    params = {
        "$where": "market_and_exchange_names like 'GOLD%COMMODITY EXCHANGE%'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 1,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        print(f"[WARN] Не удалось получить COT из CFTC: {e}")
        return None


# ============================================================
# СБОРКА СООБЩЕНИЯ
# ============================================================

# ============================================================
# БОЛЬШИЕ НАПОМИНАНИЯ (заглавными буквами) — про важные даты и про
# гигиену GitHub-токена. Не требуют state-файла: просто математика дат.
# ============================================================
def build_event_reminders():
    today = datetime.now(TASHKENT_TZ).date()
    reminders = []
    for event in REMINDER_EVENTS:
        event_date = datetime.strptime(event["date"], "%Y-%m-%d").date()
        days_until = (event_date - today).days
        label_upper = event["label"].upper()
        if 0 < days_until <= 3:
            reminders.append(
                f"⚠️ <b>ЧЕРЕЗ {days_until} ДН. ВЫХОДИТ: {label_upper}</b>\n"
                f"Дата: {event_date.strftime('%d.%m.%Y')}\n\n"
                f"ПОСЛЕ ВЫХОДА ЭТИХ ДАННЫХ СТОИТ ПОПРОСИТЬ «ОБНОВИ ДАШБОРД»."
            )
        elif -2 <= days_until <= 0:
            reminders.append(
                f"🔔 <b>{label_upper} УЖЕ ВЫШЕЛ</b> ({event_date.strftime('%d.%m.%Y')})\n\n"
                f"ЕСЛИ ЕЩЁ НЕ ПРОСИЛИ ОБНОВИТЬ ДАШБОРД — СЕЙЧАС САМОЕ ВРЕМЯ."
            )
    return reminders


def build_token_hygiene_reminder():
    """Раз в месяц — напоминание проверить GitHub-токен.

    ИСПРАВЛЕНО: раньше проверяло строго 'today.day == 1' — если 1-е число
    попадало на закрытый рынком день (суббота/воскресенье), бот в этот
    день вообще не запускал отправку (см. is_market_closed в main()), и
    напоминание молча пропадало на весь месяц. Теперь ищем ПЕРВЫЙ
    открытый день месяца (проверяем 1, 2, 3 числа по очереди) и шлём
    именно в этот день — 1-го, 2-го или 3-го, смотря по обстоятельствам."""
    now = datetime.now(TASHKENT_TZ)
    first_open_day = None
    for day_num in range(1, 4):  # у длинных выходных на стыке месяцев больше 2 дней подряд не бывает
        candidate = now.replace(day=day_num, hour=12, minute=0, second=0, microsecond=0)
        if not _is_market_closed_at(candidate):
            first_open_day = day_num
            break
    if first_open_day is not None and now.day == first_open_day:
        return (
            f"🔐 <b>ЕЖЕМЕСЯЧНОЕ НАПОМИНАНИЕ: ПРОВЕРЬТЕ GITHUB-ТОКЕН</b>\n\n"
            f"Зайдите в Settings → Developer settings → Personal access tokens "
            f"и убедитесь, что токен ещё нужен, не истёк и не выдан с более "
            f"широкими правами, чем требуется (для этого проекта достаточно "
            f"Contents: Read/write)."
        )
    return None


def is_full_report_day():
    """Полную сводку из 7 сообщений шлём по понедельникам — в остальные
    дни бот только проверяет напоминания (см. main()), чтобы не спамить
    одними и теми же цифрами каждый день."""
    today = datetime.now(TASHKENT_TZ)
    return today.weekday() == 0  # 0 = понедельник


# ============================================================
# СБОРКА СООБЩЕНИЯ — С ГРУППИРОВКОЙ ПО БЫЧЬИ/МЕДВЕЖЬИ/НЕЙТРАЛЬНЫЕ
# Каждый фактор классифицируется на основе РЕАЛЬНОГО сравнения с
# предыдущей точкой (не выдуманных чисел) — см. классификаторы ниже.
# Пороги для NFP/COT — простые, объяснённые прямо в тексте сообщения,
# не выдаются за точную науку.
# ============================================================

def classify_yield(latest, prev):
    """Реальная доходность облигаций: растёт -> медвежий (конкурент золота
    сильнее), падает -> бычий. Порог 0.02 п.п., чтобы шум не считался
    движением."""
    if latest is None or prev is None:
        return None
    diff = latest - prev
    if diff > 0.02:
        return "bearish", f"выросла до {latest:.2f}% (с {prev:.2f}%) — деньги уходят в облигации"
    elif diff < -0.02:
        return "bullish", f"упала до {latest:.2f}% (с {prev:.2f}%) — облигации менее выгодны"
    return "neutral", f"почти без изменений ({latest:.2f}%)"


def classify_dxy(latest, prev):
    """Индекс доллара: растёт -> медвежий для золота (доллар крепче,
    золото дороже для остального мира), падает -> бычий."""
    if latest is None or prev is None:
        return None
    diff_pct = (latest - prev) / prev * 100
    if diff_pct > 0.15:
        return "bearish", f"вырос на {diff_pct:.2f}% ({latest:.1f}) — золото дороже для мира"
    elif diff_pct < -0.15:
        return "bullish", f"упал на {abs(diff_pct):.2f}% ({latest:.1f}) — золото дешевле для мира"
    return "neutral", f"почти без изменений ({latest:.1f})"


def classify_nfp(value):
    """NFP: мало новых рабочих мест -> бычий (слабая экономика, ФРС мягче),
    много -> медвежий. Пороги условные, обычная норма ~100-150K."""
    if value is None:
        return None
    if value < 100:
        return "bullish", f"+{value}К, мало — слабая экономика, ФРС смягчится"
    elif value > 150:
        return "bearish", f"+{value}К, много — сильная экономика, ФРС жёстче"
    return "neutral", f"+{value}К, в пределах нормы"


def classify_cot(long_pos, short_pos):
    """COT: сильный перевес в лонг -> бычий (но с оговоркой про риск
    перегрева, см. текст ниже)."""
    if long_pos is None or short_pos is None:
        return None
    total = long_pos + short_pos
    if total == 0:
        return None
    long_share = long_pos / total * 100
    if long_share > 70:
        return "bullish", f"{long_share:.0f}% в лонге — крупные игроки скупают золото"
    elif long_share < 30:
        return "bearish", f"{long_share:.0f}% в лонге — крупные игроки распродают"
    return "neutral", f"{long_share:.0f}% в лонге — позиции сбалансированы"


VERDICT_EMOJI = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}
VERDICT_LABEL = {"bullish": "Бычий", "bearish": "Медвежий", "neutral": "Нейтральный"}
REACTION_TAG = {"bullish": "золото растёт", "bearish": "золото падает", "neutral": "эффект смешанный"}


def build_messages():
    now_str = datetime.now(TASHKENT_TZ).strftime("%d.%m.%Y %H:%M")

    price = fetch_gold_price()
    price_str = f"${price:,.2f}" if price else "н/д"

    real_yield, real_date, real_prev = fetch_fred_series("DFII10")
    dxy, dxy_date, dxy_prev = fetch_dxy_proxy()
    cpi = fetch_cpi_yoy()
    nfp = fetch_nfp_change()
    pce = fetch_pce_yoy()
    cot = fetch_cot_gold()

    mm_long = mm_short = None
    if cot:
        try:
            mm_long = int(cot.get("m_money_positions_long_all", 0))
            mm_short = int(cot.get("m_money_positions_short_all", 0))
        except (ValueError, TypeError):
            pass

    # Классификация каждого фактора
    factors = []  # список (ключ, вердикт, причина, строка_с_описанием)

    yield_verdict = classify_yield(real_yield, real_prev)
    if yield_verdict:
        v, reason = yield_verdict
        factors.append(("Доходность облигаций", v, reason))

    dxy_verdict = classify_dxy(dxy, dxy_prev)
    if dxy_verdict:
        v, reason = dxy_verdict
        factors.append(("Доллар", v, reason))

    nfp_verdict = classify_nfp(nfp[0]) if nfp else None
    if nfp_verdict:
        v, reason = nfp_verdict
        factors.append(("Рабочие места", v, reason))

    cot_verdict = classify_cot(mm_long, mm_short)
    if cot_verdict:
        v, reason = cot_verdict
        factors.append(("Хедж-фонды", v, reason))

    # CPI и PCE — намеренно всегда нейтральные: инфляция двулика для золота
    # (давит на ставку, но золото же и защита от инфляции), не форсируем
    # ложную однозначность
    if cpi:
        cpi_val, _, cpi_prev = cpi
        factors.append(("CPI", "neutral",
                         f"{cpi_val}% (было {cpi_prev}%) — двойной эффект: давит на ставку ФРС, но золото и защита от инфляции"))
    if pce:
        pce_val, _ = pce
        factors.append(("PCE", "neutral", f"{pce_val}% — тот же двойной эффект"))

    bullish = [f for f in factors if f[1] == "bullish"]
    bearish = [f for f in factors if f[1] == "bearish"]
    neutral = [f for f in factors if f[1] == "neutral"]

    if len(bullish) > len(bearish):
        tilt_label, tilt_emoji = "БЫЧИЙ", "🟢"
    elif len(bearish) > len(bullish):
        tilt_label, tilt_emoji = "МЕДВЕЖИЙ", "🔴"
    else:
        tilt_label, tilt_emoji = "БАЛАНС", "🟡"
    strength = "СЛАБО " if abs(len(bullish) - len(bearish)) <= 1 else ""

    lines = [
        f"🥇 <b>XAUUSD — сводка</b> ({now_str}, Ташкент)",
        "",
        f"{tilt_emoji} <b>УКЛОН СЕЙЧАС: {strength}{tilt_label} ({len(bullish)} против {len(bearish)})</b>",
    ]

    if bullish:
        lines.append(f"\n🟢 <b>Бычьи ({len(bullish)}):</b>")
        for name, v, reason in bullish:
            lines.append(f"• {name} {reason} → <b>{REACTION_TAG[v]}</b>")
    if bearish:
        lines.append(f"\n🔴 <b>Медвежьи ({len(bearish)}):</b>")
        for name, v, reason in bearish:
            lines.append(f"• {name} {reason} → <b>{REACTION_TAG[v]}</b>")
    if neutral:
        lines.append(f"\n🟡 <b>Нейтральные ({len(neutral)}):</b>")
        for name, v, reason in neutral:
            lines.append(f"• {name} {reason} → {REACTION_TAG[v]}")

    lines.append(f"\n📌 Дашборд: https://topshoh.github.io/xauusd-telegram-bot/dashboard.html")
    lines.append(f"💰 Цена: <b>{price_str}</b>")
    lines.append("\nℹ️ Не финансовый совет. Подробнее — в дашборде или спросите здесь.")

    return ["\n".join(lines)]



# ============================================================
# ОТПРАВКА В TELEGRAM
# ============================================================
def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERROR] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — не могу отправить.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[ERROR] Не удалось отправить сообщение в Telegram: {e}")
        print(f"[ERROR] Ответ сервера: {getattr(e, 'response', None) and e.response.text}")
        return False


def main():
    force_test = os.environ.get("FORCE_TEST_RUN", "").lower() == "true"
    if is_market_closed() and not force_test:
        print("Рынок закрыт (выходные) — сообщение не отправляется. "
              "(Чтобы протестировать в выходной, запустите workflow с FORCE_TEST_RUN=true.)")
        return
    if force_test:
        print("⚠️ FORCE_TEST_RUN включён — игнорирую проверку на закрытый рынок (тестовый прогон).")

    # Собираем все сообщения-кандидаты: сначала напоминания (важные, идут первыми),
    # затем полную сводку — но только по понедельникам либо когда есть хотя бы
    # одно напоминание (иначе бот молчит, чтобы не спамить).
    messages = []

    event_reminders = build_event_reminders()
    messages.extend(event_reminders)

    token_reminder = build_token_hygiene_reminder()
    if token_reminder:
        messages.append(token_reminder)

    full_report_triggered = is_full_report_day() or len(event_reminders) > 0
    if full_report_triggered:
        messages.extend(build_messages())
    else:
        print("Не понедельник и нет напоминаний рядом — сегодня бот молчит (это ожидаемо, не баг).")

    if not messages:
        print("Сообщений к отправке нет.")
        return

    print(f"---- Сформировано {len(messages)} сообщений ----")
    sent_count = 0
    for i, msg in enumerate(messages, start=1):
        print(f"--- Сообщение {i}/{len(messages)} ({len(msg)} символов) ---")
        print(msg)
        ok = send_telegram_message(msg)
        if ok:
            sent_count += 1
        else:
            print(f"[ERROR] Сообщение {i} не отправилось, останавливаюсь, чтобы не слать в неправильном порядке.")
            break
        time.sleep(1.5)  # пауза между сообщениями, чтобы Telegram не посчитал это спамом
    print(f"Отправлено {sent_count} из {len(messages)} сообщений.")


if __name__ == "__main__":
    main()
