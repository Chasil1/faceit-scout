#!/usr/bin/env python3
"""Faceit Dota 2 Scout — FastAPI web server."""

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager

import aiohttp
import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("faceit")

FACEIT_KEY = os.environ.get("FACEIT_KEY", "1ca837fd-a345-47c8-9adc-e78f717489e8")
FACEIT_BASE = "https://open.faceit.com/data/v4"
OPENDOTA_BASE = "https://api.opendota.com/api"
STEAM64_BASE = 76561197960265728
DATABASE_URL = os.environ.get("DATABASE_URL")

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
            SELECT rank_tier, leaderboard_rank, recent_matches
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
    }


async def set_opendota_cache(
    account_id: int,
    rank_tier: int | None,
    leaderboard_rank: int | None,
    recent_matches: list,
) -> None:
    if not _pool:
        log.warning("cache write skipped: pool is None")
        return
    try:
        await _pool.execute(
            """
            INSERT INTO opendota_cache (account_id, rank_tier, leaderboard_rank, recent_matches, cached_at)
            VALUES ($1, $2, $3, $4::jsonb, NOW())
            ON CONFLICT (account_id) DO UPDATE
            SET rank_tier        = EXCLUDED.rank_tier,
                leaderboard_rank = EXCLUDED.leaderboard_rank,
                recent_matches   = EXCLUDED.recent_matches,
                cached_at        = EXCLUDED.cached_at
            """,
            account_id,
            rank_tier,
            leaderboard_rank,
            json.dumps(recent_matches),
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
        "nickname": nickname,
        "avatar": avatar,
        "faceit_elo": None,
        "faceit_level": skill_level,
        "dota_rank_major": 0,
        "dota_rank": "Unranked",
        "position": None,
        "position2": None,
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
        if cached:
            profile = {
                "rank_tier": cached["rank_tier"],
                "leaderboard_rank": cached["leaderboard_rank"],
            }
            recent = cached["recent_matches"]
        else:
            # Cache miss — fetch from OpenDota
            od_responses = await asyncio.gather(
                opendota_get(session, f"/players/{account_id}"),
                opendota_get(session, f"/players/{account_id}/recentMatches"),
                return_exceptions=True,
            )
            profile = od_responses[0] if not isinstance(od_responses[0], Exception) else None
            recent = od_responses[1] if not isinstance(od_responses[1], Exception) else None
            # Save to cache
            if profile or recent:
                await set_opendota_cache(
                    account_id,
                    profile.get("rank_tier") if profile else None,
                    profile.get("leaderboard_rank") if profile else None,
                    recent or [],
                )

        if profile:
            major, label = rank_label(
                profile.get("rank_tier"), profile.get("leaderboard_rank")
            )
            result["dota_rank_major"] = major
            result["dota_rank"] = label

        if recent:
            result["position"], result["position2"] = calc_position(recent)

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
                "nickname": nickname,
                "avatar": avatar,
                "faceit_elo": faceit_elo,
                "faceit_level": faceit_level,
                "dota_rank_major": 0,
                "dota_rank": "Unranked",
                "position": None,
                "position2": None,
                "opendota_link": f"https://www.opendota.com/players/{account_id}"
                if account_id
                else None,
            }

            if account_id:
                cached = await get_opendota_cache(account_id)
                if cached:
                    profile = {
                        "rank_tier": cached["rank_tier"],
                        "leaderboard_rank": cached["leaderboard_rank"],
                    }
                    recent = cached["recent_matches"]
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
                        )

                if profile:
                    major, label = rank_label(
                        profile.get("rank_tier"), profile.get("leaderboard_rank")
                    )
                    result["dota_rank_major"] = major
                    result["dota_rank"] = label
                if recent:
                    result["position"], result["position2"] = calc_position(recent)

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


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
