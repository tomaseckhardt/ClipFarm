#!/bin/bash
# ClipFarm – instalace pro macOS: stačí dvakrát kliknout ve Finderu.
# Nainstaluje Homebrew (když chybí), Python s Tk (okno), yt-dlp, ffmpeg a Node.js (YouTube ho potřebuje),
# pak spustí ClipFarm. Ten si při prvním spuštění vytvoří ~/Applications/ClipFarm.app (Launchpad, Dock).
set -e
cd "$(dirname "$0")"
brew_env() {  # Homebrew do PATH: Apple Silicon (/opt/homebrew) i Intel (/usr/local)
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
  return 0
}
brew_env
if ! command -v brew >/dev/null; then
  echo "Instaluji Homebrew (zeptá se na heslo k Macu)…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  brew_env
fi
brew install python@3.13 python-tk@3.13 yt-dlp ffmpeg node
echo
echo "Hotovo. Spouštím ClipFarm – příště ho najdeš v Launchpadu nebo v ~/Applications jako ClipFarm."
"$(brew --prefix python@3.13)/bin/python3.13" ClipFarm.py &
