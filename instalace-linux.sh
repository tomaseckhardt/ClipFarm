#!/bin/bash
# ClipFarm – instalace pro Linux (Fedora, Ubuntu/Debian, Arch). Doinstaluje jen to, co chybí, a spustí ClipFarm,
# který se tím přidá do nabídky aplikací. Spuštění: v terminálu `bash instalace-linux.sh`,
# nebo v Souborech pravým tlačítkem -> Spustit jako program (otevře si terminál sám).
set -e
self=$(readlink -f "$0")
cd "$(dirname "$self")"

if [ ! -t 1 ]; then  # spuštěno bez terminálu -> otevřít ho, ať je vidět průběh a jde zadat heslo
  for t in "kgx --" "gnome-terminal --" "konsole -e" "xfce4-terminal -x" "xterm -e"; do
    if command -v "${t%% *}" >/dev/null; then exec ${t} bash "$self"; fi  # záměrně bez uvozovek: "program přepínač"
  done
fi

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
  echo "ClipFarm potřebuje Python 3.12 nebo novější (Fedora 39+, Ubuntu 24.04+, Debian 13+)."
  read -rp "Enter zavře okno… " || true
  exit 1
fi

# co chybí (pořadí jako v names níž): tkinter (okno), ffmpeg, node (YouTube), dekodér H.264 pro přehrávač klipů
missing=()
python3 -c 'import tkinter' 2>/dev/null || missing+=(0)
command -v ffmpeg >/dev/null || missing+=(1)
command -v node >/dev/null || missing+=(2)
gst-inspect-1.0 openh264dec >/dev/null 2>&1 || gst-inspect-1.0 avdec_h264 >/dev/null 2>&1 || missing+=(3)

if command -v dnf >/dev/null; then
  install=(sudo dnf install -y); names=(python3-tkinter ffmpeg-free nodejs gstreamer1-plugin-openh264)
elif command -v apt-get >/dev/null; then
  install=(sudo apt-get install -y); names=(python3-tk ffmpeg nodejs gstreamer1.0-libav)
elif command -v pacman >/dev/null; then
  install=(sudo pacman -S --needed --noconfirm); names=(tk ffmpeg nodejs gst-libav)
else
  install=(); names=()
fi
pkgs=()
if [ ${#names[@]} -gt 0 ]; then
  for i in "${missing[@]}"; do pkgs+=("${names[$i]}"); done
fi

if [ ${#pkgs[@]} -gt 0 ]; then
  echo "Instaluji: ${pkgs[*]} (zeptá se na heslo)…"
  if [ "${install[1]}" = apt-get ]; then sudo apt-get update; fi
  "${install[@]}" "${pkgs[@]}"
elif [ ${#missing[@]} -gt 0 ]; then
  echo "Neznámá distribuce: nainstaluj ručně tkinter pro Python, ffmpeg a Node.js, pak spusť tenhle soubor znovu."
fi

# yt-dlp přímo od autorů: verze v distribucích bývají staré a YouTube s nimi nefunguje
if ! command -v yt-dlp >/dev/null; then
  echo "Stahuji yt-dlp do ~/.local/bin…"
  mkdir -p ~/.local/bin
  python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp", sys.argv[1])' ~/.local/bin/yt-dlp
  chmod +x ~/.local/bin/yt-dlp
  export PATH="$HOME/.local/bin:$PATH"
fi

setsid -f python3 ClipFarm.py </dev/null >/dev/null 2>&1  # okno + ikona v nabídce; přežije zavření terminálu
echo
echo "Hotovo. ClipFarm se otevírá; příště ho najdeš v nabídce aplikací (klávesa Super -> ClipFarm)."
read -rp "Enter zavře okno… " || true
