# Toxicity Moderation + Review Filters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Gemini-powered toxicity detection, player banning, and review filters to the admin panel.

**Architecture:** Async asyncio queue in FastAPI receives toxicity check jobs when reviews are posted; a background worker calls Gemini API and writes results to DB. New admin endpoints handle banning and toxicity overrides. Frontend adds toolbar with filters, toxic badges, and ban modal.

**Tech Stack:** FastAPI + asyncpg (backend), aiohttp (Gemini calls, already installed), vanilla JS (frontend), PostgreSQL (Supabase), Google Gemini API (`gemini-2.0-flash-latest`).

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `migration_toxicity_ban.sql` | Create | Add `is_toxic`, `is_toxic_override` to `player_reviews`; `is_banned`, `banned_at` to `users` |
| `server.py` | Modify | Gemini worker, new endpoints, updated reviews endpoint, auth ban check |
| `admin.html` | Modify | CSS, HTML toolbar + ban modal, JS filter/toxic/ban logic |

---

## Task 1: SQL Migration

**Files:**
- Create: `migration_toxicity_ban.sql`

- [ ] **Step 1: Write migration file**

```sql
-- Migration: toxicity detection + player ban
-- Run on Railway/Supabase Postgres

ALTER TABLE player_reviews
  ADD COLUMN IF NOT EXISTS is_toxic BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS is_toxic_override BOOLEAN DEFAULT NULL;

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS banned_at TIMESTAMP DEFAULT NULL;
```

Create `C:\Users\Любо\Desktop\faceit-checker\migration_toxicity_ban.sql` with the content above.

- [ ] **Step 2: Apply migration via Supabase MCP**

Use `mcp__supabase__execute_sql` with the migration SQL. Verify both tables have the new columns.

- [ ] **Step 3: Commit**

```bash
git add migration_toxicity_ban.sql
git commit -m "feat: add toxicity and ban columns to DB"
```

---

## Task 2: Gemini async worker in server.py

**Files:**
- Modify: `server.py` — add before line 347 (before `lifespan`), modify `lifespan` function

- [ ] **Step 1: Add Gemini constants and queue after existing globals (after line 41 `_pool: asyncpg.Pool | None = None`)**

Find this exact line in server.py:
```python
_pool: asyncpg.Pool | None = None
```

Replace with:
```python
_pool: asyncpg.Pool | None = None

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyCV839C66pP4WY2LfFRfxKfYHTW2RZTAjI")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash-latest:generateContent"
)
_toxicity_queue: asyncio.Queue = asyncio.Queue()
```

- [ ] **Step 2: Add `check_toxicity_gemini` and `_toxicity_worker` functions before the `lifespan` function**

Find this line in server.py:
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
```

Insert the following TWO functions directly above it:

```python
async def check_toxicity_gemini(comment: str) -> bool:
    prompt = (
        "Проаналізуй наступний коментар у грі (Dota 2 / FACEIT) і визнач чи є він токсичним.\n"
        "Токсичний коментар містить: образи, погрози, расистські/сексистські висловлювання, "
        "систематичне приниження, заклики до насильства.\n\n"
        f'Коментар: "{comment}"\n\n'
        'Відповідь лише у форматі JSON: {"toxic": true} або {"toxic": false}'
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 20},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{GEMINI_URL}?key={GEMINI_API_KEY}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.error("Gemini API error %s", resp.status)
                    return False
                data = await resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                text = text.strip("`").strip()
                if text.startswith("json"):
                    text = text[4:].strip()
                return bool(json.loads(text).get("toxic", False))
    except Exception as e:
        log.error("Gemini toxicity check failed: %s", e)
        return False


async def _toxicity_worker():
    while True:
        try:
            reviewer_faceit_id, target_account_id, comment = await _toxicity_queue.get()
            is_toxic = await check_toxicity_gemini(comment)
            if _pool:
                await _pool.execute(
                    """
                    UPDATE player_reviews SET is_toxic = $3
                    WHERE reviewer_faceit_id = $1 AND target_account_id = $2
                      AND is_toxic_override IS NULL
                    """,
                    reviewer_faceit_id,
                    target_account_id,
                    is_toxic,
                )
                log.info("toxicity %s/%s -> %s", reviewer_faceit_id, target_account_id, is_toxic)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("toxicity worker error: %s", e)
        finally:
            try:
                _toxicity_queue.task_done()
            except Exception:
                pass


```

- [ ] **Step 3: Update `lifespan` to start/stop the worker**

Find exact text:
```python
    else:
        log.warning("DATABASE_URL not set — caching disabled")
    yield
    if _pool:
        await _pool.close()
        _pool = None
```

Replace with:
```python
    else:
        log.warning("DATABASE_URL not set — caching disabled")
    worker_task = asyncio.create_task(_toxicity_worker())
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    if _pool:
        await _pool.close()
        _pool = None
```

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat: add Gemini toxicity worker"
```

---

## Task 3: Update POST /api/profile/{account_id}/review

**Files:**
- Modify: `server.py:1413-1426`

When a review with a comment is saved, reset toxicity fields and queue a check.

- [ ] **Step 1: Update the INSERT statement to reset toxicity on update**

Find exact text (inside `post_review` function):
```python
    comment = (body.comment or "").strip() or None
    await _pool.execute(
        """
        INSERT INTO player_reviews (reviewer_faceit_id, target_account_id, rating, comment, is_anonymous, updated_at)
        VALUES ($1, $2, $3, $4, $5, NOW())
        ON CONFLICT (reviewer_faceit_id, target_account_id)
        DO UPDATE SET rating = EXCLUDED.rating, comment = EXCLUDED.comment,
                      is_anonymous = EXCLUDED.is_anonymous, updated_at = NOW()
        """,
        viewer["faceit_id"],
        account_id,
        body.rating,
        comment,
        body.is_anonymous,
    )
    return {"ok": True}
```

Replace with:
```python
    comment = (body.comment or "").strip() or None
    await _pool.execute(
        """
        INSERT INTO player_reviews (reviewer_faceit_id, target_account_id, rating, comment, is_anonymous, updated_at)
        VALUES ($1, $2, $3, $4, $5, NOW())
        ON CONFLICT (reviewer_faceit_id, target_account_id)
        DO UPDATE SET rating = EXCLUDED.rating, comment = EXCLUDED.comment,
                      is_anonymous = EXCLUDED.is_anonymous, updated_at = NOW(),
                      is_toxic = FALSE, is_toxic_override = NULL
        """,
        viewer["faceit_id"],
        account_id,
        body.rating,
        comment,
        body.is_anonymous,
    )
    if comment:
        await _toxicity_queue.put((viewer["faceit_id"], account_id, comment))
    return {"ok": True}
```

- [ ] **Step 2: Commit**

```bash
git add server.py
git commit -m "feat: queue toxicity check on new reviews"
```

---

## Task 4: Add ban check in auth endpoints

**Files:**
- Modify: `server.py` — `auth_callback` (~line 1039) and `exchange_code` (~line 1154)

Banned users get redirected/rejected instead of receiving a JWT.

- [ ] **Step 1: Add ban check in `auth_callback`**

Find exact text inside `auth_callback`:
```python
            await save_user_to_db(
                user_data["player_id"],
                user_data["nickname"],
                user_data.get("avatar"),
            )

            # Create JWT
            jwt_token = create_jwt_token(user_data)

            dest = "/auth/done" if is_popup else "/"
```

Replace with:
```python
            await save_user_to_db(
                user_data["player_id"],
                user_data["nickname"],
                user_data.get("avatar"),
            )

            if _pool:
                ban_row = await _pool.fetchrow(
                    "SELECT is_banned FROM users WHERE faceit_id = $1",
                    user_data["player_id"],
                )
                if ban_row and ban_row["is_banned"]:
                    return RedirectResponse("/?auth_error=banned")

            # Create JWT
            jwt_token = create_jwt_token(user_data)

            dest = "/auth/done" if is_popup else "/"
```

- [ ] **Step 2: Add ban check in `exchange_code`**

Find exact text inside `exchange_code`:
```python
            await save_user_to_db(
                user_data["player_id"],
                user_data["nickname"],
                user_data.get("avatar"),
            )

            jwt_token = create_jwt_token(user_data)
            response = JSONResponse({"ok": True})
```

Replace with:
```python
            await save_user_to_db(
                user_data["player_id"],
                user_data["nickname"],
                user_data.get("avatar"),
            )

            if _pool:
                ban_row = await _pool.fetchrow(
                    "SELECT is_banned FROM users WHERE faceit_id = $1",
                    user_data["player_id"],
                )
                if ban_row and ban_row["is_banned"]:
                    raise HTTPException(status_code=403, detail="banned")

            jwt_token = create_jwt_token(user_data)
            response = JSONResponse({"ok": True})
```

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "feat: block banned users from logging in"
```

---

## Task 5: Update GET /api/admin/reviews with filters

**Files:**
- Modify: `server.py:1515-1546`

Add query params, new columns in SELECT, ORDER BY toxic first.

- [ ] **Step 1: Replace the entire `admin_list_reviews` function**

Find exact text:
```python
@app.get("/api/admin/reviews")
async def admin_list_reviews(admin_session: str | None = Cookie(default=None)):
    if not _is_admin(admin_session):
        raise HTTPException(status_code=403, detail="forbidden")
    if not _pool:
        raise HTTPException(status_code=503, detail="db unavailable")
    rows = await _pool.fetch(
        """
        SELECT pr.reviewer_faceit_id, pr.target_account_id, pr.rating, pr.comment, pr.updated_at,
               pr.is_anonymous,
               u.nickname AS reviewer_nickname, u.avatar AS reviewer_avatar,
               oc.nickname AS target_nickname
        FROM player_reviews pr
        LEFT JOIN users u ON u.faceit_id = pr.reviewer_faceit_id
        LEFT JOIN opendota_cache oc ON oc.account_id = pr.target_account_id
        ORDER BY pr.updated_at DESC
        """
    )
    return {"reviews": [
        {
            "reviewer_faceit_id": r["reviewer_faceit_id"],
            "reviewer_nickname": r["reviewer_nickname"] or r["reviewer_faceit_id"][:8],
            "reviewer_avatar": r["reviewer_avatar"],
            "target_account_id": r["target_account_id"],
            "target_nickname": r["target_nickname"],
            "rating": r["rating"],
            "comment": r["comment"],
            "is_anonymous": r["is_anonymous"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]}
```

Replace with:
```python
@app.get("/api/admin/reviews")
async def admin_list_reviews(
    admin_session: str | None = Cookie(default=None),
    toxic_only: bool = False,
    reviewer_faceit_id: str | None = None,
    target_account_id: int | None = None,
):
    if not _is_admin(admin_session):
        raise HTTPException(status_code=403, detail="forbidden")
    if not _pool:
        raise HTTPException(status_code=503, detail="db unavailable")

    conditions: list[str] = []
    params: list = []

    if toxic_only:
        conditions.append(
            "(pr.is_toxic_override IS NULL AND pr.is_toxic = TRUE) OR pr.is_toxic_override = TRUE"
        )
    if reviewer_faceit_id:
        params.append(reviewer_faceit_id)
        conditions.append(f"pr.reviewer_faceit_id = ${len(params)}")
    if target_account_id is not None:
        params.append(target_account_id)
        conditions.append(f"pr.target_account_id = ${len(params)}")

    where = ("WHERE " + " AND ".join(f"({c})" for c in conditions)) if conditions else ""

    rows = await _pool.fetch(
        f"""
        SELECT pr.reviewer_faceit_id, pr.target_account_id, pr.rating, pr.comment, pr.updated_at,
               pr.is_anonymous, pr.is_toxic, pr.is_toxic_override,
               u.nickname AS reviewer_nickname, u.avatar AS reviewer_avatar,
               COALESCE(u.is_banned, FALSE) AS reviewer_is_banned,
               oc.nickname AS target_nickname
        FROM player_reviews pr
        LEFT JOIN users u ON u.faceit_id = pr.reviewer_faceit_id
        LEFT JOIN opendota_cache oc ON oc.account_id = pr.target_account_id
        {where}
        ORDER BY
          CASE WHEN (pr.is_toxic_override IS NULL AND pr.is_toxic = TRUE)
                 OR pr.is_toxic_override = TRUE THEN 0 ELSE 1 END,
          pr.updated_at DESC
        """,
        *params,
    )

    def effective_toxic(r) -> bool:
        if r["is_toxic_override"] is not None:
            return bool(r["is_toxic_override"])
        return bool(r["is_toxic"])

    return {"reviews": [
        {
            "reviewer_faceit_id": r["reviewer_faceit_id"],
            "reviewer_nickname": r["reviewer_nickname"] or r["reviewer_faceit_id"][:8],
            "reviewer_avatar": r["reviewer_avatar"],
            "reviewer_is_banned": r["reviewer_is_banned"],
            "target_account_id": r["target_account_id"],
            "target_nickname": r["target_nickname"],
            "rating": r["rating"],
            "comment": r["comment"],
            "is_anonymous": r["is_anonymous"],
            "is_toxic": r["is_toxic"],
            "is_toxic_override": r["is_toxic_override"],
            "effective_toxic": effective_toxic(r),
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]}
```

- [ ] **Step 2: Commit**

```bash
git add server.py
git commit -m "feat: add filters and toxicity fields to admin reviews endpoint"
```

---

## Task 6: New admin endpoints

**Files:**
- Modify: `server.py` — add after `admin_delete_review` function (after line 1565)

- [ ] **Step 1: Add `ToxicityOverride` model and 4 new endpoints**

Find exact text:
```python
@app.post("/api/admin/smurf/{account_id}")
```

Insert the following block **directly before** that line:

```python
class ToxicityOverride(BaseModel):
    override: bool | None = None


@app.get("/api/admin/users/search")
async def admin_search_users(
    q: str = "",
    role: str = "reviewer",
    admin_session: str | None = Cookie(default=None),
):
    if not _is_admin(admin_session):
        raise HTTPException(status_code=403, detail="forbidden")
    if not _pool or len(q) < 2:
        return {"users": []}
    if role == "reviewer":
        rows = await _pool.fetch(
            "SELECT faceit_id, nickname, avatar FROM users WHERE nickname ILIKE $1 LIMIT 20",
            f"%{q}%",
        )
        return {"users": [{"faceit_id": r["faceit_id"], "nickname": r["nickname"], "avatar": r["avatar"]} for r in rows]}
    rows = await _pool.fetch(
        "SELECT account_id, nickname, avatar FROM opendota_cache WHERE nickname ILIKE $1 LIMIT 20",
        f"%{q}%",
    )
    return {"users": [{"account_id": r["account_id"], "nickname": r["nickname"], "avatar": r["avatar"]} for r in rows]}


@app.patch("/api/admin/reviews/{reviewer_faceit_id}/{target_account_id}/toxicity")
async def admin_set_toxicity(
    reviewer_faceit_id: str,
    target_account_id: int,
    body: ToxicityOverride,
    admin_session: str | None = Cookie(default=None),
):
    if not _is_admin(admin_session):
        raise HTTPException(status_code=403, detail="forbidden")
    if not _pool:
        raise HTTPException(status_code=503, detail="db unavailable")
    result = await _pool.execute(
        "UPDATE player_reviews SET is_toxic_override = $3 WHERE reviewer_faceit_id = $1 AND target_account_id = $2",
        reviewer_faceit_id,
        target_account_id,
        body.override,
    )
    if result.endswith(" 0"):
        raise HTTPException(status_code=404, detail="review not found")
    return {"ok": True}


@app.post("/api/admin/users/{faceit_id}/ban")
async def admin_ban_user(
    faceit_id: str,
    admin_session: str | None = Cookie(default=None),
):
    if not _is_admin(admin_session):
        raise HTTPException(status_code=403, detail="forbidden")
    if not _pool:
        raise HTTPException(status_code=503, detail="db unavailable")
    result = await _pool.execute(
        "UPDATE users SET is_banned = TRUE, banned_at = NOW() WHERE faceit_id = $1",
        faceit_id,
    )
    if result.endswith(" 0"):
        raise HTTPException(status_code=404, detail="user not found")
    deleted = await _pool.execute(
        "DELETE FROM player_reviews WHERE reviewer_faceit_id = $1",
        faceit_id,
    )
    deleted_count = int(deleted.split()[-1])
    return {"ok": True, "deleted_reviews": deleted_count}


@app.post("/api/admin/reviews/reanalyze")
async def admin_reanalyze_reviews(admin_session: str | None = Cookie(default=None)):
    if not _is_admin(admin_session):
        raise HTTPException(status_code=403, detail="forbidden")
    if not _pool:
        raise HTTPException(status_code=503, detail="db unavailable")
    rows = await _pool.fetch(
        "SELECT reviewer_faceit_id, target_account_id, comment FROM player_reviews "
        "WHERE comment IS NOT NULL AND is_toxic_override IS NULL"
    )
    for row in rows:
        await _toxicity_queue.put((row["reviewer_faceit_id"], row["target_account_id"], row["comment"]))
    return {"ok": True, "queued": len(rows)}


```

- [ ] **Step 2: Commit**

```bash
git add server.py
git commit -m "feat: add toxicity override, ban, reanalyze, and user search admin endpoints"
```

---

## Task 7: Frontend CSS in admin.html

**Files:**
- Modify: `admin.html` — add after `.rv-del-btn:hover { ... }` block (~line 69)

- [ ] **Step 1: Add new CSS styles**

Find exact text in admin.html:
```css
.rv-del-btn:hover { border-color: var(--red); color: var(--red); }
```

Replace with:
```css
.rv-del-btn:hover { border-color: var(--red); color: var(--red); }

/* Reviews toolbar */
.reviews-toolbar {
  display: flex; flex-wrap: wrap; gap: .75rem; align-items: flex-end;
  margin-bottom: .75rem; padding-bottom: .75rem;
  border-bottom: 1px solid var(--border);
}
.rv-toxic-check-wrap { display: flex; align-items: center; gap: .4rem; font-size: .82rem; color: var(--dim); cursor: pointer; user-select: none; padding-bottom: .1rem; }
.rv-toxic-check-wrap input[type=checkbox] { accent-color: var(--red); width: 15px; height: 15px; cursor: pointer; }
.rv-search-wrap { display: flex; flex-direction: column; gap: .2rem; position: relative; }
.rv-search-label { font-size: .68rem; color: var(--dim); letter-spacing: .07em; text-transform: uppercase; font-weight: 600; }
.rv-search-input {
  background: var(--bg3); border: 1px solid var(--border2); color: var(--text);
  padding: .3rem .6rem; font-family: inherit; font-size: .8rem; width: 185px; outline: none;
}
.rv-search-input:focus { border-color: var(--orange); }
.rv-autocomplete {
  position: absolute; top: 100%; left: 0; right: 0; background: var(--bg2);
  border: 1px solid var(--border2); border-top: none; z-index: 200;
  max-height: 200px; overflow-y: auto;
}
.rv-autocomplete-item { padding: .35rem .6rem; font-size: .8rem; cursor: pointer; display: flex; align-items: center; gap: .5rem; }
.rv-autocomplete-item:hover { background: var(--bg3); }
.rv-autocomplete-item img { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }
.rv-autocomplete-item .ac-avatar-placeholder { width: 20px; height: 20px; border-radius: 50%; background: var(--bg3); border: 1px solid var(--border2); flex-shrink: 0; }
.rv-reanalyze-btn {
  margin-left: auto; background: var(--bg3); border: 1px solid var(--border2); color: var(--dim);
  padding: .3rem .7rem; font-family: inherit; font-size: .75rem; font-weight: 600;
  letter-spacing: .04em; cursor: pointer; white-space: nowrap; align-self: flex-end;
}
.rv-reanalyze-btn:hover:not(:disabled) { border-color: var(--orange); color: var(--orange); }
.rv-reanalyze-btn:disabled { opacity: .5; cursor: default; }

/* Toxic badge on review card */
.rv-toxic-badge {
  display: inline-flex; align-items: center; gap: .2rem;
  background: rgba(255,59,59,.1); border: 1px solid rgba(255,59,59,.35);
  color: var(--red); font-size: .65rem; font-weight: 700;
  padding: .08rem .4rem; letter-spacing: .07em; vertical-align: middle;
}
.rv-banned-badge {
  display: inline-flex; align-items: center;
  background: rgba(255,59,59,.06); border: 1px solid rgba(255,59,59,.2);
  color: #cc4444; font-size: .65rem; font-weight: 700;
  padding: .08rem .4rem; letter-spacing: .06em; vertical-align: middle;
}
.rv-remove-toxic-btn {
  display: inline-block; margin-top: .3rem;
  background: none; border: 1px solid var(--border2); color: var(--dim);
  padding: .18rem .5rem; font-family: inherit; font-size: .68rem; font-weight: 600;
  cursor: pointer; white-space: nowrap;
}
.rv-remove-toxic-btn:hover { border-color: var(--orange); color: var(--orange); }
.rv-ban-btn {
  background: none; border: 1px solid transparent; color: var(--dim);
  padding: .28rem .6rem; font-family: inherit; font-size: .72rem; font-weight: 600;
  cursor: pointer; white-space: nowrap; display: block;
}
.rv-ban-btn:hover { border-color: var(--red); color: var(--red); }

/* Ban confirmation modal */
.ban-modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.72); z-index: 500;
  display: flex; align-items: center; justify-content: center;
}
.ban-modal-box {
  background: var(--bg2); border: 1px solid var(--border2); padding: 1.75rem 1.5rem;
  min-width: 300px; max-width: 400px; text-align: center;
}
.ban-modal-box h3 { font-size: 1.05rem; margin-bottom: .6rem; color: var(--text); }
.ban-modal-box p { font-size: .85rem; color: var(--dim); margin-bottom: 1.4rem; line-height: 1.5; }
.ban-modal-actions { display: flex; gap: .6rem; justify-content: center; }
.ban-confirm-btn {
  background: rgba(255,59,59,.14); border: 1px solid var(--red); color: var(--red);
  padding: .45rem 1.2rem; font-family: inherit; font-size: .85rem; font-weight: 700; cursor: pointer;
}
.ban-confirm-btn:hover:not(:disabled) { background: rgba(255,59,59,.24); }
.ban-confirm-btn:disabled { opacity: .5; cursor: default; }
.ban-cancel-btn {
  background: var(--bg3); border: 1px solid var(--border2); color: var(--dim);
  padding: .45rem 1.2rem; font-family: inherit; font-size: .85rem; font-weight: 600; cursor: pointer;
}
.ban-cancel-btn:hover { border-color: var(--text); color: var(--text); }
```

- [ ] **Step 2: Commit**

```bash
git add admin.html
git commit -m "feat: add admin reviews toolbar and toxicity CSS"
```

---

## Task 8: Frontend HTML in admin.html

**Files:**
- Modify: `admin.html` — reviews tab panel (~line 465) and before `</body>`

- [ ] **Step 1: Replace the reviews tab panel HTML**

Find exact text:
```html
<!-- Reviews tab -->
<div class="tab-panel" id="tab-reviews">
  <div id="reviews-state" class="state">Завантаження…</div>
  <div class="reviews-list" id="reviews-list"></div>
  <div class="pagination" id="reviews-pagination"></div>
</div>
```

Replace with:
```html
<!-- Reviews tab -->
<div class="tab-panel" id="tab-reviews">
  <div class="reviews-toolbar">
    <label class="rv-toxic-check-wrap">
      <input type="checkbox" id="rv-toxic-only">
      🔴 Тільки токсичні
    </label>
    <div class="rv-search-wrap">
      <span class="rv-search-label">Відгуки від</span>
      <input class="rv-search-input" id="rv-from-input" placeholder="Пошук гравця…" autocomplete="off">
      <div class="rv-autocomplete" id="rv-from-dropdown" style="display:none"></div>
    </div>
    <div class="rv-search-wrap">
      <span class="rv-search-label">Відгуки гравцю</span>
      <input class="rv-search-input" id="rv-to-input" placeholder="Пошук гравця…" autocomplete="off">
      <div class="rv-autocomplete" id="rv-to-dropdown" style="display:none"></div>
    </div>
    <button class="rv-reanalyze-btn" id="rv-reanalyze-btn">🔄 Перевірити всі</button>
  </div>
  <div id="reviews-state" class="state">Завантаження…</div>
  <div class="reviews-list" id="reviews-list"></div>
  <div class="pagination" id="reviews-pagination"></div>
</div>
```

- [ ] **Step 2: Add ban modal before `</body>`**

Find exact text:
```html
<script>
const RANK_NAMES = {
```

Insert the following directly before that `<script>` tag:
```html
<!-- Ban confirmation modal -->
<div class="ban-modal-overlay" id="ban-modal" style="display:none">
  <div class="ban-modal-box">
    <h3>Забанити гравця</h3>
    <p id="ban-modal-text"></p>
    <div class="ban-modal-actions">
      <button class="ban-cancel-btn" id="ban-cancel-btn">Скасувати</button>
      <button class="ban-confirm-btn" id="ban-confirm-btn">Забанити</button>
    </div>
  </div>
</div>

```

- [ ] **Step 3: Commit**

```bash
git add admin.html
git commit -m "feat: add reviews toolbar and ban modal HTML"
```

---

## Task 9: Frontend JS in admin.html

**Files:**
- Modify: `admin.html` — replace the `// ── Reviews ──` section and add new functions (~lines 850–964)

- [ ] **Step 1: Replace entire reviews JS section**

Find exact text:
```javascript
// ── Reviews ────────────────────────────────────────────────────────────
const REVIEWS_PER_PAGE = 100;
let reviewsData = [];
let reviewsPage = 0;

async function loadReviews() {
  document.getElementById('reviews-state').style.display = '';
  document.getElementById('reviews-state').textContent = 'Завантаження…';
  document.getElementById('reviews-list').innerHTML = '';
  document.getElementById('reviews-pagination').innerHTML = '';
  try {
    const r = await fetch('/api/admin/reviews');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    reviewsData = data.reviews || [];
    reviewsPage = 0;
    renderReviews();
  } catch (e) {
    document.getElementById('reviews-state').textContent = 'Помилка: ' + e.message;
  }
}
```

Replace with:
```javascript
// ── Reviews ────────────────────────────────────────────────────────────
const REVIEWS_PER_PAGE = 100;
let reviewsData = [];
let reviewsPage = 0;
let rvToxicOnly = false;
let rvFromFaceitId = null;
let rvToAccountId = null;

async function loadReviews() {
  document.getElementById('reviews-state').style.display = '';
  document.getElementById('reviews-state').textContent = 'Завантаження…';
  document.getElementById('reviews-list').innerHTML = '';
  document.getElementById('reviews-pagination').innerHTML = '';
  try {
    const params = new URLSearchParams();
    if (rvToxicOnly) params.set('toxic_only', 'true');
    if (rvFromFaceitId) params.set('reviewer_faceit_id', rvFromFaceitId);
    if (rvToAccountId !== null) params.set('target_account_id', String(rvToAccountId));
    const r = await fetch('/api/admin/reviews?' + params.toString());
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    reviewsData = data.reviews || [];
    reviewsPage = 0;
    renderReviews();
  } catch (e) {
    document.getElementById('reviews-state').textContent = 'Помилка: ' + e.message;
  }
}
```

- [ ] **Step 2: Replace the `renderReviews` function**

Find exact text:
```javascript
function renderReviews() {
  const badge = document.getElementById('reviews-badge');
  if (reviewsData.length) {
    badge.textContent = reviewsData.length;
    badge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
  }

  const list = document.getElementById('reviews-list');
  if (reviewsData.length === 0) {
    document.getElementById('reviews-state').style.display = '';
    document.getElementById('reviews-state').textContent = 'Відгуків немає.';
    list.innerHTML = '';
    renderReviewsPagination();
    return;
  }
  document.getElementById('reviews-state').style.display = 'none';

  const totalPages = Math.ceil(reviewsData.length / REVIEWS_PER_PAGE);
  reviewsPage = Math.max(0, Math.min(reviewsPage, totalPages - 1));
  const pageData = reviewsData.slice(reviewsPage * REVIEWS_PER_PAGE, (reviewsPage + 1) * REVIEWS_PER_PAGE);

  list.innerHTML = pageData.map((rv, idx) => {
    const ratingIcon = rv.rating === 1 ? '👍' : '👎';
    const targetLink = rv.target_account_id
      ? `<span class="rv-player-link" data-acc="${rv.target_account_id}" style="color:var(--orange);cursor:pointer;font-weight:700">${esc(rv.target_nickname || rv.target_account_id)}</span>`
      : esc(rv.target_nickname || rv.target_account_id);
    const anonBadge = rv.is_anonymous
      ? ' <span style="font-size:.7rem;color:var(--dim);background:#2a2a2a;border:1px solid #3a3a3a;padding:.1rem .35rem;border-radius:3px">анонімно</span>'
      : '';
    const reviewerText = esc(rv.reviewer_nickname || rv.reviewer_faceit_id);
    const commentHtml = rv.comment
      ? `<div class="rv-comment">${esc(rv.comment)}</div>`
      : `<div class="rv-comment empty">(без коментаря)</div>`;
    return `
      <div class="review-card" data-idx="${idx}">
        <div class="rv-rating">${ratingIcon}</div>
        <div class="rv-body">
          <div class="rv-players">
            <span>${reviewerText}${anonBadge}</span>
            <span style="color:var(--muted)">→</span>
            ${targetLink}
          </div>
          ${commentHtml}
          <div class="rv-time">${fmtTime(rv.updated_at)}</div>
        </div>
        <div class="rv-actions">
          <button class="rv-del-btn" data-reviewer="${esc(rv.reviewer_faceit_id)}" data-target="${rv.target_account_id}">
            Видалити
          </button>
        </div>
      </div>`;
  }).join('');

  list.querySelectorAll('.rv-del-btn').forEach(btn => {
    btn.onclick = () => deleteReview(btn.dataset.reviewer, btn.dataset.target);
  });
  list.querySelectorAll('.rv-player-link').forEach(el => {
    el.onclick = () => openProfileModal(+el.dataset.acc);
  });

  renderReviewsPagination();
}
```

Replace with:
```javascript
function renderReviews() {
  const badge = document.getElementById('reviews-badge');
  if (reviewsData.length) {
    badge.textContent = reviewsData.length;
    badge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
  }

  const list = document.getElementById('reviews-list');
  if (reviewsData.length === 0) {
    document.getElementById('reviews-state').style.display = '';
    document.getElementById('reviews-state').textContent = 'Відгуків немає.';
    list.innerHTML = '';
    renderReviewsPagination();
    return;
  }
  document.getElementById('reviews-state').style.display = 'none';

  const totalPages = Math.ceil(reviewsData.length / REVIEWS_PER_PAGE);
  reviewsPage = Math.max(0, Math.min(reviewsPage, totalPages - 1));
  const pageData = reviewsData.slice(reviewsPage * REVIEWS_PER_PAGE, (reviewsPage + 1) * REVIEWS_PER_PAGE);

  list.innerHTML = pageData.map((rv, idx) => {
    const ratingIcon = rv.rating === 1 ? '👍' : '👎';
    const targetLink = rv.target_account_id
      ? `<span class="rv-player-link" data-acc="${rv.target_account_id}" style="color:var(--orange);cursor:pointer;font-weight:700">${esc(rv.target_nickname || rv.target_account_id)}</span>`
      : esc(rv.target_nickname || rv.target_account_id);
    const anonBadge = rv.is_anonymous
      ? ' <span style="font-size:.7rem;color:var(--dim);background:#2a2a2a;border:1px solid #3a3a3a;padding:.1rem .35rem;border-radius:3px">анонімно</span>'
      : '';
    const reviewerText = esc(rv.reviewer_nickname || rv.reviewer_faceit_id);
    const commentHtml = rv.comment
      ? `<div class="rv-comment">${esc(rv.comment)}</div>`
      : `<div class="rv-comment empty">(без коментаря)</div>`;
    const toxicBadge = rv.effective_toxic
      ? `<span class="rv-toxic-badge">🔴 ТОКСИЧНИЙ</span>`
      : '';
    const bannedBadge = rv.reviewer_is_banned
      ? `<span class="rv-banned-badge">⛔ ЗАБЛОКОВАНО</span>`
      : '';
    const removeToxicBtn = rv.effective_toxic
      ? `<button class="rv-remove-toxic-btn" data-reviewer="${esc(rv.reviewer_faceit_id)}" data-target="${rv.target_account_id}">Зняти мітку</button>`
      : '';
    const banBtn = !rv.reviewer_is_banned
      ? `<button class="rv-ban-btn" data-reviewer="${esc(rv.reviewer_faceit_id)}" data-nickname="${esc(rv.reviewer_nickname || rv.reviewer_faceit_id)}">Забанити</button>`
      : '';
    return `
      <div class="review-card" data-idx="${idx}">
        <div class="rv-rating">${ratingIcon}</div>
        <div class="rv-body">
          <div class="rv-players">
            <span>${reviewerText}${anonBadge}</span>
            ${bannedBadge}
            ${toxicBadge}
            <span style="color:var(--muted)">→</span>
            ${targetLink}
          </div>
          ${commentHtml}
          <div class="rv-time">${fmtTime(rv.updated_at)}</div>
          ${removeToxicBtn}
        </div>
        <div class="rv-actions">
          ${banBtn}
          <button class="rv-del-btn" data-reviewer="${esc(rv.reviewer_faceit_id)}" data-target="${rv.target_account_id}">
            Видалити
          </button>
        </div>
      </div>`;
  }).join('');

  list.querySelectorAll('.rv-del-btn').forEach(btn => {
    btn.onclick = () => deleteReview(btn.dataset.reviewer, btn.dataset.target);
  });
  list.querySelectorAll('.rv-player-link').forEach(el => {
    el.onclick = () => openProfileModal(+el.dataset.acc);
  });
  list.querySelectorAll('.rv-remove-toxic-btn').forEach(btn => {
    btn.onclick = () => removeToxicLabel(btn.dataset.reviewer, btn.dataset.target, btn);
  });
  list.querySelectorAll('.rv-ban-btn').forEach(btn => {
    btn.onclick = () => openBanModal(btn.dataset.reviewer, btn.dataset.nickname);
  });

  renderReviewsPagination();
}
```

- [ ] **Step 3: Add new review helper functions after `deleteReview` function**

Find exact text:
```javascript
async function deleteReview(reviewerFaceitId, targetAccountId) {
  if (!confirm('Видалити цей відгук?')) return;
  try {
    const r = await fetch(`/api/admin/reviews/${encodeURIComponent(reviewerFaceitId)}/${targetAccountId}`, { method: 'DELETE' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    await loadReviews();
  } catch (e) {
    alert('Помилка: ' + e.message);
  }
}
```

Replace with:
```javascript
async function deleteReview(reviewerFaceitId, targetAccountId) {
  if (!confirm('Видалити цей відгук?')) return;
  try {
    const r = await fetch(`/api/admin/reviews/${encodeURIComponent(reviewerFaceitId)}/${targetAccountId}`, { method: 'DELETE' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    await loadReviews();
  } catch (e) {
    alert('Помилка: ' + e.message);
  }
}

async function removeToxicLabel(reviewerFaceitId, targetAccountId, btn) {
  const orig = btn.textContent;
  btn.textContent = '…';
  btn.disabled = true;
  try {
    const r = await fetch(
      `/api/admin/reviews/${encodeURIComponent(reviewerFaceitId)}/${targetAccountId}/toxicity`,
      { method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({override: false}) }
    );
    if (!r.ok) throw new Error('HTTP ' + r.status);
    await loadReviews();
  } catch (e) {
    alert('Помилка: ' + e.message);
    btn.textContent = orig;
    btn.disabled = false;
  }
}

// ── Ban modal ───────────────────────────────────────────────────────────
let banPendingFaceitId = null;

function openBanModal(faceitId, nickname) {
  banPendingFaceitId = faceitId;
  document.getElementById('ban-modal-text').textContent =
    `Забанити "${nickname}"? Гравець не зможе увійти на сайт, а всі його відгуки будуть видалені.`;
  document.getElementById('ban-modal').style.display = 'flex';
}

function closeBanModal() {
  banPendingFaceitId = null;
  document.getElementById('ban-modal').style.display = 'none';
  const btn = document.getElementById('ban-confirm-btn');
  btn.textContent = 'Забанити';
  btn.disabled = false;
}

async function confirmBan() {
  if (!banPendingFaceitId) return;
  const faceitId = banPendingFaceitId;
  const btn = document.getElementById('ban-confirm-btn');
  btn.textContent = '…';
  btn.disabled = true;
  try {
    const r = await fetch(`/api/admin/users/${encodeURIComponent(faceitId)}/ban`, { method: 'POST' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    closeBanModal();
    await loadReviews();
  } catch (e) {
    alert('Помилка: ' + e.message);
    btn.textContent = 'Забанити';
    btn.disabled = false;
  }
}

document.getElementById('ban-confirm-btn').addEventListener('click', confirmBan);
document.getElementById('ban-cancel-btn').addEventListener('click', closeBanModal);
document.getElementById('ban-modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeBanModal();
});

// ── Reviews filters setup ──────────────────────────────────────────────
document.getElementById('rv-toxic-only').addEventListener('change', function() {
  rvToxicOnly = this.checked;
  loadReviews();
});

document.getElementById('rv-reanalyze-btn').addEventListener('click', async function() {
  this.textContent = '⏳ Аналіз…';
  this.disabled = true;
  try {
    const r = await fetch('/api/admin/reviews/reanalyze', { method: 'POST' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    this.textContent = `✓ ${data.queued} в черзі`;
    setTimeout(() => { this.textContent = '🔄 Перевірити всі'; this.disabled = false; }, 2500);
  } catch (e) {
    alert('Помилка: ' + e.message);
    this.textContent = '🔄 Перевірити всі';
    this.disabled = false;
  }
});

// ── Autocomplete helper ────────────────────────────────────────────────
function renderAutocomplete(dropdownId, items, idKey, onSelect) {
  const dropdown = document.getElementById(dropdownId);
  if (!items || !items.length) { dropdown.style.display = 'none'; return; }
  dropdown.innerHTML = items.map((u, i) => {
    const avatar = u.avatar
      ? `<img src="${esc(u.avatar)}" alt="">`
      : `<span class="ac-avatar-placeholder"></span>`;
    return `<div class="rv-autocomplete-item" data-i="${i}">${avatar}<span>${esc(u.nickname)}</span></div>`;
  }).join('');
  dropdown.style.display = 'block';
  dropdown.querySelectorAll('.rv-autocomplete-item').forEach(el => {
    el.addEventListener('click', () => onSelect(items[+el.dataset.i]));
  });
}

// "Відгуки від" autocomplete (reviewers — from users table)
let rvFromDebounce = null;
const rvFromInput = document.getElementById('rv-from-input');
const rvFromDropdown = document.getElementById('rv-from-dropdown');

rvFromInput.addEventListener('input', function() {
  clearTimeout(rvFromDebounce);
  const q = this.value.trim();
  if (q.length < 2) {
    rvFromDropdown.style.display = 'none';
    if (!q && rvFromFaceitId) { rvFromFaceitId = null; loadReviews(); }
    return;
  }
  rvFromDebounce = setTimeout(async () => {
    try {
      const r = await fetch(`/api/admin/users/search?q=${encodeURIComponent(q)}&role=reviewer`);
      const data = await r.json();
      renderAutocomplete('rv-from-dropdown', data.users, 'faceit_id', u => {
        rvFromInput.value = u.nickname;
        rvFromDropdown.style.display = 'none';
        rvFromFaceitId = u.faceit_id;
        loadReviews();
      });
    } catch {}
  }, 300);
});

rvFromInput.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    rvFromDropdown.style.display = 'none';
    this.value = '';
    rvFromFaceitId = null;
    loadReviews();
  }
});

// "Відгуки гравцю" autocomplete (targets — from opendota_cache)
let rvToDebounce = null;
const rvToInput = document.getElementById('rv-to-input');
const rvToDropdown = document.getElementById('rv-to-dropdown');

rvToInput.addEventListener('input', function() {
  clearTimeout(rvToDebounce);
  const q = this.value.trim();
  if (q.length < 2) {
    rvToDropdown.style.display = 'none';
    if (!q && rvToAccountId !== null) { rvToAccountId = null; loadReviews(); }
    return;
  }
  rvToDebounce = setTimeout(async () => {
    try {
      const r = await fetch(`/api/admin/users/search?q=${encodeURIComponent(q)}&role=target`);
      const data = await r.json();
      renderAutocomplete('rv-to-dropdown', data.users, 'account_id', u => {
        rvToInput.value = u.nickname;
        rvToDropdown.style.display = 'none';
        rvToAccountId = u.account_id;
        loadReviews();
      });
    } catch {}
  }, 300);
});

rvToInput.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    rvToDropdown.style.display = 'none';
    this.value = '';
    rvToAccountId = null;
    loadReviews();
  }
});

// Close autocomplete dropdowns on outside click
document.addEventListener('click', e => {
  if (!e.target.closest('.rv-search-wrap')) {
    rvFromDropdown.style.display = 'none';
    rvToDropdown.style.display = 'none';
  }
});
```

- [ ] **Step 4: Commit**

```bash
git add admin.html
git commit -m "feat: implement reviews filters, toxicity UI, and ban modal"
```

---

## Self-Review Checklist

After all tasks are done:
- [ ] Verify all 9 spec requirements are covered (toxicity auto-check, toxic filter, remove label, ban player, filter from, filter to)
- [ ] Test: POST a review with a bad comment → admin panel shows 🔴 ТОКСИЧНИЙ after ~5 sec
- [ ] Test: "Зняти мітку" → badge disappears, review stays
- [ ] Test: "Забанити" → modal appears → confirm → review list refreshes, banned badge appears
- [ ] Test: "Відгуки від" search → autocomplete shows users → select → list filters
- [ ] Test: "Відгуки гравцю" search → autocomplete shows targets → select → list filters
- [ ] Test: checkbox "Тільки токсичні" → only toxic reviews shown at top
- [ ] Test: "🔄 Перевірити всі" → shows queued count
- [ ] Test: banned user tries to log in → redirected to `/?auth_error=banned`
