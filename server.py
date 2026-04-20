#!/usr/bin/env python3
"""Faceit Dota 2 Scout — FastAPI web server."""

import asyncio
import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import aiohttp
import asyncpg
import jwt
from fastapi import FastAPI, HTTPException, Cookie, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("faceit")

FACEIT_KEY = os.environ.get("FACEIT_KEY", "1ca837fd-a345-47c8-9adc-e78f717489e8")
FACEIT_BASE = "https://open.faceit.com/data/v4"
OPENDOTA_BASE = "https://api.opendota.com/api"
STEAM64_BASE = 76561197960265728
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "5540")
ADMIN_TOKEN = "faceit_admin_ok"

# OAuth config
FACEIT_CLIENT_ID = os.environ.get("FACEIT_CLIENT_ID", "")
FACEIT_CLIENT_SECRET = os.environ.get("FACEIT_CLIENT_SECRET", "")
FACEIT_REDIRECT_URI = os.environ.get("FACEIT_REDIRECT_URI", "http://localhost:8000/auth/callback")
JWT_SECRET = os.environ.get("JWT_SECRET", secrets.token_urlsafe(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 30

_pool: asyncpg.Pool | None = None

RANK_NAMES = {
    1: "Herald",
    2: "Guardian",
    3: "Crusader",
    4: "Archon",
    5: "Legend",
    6: "Ancient",
    7: "Divine",
    8: "Immortal",
}


def rank_label(tier, leaderboard=None):
    if tier is None:
        return 0, "Unranked"
    try:
        t = int(tier)
    except Exception:
        return 0, "Unranked"
    major, minor = t // 10, t % 10
    name = RANK_NAMES.get(major, "?")
    if major == 8:
        label = f"Immortal #{leaderboard}" if leaderboard else "Immortal"
        return 8, label
    return major, f"{name} {minor}" if minor else name


def to_account_id(sid):
    if not sid:
        return None
    s = str(sid).strip()
    m = re.match(r"^\[U:\d+:(\d+)\]$", s)
    if m:
        return int(m.group(1))
    if not s.isdigit():
        return None
    n = int(s)
    return n - STEAM64_BASE if n > STEAM64_BASE else n


def calc_position(matches):
    """Return (primary, secondary) position strings; each may be None."""
    if not matches:
        return None, None
    counts = {"Pos 1": 0, "Pos 2": 0, "Pos 3": 0, "Pos 4": 0, "Pos 5": 0}
    parsed = 0
    for m in matches:
        lane = m.get("lane_role")
        gpm = m.get("gold_per_min") or 0
        if lane == 1:
            counts["Pos 1" if gpm >= 400 else "Pos 5"] += 1
            parsed += 1
        elif lane == 2:
            counts["Pos 2"] += 1
            parsed += 1
        elif lane == 3:
            counts["Pos 3" if gpm >= 400 else "Pos 4"] += 1
            parsed += 1
    if parsed == 0:
        gpms = [m.get("gold_per_min") or 0 for m in matches if m.get("gold_per_min")]
        if not gpms:
            return None, None
        avg = sum(gpms) / len(gpms)
        for threshold, pos in [
            (450, "Pos 1"),
            (400, "Pos 2"),
            (350, "Pos 3"),
            (300, "Pos 4"),
        ]:
            if avg >= threshold:
                return pos + "~", None
        return "Pos 5~", None
    ranked = sorted([(pos, cnt) for pos, cnt in counts.items() if cnt > 0], key=lambda x: -x[1])
    def fmt(pos, cnt):
        return f"{pos} ({cnt / parsed * 100:.0f}%)"
    primary   = fmt(*ranked[0]) if ranked else None
    secondary = fmt(*ranked[1]) if len(ranked) > 1 else None
    return primary, secondary


async def get_opendota_cache(account_id: int) -> dict | None:
    if not _pool:
        log.warning("cache read skipped: pool is None")
        return None
    try:
        row = await _pool.fetchrow(
            """
            SELECT rank_tier, leaderboard_rank, recent_matches,
                   is_smurf, real_rank_tier, real_leaderboard_rank
            FROM opendota_cache
            WHERE account_id = $1 AND cached_at > NOW() - INTERVAL '30 days'
            """,
            account_id,
        )
    except Exception as e:
        log.error("cache read failed for %s: %s", account_id, e)
        return None
    if row is None:
        log.info("cache miss for %s", account_id)
        return None
    log.info("cache hit for %s", account_id)
    return {
        "rank_tier": row["rank_tier"],
        "leaderboard_rank": row["leaderboard_rank"],
        "recent_matches": row["recent_matches"],
        "is_smurf": row["is_smurf"],
        "real_rank_tier": row["real_rank_tier"],
        "real_leaderboard_rank": row["real_leaderboard_rank"],
    }


async def get_smurf_info(account_id: int) -> dict | None:
    """Fetch manual smurf flags regardless of cache age."""
    if not _pool:
        return None
    try:
        row = await _pool.fetchrow(
            """
            SELECT is_smurf, real_rank_tier, real_leaderboard_rank
            FROM opendota_cache
            WHERE account_id = $1
            """,
            account_id,
        )
    except Exception as e:
        log.error("smurf read failed for %s: %s", account_id, e)
        return None
    if row is None or not row["is_smurf"]:
        return None
    return {
        "is_smurf": True,
        "real_rank_tier": row["real_rank_tier"],
        "real_leaderboard_rank": row["real_leaderboard_rank"],
    }


async def set_opendota_cache(
    account_id: int,
    rank_tier: int | None,
    leaderboard_rank: int | None,
    recent_matches: list,
    nickname: str | None = None,
    avatar: str | None = None,
    faceit_player_id: str | None = None,
    faceit_level: int | None = None,
    faceit_elo: int | None = None,
) -> None:
    if not _pool:
        log.warning("cache write skipped: pool is None")
        return
    try:
        await _pool.execute(
            """
            INSERT INTO opendota_cache (
                account_id, rank_tier, leaderboard_rank, recent_matches, cached_at,
                nickname, avatar, faceit_player_id, faceit_level, faceit_elo
            )
            VALUES ($1, $2, $3, $4::jsonb, NOW(), $5, $6, $7, $8, $9)
            ON CONFLICT (account_id) DO UPDATE
            SET rank_tier        = EXCLUDED.rank_tier,
                leaderboard_rank = EXCLUDED.leaderboard_rank,
                recent_matches   = EXCLUDED.recent_matches,
                cached_at        = EXCLUDED.cached_at,
                nickname         = COALESCE(EXCLUDED.nickname, opendota_cache.nickname),
                avatar           = COALESCE(EXCLUDED.avatar, opendota_cache.avatar),
                faceit_player_id = COALESCE(EXCLUDED.faceit_player_id, opendota_cache.faceit_player_id),
                faceit_level     = COALESCE(EXCLUDED.faceit_level, opendota_cache.faceit_level),
                faceit_elo       = COALESCE(EXCLUDED.faceit_elo, opendota_cache.faceit_elo)
            """,
            account_id,
            rank_tier,
            leaderboard_rank,
            json.dumps(recent_matches),
            nickname,
            avatar,
            faceit_player_id,
            faceit_level,
            faceit_elo,
        )
        log.info("cache write ok for %s", account_id)
    except Exception as e:
        log.error("cache write failed for %s: %s", account_id, e)


async def faceit_get(s, path):
    h = {"Authorization": f"Bearer {FACEIT_KEY}"}
    timeout = aiohttp.ClientTimeout(total=10)
    async with s.get(f"{FACEIT_BASE}{path}", headers=h, timeout=timeout) as r:
        r.raise_for_status()
        return await r.json()


async def opendota_get(s, path):
    timeout = aiohttp.ClientTimeout(total=15)
    async with s.get(f"{OPENDOTA_BASE}{path}", timeout=timeout) as r:
        if r.status == 404:
            return None
        r.raise_for_status()
        return await r.json()


async def fetch_player(session, roster_entry):
    pid = roster_entry.get("player_id", "")
    nickname = roster_entry.get("nickname", "?")
    avatar = roster_entry.get("avatar", "")
    skill_level = roster_entry.get("skill_level", 0)
    account_id = to_account_id(roster_entry.get("game_player_id"))

    result = {
        "player_id": pid,
        "account_id": account_id,
        "nickname": nickname,
        "avatar": avatar,
        "faceit_elo": None,
        "faceit_level": skill_level,
        "dota_rank_major": 0,
        "dota_rank": "Unranked",
        "position": None,
        "position2": None,
        "is_smurf": False,
        "real_rank_major": 0,
        "real_rank": None,
        "opendota_link": f"https://www.opendota.com/players/{account_id}"
        if account_id
        else None,
    }

    try:
        # Always fetch fresh Faceit data
        fp_responses = await asyncio.gather(
            faceit_get(session, f"/players/{pid}"),
            return_exceptions=True,
        )
        fp = fp_responses[0] if not isinstance(fp_responses[0], Exception) else None
        if fp:
            dota = (fp.get("games") or {}).get("dota2") or {}
            result["faceit_elo"] = dota.get("faceit_elo")
            result["faceit_level"] = dota.get("skill_level") or skill_level

        if account_id is None:
            return result

        # Check cache before hitting OpenDota
        cached = await get_opendota_cache(account_id)
        smurf_row = None
        if cached:
            profile = {
                "rank_tier": cached["rank_tier"],
                "leaderboard_rank": cached["leaderboard_rank"],
            }
            recent = cached["recent_matches"]
            smurf_row = cached
        else:
            # Cache miss — fetch from OpenDota
            od_responses = await asyncio.gather(
                opendota_get(session, f"/players/{account_id}"),
                opendota_get(session, f"/players/{account_id}/recentMatches"),
                return_exceptions=True,
            )
            profile = od_responses[0] if not isinstance(od_responses[0], Exception) else None
            recent = od_responses[1] if not isinstance(od_responses[1], Exception) else None
            # Save to cache (preserves manual smurf fields via UPSERT SET clause)
            if profile or recent:
                await set_opendota_cache(
                    account_id,
                    profile.get("rank_tier") if profile else None,
                    profile.get("leaderboard_rank") if profile else None,
                    recent or [],
                    nickname=nickname,
                    avatar=avatar,
                    faceit_player_id=pid,
                    faceit_level=result["faceit_level"],
                    faceit_elo=result["faceit_elo"],
                )
            # Fresh write doesn't return smurf flags — fetch them explicitly
            smurf_row = await get_smurf_info(account_id)

        if profile:
            major, label = rank_label(
                profile.get("rank_tier"), profile.get("leaderboard_rank")
            )
            result["dota_rank_major"] = major
            result["dota_rank"] = label

        if recent:
            result["position"], result["position2"] = calc_position(recent)

        if smurf_row and smurf_row.get("is_smurf"):
            result["is_smurf"] = True
            rmajor, rlabel = rank_label(
                smurf_row.get("real_rank_tier"),
                smurf_row.get("real_leaderboard_rank"),
            )
            result["real_rank_major"] = rmajor
            result["real_rank"] = rlabel

    except Exception:
        pass

    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    if DATABASE_URL:
        try:
            _pool = await asyncpg.create_pool(
                DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0
            )
            async with _pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            log.info("db pool ready")
        except Exception as e:
            log.error("db pool init failed: %s", e)
            _pool = None
    else:
        log.warning("DATABASE_URL not set — caching disabled")
    yield
    if _pool:
        await _pool.close()
        _pool = None


app = FastAPI(lifespan=lifespan)

# Use APP_BASE_PATH from launcher.py for bundled .exe
BASE_PATH = os.environ.get("APP_BASE_PATH", os.path.dirname(__file__))
app.mount(
    "/photo", StaticFiles(directory=os.path.join(BASE_PATH, "photo")), name="photo"
)


@app.get("/")
async def index():
    path = os.path.join(BASE_PATH, "index.html")
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/match/{room_id}")
async def get_match(room_id: str):
    try:
        async with aiohttp.ClientSession() as session:
            match_data = await faceit_get(session, f"/matches/{room_id}")
            teams_raw = match_data.get("teams", {})

            all_tasks, team_keys = [], []
            for fk in ["faction1", "faction2"]:
                for p in teams_raw.get(fk, {}).get("roster", []):
                    all_tasks.append(fetch_player(session, p))
                    team_keys.append(fk)

            players_results = await asyncio.gather(*all_tasks, return_exceptions=True)

            teams = {
                fk: {
                    "name": teams_raw.get(fk, {}).get("name", fk),
                    "avatar": teams_raw.get(fk, {}).get("avatar", ""),
                    "captain_id": teams_raw.get(fk, {}).get("leader", ""),
                    "players": [],
                }
                for fk in ["faction1", "faction2"]
            }

            for i, r in enumerate(players_results):
                fk = team_keys[i]
                teams[fk]["players"].append(
                    r
                    if not isinstance(r, Exception)
                    else {
                        "nickname": "Error",
                        "faceit_level": 0,
                        "dota_rank": "—",
                        "position": None,
                        "position2": None,
                        "avatar": "",
                    }
                )

            return {
                "match_id": room_id,
                "status": match_data.get("status", ""),
                "competition": match_data.get("competition_name", ""),
                "region": match_data.get("region", ""),
                "team1": teams["faction1"],
                "team2": teams["faction2"],
            }

    except aiohttp.ClientResponseError as e:
        raise HTTPException(status_code=e.status, detail=f"Faceit API {e.status}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/player/{player_id}")
async def get_player(player_id: str):
    """Fetch full data for a single player by Faceit player_id."""
    try:
        async with aiohttp.ClientSession() as session:
            fp_data = await faceit_get(session, f"/players/{player_id}")
            nickname = fp_data.get("nickname", "?")
            avatar = fp_data.get("avatar", "")
            dota = (fp_data.get("games") or {}).get("dota2") or {}
            faceit_elo = dota.get("faceit_elo")
            faceit_level = dota.get("skill_level") or 0
            account_id = to_account_id(dota.get("game_player_id"))

            result = {
                "player_id": player_id,
                "account_id": account_id,
                "nickname": nickname,
                "avatar": avatar,
                "faceit_elo": faceit_elo,
                "faceit_level": faceit_level,
                "dota_rank_major": 0,
                "dota_rank": "Unranked",
                "position": None,
                "position2": None,
                "is_smurf": False,
                "real_rank_major": 0,
                "real_rank": None,
                "opendota_link": f"https://www.opendota.com/players/{account_id}"
                if account_id
                else None,
            }

            if account_id:
                cached = await get_opendota_cache(account_id)
                smurf_row = None
                if cached:
                    profile = {
                        "rank_tier": cached["rank_tier"],
                        "leaderboard_rank": cached["leaderboard_rank"],
                    }
                    recent = cached["recent_matches"]
                    smurf_row = cached
                else:
                    profile, recent = await asyncio.gather(
                        opendota_get(session, f"/players/{account_id}"),
                        opendota_get(session, f"/players/{account_id}/recentMatches"),
                        return_exceptions=True,
                    )
                    if isinstance(profile, Exception):
                        profile = None
                    if isinstance(recent, Exception):
                        recent = None
                    if profile or recent:
                        await set_opendota_cache(
                            account_id,
                            profile.get("rank_tier") if profile else None,
                            profile.get("leaderboard_rank") if profile else None,
                            recent or [],
                            nickname=nickname,
                            avatar=avatar,
                            faceit_player_id=player_id,
                            faceit_level=faceit_level,
                            faceit_elo=faceit_elo,
                        )
                    smurf_row = await get_smurf_info(account_id)

                if profile:
                    major, label = rank_label(
                        profile.get("rank_tier"), profile.get("leaderboard_rank")
                    )
                    result["dota_rank_major"] = major
                    result["dota_rank"] = label
                if recent:
                    result["position"], result["position2"] = calc_position(recent)

                if smurf_row and smurf_row.get("is_smurf"):
                    result["is_smurf"] = True
                    rmajor, rlabel = rank_label(
                        smurf_row.get("real_rank_tier"),
                        smurf_row.get("real_leaderboard_rank"),
                    )
                    result["real_rank_major"] = rmajor
                    result["real_rank"] = rlabel

            return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/match/{room_id}/poll")
async def poll_match(room_id: str):
    """Lightweight poll — returns only Faceit data, no OpenDota. Used for auto-refresh."""
    try:
        async with aiohttp.ClientSession() as session:
            match_data = await faceit_get(session, f"/matches/{room_id}")
            teams_raw = match_data.get("teams", {})

            def faction_players(fk):
                faction = teams_raw.get(fk, {})
                return {
                    "name": faction.get("name", fk),
                    "avatar": faction.get("avatar", ""),
                    "captain_id": faction.get("leader", ""),
                    "players": [
                        {
                            "player_id": p.get("player_id", ""),
                            "nickname": p.get("nickname", "?"),
                            "avatar": p.get("avatar", ""),
                            "faceit_level": p.get("game_skill_level")
                            or p.get("skill_level")
                            or 0,
                            "game_player_id": p.get("game_player_id", ""),
                        }
                        for p in faction.get("roster", [])
                    ],
                }

            return {
                "status": match_data.get("status", ""),
                "team1": faction_players("faction1"),
                "team2": faction_players("faction2"),
            }
    except aiohttp.ClientResponseError as e:
        raise HTTPException(status_code=e.status, detail=f"Faceit API {e.status}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ReportCreate(BaseModel):
    account_id: int
    nickname: str | None = None
    real_rank_tier: int | None = None
    real_leaderboard_rank: int | None = None
    match_room_id: str | None = None
    discord: str | None = None


@app.post("/api/report")
async def submit_report(body: ReportCreate):
    if not _pool:
        raise HTTPException(status_code=503, detail="db unavailable")
    row = await _pool.fetchrow(
        """
        INSERT INTO smurf_reports (account_id, nickname, real_rank_tier, real_leaderboard_rank, match_room_id, discord)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        body.account_id,
        body.nickname,
        body.real_rank_tier,
        body.real_leaderboard_rank,
        body.match_room_id,
        body.discord,
    )
    return {"ok": True, "report_id": row["id"]}


@app.get("/api/admin/reports")
async def admin_list_reports(admin_session: str | None = Cookie(default=None)):
    if not _is_admin(admin_session):
        raise HTTPException(status_code=403, detail="forbidden")
    if not _pool:
        raise HTTPException(status_code=503, detail="db unavailable")
    rows = await _pool.fetch(
        """
        SELECT id, account_id, nickname, real_rank_tier, real_leaderboard_rank,
               match_room_id, created_at, reviewed, action_taken, discord
        FROM smurf_reports
        ORDER BY reviewed ASC, created_at DESC
        LIMIT 200
        """
    )
    out = []
    for r in rows:
        real_major, real_label = (0, None)
        if r["real_rank_tier"]:
            real_major, real_label = rank_label(r["real_rank_tier"], r["real_leaderboard_rank"])
        out.append({
            "id": r["id"],
            "account_id": r["account_id"],
            "nickname": r["nickname"],
            "real_rank_tier": r["real_rank_tier"],
            "real_leaderboard_rank": r["real_leaderboard_rank"],
            "real_rank_major": real_major,
            "real_rank": real_label,
            "match_room_id": r["match_room_id"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "reviewed": r["reviewed"],
            "action_taken": r["action_taken"],
            "discord": r["discord"],
        })
    return {"reports": out}


@app.post("/api/admin/reports/{report_id}/review")
async def admin_review_report(report_id: int, action: str = "dismiss", admin_session: str | None = Cookie(default=None)):
    if not _is_admin(admin_session):
        raise HTTPException(status_code=403, detail="forbidden")
    if not _pool:
        raise HTTPException(status_code=503, detail="db unavailable")
    res = await _pool.execute(
        """
        UPDATE smurf_reports
        SET reviewed = true, reviewed_at = NOW(), action_taken = $2
        WHERE id = $1
        """,
        report_id, action,
    )
    if res.endswith(" 0"):
        raise HTTPException(status_code=404, detail="report not found")
    return {"ok": True}


@app.get("/api/smurfs")
async def get_smurfs(ids: str = ""):
    """Return smurf flags for a comma-separated list of account_ids."""
    if not _pool or not ids.strip():
        return {"smurfs": {}}
    try:
        raw = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
    except Exception:
        return {"smurfs": {}}
    if not raw:
        return {"smurfs": {}}
    rows = await _pool.fetch(
        """
        SELECT account_id, is_smurf, real_rank_tier, real_leaderboard_rank
        FROM opendota_cache
        WHERE account_id = ANY($1::bigint[]) AND is_smurf = true
        """,
        raw,
    )
    result = {}
    for r in rows:
        rmajor, rlabel = rank_label(r["real_rank_tier"], r["real_leaderboard_rank"])
        result[str(r["account_id"])] = {
            "is_smurf": True,
            "real_rank_major": rmajor,
            "real_rank": rlabel,
        }
    return {"smurfs": result}


@app.post("/api/admin/backfill")
async def admin_backfill(admin_session: str | None = Cookie(default=None)):
    """Fetch Faceit data for cached players that are missing nicknames."""
    if not _is_admin(admin_session):
        raise HTTPException(status_code=403, detail="forbidden")
    if not _pool:
        raise HTTPException(status_code=503, detail="db unavailable")
    rows = await _pool.fetch(
        "SELECT account_id FROM opendota_cache WHERE nickname IS NULL LIMIT 200"
    )
    if not rows:
        return {"updated": 0}

    updated = 0
    async with aiohttp.ClientSession() as session:
        for row in rows:
            account_id = row["account_id"]
            steam64 = account_id + STEAM64_BASE
            try:
                data = await faceit_get(session, f"/players?game=dota2&game_player_id={steam64}")
                pid = data.get("player_id", "")
                nickname = data.get("nickname") or None
                avatar = data.get("avatar") or None
                dota = (data.get("games") or {}).get("dota2") or {}
                faceit_level = dota.get("skill_level") or None
                faceit_elo = dota.get("faceit_elo") or None
                await _pool.execute(
                    """
                    UPDATE opendota_cache
                    SET nickname         = $2,
                        avatar           = $3,
                        faceit_player_id = $4,
                        faceit_level     = $5,
                        faceit_elo       = $6
                    WHERE account_id = $1
                    """,
                    account_id, nickname, avatar, pid, faceit_level, faceit_elo,
                )
                updated += 1
            except Exception as e:
                log.warning("backfill skip %s: %s", account_id, e)

    return {"updated": updated}


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin · Вхід</title>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#111;color:#f0f0f0;font-family:'Rajdhani',sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#181818;border:1px solid #2a2a2a;padding:2rem 2.5rem;width:320px}
h1{font-size:1.1rem;letter-spacing:.2em;text-transform:uppercase;color:#FF5500;margin-bottom:1.5rem}
input{width:100%;background:#202020;border:1px solid #2a2a2a;color:#f0f0f0;
  padding:.65rem .9rem;font-family:inherit;font-size:1rem;margin-bottom:.8rem;outline:none}
input:focus{border-color:#FF5500}
button{width:100%;background:#FF5500;border:none;color:#111;
  padding:.65rem;font-family:inherit;font-size:.9rem;font-weight:700;
  letter-spacing:.1em;text-transform:uppercase;cursor:pointer}
button:hover{filter:brightness(1.08)}
.err{color:#ff3b3b;font-size:.82rem;margin-bottom:.7rem}
</style>
</head>
<body>
<div class="box">
  <h1>Admin · Вхід</h1>
  {error}
  <form method="post" action="/admin/login">
    <input type="password" name="password" placeholder="Пароль" autofocus autocomplete="current-password">
    <button type="submit">Увійти</button>
  </form>
</div>
</body>
</html>"""


def _is_admin(admin_session: str | None) -> bool:
    return admin_session == ADMIN_TOKEN


# ── OAuth helpers ──────────────────────────────────────────────────────────
def create_jwt_token(faceit_user: dict) -> str:
    """Create JWT token with user data."""
    payload = {
        "faceit_id": faceit_user["player_id"],
        "nickname": faceit_user["nickname"],
        "avatar": faceit_user.get("avatar"),
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_jwt_token(token: str) -> dict | None:
    """Verify JWT and return payload, or None if invalid."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def get_current_user(request: Request) -> dict | None:
    """Extract user from JWT cookie."""
    token = request.cookies.get("faceit_token")
    if not token:
        return None
    return verify_jwt_token(token)


async def save_user_to_db(faceit_id: str, nickname: str, avatar: str | None):
    """Save or update user in database."""
    if not _pool:
        return
    try:
        await _pool.execute(
            """
            INSERT INTO users (faceit_id, nickname, avatar, last_login)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (faceit_id)
            DO UPDATE SET
                nickname = EXCLUDED.nickname,
                avatar = EXCLUDED.avatar,
                last_login = NOW()
            """,
            faceit_id,
            nickname,
            avatar,
        )
    except Exception as e:
        log.error("Failed to save user to DB: %s", e)


# ── OAuth endpoints ────────────────────────────────────────────────────────
@app.get("/auth/login")
async def auth_login():
    """Redirect to Faceit OAuth."""
    if not FACEIT_CLIENT_ID:
        raise HTTPException(status_code=500, detail="OAuth not configured")
    auth_url = (
        f"https://accounts.faceit.com/oauth/authorize"
        f"?client_id={FACEIT_CLIENT_ID}"
        f"&redirect_uri={FACEIT_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=openid email profile"
    )
    return RedirectResponse(auth_url)


@app.get("/auth/callback")
async def auth_callback(code: str | None = None, error: str | None = None):
    """Handle Faceit OAuth callback."""
    log.info(f"OAuth callback: code={code[:20] if code else None}, error={error}")
    if error or not code:
        log.error(f"OAuth callback error: {error}")
        return RedirectResponse("/?auth_error=1")
    
    if not FACEIT_CLIENT_ID or not FACEIT_CLIENT_SECRET:
        log.error("OAuth not configured")
        raise HTTPException(status_code=500, detail="OAuth not configured")
    
    # Exchange code for token
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                "https://accounts.faceit.com/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": FACEIT_REDIRECT_URI,
                    "client_id": FACEIT_CLIENT_ID,
                    "client_secret": FACEIT_CLIENT_SECRET,
                },
            ) as resp:
                if resp.status != 200:
                    log.error("OAuth token exchange failed: %s", await resp.text())
                    return RedirectResponse("/?auth_error=2")
                token_data = await resp.json()
                access_token = token_data.get("access_token")
            
            if not access_token:
                return RedirectResponse("/?auth_error=3")
            
            # Get user info
            async with session.get(
                "https://api.faceit.com/auth/v1/resources/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as resp:
                if resp.status != 200:
                    log.error("OAuth userinfo failed: %s", await resp.text())
                    return RedirectResponse("/?auth_error=4")
                user_data = await resp.json()
            
            # Save user to database
            await save_user_to_db(
                user_data["player_id"],
                user_data["nickname"],
                user_data.get("avatar"),
            )
            
            # Create JWT
            jwt_token = create_jwt_token(user_data)
            
            # Redirect with cookie
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                "faceit_token",
                jwt_token,
                httponly=True,
                samesite="lax",
                max_age=60 * 60 * 24 * JWT_EXPIRE_DAYS,
            )
            return response
            
        except Exception as e:
            log.error("OAuth error: %s", e)
            return RedirectResponse("/?auth_error=5")


@app.get("/auth/logout")
async def auth_logout():
    """Logout user."""
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("faceit_token")
    return response


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """Get current user info."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"authenticated": False}, status_code=200)
    return JSONResponse({
        "authenticated": True,
        "faceit_id": user["faceit_id"],
        "nickname": user["nickname"],
        "avatar": user.get("avatar"),
    })


@app.get("/admin")
async def admin_page(admin_session: str | None = Cookie(default=None)):
    if not _is_admin(admin_session):
        return HTMLResponse(_LOGIN_HTML.replace("{error}", ""), status_code=200)
    path = os.path.join(BASE_PATH, "admin.html")
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/admin/login")
async def admin_login(password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        resp = RedirectResponse(url="/admin", status_code=303)
        resp.set_cookie("admin_session", ADMIN_TOKEN, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
        return resp
    html = _LOGIN_HTML.replace("{error}", '<p class="err">Невірний пароль</p>')
    return HTMLResponse(html, status_code=200)


@app.get("/api/admin/players")
async def admin_list_players(admin_session: str | None = Cookie(default=None)):
    if not _is_admin(admin_session):
        raise HTTPException(status_code=403, detail="forbidden")
    if not _pool:
        raise HTTPException(status_code=503, detail="db unavailable")
    rows = await _pool.fetch(
        """
        SELECT account_id, nickname, avatar, faceit_player_id,
               faceit_level, faceit_elo, rank_tier, leaderboard_rank,
               is_smurf, real_rank_tier, real_leaderboard_rank,
               cached_at, updated_manually_at
        FROM opendota_cache
        ORDER BY COALESCE(updated_manually_at, cached_at) DESC
        LIMIT 1000
        """
    )
    out = []
    for r in rows:
        major, label = rank_label(r["rank_tier"], r["leaderboard_rank"])
        real_major, real_label = (0, None)
        if r["is_smurf"]:
            real_major, real_label = rank_label(r["real_rank_tier"], r["real_leaderboard_rank"])
        out.append({
            "account_id": r["account_id"],
            "nickname": r["nickname"],
            "avatar": r["avatar"],
            "faceit_player_id": r["faceit_player_id"],
            "faceit_level": r["faceit_level"],
            "faceit_elo": r["faceit_elo"],
            "rank_tier": r["rank_tier"],
            "leaderboard_rank": r["leaderboard_rank"],
            "dota_rank_major": major,
            "dota_rank": label,
            "is_smurf": r["is_smurf"],
            "real_rank_tier": r["real_rank_tier"],
            "real_leaderboard_rank": r["real_leaderboard_rank"],
            "real_rank_major": real_major,
            "real_rank": real_label,
            "cached_at": r["cached_at"].isoformat() if r["cached_at"] else None,
            "updated_manually_at": r["updated_manually_at"].isoformat() if r["updated_manually_at"] else None,
        })
    return {"players": out}


class SmurfUpdate(BaseModel):
    is_smurf: bool
    real_rank_tier: int | None = None
    real_leaderboard_rank: int | None = None


@app.post("/api/admin/smurf/{account_id}")
async def admin_set_smurf(account_id: int, body: SmurfUpdate, admin_session: str | None = Cookie(default=None)):
    if not _is_admin(admin_session):
        raise HTTPException(status_code=403, detail="forbidden")
    if not _pool:
        raise HTTPException(status_code=503, detail="db unavailable")
    if body.is_smurf and body.real_rank_tier is None:
        raise HTTPException(status_code=400, detail="real_rank_tier required when is_smurf=true")
    result = await _pool.execute(
        """
        UPDATE opendota_cache
        SET is_smurf              = $2,
            real_rank_tier        = $3,
            real_leaderboard_rank = $4,
            updated_manually_at   = NOW()
        WHERE account_id = $1
        """,
        account_id,
        body.is_smurf,
        body.real_rank_tier if body.is_smurf else None,
        body.real_leaderboard_rank if body.is_smurf else None,
    )
    if result.endswith(" 0"):
        raise HTTPException(status_code=404, detail="player not in cache")
    return {"ok": True, "account_id": account_id, "is_smurf": body.is_smurf}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
