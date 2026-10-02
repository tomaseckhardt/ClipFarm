#!/usr/bin/env python3
"""ClipFarm: python Klipy.py <url>  ->  klipy/<kanál>/<datum – název>/01.mp4 ... + index.html
(složka klipy/ vždy vedle Klipy.py, video max. 720p). Přehled všech streamů: klipy/index.html

<url> = YouTube video/VOD, nebo živý Kick kanál (https://kick.com/<kanál>): nahrává video i chat
do konce streamu, Ctrl+C = ukončit nahrávání a hned nastříhat, co je nahrané.

Potřebuje: yt-dlp (pip install yt-dlp), ffmpeg.  Self-check: python Klipy.py --test
"""
import base64
import html
import json
import os
import re
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

TOP_N = 10                 # kolik klipů
GAP = 120                  # min. rozestup mezi klipy (s)
BEFORE, AFTER = 22, 12     # klip = moment -22 s / +12 s, pak se zkrátí max. o SNAP na ticho mezi slovy
SNAP = 2.0                 # o kolik s se smí začátek/konec posunout dovnitř, aby nestříhal uprostřed slova
SILENCE_DB, SILENCE_LEN = -55, 1.5  # ticho = černá obrazovka / pauza / přechod scény, klip přes něj nejde
CHAT_WINDOW = 10           # chat reaguje se zpožděním: počítá se 10 s PO momentu
AROUND = 30                # s okolí, se kterým se moment porovnává (před i po)
W_CHAT, W_AUDIO = 1.0, 1.0
HYPE = re.compile(r"kekw|omegalul|lul|lmao|pog|xd+|wtf|haha|\?{3,}|!{3,}|😂|🤣|💀", re.IGNORECASE)
HYPE_WEIGHT = 2            # zpráva s emotem/"xddd" = 1 + 2 ...
HYPE_AUTHORS = 3           # ... ale jen když je během ~10 s píšou aspoň 3 různí lidé


def download(url):
    """Stáhne video (+ YouTube chat). Živý stream nahrává do konce nebo do Ctrl+C,
    u Kicku mezitím zapisuje chat. Vrací (info, cesta k videu)."""
    kick = KICK_LIVE.fullmatch(url)
    when = time.strftime(" %H.%M") if kick else ""  # živák: čas začátku nahrávání, ať další běh nic nepřepíše
    proc = subprocess.Popen(
        ["yt-dlp", "--js-runtimes", "node", "--no-playlist", "--progress", "-f", "bv*+ba/b", "-S", "res:720",
         "--merge-output-format", "mp4", "--write-subs", "--sub-langs", "live_chat", "-P", str(ROOT),
         "-o", f"%(channel,uploader)s/%(upload_date>%Y-%m-%d)s{when} – %(title).60s/stream.%(ext)s",
         "--print", "before_dl:START\t%(filename)s", "--print", "after_move:DONE\t" + "\t".join(f"%({k})s" for k in INFO),
         url],
        stdout=subprocess.PIPE, text=True)
    stop, done = threading.Event(), ""

    def read():
        nonlocal done
        for line in proc.stdout or ():  # kromě našich řádků tu může být i průběh stahování
            tag, _, rest = line.rstrip("\n").partition("\t")
            if tag == "DONE":
                done = rest
            elif tag == "START" and kick:  # nahrávání začíná právě teď
                chat = Path(rest).with_suffix(".live_chat.json")
                threading.Thread(target=kick_chat, args=(kick[1], chat, time.time(), stop), daemon=True).start()

    try:
        read()
    except KeyboardInterrupt:
        print("\n⏹  Končím nahrávání a stříhám to, co je nahrané…", file=sys.stderr)
        read()  # Ctrl+C dostal i yt-dlp: dopíše soubor a vypíše cestu
    stop.set()
    proc.wait()
    if not done:
        sys.exit("yt-dlp skončil bez videa")
    info = dict(zip(INFO, done.split("\t")))
    return info, Path(info["filepath"])


INFO = ("id", "title", "channel,uploader", "channel_url,uploader_url", "upload_date>%d. %m. %Y", "webpage_url",
        "extractor_key", "filepath")
ROOT = Path(__file__).resolve().parent / "klipy"  # klipy se ukládají vedle Klipy.py, ať je kdekoli
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
                         check=True, stdout=subprocess.PIPE, text=True).stdout
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


def burst(xs, a, b, around=AROUND):
    """Pro každé s: průměr xs v [s+a, s+b) minus vyšší z medianů okolí před a po.
    Krátký výkyv (smích, křik, spam v chatu) vyjde vysoko. Trvalý skok (přepnutí scény,
    konec pauzy) ne, protože strana po skoku je stejně hlasitá."""
    out = []
    for s in range(len(xs)):
        sides = [statistics.median(w) for w in (win(xs, s + a - around, s + a), win(xs, s + b, s + b + around)) if w]
        out.append(statistics.fmean(win(xs, s + a, s + b) or [0.0]) - (max(sides) if sides else 0.0))
    return out


def zscore(xs):
    mu, sd = statistics.fmean(xs), statistics.pstdev(xs) or 1
    return [(x - mu) / sd for x in xs]


def pick_peaks(score, n, gap, margin=0):
    picked = []
    for s in sorted(range(margin, len(score) - margin), key=score.__getitem__, reverse=True):
        if all(abs(s - p) >= gap for p in picked):
            picked.append(s)
            if len(picked) == n:
                break
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


def clip_range(loud10, sil, s):
    """Začátek a konec klipu kolem momentu s: bez ticha uvnitř, střih v pauze mezi slovy."""
    start, end = max(s - BEFORE, 0), min(s + AFTER, len(loud10) / 10)
    for a, b in sil:
        if start < b <= s:
            start = b
        if s <= a < end:
            end = a
    # ponytail: přechod scény pozná jen podle ticha; prolínačka bez ztišení (video->kamera) projde
    return quietest(loud10, start, start + SNAP), quietest(loud10, end - SNAP, end)


def hms(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def write_overview(root):
    """root/index.html: všechny streamy podle streamera, nejnovější nahoře."""
    parts = []
    for ch in sorted(d for d in root.iterdir() if d.is_dir()):
        streams = sorted((d for d in ch.iterdir() if (d / "index.html").exists()), reverse=True)
        items = "".join(f'<li><a href="{quote(f"{ch.name}/{d.name}/index.html")}">{html.escape(d.name)}</a>'
                        f' · {len(list(d.glob("[0-9][0-9].mp4")))} klipů</li>' for d in streams)
        if items:
            parts.append(f"<h2>{html.escape(ch.name)}</h2><ul>{items}</ul>")
    (root / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>ClipFarm</title><style>body{font-family:sans-serif;margin:1rem}'
        f'li{{margin:.3rem 0}}</style><h1>ClipFarm</h1>{"".join(parts)}', encoding="utf-8")


def main(url):
    info, video = download(url)
    vid, title, channel = info["id"], info["title"], info["channel,uploader"]
    yt = info["extractor_key"].startswith("Youtube")  # odkaz na čas v klipu umí jen YouTube
    loud10 = load_audio(video)
    loud = [statistics.fmean(loud10[i:i + 10]) for i in range(0, len(loud10) - 9, 10)]
    sil = silences(loud10)
    chat = load_chat(video.with_suffix(".live_chat.json"), len(loud))
    score = [W_CHAT * c + W_AUDIO * a for c, a in
             zip(zscore(burst(chat, 0, CHAT_WINDOW)), zscore(burst(loud, -2, 3)))]

    figs = []
    for i, s in enumerate(pick_peaks(score, TOP_N, GAP, AROUND), 1):
        start, end = clip_range(loud10, sil, s)
        clip = f"{i:02d}.mp4"
        print(f"{clip}  {hms(start)}–{hms(end)} ({end - start:.1f} s)  moment {hms(s)}  skóre {score[s]:.1f}")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.1f}", "-i", str(video), "-t", f"{end - start:.1f}",
                        "-c:v", "h264", "-b:v", "6M", "-c:a", "aac", "-movflags", "+faststart",
                        str(video.with_name(clip))], check=True)
        figs.append(f'<figure><video controls preload="metadata" src="{clip}"></video><figcaption>{clip} · '
                    f'{f"<a href=\"https://youtu.be/{vid}?t={int(start)}\">" if yt else ""}{hms(start)}–{hms(end)}'
                    f'{"</a>" if yt else ""} · '
                    f'skóre {score[s]:.1f}</figcaption></figure>')

    t, ch, url_ch = html.escape(title), html.escape(channel), info["channel_url,uploader_url"]
    who = f'<a href="{html.escape(url_ch)}">{ch}</a>' if url_ch.startswith("http") else ch
    (video.parent / "index.html").write_text(
        f'<!doctype html><meta charset="utf-8"><title>{ch} – {t}</title><style>body{{font-family:sans-serif;display:grid;'
        f'grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:1rem;margin:1rem}}header{{grid-column:1/-1}}'
        f'video{{width:100%}}</style><header><p><a href="../../index.html">← všechny streamy</a></p><h1>{t}</h1>'
        f'<p>Streamer: <b>{who}</b> · {html.escape(info["upload_date>%d. %m. %Y"])} · '
        f'<a href="{html.escape(info["webpage_url"])}">původní {"video na YouTube" if yt else "stream"}</a></p>'
        f'</header>{"".join(figs)}', encoding="utf-8")
    write_overview(video.parents[2])
    print(f"Hotovo: {channel} – {title}\n  {video.parent / 'index.html'}\n  přehled: {video.parents[2] / 'index.html'}")


def selftest():
    import tempfile
    assert pick_peaks([0, 5, 4, 0, 9, 0], 2, 3) == [1, 4]
    assert zscore([3, 3, 3]) == [0, 0, 0]
    # tichý mikrofon -> hlasité video (skok v 100 s) + krátký smích v 50 s: vyhrát musí smích
    loud = [-30.0] * 100 + [-15.0] * 100
    loud[50:53] = [-12.0] * 3
    b = burst(loud, -2, 3)
    assert 48 <= max(range(len(b)), key=b.__getitem__) <= 53, "skok scény nesmí porazit smích"
    assert pick_peaks([9, 1, 2, 9], 1, 1, margin=1) == [2]
    # klip kolem 110 s: černá obrazovka 97-100 s -> začne až po ní; konec v pauze mezi slovy
    loud10 = [v for v in loud for _ in range(10)]
    loud10[970:1000] = [-90.0] * 30
    loud10[1050:1055] = [-60.0] * 5                         # krátká pauza v řeči, není to ticho
    loud10[1205] = -50.0                                    # mezera mezi slovy v 120,5 s
    sil = silences(loud10)
    assert sil == [(97.0, 100.0)], sil
    start, end = clip_range(loud10, sil, 110)
    assert start == 100 and end == 120.5, (start, end)

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
    assert KICK_LIVE.fullmatch("https://kick.com/lukyonair1") and not KICK_LIVE.fullmatch("https://kick.com/a/videos/x")
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        selftest()
    elif len(sys.argv) == 2:
        main(sys.argv[1])
    else:
        sys.exit("Použití: python Klipy.py <youtube-url | https://kick.com/<kanál>>")
