# tg-stream

Свой музыкальный сервер поверх Telegram-канала: индексирует аудио из канала
и отдаёт его по протоколу Subsonic (v1.16.1, JSON) — подходят любые
Subsonic-клиенты (Symfonium, Substreamer, Feeldin, Tempo, Jamstash и др.).

Аудио стримится напрямую из Telegram с поддержкой HTTP Range (seek работает
без скачивания всего файла), прослушанное кэшируется на диск (LRU).

## Установка

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
copy config.example.yaml config.yaml   # и заполни (см. ниже)
```

## config.yaml

- `telegram.api_id` / `api_hash` — с https://my.telegram.org (API development tools)
- `telegram.channels` — список каналов (`@username` или `https://t.me/...`),
  аккаунт должен быть подписан на канал
- `telegram.proxy` — опционально, если Telegram напрямую недоступен:
  `type: mtproto` (+`secret`) или `socks5`/`http`. Не нужен — удали секцию.
- `users` — логины/пароли для Subsonic-клиентов
- `cache.max_bytes` — лимит дискового кэша (LRU-вытеснение)

## Первый вход в Telegram (один раз)

```bash
.venv/Scripts/python -m app.login_qr   # рекомендуемый способ: сканируешь QR из терминала
.venv/Scripts/python -m app.login      # запасной: телефон + код (пустой Enter = повторная отправка)
```

QR: на телефоне Telegram -> Настройки -> Устройства -> Подключить устройство.

## Запуск

```bash
.venv/Scripts/python -m app
```

При старте подключается к Telegram (через прокси из конфига, если задан),
инкрементально доиндексирует каналы. Сервер: `http://0.0.0.0:8080`.

## Подключение клиента

- Сервер: `http://<ip-компьютера>:8080`
- Логин/пароль: из `users` в config.yaml
- Новый посты канала подтягиваются кнопкой скана в клиенте (startScan)
  или перезапуском сервера

## Заметки

- Кэш: `cache/tracks` (аудио, LRU), `cache/covers` (обложки); база: `data/library.db`
- Альбом = название канала (MVP); исполнитель берётся из атрибутов аудио
- Сессия Telegram: файл `<session>.session` — не потеряй, вход без неё придётся повторить
- Если файлы не влезают в лимит кэша, самые давно слушанные удаляются автоматически

## Структура

- `app/telegram/` — клиент Telethon, загрузки со смещением (Range -> offset)
- `app/library/` — SQLite-библиотека, индексер канала
- `app/subsonic/` — API: auth, сериализация, эндпоинты, стриминг
- `app/cache/` — LRU-кэш
- `app/login.py`, `app/login_qr.py` — интерактивный вход
