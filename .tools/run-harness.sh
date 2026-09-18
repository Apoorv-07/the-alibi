#!/bin/bash
# One-shot recovery of the *browser harness* environment in a sandbox that deletes generated directories,
# then the harness itself. Scratch script: it exists so the harness run below is reproducible, not as product.
set -e
cd /home/user/alibi-twin

echo "[1/4] npm + chromium"
make setup-browser 2>&1 | tail -3

echo "[2/4] chromium's shared libraries (no root here, so extract the .debs)"
if [ ! -e /home/user/.local/lib/libnspr4.so ]; then
  python3 - > /tmp/urls.txt <<'PY'
import urllib.request, lzma, re
data = lzma.decompress(urllib.request.urlopen(
    "http://deb.debian.org/debian/dists/trixie/main/binary-amd64/Packages.xz").read()).decode("utf8", "replace")
want = {"libnspr4","libnss3","libatk1.0-0t64","libatk-bridge2.0-0t64","libatspi2.0-0t64","libxkbcommon0",
        "libxdamage1","libasound2t64","libcups2t64","libgbm1","libdrm2","libxcomposite1","libxcursor1",
        "libxfixes3","libxrandr2","libxi6","libexpat1","libgraphite2-3","libharfbuzz0b","libthai0",
        "libfribidi0","libdatrie1","libpixman-1-0","libdbus-1-3","libudev1","libpango-1.0-0","libcairo2"}
for block in data.split("\n\n"):
    pk = re.search(r"^Package: (\S+)$", block, re.M)
    fn = re.search(r"^Filename: (\S+)$", block, re.M)
    if pk and fn and pk.group(1) in want:
        print("http://deb.debian.org/debian/" + fn.group(1))
PY
  mkdir -p /tmp/debs /home/user/.local/lib
  cd /tmp/debs && xargs -n1 -P8 curl -sO < /tmp/urls.txt
  for f in *.deb; do ar x "$f" data.tar.xz && tar -xJf data.tar.xz ./usr/lib/x86_64-linux-gnu/ 2>/dev/null || true; done
  cp -a usr/lib/x86_64-linux-gnu/*.so* /home/user/.local/lib/
  cd /home/user/alibi-twin
fi
echo "  missing libs (must be 0): $(LD_LIBRARY_PATH=/home/user/.local/lib ldd /home/user/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell 2>/dev/null | grep -c 'not found' || echo '?')"

echo "[3/4] the app must be up for the harness to drive it"
curl -sf -o /dev/null http://127.0.0.1:8000/ && echo "  :8000 up" || { echo "  server down — aborting"; exit 1; }

echo "[4/4] harness"
make browser-check 2>&1 | tail -40
