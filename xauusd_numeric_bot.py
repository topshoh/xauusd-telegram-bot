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
# СБОРКА СООБЩЕНИЯ — КОРОТКО И ПО ДЕЛУ
# (по просьбе пользователя: раньше было 7 подробных сообщений с 5-пунктовым
# разбором на каждый показатель — оказалось слишком много текста для
# ежедневного использования. Теперь один компактный пост с цифрами и
# ссылкой на дашборд, где подробности всё ещё доступны при желании.)
# ============================================================
def build_messages():
    now_str = datetime.now(TASHKENT_TZ).strftime("%d.%m.%Y %H:%M")

    price = fetch_gold_price()
    price_str = f"${price:,.2f}" if price else "н/д"

    nominal, _ = fetch_fred_series("DGS10")
    real, _ = fetch_fred_series("DFII10")
    nominal_str = f"{nominal:.2f}%" if nominal else "н/д"
    real_str = f"{real:.2f}%" if real else "н/д"

    cpi = fetch_cpi_yoy()
    cpi_str = f"{cpi[0]}%" if cpi else "н/д"

    nfp = fetch_nfp_change()
    nfp_str = f"{nfp[0]:+,}K" if nfp else "н/д"

    pce = fetch_pce_yoy()
    pce_str = f"{pce[0]}%" if pce else "н/д"

    cot = fetch_cot_gold()
    if cot:
        mm_long = cot.get("m_money_positions_long_all", "н/д")
        mm_short = cot.get("m_money_positions_short_all", "н/д")
        cot_str = f"Long {mm_long} / Short {mm_short}"
    else:
        cot_str = "н/д"

    message = (
        f"🥇 <b>XAUUSD — сводка</b> ({now_str}, Ташкент)\n"
        f"📌 Дашборд (подробности по каждой цифре): "
        f"https://topshoh.github.io/xauusd-telegram-bot/dashboard.html\n\n"
        f"💰 Цена: <b>{price_str}</b>\n"
        f"📈 Доходность 10Y: <b>{nominal_str}</b> (номинал) / <b>{real_str}</b> (реальная, TIPS)\n"
        f"🧾 CPI г/г: <b>{cpi_str}</b>\n"
        f"👷 NFP (занятость): <b>{nfp_str}</b>\n"
        f"🧾 PCE г/г: <b>{pce_str}</b>\n"
        f"📊 COT (хедж-фонды): <b>{cot_str}</b>\n\n"
        f"ℹ️ Сырые данные, без сигналов. Подробное объяснение каждой цифры "
        f"(перевод, откуда, кто считает, как влияет) — в дашборде по ссылке "
        f"выше, либо просто спросите в чате."
    )
    return [message]



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
