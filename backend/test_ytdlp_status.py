"""
Самопроверка строки состояния yt-dlp (низ страницы) и флага busy.

Ловит то, на чём стоит вся схема автообновления: yt-dlp обновляется ночным
таймером, но применяется только после перезапуска сервиса — и «ждёт
перезапуска» мы определяем БЕЗ файла состояния, сравнивая импортированную
версию (что реально качает процесс) с версией на диске. Если
importlib.metadata однажды начнёт отдавать импортированное значение, строка
навсегда замрёт на «всё свежо», а пользователь будет ловить 403 и не понимать
почему. Плюс busy: скрипт обновления по нему решает, можно ли перезапускаться,
и ошибка здесь означает оборванную посреди ночи загрузку.

Запуск:  python backend/test_ytdlp_status.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app  # noqa: E402


def test_versions():
    st = app._ytdlp_status()
    assert st["running"] and st["running"] != "?", st
    assert st["installed"], st

    # Главное: версия с диска не зависит от того, что лежит в памяти процесса.
    # Именно так выглядит «таймер ночью обновил, сервис ещё на старой».
    real = app.yt_dlp.version.__version__
    try:
        app.yt_dlp.version.__version__ = "1970.1.1"
        st = app._ytdlp_status()
        assert st["running"] == "1970.1.1", st
        assert st["restart_pending"] is True, st
    finally:
        app.yt_dlp.version.__version__ = real

    # Ничего не обновлялось — перезапуск не нужен, фронт покажет «проверено ...».
    assert app._ytdlp_status()["restart_pending"] is False, app._ytdlp_status()


def test_version_normalization():
    """yt_dlp печатает «2026.07.04», а importlib.metadata — «2026.7.4».

    Сравнение этих строк напрямую (первая версия этого кода так и делала)
    держало бы на сайте вечное «обновлён, применится после перезапуска»:
    пользователь перезапускает сервис, надпись не уходит, доверие к строке
    пропадает — и настоящее обновление он однажды не заметит."""
    assert app._vparts("2026.07.04") == app._vparts("2026.7.4")
    assert app._vparts("2026.8.19") != app._vparts("2026.3.17")
    # Ночная сборка с лишним разрядом — просто другая версия, не падаем.
    assert app._vparts("2026.8.19.232946") != app._vparts("2026.8.19")


def test_stamp():
    real = app.YTDLP_STAMP
    try:
        with tempfile.TemporaryDirectory() as d:
            app.YTDLP_STAMP = Path(d) / "ytdlp_checked"
            # Таймер ещё ни разу не отрабатывал — не падаем, просто нет даты.
            assert app._ytdlp_status()["checked_at"] is None
            app.YTDLP_STAMP.touch()
            assert isinstance(app._ytdlp_status()["checked_at"], float)
    finally:
        app.YTDLP_STAMP = real


def test_busy():
    assert app._is_busy() is False, "на старте занятых задач быть не должно"
    # Обычная загрузка: рестарт её оборвёт, возобновить нечем.
    app.JOBS["t-dl"] = {"state": "downloading"}
    assert app._is_busy() is True
    del app.JOBS["t-dl"]
    # Транскрибация живёт в отдельном словаре — забыть про неё легче всего.
    app.TJOBS["t-tr"] = {"state": "transcribing"}
    assert app._is_busy() is True
    del app.TJOBS["t-tr"]
    # Готовая задача перезапуску не мешает.
    app.JOBS["t-done"] = {"state": "done"}
    assert app._is_busy() is False
    del app.JOBS["t-done"]


def main():
    test_versions()
    test_version_normalization()
    test_stamp()
    test_busy()
    print("OK: yt-dlp status + busy")


if __name__ == "__main__":
    main()
