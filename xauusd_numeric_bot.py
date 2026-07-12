"""
xauusd_numeric_bot.py
======================
Полностью автономный бот: тянет ТОЛЬКО реальные цифры из настоящих
бесплатных API (без ИИ в контуре, без риска выдумать данные) и шлёт
сводку в Telegram. Рассчитан на запуск по расписанию через GitHub Actions
(cron), но можно запускать и вручную для проверки.

ЧТО ЭТОТ БОТ УМЕЕТ САМ, БЕЗ ЧЕЛОВЕКА И БЕЗ ИИ:
  - Живая цена золота (gold-api.com)
  - Номинальная и реальная (TIPS) доходность 10-летних облигаций (FRED)
  - Последние факт/прогноз по CPI, NFP, PCE (BLS + BEA)
  - Последний отчёт COT по золоту (CFTC, Socrata API)

ЧЕГО ЭТОТ БОТ НЕ ДЕЛАЕТ (осознанно, чтобы не выдумывать):
  - Не считает "вероятность решения ФРС" (нет бесплатного API у CME FedWatch)
  - Не пишет вердикт/анализ/интерпретацию — только сырые цифры с подписанными
    источниками. Синтез "бычий/медвежий счёт" по-прежнему делает человек
    через чат с Claude, либо это отдельная (платная) надстройка поверх
    этого бота, если её решат добавить позже.

НУЖНЫЕ СЕКРЕТЫ (задаются в GitHub Secrets репозитория, см. ниже список):
  TELEGRAM_BOT_TOKEN   - токен бота от @BotFather
  TELEGRAM_CHAT_ID     - куда слать (личный чат или канал)
  FRED_API_KEY         - бесплатный ключ https://fred.stlouisfed.org/docs/api/api_key.html
  BLS_API_KEY          - бесплатный ключ https://www.bls.gov/developers/home.htm (не обязателен,
                          но без ключа лимит намного ниже и без него часто падает)
  BEA_API_KEY          - бесплатный ключ https://apps.bea.gov/api/signup/

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
BEA_API_KEY = os.environ.get("BEA_API_KEY")

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


def is_market_closed() -> bool:
    """Рынок золота закрыт с субботы 02:00 до понедельника 03:00 по Ташкенту
    (тот же паттерн, что и в старых ботах проекта — см. project_context.md)."""
    now = datetime.now(TASHKENT_TZ)
    wd, hr = now.weekday(), now.hour  # 0=Пн ... 5=Сб, 6=Вс
    if wd == 5 and hr >= 2:
        return True
    if wd == 6:
        return True
    if wd == 0 and hr < 3:
        return True
    return False


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
# 2. ДОХОДНОСТИ ОБЛИГАЦИЙ (FRED — официальный API Федерального резервного банка Сент-Луиса)
#    DGS10  = номинальная доходность 10-летних Treasury
#    DFII10 = реальная доходность 10-летних TIPS
# ============================================================
def fetch_fred_series(series_id: str):
    if not FRED_API_KEY:
        print(f"[WARN] FRED_API_KEY не задан, пропускаю {series_id}")
        return None, None
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        if not obs:
            return None, None
        latest = obs[0]
        value = latest.get("value")
        date = latest.get("date")
        if value == ".":  # FRED так помечает пропуски (выходные/праздники)
            return None, date
        return float(value), date
    except Exception as e:
        print(f"[WARN] Не удалось получить {series_id} из FRED: {e}")
        return None, None


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
    так как BLS отдаёт индекс, а не готовый % изменения."""
    points = fetch_bls_series("CUUR0000SA0", n_points=13)
    if len(points) < 13:
        return None
    latest = float(points[0]["value"])
    year_ago = float(points[12]["value"])
    yoy = (latest - year_ago) / year_ago * 100
    period_label = f"{points[0]['periodName']} {points[0]['year']}"
    return round(yoy, 2), period_label


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
    """PCE год к году. Таблица T20804 отдаёт уровень индекса цен (не готовый
    %), поэтому берём точку сейчас и точку 12 месяцев назад и считаем %
    сами — тем же способом, что и для CPI выше. LineNumber="1" — это
    headline PCE (расходы в целом, не Core). ВАЖНО: как и с NFP, я не смог
    протестировать это вживую без вашего реального BEA-ключа — при первом
    запуске сверьте результат с официальной цифрой (например, с сайта
    bea.gov) и напишите мне, если разойдётся."""
    if not BEA_API_KEY:
        print("[WARN] BEA_API_KEY не задан, пропускаю PCE")
        return None
    url = "https://apps.bea.gov/api/data"
    params = {
        "UserID": BEA_API_KEY,
        "method": "GetData",
        "datasetname": "NIPA",
        "TableName": "T20804",
        "LineNumber": "1",
        "Frequency": "M",
        "Year": "X",
        "ResultFormat": "JSON",
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        rows = data["BEAAPI"]["Results"]["Data"]
        rows_sorted = sorted(rows, key=lambda x: x["TimePeriod"], reverse=True)
        if len(rows_sorted) < 13:
            return None
        latest_val = float(rows_sorted[0]["DataValue"].replace(",", ""))
        year_ago_val = float(rows_sorted[12]["DataValue"].replace(",", ""))
        yoy = (latest_val - year_ago_val) / year_ago_val * 100
        period_label = rows_sorted[0]["TimePeriod"]
        return round(yoy, 2), period_label
    except Exception as e:
        print(f"[WARN] Не удалось получить PCE из BEA: {e}")
        return None


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
    """Раз в месяц (1 числа) — напоминание проверить GitHub-токен."""
    today = datetime.now(TASHKENT_TZ).date()
    if today.day == 1:
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
# СБОРКА СООБЩЕНИЙ — "ДЛЯ ШКОЛЬНИКА", ПО 5 ПУНКТАМ НА КАЖДЫЙ ПОКАЗАТЕЛЬ
# (перевод названия → откуда цифра и что значит → кто считает и как →
#  из чего состоит расчёт → как влияет на золото)
# Возвращает СПИСОК сообщений, а не одну строку — иначе не влезет в лимит
# Telegram на одно сообщение (4096 символов).
# ============================================================
def build_messages():
    now_str = datetime.now(TASHKENT_TZ).strftime("%d.%m.%Y %H:%M")
    messages = []

    # ---------- Сообщение 0: заголовок-шапка ----------
    messages.append(
        f"🥇 <b>XAUUSD — автосводка цифр</b> ({now_str}, Ташкент)\n\n"
        f"📌 Полный дашборд (всегда актуальный): "
        f"https://topshoh.github.io/xauusd-telegram-bot/dashboard.html\n\n"
        f"Дальше — {7} отдельных сообщений, по одному на каждый показатель. "
        f"В каждом: что это значит простыми словами, откуда цифра, кто её "
        f"считает и как именно она влияет на золото. Это сырые данные из "
        f"открытых источников, без сигналов на покупку/продажу."
    )

    # ---------- 1. Цена золота ----------
    price = fetch_gold_price()
    price_str = f"${price:,.2f}" if price else "н/д"
    messages.append(
        f"💰 <b>Цена золота: {price_str}</b>\n\n"
        f"<b>1. Перевод названия:</b> XAU/USD — биржевой код. XAU = золото "
        f"(AU — «аурум», «золото» на латыни), USD — доллары США. Значит "
        f"«сколько долларов стоит одна унция золота».\n\n"
        f"<b>2. Откуда цифра:</b> живая котировка с биржи через сервис "
        f"gold-api.com, обновляется в реальном времени, без задержек.\n\n"
        f"<b>3. Кто считает:</b> это агрегатор реальных сделок на мировом "
        f"рынке золота (Лондон, COMEX) — не расчётный показатель, а прямая "
        f"рыночная цена.\n\n"
        f"<b>4. Из чего состоит:</b> цена одной тройской унции (31.1 грамма) "
        f"золота в долларах прямо сейчас.\n\n"
        f"<b>5. Как влияет на золото:</b> это и есть сам предмет анализа — "
        f"все остальные цифры в этой сводке объясняют, ПОЧЕМУ цена такая, "
        f"а не наоборот."
    )

    # ---------- 2. Доходности облигаций ----------
    nominal, nominal_date = fetch_fred_series("DGS10")
    real, real_date = fetch_fred_series("DFII10")
    nominal_str = f"{nominal:.2f}%" if nominal else "н/д"
    real_str = f"{real:.2f}%" if real else "н/д"
    messages.append(
        f"📈 <b>Доходность облигаций США (10 лет)</b>\n"
        f"Номинальная: <b>{nominal_str}</b> (на {nominal_date})\n"
        f"Реальная (с поправкой на инфляцию): <b>{real_str}</b> (на {real_date})\n\n"
        f"<b>1. Перевод названия:</b> «10Y» = облигация США сроком на 10 лет "
        f"(государство берёт у вас деньги в долг на 10 лет). «Доходность» — "
        f"сколько % годовых вам за это платят. «Реальная» (TIPS) — та же "
        f"облигация, но защищённая от инфляции: реальный % = номинальный "
        f"минус ожидаемая инфляция.\n\n"
        f"<b>2. Откуда цифра:</b> FRED — база данных Федерального резервного "
        f"банка Сент-Луиса (это часть системы ФРС США, официальный "
        f"источник).\n\n"
        f"<b>3. Кто считает:</b> Минфин США выпускает облигации и продаёт их "
        f"на аукционах, дальше ими торгуют на бирже — доходность считается "
        f"по текущей рыночной цене облигации.\n\n"
        f"<b>4. Из чего состоит:</b> % годовых, который получит инвестор, "
        f"если купит облигацию сегодня по рыночной цене и будет держать её "
        f"10 лет до погашения.\n\n"
        f"<b>5. Как влияет на золото:</b> золото само по себе не платит "
        f"процентов — просто лежит и может дорожать или дешеветь. Если "
        f"облигации начинают платить много % — часть инвесторов перекладывает "
        f"деньги туда, а не в золото → доходность растёт = давит на золото "
        f"вниз. Особенно важна именно РЕАЛЬНАЯ доходность — она самый "
        f"честный конкурент золота, потому что тоже не съедается инфляцией."
    )

    # ---------- 3. CPI ----------
    cpi = fetch_cpi_yoy()
    cpi_str = f"{cpi[0]}%" if cpi else "н/д"
    cpi_period = cpi[1] if cpi else ""
    messages.append(
        f"🧾 <b>CPI (инфляция №1): {cpi_str}</b> ({cpi_period})\n\n"
        f"<b>1. Перевод названия:</b> CPI = Consumer Price Index = «индекс "
        f"потребительских цен» — насколько подорожала обычная жизнь: еда, "
        f"аренда, бензин, услуги.\n\n"
        f"<b>2. Откуда цифра:</b> BLS — Bureau of Labor Statistics, "
        f"«Бюро статистики труда США», официальное государственное "
        f"агентство.\n\n"
        f"<b>3. Кто считает:</b> сотрудники BLS каждый месяц вручную и "
        f"автоматически собирают цены на сотни товаров и услуг в разных "
        f"городах США.\n\n"
        f"<b>4. Из чего состоит:</b> сравнивают корзину цен сейчас с той же "
        f"корзиной год назад — получают % изменения («год к году»).\n\n"
        f"<b>5. Как влияет на золото:</b> высокая инфляция обычно означает, "
        f"что ФРС будет повышать (или дольше держать высокой) процентную "
        f"ставку, чтобы её сбить — а высокая ставка давит на золото (см. "
        f"пункт про доходность облигаций выше). Но у золота есть и обратный "
        f"эффект: его исторически покупают именно КАК защиту от инфляции — "
        f"поэтому реакция золота на CPI не всегда однозначная, зависит от "
        f"контекста."
    )

    # ---------- 4. NFP ----------
    nfp = fetch_nfp_change()
    nfp_str = f"{nfp[0]:+,}K" if nfp else "н/д"
    nfp_period = nfp[1] if nfp else ""
    messages.append(
        f"👷 <b>NFP (рынок труда): {nfp_str}</b> ({nfp_period})\n\n"
        f"<b>1. Перевод названия:</b> NFP = Non-Farm Payrolls = «число новых "
        f"рабочих мест вне сельского хозяйства» — главный ежемесячный "
        f"градусник экономики США.\n\n"
        f"<b>2. Откуда цифра:</b> тот же BLS, что считает и CPI — это разные "
        f"отчёты одного и того же государственного агентства.\n\n"
        f"<b>3. Кто считает:</b> BLS опрашивает десятки тысяч работодателей "
        f"по всей стране (кроме ферм — отсюда «non-farm»), сколько людей у "
        f"них работает.\n\n"
        f"<b>4. Из чего состоит:</b> сравнивают общее число занятых сейчас с "
        f"прошлым месяцем — разница в тысячах человек и есть NFP.\n\n"
        f"<b>5. Как влияет на золото:</b> сильный отчёт (много новых рабочих "
        f"мест) = экономика в порядке = у ФРС больше поводов держать ставку "
        f"высокой или повышать её → давит на золото вниз. Слабый отчёт "
        f"(мало рабочих мест) = экономика буксует = ФРС может смягчить "
        f"политику → обычно поддерживает золото вверх."
    )

    # ---------- 5. PCE ----------
    pce = fetch_pce_yoy()
    pce_str = f"{pce[0]}%" if pce else "н/д"
    pce_period = pce[1] if pce else ""
    messages.append(
        f"🧾 <b>PCE (инфляция №2, любимая у ФРС): {pce_str}</b> ({pce_period})\n\n"
        f"<b>1. Перевод названия:</b> PCE = Personal Consumption "
        f"Expenditures = «расходы населения на личное потребление» — ещё "
        f"один способ измерить инфляцию, похожий на CPI, но по-другому "
        f"устроенный.\n\n"
        f"<b>2. Откуда цифра:</b> BEA — Bureau of Economic Analysis, "
        f"«Бюро экономического анализа США». Это другое ведомство, не BLS.\n\n"
        f"<b>3. Кто считает:</b> BEA смотрит не на фиксированную корзину "
        f"товаров (как CPI), а на то, что люди РЕАЛЬНО покупали в отчётном "
        f"месяце — корзина «плавает» вслед за поведением покупателей.\n\n"
        f"<b>4. Из чего состоит:</b> % изменения цен год к году, как и CPI, "
        f"но с другими весами товаров.\n\n"
        f"<b>5. Как влияет на золото:</b> логика та же, что и у CPI (высокая "
        f"инфляция обычно давит на золото через ожидания по ставке), но "
        f"именно PCE — это тот показатель, на который ФРС официально "
        f"ориентируется при решениях по ставке (не CPI!). Поэтому сюрприз в "
        f"PCE обычно двигает золото сильнее, чем такой же сюрприз в CPI."
    )

    # ---------- 6. COT ----------
    cot = fetch_cot_gold()
    if cot:
        report_date = cot.get("report_date_as_yyyy_mm_dd", "н/д")[:10]
        mm_long = cot.get("m_money_positions_long_all", "н/д")
        mm_short = cot.get("m_money_positions_short_all", "н/д")
    else:
        report_date, mm_long, mm_short = "н/д", "н/д", "н/д"
    messages.append(
        f"📊 <b>COT — позиции хедж-фондов (отчёт на {report_date})</b>\n"
        f"Managed Money: Long {mm_long} / Short {mm_short}\n\n"
        f"<b>1. Перевод названия:</b> COT = Commitments of Traders = "
        f"«обязательства трейдеров» — отчёт о том, кто и сколько поставил "
        f"на рост или падение золота через фьючерсы. Managed Money = "
        f"профессиональные управляющие деньгами (хедж-фонды).\n\n"
        f"<b>2. Откуда цифра:</b> CFTC — Commodity Futures Trading "
        f"Commission, государственный регулятор товарных бирж США.\n\n"
        f"<b>3. Кто считает:</b> сами биржи обязаны по закону сообщать CFTC "
        f"о крупных позициях трейдеров; CFTC собирает это и публикует сводку "
        f"каждую пятницу.\n\n"
        f"<b>4. Из чего состоит:</b> Long — сколько контрактов «на рост» "
        f"держат хедж-фонды прямо сейчас, Short — сколько контрактов "
        f"«на падение».\n\n"
        f"<b>5. Как влияет на золото:</b> если Long намного больше Short — "
        f"фонды массово верят в рост золота, это бычий сигнал. Но есть "
        f"обратная сторона: если Long УЖЕ огромный (все, кто хотел купить, "
        f"уже купили) — расти дальше особо некому, и риск разворота вниз "
        f"растёт. Поэтому важна не только цифра, но и то, насколько она "
        f"близка к историческим экстремумам."
    )

    return messages



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
