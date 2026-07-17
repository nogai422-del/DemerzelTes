# Развёртывание на Bothost

1. Включите «Использовать домен».
2. Внутренний порт в панели Bothost должен совпадать с `PORT`. Рекомендуется `7196`.
3. Добавьте переменные: BOT_TOKEN, FLASK_SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, BACKUP_CHANNEL_ID, LOG_CHANNEL_ID, SOURCE_CHAT_ID, COSMOS_ID.
4. Не удаляйте `/app/data/database.db`: при старте приложение само добавит недостающие таблицы и столбцы.
5. После замены архива выполните именно повторный деплой/пересборку, а не только перезапуск.
6. Проверка: откройте `/health` — должен быть ответ `ok`.
7. При 500 смотрите runtime-лог: теперь Flask пишет traceback с текстом `Admin panel request failed`.

Dockerfile хранит код в `/usr/src/app`, поскольку на части нод Bothost `/app` подменяется bind mount. Постоянные данные остаются в `/app/data`.
