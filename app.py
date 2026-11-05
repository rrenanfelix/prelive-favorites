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
import urllib.error
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
STANDINGS_PATH = DATA_DIR / "standings_frozen.json"
STAR_LOCK_PATH = DATA_DIR / "star_locked.json"
AWAY_CACHE_PATH = DATA_DIR / "away_cache.json"
TG_CONFIG_PATH = Path(os.environ.get("PRELIVE_TG_CONFIG", HERE / "telegram_config.json"))
TG_SENT_PATH = DATA_DIR / "telegram_sent.json"
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
# Envio pulverizado (entrada por entrada, em tempo real durante o scan). Desligado
# por padrao: agora as entradas 4/4 vao num digest consolidado as 00:30 (daily_bot.py).
REALTIME_TG = os.environ.get("PRELIVE_REALTIME_TG", "0") != "0"
AUTO_MIDNIGHT_SCAN = os.environ.get("PRELIVE_AUTO_MIDNIGHT_SCAN", "0") != "0"
AUTO_SCAN = os.environ.get("PRELIVE_AUTO_SCAN", "0") != "0"
AUTO_SCAN_DAYS_AHEAD = int(os.environ.get("PRELIVE_AUTO_SCAN_DAYS_AHEAD", "1"))
AUTO_SCAN_INTERVAL_MINUTES = max(5, int(os.environ.get("PRELIVE_AUTO_SCAN_INTERVAL_MINUTES", "30")))
AUTO_SCAN_INCLUDE_PLAYED = os.environ.get("PRELIVE_AUTO_SCAN_INCLUDE_PLAYED", "0") != "0"
RESULTS_REFRESH_MINUTES = max(2, int(os.environ.get("PRELIVE_RESULTS_REFRESH_MINUTES", "10")))
# Protecoes de quota (API-Football free = 100 req/dia).
RESULTS_MAX_AGE_DAYS = int(os.environ.get("PRELIVE_RESULTS_MAX_AGE_DAYS", "3"))
RESULTS_STALE_HOURS = int(os.environ.get("PRELIVE_RESULTS_STALE_HOURS", "18"))
RESULTS_MAX_PER_CYCLE = int(os.environ.get("PRELIVE_RESULTS_MAX_PER_CYCLE", "80"))
RESULTS_BATCH_SIZE = min(20, max(1, int(os.environ.get("PRELIVE_RESULTS_BATCH_SIZE", "20"))))

state_lock = threading.Lock()
standings_lock = threading.Lock()
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


def _br_date_from_ts(ts) -> str | None:
    """Data BR (YYYY-MM-DD) do horario de inicio a partir do timestamp unix."""
    try:
        return datetime.fromtimestamp(int(ts), BR_TZ).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _collect_games_by_br_day(include_rejected: bool = False) -> dict[str, list[dict]]:
    """Junta todos os snapshots + historico, dedup por fixture_id e agrupa pela
    data REAL no horario do Brasil (derivada do timestamp de inicio do jogo).

    Resolve a virada de dia: um jogo perto da meia-noite cai no dia BR correto,
    nao no dia do parametro do scan. Nao consome quota (le so o cache local).
    """
    best: dict[int, dict] = {}

    def rank(item: dict) -> int:
        status = (item.get("entry_result") or {}).get("status")
        if status in {"green", "red"}:
            return 3
        if item.get("status") in {"1H", "HT", "2H", "ET", "BT", "P", "LIVE"}:
            return 2
        return 1

    def consider(item: dict) -> None:
        fixture_id = item.get("fixture_id")
        if not fixture_id:
            return
        prev = best.get(int(fixture_id))
        if prev is None or rank(item) >= rank(prev):
            best[int(fixture_id)] = item

    cache = _load_cache_index()
    for snap in cache.get("scans", {}).values():
        if not isinstance(snap, dict):
            continue
        lists = ["qualified"] + (["rejected"] if include_rejected else [])
        for list_name in lists:
            for item in snap.get(list_name) or []:
                consider(item)
    # historico validado tambem entra
    for item in _load_history()["entries"].values():
        consider(item)

    buckets: dict[str, list[dict]] = {}
    for item in best.values():
        day = _br_date_from_ts(item.get("timestamp")) or _iso_date(item.get("date"))
        buckets.setdefault(day, []).append(item)
    for day in buckets:
        buckets[day].sort(key=lambda x: x.get("timestamp") or 0)
    return buckets


def _day_summary(items: list[dict]) -> dict:
    def count(status):
        return sum(1 for it in items if (it.get("entry_result") or {}).get("status") == status)
    return {
        "total": len(items),
        "green": count("green"),
        "red": count("red"),
        "pending": len(items) - count("green") - count("red"),
    }


def _pnl(items: list[dict], stake: float = 100.0) -> dict:
    """Lucro/prejuizo do back 'casa vence' apostando `stake` em cada entrada
    finalizada, usando a odd real gravada. GREEN paga (odd-1)*stake; RED perde stake."""
    green = red = 0
    profit = 0.0
    for it in items:
        status = (it.get("entry_result") or {}).get("status")
        try:
            odd = float((it.get("home_odd") or {}).get("odd"))
        except (TypeError, ValueError):
            odd = None
        if status == "green" and odd:
            profit += (odd - 1) * stake
            green += 1
        elif status == "red":
            profit -= stake
            red += 1
    finished = green + red
    staked = finished * stake
    return {
        "stake": stake,
        "finished": finished,
        "green": green,
        "red": red,
        "hit": round(green / finished * 100, 1) if finished else 0.0,
        "staked": round(staked, 2),
        "profit": round(profit, 2),
        "roi": round(profit / staked * 100, 1) if staked else 0.0,
    }


def _is_rate_limited(data: dict) -> bool:
    errs = data.get("errors")
    if isinstance(errs, dict):
        for k, v in errs.items():
            if "rate" in str(k).lower() or "rate" in str(v).lower() or "requests" in str(k).lower():
                return True
    return False


def _af_get(endpoint: str, params: dict | None = None, timeout: int = 20) -> dict:
    if not API_FOOTBALL_KEY:
        return {"response": [], "errors": {"config": "API_FOOTBALL_KEY ausente"}}
    query = urllib.parse.urlencode(params or {})
    url = API_FOOTBALL_BASE + endpoint + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"x-apisports-key": API_FOOTBALL_KEY})
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            # API-Sports pode devolver HTTP 200 com erro de rate limit no corpo.
            if _is_rate_limited(data) and attempt < max_attempts - 1:
                time.sleep(min(2 ** attempt, 8) + 0.5)
                continue
            return data
        except urllib.error.HTTPError as exc:
            # 429 = rate limit por minuto; espera e tenta de novo.
            if exc.code == 429 and attempt < max_attempts - 1:
                time.sleep(min(2 ** attempt, 8) + 0.5)
                continue
            return {"response": [], "errors": {"http": f"{exc.code} {exc.reason}"}}
        except Exception as exc:
            if attempt < max_attempts - 1:
                time.sleep(min(2 ** attempt, 4))
                continue
            return {"response": [], "errors": {"exception": str(exc)}}
    return {"response": [], "errors": {"exception": "max_attempts"}}


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

    # agrupa keys por fixture_id e busca em lote (ids) pra economizar quota
    by_fixture: dict[int, list[str]] = {}
    for key, item in targets:
        by_fixture.setdefault(int(item["fixture_id"]), []).append(key)
    fixture_ids = list(by_fixture.keys())
    batches = [fixture_ids[i:i + RESULTS_BATCH_SIZE] for i in range(0, len(fixture_ids), RESULTS_BATCH_SIZE)]

    def fetch_batch(chunk: list[int]):
        ids = "-".join(str(fid) for fid in chunk)
        data = _af_get("/fixtures", {"ids": ids})
        return data.get("response") or []

    with ThreadPoolExecutor(max_workers=4) as ex:
        for fixtures in ex.map(fetch_batch, batches):
            for fixture in fixtures:
                fid = (fixture.get("fixture") or {}).get("id")
                if fid is None:
                    continue
                for key in by_fixture.get(int(fid), []):
                    entries[key] = _update_item_result_from_fixture(entries[key], fixture)
    _save_history(history)
    return _history_response()


def refresh_snapshot_results() -> int:
    """Busca o placar final dos jogos ja iniciados e atualiza os snapshots em cache.

    Assim as abas Entradas/Rejeitados passam a exibir o resultado depois que os
    jogos terminam, sem precisar rodar um novo scan. Com protecoes de quota:
    apenas snapshots recentes, ignora jogos antigos ainda pendentes (postergados),
    busca em lote (ids) e limita a quantidade por ciclo.
    """
    cache = _load_cache_index()
    scans = cache.get("scans", {})
    if not scans:
        return 0
    now_ts = int(time.time())
    min_scan_date = (_today_br() - timedelta(days=RESULTS_MAX_AGE_DAYS)).isoformat()
    stale_before_ts = now_ts - RESULTS_STALE_HOURS * 3600
    pending: dict[int, list[dict]] = {}
    for key, snap in scans.items():
        if not isinstance(snap, dict):
            continue
        # so snapshots recentes (data no inicio da chave "YYYY-MM-DD:modo")
        if str(key)[:10] < min_scan_date:
            continue
        for list_name in ("qualified", "rejected"):
            for item in snap.get(list_name) or []:
                fixture_id = item.get("fixture_id")
                if not fixture_id:
                    continue
                res_status = (item.get("entry_result") or {}).get("status")
                if res_status in {"green", "red"}:
                    continue
                ts = item.get("timestamp") or 0
                # jogo ainda nao comecou (folga de 1 min): pula
                if ts and ts > now_ts + 60:
                    continue
                # jogo muito antigo e ainda sem resultado final: provavelmente
                # adiado/cancelado -> para de consultar pra nao gastar quota
                if ts and ts < stale_before_ts:
                    continue
                pending.setdefault(int(fixture_id), []).append(item)
    if not pending:
        return 0

    fixture_ids = list(pending.keys())[:RESULTS_MAX_PER_CYCLE]

    def fetch_batch(chunk: list[int]):
        ids = "-".join(str(fid) for fid in chunk)
        data = _af_get("/fixtures", {"ids": ids})
        return data.get("response") or []

    batches = [fixture_ids[i:i + RESULTS_BATCH_SIZE] for i in range(0, len(fixture_ids), RESULTS_BATCH_SIZE)]
    updated = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for fixtures in ex.map(fetch_batch, batches):
            for fixture in fixtures:
                fid = ((fixture.get("fixture") or {}).get("id"))
                if fid is None:
                    continue
                for item in pending.get(int(fid), []):
                    _update_item_result_from_fixture(item, fixture)
                    updated += 1
    if updated:
        _json_save(CACHE_PATH, cache)
    return updated


def _results_loop() -> None:
    """Atualiza periodicamente o placar dos jogos ja iniciados nos snapshots."""
    while True:
        time.sleep(RESULTS_REFRESH_MINUTES * 60)
        try:
            with state_lock:
                running = state.get("status") == "running"
            if running:
                continue
            updated = refresh_snapshot_results()
            if updated:
                _seed_history_from_cache()
            try:
                refresh_away_results()
            except Exception:
                pass
            # Resultado pulverizado (um green/red por entrada) so no modo realtime.
            # No modo padrao o fechamento vai no relatorio das 23:59 (daily_bot.py).
            if REALTIME_TG:
                try:
                    _notify_results_for_sent_entries()
                except Exception as exc:
                    print(f"[telegram] hook de resultado falhou: {exc}")
        except Exception:
            pass


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
    # timezone garante que scan_date bata com o dia BR (senao a API considera UTC
    # e jogos noturnos BR caem no dia seguinte).
    data = _af_get_pages("/odds", {"date": scan_date, "bet": 1, "timezone": "America/Sao_Paulo"})
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


def _frozen_standings_table(league_id: int, season: int, scan_date: str) -> dict:
    """Tabela da liga CONGELADA por data ({team_id: standing_row}).

    Na primeira vez que a liga aparece num scan_date, busca a classificacao e a
    grava. Scans seguintes do mesmo dia reusam a versao congelada -> os checks de
    tabela nao mudam ao longo do dia (fim do drift "aparece e some")."""
    key = f"{league_id}:{season}:{scan_date}"
    with standings_lock:
        cache = _json_load(STANDINGS_PATH, {})
        if key in cache:
            return cache[key].get("teams") or {}
    # ainda nao congelado: busca da API (fora do lock)
    data = _af_get("/standings", {"league": league_id, "season": season})
    teams: dict = {}
    for response in data.get("response") or []:
        for group in ((response.get("league") or {}).get("standings") or []):
            for row in group:
                tid = (row.get("team") or {}).get("id")
                if tid is not None:
                    teams[str(tid)] = _standing_row(row)
    # so congela se veio tabela de verdade (evita fixar vazio por erro/rate limit)
    if teams:
        with standings_lock:
            cache = _json_load(STANDINGS_PATH, {})
            if key not in cache:
                cache[key] = {"frozen_at": _now_br().isoformat(timespec="seconds"), "teams": teams}
                _json_save(STANDINGS_PATH, cache)
    return teams


def _standings(league_id: int, season: int, home_id: int, away_id: int, scan_date: str | None = None) -> tuple[dict | None, dict | None]:
    if scan_date:
        teams = _frozen_standings_table(league_id, season, scan_date)
        return teams.get(str(home_id)), teams.get(str(away_id))
    # fallback (sem data): comportamento antigo, tabela ao vivo
    data = _af_get("/standings", {"league": league_id, "season": season})
    for response in data.get("response") or []:
        for group in ((response.get("league") or {}).get("standings") or []):
            home_row = away_row = None
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


def _load_tg_config() -> dict | None:
    """Carrega config do bot Telegram (bot_token + chat_id). Retorna None se ausente."""
    try:
        with open(TG_CONFIG_PATH) as f:
            cfg = json.load(f)
        if cfg.get("bot_token") and cfg.get("chat_id"):
            return cfg
    except FileNotFoundError:
        return None
    except Exception as exc:
        print(f"[telegram] falha ao ler config: {exc}")
    return None


def _load_tg_state() -> dict:
    """State do bot: quem ja recebeu notificacao de entrada e de resultado.

    Formato:
        {
          "entries_sent": {"<fixture_id>": <message_id or null>, ...},
          "results_sent": ["<fixture_id>", ...]
        }
    Aceita tambem o formato antigo {"sent": [ids]} pra retrocompatibilidade.
    """
    try:
        with open(TG_SENT_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"entries_sent": {}, "results_sent": []}
    except Exception:
        return {"entries_sent": {}, "results_sent": []}
    entries = data.get("entries_sent")
    if entries is None:
        # Migra formato antigo (lista de ids sem message_id).
        legacy = data.get("sent") or []
        entries = {str(fid): None for fid in legacy}
    if not isinstance(entries, dict):
        entries = {}
    results = data.get("results_sent") or []
    if not isinstance(results, list):
        results = []
    return {"entries_sent": entries, "results_sent": list(results)}


def _save_tg_state(state: dict) -> None:
    # Mantem no maximo 2000 keys pra nao crescer infinito.
    entries = state.get("entries_sent") or {}
    results = state.get("results_sent") or []
    if len(entries) > 2000:
        entries = dict(list(entries.items())[-2000:])
    if len(results) > 2000:
        results = results[-2000:]
    try:
        TG_SENT_PATH.write_text(json.dumps({
            "entries_sent": entries,
            "results_sent": results,
        }, ensure_ascii=False))
    except Exception as exc:
        print(f"[telegram] falha ao salvar dedup: {exc}")


def _tg_send(cfg: dict, text: str, reply_to: int | None = None) -> int | None:
    """Envia mensagem HTML e retorna o message_id (ou None em caso de falha)."""
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
    fields = {
        "chat_id": cfg["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    # Supergrupos com Topicos exigem message_thread_id pra postar no topico correto.
    thread_id = cfg.get("message_thread_id")
    if thread_id not in (None, "", 0):
        fields["message_thread_id"] = str(thread_id)
    if reply_to:
        fields["reply_to_message_id"] = str(reply_to)
        fields["allow_sending_without_reply"] = "true"
    payload = urllib.parse.urlencode(fields).encode()
    try:
        with urllib.request.urlopen(url, data=payload, timeout=10) as r:
            body = json.loads(r.read().decode())
        if not body.get("ok"):
            print(f"[telegram] resposta nao-ok: {body}")
            return None
        return int(((body.get("result") or {}).get("message_id")) or 0) or None
    except Exception as exc:
        print(f"[telegram] envio falhou: {exc}")
        return None


def _tg_format_entry(item: dict) -> str:
    # Formato minimalista pedido pelo Renan: partida, dia/hora BR, entrada e odd (sem stats).
    home = item.get("home") or {}
    away = item.get("away") or {}
    odd = (item.get("home_odd") or {}).get("odd")
    date_iso = item.get("date") or ""  # YYYY-MM-DD
    time_txt = item.get("time") or ""  # HH:MM (ja em BR)
    # Converte pra DD/MM pra ficar mais familiar.
    date_br = date_iso
    if len(date_iso) == 10 and date_iso.count("-") == 2:
        y, m, d = date_iso.split("-")
        date_br = f"{d}/{m}"
    when = f"{date_br} {time_txt}".strip()
    lines = [
        f"⚽ <b>{home.get('name', '')}</b> x {away.get('name', '')}",
        f"🗓 {when} (BR)" if when else "",
        "🎯 Entrada: Back mandante",
        f"💰 Odd: <b>{odd}</b>" if odd else "",
    ]
    return "\n".join(l for l in lines if l)


def _tg_sent_ids() -> set:
    """IDs de fixtures que ja foram enviados ao Telegram (todos eram 4/4 no envio)."""
    try:
        return set(str(k) for k in (_load_tg_state().get("entries_sent") or {}).keys())
    except Exception:
        return set()


def _load_star_locks() -> set:
    """Fixtures travados como 4/4 (⭐): uma vez 4/4, sempre 4/4.

    Uniao de: jogos ja enviados ao Telegram + jogos que ja bateram 4/4 em algum
    scan. O conjunto so cresce; um jogo nunca perde a estrela por drift."""
    ids = _tg_sent_ids()
    try:
        data = _json_load(STAR_LOCK_PATH, {})
        for k in (data.get("fixture_ids") or []):
            ids.add(str(k))
    except Exception:
        pass
    return ids


def _save_star_locks(ids: set) -> None:
    with standings_lock:
        data = _json_load(STAR_LOCK_PATH, {})
        cur = set(str(k) for k in (data.get("fixture_ids") or []))
        cur |= set(str(k) for k in ids)
        _json_save(STAR_LOCK_PATH, {"fixture_ids": sorted(cur)})


def _is_star_now(item: dict) -> bool:
    checks = item.get("checks") or {}
    total = item.get("checks_total") or len(checks) or 4
    return total > 0 and (item.get("checks_passed") or 0) >= total


def _force_star(item: dict, locked_ids: set | None = None) -> dict:
    """Se o jogo ja foi 4/4 alguma vez (ou enviado ao Telegram), trava em 4/4 (⭐)
    mesmo que a tabela tenha driftado depois."""
    if locked_ids is None:
        locked_ids = _load_star_locks()
    fid = item.get("fixture_id")
    if fid is None or str(fid) not in locked_ids:
        return item
    checks = item.get("checks") or {}
    total = item.get("checks_total") or len(checks) or 4
    st = checks.get("standing")
    if isinstance(st, dict) and not st.get("ok"):
        st["ok"] = True  # estava ok quando marcado
    item["checks_total"] = total
    item["checks_passed"] = total
    item["qualified"] = True
    item["tg_star"] = True
    return item


# alias retrocompat
_force_tg_star = _force_star


def _notify_perfect_entries(qualified: list[dict]) -> None:
    """Manda pro Telegram apenas as entradas 4/4 ainda nao enviadas."""
    cfg = _load_tg_config()
    if not cfg:
        return
    only_perfect = cfg.get("notify_only_perfect", True)
    tg_state = _load_tg_state()
    entries_sent = tg_state["entries_sent"]
    delivered = 0
    for item in qualified:
        passed = item.get("checks_passed") or 0
        total = item.get("checks_total") or 0
        if only_perfect and (total == 0 or passed < total):
            continue
        fixture_id = item.get("fixture_id")
        if not fixture_id:
            continue
        key = str(fixture_id)
        if key in entries_sent:
            continue
        text = _tg_format_entry(item)
        msg_id = _tg_send(cfg, text)
        if msg_id is not None or msg_id == 0:
            # Registra mesmo com msg_id None (falha na leitura do id) pra nao repetir.
            entries_sent[key] = msg_id
            delivered += 1
    if delivered:
        _save_tg_state(tg_state)
        print(f"[telegram] {delivered} entrada(s) 4/4 enviada(s)")


def _tg_format_away_entry(item: dict) -> str:
    home = (item.get("home") or {}).get("name", "")
    away = (item.get("away") or {}).get("name", "")
    odd = (item.get("home_odd") or {}).get("odd")
    date_iso = item.get("date") or ""
    time_txt = item.get("time") or ""
    date_br = date_iso
    if len(date_iso) == 10 and date_iso.count("-") == 2:
        y, m, d = date_iso.split("-")
        date_br = f"{d}/{m}"
    when = f"{date_br} {time_txt}".strip()
    lines = [
        "🚌 <b>ENTRADA VISITANTE</b>",
        f"⚽ {home} x <b>{away}</b>",
        f"🗓 {when} (BR)" if when else "",
        f"🎯 Entrada: Back <b>{away}</b> (visitante)",
        f"💰 Odd: <b>{odd}</b>" if odd else "",
    ]
    return "\n".join(l for l in lines if l)


def _notify_perfect_away_entries(qualified: list[dict]) -> None:
    """Manda pro Telegram as entradas 4/4 de VISITANTE ainda nao enviadas.
    Usa chave 'away:<id>' no dedup pra nao colidir com a trava/dedup da casa."""
    cfg = _load_tg_config()
    if not cfg:
        return
    tg_state = _load_tg_state()
    entries_sent = tg_state["entries_sent"]
    delivered = 0
    for item in qualified:
        passed = item.get("checks_passed") or 0
        total = item.get("checks_total") or 0
        if total == 0 or passed < total:  # so 4/4
            continue
        fixture_id = item.get("fixture_id")
        if not fixture_id:
            continue
        key = f"away:{fixture_id}"
        if key in entries_sent:
            continue
        msg_id = _tg_send(cfg, _tg_format_away_entry(item))
        entries_sent[key] = msg_id
        delivered += 1
    if delivered:
        _save_tg_state(tg_state)
        print(f"[telegram] {delivered} entrada(s) 4/4 VISITANTE enviada(s)")


def _tg_format_result(item: dict) -> str:
    result = (item.get("entry_result") or {}).get("status")
    score = item.get("score") or "?"
    home = (item.get("home") or {}).get("name", "")
    away = (item.get("away") or {}).get("name", "")
    if result == "green":
        header = "🟢 <b>GREEN</b>"
    elif result == "red":
        header = "🔴 <b>RED</b>"
    else:
        header = "⚪ <b>Resultado</b>"
    return f"{header}\n⚽ {home} <b>{score}</b> {away}"


def _notify_results_for_sent_entries() -> None:
    """Varre o cache e manda o resultado (GREEN/RED) das entradas 4/4 ja notificadas."""
    cfg = _load_tg_config()
    if not cfg:
        return
    tg_state = _load_tg_state()
    entries_sent = tg_state["entries_sent"]
    if not entries_sent:
        return
    results_sent = set(tg_state["results_sent"])
    pending_keys = set(entries_sent) - results_sent
    if not pending_keys:
        return
    # Procura os itens no cache (mais recente vence se aparecer em multiplos scans).
    cache = _load_cache_index()
    scans = (cache.get("scans") or {}) if isinstance(cache, dict) else {}
    lookup: dict[str, dict] = {}
    # Ordena por chave (data no prefixo) — a ultima entra por ultimo -> vence.
    for _, snap in sorted(scans.items()) if isinstance(scans, dict) else []:
        if not isinstance(snap, dict):
            continue
        for list_name in ("qualified", "rejected"):
            for item in snap.get(list_name) or []:
                fid = item.get("fixture_id")
                if fid is None:
                    continue
                key = str(fid)
                if key in pending_keys:
                    lookup[key] = item
    delivered = 0
    for key in list(pending_keys):
        item = lookup.get(key)
        if not item:
            continue
        status = (item.get("entry_result") or {}).get("status")
        if status not in {"green", "red"}:
            continue
        reply_to = entries_sent.get(key)
        text = _tg_format_result(item)
        msg_id = _tg_send(cfg, text, reply_to=reply_to if isinstance(reply_to, int) else None)
        if msg_id is not None or msg_id == 0:
            results_sent.add(key)
            delivered += 1
    if delivered:
        tg_state["results_sent"] = sorted(results_sent)
        _save_tg_state(tg_state)
        print(f"[telegram] {delivered} resultado(s) GREEN/RED enviado(s)")


def _evaluate_candidate(fixture: dict, odd: dict, scan_date: str | None = None) -> dict:
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
        f_table = ex.submit(_standings, league_id, season, home_id, away_id, scan_date)
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


# ========================= SUBSISTEMA VISITANTES =========================
# Espelho da estrategia: back no VISITANTE quando ele bate as 4 metricas fora.
# Isolado do scan principal (nao mexe no fluxo de mando que ja funciona).

def _extract_away_odd(odds_item: dict) -> dict | None:
    for bookmaker in odds_item.get("bookmakers") or []:
        for bet in bookmaker.get("bets") or []:
            if (bet.get("name") or "").strip().lower() not in {"match winner", "winner", "1x2"}:
                continue
            for value in bet.get("values") or []:
                if (value.get("value") or "").strip().lower() in {"away", "2"}:
                    odd = _value_float(value.get("odd"))
                    if odd:
                        return {"odd": odd, "bookmaker": bookmaker.get("name") or "", "market": bet.get("name") or "Match Winner"}
    return None


def _away_odds_by_fixture(scan_date: str) -> dict[int, dict]:
    data = _af_get_pages("/odds", {"date": scan_date, "bet": 1, "timezone": "America/Sao_Paulo"})
    by_id = {}
    for item in data.get("response") or []:
        fid = ((item.get("fixture") or {}).get("id"))
        odd = _extract_away_odd(item)
        if fid and odd:
            by_id[fid] = odd
    return by_id


def _away_form(team_id: int) -> tuple[list[dict], int]:
    """Forma do time como VISITANTE (ultimos jogos jogando fora)."""
    data = _af_get("/fixtures", {"team": team_id, "last": HOME_FORM_LOOKBACK})
    away_fixtures = [
        f for f in data.get("response") or []
        if (((f.get("teams") or {}).get("away") or {}).get("id") == team_id)
    ]
    matches = [m for m in (_normalize_match(f, team_id) for f in away_fixtures) if m][:HOME_FORM_LIMIT]
    return matches, sum(1 for m in matches if m["result"] == "W")


def _entry_result_away(fixture: dict) -> dict:
    status = _status_short(fixture)
    goals = fixture.get("goals") or {}
    gh, ga = goals.get("home"), goals.get("away")
    if status not in {"FT", "AET", "PEN"} or gh is None or ga is None:
        return {"status": "pending", "label": "Pendente", "class": "pending"}
    if ga > gh:
        return {"status": "green", "label": "GREEN", "class": "green"}
    return {"status": "red", "label": "RED", "class": "red"}


def _evaluate_away_candidate(fixture: dict, away_odd: dict, scan_date: str | None = None) -> dict:
    fx = fixture.get("fixture") or {}
    br_date, br_time = _fixture_br_parts(fx)
    league = fixture.get("league") or {}
    teams = fixture.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    item = {
        "fixture_id": fx.get("id"),
        "date": br_date, "time": br_time, "timestamp": fx.get("timestamp"),
        "status": _status_short(fixture),
        "league": {"id": league.get("id"), "name": league.get("name") or "", "country": league.get("country") or "", "season": league.get("season"), "logo": league.get("logo") or ""},
        "home": {"id": home.get("id"), "name": home.get("name") or "", "logo": home.get("logo") or ""},
        "away": {"id": away.get("id"), "name": away.get("name") or "", "logo": away.get("logo") or ""},
        "score": _score_for_fixture(fixture),
        "entry_result": _entry_result_away(fixture),
        "home_odd": away_odd,  # reusa o campo pra a odd da ENTRADA (visitante)
        "entry_side": "away",
        "checks": {},
    }
    home_id, away_id = home.get("id"), away.get("id")
    league_id, season = league.get("id"), league.get("season")

    odd_ok = ODD_MIN <= away_odd["odd"] <= ODD_MAX
    item["checks"]["odd"] = {"ok": odd_ok, "value": away_odd["odd"], "target": f"{ODD_MIN:.2f}-{ODD_MAX:.2f}"}
    if not odd_ok:
        item["qualified"] = False
        item["checks_passed"] = 0
        item["checks_total"] = 1
        return item

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_h2h = ex.submit(_h2h, away_id, home_id)   # vitorias do VISITANTE no confronto
        f_form = ex.submit(_away_form, away_id)      # forma do visitante FORA
        f_table = ex.submit(_standings, league_id, season, home_id, away_id, scan_date)
        h2h_matches, h2h_wins = f_h2h.result()
        away_matches, away_wins = f_form.result()
        home_standing, away_standing = f_table.result()

    rank_gap = None
    if home_standing and away_standing and home_standing.get("rank") and away_standing.get("rank"):
        rank_gap = int(home_standing["rank"]) - int(away_standing["rank"])  # visitante ACIMA do mandante

    item["h2h"] = h2h_matches
    item["home_form"] = away_matches  # reusa o campo (forma fora)
    item["standing"] = {"home": home_standing, "away": away_standing, "rank_gap": rank_gap}
    item["checks"]["h2h"] = {"ok": h2h_wins >= H2H_MIN_HOME_WINS, "value": h2h_wins, "target": f">= {H2H_MIN_HOME_WINS}/{H2H_LIMIT}"}
    item["checks"]["home_form"] = {"ok": away_wins >= HOME_FORM_MIN_WINS, "value": away_wins, "target": f">= {HOME_FORM_MIN_WINS}/{HOME_FORM_LIMIT}"}
    item["checks"]["standing"] = {"ok": rank_gap is not None and rank_gap >= MIN_RANK_GAP, "value": rank_gap, "target": f">= {MIN_RANK_GAP} posicoes"}
    passed = sum(1 for c in item["checks"].values() if c["ok"])
    item["checks_passed"] = passed
    item["checks_total"] = len(item["checks"])
    item["qualified"] = passed >= MIN_CHECKS_PASSED
    return item


def _load_away_cache() -> dict:
    data = _json_load(AWAY_CACHE_PATH, {})
    return data if isinstance(data, dict) else {}


def refresh_away_results() -> int:
    """Atualiza SO o placar/resultado dos jogos de visitante ja iniciados, sem
    recalcular os checks (que sao point-in-time e devem ficar congelados)."""
    cache = _load_away_cache()
    if not cache:
        return 0
    now_ts = int(time.time())
    stale_before = now_ts - RESULTS_STALE_HOURS * 3600
    pending: dict[int, list[dict]] = {}
    for snap in cache.values():
        if not isinstance(snap, dict):
            continue
        for ln in ("qualified", "rejected"):
            for it in snap.get(ln) or []:
                fid = it.get("fixture_id")
                if not fid:
                    continue
                if (it.get("entry_result") or {}).get("status") in {"green", "red"}:
                    continue
                ts = it.get("timestamp") or 0
                if ts and (ts > now_ts + 60 or ts < stale_before):
                    continue
                pending.setdefault(int(fid), []).append(it)
    if not pending:
        return 0
    ids = list(pending.keys())[:RESULTS_MAX_PER_CYCLE]
    batches = [ids[i:i + RESULTS_BATCH_SIZE] for i in range(0, len(ids), RESULTS_BATCH_SIZE)]
    updated = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for fixtures in ex.map(lambda ch: _af_get("/fixtures", {"ids": "-".join(map(str, ch))}).get("response") or [], batches):
            for fx in fixtures:
                fid = (fx.get("fixture") or {}).get("id")
                if fid is None:
                    continue
                for it in pending.get(int(fid), []):
                    it["status"] = _status_short(fx)
                    it["score"] = _score_for_fixture(fx)
                    it["entry_result"] = _entry_result_away(fx)
                    updated += 1
    if updated:
        with standings_lock:
            _json_save(AWAY_CACHE_PATH, cache)
    return updated


def run_away_scan(scan_date: str, include_played: bool = True) -> dict:
    """Avalia os VISITANTES favoritos do dia. Salva em away_cache.json[scan_date]."""
    scan_date = _iso_date(scan_date)
    data = _af_get_pages("/fixtures", {"date": scan_date, "timezone": "America/Sao_Paulo"})
    fixtures = data.get("response") or []
    scan_fixtures = fixtures if include_played else [f for f in fixtures if _is_prelive(f)]
    away_odds = _away_odds_by_fixture(scan_date)
    candidates = [(f, away_odds.get(_fixture_id(f))) for f in scan_fixtures]
    candidates = [(f, o) for f, o in candidates if o and ODD_MIN <= o["odd"] <= ODD_MAX][:MAX_CANDIDATES]

    fresh = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for item in ex.map(lambda c: _evaluate_away_candidate(c[0], c[1], scan_date), candidates):
            fresh.append(item)

    with standings_lock:
        cache = _load_away_cache()
        old = cache.get(scan_date) or {}
        # PRESERVA jogos ja capturados: os que sairam do scan (ja comecaram) ficam,
        # e os do scan novo (ainda prelive) atualizam/entram. Nada some.
        existing: dict[int, dict] = {}
        for ln in ("qualified", "rejected"):
            for it in old.get(ln) or []:
                fid = it.get("fixture_id")
                if fid:
                    existing[int(fid)] = it
        for it in fresh:
            fid = it.get("fixture_id")
            if fid:
                existing[int(fid)] = it
        merged = list(existing.values())
        q = sorted([it for it in merged if it.get("qualified")], key=lambda x: (x.get("timestamp") or 0, x["home_odd"]["odd"]))
        r = sorted([it for it in merged if not it.get("qualified")], key=lambda x: (x.get("timestamp") or 0, x["home_odd"]["odd"]))
        snapshot = {
            "date": scan_date,
            "finished_at": _now_br().isoformat(timespec="seconds"),
            "total_fixtures": len(fixtures),
            "odd_candidates": len(candidates),
            "qualified": q,
            "rejected": r,
        }
        cache[scan_date] = snapshot
        _json_save(AWAY_CACHE_PATH, cache)
    # notifica no Telegram os 4/4 de visitante ainda nao iniciados (pre-jogo)
    now_ts = int(time.time())
    upcoming = [it for it in q if (it.get("timestamp") or 0) > now_ts]
    if REALTIME_TG:
        try:
            _notify_perfect_away_entries(upcoming)
        except Exception as exc:
            print(f"[telegram] falha visitante: {exc}")
    return snapshot


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
        # timezone alinha o "date" com a data BR (jogos noturnos senao pulariam pro dia seguinte UTC).
        fixtures_data = _af_get_pages("/fixtures", {"date": scan_date, "timezone": "America/Sao_Paulo"})
        scan_errors = []
        if fixtures_data.get("errors"):
            scan_errors.append({"fixtures": fixtures_data.get("errors")})
        fixtures = fixtures_data.get("response") or []
        # Guarda anti-clobber: a API-Football as vezes devolve response vazio
        # (rate-limit por-minuto/timeout) SEM sinalizar erro. Nesse caso nao
        # sobrescreve um snapshot anterior que ja tinha jogos -- mantem o bom.
        if not fixtures:
            prev = _get_cached_snapshot(scan_date, include_played)
            if prev and (prev.get("total_fixtures") or 0) > 0:
                with state_lock:
                    state.update(prev)
                    state["status"] = "ready"
                    state["message"] = (
                        f"Fetch de fixtures vazio (rate-limit/timeout) - mantido "
                        f"scan anterior de {prev.get('finished_at')} "
                        f"({len(prev.get('qualified') or [])} entradas)."
                    )
                print(f"[scan] fixtures vazio p/ {scan_date}; snapshot anterior preservado.")
                return
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

        errors = []
        raw = []
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = [ex.submit(_evaluate_candidate, fixture, odd, scan_date) for fixture, odd in candidates]
            for future in as_completed(futures):
                try:
                    raw.append(future.result())
                except Exception as exc:
                    errors.append(str(exc))

        # Regra: uma vez 4/4, sempre 4/4. Trava os que estao 4/4 agora e reaplica
        # a estrela em todos os travados (novos podem entrar, nenhum sai).
        locked = _load_star_locks()
        novos = {str(it["fixture_id"]) for it in raw if it.get("fixture_id") and _is_star_now(it)}
        if novos:
            _save_star_locks(novos)
        locked |= novos

        qualified = []
        rejected = []
        for item in raw:
            _force_star(item, locked)
            if item.get("qualified"):
                qualified.append(item)
            else:
                rejected.append(item)

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
        if REALTIME_TG:
            try:
                _notify_perfect_entries(qualified)
            except Exception as exc:
                print(f"[telegram] hook falhou: {exc}")
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
            today = _today_br()
            return self._json(200, {
                "today": today.isoformat(),
                "yesterday": (today - timedelta(days=1)).isoformat(),
                "tomorrow": _tomorrow(),
            })
        if parsed.path == "/api/by-day":
            query = urllib.parse.parse_qs(parsed.query)
            include_rejected = (query.get("include_rejected", ["0"])[0] == "1")
            buckets = _collect_games_by_br_day(include_rejected=include_rejected)
            if "date" in query:
                day = _iso_date(query.get("date", [""])[0])
                items = buckets.get(day, [])
                return self._json(200, {
                    "date": day,
                    "stats": _day_summary(items),
                    "qualified": items,
                })
            days = sorted(buckets.keys(), reverse=True)
            return self._json(200, {
                "days": [{"date": d, **_day_summary(buckets[d])} for d in days],
            })
        if parsed.path == "/api/by-days":
            # Intervalo de dias em UMA chamada (casa + visitante), evita 2 requests por dia no front
            query = urllib.parse.parse_qs(parsed.query)
            raw = [d for d in (query.get("dates", [""])[0] or "").split(",") if d]
            if not raw and "from" in query:
                lo = datetime.strptime(_iso_date(query.get("from", [""])[0]), "%Y-%m-%d").date()
                hi = datetime.strptime(_iso_date(query.get("to", [lo.isoformat()])[0]), "%Y-%m-%d").date()
                if hi < lo:
                    lo, hi = hi, lo
                raw = [(lo + timedelta(days=i)).isoformat() for i in range((hi - lo).days + 1)]
            days = sorted({_iso_date(d) for d in raw})[:400]
            buckets = _collect_games_by_br_day(include_rejected=False)
            away = _load_away_cache()
            seen: set[str] = set()
            merged: list[dict] = []
            for d in days:
                homes = buckets.get(d, [])
                aways = ((away.get(d) or {}).get("qualified") or []) if isinstance(away.get(d), dict) else []
                for it in list(homes) + list(aways):
                    k = f"{it.get('fixture_id')}:{it.get('entry_side') or 'home'}"
                    if k in seen:
                        continue
                    seen.add(k)
                    merged.append(it)
            merged.sort(key=lambda x: x.get("timestamp") or 0)
            return self._json(200, {"dates": days, "stats": _day_summary(merged), "pnl": _pnl(merged, 100.0), "qualified": merged})
        if parsed.path == "/api/overall":
            query = urllib.parse.parse_qs(parsed.query)
            include_rejected = (query.get("include_rejected", ["0"])[0] == "1")
            try:
                stake = float(query.get("stake", ["100"])[0])
            except (TypeError, ValueError):
                stake = 100.0
            buckets = _collect_games_by_br_day(include_rejected=include_rejected)
            items = [it for day_items in buckets.values() for it in day_items]
            items.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)
            return self._json(200, {
                "stats": _day_summary(items),
                "pnl": _pnl(items, stake),
                "qualified": items,
            })
        if parsed.path == "/api/away":
            query = urllib.parse.parse_qs(parsed.query)
            cache = _load_away_cache()
            if "date" in query:
                day = _iso_date(query.get("date", [""])[0])
                snap = cache.get(day) or {"date": day, "qualified": [], "rejected": []}
                q = snap.get("qualified") or []
                return self._json(200, {"date": day, "stats": _day_summary(q), "pnl": _pnl(q, 100.0), "qualified": q})
            # sem data: consolidado geral (todos os dias)
            allq = [it for snap in cache.values() if isinstance(snap, dict) for it in (snap.get("qualified") or [])]
            allq.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)
            days = sorted(cache.keys(), reverse=True)
            return self._json(200, {
                "days": [{"date": d, **_day_summary(cache[d].get("qualified") or [])} for d in days],
                "stats": _day_summary(allq),
                "pnl": _pnl(allq, 100.0),
                "qualified": allq,
            })
        if parsed.path == "/api/history":
            return self._json(200, _history_response())
        path = HERE / parsed.path.lstrip("/")
        if path.exists() and path.is_file() and path.parent == HERE:
            ctype = "text/css; charset=utf-8" if path.suffix == ".css" else "text/plain; charset=utf-8"
            return self._send(200, path.read_bytes(), ctype)
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        # Public (Tailscale Funnel) visitors are read-only: POSTs trigger paid API-Football scans.
        if self.headers.get("Tailscale-Funnel-Request"):
            return self._json(403, {"error": "read_only"})
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/history/refresh":
            refresh_snapshot_results()
            try:
                refresh_away_results()  # visitantes tambem (usado pelo botao "Atualizar resultados" do Lab20)
            except Exception:
                pass
            return self._json(200, refresh_history_results())
        if parsed.path == "/api/refresh-all":
            # atualiza placares (rapido) de casa e visitante
            try:
                refresh_snapshot_results()
                refresh_away_results()
                refresh_history_results()
            except Exception:
                pass
            # re-scan em background (casa + visitante, hoje e amanha)
            today = _today_br().isoformat()
            tomorrow = _tomorrow()

            def _bg():
                try:
                    with state_lock:
                        busy = state.get("status") == "running"
                    if not busy:
                        run_scan(today, False)
                except Exception:
                    pass
                for d in (today, tomorrow):
                    try:
                        run_away_scan(d, False)
                    except Exception:
                        pass

            threading.Thread(target=_bg, daemon=True).start()
            return self._json(200, {"ok": True, "today": today, "tomorrow": tomorrow})
        if parsed.path == "/api/away/scan":
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                payload = {}
            scan_date = _iso_date(payload.get("date"))
            include_played = bool(payload.get("include_played", False))
            try:
                snap = run_away_scan(scan_date, include_played)
                q = snap.get("qualified") or []
                q4 = sum(1 for it in q if it.get("checks_passed", 0) >= it.get("checks_total", 4))
                return self._json(200, {"ok": True, "date": scan_date, "qualified": len(q), "star4": q4})
            except Exception as exc:
                return self._json(500, {"error": str(exc)})
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
    threading.Thread(target=_results_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"WAVE Pre-Live Favorites em http://localhost:{port}")
    print(f"Data default de amanha: {_tomorrow()}")
    server.serve_forever()


if __name__ == "__main__":
    main()
