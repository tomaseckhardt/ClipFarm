#!/usr/bin/env python3
"""ClipFarm: python Klipy.py <url>  ->  klipy/<kanál>/<datum – název>/01.mp4 ... + index.html
(složka klipy/ vždy vedle Klipy.py, video max. 720p). Přehled všech streamů: klipy/index.html

<url> = YouTube video/VOD, nebo živý Kick kanál (https://kick.com/<kanál>): nahrává video i chat
do konce streamu, Ctrl+C (nebo „Ukončit“ v okně ClipFarm) = ukončit nahrávání a hned nastříhat, co je nahrané.
Místo odkazu složka už staženého streamu = jen znovu nastříhá. Volitelně 2. argument = limit skóre,
3. argument = kolik minut živák z Kicku nahrávat (pak sám skončí a nastříhá; bez něj do konce streamu).

Potřebuje: Python 3.12+, yt-dlp, ffmpeg (Windows: instalace-windows.bat, macOS: instalace-macos.command).  Self-check: python Klipy.py --test
"""
import atexit
import base64
import html
import io
import json
import math
import os
import re
import shutil
import signal
import socket
import ssl
import statistics
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

MIN_SCORE = 2.5            # klip jen z momentu aspoň takhle výrazného (σ nad běžným stavem streamu), počet neomezený
GAP = 120                  # min. rozestup mezi momenty (s)
AROUND = 60                # s okolí, se kterým se moment porovnává (60 s před a 60 s po)
LONG, LONG_AROUND = 30, 120  # dlouhá vlna: 30 s vzrušení oproti 2 min před ní a 2 min až po ní
CHAT_WINDOW = 10           # chat reaguje se zpožděním: počítá se 10 s PO momentu
BEFORE = 22                # začátek klipu: 22 s před momentem (nebo střih scény 8–30 s před ním)
AFTER, MAX_AFTER = 8, 40   # konec: až reakce opadne, nejdřív 8 s a nejpozději 40 s po momentu
CALM, CALM_LEN = 1.0, 3    # reakce opadla = skóre aspoň 3 s pod 1 σ
SCENE = 0.2                # změna obrazu mezi snímky po 0,2 s nad tuhle mez = střih / prolínačka scény
SNAP = 2.0                 # o kolik s se smí začátek/konec posunout dovnitř, aby nestříhal uprostřed slova
SILENCE_DB, SILENCE_LEN = -55, 1.5  # ticho = černá obrazovka / pauza / přechod scény, klip přes něj nejde
HYPE = re.compile(r"kekw|omegalul|lul|lmao|pog|xd+|wtf|haha|\?{3,}|!{3,}|😂|🤣|💀", re.IGNORECASE)
HYPE_WEIGHT = 2            # zpráva s emotem/"xddd" = 1 + 2 ...
HYPE_AUTHORS = 3           # ... ale jen když je během ~10 s píšou aspoň 3 různí lidé


def download(url):
    """Stáhne video (+ YouTube chat). Živý stream nahrává do konce, do Ctrl+C nebo do „Ukončit“ v okně
    ClipFarm (soubor _logy/<pid>.stop); u Kicku mezitím zapisuje chat. Vrací (info, cesta k videu)."""
    kick = KICK_LIVE.fullmatch(url)
    when = time.strftime(" %H.%M") if kick else ""  # živák: čas začátku nahrávání, ať další běh nic nepřepíše
    fields = "\t".join(f"%({k})s" for k in INFO)
    proc = subprocess.Popen(
        ["yt-dlp", "--js-runtimes", "node", "--no-playlist", "--progress", "--encoding", "utf-8", "-f", "bv*+ba/b",
         "-S", "res:720", "--merge-output-format", "mp4", "--write-subs", "--sub-langs", "live_chat", "-P", str(ROOT),
         "-o", f"%(channel,uploader)s/%(upload_date>%Y-%m-%d)s{when} – %(title).60s/stream.%(ext)s",
         "--print", f"before_dl:START\t%(filename)s\t{fields}", "--print", f"after_move:DONE\t{fields}", url],
        stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        # vlastní skupina procesů (Linux i Windows): jde jí poslat Ctrl+C/Break, aniž by ho dostal i tenhle proces
        start_new_session=True, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    stop, done, begun = threading.Event(), "", {}

    def read():
        nonlocal done
        for line in proc.stdout or ():  # kromě našich řádků tu může být i průběh stahování
            tag, _, rest = line.rstrip("\n").partition("\t")
            if tag == "DONE":
                done = rest
            elif tag == "START":  # stahování / nahrávání začíná právě teď
                filename, _, values = rest.partition("\t")
                begun.update(dict(zip(INFO, values.split("\t"))), filepath=filename)
                job_update(part=filename + ".part")
                if kick:
                    chat = Path(filename).with_suffix(".live_chat.json")
                    threading.Thread(target=kick_chat, args=(kick[1], chat, time.time(), stop), daemon=True).start()

    threading.Thread(target=watch_stop, args=(proc,), daemon=True).start()
    try:
        read()
    except KeyboardInterrupt:
        interrupt(proc)
        read()
    stop.set()
    proc.wait()
    if done:
        info = dict(zip(INFO, done.split("\t")))
    elif begun and Path(begun["filepath"] + ".part").exists():  # yt-dlp nedopsal (Windows, výpadek): zachránit
        salvage(Path(begun["filepath"]))
        info = begun
    else:
        sys.exit("yt-dlp skončil bez videa")
    return info, Path(info["filepath"])


def interrupt(proc):
    """= Ctrl+C pro yt-dlp a jeho ffmpeg: dopíšou, co mají, a skončí."""
    print("\n⏹  Končím nahrávání a stříhám to, co je nahrané…", file=sys.stderr)
    try:
        if sys.platform == "win32":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGINT)
    except OSError:  # už skončil
        pass


def watch_stop(proc):
    """_logy/<pid>.stop = ukončit nahrávání jako Ctrl+C: prázdný hned, s časem (unix) až v tu chvíli (časovač)."""
    flag = JOBS / f"{os.getpid()}.stop"
    while proc.poll() is None:
        at = stop_time(flag)
        if at is not None and time.time() >= at:
            flag.unlink(missing_ok=True)
            interrupt(proc)
            return
        time.sleep(1)


def stop_time(flag):
    """Kdy se má nahrávání ukončit podle souboru .stop: None = dokud neskončí, 0 = hned."""
    try:
        text = flag.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return float(text) if text else 0.0


def salvage(final):
    """Nedopsaný živák (stream.mp4.part) -> stream.mp4, ať se z něj dá stříhat."""
    part = final.with_name(final.name + ".part")
    for _ in range(10):  # ffmpeg ještě může chvíli dopisovat
        size = part.stat().st_size
        time.sleep(1)
        if part.stat().st_size == size:
            break
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(part), "-c", "copy", "-movflags", "+faststart", str(final)],
                   check=True)
    part.unlink()


def job_update(**fields):
    """Stav běhu pro okno ClipFarm v _logy/<pid>.json (cíl, začátek, log, nahrávaný soubor)."""
    f = JOBS / f"{os.getpid()}.json"
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    f.write_text(json.dumps(data | fields, ensure_ascii=False), encoding="utf-8")


def keep_awake():
    """Nenechat počítač usnout, dokud tenhle proces běží (Linux i po zaklapnutí víka)."""
    if sys.platform == "win32":  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED, platí do konce procesu
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        # ponytail: zaklapnutí víka na Windows řídí Nastavení napájení („Při zavření víka: Nic nedělat“)
    elif sys.platform == "darwin":  # caffeinate je v macOS; víko i tak uspí (kromě zapojeného monitoru)
        subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
    elif shutil.which("systemd-inhibit"):
        subprocess.Popen(["systemd-inhibit", "--what=sleep:idle:handle-lid-switch", "--who=ClipFarm", "--mode=block",
                          f"--why=ClipFarm nahrává (PID {os.getpid()})", "tail", f"--pid={os.getpid()}", "-f", "/dev/null"],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


INFO = ("id", "title", "channel,uploader", "channel_url,uploader_url", "upload_date>%d. %m. %Y", "webpage_url",
        "extractor_key", "filepath")
ROOT = Path(__file__).resolve().parent / "klipy"  # klipy se ukládají vedle Klipy.py, ať je kdekoli
JOBS = ROOT / "_logy"  # stav běžících Klipy.py pro okno ClipFarm + logy
KICK_LIVE = re.compile(r"https?://(?:www\.)?kick\.com/([\w-]+)/?")  # kanál = živý stream (VOD má /videos/)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0 Safari/537.36"


def kick_line(msg, offset):
    """Zpráva z Kick chatu jako řádek YouTube live_chat.json, ať ji load_chat přečte stejně."""
    text = re.sub(r"\[emote:\d+:([^\]]+)\]", r"\1 ", msg.get("content", ""))
    item = {"message": {"runs": [{"text": text}]}, "authorExternalChannelId": str(msg["sender"]["id"])}
    return json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(int(offset * 1000)), "actions": [
        {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": item}}}]}}, ensure_ascii=False)


def kick_chat(slug, path, t0, stop):
    """Zapisuje Kick chat do path (čas od t0 = začátek nahrávání), dokud není stop."""
    try:
        req = urllib.request.Request(f"https://kick.com/api/v2/channels/{slug}", headers={"User-Agent": UA})
        room = json.load(urllib.request.urlopen(req, timeout=20))["chatroom"]["id"]
    except (OSError, KeyError, ValueError) as e:
        print(f"! Kick chat nejde načíst ({e}), jedu jen podle zvuku", file=sys.stderr)
        return
    with path.open("a", encoding="utf-8") as out:
        while not stop.is_set():
            try:
                for msg in pusher(f"chatrooms.{room}.v2"):
                    if stop.is_set():
                        return
                    out.write(kick_line(msg, time.time() - t0) + "\n")
                    out.flush()
            except (OSError, ValueError) as e:  # výpadek spojení -> znovu za 5 s
                print(f"! Kick chat: {e}, připojuji znovu", file=sys.stderr)
                stop.wait(5)


def pusher(channel):
    """Minimální websocket klient na Pusher (přes něj Kick posílá chat). Vrací zprávy z chatu."""
    host = "ws-us2.pusher.com"
    s = ssl.create_default_context().wrap_socket(socket.create_connection((host, 443), timeout=60), server_hostname=host)
    buf = b""

    def read(n):
        nonlocal buf
        while len(buf) < n:
            chunk = s.recv(65536)
            if not chunk:
                raise OSError("spojení ukončeno")
            buf += chunk
        out, buf = buf[:n], buf[n:]
        return out

    def send(data, op=0x1):
        data, mask = (json.dumps(data).encode() if op == 0x1 else data), os.urandom(4)
        size = bytes([0x80 | len(data)]) if len(data) < 126 else bytes([0xFE]) + struct.pack(">H", len(data))
        s.sendall(bytes([0x80 | op]) + size + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    with s:
        s.sendall(f"GET /app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0 HTTP/1.1\r\nHost: {host}\r\n"
                  f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                  f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\n\r\n".encode())
        while b"\r\n\r\n" not in buf:
            buf += s.recv(4096)
        head, buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError(f"websocket odmítnut: {head[:40]!r}")
        send({"event": "pusher:subscribe", "data": {"auth": "", "channel": channel}})
        while True:
            try:
                b1, b2 = read(2)
            except TimeoutError:  # minutu ticho -> udržet spojení naživu
                send({"event": "pusher:ping", "data": {}})
                continue
            size = b2 & 0x7F
            if size == 126:
                size = struct.unpack(">H", read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", read(8))[0]
            payload, op = read(size), b1 & 0x0F
            if op == 0x8:
                raise OSError("server zavřel spojení")
            if op == 0x9:
                send(payload, 0xA)
            elif op == 0x1:
                ev = json.loads(payload)
                if ev["event"] == "pusher:ping":
                    send({"event": "pusher:pong", "data": {}})
                elif ev["event"] == "App\\Events\\ChatMessageEvent":
                    yield json.loads(ev["data"])


def load_audio(video):
    """Hlasitost (RMS dB) po 0,1 s."""
    af = ("aformat=sample_rates=8000:channel_layouts=mono,asetnsamples=n=800,astats=metadata=1:reset=1,"
          "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-")
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-af", af, "-f", "null", "-"],
                         check=True, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace").stdout
    vals = (float(l.split("=")[1]) for l in out.splitlines() if "RMS_level=" in l)
    return [v if v > -90 else -90.0 for v in vals]  # ticho = -inf/nan


def load_chat(path, n):
    """Vážený počet zpráv po sekundách z yt-dlp live_chat.json. Proti spamu: každý autor
    se počítá max. jednou za CHAT_WINDOW s a bonus za hype jen při HYPE_AUTHORS různých lidech."""
    counts = [0.0] * n
    if not path.exists():
        print(f"! {path.name} chybí, jedu jen podle zvuku", file=sys.stderr)
        return counts
    msgs = []  # (sekunda, autor, hype?)
    for line in path.open(encoding="utf-8"):
        if not line.strip():
            continue
        replay = json.loads(line).get("replayChatItemAction", {})
        sec = int(replay.get("videoOffsetTimeMsec", -1)) // 1000
        if not 0 <= sec < n:
            continue
        for action in replay.get("actions", []):
            for item in action.get("addChatItemAction", {}).get("item", {}).values():
                runs = item.get("message", {}).get("runs", [])
                text = "".join(r.get("text") or (r.get("emoji", {}).get("shortcuts") or [""])[0] for r in runs)
                author = item.get("authorExternalChannelId")  # systémové hlášky YouTube autora nemají
                if text and author:
                    msgs.append((sec, author, bool(HYPE.search(text))))

    hypers = [set() for _ in range(n)]
    for sec, author, hype in msgs:
        if hype:
            hypers[sec].add(author)
    half = CHAT_WINDOW // 2
    last, last_hype = {}, {}
    for sec, author, hype in sorted(msgs, key=lambda m: m[0]):
        if sec - last.get(author, -CHAT_WINDOW) >= CHAT_WINDOW:
            last[author] = sec
            counts[sec] += 1
        if hype and sec - last_hype.get(author, -CHAT_WINDOW) >= CHAT_WINDOW \
                and len(set().union(*hypers[max(sec - half, 0):sec + half + 1])) >= HYPE_AUTHORS:
            last_hype[author] = sec
            counts[sec] += HYPE_WEIGHT
    return counts


def win(xs, lo, hi):
    return xs[max(lo, 0):max(min(hi, len(xs)), 0)]


def burst(xs, a, b, around=AROUND, skip=0):
    """Pro každé s: průměr xs v [s+a, s+b) minus vyšší z medianů okolí: around s před a around s
    po (po vynechání skip s). Výkyv (smích, křik, spam v chatu) vyjde vysoko. Trvalý skok (přepnutí
    scény, konec pauzy) ne, protože strana po skoku je stejně hlasitá."""
    out = []
    for s in range(len(xs)):
        sides = [statistics.median(w) for w in (win(xs, s + a - around, s + a),
                                                 win(xs, s + b + skip, s + b + skip + around)) if w]
        out.append(statistics.fmean(win(xs, s + a, s + b) or [0.0]) - (max(sides) if sides else 0.0))
    return out


def score_of(loud, chat):
    """Skóre každé sekundy v σ: o kolik je moment výraznější než běžný stav streamu. Krátká vlna
    (výkřik, smích ~5 s) i dlouhá (vzrušená pasáž ~30 s, třeba 2 min křiku), bere se silnější."""
    signals = [(burst(loud, -2, 3), burst(loud, 0, LONG, LONG_AROUND, LONG_AROUND))]
    if any(chat):
        signals.append((burst(chat, 0, CHAT_WINDOW), burst(chat, 0, LONG, LONG_AROUND, LONG_AROUND)))
    k = math.sqrt(len(signals))  # součet n nezávislých z-skóre má odchylku sqrt(n) -> stejná stupnice s chatem i bez
    short = [sum(z) / k for z in zip(*(zscore(sh) for sh, _ in signals))]
    long = [sum(z) / k for z in zip(*(zscore(lo) for _, lo in signals))]
    return [max(x, y) for x, y in zip(short, long)]


def zscore(xs):
    mu, sd = statistics.fmean(xs), statistics.pstdev(xs) or 1
    return [(x - mu) / sd for x in xs]


def pick_peaks(score, gap, margin=0, min_score=MIN_SCORE):
    """Všechny momenty se skóre >= min_score, nejsilnější první, aspoň gap s od sebe."""
    picked = []
    for s in sorted(range(margin, len(score) - margin), key=score.__getitem__, reverse=True):
        if score[s] < min_score:
            break
        if all(abs(s - p) >= gap for p in picked):
            picked.append(s)
    return sorted(picked)


def silences(loud10):
    """Úseky ticha (od, do) v s, delší než SILENCE_LEN."""
    runs, i = [], 0
    while i < len(loud10):
        j = i
        while j < len(loud10) and loud10[j] < SILENCE_DB:
            j += 1
        if j - i >= SILENCE_LEN * 10:
            runs.append((i / 10, j / 10))
        i = j + 1
    return runs


def quietest(loud10, a, b):
    """Nejtišší místo (s) v [a, b] po 0,1 s = pauza mezi slovy."""
    idx = range(max(round(a * 10), 0), min(round(b * 10), len(loud10) - 1) + 1)
    return min(idx, key=loud10.__getitem__) / 10 if idx else a


def scene_cuts(video, a, b):
    """Časy střihů a přechodů scén v [a, b] (s). Obraz po 0,2 s, takže chytí i prolínačku."""
    a = max(a, 0)
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{a:.1f}", "-t", f"{b - a:.1f}", "-i", str(video), "-an",
                          "-vf", f"fps=5,scale=160:-1,select='gt(scene,{SCENE})',metadata=print:file=-", "-f", "null", "-"],
                         check=True, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace").stdout
    return [a + float(t) for t in re.findall(r"pts_time:([\d.]+)", out)]


def clip_range(loud10, sil, score, cuts, s):
    """Začátek a konec klipu kolem momentu s.
    Začátek: poslední střih scény 8–30 s před momentem (ať klip nezačíná kusem jiné scény), jinak 22 s před.
    Konec: až reakce opadne (skóre CALM_LEN s pod CALM), 8–40 s po momentu; na střihu scény do 4 s od toho,
    jinak v pauze mezi slovy. Ticho (černá obrazovka, pauza streamu) uvnitř klipu nikdy."""
    n = len(loud10) / 10
    start = max((c for c in cuts if s - BEFORE - 8 <= c <= s - 8), default=None)
    snap_start = start is None
    start = max(s - BEFORE, 0) if start is None else start + 0.3  # +0.3 s: ne rozpitý snímek z prolínačky
    calm = next((t for t in range(s + AFTER, int(min(s + MAX_AFTER, n)))
                 if max(score[t:t + CALM_LEN], default=0) < CALM), s + MAX_AFTER)
    end = min(calm + 2, n)
    cut = min((c for c in cuts if abs(c - end) <= 4 and c > s + AFTER), key=lambda c: abs(c - end), default=None)
    snap_end = cut is None
    end = end if cut is None else cut - 0.3
    for a, b in sil:
        if start < b <= s:
            start, snap_start = b, True
        if s <= a < end:
            end, snap_end = a, True
    return (quietest(loud10, start, start + SNAP) if snap_start else start,
            quietest(loud10, end - SNAP, end) if snap_end else end)


def clips_word(n):
    return f"{n} {'klip' if n == 1 else 'klipy' if 2 <= n <= 4 else 'klipů'}"


def hms(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def write_overview(root):
    """root/index.html: všechny streamy podle streamera, nejnovější nahoře."""
    parts = []
    for ch in sorted(d for d in root.iterdir() if d.is_dir()):
        streams = sorted((d for d in ch.iterdir() if (d / "index.html").exists()), reverse=True)
        items = "".join(f'<li><a href="{quote(f"{ch.name}/{d.name}/index.html")}">{html.escape(d.name)}</a>'
                        f' · {clips_word(len(list(d.glob("[0-9]*.mp4"))))}</li>' for d in streams)
        if items:
            parts.append(f"<h2>{html.escape(ch.name)}</h2><ul>{items}</ul>")
    (root / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>ClipFarm</title><style>body{font-family:sans-serif;margin:1rem}'
        f'li{{margin:.3rem 0}}</style><h1>ClipFarm</h1>{"".join(parts)}', encoding="utf-8")


def main(target, min_score=MIN_SCORE, minutes=0):
    """target = odkaz (stáhne / nahraje živák), nebo složka už staženého streamu (jen znovu nastříhá).
    minutes > 0: živák z Kicku nahrávat jen tak dlouho, pak ukončit a nastříhat."""
    JOBS.mkdir(parents=True, exist_ok=True)
    job_update(pid=os.getpid(), target=target, started=time.time(), log=os.environ.get("CLIPFARM_LOG"), part=None)
    for f in (JOBS / f"{os.getpid()}.json", JOBS / f"{os.getpid()}.stop"):
        atexit.register(f.unlink, missing_ok=True)
    if minutes and KICK_LIVE.fullmatch(target):
        (JOBS / f"{os.getpid()}.stop").write_text(str(time.time() + minutes * 60), encoding="utf-8")
    if os.environ.get("CLIPFARM_AWAKE", "1") == "1":
        keep_awake()
    if Path(target).exists():
        folder = Path(target).resolve()
        folder = folder if folder.is_dir() else folder.parent
        video = folder / "stream.mp4"
        if not (folder / "info.json").exists() or not video.exists():
            sys.exit(f"{folder.name}: chybí info.json nebo stream.mp4 (zdrojové video smazané?), nejde přestříhat")
        info = json.loads((folder / "info.json").read_text(encoding="utf-8")) | {"filepath": str(video)}
    else:
        info, video = download(target)
        (video.parent / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    title, channel = info["title"], info["channel,uploader"]
    loud10 = load_audio(video)
    loud = [statistics.fmean(loud10[i:i + 10]) for i in range(0, len(loud10) - 9, 10)]
    sil = silences(loud10)
    chat = load_chat(video.with_suffix(".live_chat.json"), len(loud))
    score = score_of(loud, chat)
    peaks = pick_peaks(score, GAP, AROUND, min_score)
    tmp = video.parent / "_nove"  # nové klipy bokem: přerušení přestříhání nesmaže ty staré
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    print(f"{len(peaks)} momentů se skóre >= {min_score} (nejvyšší {max(score, default=0):.1f})")

    clips = []
    for i, s in enumerate(peaks, 1):
        start, end = clip_range(loud10, sil, score, scene_cuts(video, s - BEFORE - 10, s + MAX_AFTER + 6), s)
        clip = f"{i:02d}.mp4"
        print(f"{clip}  {hms(start)}–{hms(end)} ({end - start:.1f} s)  moment {hms(s)}  skóre {score[s]:.1f}")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.1f}", "-i", str(video), "-t", f"{end - start:.1f}",
                        "-c:v", "h264", "-b:v", "6M", "-c:a", "aac", "-movflags", "+faststart",
                        str(tmp / clip)], check=True)
        clips.append({"file": clip, "start": start, "end": end, "score": round(score[s], 1)})
    for old in video.parent.glob("[0-9]*.mp4"):  # hotovo: staré klipy pryč, nové na jejich místo
        old.unlink()
    for new in tmp.iterdir():
        new.rename(video.parent / new.name)
    tmp.rmdir()
    (video.parent / "klipy.json").write_text(json.dumps({"min_score": min_score, "clips": clips}, indent=1), encoding="utf-8")
    write_page(video.parent)
    print(f"Hotovo: {channel} – {title}\n  {video.parent / 'index.html'}\n  přehled: {video.parents[2] / 'index.html'}")


def write_page(folder):
    """index.html streamu z info.json + klipy.json (a přehled všech streamů)."""
    info = json.loads((folder / "info.json").read_text(encoding="utf-8"))
    data = json.loads((folder / "klipy.json").read_text(encoding="utf-8"))
    yt = info["extractor_key"].startswith("Youtube")  # odkaz na čas v klipu umí jen YouTube
    figs = [f'<figure><video controls preload="metadata" src="{c["file"]}"></video><figcaption>{c["file"]} · '
            f'{f"<a href=\"https://youtu.be/{info["id"]}?t={int(c["start"])}\">" if yt else ""}'
            f'{hms(c["start"])}–{hms(c["end"])}{"</a>" if yt else ""} · skóre {c["score"]:.1f}</figcaption></figure>'
            for c in data["clips"]]
    t, ch, url_ch = html.escape(info["title"]), html.escape(info["channel,uploader"]), info["channel_url,uploader_url"]
    who = f'<a href="{html.escape(url_ch)}">{ch}</a>' if url_ch.startswith("http") else ch
    (folder / "index.html").write_text(
        f'<!doctype html><meta charset="utf-8"><title>{ch} – {t}</title><style>body{{font-family:sans-serif;display:grid;'
        f'grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:1rem;margin:1rem}}header{{grid-column:1/-1}}'
        f'video{{width:100%}}</style><header><p><a href="../../index.html">← všechny streamy</a></p><h1>{t}</h1>'
        f'<p>Streamer: <b>{who}</b> · {html.escape(info["upload_date>%d. %m. %Y"])} · '
        f'<a href="{html.escape(info["webpage_url"])}">původní {"video na YouTube" if yt else "stream"}</a></p>'
        f'</header>{"".join(figs) or f"<p>Žádný moment nepřekročil skóre {data["min_score"]}.</p>"}', encoding="utf-8")
    write_overview(folder.parent.parent)


def selftest():
    import tempfile
    assert pick_peaks([0, 5, 4, 0, 9, 0], 3, min_score=0) == [1, 4]
    assert pick_peaks([0, 5, 4, 0, 9, 0], 3, min_score=6) == [4], "pod limitem se nestříhá"
    assert pick_peaks([9, 1, 2, 9], 2, margin=1, min_score=0) == [2], "okraje streamu se přeskakují"
    assert len(pick_peaks([5.0] * 1000, 10, min_score=1)) == 100, "počet klipů není omezený"
    assert zscore([3, 3, 3]) == [0, 0, 0]
    # tichý mikrofon -> hlasité video (skok v 100 s) + krátký smích v 50 s: vyhrát musí smích
    loud = [-30.0] * 100 + [-15.0] * 100
    loud[50:53] = [-12.0] * 3
    b = burst(loud, -2, 3)
    assert 48 <= max(range(len(b)), key=b.__getitem__) <= 53, "skok scény nesmí porazit smích"
    # 2 min křiku (200-320 s) musí vyjít vysoko, trvalé zesílení od 500 s ne
    long_loud = [-30.0] * 200 + [-15.0] * 120 + [-30.0] * 180 + [-15.0] * 200
    sc = score_of(long_loud, [0.0] * len(long_loud))
    assert max(sc[195:210]) > max(sc[495:510]) + 1, "dlouhá vzrušená pasáž > trvalý skok"
    # klip kolem 110 s: černá obrazovka 97-100 s -> začne až po ní; konec v pauze mezi slovy
    loud10 = [v for v in loud for _ in range(10)]
    loud10[970:1000] = [-90.0] * 30
    loud10[1050:1055] = [-60.0] * 5                         # krátká pauza v řeči, není to ticho
    loud10[1195] = -50.0                                    # mezera mezi slovy v 119,5 s
    sil = silences(loud10)
    assert sil == [(97.0, 100.0)], sil
    start, end = clip_range(loud10, sil, [0.0] * 200, [], 110)
    assert start == 100 and end == 119.5, (start, end)
    # střih scény 15 s před momentem -> začátek na něm; reakce trvá do 130 s -> konec až po ní
    plain10 = [-15.0] * 2000
    plain10[1315] = -40.0
    start, end = clip_range(plain10, [], [3.0] * 130 + [0.0] * 70, [95.0, 160.0], 110)
    assert abs(start - 95.3) < 1e-9 and end == 131.5, (start, end)
    start, end = clip_range(plain10, [], [0.0] * 200, [95.0, 121.0], 110)
    assert abs(end - 120.7) < 1e-9, "konec na střihu scény poblíž"

    def msg(ms, author, *runs):
        item = {"message": {"runs": list(runs)}} | ({"authorExternalChannelId": author} if author else {})
        return json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(ms), "actions": [
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": item}}}]}})
    kekw = {"emoji": {"shortcuts": [":KEKW:"]}}
    lines = [msg(2500, "a", {"text": "to je "}, kekw), "", msg(-500, "a", kekw), msg(3000, None, {"text": "systém"})]
    lines += [msg(20000 + i * 100, "spammer", kekw) for i in range(20)]           # 1 člověk spamuje
    lines += [msg(40000 + i * 1000, who, kekw) for i, who in enumerate("bcd")]    # 3 různí lidé
    lines += [kick_line({"content": "[emote:37226:KEKW]", "sender": {"id": i}}, 45.5) for i in range(3)]  # Kick
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    chat = load_chat(Path(f.name), 50)
    Path(f.name).unlink()
    assert chat[2] == 1, "jeden KEKW = bez bonusu"
    assert sum(chat[20:23]) == 1, "spam jednoho člověka = 1 zpráva"
    assert chat[40:43] == [1 + HYPE_WEIGHT] * 3, chat[40:43]
    assert chat[45] == 3 * (1 + HYPE_WEIGHT), "Kick zprávy se čtou stejně jako YouTube"
    assert sum(chat) == 1 + 1 + 6 * (1 + HYPE_WEIGHT), "systémová hláška a čas < 0 se nepočítají"
    with tempfile.TemporaryDirectory() as d:  # nedopsaný živák (.part) se dá zachránit a stříhat
        final = Path(d) / "stream.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=2:s=64x64", "-f", "mpegts",
                        f"{final}.part"], check=True)
        salvage(final)
        assert final.exists() and not Path(f"{final}.part").exists(), "salvage"
    # časovač: .stop s časem za 1 s -> watch_stop počká a pak nahrávání ukončí (jako Ctrl+C)
    JOBS.mkdir(parents=True, exist_ok=True)
    flag = JOBS / f"{os.getpid()}.stop"
    assert stop_time(flag) is None
    flag.write_text("", encoding="utf-8")
    assert stop_time(flag) == 0.0, "prázdný .stop = hned"
    flag.write_text(str(time.time() + 1), encoding="utf-8")
    rec = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True,
                           stderr=subprocess.DEVNULL)
    t0 = time.time()
    watch_stop(rec)
    assert rec.wait(timeout=5) != 0 and 0.9 < time.time() - t0 < 4 and not flag.exists(), "časovač"
    assert KICK_LIVE.fullmatch("https://kick.com/lukyonair1") and not KICK_LIVE.fullmatch("https://kick.com/a/videos/x")
    print("ok")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):  # Windows by do logu psal v cp1250 a spadl na emoji / cizím písmu
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if sys.argv[1:] == ["--test"]:
        selftest()
    elif 2 <= len(sys.argv) <= 4:
        main(sys.argv[1], float(sys.argv[2].replace(",", ".")) if len(sys.argv) >= 3 else MIN_SCORE,
             int(sys.argv[3]) if len(sys.argv) == 4 else 0)
    else:
        sys.exit("Použití: python Klipy.py <youtube-url | https://kick.com/<kanál> | složka streamu> [limit skóre]"
                 " [minuty nahrávání živáku]")
