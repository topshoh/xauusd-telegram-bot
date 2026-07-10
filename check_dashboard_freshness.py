#!/usr/bin/env python3
"""
check_dashboard_freshness.py
==============================
Инструмент для предотвращения неполных обновлений дашборда — той самой
проблемы, на которую пожаловался пользователь: "прошу обновить дашборд —
одно обновилось, другое нет".

ЧТО ДЕЛАЕТ:
Сканирует dashboard.html и находит ВСЕ места, где встречается дата/метка
времени данных (паттерны "данные на...", "срез на...", "(на DD.MM...",
"на DD месяц YYYY" и т.п.). Выводит построчный список с номером строки и
контекстом.

КАК ИСПОЛЬЗОВАТЬ ПРИ "ОБНОВИ ДАШБОРД":
  1. ДО обновления: запустить скрипт, сохранить список найденных дат —
     это и есть чек-лист того, что нужно проверить/обновить.
  2. Внести правки в файл как обычно.
  3. ПОСЛЕ обновления: запустить скрипт ещё раз на новой версии файла.
  4. Сравнить два списка — если где-то осталась старая дата, которая
     должна была измениться, скрипт её покажет, и это сигнал, что блок
     забыли обновить, ДО того как это увидит пользователь.

Использование:
    python3 check_dashboard_freshness.py dashboard.html
    python3 check_dashboard_freshness.py dashboard.html --json   # машиночитаемый вывод
"""

import re
import sys
import json
from collections import defaultdict

# Паттерны, которые ловят даты/метки "свежести" данных в разных форматах,
# встречающихся в дашборде за время работы над проектом.
DATE_PATTERNS = [
    r'данные на[^<]{0,40}',
    r'срез на[^<]{0,40}',
    r'на \d{1,2}[.\s]+(?:июня|июля|июн[ья]|мая|апреля|марта)[^<]{0,20}',
    r'\(на \d{4}-\d{2}-\d{2}\)',
    r'\(на \d{1,2}\.\d{1,2}\.\d{4}[^)]{0,20}\)',
    r'report_date[^,<]{0,30}',
    r'отчёт на \d{4}-\d{2}-\d{2}',
]

COMBINED_PATTERN = re.compile('|'.join(f'(?:{p})' for p in DATE_PATTERNS), re.IGNORECASE)


def scan_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    findings = []
    for line_num, line in enumerate(lines, start=1):
        matches = COMBINED_PATTERN.findall(line)
        # findall с несколькими группами в паттерне вернёт кортежи — разворачиваем
        for m in COMBINED_PATTERN.finditer(line):
            snippet = m.group(0).strip()
            # Контекст: 40 символов до и после найденного совпадения, для понимания где это
            start = max(0, m.start() - 50)
            end = min(len(line), m.end() + 20)
            context = line[start:end].strip()
            findings.append({
                "line": line_num,
                "match": snippet,
                "context": context,
            })
    return findings


def group_by_date(findings):
    """Группирует находки по самой дате, чтобы сразу увидеть разброс дат по файлу."""
    date_re = re.compile(r'\d{1,2}[.\s]?(?:июня|июля|мая|апреля|марта|\d{1,2})[^\d,)]{0,15}\d{4}|\d{4}-\d{2}-\d{2}')
    groups = defaultdict(list)
    for item in findings:
        date_match = date_re.search(item["match"]) or date_re.search(item["context"])
        key = date_match.group(0) if date_match else "(дата не распознана автоматически)"
        groups[key].append(item)
    return groups


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 check_dashboard_freshness.py <путь_к_dashboard.html> [--json]")
        sys.exit(1)

    path = sys.argv[1]
    as_json = '--json' in sys.argv

    findings = scan_file(path)
    groups = group_by_date(findings)

    if as_json:
        print(json.dumps({"total_found": len(findings), "groups": {k: len(v) for k, v in groups.items()}, "details": findings}, ensure_ascii=False, indent=2))
        return

    print(f"Всего найдено меток даты/свежести данных: {len(findings)}")
    print(f"Уникальных дат встречается: {len(groups)}\n")

    if len(groups) > 1:
        print("⚠️  ВНИМАНИЕ: в файле встречается БОЛЬШЕ ОДНОЙ даты — это именно то,")
        print("    что нужно проверить перед тем, как считать обновление завершённым.\n")
    else:
        print("✅ Все найденные метки указывают на одну и ту же дату — расхождений нет.\n")

    print("=" * 70)
    for date_key, items in sorted(groups.items(), key=lambda x: -len(x[1])):
        print(f"\nДата «{date_key}» встречается {len(items)} раз(а):")
        for item in items:
            print(f"  строка {item['line']:5d} | ...{item['context']}...")
    print("=" * 70)


if __name__ == "__main__":
    main()
