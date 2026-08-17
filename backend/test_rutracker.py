"""
Самопроверка разбора результатов поиска rutracker.

Сети не трогает: разметку скармливаем строкой, потому что на машине без VPN
живую страницу всё равно не открыть. Падает ровно тогда, когда сломан парсер
(сменилась вёрстка сайта или правка регулярок) — а это единственное хрупкое
место в rutracker-источнике, всё остальное переиспользует торрент-пайплайн.

Запуск:  python backend/test_rutracker.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import app  # noqa: E402

# Кусок таблицы tracker.php: две раздачи + строка-шапка без ссылки на раздачу
# (её парсер обязан пропустить). Пробелы и порядок атрибутов — как на сайте.
PAGE = """
<table class="forumline tablesorter">
<tr class="row1"><th>Форум</th><th>Тема</th></tr>
<tr id="trs-tr-1" class="tCenter hl-tr">
  <td class="row1 t-title-col tt"><div class="t-title">
    <a data-topic_id="6362323" class="med tLink ts-text hl-tags bold"
       href="viewtopic.php?t=6362323">Большой фильм (2024) BDRip 1080p</a></div></td>
  <td class="row4 small nowrap tor-size"><u>3013704294</u>
    <a class="small tr-dl dl-stub" href="dl.php?t=6362323">2.81&nbsp;GB</a></td>
  <td class="row4 nowrap"><b class="seedmed" title="сиды">42</b></td>
  <td class="row4 leechmed bold">7</td>
</tr>
<tr id="trs-tr-2" class="tCenter hl-tr">
  <td class="row1 t-title-col tt"><div class="t-title">
    <a data-topic_id="777" class="med tLink" href="viewtopic.php?t=777">Мелкая
       раздача &amp; спецсимвол</a></div></td>
  <td class="row4 small nowrap tor-size"><u>524288000</u></td>
  <td class="row4 nowrap"><b class="seedmed">3</b></td>
  <td class="row4 leechmed bold">1</td>
</tr>
</table>
"""


def test_parse():
    rows = app._rt_parse_rows(PAGE)
    assert len(rows) == 2, f"ожидались 2 раздачи, разобрано {len(rows)}: {rows}"

    top = rows[0]                       # сортировка по сидам: 42 выше, чем 3
    assert top["title"] == "Большой фильм (2024) BDRip 1080p", top["title"]
    assert top["detail"] == "/viewtopic.php?t=6362323", top["detail"]
    assert top["size"] == "2.8 ГБ", top["size"]
    assert top["seeders"] == 42 and top["leechers"] == 7, top

    small = rows[1]
    # Заголовок в разметке перенесён по строкам и содержит HTML-мнемонику —
    # в списке он должен выглядеть как обычный текст в одну строку.
    assert "&amp;" not in small["title"] and "&" in small["title"], small["title"]
    assert small["size"] == "500 МБ", small["size"]   # меньше гигабайта — в МБ


def test_detail_prefix():
    # detail из результата поиска должен уводить именно в rutracker-ветку
    # _resolve_magnet, иначе раздача уедет скачиваться на 1337x.
    assert app._rt_parse_rows(PAGE)[0]["detail"].startswith(app.RT_DETAIL_PREFIX)


def test_decode_cp1251():
    # Сайт отдаёт windows-1251: без явного декодирования кириллица в названиях
    # раздач превращается в мусор.
    assert app._rt_decode("Раздача".encode("cp1251")) == "Раздача"


def test_state_without_account():
    # Логин/пароль не заданы — сеть не трогаем вовсе, фронт покажет плашку
    # «аккаунт не настроен», а не «нет VPN».
    saved = app.RUTRACKER_LOGIN, app.RUTRACKER_PASSWORD
    app.RUTRACKER_LOGIN = app.RUTRACKER_PASSWORD = ""
    try:
        assert app._rt_probe() == "no_account"
    finally:
        app.RUTRACKER_LOGIN, app.RUTRACKER_PASSWORD = saved


if __name__ == "__main__":
    test_parse()
    test_detail_prefix()
    test_decode_cp1251()
    test_state_without_account()
    print("Разбор результатов rutracker в порядке: 2 раздачи, размеры, "
          "сортировка по сидам, detail-путь, cp1251.")
