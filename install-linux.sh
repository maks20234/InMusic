#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is missing. Install Python 3 first."; exit 1; }
python3 - <<'PY'
import sys
if sys.version_info < (3,8): raise SystemExit('InMusic needs Python 3.8 or newer.')
print('Python version OK:',sys.version.split()[0])
PY
mkdir -p "Data/All Artists" "Data/Home Widgets" "Data/Playlists"
if [ ! -f "Data/user-library.json" ]; then printf '{\n  "favoriteArtists": [],\n  "favoriteSongs": []\n}\n' > "Data/user-library.json"; fi
echo "InMusic is ready. Run ./run-linux.sh"
