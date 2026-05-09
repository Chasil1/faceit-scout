# Токсичність + Фільтри в адмін-панелі відгуків

**Дата:** 2026-05-09  
**Проект:** faceit-checker  
**Статус:** Approved

---

## Контекст

Адмін-панель має розділ "Відгуки" де показуються всі відгуки гравців. Зараз немає:
- Автоматичної модерації токсичних коментарів
- Можливості бану гравців
- Фільтрації відгуків по гравцю

## Вимоги

### Токсичність
- Кожен новий відгук з коментарем автоматично перевіряється Gemini у фоновому режимі
- Відгук публікується миттєво, незалежно від результату перевірки
- Результат токсичності відображається лише в адмін-панелі
- Адмін може зняти мітку токсичності якщо Gemini помилилась
- Адмін може запустити масову re-analyze всіх існуючих коментарів

### Бан гравця
- Бан блокує вхід гравця по FACEIT акаунту
- Бан видаляє всі відгуки від цього гравця
- Дія незворотня (в рамках цієї реалізації)

### Фільтри
- "Тільки токсичні" — checkbox, токсичні відгуки виводяться зверху
- "Відгуки від" — autocomplete поле для пошуку гравця-рецензента по нікнейму
- "Відгуки гравцю" — autocomplete поле для пошуку цільового гравця по нікнейму
- Фільтри комбінуються між собою

---

## Архітектура

### Gemini API
- Модель: `gemini-2.0-flash-latest` (аліас `gemini-flash-latest`)
- Ключ: `AIzaSyCV839C66pP4WY2LfFRfxKfYHTW2RZTAjI` зберігається в env var `GEMINI_API_KEY`
- HTTP клієнт: `httpx` (async, встановити якщо відсутній: `pip install httpx`)
- Endpoint: `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-latest:generateContent`
- Prompt: класифікація коментаря як toxic/safe, відповідь JSON `{"toxic": true/false}`

### Фонова черга (asyncio)
```
POST /review (з коментарем)
    → зберегти в БД (миттєво)
    → queue.put(reviewer_id, target_id, comment)
    
Worker (фоновий):
    → queue.get()
    → Gemini API call
    → UPDATE player_reviews SET is_toxic=... WHERE ...
```

Worker запускається як asyncio task при старті FastAPI (`@app.on_event("startup")`).

---

## Зміни БД

### player_reviews — нові колонки
```sql
ALTER TABLE player_reviews
  ADD COLUMN is_toxic BOOLEAN DEFAULT FALSE NOT NULL,
  ADD COLUMN is_toxic_override BOOLEAN DEFAULT NULL;
```

**Логіка відображення токсичності:**
- `is_toxic_override IS NULL` → показуємо значення `is_toxic` (автоматична оцінка)
- `is_toxic_override = FALSE` → адмін зняв мітку, показуємо як безпечний
- `is_toxic_override = TRUE` → адмін вручну позначив як токсичний

**Ефективна токсичність:** `(is_toxic_override IS NULL AND is_toxic = TRUE) OR (is_toxic_override = TRUE)`

### users — нові колонки
```sql
ALTER TABLE users
  ADD COLUMN is_banned BOOLEAN DEFAULT FALSE NOT NULL,
  ADD COLUMN banned_at TIMESTAMP DEFAULT NULL;
```

---

## Backend — Нові/змінені ендпоінти

### GET /api/admin/reviews (змінений)
Нові query parameters:
- `toxic_only=true` — тільки токсичні
- `reviewer_faceit_id=xxx` — відгуки від конкретного гравця
- `target_account_id=xxx` — відгуки конкретному гравцю

Нові поля у відповіді:
```json
{
  "is_toxic": true,
  "is_toxic_override": null,
  "effective_toxic": true
}
```

### GET /api/admin/users/search?q=&role=reviewer|target (новий)
Autocomplete пошук. Різний формат залежно від ролі:

`role=reviewer` → шукає в таблиці `users` (FACEIT акаунти):
```json
{
  "users": [
    {"faceit_id": "abc123", "nickname": "PlayerOne", "avatar": "https://..."}
  ]
}
```

`role=target` → шукає в `opendota_cache` (Dota 2 профілі по account_id):
```json
{
  "users": [
    {"account_id": 123456789, "nickname": "PlayerTwo", "avatar": "https://..."}
  ]
}
```
Пошук по полю `nickname` (ILIKE `%q%`), ліміт 20 результатів.

### PATCH /api/admin/reviews/{reviewer_faceit_id}/{target_account_id}/toxicity (новий)
Body: `{"override": false}` — зняти мітку; `{"override": true}` — поставити; `{"override": null}` — скинути до авто
```json
{"ok": true}
```

### POST /api/admin/users/{faceit_id}/ban (новий)
- Встановлює `is_banned=TRUE, banned_at=NOW()` в `users`
- Видаляє всі рядки з `player_reviews WHERE reviewer_faceit_id=$1`
```json
{"ok": true, "deleted_reviews": 5}
```

### POST /api/admin/reviews/reanalyze (новий)
- Ставить в чергу всі коментарі де `comment IS NOT NULL AND is_toxic_override IS NULL`
- Повертає кількість поставлених в чергу
```json
{"ok": true, "queued": 42}
```

### Захист від забанених
В `/api/auth` (OAuth callback) → перевірка `is_banned` → якщо `TRUE` → повертати 403 з повідомленням

---

## Frontend — admin.html

### Тулбар над відгуками
```html
[☐ Тільки токсичні]  
[Відгуки від: 🔍 search input + datalist]  
[Відгуки гравцю: 🔍 search input + datalist]  
[🔄 Перевірити всі відгуки]
```

### Картка відгуку — нові елементи
- Бейдж `🔴 ТОКСИЧНИЙ` для `effective_toxic = true`
- Кнопка `Зняти мітку` (тільки на токсичних картках) → PATCH toxicity з `{"override": false}`
- Кнопка `Забанити` на кожній картці → модалка підтвердження з нікнеймом

### Поведінка фільтрів
- Фільтри застосовуються на backend (query params до GET /api/admin/reviews)
- Autocomplete: при введенні 2+ символів → GET /api/admin/users/search?q=...
- При виборі гравця з dropdown → оновлення списку відгуків

---

## Gemini Prompt

```
Проаналізуй наступний коментар у грі (Dota 2 / FACEIT) і визнач чи є він токсичним.
Токсичний коментар містить: образи, погрози, расистські/сексистські висловлювання, 
систематичне приниження, заклики до насильства.

Коментар: "{comment}"

Відповідь лише у форматі JSON: {"toxic": true} або {"toxic": false}
```

---

## Порядок реалізації

1. SQL міграція (нові колонки)
2. Gemini async worker в server.py
3. Нові ендпоінти в server.py
4. Змінений GET /api/admin/reviews (фільтри)
5. Захист від бану в auth
6. Frontend: тулбар з фільтрами
7. Frontend: оновлені картки відгуків
8. Frontend: autocomplete логіка
9. Frontend: модалка бану
