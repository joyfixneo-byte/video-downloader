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
- Необязательный пароль на вход

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
