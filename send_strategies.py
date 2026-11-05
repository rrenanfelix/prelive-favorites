#!/usr/bin/env python3
"""
Envio das ESTRATÉGIAS SEPARADAS no grupo BET WAVE — um tópico (thread) por estratégia.

Modos:
  night  (22:05) — jogos de AMANHÃ de: WAVE LOW 4/4 só TIER 1 (8893), WAVE GAP 4/4 gap 12–15 (8907),
                   LAB 20 GAP EXTREMO s05 ligas grandes (prewave_lab20_big_picks.json)
  (GOLEADA e PLACAR EXATO removidos em 14/09 a pedido do Renan. UNDER 0.5 HT e ZEBRA são ao vivo, direto pelos apps 8896/8897.)

Config: strategies_config.json (thread_id por estratégia; criados via createForumTopic se faltarem).
Dedup: data/strategies_sent.json (não repete envio do mesmo dia/estratégia, salvo --force).
Uso: send_strategies.py [night|placar] [--force] [--dry] [--only low,gap,goleada,placar] [--date YYYY-MM-DD]
"""
from __future__ import annotations
import json, sys, datetime, urllib.request, urllib.parse
from pathlib import Path

HERE = Path(__file__).parent
WS = HERE.parent.parent
CFG_PATH = HERE / "strategies_config.json"
TG_CFG = HERE / "telegram_config.json"
SENT_PATH = HERE / "data" / "strategies_sent.json"
SB = WS / "agents" / "sports-betting"

sys.path.insert(0, str(HERE))
from send_daily_v2 import _flag, _is_4, _tier  # reutiliza bandeira, regra 4/4 e tier

TOPICS = {
    "low":      "WAVE LOW 4/4",
    "gap":      "WAVE GAP 4/4",
    "lab20gap": "LAB 20 GAP EXTREMO",
    "underht":  "UNDER 0.5 HT",   # ao vivo (app 8896)
    "zebra":    "ZEBRA WAVE",     # ao vivo (app 8897)
}

# ---------------- telegram ----------------
def _tg(tok: str, method: str, **payload) -> dict:
    data = urllib.parse.urlencode({k: v for k, v in payload.items() if v is not None}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def load_cfg() -> dict:
    # Grupo BET TEST usa o @betwaveclaw_bot (agents/sports-betting); wavepre_bot fica so no BET WAVE / WAVE FAV 4/4.
    cfg = json.loads(CFG_PATH.read_text()) if CFG_PATH.exists() else {}
    cfg.setdefault("topics", {})
    src = cfg.get("bot_token_source")
    tg = json.loads((WS / src).read_text()) if src else json.loads(TG_CFG.read_text())
    cfg.setdefault("chat_id", tg.get("chat_id") or tg.get("group_id"))
    cfg["_bot_token"] = tg.get("bot_token") or tg.get("token")
    return cfg

def save_cfg(cfg: dict) -> None:
    CFG_PATH.write_text(json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")},
                                   ensure_ascii=False, indent=2) + "\n")

def ensure_topic(cfg: dict, key: str) -> int:
    t = cfg["topics"].get(key)
    if t and t.get("thread_id"):
        return int(t["thread_id"])
    res = _tg(cfg["_bot_token"], "createForumTopic", chat_id=cfg["chat_id"], name=TOPICS[key])
    if not res.get("ok"):
        raise RuntimeError(f"createForumTopic {key}: {json.dumps(res)[:300]}")
    tid = res["result"]["message_thread_id"]
    cfg["topics"][key] = {"name": TOPICS[key], "thread_id": tid}
    save_cfg(cfg)
    print(f"topic created {key} -> thread {tid}")
    return tid

def send(cfg: dict, key: str, text: str, dry=False) -> int | None:
    tid = ensure_topic(cfg, key)
    if dry:
        print(f"--- [{key} / thread {tid}] ---\n{text}\n")
        return None
    res = _tg(cfg["_bot_token"], "sendMessage", chat_id=cfg["chat_id"], message_thread_id=tid,
              text=text, disable_web_page_preview=True)
    if not res.get("ok"):
        print(f"FAIL {key}: {json.dumps(res)[:300]}")
        return None
    mid = res["result"]["message_id"]
    print(f"OK {key} sent msg_id={mid}")
    return mid

# ---------------- dedup ----------------
def _sent() -> dict:
    try: return json.loads(SENT_PATH.read_text())
    except Exception: return {}

def _mark(key: str, day: str, mid, n: int) -> None:
    d = _sent(); d.setdefault(day, {})[key] = {"msg_id": mid, "n": n, "at": datetime.datetime.now().isoformat(timespec="seconds")}
    SENT_PATH.parent.mkdir(exist_ok=True); SENT_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2))

# ---------------- fontes ----------------
def _get(url: str, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)

def fetch_low(day: str) -> list[dict]:
    out = []
    for path in ("state", "away"):
        try: q = _get(f"http://localhost:8893/api/{path}?date={day}").get("qualified") or []
        except Exception as e: print(f"warn low/{path}: {e}"); q = []
        out += [it for it in q if it.get("date") == day and _is_4(it) and _tier(it) == 1]  # só TIER 1
    out.sort(key=lambda it: it.get("time") or "99:99")
    return out

def fetch_gap(day: str) -> list[dict]:
    try: d = _get("http://localhost:8907/api/data")
    except Exception as e: print(f"warn gap: {e}"); return []
    rows = [r for r in d.get("upcoming", []) if r.get("date") == day and r.get("decision") == "principal"]
    rows.sort(key=lambda r: r.get("time") or "99:99")
    return rows

def fetch_lab20gap(day: str) -> list[dict]:
    p = SB / "prewave_lab20" / "out" / "prewave_lab20_big_picks.json"
    picks = list((json.loads(p.read_text()).get(day) or {}).get("s05") or [])
    picks.sort(key=lambda x: x.get("time") or "99:99")
    return picks

# ---------------- mensagens ----------------
def _hdr(title: str, day_br: str, when: str, sub: str) -> list[str]:
    return [f"{title} — {when} {day_br}", sub, ""]

def msg_low(day_br: str, when: str, items: list[dict]) -> str:
    lines = _hdr("🟦 WAVE LOW 4/4", day_br, when, "favoritos odd 1.30–1.49 · só 4/4 TIER 1")
    if not items:
        return "\n".join(lines + ["⚠️ Nenhum 4/4 TIER 1 até agora. Dia de passar."])
    for it in items:
        lg, side = it["league"], it.get("entry_side") == "away"
        home, away = it["home"]["name"], it["away"]["name"]
        pick = f"{away} (fora)" if side else home
        lines += [f"• {_flag(lg.get('country'))} {lg.get('name')} · {it.get('time') or '??:??'}",
                  f"{'🚌' if side else '🏠'} {home} x {away}",
                  f"• Aposta: {pick} · odd {float(it['home_odd']['odd']):.2f}", ""]
    return "\n".join(lines).rstrip()

def msg_gap(day_br: str, when: str, items: list[dict]) -> str:
    lines = _hdr("🟩 WAVE GAP 4/4", day_br, when, "4/4 mandante · gap de tabela 12–15 · stake cheia")
    if not items:
        return "\n".join(lines + ["⚠️ Nenhum 4/4 com gap 12–15 até agora. Dia de passar."])
    for r in items:
        country = (r.get("league") or "").split(" · ")[0]
        extra = f" · {' · '.join(r['tags'])}" if r.get("tags") else ""
        lines += [f"• {_flag(country)} {r.get('league')} · {r.get('time') or '??:??'}",
                  f"🏠 {r.get('home')} x {r.get('away')}",
                  f"• Aposta: {r.get('home')} · odd {float(r.get('odd') or 0):.2f} · gap {r.get('gap')}{extra}", ""]
    return "\n".join(lines).rstrip()

def msg_lab20gap(day_br: str, when: str, items: list[dict]) -> str:
    lines = _hdr("🏆 LAB 20 GAP EXTREMO", day_br, when, "favorito 13+ posições acima · só ligas grandes · back")
    if not items:
        return "\n".join(lines + ["⚠️ Nenhum favorito com gap 13+ nas ligas grandes até agora. Dia de passar."])
    for x in items:
        side = x.get("side") == "away"
        pick = f"{x.get('away')} (fora)" if side else x.get("home")
        odd = x.get("odd")
        lines += [f"• {_flag(x.get('country'))} {x.get('league')} · {x.get('time') or '??:??'}",
                  f"{'🚌' if side else '🏠'} {x.get('home')} x {x.get('away')}",
                  f"• Aposta: {pick} · odd {odd:.2f} · gap {x.get('gap')}" if odd else f"• Aposta: {pick} · sem odd · gap {x.get('gap')}", ""]
    return "\n".join(lines).rstrip()

BUILDERS = {"low": (fetch_low, msg_low), "gap": (fetch_gap, msg_gap), "lab20gap": (fetch_lab20gap, msg_lab20gap)}

# ---------------- main ----------------
def main(argv: list[str]) -> int:
    mode = "night"; force = dry = False; only = None; date_override = None
    it = iter(argv)
    for a in it:
        if a == "night": mode = a
        elif a == "--force": force = True
        elif a == "--dry": dry = True
        elif a == "--only": only = set(next(it).split(","))
        elif a == "--date": date_override = next(it)
    today = datetime.date.today()
    keys, day, when = ["low", "gap", "lab20gap"], today + datetime.timedelta(days=1), "AMANHÃ"
    if date_override:
        day = datetime.date.fromisoformat(date_override)
    if only: keys = [k for k in keys if k in only]
    day_iso, day_br = day.isoformat(), day.strftime("%d/%m")
    cfg = load_cfg()
    sent = _sent().get(day_iso, {})
    rc = 0
    for k in keys:
        if k in sent and not force and not dry:
            print(f"skip {k}: já enviado {day_iso} (msg {sent[k].get('msg_id')})"); continue
        fetch, build = BUILDERS[k]
        try: items = fetch(day_iso)
        except Exception as e:
            print(f"FAIL fetch {k}: {e}"); rc = 1; continue
        text = build(day_br, when, items)
        print(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {k}: {len(items)} itens p/ {day_iso}")
        mid = send(cfg, k, text, dry=dry)
        if mid and not dry: _mark(k, day_iso, mid, len(items))
        elif not dry: rc = 1
    return rc

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
