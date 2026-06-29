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

### 2. SMB-витрина: телевизор видит файлы списком
Готовые файлы автоматически складываются в папку-витрину на хосте, расшаренную по
SMB. Apple TV в VLC просто открывает её и выбирает файл — без ПК и без ввода ссылок.

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

**На Apple TV:** VLC → «Сеть» → «Подключиться к серверу» → `smb://192.168.88.251`,
логин `videoshare` и пароль. Папка `VideoShare` — список готовых файлов.

> Файлы в витрине не чистятся автоматически (это твоя «библиотека»). Управлять
> ими можно прямо с сайта — раздел **«📚 Библиотека (SMB)»**: список, скачать,
> удалить (появляется, только если задан `SHARE_DIR`). Либо вручную из папки
> `S:\VideoShare`. Сайт по-прежнему сам удаляет свои рабочие копии в
> `downloads/` через `RETENTION_HOURS`.

## Полезные команды
```bash
systemctl status video-downloader      # статус
journalctl -u video-downloader -f      # логи
sudo systemctl restart video-downloader
```

## Обновление yt-dlp
Сайты меняются, и yt-dlp надо периодически обновлять:
```bash
.venv/bin/pip install -U yt-dlp
sudo systemctl restart video-downloader
```

## Важно
Скачивай для личного офлайн-просмотра то, что доступно легально и бесплатно.
Уважай авторские права и правила сайтов. Контент с DRM-защитой (платные
стриминги) не скачивается — это by design.
