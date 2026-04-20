# Faceit OAuth Setup

## 1. Створи Faceit OAuth додаток

1. Зайди на https://developers.faceit.com/apps
2. Натисни "Create App"
3. Заповни:
   - **Name**: Faceit Scout
   - **Redirect URI**: `https://your-domain.railway.app/auth/callback` (або `http://localhost:8000/auth/callback` для локалу)
   - **Scopes**: `openid`, `email`, `profile`
4. Збережи **Client ID** та **Client Secret**

## 2. Додай змінні оточення на Railway

```bash
FACEIT_CLIENT_ID=your_client_id_here
FACEIT_CLIENT_SECRET=your_client_secret_here
FACEIT_REDIRECT_URI=https://your-domain.railway.app/auth/callback
JWT_SECRET=your_random_secret_here
```

Згенеруй JWT_SECRET:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 3. Запусти міграцію БД

На Railway:
1. Відкрий Postgres service
2. Перейди в "Data" → "Query"
3. Скопіюй вміст `migration_users.sql` і виконай

Або через CLI:
```bash
psql $DATABASE_URL < migration_users.sql
```

## 4. Перевір роботу

1. Відкрий сайт
2. Натисни "ВХІД ЧЕРЕЗ FACEIT" в правому верхньому куті
3. Авторизуйся через Faceit
4. Після редіректу побачиш свій нікнейм та аватар

## Локальна розробка

```bash
export FACEIT_CLIENT_ID=your_client_id
export FACEIT_CLIENT_SECRET=your_client_secret
export FACEIT_REDIRECT_URI=http://localhost:8000/auth/callback
export JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export DATABASE_URL=postgresql://user:pass@localhost/dbname

python server.py
```
