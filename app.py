#!/usr/bin/env python3
"""
WAVE Pre-Live Favorites

Dashboard para encontrar jogos pre-live onde o mandante favorito passa em pelo
menos 3 das 4 metricas:
- odd mandante entre 1.50 e 2.30
- mandante com pelo menos 3 vitorias nos ultimos 5 H2H
- mandante com pelo menos 5 vitorias nos ultimos 10 jogos em casa
- mandante pelo menos 7 posicoes acima do visitante na tabela
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo


HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_PATH = DATA_DIR / "scan_cache.json"
HISTORY_PATH = DATA_DIR / "validated_history.json"
BR_TZ = ZoneInfo("America/Sao_Paulo")

API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")

ODD_MIN = float(os.environ.get("PRELIVE_ODD_MIN", "1.50"))
ODD_MAX = float(os.environ.get("PRELIVE_ODD_MAX", "2.30"))
H2H_LIMIT = int(os.environ.get("PRELIVE_H2H_LIMIT", "5"))
H2H_MIN_HOME_WINS = int(os.environ.get("PRELIVE_H2H_MIN_HOME_WINS", "3"))
HOME_FORM_LIMIT = int(os.environ.get("PRELIVE_HOME_FORM_LIMIT", "10"))
HOME_FORM_MIN_WINS = int(os.environ.get("PRELIVE_HOME_FORM_MIN_WINS", "5"))
MIN_RANK_GAP = int(os.environ.get("PRELIVE_MIN_RANK_GAP", "7"))
MIN_CHECKS_PASSED = int(os.environ.get("PRELIVE_MIN_CHECKS_PASSED", "3"))
MAX_CANDIDATES = int(os.environ.get("PRELIVE_MAX_CANDIDATES", "500"))
HOME_FORM_LOOKBACK = int(os.environ.get("PRELIVE_HOME_FORM_LOOKBACK", "50"))
AUTO_MIDNIGHT_SCAN = os.environ.get("PRELIVE_AUTO_MIDNIGHT_SCAN", "0") != "0"
AUTO_SCAN = os.environ.get("PRELIVE_AUTO_SCAN", "0") != "0"
AUTO_SCAN_DAYS_AHEAD = int(os.environ.get("PRELIVE_AUTO_SCAN_DAYS_AHEAD", "1"))
AUTO_SCAN_INTERVAL_MINUTES = max(5, int(os.environ.get("PRELIVE_AUTO_SCAN_INTERVAL_MINUTES", "30")))
AUTO_SCAN_INCLUDE_PLAYED = os.environ.get("PRELIVE_AUTO_SCAN_INCLUDE_PLAYED", "0") != "0"

state_lock = threading.Lock()
state = {
    "job_id": None,
    "date": None,
    "status": "idle",
    "message": "Pronto para escanear.",
    "started_at": None,
    "finished_at": None,
    "total_fixtures": 0,
    "prelive_fixtures": 0,
    "scan_fixtures": 0,
    "include_played": False,
    "odd_candidates": 0,
    "processed_candidates": 0,
    "qualified": [],
    "rejected": [],
    "errors": [],
}


def _empty_state(scan_date: str | None = None, include_played: bool = False) -> dict:
    payload = dict(state)
    payload.update({
        "job_id": None,
        "date": scan_date,
        "status": "idle",
        "message": "Nenhum scan salvo para esta data.",
        "started_at": None,
        "finished_at": None,
        "total_fixtures": 0,
        "prelive_fixtures": 0,
        "scan_fixtures": 0,
        "include_played": include_played,
        "odd_candidates": 0,
        "processed_candidates": 0,
        "qualified": [],
        "rejected": [],
        "errors": [],
    })
    return payload


def _now_br() -> datetime:
    return datetime.now(BR_TZ)


def _today_br() -> date:
    return _now_br().date()


def _fixture_br_datetime(fixture_or_fx: dict) -> datetime | None:
    fx = fixture_or_fx.get("fixture") or fixture_or_fx
    ts = fx.get("timestamp")
    if ts:
        try:
            return datetime.fromtimestamp(int(ts), tz=BR_TZ)
        except Exception:
            pass

    raw = fx.get("date") or ""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(BR_TZ)
    except Exception:
        return None


def _fixture_br_parts(fixture_or_fx: dict) -> tuple[str, str]:
    dt = _fixture_br_datetime(fixture_or_fx)
    if not dt:
        fx = fixture_or_fx.get("fixture") or fixture_or_fx
        raw = fx.get("date") or ""
        return raw[:10], raw[11:16]
    return dt.date().isoformat(), dt.strftime("%H:%M")


def _json_load(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _json_save(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _cache_key(scan_date: str, include_played: bool) -> str:
    mode = "historico" if include_played else "prelive"
    return f"{scan_date}:{mode}"


def _load_cache_index() -> dict:
    cached = _json_load(CACHE_PATH, {})
    if not isinstance(cached, dict):
        return {"active_key": None, "scans": {}}
    if "scans" in cached and isinstance(cached["scans"], dict):
        return {"active_key": cached.get("active_key"), "scans": cached["scans"]}
    if cached.get("date") and "qualified" in cached:
        key = _cache_key(_iso_date(cached.get("date")), bool(cached.get("include_played")))
        return {"active_key": key, "scans": {key: cached}}
    return {"active_key": None, "scans": {}}


def _save_cached_snapshot(snapshot: dict) -> None:
    scan_date = _iso_date(snapshot.get("date"))
    include_played = bool(snapshot.get("include_played"))
    key = _cache_key(scan_date, include_played)
    cache = _load_cache_index()
    cache["scans"][key] = snapshot
    cache["active_key"] = key
    _json_save(CACHE_PATH, cache)


def _entry_key(item: dict) -> str:
    fixture_id = item.get("fixture_id")
    if fixture_id:
        return str(fixture_id)
    home = (item.get("home") or {}).get("name") or ""
    away = (item.get("away") or {}).get("name") or ""
    return f"{item.get('date')}:{home}:{away}"


def _load_history() -> dict:
    payload = _json_load(HISTORY_PATH, {"updated_at": None, "entries": {}})
    if not isinstance(payload, dict):
        return {"updated_at": None, "entries": {}}
    entries = payload.get("entries")
    if isinstance(entries, list):
        entries = {_entry_key(item): item for item in entries if isinstance(item, dict)}
    if not isinstance(entries, dict):
        entries = {}
    return {"updated_at": payload.get("updated_at"), "entries": entries}


def _save_history(payload: dict) -> None:
    payload["updated_at"] = _now_br().isoformat(timespec="seconds")
    _json_save(HISTORY_PATH, payload)


def _merge_history_entries(items: list[dict], source_key: str | None = None) -> int:
    if not items:
        return 0
    history = _load_history()
    entries = history["entries"]
    changed = 0
    now = _now_br().isoformat(timespec="seconds")
    for item in items:
        if not isinstance(item, dict) or not item.get("qualified"):
            continue
        key = _entry_key(item)
        existing = entries.get(key, {})
        merged = dict(item)
        merged["history_key"] = key
        merged["first_validated_at"] = existing.get("first_validated_at") or now
        merged["last_seen_at"] = now
        merged["source_scans"] = sorted(set((existing.get("source_scans") or []) + ([source_key] if source_key else [])))
        entries[key] = merged
        changed += 1
    if changed:
        _save_history(history)
    return changed


def _seed_history_from_cache() -> None:
    cache = _load_cache_index()
    for key, snapshot in (cache.get("scans") or {}).items():
        if isinstance(snapshot, dict):
            _merge_history_entries(snapshot.get("qualified") or [], key)


def _history_summary(entries: list[dict]) -> dict:
    stats = {"total": len(entries), "green": 0, "red": 0, "pending": 0}
    for item in entries:
        result = ((item.get("entry_result") or {}).get("status") or "pending").lower()
        if result in stats:
            stats[result] += 1
        else:
            stats["pending"] += 1
    return stats


def _history_response() -> dict:
    history = _load_history()
    entries = list(history["entries"].values())
    entries.sort(key=lambda x: (x.get("timestamp") or 0, x.get("home_odd", {}).get("odd") or 99), reverse=True)
    return {
        "updated_at": history.get("updated_at"),
        "stats": _history_summary(entries),
        "entries": entries,
    }


def _get_cached_snapshot(scan_date: str, include_played: bool) -> dict | None:
    cache = _load_cache_index()
    snapshot = cache.get("scans", {}).get(_cache_key(scan_date, include_played))
    return snapshot if isinstance(snapshot, dict) else None


def _af_get(endpoint: str, params: dict | None = None, timeout: int = 20) -> dict:
    if not API_FOOTBALL_KEY:
        return {"response": [], "errors": {"config": "API_FOOTBALL_KEY ausente"}}
    query = urllib.parse.urlencode(params or {})
    url = API_FOOTBALL_BASE + endpoint + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"x-apisports-key": API_FOOTBALL_KEY})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"response": [], "errors": {"exception": str(exc)}}


def _af_get_pages(endpoint: str, params: dict | None = None, timeout: int = 25) -> dict:
    base_params = dict(params or {})
    params = dict(base_params)
    merged = []
    last = None
    page = 1
    while True:
        params = dict(base_params)
        if page > 1:
            params["page"] = page
        data = _af_get(endpoint, params, timeout=timeout)
        if page == 1 and isinstance(data.get("errors"), dict) and data["errors"].get("page"):
            data = _af_get(endpoint, base_params, timeout=timeout)
        if data.get("errors"):
            return data
        merged.extend(data.get("response") or [])
        paging = data.get("paging") or {}
        current = int(paging.get("current") or page)
        total = int(paging.get("total") or current)
        last = data
        if current >= total:
            break
        if page == 1 and total > 1:
            probe = _af_get(endpoint, {**base_params, "page": 2}, timeout=timeout)
            if isinstance(probe.get("errors"), dict) and probe["errors"].get("page"):
                break
            if probe.get("errors"):
                return probe
            merged.extend(probe.get("response") or [])
            last = probe
            paging = probe.get("paging") or {}
            current = int(paging.get("current") or 2)
            total = int(paging.get("total") or current)
            page = current
            if current >= total:
                break
        page += 1
        if page > 100:
            break
    if last is None:
        last = {}
    last["response"] = merged
    return last


def _iso_date(value: str | None) -> str:
    if not value:
        return _today_br().isoformat()
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return _today_br().isoformat()


def _tomorrow() -> str:
    return (_today_br() + timedelta(days=1)).isoformat()


def _status_short(fixture: dict) -> str:
    return ((fixture.get("fixture") or {}).get("status") or {}).get("short") or ""


def _is_prelive(fixture: dict) -> bool:
    return _status_short(fixture) in {"NS", "TBD"}


def _score_for_fixture(fixture: dict) -> str:
    goals = fixture.get("goals") or {}
    gh = goals.get("home")
    ga = goals.get("away")
    return "-" if gh is None or ga is None else f"{gh}x{ga}"


def _entry_result(fixture: dict) -> dict:
    status = _status_short(fixture)
    goals = fixture.get("goals") or {}
    gh = goals.get("home")
    ga = goals.get("away")
    final_statuses = {"FT", "AET", "PEN"}
    if status not in final_statuses or gh is None or ga is None:
        return {"status": "pending", "label": "Pendente", "class": "pending"}
    if gh > ga:
        return {"status": "green", "label": "GREEN", "class": "green"}
    return {"status": "red", "label": "RED", "class": "red"}


def _update_item_result_from_fixture(item: dict, fixture: dict) -> dict:
    fx = fixture.get("fixture") or {}
    br_date, br_time = _fixture_br_parts(fx)
    item["status"] = _status_short(fixture)
    item["score"] = _score_for_fixture(fixture)
    item["entry_result"] = _entry_result(fixture)
    item["timestamp"] = fx.get("timestamp") or item.get("timestamp")
    if fx.get("date"):
        item["date"] = br_date
        item["time"] = br_time
    item["last_result_check_at"] = _now_br().isoformat(timespec="seconds")
    return item


def refresh_history_results() -> dict:
    history = _load_history()
    entries = history["entries"]
    targets = [
        (key, item)
        for key, item in entries.items()
        if item.get("fixture_id") and (item.get("entry_result") or {}).get("status") != "green" and (item.get("entry_result") or {}).get("status") != "red"
    ]
    if not targets:
        return _history_response()

    def fetch_one(key_item):
        key, item = key_item
        data = _af_get("/fixtures", {"id": item["fixture_id"]})
        fixture = (data.get("response") or [None])[0]
        return key, fixture

    with ThreadPoolExecutor(max_workers=6) as ex:
        for key, fixture in ex.map(fetch_one, targets):
            if fixture:
                entries[key] = _update_item_result_from_fixture(entries[key], fixture)
    _save_history(history)
    return _history_response()


def _fixture_id(fixture: dict) -> int | None:
    return (fixture.get("fixture") or {}).get("id")


def _value_float(value) -> float | None:
    try:
        number = float(str(value).replace(",", "."))
    except Exception:
        return None
    return number if number > 1 else None


def _extract_home_odd(odds_item: dict) -> dict | None:
    for bookmaker in odds_item.get("bookmakers") or []:
        for bet in bookmaker.get("bets") or []:
            bet_name = (bet.get("name") or "").strip().lower()
            if bet_name not in {"match winner", "winner", "1x2"}:
                continue
            for value in bet.get("values") or []:
                label = (value.get("value") or "").strip().lower()
                if label in {"home", "1"}:
                    odd = _value_float(value.get("odd"))
                    if odd:
                        return {
                            "odd": odd,
                            "bookmaker": bookmaker.get("name") or "",
                            "market": bet.get("name") or "Match Winner",
                        }
    return None


def _odds_by_fixture(scan_date: str) -> dict[int, dict]:
    data = _af_get_pages("/odds", {"date": scan_date, "bet": 1})
    by_id = {}
    for item in data.get("response") or []:
        fid = ((item.get("fixture") or {}).get("id"))
        odd = _extract_home_odd(item)
        if fid and odd:
            by_id[fid] = odd
    return by_id


def _match_result_for_team(fixture: dict, team_id: int) -> str | None:
    teams = fixture.get("teams") or {}
    goals = fixture.get("goals") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    gh = goals.get("home")
    ga = goals.get("away")
    if gh is None or ga is None:
        return None
    is_home = home.get("id") == team_id
    gf = gh if is_home else ga
    gc = ga if is_home else gh
    if gf > gc:
        return "W"
    if gf < gc:
        return "L"
    return "D"


def _normalize_match(fixture: dict, team_id: int) -> dict | None:
    result = _match_result_for_team(fixture, team_id)
    if not result:
        return None
    fx = fixture.get("fixture") or {}
    br_date, _br_time = _fixture_br_parts(fx)
    teams = fixture.get("teams") or {}
    goals = fixture.get("goals") or {}
    return {
        "date": br_date,
        "timestamp": fx.get("timestamp"),
        "home": (teams.get("home") or {}).get("name") or "",
        "away": (teams.get("away") or {}).get("name") or "",
        "score": f"{goals.get('home')}x{goals.get('away')}",
        "result": result,
    }


def _home_form(team_id: int) -> tuple[list[dict], int]:
    data = _af_get("/fixtures", {"team": team_id, "last": HOME_FORM_LOOKBACK})
    home_fixtures = [
        f for f in data.get("response") or []
        if (((f.get("teams") or {}).get("home") or {}).get("id") == team_id)
    ]
    matches = [m for m in (_normalize_match(f, team_id) for f in home_fixtures) if m][:HOME_FORM_LIMIT]
    return matches, sum(1 for m in matches if m["result"] == "W")


def _h2h(home_id: int, away_id: int) -> tuple[list[dict], int]:
    data = _af_get("/fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": H2H_LIMIT})
    matches = [m for m in (_normalize_match(f, home_id) for f in data.get("response") or []) if m]
    return matches, sum(1 for m in matches if m["result"] == "W")


def _standings(league_id: int, season: int, home_id: int, away_id: int) -> tuple[dict | None, dict | None]:
    data = _af_get("/standings", {"league": league_id, "season": season})
    for response in data.get("response") or []:
        standings = ((response.get("league") or {}).get("standings") or [])
        for group in standings:
            home_row = None
            away_row = None
            for row in group:
                team = row.get("team") or {}
                if team.get("id") == home_id:
                    home_row = row
                if team.get("id") == away_id:
                    away_row = row
            if home_row and away_row:
                return _standing_row(home_row), _standing_row(away_row)
    return None, None


def _standing_row(row: dict) -> dict:
    all_stats = row.get("all") or {}
    return {
        "rank": row.get("rank"),
        "points": row.get("points"),
        "form": row.get("form") or "",
        "played": all_stats.get("played"),
        "wins": all_stats.get("win"),
        "draws": all_stats.get("draw"),
        "losses": all_stats.get("lose"),
        "goals_diff": row.get("goalsDiff"),
    }


def _base_candidate(fixture: dict, home_odd: dict) -> dict:
    fx = fixture.get("fixture") or {}
    br_date, br_time = _fixture_br_parts(fx)
    league = fixture.get("league") or {}
    teams = fixture.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    return {
        "fixture_id": fx.get("id"),
        "date": br_date,
        "time": br_time,
        "timestamp": fx.get("timestamp"),
        "status": _status_short(fixture),
        "league": {
            "id": league.get("id"),
            "name": league.get("name") or "",
            "country": league.get("country") or "",
            "season": league.get("season"),
            "logo": league.get("logo") or "",
        },
        "home": {"id": home.get("id"), "name": home.get("name") or "", "logo": home.get("logo") or ""},
        "away": {"id": away.get("id"), "name": away.get("name") or "", "logo": away.get("logo") or ""},
        "score": _score_for_fixture(fixture),
        "entry_result": _entry_result(fixture),
        "home_odd": home_odd,
        "checks": {},
    }


def _evaluate_candidate(fixture: dict, odd: dict) -> dict:
    item = _base_candidate(fixture, odd)
    home_id = item["home"]["id"]
    away_id = item["away"]["id"]
    league_id = item["league"]["id"]
    season = item["league"]["season"]

    odd_ok = ODD_MIN <= odd["odd"] <= ODD_MAX
    item["checks"]["odd"] = {"ok": odd_ok, "value": odd["odd"], "target": f"{ODD_MIN:.2f}-{ODD_MAX:.2f}"}
    if not odd_ok:
        item["qualified"] = False
        return item

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_h2h = ex.submit(_h2h, home_id, away_id)
        f_form = ex.submit(_home_form, home_id)
        f_table = ex.submit(_standings, league_id, season, home_id, away_id)
        h2h_matches, h2h_wins = f_h2h.result()
        home_matches, home_wins = f_form.result()
        home_standing, away_standing = f_table.result()

    rank_gap = None
    if home_standing and away_standing and home_standing.get("rank") and away_standing.get("rank"):
        rank_gap = int(away_standing["rank"]) - int(home_standing["rank"])

    item["h2h"] = h2h_matches
    item["home_form"] = home_matches
    item["standing"] = {"home": home_standing, "away": away_standing, "rank_gap": rank_gap}
    item["checks"]["h2h"] = {"ok": h2h_wins >= H2H_MIN_HOME_WINS, "value": h2h_wins, "target": f">= {H2H_MIN_HOME_WINS}/{H2H_LIMIT}"}
    item["checks"]["home_form"] = {"ok": home_wins >= HOME_FORM_MIN_WINS, "value": home_wins, "target": f">= {HOME_FORM_MIN_WINS}/{HOME_FORM_LIMIT}"}
    item["checks"]["standing"] = {"ok": rank_gap is not None and rank_gap >= MIN_RANK_GAP, "value": rank_gap, "target": f">= {MIN_RANK_GAP} posicoes"}
    checks_passed = sum(1 for check in item["checks"].values() if check["ok"])
    item["checks_passed"] = checks_passed
    item["checks_total"] = len(item["checks"])
    item["qualified"] = checks_passed >= MIN_CHECKS_PASSED
    return item


def run_scan(scan_date: str, include_played: bool = False) -> None:
    scan_date = _iso_date(scan_date)
    job_id = str(uuid.uuid4())[:8]
    with state_lock:
        state.update({
            "job_id": job_id,
            "date": scan_date,
            "status": "running",
            "message": "Buscando jogos e odds do dia...",
            "started_at": _now_br().isoformat(timespec="seconds"),
            "finished_at": None,
            "total_fixtures": 0,
            "prelive_fixtures": 0,
            "scan_fixtures": 0,
            "include_played": include_played,
            "odd_candidates": 0,
            "processed_candidates": 0,
            "qualified": [],
            "rejected": [],
            "errors": [],
        })

    try:
        fixtures_data = _af_get_pages("/fixtures", {"date": scan_date})
        scan_errors = []
        if fixtures_data.get("errors"):
            scan_errors.append({"fixtures": fixtures_data.get("errors")})
        fixtures = fixtures_data.get("response") or []
        prelive = [f for f in fixtures if _is_prelive(f)]
        scan_fixtures = fixtures if include_played else prelive
        odds = _odds_by_fixture(scan_date)
        candidates = []
        for fixture in scan_fixtures:
            fid = _fixture_id(fixture)
            odd = odds.get(fid)
            if odd and ODD_MIN <= odd["odd"] <= ODD_MAX:
                candidates.append((fixture, odd))
        odd_candidate_count = len(candidates)
        candidates = candidates[:MAX_CANDIDATES]

        with state_lock:
            state.update({
                "message": f"Aprofundando {len(candidates)} candidatos pela odd...",
                "total_fixtures": len(fixtures),
                "prelive_fixtures": len(prelive),
                "scan_fixtures": len(scan_fixtures),
                "odd_candidates": odd_candidate_count,
                "processed_candidates": len(candidates),
                "errors": scan_errors,
            })

        qualified = []
        rejected = []
        errors = []
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = [ex.submit(_evaluate_candidate, fixture, odd) for fixture, odd in candidates]
            for future in as_completed(futures):
                try:
                    item = future.result()
                    if item.get("qualified"):
                        qualified.append(item)
                    else:
                        rejected.append(item)
                except Exception as exc:
                    errors.append(str(exc))

        qualified.sort(key=lambda x: (x.get("timestamp") or 0, x["home_odd"]["odd"]))
        rejected.sort(key=lambda x: (x.get("timestamp") or 0, x["home_odd"]["odd"]))

        with state_lock:
            state.update({
                "status": "ready",
                "message": f"Scan concluido: {len(qualified)} entradas encontradas.",
                "finished_at": _now_br().isoformat(timespec="seconds"),
                "qualified": qualified,
                "rejected": rejected[:120],
                "errors": errors,
            })
            snapshot = dict(state)
        _save_cached_snapshot(snapshot)
        _merge_history_entries(qualified, _cache_key(scan_date, include_played))
    except Exception as exc:
        with state_lock:
            state.update({
                "status": "error",
                "message": str(exc),
                "finished_at": _now_br().isoformat(timespec="seconds"),
                "errors": [str(exc)],
            })


def _midnight_loop() -> None:
    """Com o servidor ligado, roda uma varredura diaria logo depois da meia-noite."""
    last_run = ""
    while True:
        now = _now_br()
        if now.hour == 0 and now.minute == 0:
            key = now.date().isoformat()
            with state_lock:
                running = state.get("status") == "running"
            if key != last_run and not running:
                last_run = key
                run_scan(now.date().isoformat())
        time.sleep(30)


def _auto_scan_loop() -> None:
    """Atualiza automaticamente a data-alvo enquanto o servidor estiver online."""
    last_started = ""
    while True:
        target_date = (_today_br() + timedelta(days=AUTO_SCAN_DAYS_AHEAD)).isoformat()
        minute_bucket = int(time.time() // (AUTO_SCAN_INTERVAL_MINUTES * 60))
        key = f"{target_date}:{minute_bucket}:{int(AUTO_SCAN_INCLUDE_PLAYED)}"
        with state_lock:
            running = state.get("status") == "running"
        if key != last_started and not running:
            last_started = key
            run_scan(target_date, AUTO_SCAN_INCLUDE_PLAYED)
        time.sleep(30)


def _load_cached_state() -> None:
    cache = _load_cache_index()
    cached = cache.get("scans", {}).get(cache.get("active_key"))
    if not cached and cache.get("scans"):
        cached = next(reversed(cache["scans"].values()))
    if isinstance(cached, dict):
        with state_lock:
            state.update(cached)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if parsed.path == "/health":
            return self._json(200, {"ok": True, "status": state.get("status"), "date": state.get("date")})
        if parsed.path == "/api/state":
            query = urllib.parse.parse_qs(parsed.query)
            if "date" in query:
                scan_date = _iso_date(query.get("date", [""])[0])
                include_played = (query.get("include_played", ["0"])[0] == "1")
                with state_lock:
                    running_state = dict(state) if state.get("status") == "running" and state.get("date") == scan_date and bool(state.get("include_played")) == include_played else None
                if running_state:
                    return self._json(200, running_state)
                cached = _get_cached_snapshot(scan_date, include_played)
                return self._json(200, cached or _empty_state(scan_date, include_played))
            with state_lock:
                return self._json(200, dict(state))
        if parsed.path == "/api/default-date":
            return self._json(200, {"today": _today_br().isoformat(), "tomorrow": _tomorrow()})
        if parsed.path == "/api/history":
            return self._json(200, _history_response())
        path = HERE / parsed.path.lstrip("/")
        if path.exists() and path.is_file() and path.parent == HERE:
            ctype = "text/css; charset=utf-8" if path.suffix == ".css" else "text/plain; charset=utf-8"
            return self._send(200, path.read_bytes(), ctype)
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/history/refresh":
            return self._json(200, refresh_history_results())
        if parsed.path != "/api/scan":
            return self._json(404, {"error": "not_found"})
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            payload = {}
        scan_date = _iso_date(payload.get("date"))
        include_played = bool(payload.get("include_played"))
        force = bool(payload.get("force", True))
        if not force:
            cached = _get_cached_snapshot(scan_date, include_played)
            if cached:
                with state_lock:
                    state.update(cached)
                return self._json(200, {"ok": True, "cached": True, "state": cached})
        with state_lock:
            if state.get("status") == "running":
                return self._json(409, {"error": "scan_running", "state": dict(state)})
        threading.Thread(target=run_scan, args=(scan_date, include_played), daemon=True).start()
        return self._json(202, {"ok": True, "date": scan_date, "include_played": include_played})


def main() -> None:
    _load_cached_state()
    _seed_history_from_cache()
    port = int(os.environ.get("PORT", "8891"))
    if AUTO_MIDNIGHT_SCAN:
        threading.Thread(target=_midnight_loop, daemon=True).start()
    if AUTO_SCAN:
        threading.Thread(target=_auto_scan_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"WAVE Pre-Live Favorites em http://localhost:{port}")
    print(f"Data default de amanha: {_tomorrow()}")
    server.serve_forever()


if __name__ == "__main__":
    main()
