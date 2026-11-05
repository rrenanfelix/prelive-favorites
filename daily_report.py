#!/usr/bin/env python3
"""Relatorio diario + mensal das entradas 4/4 ja enviadas pro Telegram.

Roda todo dia 00:00 BR via crontab, olha para trás:
- "Ontem" (data BR do dia que fechou) -> total green/red + lista jogo a jogo
- Mes corrente ate ontem -> resumo por dia (green/red/saldo)

Fonte:
- data/telegram_sent.json -> conjunto de fixture_ids que foram notificados como 4/4
- data/validated_history.json -> entry_result.status ("green"/"red"/"pending") + score/times

Publica no mesmo topico Telegram (WAVE FAV 4/4) usando telegram_config.json.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CFG_PATH = ROOT / "telegram_config.json"
SENT_PATH = DATA_DIR / "telegram_sent.json"
HIST_PATH = DATA_DIR / "validated_history.json"

BR_TZ = timezone(timedelta(hours=-3))


def _now_br() -> datetime:
    return datetime.now(BR_TZ)


def _fmt_br_date(iso: str) -> str:
    if not iso or len(iso) != 10:
        return iso or ""
    y, m, d = iso.split("-")
    return f"{d}/{m}"


def _load_json(path: Path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as exc:
        print(f"[report] falha lendo {path}: {exc}", file=sys.stderr)
        return default


def _tg_send(cfg: dict, text: str) -> None:
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
    fields = {
        "chat_id": cfg["chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    thread_id = cfg.get("message_thread_id")
    if thread_id not in (None, "", 0):
        fields["message_thread_id"] = str(thread_id)
    payload = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(url, data=payload, timeout=15) as r:
        body = json.loads(r.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"telegram nao-ok: {body}")


def _classify(entry: dict) -> str:
    """Devolve 'green' | 'red' | 'pending' (inclui void/nulo)."""
    st = ((entry.get("entry_result") or {}).get("status") or "pending").lower()
    if st not in {"green", "red"}:
        return "pending"
    return st


def build_report() -> str:
    now = _now_br()
    today = now.date()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)

    sent_state = _load_json(SENT_PATH, {"entries_sent": {}})
    sent_ids = set((sent_state.get("entries_sent") or {}).keys())

    hist = _load_json(HIST_PATH, {"entries": {}}).get("entries", {})

    # Coleta entradas do mes corrente ate ontem (inclusive) que foram enviadas ao Telegram.
    per_day: dict[str, dict[str, int]] = {}
    day_detail: list[dict] = []
    for fid in sent_ids:
        entry = hist.get(fid)
        if not entry:
            continue
        date_iso = entry.get("date") or ""
        if not date_iso or len(date_iso) != 10:
            continue
        try:
            dt = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError:
            continue
        if dt < month_start or dt > yesterday:
            continue
        status = _classify(entry)
        bucket = per_day.setdefault(date_iso, {"green": 0, "red": 0, "pending": 0})
        bucket[status] += 1
        if dt == yesterday:
            day_detail.append({
                "home": (entry.get("home") or {}).get("name", ""),
                "away": (entry.get("away") or {}).get("name", ""),
                "score": entry.get("score") or "?",
                "odd": (entry.get("home_odd") or {}).get("odd"),
                "status": status,
                "time": entry.get("time") or "",
            })

    # ---- monta mensagem ----
    y_bucket = per_day.get(yesterday.isoformat(), {"green": 0, "red": 0, "pending": 0})
    y_total = y_bucket["green"] + y_bucket["red"]
    y_hit = f"{(y_bucket['green'] / y_total * 100):.0f}%" if y_total else "—"
    lines: list[str] = []
    lines.append("📊 <b>Fechamento do dia — WAVE FAV 4/4</b>")
    lines.append(f"🗓 {_fmt_br_date(yesterday.isoformat())} (BR)")
    lines.append(
        f"🟢 GREEN: <b>{y_bucket['green']}</b> · 🔴 RED: <b>{y_bucket['red']}</b>"
        + (f" · ⏳ Pendente: {y_bucket['pending']}" if y_bucket['pending'] else "")
    )
    lines.append(f"🎯 Acerto: <b>{y_hit}</b>")

    if day_detail:
        lines.append("")
        lines.append("<b>Jogos:</b>")
        icon = {"green": "🟢", "red": "🔴", "pending": "⏳"}
        # ordena por horário
        for item in sorted(day_detail, key=lambda x: x.get("time") or ""):
            odd_txt = f" @ {item['odd']}" if item.get("odd") else ""
            lines.append(
                f"{icon.get(item['status'], '⏳')} {item['time']} — "
                f"{item['home']} <b>{item['score']}</b> {item['away']}{odd_txt}"
            )

    # ---- mensal por dia ----
    lines.append("")
    lines.append(f"📅 <b>Mês {yesterday.strftime('%m/%Y')} — dia a dia</b>")
    total_g = total_r = total_p = 0
    if per_day:
        for date_iso in sorted(per_day.keys()):
            b = per_day[date_iso]
            g, r, p = b["green"], b["red"], b["pending"]
            total_g += g
            total_r += r
            total_p += p
            n = g + r
            hit = f"{(g / n * 100):.0f}%" if n else "—"
            saldo = g - r
            saldo_txt = f"+{saldo}" if saldo > 0 else str(saldo)
            extra = f" · ⏳{p}" if p else ""
            lines.append(
                f"• {_fmt_br_date(date_iso)}: 🟢{g} · 🔴{r}{extra} · saldo {saldo_txt} · {hit}"
            )
        tot_n = total_g + total_r
        tot_hit = f"{(total_g / tot_n * 100):.0f}%" if tot_n else "—"
        tot_saldo = total_g - total_r
        tot_saldo_txt = f"+{tot_saldo}" if tot_saldo > 0 else str(tot_saldo)
        lines.append(
            f"<b>Total mês:</b> 🟢<b>{total_g}</b> · 🔴<b>{total_r}</b>"
            + (f" · ⏳{total_p}" if total_p else "")
            + f" · saldo {tot_saldo_txt} · acerto <b>{tot_hit}</b>"
        )
    else:
        lines.append("<i>Sem entradas registradas neste mês ainda.</i>")

    return "\n".join(lines)


def main() -> int:
    cfg = _load_json(CFG_PATH, None)
    if not cfg or not cfg.get("bot_token") or not cfg.get("chat_id"):
        print("[report] telegram_config.json ausente/incompleto", file=sys.stderr)
        return 1
    text = build_report()
    if os.environ.get("DRY_RUN"):
        print(text)
        return 0
    _tg_send(cfg, text)
    print(f"[report] enviado em {_now_br().isoformat(timespec='seconds')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
