# 🎬 Video Downloader

Мини-сайт для скачивания видео по ссылке. Вставляешь ссылку на страницу с
плеером → выбираешь серию (если это сериал/плейлист) и качество → получаешь
готовый файл `.mp4` (или `.mp3`).

Под капотом — [yt-dlp](https://github.com/yt-dlp/yt-dlp) (поддерживает ~1800
сайтов) + ffmpeg для склейки видео и звука. Бэкенд на FastAPI.

## Возможности
- Распознаёт плейлисты → выбор конкретных серий галочками
- Выбор качества: лучшее / 1080p / 720p / 480p / только звук (mp3)
- Прогресс скачивания в реальном времени
- Нет лимита на размер файла (в отличие от Telegram)
- **Транскрибация видео в текст** — кнопка «📝 Транскрибация»: сначала берутся
  готовые субтитры сайта, а если их нет — речь распознаётся локально через
  Whisper (бесплатно, на CPU). Текст выводится прямо на странице с кнопкой
  «Копировать».
- Необязательный пароль на вход

## Транскрибация
Кнопка «📝 Транскрибация» рядом с «Скачать»:
1. Сначала пробуем готовые субтитры сайта (через yt-dlp) — мгновенно, без нагрузки.
2. Если субтитров нет — скачиваем аудио и распознаём его локально моделью
   [Whisper](https://github.com/SYSTRAN/faster-whisper) на CPU. Первый запуск
   скачает веса модели (~150 МБ для `base`), дальше работает офлайн.

Размер модели задаётся переменной окружения `WHISPER_MODEL`
(`tiny` / `base` / `small` / `medium`). По умолчанию `base` — компромисс
скорости и качества. Для русского точнее `small`, но он медленнее на CPU.
Распознавание ролика занимает от десятков секунд до нескольких минут —
зависит от длины видео, модели и мощности сервера.

## Установка на Ubuntu-сервер

```bash
git clone <URL-репозитория> video-downloader
cd video-downloader
sudo bash deploy/install.sh
```

Скрипт поставит python, ffmpeg, зависимости и запустит сервис на `127.0.0.1:8000`
через systemd.

### Открыть наружу через nginx
```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/video-downloader
# отредактируй server_name в файле (домен или IP)
sudo ln -s /etc/nginx/sites-available/video-downloader /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### Пароль доступа
Открой `/etc/systemd/system/video-downloader.service`, раскомментируй и задай:
```
Environment=DOWNLOADER_PASSWORD=твой_пароль
```
затем:
```bash
sudo systemctl daemon-reload && sudo systemctl restart video-downloader
```

## Просмотр на телевизоре / Apple TV без ПК (по локальной сети)

Сервер стоит в той же сети, что и телевизор, поэтому готовые файлы не нужно
гонять через VPS — их можно отдавать по локалке на полной скорости. Два уровня:

### 1. Доступ к сайту по локальной сети (мимо VPS)
На хосте Hyper-V (SRV-HOST) пробрасываем порт с локальной сети прямо в VM
(PowerShell от администратора; подставь свои адреса хоста и VM):
```powershell
netsh interface portproxy add v4tov4 listenaddress=192.168.88.251 listenport=8000 connectaddress=10.10.0.31 connectport=8000
New-NetFirewallRule -DisplayName "video-dl LAN 8000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -RemoteAddress 192.168.88.0/24
```
Приложение должно слушать `0.0.0.0:8000` (уже так в `deploy/video-downloader.service`).
Теперь сайт открывается по `http://192.168.88.251:8000` — отдача идёт по локалке.
Прямую ссылку на файл `http://192.168.88.251:8000/api/file/<job_id>` можно вставить
в VLC на Apple TV («Сеть» → «Открыть сетевой поток»).

### 2. SMB-библиотека: телевизор видит готовые файлы списком
Все обычные загрузки и завершённые торренты сохраняются в общей SMB-библиотеке.
Рабочие данные и незавершённые файлы находятся в скрытом каталоге
`SHARE_DIR/.staging`; после завершения файл атомарно переименовывается в корень
SMB. Постоянной копии в `downloads/` нет. Веб-страница показывает задачи и
готовые файлы в одной карточке «Библиотека SMB».

**На SRV-HOST** (PowerShell от администратора; пароль придумай свой):
```powershell
$base = "S:\VideoShare"
New-Item -ItemType Directory -Force $base | Out-Null
net user videoshare 'ПоменяйЭтотПароль1!' /add
icacls $base /grant "videoshare:(OI)(CI)M" | Out-Null
New-SmbShare -Name "VideoShare" -Path $base -FullAccess "videoshare"
# SMB с локалки (для Apple TV) и из внутренней сети VM (для монтирования):
New-NetFirewallRule -DisplayName "SMB LAN 445" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 445 -RemoteAddress 192.168.88.0/24,10.10.0.0/24
```

**На VM-apps-vpn** (хост виден из VM как `10.10.0.1`):
```bash
sudo apt-get install -y cifs-utils
sudo mkdir -p /mnt/videoshare
printf 'username=videoshare\npassword=ПоменяйЭтотПароль1!\n' | sudo tee /root/.videoshare-cred >/dev/null
sudo chmod 600 /root/.videoshare-cred
echo '//10.10.0.1/VideoShare /mnt/videoshare cifs credentials=/root/.videoshare-cred,uid=__USER__,gid=__USER__,iocharset=utf8,vers=3.0,nofail,x-systemd.automount 0 0' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo mount -a
touch /mnt/videoshare/_test && rm /mnt/videoshare/_test && echo "Запись в шару работает"
```
Затем в `/etc/systemd/system/video-downloader.service` раскомментируй
`Environment=SHARE_DIR=/mnt/videoshare` и `sudo systemctl daemon-reload && sudo systemctl restart video-downloader`.
Убедись, что сервисный пользователь может писать в `/mnt/videoshare` и что это
точка монтирования, а не локальная папка VM.

**На Apple TV:** VLC → «Сеть» → «Подключиться к серверу» → `smb://192.168.88.251`,
логин `videoshare` и пароль. Папка `VideoShare` — список готовых файлов.

> Готовые файлы в библиотеке не удаляются автоматически. Управлять ими можно
> на сайте в карточке **«Библиотека SMB»**: посмотреть, скачать или удалить.
> Незавершённые задачи хранятся в `.staging`; торрент-метаданные остаются там
> для возобновления и докачки.

## Личный Telegram-бот для YouTube (только VM-apps-vpn)

Бот принимает ссылку на одно видео YouTube, ставит в очередь существующего `/api/download` с качеством `best`, следит за `/api/status` и отправляет результат в тот же чат. Кнопка «Остановить» вызывает `/api/cancel`. Доступ разрешён только одному Telegram user ID. Индекс canonical YouTube ID хранится в `backend/data/telegram_bot.sqlite3`; при совпадении названия со старым файлом без индекса бот сообщает о возможном дубле и не скачивает его.

1. Создай бота через `@BotFather` и узнай свой числовой Telegram user ID.
2. На **apps-vpn** создай закрытый env-файл, заменив значения:

   ```bash
   sudo install -d -m 700 /etc/video-downloader
   sudo install -m 600 /dev/null /etc/video-downloader/telegram.env
   sudo sh -c 'cat > /etc/video-downloader/telegram.env' <<'EOF'
   TELEGRAM_BOT_TOKEN=токен_от_BotFather
   TELEGRAM_ALLOWED_ID=твой_числовой_Telegram_ID
   # Укажи, только если у веб-сервиса настроен пароль:
   # DOWNLOADER_PASSWORD=тот_же_пароль_что_в_video-downloader.service
   EOF
   ```

3. Убедись, что на этой VM уже настроен `SHARE_DIR=/mnt/videoshare`, SMB смонтирован, и веб-сервис отвечает на `http://127.0.0.1:8000/api/library`. Установщик кладёт unit worker-а, но не запускает его. Включи его только на VM-apps-vpn:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now video-downloader-telegram
   sudo systemctl status video-downloader-telegram
   sudo journalctl -u video-downloader-telegram -f
   ```

4. Отправь боту `/start`, затем ссылку на YouTube. Проверь готовый файл в SMB и сообщение о результате. Повтори ту же ссылку: повторная загрузка не должна стартовать. Для проверки отмены отправь другое видео и нажми «Остановить».

Не включай `video-downloader-telegram` на VM-apps с прямым egress. После изменения токена или ID обнови env-файл и выполни `sudo systemctl restart video-downloader-telegram`.

## Полезные команды
```bash
systemctl status video-downloader      # статус
journalctl -u video-downloader -f      # логи
sudo systemctl restart video-downloader
```

## Обновление yt-dlp
YouTube меняет защиту ссылок почти каждую неделю, и устаревший yt-dlp начинает
отдавать `HTTP Error 403: Forbidden` — причём разбор ссылки проходит, заголовок
видео виден, а падает уже само скачивание.

Обновлять руками не нужно: `install.sh` ставит systemd-таймер
`video-downloader-ytdlp.timer`, который каждую ночь в 04:00 запускает
`deploy/ytdlp-update.sh`. Скрипт ставит свежий yt-dlp и перезапускает сервис —
но только если в этот момент ничего не качается и не распознаётся (иначе
перезапуск откладывается до следующей проверки). Тот же скрипт вызывается из
`vdup`.

Какая версия работает сейчас и когда была последняя проверка — видно строкой
в самом низу страницы сайта.

Проверить таймер и посмотреть, что он делал:
```bash
systemctl list-timers | grep ytdlp
journalctl -u video-downloader-ytdlp -n 30
```

Обновить прямо сейчас, не дожидаясь ночи:
```bash
sudo /home/$USER/video-downloader/deploy/ytdlp-update.sh
```
`sudo` обязателен: `.venv` создан установщиком от root, и обычный `pip`
упирается в `Permission denied`.

## Сторож: сайт поднимается сам

Раньше «502 Bad Gateway» лечился руками. Причина неочевидная: процесс может
зависнуть, **не упав**, — все ручки приложения синхронные и крутятся в общем
пуле из 40 потоков, и одного залипшего ffprobe, мёртвого SMB-маунта или
скрейпера трекера хватает, чтобы пул кончился. Для systemd сервис при этом
живой (`Restart=on-failure` не срабатывает), а снаружи сайт мёртв.

Поэтому `install.sh` ставит таймер `video-downloader-watchdog.timer`, который
раз в минуту запускает `deploy/watchdog.sh`. Тот дёргает `/api/config` (ручка
без пароля, но трогает и блокировки задач, и диск — значит отвечает только
по-настоящему работающее приложение) и после **трёх провалов подряд** чинит:

| Что видит сторож | Что делает |
|---|---|
| локально `/api/config` не отвечает | `systemctl restart video-downloader` |
| локально отвечает, а снаружи нет | перезапускает frp-туннель (юнит ищет по маске `frp*.service`) |

Худший случай — сайт лежит ~3 минуты. Занятость сервера сторож не спрашивает
(в отличие от ночного обновления yt-dlp): раз сайт отдаёт 502, управлять
загрузкой всё равно нельзя. Торренты после перезапуска поднимаются сами,
обычная yt-dlp-загрузка теряется.

Посмотреть, что он делал:
```bash
journalctl -u video-downloader-watchdog -n 50 --no-pager
systemctl list-timers | grep watchdog
```

### Внешняя проба (обрыв туннеля) — включается вручную
Внешний адрес разный на двух машинах (`:8000` у apps-vpn, `:8002` у apps), а
файл юнита один, поэтому по умолчанию проверяется только локальный сайт.
Сначала убедись, что VM вообще ходит на свой внешний адрес:
```bash
curl -s -o /dev/null -w '%{http_code}
' -m 15 http://91.186.220.103:8002/api/config
```
Если `200` — раскомментируй в `/etc/systemd/system/video-downloader-watchdog.service`
строку `Environment=WATCHDOG_PUBLIC_URL=...` со своим портом и сделай
`sudo systemctl daemon-reload`. Если не `200` — не включай: сторож примет это за
обрыв туннеля и будет зря перезапускать frpc.

Проверить логику сторожа без сервера (гоняется и на Windows в Git Bash):
```bash
bash deploy/test_watchdog.sh
```

## Важно
Скачивай для личного офлайн-просмотра то, что доступно легально и бесплатно.
Уважай авторские права и правила сайтов. Контент с DRM-защитой (платные
стриминги) не скачивается — это by design.
