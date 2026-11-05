#!/usr/bin/env python3
"""Bot diario consolidado do WAVE FAV 4/4.

Dois comandos, pensados pra rodar via launchd:

  python3 daily_bot.py digest [YYYY-MM-DD]
      -> 00:30 BR: manda TODAS as entradas 4/4 do dia (uma mensagem so),
         filtradas por Tier 1 e Tier 2 (ver tiers.json). Mandante e visitante.

  python3 daily_bot.py report [YYYY-MM-DD]
      -> 23:59 BR: manda o resultado do dia (green/red) + fechamento do mes,
         simulando stake fixa de R$100 por entrada.

Reaproveita toda a logica de scan/resultado/P&L do app.py (importado como modulo,
sem subir o servidor). Nao consome quota alem do refresh de placares no report.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import app  # importa o modulo do servidor; NAO sobe o servidor (main() so roda em __main__)

HERE = Path(__file__).resolve().parent
TIERS_PATH = HERE / "tiers.json"
DIGEST_LOG_PATH = app.DATA_DIR / "digest_log.json"
STAKE = 100.0

# ------------------------------------------------------------------ tiers
def _load_tiers():
    data = json.loads(TIERS_PATH.read_text(encoding="utf-8"))
    t1 = set(int(k) for k in (data.get("tier1") or {}).keys())
    t2 = set(int(k) for k in (data.get("tier2") or {}).keys())
    return t1, t2

TIER1, TIER2 = _load_tiers()

def _tier_of(item) -> int | None:
    lid = (item.get("league") or {}).get("id")
    if lid in TIER1:
        return 1
    if lid in TIER2:
        return 2
    return None

# ------------------------------------------------------------------ star (4/4)
def _is_star_home(item, locks) -> bool:
    total = item.get("checks_total") or 0
    passed = item.get("checks_passed") or 0
    if total > 0 and passed >= total:
        return True
    fid = item.get("fixture_id")
    return fid is not None and str(fid) in locks

def _is_star_away(item) -> bool:
    total = item.get("checks_total") or 0
    passed = item.get("checks_passed") or 0
    return total > 0 and passed >= total

# ------------------------------------------------------------------ coleta
def _away_by_br_day():
    """Agrupa entradas de VISITANTE por data BR (deriva do timestamp)."""
    cache = app._load_away_cache()
    best = {}
    def rank(x):
        return 1 if (x.get("entry_result") or {}).get("status") in ("green", "red") else 0
    for snap in cache.values():
        if not isinstance(snap, dict):
            continue
        for it in snap.get("qualified") or []:
            fid = it.get("fixture_id")
            if not fid:
                continue
            prev = best.get(int(fid))
            if prev is None or rank(it) >= rank(prev):
                best[int(fid)] = it
    buckets = {}
    for it in best.values():
        day = app._br_date_from_ts(it.get("timestamp")) or app._iso_date(it.get("date"))
        buckets.setdefault(day, []).append(it)
    return buckets

def _collect(day_filter):
    """Retorna entradas 4/4 tier 1/2 (casa + visitante) cujo dia BR passa em day_filter(day)."""
    locks = app._load_star_locks()
    home_buckets = app._collect_games_by_br_day()
    away_buckets = _away_by_br_day()
    out = []
    for day, items in home_buckets.items():
        if not day_filter(day):
            continue
        for it in items:
            t = _tier_of(it)
            if t and _is_star_home(it, locks):
                out.append({"item": it, "side": "home", "tier": t, "day": day})
    for day, items in away_buckets.items():
        if not day_filter(day):
            continue
        for it in items:
            t = _tier_of(it)
            if t and _is_star_away(it):
                out.append({"item": it, "side": "away", "tier": t, "day": day})
    out.sort(key=lambda e: (e["tier"], e["item"].get("timestamp") or 0))
    return out

def _entry_key(e) -> str:
    return f"{e['side']}:{e['item'].get('fixture_id')}"

# ------------------------------------------------------------------ format
def _ddmm(day: str) -> str:
    try:
        y, m, d = day.split("-")
        return f"{d}/{m}"
    except ValueError:
        return day

def _brl(v: float) -> str:
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    sign = "-" if v < 0 else "+"
    return f"{sign}R${s}"

def _fmt_entry(e) -> str:
    it = e["item"]
    home = (it.get("home") or {}).get("name", "")
    away = (it.get("away") or {}).get("name", "")
    lg = it.get("league") or {}
    odd = (it.get("home_odd") or {}).get("odd")
    time_txt = it.get("time") or ""
    league_txt = f"{lg.get('country', '')} - {lg.get('name', '')}".strip(" -")
    if e["side"] == "home":
        match = f"⚽ <b>{home}</b> x {away}"
        pick = f"Back <b>{home}</b> (mandante)"
    else:
        match = f"⚽ {home} x <b>{away}</b>"
        pick = f"🚌 Back <b>{away}</b> (visitante)"
    return f"{match}\n🕒 {time_txt} · 🏆 {league_txt}\n🎯 {pick} · 💰 <b>{odd}</b>"

# ------------------------------------------------------------------ envio chunk
def _send_paragraphs(cfg, paragraphs, limit=3500):
    """Junta paragrafos em mensagens <= limit e envia em sequencia."""
    buf = []
    size = 0
    for p in paragraphs:
        plen = len(p) + 2
        if buf and size + plen > limit:
            app._tg_send(cfg, "\n\n".join(buf))
            buf, size = [], 0
        buf.append(p)
        size += plen
    if buf:
        app._tg_send(cfg, "\n\n".join(buf))

# ------------------------------------------------------------------ digest log
def _load_digest_log():
    try:
        return json.loads(DIGEST_LOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_digest_log(data):
    DIGEST_LOG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

# ------------------------------------------------------------------ comandos
def cmd_digest(day: str) -> int:
    cfg = app._load_tg_config()
    if not cfg:
        print("[digest] telegram_config.json ausente")
        return 1
    all_entries = _collect(lambda d: d == day)
    date_br = _ddmm(day)

    if not all_entries:
        app._tg_send(cfg, f"📋 <b>WAVE FAV 4/4 — {date_br} (BR)</b>\nSem entradas 4/4 (Tier 1/2) para hoje.")
        print("[digest] 0 entradas")
        return 0

    # Idempotencia: se o job rodar de novo no mesmo dia, so envia o que ainda nao foi.
    log = _load_digest_log()
    day_sent = set(log.get(day, []))
    entries = [e for e in all_entries if _entry_key(e) not in day_sent]
    if not entries:
        print(f"[digest] nada novo (todas as {len(all_entries)} ja enviadas hoje)")
        return 0

    t1 = [e for e in entries if e["tier"] == 1]
    t2 = [e for e in entries if e["tier"] == 2]
    header = (
        f"📋 <b>WAVE FAV 4/4 — {date_br} (BR)</b>\n"
        f"Entradas do dia · Tier 1 e 2 · <b>{len(entries)}</b> jogo(s)\n"
        f"Stake simulada: <b>R$100</b>/entrada"
    )
    paragraphs = [header]
    if t1:
        paragraphs.append("🥇 <b>TIER 1</b>")
        paragraphs += [_fmt_entry(e) for e in t1]
    if t2:
        paragraphs.append("🥈 <b>TIER 2</b>")
        paragraphs += [_fmt_entry(e) for e in t2]

    _send_paragraphs(cfg, paragraphs)

    log[day] = sorted(day_sent | {_entry_key(e) for e in entries})
    _save_digest_log(log)
    print(f"[digest] {len(entries)} entrada(s) enviada(s) ({len(t1)} T1 / {len(t2)} T2)")
    return 0

def _detail_line(e) -> str:
    it = e["item"]
    status = (it.get("entry_result") or {}).get("status")
    home = (it.get("home") or {}).get("name", "")
    away = (it.get("away") or {}).get("name", "")
    score = it.get("score") or "-"
    try:
        odd = float((it.get("home_odd") or {}).get("odd"))
    except (TypeError, ValueError):
        odd = None
    pick = home if e["side"] == "home" else away
    if status == "green":
        pnl = (odd - 1) * STAKE if odd else 0.0
        return f"🟢 {home} <b>{score}</b> {away} — {pick} @{odd} · {_brl(pnl)}"
    if status == "red":
        return f"🔴 {home} <b>{score}</b> {away} — {pick} @{odd} · {_brl(-STAKE)}"
    return f"⏳ {home} x {away} — {pick} @{odd} · pendente"

def cmd_report(day: str) -> int:
    cfg = app._load_tg_config()
    if not cfg:
        print("[report] telegram_config.json ausente")
        return 1
    # atualiza placares (casa + visitante + historico) antes de contabilizar
    for fn in (app.refresh_snapshot_results, app.refresh_away_results, app.refresh_history_results):
        try:
            fn()
        except Exception as exc:
            print(f"[report] refresh falhou ({fn.__name__}): {exc}")

    day_entries = _collect(lambda d: d == day)
    month_prefix = day[:7]  # YYYY-MM
    month_entries = _collect(lambda d: d.startswith(month_prefix))

    day_items = [e["item"] for e in day_entries]
    month_items = [e["item"] for e in month_entries]
    day_sum = app._day_summary(day_items)
    day_pnl = app._pnl(day_items, STAKE)
    month_sum = app._day_summary(month_items)
    month_pnl = app._pnl(month_items, STAKE)

    date_br = _ddmm(day)
    mm, yyyy = month_prefix.split("-")[1], month_prefix.split("-")[0]

    lines = [
        f"📊 <b>RESULTADO DO DIA — {date_br} (BR)</b>",
        "Tier 1 e 2 · stake R$100/entrada",
        "",
        f"Entradas: <b>{day_sum['total']}</b> · 🟢 {day_sum['green']} · 🔴 {day_sum['red']} · ⏳ {day_sum['pending']}",
        f"Lucro do dia: <b>{_brl(day_pnl['profit'])}</b>",
    ]
    finished_day = [e for e in day_entries if (e["item"].get("entry_result") or {}).get("status") in ("green", "red")]
    finished_day.sort(key=lambda e: e["item"].get("timestamp") or 0)
    if finished_day:
        lines.append("")
        lines += [_detail_line(e) for e in finished_day]

    roi = (month_pnl["profit"] / (month_pnl["finished"] * STAKE) * 100) if month_pnl["finished"] else 0.0
    hit = (month_sum["green"] / (month_sum["green"] + month_sum["red"]) * 100) if (month_sum["green"] + month_sum["red"]) else 0.0
    lines += [
        "",
        "━━━━━━━━━━━━━━",
        f"📅 <b>FECHAMENTO DO MÊS — {mm}/{yyyy}</b>",
        f"Entradas: {month_sum['total']} · finalizadas {month_pnl['finished']} · ⏳ {month_sum['pending']}",
        f"🟢 {month_sum['green']} green · 🔴 {month_sum['red']} red · aproveitamento <b>{hit:.0f}%</b>",
        f"Lucro do mês: <b>{_brl(month_pnl['profit'])}</b> · ROI {roi:+.1f}%",
    ]

    _send_paragraphs(cfg, ["\n".join(lines)])
    print(f"[report] dia {date_br}: {_brl(day_pnl['profit'])} | mes {mm}/{yyyy}: {_brl(month_pnl['profit'])} (ROI {roi:+.1f}%)")
    return 0

# ------------------------------------------------------------------ cli
def main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    day = argv[2] if len(argv) > 2 else app._today_br().isoformat()
    day = app._iso_date(day)
    if cmd == "digest":
        return cmd_digest(day)
    if cmd == "report":
        return cmd_report(day)
    print("uso: daily_bot.py [digest|report] [YYYY-MM-DD]")
    return 2

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
