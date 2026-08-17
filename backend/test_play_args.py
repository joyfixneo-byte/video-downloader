"""
Самопроверка аргументов плеера: выбор звуковой дорожки.

Ловит главное, что легко сломать молча: поток должен брать ИМЕННО выбранную
дорожку и решать «копировать или перекодировать» по ЕЁ кодеку. Если смотреть,
как раньше, на кодек первой дорожки — выбор второй (eac3 при первой aac) отдал
бы браузеру звук, который тот не понимает, и вместо ошибки было бы немое видео.

Запуск:  python backend/test_play_args.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app  # noqa: E402

INFO = {"video": "hevc", "audio": "aac",
        "atracks": [{"codec": "aac"}, {"codec": "eac3"}, {"codec": "dts"}]}
SRC = Path("/tmp/film.mkv")


def main():
    args = app._input_args(SRC, 0, 2, INFO)
    assert "0:a:2?" in args, args                    # выбрана третья дорожка
    assert "libx264" in args, args                   # hevc браузер не понимает

    assert app._remux_codec_args(INFO, 0)[-2:] == ["-c:a", "copy"]   # aac как есть
    assert "aac" in app._remux_codec_args(INFO, 1)                   # eac3 → aac
    assert "aac" in app._remux_codec_args(INFO, 2)                   # dts → aac
    # Дорожки с таким номером нет (файл сменился) — не падаем, берём первую.
    assert app._remux_codec_args(INFO, 9)[-2:] == ["-c:a", "copy"]

    cmd = app._remux_cmd(SRC, 60, INFO, 1)
    assert cmd.index("-ss") < cmd.index("-i"), cmd   # перемотка без чтения файла
    assert cmd[-1] == "pipe:1", cmd

    print("Плеер: аргументы дорожек в порядке.")


if __name__ == "__main__":
    main()
