#!/usr/bin/env python3
"""
Envio diário WAVE FAV 4/4 no formato novo (Renan).
- Puxa 4/4 do dia (casa + visitante) pela API do dashboard local.
- Classifica cada jogo em Tier 1..4 pela heurística acordada (odd, gap, H2H, forma, liga, reserva).
- Filtra Tier 1 e Tier 2, ordena por horário do jogo.
- Envia no grupo BET WAVE / tópico WAVE FAV 4/4 via bot wavepre_bot.

Rodado pelo launchd às 00:00 (America/Sao_Paulo).
"""
from __future__ import annotations
import json, sys, os, datetime, urllib.request, urllib.parse
from pathlib import Path

HERE = Path(__file__).parent
CFG_PATH = HERE / "telegram_config.json"
SENT_LOG_PATH = HERE / "data" / "sent_games_log.json"
DIGEST_LOG_PATH = HERE / "data" / "digest_log.json"
API_HOME = "http://localhost:8891/api/state?date={d}"
API_AWAY = "http://localhost:8891/api/away?date={d}"

STRONG = ["premier league","serie a","eredivisie","superliga","super league","j1 league",
          "major league soccer","liga profesional","primera division","primera división",
          "primera a","bundesliga","la liga","ligue 1","primeira liga","championship"]
WEAK   = ["division 2","ykkönen","liga de ascenso","2. liga","primera c","non league",
          "regionalliga","ii division","tercera","reserve","4. liga"]
RES    = [" ii"," iii"," res."," res ","reserv","akatemia","academy","academia",
          "u19","u20","u21","u23","sub-20","sub-21","sub 20","sub-23","sub 23"]

# ISO country -> flag emoji (regional indicators)
def _flag(name: str) -> str:
    # mapping minimal by common names (dashboard uses country names, not ISO)
    m = {
        "argentina":"🇦🇷","brazil":"🇧🇷","england":"🏴󠁧󠁢󠁥󠁮󠁧󠁿","france":"🇫🇷","germany":"🇩🇪",
        "italy":"🇮🇹","spain":"🇪🇸","portugal":"🇵🇹","netherlands":"🇳🇱","belgium":"🇧🇪",
        "usa":"🇺🇸","united-states":"🇺🇸","mexico":"🇲🇽","japan":"🇯🇵","south-korea":"🇰🇷",
        "china":"🇨🇳","russia":"🇷🇺","belarus":"🇧🇾","ukraine":"🇺🇦","poland":"🇵🇱",
        "czech-republic":"🇨🇿","slovakia":"🇸🇰","hungary":"🇭🇺","romania":"🇷🇴","bulgaria":"🇧🇬",
        "greece":"🇬🇷","turkey":"🇹🇷","israel":"🇮🇱","egypt":"🇪🇬","tunisia":"🇹🇳",
        "morocco":"🇲🇦","algeria":"🇩🇿","south-africa":"🇿🇦","nigeria":"🇳🇬","tanzania":"🇹🇿",
        "kenya":"🇰🇪","ghana":"🇬🇭","colombia":"🇨🇴","chile":"🇨🇱","peru":"🇵🇪",
        "uruguay":"🇺🇾","paraguay":"🇵🇾","ecuador":"🇪🇨","venezuela":"🇻🇪","bolivia":"🇧🇴",
        "canada":"🇨🇦","costa-rica":"🇨🇷","honduras":"🇭🇳","el-salvador":"🇸🇻","guatemala":"🇬🇹",
        "panama":"🇵🇦","nicaragua":"🇳🇮","norway":"🇳🇴","sweden":"🇸🇪","finland":"🇫🇮",
        "denmark":"🇩🇰","iceland":"🇮🇸","ireland":"🇮🇪","scotland":"🏴󠁧󠁢󠁳󠁣󠁴󠁿","wales":"🏴󠁧󠁢󠁷󠁬󠁳󠁿",
        "northern-ireland":"🇬🇧","northern ireland":"🇬🇧","switzerland":"🇨🇭","austria":"🇦🇹",
        "croatia":"🇭🇷","serbia":"🇷🇸","slovenia":"🇸🇮","bosnia":"🇧🇦","albania":"🇦🇱",
        "malta":"🇲🇹","cyprus":"🇨🇾","luxembourg":"🇱🇺","estonia":"🇪🇪","latvia":"🇱🇻",
        "lithuania":"🇱🇹","georgia":"🇬🇪","armenia":"🇦🇲","azerbaijan":"🇦🇿","kazakhstan":"🇰🇿",
        "uzbekistan":"🇺🇿","saudi-arabia":"🇸🇦","uae":"🇦🇪","united-arab-emirates":"🇦🇪",
        "qatar":"🇶🇦","bahrain":"🇧🇭","kuwait":"🇰🇼","iran":"🇮🇷","iraq":"🇮🇶",
        "jordan":"🇯🇴","lebanon":"🇱🇧","syria":"🇸🇾","yemen":"🇾🇪","india":"🇮🇳",
        "thailand":"🇹🇭","vietnam":"🇻🇳","indonesia":"🇮🇩","malaysia":"🇲🇾","singapore":"🇸🇬",
        "philippines":"🇵🇭","australia":"🇦🇺","new-zealand":"🇳🇿",
    }
    k = (name or "").strip().lower().replace(" ", "-")
    return m.get(k, "🌍")

def _is_4(it: dict) -> bool:
    return bool(it.get("checks_total")) and it.get("checks_passed", 0) >= it.get("checks_total", 4)

def _tier(it: dict) -> int:
    c = it["checks"]
    odd = float(it["home_odd"]["odd"])
    gap = c["standing"].get("value") or 0
    h2h = c["h2h"]["value"]
    form = c["home_form"]["value"]
    lg = (it["league"]["name"] or "").lower()
    side = it.get("entry_side") == "away"
    pick = (it["away"]["name"] if side else it["home"]["name"]).lower()
    s = 0
    s += 3 if gap >= 12 else 2 if gap >= 9 else 1 if gap >= 7 else 0
    if 1.5 <= odd <= 1.9:
        s += 2
    elif odd > 2.1:
        s -= 2
    if odd >= 2.25:
        s -= 1
    if h2h >= 5:
        s += 1
    elif h2h >= 4:
        s += 0.5
    if form >= 8:
        s += 1
    elif form <= 5:
        s -= 0.5
    if any(x in lg for x in STRONG) and not any(x in lg for x in WEAK):
        s += 2
    if any(x in lg for x in WEAK):
        s -= 2
    if any(pick.endswith(f.strip()) or f in pick for f in RES):
        s -= 3
    if side:
        s -= 0.5
    return 1 if s >= 4 else 2 if s >= 2 else 3 if s >= 0 else 4

# --- Política de envio (tier x lado) calibrada com o desempenho de AGOSTO/2026 ---
# "full" = entraria stake cheio | "half" = entraria meia entrada | "block" = não envia.
# Base: T1(casa/fora) e T3-casa e T4-fora foram lucrativos e consistentes;
# T2-casa e T3-fora pagam pouco (meia); T2-fora (-45% ROI) e T4-casa foram cortados.
ENTRY_POLICY = {
    (1, "home"): "full",  (1, "away"): "full",
    (3, "home"): "full",  (4, "away"): "full",
    (2, "home"): "half",  (3, "away"): "half",
    (2, "away"): "block", (4, "home"): "block",
}

def _side(it: dict) -> str:
    return "away" if it.get("entry_side") == "away" else "home"

def _policy(it: dict) -> str:
    return ENTRY_POLICY.get((_tier(it), _side(it)), "block")

def _fetch(url: str) -> list:
    with urllib.request.urlopen(url, timeout=15) as r:
        d = json.load(r)
    return d.get("qualified", []) or []

def _pick_line(it: dict) -> tuple[str, str, str, str, float]:
    side = it.get("entry_side") == "away"
    home = it["home"]["name"]
    away = it["away"]["name"]
    pick_name = away if side else home
    pick_label = f"{pick_name} (fora)" if side else pick_name
    icon = "🚌" if side else "🏠"
    odd = float(it["home_odd"]["odd"])
    match = f"{home} x {away}"
    return match, pick_label, icon, pick_name, odd

TIER_HDR = {
    1: "🟢 TIER 1 · Entraria",
    2: "🟡 TIER 2 · Consideraria",
    3: "🟠 TIER 3 · Marginal",
    4: "🔴 TIER 4 · Cuidado",
}

STAKE_LABEL = {"full": "💵 stake cheio", "half": "½ meia entrada"}

def build_message(date_br: str, items: list[dict]) -> str:
    lines = [f"⚽ FAVORITOS 4/4 — {date_br} (seleção tier×lado)", ""]
    picks = [it for it in items if _policy(it) in ("full", "half")]
    if not picks:
        lines.append("⚠️ Nenhuma entrada dentro da política (tier×lado) hoje. Dia de passar.")
        return "\n".join(lines)
    for it in picks:
        t = _tier(it)
        pol = _policy(it)
        country = it["league"].get("country") or ""
        league = it["league"].get("name") or ""
        time = it.get("time") or it.get("kickoff") or "??:??"
        flag = _flag(country)
        match, pick_label, icon, _, odd = _pick_line(it)
        lines += [
            f"• {flag} {league} · {time}",
            f"{TIER_HDR[t]} · {STAKE_LABEL[pol]}",
            f"{icon} {match}",
            f"• Aposta: {pick_label} · odd {odd:.2f}",
            "",
        ]
    return "\n".join(lines).rstrip()

def send(cfg: dict, text: str) -> dict:
    tok = cfg["bot_token"]
    payload = {
        "chat_id": cfg["chat_id"],
        "text": text,
        "disable_web_page_preview": True,
    }
    if cfg.get("message_thread_id"):
        payload["message_thread_id"] = cfg["message_thread_id"]
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendMessage", data=data
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)

def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _entry_key(it: dict) -> str:
    side = "away" if it.get("entry_side") == "away" else "home"
    return f"{side}:{it.get('fixture_id')}"

def _freeze_sent_item(it: dict) -> dict:
    side = "away" if it.get("entry_side") == "away" else "home"
    home = it.get("home") or {}
    away = it.get("away") or {}
    league = it.get("league") or {}
    return {
        "key": _entry_key(it),
        "fixture_id": it.get("fixture_id"),
        "side": side,
        "tier": _tier(it),
        "time": it.get("time") or it.get("kickoff") or "99:99",
        "home": home.get("name"),
        "away": away.get("name"),
        "pick": away.get("name") if side == "away" else home.get("name"),
        "league": league.get("name"),
        "country": league.get("country"),
        "odd": float((it.get("home_odd") or {}).get("odd") or 0),
    }

def _record_sent_games(day_iso: str, items: list[dict], message_id=None) -> None:
    log = _load_json(SENT_LOG_PATH, {})
    rec = log.get(day_iso) if isinstance(log.get(day_iso), dict) else {}
    old = {e.get("key"): e for e in rec.get("entries", []) if isinstance(e, dict)}
    for it in items:
        frozen = _freeze_sent_item(it)
        old[frozen["key"]] = frozen
    mids = rec.get("message_ids", [])
    if message_id and message_id not in mids:
        mids.append(message_id)
    log[day_iso] = {
        "entries": [old[k] for k in sorted(old)],
        "message_ids": mids,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _save_json(SENT_LOG_PATH, log)

def _legacy_sent_keys(day_iso: str) -> list[str]:
    """Compatibilidade com o digest antigo, que guardava so side:fixture_id."""
    log = _load_json(DIGEST_LOG_PATH, {})
    val = log.get(day_iso)
    if isinstance(val, list):
        return [x for x in val if isinstance(x, str) and ":" in x]
    if isinstance(val, dict):
        return [
            str(e.get("key"))
            for e in val.get("entries", [])
            if isinstance(e, dict) and e.get("key")
        ]
    return []

def _items_of(b):
    if isinstance(b, list):
        return [x for x in b if isinstance(x, dict) and "home" in x]
    if isinstance(b, dict):
        cand = b.get("qualified") if isinstance(b.get("qualified"), list) else list(b.values())
        return [x for x in cand if isinstance(x, dict) and "home" in x]
    return []

def _is4_home(entry):
    c = entry.get("checks", {})
    return all((c.get(k) or {}).get("ok") for k in ["odd","h2h","home_form","standing"])

def _all_away_items(away_cache: dict) -> list[dict]:
    out = []
    for b in away_cache.values():
        out.extend(_items_of(b))
    return out

def _find_live_item(sent: dict, home_hist: dict, away_cache: dict) -> dict | None:
    fid = str(sent.get("fixture_id") or "").strip()
    side = sent.get("side")
    if not fid:
        return None
    if side == "home":
        for it in home_hist.values():
            if str(it.get("fixture_id")) == fid:
                return it
    else:
        for it in _all_away_items(away_cache):
            if str(it.get("fixture_id")) == fid:
                return it
    return None

def _sent_entries_for_day(day_iso: str, home_hist: dict, away_cache: dict) -> list[dict]:
    sent_log = _load_json(SENT_LOG_PATH, {})
    rec = sent_log.get(day_iso) if isinstance(sent_log.get(day_iso), dict) else {}
    sent = [e for e in rec.get("entries", []) if isinstance(e, dict)]

    if not sent:
        for key in _legacy_sent_keys(day_iso):
            side, fid = key.split(":", 1)
            sent.append({"key": key, "side": side, "fixture_id": fid})

    out = []
    for s in sent:
        live = _find_live_item(s, home_hist, away_cache)
        out.append({"sent": s, "item": live})
    return out

def _sent_detail(e: dict) -> dict:
    sent = e["sent"]
    it = e.get("item") or {}
    side = sent.get("side") or ("away" if it.get("entry_side") == "away" else "home")
    home = ((it.get("home") or {}).get("name")) or sent.get("home") or "Casa"
    away = ((it.get("away") or {}).get("name")) or sent.get("away") or "Visitante"
    pick = sent.get("pick") or (away if side == "away" else home)
    icon = "🚌" if side == "away" else "🏠"
    odd = sent.get("odd")
    if not odd:
        odd = float((it.get("home_odd") or {}).get("odd") or 0)
    tier = sent.get("tier")
    if not tier and it:
        try:
            tier = _tier(it)
        except Exception:
            tier = "?"
    status = (it.get("entry_result") or {}).get("status", "pending")
    score = it.get("score") or "-"
    time = sent.get("time") or it.get("time") or "99:99"
    if status == "green":
        pnl = float(odd or 0) - 1
        line = f"🟢 {pick} {icon} {score} · @{float(odd):.2f} · +{pnl:.2f}u [T{tier}]"
    elif status == "red":
        pnl = -1
        line = f"🔴 {pick} {icon} {score} · @{float(odd):.2f} · −1,00u [T{tier}]"
    else:
        pnl = 0
        line = f"⏳ {pick} {icon} pendente · @{float(odd):.2f} [T{tier}]"
    return {"line": line, "pnl": pnl, "result": status, "time": time, "side": side}

def build_closing_message(day_iso: str, day_br: str, home_hist: dict, away_cache: dict, month_prefix: str) -> str | None:
    """Fechamento do dia + acumulado mensal (formato unidades)."""
    yyyy, mm = month_prefix.split("-", 1)
    sent_day = _sent_entries_for_day(day_iso, home_hist, away_cache)
    entries = [_sent_detail(e) for e in sent_day]

    if not entries:
        return None  # sem lista oficial enviada -> nao posta fechamento inventado

    entries.sort(key=lambda e: e["time"])
    g = sum(1 for e in entries if e["result"] == "green")
    r = sum(1 for e in entries if e["result"] == "red")
    saldo = sum(e["pnl"] for e in entries)
    hit = f"{g/(g+r)*100:.0f}%" if (g + r) else "—"

    month_details = []
    sent_days = set()
    sent_log = _load_json(SENT_LOG_PATH, {})
    if isinstance(sent_log, dict):
        sent_days.update(d for d in sent_log.keys() if str(d).startswith(month_prefix))
    legacy_log = _load_json(DIGEST_LOG_PATH, {})
    if isinstance(legacy_log, dict):
        sent_days.update(d for d in legacy_log.keys() if str(d).startswith(month_prefix))
    for d in sorted(sent_days):
        month_details.extend(_sent_detail(e) for e in _sent_entries_for_day(d, home_hist, away_cache))

    hg = sum(1 for e in month_details if e["side"] == "home" and e["result"] == "green")
    hr = sum(1 for e in month_details if e["side"] == "home" and e["result"] == "red")
    ag = sum(1 for e in month_details if e["side"] == "away" and e["result"] == "green")
    ar = sum(1 for e in month_details if e["side"] == "away" and e["result"] == "red")
    G = hg + ag
    R = hr + ar
    profM = sum(e["pnl"] for e in month_details)

    parts = [f"📋 FECHAMENTO {day_br} — WAVE FAV 4/4", ""]
    parts.append(f"{len(entries)} entrada(s) enviadas no WAVE FAV 4/4:")
    parts.append("")
    parts += [e["line"] for e in entries]
    parts += [
        "",
        "━━━━━━━━━━━━━━",
        f"Placar do dia: 🟢 {g} · 🔴 {r}" + (f"  ({hit})" if (g + r) else ""),
        f"Saldo do dia: {saldo:+.2f}u".replace(".", ","),
        "━━━━━━━━━━━━━━",
        "",
        f"📊 MENSAL {mm}/{yyyy} (somente envios registrados)",
        "",
        f"🏠 Casa:       🟢 {hg} · 🔴 {hr}",
        f"🚌 Visitante:  🟢 {ag} · 🔴 {ar}",
        "",
        f"✅ TOTAL: 🟢 {G} · 🔴 {R}",
        f"🎯 Assertividade: {G/(G+R)*100:.0f}% ({G}/{G+R})" if (G+R) else "🎯 Sem finalizados ainda",
        f"💰 Saldo acumulado: {profM:+.2f}u".replace(".", ","),
    ]
    return "\n".join(parts)

def main():
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    today = datetime.datetime.now().date()
    tomorrow = today + datetime.timedelta(days=1)
    today_iso = today.isoformat()
    tom_iso = tomorrow.isoformat()
    today_br = today.strftime("%d/%m")
    tom_br = tomorrow.strftime("%d/%m")
    month_prefix = today.strftime("%Y-%m")

    # ---- 1) FECHAMENTO DO DIA ATUAL (jogos ainda em andamento ficam como ⏳ pendente) ----
    # Atualiza resultados no dashboard antes de montar o fechamento (evita jogo já encerrado ficar como pendente)
    try:
        req = urllib.request.Request("http://localhost:8891/api/history/refresh", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=180):
            pass
        print("OK results refreshed")
    except Exception as e:
        print(f"warn: refresh de resultados falhou: {e}")
    try:
        home_hist = json.loads((HERE / "data" / "validated_history.json").read_text(encoding="utf-8"))["entries"]
        away_cache = json.loads((HERE / "data" / "away_cache.json").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"warn: sem dados de fechamento: {e}")
        home_hist, away_cache = {}, {}

    closing = build_closing_message(today_iso, today_br, home_hist, away_cache, month_prefix)
    if closing:
        res1 = send(cfg, closing)
        if res1.get("ok"):
            print(f"OK closing sent msg_id={res1['result']['message_id']}")
        else:
            print("FAIL closing", json.dumps(res1)[:400])
    else:
        print(f"skip closing: nenhum T1/T2 em {today_iso}")

    # ---- 2) JOGOS 4/4 T1+T2 DE AMANHÃ ----
    home = [it for it in _fetch(API_HOME.format(d=tom_iso)) if _is_4(it)]
    away = [it for it in _fetch(API_AWAY.format(d=tom_iso)) if _is_4(it)]
    combined = home + away
    combined.sort(key=lambda it: (it.get("time") or it.get("kickoff") or "99:99"))
    filtered = [it for it in combined if _policy(it) in ("full", "half")]

    if filtered:
        msg = build_message(tom_br, filtered).replace(
            f"⚽ FAVORITOS 4/4 — {tom_br}", f"⚽ FAVORITOS 4/4 — AMANHÃ {tom_br}"
        )
    else:
        msg = f"⚽ FAVORITOS 4/4 — AMANHÃ {tom_br} (seleção tier×lado)\n\n⚠️ Nenhuma entrada dentro da política (tier×lado) até agora pra amanhã."

    print(f"[{datetime.datetime.now().isoformat()}] posting {len(filtered)} T1/T2 games for {tom_iso}")
    res = send(cfg, msg)
    if res.get("ok"):
        mid = res.get("result", {}).get("message_id")
        print(f"OK games sent msg_id={mid}")
        _record_sent_games(tom_iso, filtered, mid)
    else:
        print("FAIL games", json.dumps(res)[:400])
        sys.exit(1)

if __name__ == "__main__":
    main()
