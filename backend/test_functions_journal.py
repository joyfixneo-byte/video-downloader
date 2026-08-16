"""
Самопроверка журнала функций (docs/FUNCTIONS.md).

Ловит четыре вида рассинхрона:
  1. ручка есть в app.py, но её забыли записать в журнал;
  2. журнал ссылается на ручку, которой в коде уже нет (протух);
  3. в журнале есть функция фронта, которой в index.html уже нет;
  4. ручка описана, но фронт её не вызывает — «сделано, но не применяется
     в системе» (можно осознанно отключить: начать ячейку UI со слова «нет»).

Запуск:  python backend/test_functions_journal.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "backend" / "app.py"
FRONT = ROOT / "backend" / "static" / "index.html"
JOURNAL = ROOT / "docs" / "FUNCTIONS.md"

ROUTE = re.compile(r"^(GET|POST|PUT|DELETE) (/api/\S+)$")
FUNC = re.compile(r"^[A-Za-z_][\w.]*\(\)$|^[A-Z][A-Z_]+$")   # foo() или GAME


def code_routes():
    """Все ручки, объявленные в app.py."""
    src = APP.read_text(encoding="utf-8")
    return {(m.group(1).upper(), m.group(2)) for m in
            re.finditer(r"@app\.(get|post|put|delete)\(\"([^\"]+)\"", src)}


def journal_rows():
    """Строки таблиц журнала: (функция, [токены точки входа], ячейка UI)."""
    rows = []
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 3 or cells[0] in ("Функция", "") or set(cells[1]) <= set("-: "):
            continue                       # шапка/разделитель таблицы
        rows.append((cells[0], re.findall(r"`([^`]+)`", cells[1]), cells[2]))
    return rows


def used_in_frontend(path, front):
    """Фронт вызывает ручку, если встречаются все её литеральные куски.

    Пути с параметром фронт склеивает из частей ("/api/torrent/" + id +
    "/pause"), поэтому целиком строка в index.html не встречается никогда.
    """
    return all(part in front for part in re.split(r"\{[^}]+\}", path) if part)


def main():
    front = FRONT.read_text(encoding="utf-8")
    rows = journal_rows()
    errors = []

    if not rows:
        errors.append("журнал пуст — не разобралась ни одна строка таблицы")

    in_journal = set()
    for name, entries, ui in rows:
        if not entries:
            errors.append(f"«{name}»: пустая колонка «Точка входа»")
        if not ui:
            errors.append(f"«{name}»: пустая колонка «Где в интерфейсе» — "
                          "непонятно, как этим пользоваться")
        opted_out = ui.lower().startswith("нет")
        for token in entries:
            m = ROUTE.match(token)
            if m:
                route = (m.group(1), m.group(2))
                in_journal.add(route)
                if route not in code_routes():
                    errors.append(f"«{name}»: журнал протух — ручки {token} "
                                  "в app.py больше нет")
                elif not opted_out and not used_in_frontend(m.group(2), front):
                    errors.append(f"«{name}»: {token} нигде не вызывается из "
                                  "index.html — сделано, но не применяется")
            elif FUNC.match(token):
                # функция может быть и фронтовой, и бэкендовой — ищем в обоих
                if token.rstrip("()") not in front + APP.read_text(encoding="utf-8"):
                    errors.append(f"«{name}»: функции {token} больше нет "
                                  "ни в index.html, ни в app.py")
            else:
                errors.append(f"«{name}»: непонятный токен точки входа {token!r} "
                              "(ожидается `GET /api/…` или `имяФункции()`)")

    for method, path in sorted(code_routes() - in_journal):
        errors.append(f"{method} {path} есть в app.py, но не записана в журнал "
                      "— добавьте строку в docs/FUNCTIONS.md")

    if errors:
        print("Журнал функций разошёлся с кодом:\n")
        for e in errors:
            print("  -", e)
        print(f"\nвсего проблем: {len(errors)}")
        return 1

    print(f"Журнал функций в порядке: {len(rows)} записей, "
          f"{len(in_journal)} ручек, все вызываются из интерфейса.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
