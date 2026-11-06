#!/bin/bash
# publica snapshot estatico da dash no GitHub Pages: https://rrenanfelix.github.io/prelive-favorites/
# gera em dashboards/prelive-public (branch gh-pages do repo rrenanfelix/prelive-favorites) e faz push.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
PUB="$HERE/../prelive-public"
/usr/bin/python3 "$HERE/export_static.py" "$PUB" || exit 1
cd "$PUB" && git add -A >/dev/null 2>&1
git -c user.name=rrenanflx -c user.email=renan.wavecom@gmail.com commit -qm "prelive snapshot $(date "+%F %H:%M")" >/dev/null 2>&1 || { echo "prelive public: nada mudou"; exit 0; }
git push -q origin gh-pages && echo "prelive public: publicado $(date '+%F %H:%M')"
