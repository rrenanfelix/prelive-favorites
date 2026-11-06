#!/usr/bin/env python3
"""Gera snapshot estatico da dash (pro GitHub Pages) a partir do servidor local.

Uso: python3 export_static.py <pasta_destino>
"""
import json
import shutil
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8891"
BR = timezone(timedelta(hours=-3))

# Responde as chamadas api/* do index.html a partir de snap/*.json, replicando os filtros do app.py.
SHIM = r"""
<script>
(() => {
  const realFetch = window.fetch.bind(window);
  const cache = {};
  const load = (n) => cache[n] || (cache[n] = realFetch(`snap/${n}.json?v=__VER__`).then(r => {
    if (!r.ok) throw new Error("snap " + n + " " + r.status);
    return r.json();
  }));
  const dayOf = (it) => it.timestamp
    ? new Date((it.timestamp - 10800) * 1000).toISOString().slice(0, 10)
    : String(it.date || "").slice(0, 10);
  const byTs = (a, b) => (a.timestamp || 0) - (b.timestamp || 0);
  async function route(url, method) {
    const u = new URL(url, location.href);
    const p = u.pathname.split("/api/")[1];
    const q = u.searchParams;
    if (method === "POST") return { ok: false, read_only: true };
    if (p === "default-date") return load("meta");
    if (p === "state") return load("state");
    if (p === "overall") return load("overall");
    if (p === "by-day") {
      if (!q.has("date")) return load("days");
      const D = q.get("date");
      const o = await load("overall");
      return { date: D, qualified: o.qualified.filter(x => dayOf(x) === D).sort(byTs) };
    }
    if (p === "away") {
      const a = await load("away");
      if (!q.has("date")) return a;
      const D = q.get("date");
      return { date: D, qualified: a.qualified.filter(x => String(x.date || "") === D) };
    }
    if (p === "by-days") {
      let lo = q.get("from"), hi = q.get("to") || lo;
      if (hi < lo) [lo, hi] = [hi, lo];
      const inRange = (d) => d >= lo && d <= hi;
      const [o, a] = await Promise.all([load("overall"), load("away")]);
      const seen = new Set();
      const merged = [];
      for (const it of [...o.qualified.filter(x => inRange(dayOf(x))), ...a.qualified.filter(x => inRange(String(x.date || "")))]) {
        const k = `${it.fixture_id}:${it.entry_side || "home"}`;
        if (seen.has(k)) continue;
        seen.add(k);
        merged.push(it);
      }
      merged.sort(byTs);
      return { qualified: merged };
    }
    throw new Error("fora do snapshot: " + p);
  }
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    if (!/(^|\/)api\//.test(url)) return realFetch(input, init);
    const method = ((init && init.method) || "GET").toUpperCase();
    return route(url, method).then(body => ({ ok: true, status: 200, json: async () => body }));
  };
})();
</script>
"""


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=300) as r:
        return json.load(r)


def dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> None:
    out = Path(sys.argv[1]).resolve()
    snap = out / "snap"
    snap.mkdir(parents=True, exist_ok=True)

    now = datetime.now(BR)
    meta = get("/api/default-date")
    meta["generated_at"] = now.strftime("%d/%m/%Y %H:%M")
    dump(snap / "meta.json", meta)
    dump(snap / "days.json", get("/api/by-day"))
    dump(snap / "state.json", get("/api/state"))
    dump(snap / "overall.json", get("/api/overall?stake=100"))
    dump(snap / "away.json", get("/api/away"))

    html = (HERE / "index.html").read_text(encoding="utf-8")
    html = html.replace("</head>", SHIM.replace("__VER__", now.strftime("%Y%m%d%H%M")) + "</head>", 1)
    html = html.replace(
        '<button id="refreshAllBtn" type="button" class="refresh-all-btn">',
        '<button id="refreshAllBtn" type="button" class="refresh-all-btn" hidden style="display:none">', 1)
    html = html.replace(
        '<span id="refreshStatus" class="refresh-status"></span>',
        f'<span id="refreshStatus" class="refresh-status">📸 Atualizado em {meta["generated_at"]}</span>', 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    shutil.copy2(HERE / "styles.css", out / "styles.css")
    (out / ".nojekyll").touch()
    print(f"snapshot gerado em {out} ({meta['generated_at']})")


if __name__ == "__main__":
    main()
