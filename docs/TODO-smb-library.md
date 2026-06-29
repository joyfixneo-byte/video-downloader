# TODO: включить управление SMB-библиотекой с сайта

**Статус:** код готов, на проде не включено.
**Цель:** на сайте появляется карточка «📚 Библиотека (SMB для телевизора)»
со списком файлов и кнопками «Скачать» / «Удалить».

## Почему сейчас не видно

Карточка скрыта намеренно: фронт прячет её, если `/api/library` отвечает
`enabled:false`. А это значит, что на VM **apps-vpn** не задана переменная
окружения `SHARE_DIR` (и/или не примонтирована SMB-шара). Это не баг кода —
витрина выключена по умолчанию, включать только после проверки монтирования
(иначе копии лягут на локальный диск VM и забьют его).

Связанный код:
- `backend/app.py` — `SHARE_DIR`/`SHARE_PATH`, `_publish_to_share()`,
  эндпоинты `/api/library`, `/api/library/file`, `/api/library/delete`.
- `backend/static/index.html` — `loadLibrary()` (карточка `#libraryCard`).
- Полная инструкция по настройке: `README.md`, раздел «SMB-витрина».

## Шаги (на VM, по SSH)

1. **На SRV-HOST (Windows):** создать шару `\\192.168.88.251\VideoShare`
   и учётку `videoshare`, если ещё нет (см. README, раздел SMB).
2. **На VM (`ssh kevink@vm-apps-vpn`)** — смонтировать cifs:
   ```bash
   sudo apt-get install -y cifs-utils
   sudo mkdir -p /mnt/videoshare
   printf 'username=videoshare\npassword=ВАШ_ПАРОЛЬ\n' | sudo tee /root/.videoshare-cred >/dev/null
   sudo chmod 600 /root/.videoshare-cred
   echo '//10.10.0.1/VideoShare /mnt/videoshare cifs credentials=/root/.videoshare-cred,uid=kevink,gid=kevink,iocharset=utf8,vers=3.0,nofail,x-systemd.automount 0 0' | sudo tee -a /etc/fstab
   sudo systemctl daemon-reload && sudo mount -a
   ```
3. **Проверить запись (обязательно!):**
   ```bash
   touch /mnt/videoshare/_test && rm /mnt/videoshare/_test && echo OK
   ```
   Нет `OK` — остановиться, дальше не идти.
4. **Включить переменную** в `/etc/systemd/system/video-downloader.service`,
   секция `[Service]`:
   ```
   Environment=SHARE_DIR=/mnt/videoshare
   ```
   затем:
   ```bash
   sudo systemctl daemon-reload && sudo systemctl restart video-downloader
   ```
5. Обновить сайт — карточка «📚 Библиотека» должна появиться.
   Apple TV: VLC → «Сеть» → `smb://192.168.88.251`.

## Готово, когда

- [ ] Шара примонтирована, тест записи прошёл.
- [ ] `SHARE_DIR` задан, сервис перезапущен.
- [ ] На сайте видна карточка «📚 Библиотека», список/скачать/удалить работают.
- [ ] На готовом файле есть кнопка «📚 В библиотеку», по ней файл копируется
      в витрину (`/api/library/add` → `_publish_to_share`). Автоматически при
      скачивании файлы в витрину больше не копируются — только вручную.
