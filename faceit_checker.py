#!/usr/bin/env python3
"""Faceit Dota 2 draft helper — fetch rank and primary position for 10 players fast."""
import argparse
import asyncio
import re
import sys
from urllib.parse import urlparse

import aiohttp
from rich.console import Console
from rich.table import Table

FACEIT_API_KEY = "1ca837fd-a345-47c8-9adc-e78f717489e8"

FACEIT_BASE = "https://open.faceit.com/data/v4"
OPENDOTA_BASE = "https://api.opendota.com/api"
STEAM64_BASE = 76561197960265728

RANK_NAMES = {
    1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon",
    5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal",
}


def rank_tier_to_medal(tier, leaderboard=None):
    if tier is None:
        return "Unranked"
    try:
        t = int(tier)
    except (TypeError, ValueError):
        return "Unranked"
    major, minor = t // 10, t % 10
    name = RANK_NAMES.get(major, "Unknown")
    if major == 8 and leaderboard:
        return f"Immortal #{leaderboard}"
    if major == 8:
        return "Immortal"
    return f"{name} {minor}" if minor else name


def to_account_id(sid):
    if sid is None:
        return None
    s = str(sid).strip()
    m = re.match(r"^\[U:\d+:(\d+)\]$", s)
    if m:
        return int(m.group(1))
    if not s.isdigit():
        return None
    n = int(s)
    return n - STEAM64_BASE if n > STEAM64_BASE else n


def nickname_from_url(url):
    if "/" not in url:
        return url
    parts = [p for p in urlparse(url).path.split("/") if p]
    if "players" in parts:
        idx = parts.index("players")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return parts[-1]


async def faceit_get(session, path):
    headers = {"Authorization": f"Bearer {FACEIT_API_KEY}"}
    async with session.get(f"{FACEIT_BASE}{path}", headers=headers, timeout=10) as r:
        r.raise_for_status()
        return await r.json()


async def opendota_get(session, path):
    async with session.get(f"{OPENDOTA_BASE}{path}", timeout=15) as r:
        if r.status == 404:
            return None
        r.raise_for_status()
        return await r.json()


async def fetch_match_nicknames(session, room_id):
    data = await faceit_get(session, f"/matches/{room_id}")
    seen = set()
    nicks = []
    for faction in data.get("teams", {}).values():
        players = list(faction.get("roster", []) or [])
        leader_id = faction.get("leader")
        if leader_id and not any(p.get("player_id") == leader_id for p in players):
            cap = faction.get("captain") or {}
            if cap.get("nickname"):
                players.append(cap)
        for p in players:
            pid = p.get("player_id") or p.get("nickname")
            nick = p.get("nickname")
            if nick and pid not in seen:
                seen.add(pid)
                nicks.append(nick)
    return nicks


async def fetch_faceit_player(session, nickname):
    data = await faceit_get(session, f"/players?nickname={nickname}")
    games = data.get("games", {})
    dota = games.get("dota2") or {}
    steam_id = dota.get("game_player_id") or data.get("steam_id_64")
    return {"nickname": data.get("nickname", nickname), "steam_id_64": steam_id}


def primary_position(recent_matches):
    """
    Use recentMatches (20 games from OpenDota).
    Each match has gold_per_min always. lane_role only if parsed.
    - lane_role 1 (safe):  GPM >= 400 → Pos 1, else Pos 5
    - lane_role 2 (mid):   Pos 2
    - lane_role 3 (off):   GPM >= 400 → Pos 3, else Pos 4
    Count per position, return the top one as string e.g. "Pos 1 (60%)".
    Fallback if no lane_role: use GPM only to guess core vs support.
    """
    if not recent_matches:
        return "No Data"

    counts = {"Pos 1": 0, "Pos 2": 0, "Pos 3": 0, "Pos 4": 0, "Pos 5": 0}
    parsed = 0

    for m in recent_matches:
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

    # fallback: no parsed lanes → use GPM to classify core/support globally
    if parsed == 0:
        gpms = [m.get("gold_per_min") or 0 for m in recent_matches if m.get("gold_per_min")]
        if not gpms:
            return "No Data"
        avg = sum(gpms) / len(gpms)
        if avg >= 450:
            return "Pos 1 (est.)"
        elif avg >= 400:
            return "Pos 2 (est.)"
        elif avg >= 350:
            return "Pos 3 (est.)"
        elif avg >= 300:
            return "Pos 4 (est.)"
        else:
            return "Pos 5 (est.)"

    top_pos, top_cnt = max(counts.items(), key=lambda x: x[1])
    pct = top_cnt / parsed * 100
    return f"{top_pos} ({pct:.0f}%)"


async def process_player(session, nickname):
    result = {"faceit_name": nickname, "rank": "—", "pos": "—", "link": "—"}
    try:
        fp = await fetch_faceit_player(session, nickname)
        result["faceit_name"] = fp["nickname"]
        account_id = to_account_id(fp["steam_id_64"])
        if account_id is None:
            result["rank"] = f"No Steam (raw={fp['steam_id_64']!r})"
            return result
        result["link"] = f"https://www.opendota.com/players/{account_id}"

        profile, recent = await asyncio.gather(
            opendota_get(session, f"/players/{account_id}"),
            opendota_get(session, f"/players/{account_id}/recentMatches"),
            return_exceptions=True,
        )

        if isinstance(profile, Exception) or not profile:
            result["rank"] = "Private / No Data"
        else:
            result["rank"] = rank_tier_to_medal(
                profile.get("rank_tier"),
                profile.get("leaderboard_rank"),
            )

        if isinstance(recent, Exception) or not recent:
            result["pos"] = "Private / No Data"
        else:
            result["pos"] = primary_position(recent)

    except aiohttp.ClientResponseError as e:
        result["rank"] = f"Faceit err {e.status}"
    except Exception as e:
        result["rank"] = f"Err: {type(e).__name__}: {e}"
    return result


async def run(nicknames):
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(process_player(session, n) for n in nicknames))
    return results


async def resolve_inputs(args):
    async with aiohttp.ClientSession() as session:
        if args.match:
            return await fetch_match_nicknames(session, args.match)
        return [nickname_from_url(p) for p in args.players]


def render(results):
    table = Table(title="Faceit Dota 2 Draft Scout", show_lines=False)
    table.add_column("Faceit Name", style="bold cyan")
    table.add_column("Rank", style="yellow")
    table.add_column("Position", style="green")
    table.add_column("OpenDota", style="blue")
    for r in results:
        table.add_row(r["faceit_name"], r["rank"], r["pos"], r["link"])
    Console().print(table)


def main():
    parser = argparse.ArgumentParser(description="Faceit Dota 2 draft scout")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--match", help="Faceit Match Room ID")
    group.add_argument("--players", nargs="+", help="Faceit player URLs or nicknames")
    args = parser.parse_args()

    nicknames = asyncio.run(resolve_inputs(args))
    if not nicknames:
        print("No players resolved.", file=sys.stderr)
        sys.exit(1)
    results = asyncio.run(run(nicknames))
    render(results)


if __name__ == "__main__":
    main()
