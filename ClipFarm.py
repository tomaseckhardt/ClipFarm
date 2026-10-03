#!/usr/bin/env python3
"""ClipFarm okno (Linux, Windows, macOS): všechno kolem Klipy.py bez terminálu a prohlížeče.

Karta Běží: vložit odkaz -> Spustit, přehled běžících nahrávání, Ukončit a nastříhat, Stop přestříhání, log.
Karta Klipy: streamy podle streamera, přehrát klip, přestříhat stream, smazat klip / zdrojové video / stream.

Spuštění: python3 ClipFarm.py (Windows: dvojklik). Zároveň vytvoří/aktualizuje ikonu: Linux v nabídce aplikací
GNOME, Windows zástupce na ploše a v nabídce Start, macOS ~/Applications/ClipFarm.app (Launchpad, Dock).
Po přesunutí složky ho spusť jednou ručně, ať ikona ukazuje na nové místo.  Self-check: python3 ClipFarm.py --test
"""
import contextlib
import http.server
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import font, messagebox, ttk
from urllib.parse import quote

import Klipy

HERE = Path(__file__).resolve().parent
TIMES = ("dokud neskončí", "30 min", "1 h", "2 h", "3 h", "5 h")  # nabídka časovače, jde napsat i vlastní


def install_launcher():
    """Ikona ClipFarm s cestou na tenhle soubor (ať je kdekoli)."""
    script = Path(__file__).resolve()
    if sys.platform == "win32":  # zástupce na plochu a do Start (pythonw = bez černého okna)
        q = lambda p: str(p).replace("'", "''")  # cesta jako řetězec pro PowerShell
        ps = ("$w = New-Object -ComObject WScript.Shell; "
              "foreach ($d in [Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop')) {"
              " $s = $w.CreateShortcut((Join-Path $d 'ClipFarm.lnk'));"
              f" $s.TargetPath = '{q(Path(sys.executable).with_name('pythonw.exe'))}';"
              f" $s.Arguments = '\"{q(script)}\"'; $s.WorkingDirectory = '{q(HERE)}'; $s.Save() }}")
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps], creationflags=subprocess.CREATE_NO_WINDOW)
        return
    if sys.platform == "darwin":  # minimální .app: z Launchpadu/Docku nemá PATH Homebrew, tak si ho přidá sám
        app = Path.home() / "Applications/ClipFarm.app/Contents"
        (app / "MacOS").mkdir(parents=True, exist_ok=True)
        run = app / "MacOS/ClipFarm"
        run.write_text('#!/bin/sh\nexport PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"\n'
                       f"exec {shlex.quote(sys.executable)} {shlex.quote(str(script))}\n", encoding="utf-8")
        run.chmod(0o755)
        with (app / "Info.plist").open("wb") as f:
            plistlib.dump({"CFBundleName": "ClipFarm", "CFBundleExecutable": "ClipFarm",
                           "CFBundleIdentifier": "cz.clipfarm", "CFBundlePackageType": "APPL"}, f)
        return
    launcher = Path.home() / ".local/share/applications/clipfarm.desktop"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("[Desktop Entry]\nType=Application\nName=ClipFarm\nComment=Automatické klipy ze streamů\n"
                        f'Exec=python3 "{script}"\nIcon=camera-video\nTerminal=false\nCategories=AudioVideo;\n',
                        encoding="utf-8")


def login_path():
    """Appka z nabídky/Launchpadu nemá PATH z profilu shellu (nvm, Homebrew) -> nenajde node / yt-dlp / ffmpeg."""
    if sys.platform != "win32" and not (shutil.which("node") and shutil.which("yt-dlp")):
        shell = os.environ.get("SHELL") or ("/bin/zsh" if sys.platform == "darwin" else "/bin/bash")
        path = subprocess.run([shell, "-lc", "echo $PATH"], capture_output=True, text=True, check=False).stdout.strip()
        if path:
            os.environ["PATH"] = path


def open_path(path):
    """Otevře soubor/složku ve výchozí aplikaci systému."""
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def alive(pid):
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def jobs():
    """{pid: stav} všech běžících Klipy.py (i z terminálu) podle klipy/_logy/<pid>.json, po skončených uklidí."""
    out = {}
    for f in Klipy.JOBS.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # zrovna se zapisuje
            continue
        if alive(data["pid"]):
            out[data["pid"]] = data
        else:  # proces skončil natvrdo (pád, vypnutí)
            f.unlink(missing_ok=True)
            f.with_suffix(".stop").unlink(missing_ok=True)
    # ponytail: po restartu PC může stejné PID dostat jiný proces -> fantom v seznamu, dokud neskončí
    return out


def kill(pid, job):
    """Zastaví běh natvrdo i s jeho yt-dlp a ffmpeg, bez stříhání. Hotové klipy zůstanou, rozpracované se zahodí."""
    if sys.platform == "win32":  # /T = celý strom procesů
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        return
    for group in (pid, job.get("ytdlp")):  # Klipy.py a jeho ffmpeg mají jednu skupinu, yt-dlp vlastní
        if group:
            try:
                os.killpg(group, signal.SIGTERM)
            except OSError:  # už skončil, nebo spuštěno z terminálu (není vedoucí skupiny)
                with contextlib.suppress(OSError):
                    os.kill(group, signal.SIGTERM)


def set_stop(pid, mins=None):
    """Kdy má Klipy.py ukončit nahrávání a nastříhat (soubor _logy/<pid>.stop): None hned, 0 dokud neskončí."""
    flag = Klipy.JOBS / f"{pid}.stop"
    if mins == 0:
        flag.unlink(missing_ok=True)
    else:
        flag.write_text("" if mins is None else str(time.time() + mins * 60), encoding="utf-8")


def minutes(text):
    """Časovač z textu: „dokud neskončí“ -> 0, „90“ / „90 min“ / „2 h“ / „1 h 30 min“ -> minuty."""
    t = text.strip().lower()
    if not t or t.startswith("dokud"):
        return 0
    m = re.fullmatch(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*(?:min|m)?)?", t)
    if not m or not any(m.groups()):
        raise ValueError(text)
    return int(m[1] or 0) * 60 + int(m[2] or 0)


def end_text(pid, target):
    """Odpočet do konce nahrávání (H:MM:SS) pro sloupec Do konce."""
    at = Klipy.stop_time(Klipy.JOBS / f"{pid}.stop")
    if not Klipy.KICK_LIVE.fullmatch(target):
        return "–"
    if at is None:
        return "dokud neskončí"
    if at <= time.time():
        return "ukončuje se…"
    return Klipy.hms(at - time.time() + 0.999)  # zaokrouhlit nahoru: 0:00:00 až ve chvíli konce


def tail(path, n=12):
    """Posledních n neprázdných řádků logu (yt-dlp přepisuje průběh přes \\r)."""
    try:
        with open(path, "rb") as f:
            f.seek(max(f.seek(0, 2) - 8192, 0))
            text = f.read().decode("utf-8", errors="replace")
    except (OSError, TypeError):
        return []
    return [l for l in text.replace("\r", "\n").splitlines() if l.strip()][-n:]


VIDEO = (".mp4", ".mkv", ".webm", ".mov", ".part")


def streams():
    """[(kanál, složka, info, klipy, ostatní videa, nejvyšší skóre)] všech složek s nějakým videem, nejnovější nahoře: i nedokončené,
    rozpracované nebo bez klipů. klipy = [{file, start, end, score}], ostatní = zdrojové video (stream.mp4 první) apod."""
    out = []
    for folder in Klipy.ROOT.glob("*/*/"):
        videos = sorted((f.name for f in folder.iterdir() if f.suffix.lower() in VIDEO),
                        key=lambda n: (not n.startswith("stream."), n))
        if not videos:
            continue
        data = Klipy.load_json(folder / "klipy.json", {"clips": []})
        clips = [c for c in data["clips"] if c["file"] in videos]
        named = {c["file"] for c in clips}
        out.append((folder.parent.name, folder, Klipy.load_json(folder / "info.json", {}), clips,
                    [v for v in videos if v not in named], data.get("max")))
    return sorted(out, key=lambda x: (x[0].lower(), [-ord(ch) for ch in x[1].name]))


def delete_clip(folder, name):
    """Smaže klip a přegeneruje stránku streamu."""
    (folder / name).unlink(missing_ok=True)
    data_file = folder / "klipy.json"
    if data_file.exists():  # jinak to nebyl klip (jiné video ve složce)
        data = json.loads(data_file.read_text(encoding="utf-8"))
        data["clips"] = [c for c in data["clips"] if c["file"] != name]
        data_file.write_text(json.dumps(data, indent=1), encoding="utf-8")
        Klipy.write_page(folder)


def delete_stream(folder):
    """Smaže celý stream (i prázdnou složku streamera) a přegeneruje přehled."""
    shutil.rmtree(folder)
    if not any(folder.parent.iterdir()):
        folder.parent.rmdir()
    Klipy.write_overview(folder.parent.parent)


class ClipHandler(http.server.SimpleHTTPRequestHandler):
    """Soubory z klipy/ přes http://127.0.0.1, ať prohlížeč nepotřebuje přístup k disku (Flatpak, přesunutá
    složka, Windows). Umí Range, takže jde ve videu přetáčet."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Klipy.ROOT), **kwargs)

    def log_message(self, format, *args):  # pythonw na Windows nemá kam psát
        pass

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self):
        self.remaining = None
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        path = Path(self.translate_path(self.path))
        if not rng or not path.is_file() or rng.groups() == ("", ""):
            return super().send_head()
        total, (a, b) = path.stat().st_size, rng.groups()
        start = int(a) if a else max(total - int(b), 0)  # "bytes=-500" = posledních 500
        end = min(int(b), total - 1) if a and b else total - 1
        if start >= total:
            self.send_error(416)
            return None
        f = path.open("rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        left = self.remaining
        if left is None:
            return super().copyfile(source, outputfile)
        while left > 0 and (chunk := source.read(min(left, 1 << 16))):
            outputfile.write(chunk)
            left -= len(chunk)
        return None


class ClipServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # prohlížeč utnul spojení (přetáčení videa) apod.
        pass


def serve(port=8765):
    """Spustí server na 127.0.0.1 (jen pro tenhle počítač), stálý port, když je volný. Vrací adresu."""
    try:
        server = ClipServer(("127.0.0.1", port), ClipHandler)
    except OSError:  # port obsazený (třeba druhé okno ClipFarm) -> libovolný volný
        server = ClipServer(("127.0.0.1", 0), ClipHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def size(path):
    mb = path.stat().st_size / 1e6 if path.exists() else 0
    return f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb:.0f} MB"


def launch(target, limit, awake, mins=0, vertical=False, rescore=False):
    """Spustí Klipy.py na pozadí (přežije zavření okna) s logem v klipy/_logy/."""
    Klipy.JOBS.mkdir(parents=True, exist_ok=True)
    log = Klipy.JOBS / f"{time.strftime('%Y-%m-%d %H.%M.%S')}.log"
    env = os.environ | {"CLIPFARM_LOG": str(log), "CLIPFARM_AWAKE": "1" if awake else "0",
                        "CLIPFARM_VERTICAL": "1" if vertical else "0"}
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":  # okno běží bez konzole, Klipy.py potřebuje normální python
        python = python.with_name("python.exe")
    with log.open("w", encoding="utf-8") as out:
        args = ["--skore", target] if rescore else [target, str(limit), str(mins)]
        return subprocess.Popen([str(python), "-u", Klipy.__file__, *args], cwd=HERE, env=env, stdout=out,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def columns(tree, cols):
    """Sloupce stromu: (id, nadpis, ukázka nejdelšího obsahu nebo None = zbytek místa). Šířka podle písma,
    takže sedí při jakémkoli zvětšení (Linux HiDPI, Windows 125 %+)."""
    body, head = font.nametofont("TkDefaultFont").measure, font.nametofont("TkHeadingFont").measure
    for col, label, sample in cols:
        tree.heading(col, text=label, anchor="w")
        tree.column(col, width=max(body(sample), head(label)) + 24 if sample else 300, stretch=sample is None)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ClipFarm")
        self.geometry("1200x760")
        self.procs = []  # (Popen, cíl, přepočet skóre?) spuštěné z okna
        self.base = ""  # adresa webového serveru pro „V prohlížeči“, spustí se při prvním použití

        top = ttk.Frame(self, padding=(10, 10, 10, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Odkaz (YouTube video nebo kick.com/kanál):").grid(row=0, column=0, sticky="w")
        self.url = ttk.Entry(top)
        self.url.grid(row=1, column=0, sticky="ew")
        self.url.bind("<Return>", lambda _: self.start(self.url.get().strip()))
        ttk.Button(top, text="▶ Spustit", command=lambda: self.start(self.url.get().strip())).grid(row=1, column=1, padx=(8, 0))
        top.columnconfigure(0, weight=1)
        opts = ttk.Frame(self, padding=(10, 6, 10, 0))  # nastavení pro spuštění i přestříhání
        opts.pack(fill="x")
        ttk.Label(opts, text="Limit skóre:").pack(side="left")
        self.limit = tk.StringVar(value=str(Klipy.MIN_SCORE))
        ttk.Spinbox(opts, textvariable=self.limit, from_=0.5, to=10, increment=0.5, width=6).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="Nahrávat živák:").pack(side="left")
        self.timer = tk.StringVar(value=TIMES[0])
        ttk.Combobox(opts, textvariable=self.timer, values=TIMES, width=15).pack(side="left", padx=(4, 16))
        self.vertical = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Klipy na výšku 9:16", variable=self.vertical).pack(side="left")

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=10)
        self.tabs.add(self.build_jobs(), text="  Běží  ")
        self.tabs.add(self.build_clips(), text="  Klipy  ")
        self.tabs.bind("<<NotebookTabChanged>>", lambda _: self.fill_clips())

        self.status = ttk.Label(self, text="", padding=(10, 0, 10, 8))
        self.status.pack(fill="x")
        self.fill_clips()
        self.refresh()

    # --- karta Běží ---
    def build_jobs(self):
        tab = ttk.Frame(self.tabs, padding=8)
        self.jobs = ttk.Treeview(tab, columns=("target", "time", "end", "state"), show="headings", height=6)
        columns(self.jobs, (("target", "Stream", "https://kick.com/lukyonair1"), ("time", "Běží", "10:00:00"),
                            ("end", "Do konce", "dokud neskončí"), ("state", "Stav", None)))
        self.jobs.pack(fill="x")
        self.jobs.bind("<<TreeviewSelect>>", lambda _: self.show_log())
        bar = ttk.Frame(tab, padding=(0, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="⏹ Ukončit a nastříhat", command=self.finish).pack(side="left")
        ttk.Label(bar, text="Změnit konec:").pack(side="left", padx=(16, 4))
        self.new_end = tk.StringVar(value=TIMES[2])
        ttk.Combobox(bar, textvariable=self.new_end, values=TIMES, width=15).pack(side="left")
        ttk.Button(bar, text="⏱ Nastavit", command=self.set_end).pack(side="left", padx=(4, 0))
        self.stop_btn = ttk.Button(bar, text="■ Stop", command=self.stop_reclip)  # vidět jen u vybraného přestříhání
        self.awake = tk.BooleanVar(value=True)
        lid = " (ani po zaklapnutí víka)" if sys.platform.startswith("linux") else ""
        ttk.Checkbutton(tab, text=f"Neuspávat počítač, dokud něco běží{lid}", variable=self.awake).pack(anchor="w", pady=(0, 8))
        self.log = tk.Text(tab, height=12, wrap="none", state="disabled", font="TkFixedFont")
        self.log.pack(fill="both", expand=True)
        return tab

    def start(self, target):
        if not (target.startswith("http") or Path(target).is_dir()):
            messagebox.showerror("ClipFarm", "Vlož odkaz začínající http(s)://")
            return
        try:
            limit = float(self.limit.get().replace(",", "."))
        except ValueError:
            messagebox.showerror("ClipFarm", "Limit skóre musí být číslo, třeba 2.5")
            return
        try:
            mins = minutes(self.timer.get()) if Klipy.KICK_LIVE.fullmatch(target) else 0  # časovač jen u živáku
        except ValueError:
            messagebox.showerror("ClipFarm", "Délku nahrávání napiš třeba jako 45 min, 2 h nebo 1 h 30 min.")
            return
        self.procs.append((launch(target, limit, self.awake.get(), mins, self.vertical.get()), target, False))
        if target.startswith("http"):
            self.url.delete(0, "end")
        self.tabs.select(0)
        self.status.configure(text="Spouštím…")

    def selected_job(self, running=None):
        """(pid, stav) vybrané úlohy v kartě Běží; stav None, když nic vybraného neběží."""
        sel = self.jobs.selection()
        pid = int(sel[0]) if sel else None
        return pid, (running if running is not None else jobs()).get(pid)

    def finish(self):
        pid, job = self.selected_job()
        if job is None:
            messagebox.showinfo("ClipFarm", "Vyber v seznamu nahrávání, které chceš ukončit.")
        elif not Klipy.KICK_LIVE.fullmatch(job.get("target", "")):
            messagebox.showinfo("ClipFarm", "Ukončit a nastříhat jde u nahrávání živého streamu z Kicku.\n"
                                            "Přestříhání zastavíš tlačítkem Stop.")
        elif messagebox.askyesno("ClipFarm", "Ukončit nahrávání a nastříhat, co je nahrané?"):
            set_stop(pid)
            self.status.configure(text="Ukončuji nahrávání, pak se nastříhají klipy…")

    def stop_reclip(self):
        pid, job = self.selected_job()
        if job and messagebox.askyesno("ClipFarm", f"Zastavit přestříhání „{Path(job['target']).name}“?\n"
                                                   "Současné klipy zůstanou, nové se zahodí."):
            kill(pid, job)
            self.status.configure(text=f"■ Přestříhání zastaveno: {Path(job['target']).name}")

    def set_end(self):
        pid, job = self.selected_job()
        if job is None:
            messagebox.showinfo("ClipFarm", "Vyber v seznamu nahrávání, kterému chceš změnit konec.")
        elif not Klipy.KICK_LIVE.fullmatch(job.get("target", "")):
            messagebox.showinfo("ClipFarm", "Konec jde nastavit jen u živého streamu z Kicku.")
        else:
            try:
                set_stop(pid, minutes(self.new_end.get()))
            except ValueError:
                messagebox.showerror("ClipFarm", "Délku napiš třeba jako 45 min, 2 h nebo 1 h 30 min.")
                return
            self.status.configure(text=f"Do konce nahrávání: {end_text(pid, job['target'])}")

    def show_log(self, running=None):
        _, job = self.selected_job(running)
        reclip = job is not None and not job.get("target", "http").startswith("http")  # cíl = složka, ne odkaz
        if reclip and not self.stop_btn.winfo_ismapped():  # Stop jen u vybraného přestříhání
            self.stop_btn.pack(side="left", padx=(16, 0))
        elif not reclip and self.stop_btn.winfo_ismapped():
            self.stop_btn.pack_forget()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(tail(job.get("log")) if job else []))
        self.log.see("end")
        self.log.xview_moveto(0)  # dlouhé řádky (varování yt-dlp) ukázat od začátku, ne od konce
        self.log.configure(state="disabled")

    def refresh(self):
        running = jobs()
        finished = [x for x in self.procs if x[0].poll() is not None]
        for p, target, rescored in finished:
            self.procs.remove((p, target, rescored))
            name = target if target.startswith("http") else Path(target).name
            self.status.configure(text=f"✔ Hotovo: {name}" if p.returncode == 0 else
                                  f"✖ Skončilo chybou ({p.returncode}): {name}, podrobnosti v klipy/_logy/")
            if rescored and p.returncode == 0:
                self.show_preview(Path(target))
        if finished:
            self.fill_clips()
        for iid in set(self.jobs.get_children()) - {str(p) for p in running}:
            self.jobs.delete(iid)
        for pid, job in running.items():
            part = Path(job["part"]) if job.get("part") else None
            if part and part.exists():
                state = f"⏺ nahrávám · {size(part)}"
            elif job.get("log"):
                state = (tail(job["log"], 1) or ["…"])[0]
            else:
                state = "(spuštěno z terminálu)"
            target = job.get("target", "")
            icon = "Σ " if job.get("kind") == "skore" else "✂ "  # přepočet skóre / přestříhání
            row = (target if target.startswith("http") else icon + Path(target).name,
                   Klipy.hms(time.time() - job.get("started", time.time())), end_text(pid, target), state)
            if self.jobs.exists(str(pid)):
                self.jobs.item(str(pid), values=row)
            else:
                self.jobs.insert("", "end", iid=str(pid), values=row)
        self.show_log(running)
        self.after(1000, self.refresh)  # po sekundě, ať odpočet Do konce plynule běží

    # --- karta Klipy ---
    def build_clips(self):
        tab = ttk.Frame(self.tabs, padding=8)
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 8))
        for text, cmd in (("▶ Přehrát", self.play), ("✂ Přestříhat", self.reclip), ("Σ Přepočítat skóre", self.rescore),
                          ("Složka", self.open_folder),
                          ("V prohlížeči", self.open_browser), ("Smazat zdroj", self.delete_source),
                          ("Smazat", self.delete)):
            ttk.Button(bar, text=text, command=cmd, width=-4).pack(side="left", padx=(0, 6))  # šířka podle textu
        self.clips = ttk.Treeview(tab, columns=("when", "length", "score", "source"), show="tree headings")
        columns(self.clips, (("#0", "Stream / klip", None), ("when", "Kdy", "10:00:00–10:00:00"),
                             ("length", "Délka", "100 klipů"), ("score", "Skóre", "max 10.0"), ("source", "Zdrojové video", "10.0 GB")))
        scroll = ttk.Scrollbar(tab, command=self.clips.yview)
        self.clips.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.clips.pack(fill="both", expand=True)
        self.clips.bind("<Double-1>", lambda _: self.play() if self.kind() == "clip" else None)
        return tab

    def fill_clips(self):
        opened = {i for i in self.all_items() if self.clips.item(i, "open")}
        first = not self.clips.get_children()
        sel = self.clips.selection()
        self.clips.delete(*self.clips.get_children())
        for channel, folder, info, clips, others, best in streams():
            ch = f"ch:{channel}"
            if not self.clips.exists(ch):
                self.clips.insert("", "end", iid=ch, text=channel, open=first or ch in opened)
            source = folder / "stream.mp4"
            self.clips.insert(ch, "end", iid=str(folder), text=folder.name, open=str(folder) in opened,
                              values=(info.get("upload_date>%d. %m. %Y", ""), Klipy.clips_word(len(clips)),
                                      "" if best is None else f"max {best}", size(source) if source.exists() else "smazáno"))
            for name in others:  # zdrojové video, rozpracovaná nahrávka, cokoli dalšího
                what = {"stream.mp4": "zdrojové video"}.get(name, "nahrává se…" if name.endswith(".part") else "video")
                self.clips.insert(str(folder), "end", iid=str(folder / name), text=name,
                                  values=(what, "", "", size(folder / name)))
            for c in clips:
                self.clips.insert(str(folder), "end", iid=str(folder / c["file"]), text=c["file"],
                                  values=(f"{Klipy.hms(c['start'])}–{Klipy.hms(c['end'])}",
                                          f"{c['end'] - c['start']:.0f} s", c["score"]))
        self.clips.selection_set([i for i in sel if self.clips.exists(i)])

    def all_items(self, parent=""):
        for i in self.clips.get_children(parent):
            yield i
            yield from self.all_items(i)

    def kind(self):
        sel = self.clips.selection()
        if not sel:
            return None
        return "channel" if sel[0].startswith("ch:") else "stream" if Path(sel[0]).is_dir() else "clip"

    def picked(self, *kinds):
        """Vybraná položka (cesta), pokud je jednoho z druhů kinds, jinak hláška a None."""
        if self.kind() not in kinds:
            names = {"clip": "klip nebo video", "stream": "stream"}
            messagebox.showinfo("ClipFarm", f"Nejdřív vyber {' nebo '.join(names[k] for k in kinds)} v seznamu.")
            return None
        return Path(self.clips.selection()[0])

    def selected_folder(self):
        """Složka vybrané položky (streamer, stream nebo klip), bez výběru složka klipy/."""
        sel = self.clips.selection()
        if not sel:
            return Klipy.ROOT
        if sel[0].startswith("ch:"):
            return Klipy.ROOT / sel[0].removeprefix("ch:")
        path = Path(sel[0])
        return path if path.is_dir() else path.parent

    def play(self):
        if path := self.picked("clip"):
            open_path(path)

    def free_stream(self):
        """Složka vybraného streamu se zdrojovým videem, který se zrovna nezpracovává; jinak hláška a None."""
        if self.picked("stream", "clip") is None:
            return None
        folder = self.selected_folder()
        if not (folder / "stream.mp4").exists():
            messagebox.showerror("ClipFarm", "Zdrojové video (stream.mp4) chybí, s tímhle streamem už nejde pracovat.")
        elif self.job_for(folder):
            messagebox.showinfo("ClipFarm", "Tenhle stream se zrovna zpracovává. Počkej, nebo ho zastav v kartě Běží (Stop).")
        else:
            return folder
        return None

    def rescore(self):
        if folder := self.free_stream():
            self.procs.append((launch(str(folder), 0, self.awake.get(), rescore=True), str(folder), True))
            self.tabs.select(0)
            self.status.configure(text=f"Σ Přepočítávám skóre: {folder.name}…")

    def show_preview(self, folder):
        """Po přepočtu skóre: kolik klipů by dal který limit."""
        data = Klipy.load_json(folder / "klipy.json", {})
        rows = "\n".join(f"   limit {lim}  →  {Klipy.clips_word(n)}" for lim, n in data.get("preview", {}).items())
        messagebox.showinfo("ClipFarm", f"Skóre přepočítáno: {folder.name}\n\nNejvyšší skóre: {data.get('max')}\n"
                                        f"Kolik klipů by vzniklo při limitu:\n{rows}\n\n"
                                        "Současné klipy mají skóre na aktuální stupnici. Limit nastav nahoře "
                                        "a dej ✂ Přestříhat.")

    def reclip(self):
        if not (folder := self.free_stream()):
            return
        shape = "na výšku 9:16" if self.vertical.get() else "na šířku 16:9"
        if messagebox.askyesno("ClipFarm", f"Znovu nastříhat „{folder.name}“\ns limitem {self.limit.get()}, {shape}?\n"
                                           "Současné klipy se nahradí, až budou nové hotové.\n"
                                           "(Limit a formát se nastavují nahoře v okně.)"):
            self.start(str(folder))

    def open_folder(self):
        open_path(self.selected_folder())

    def open_browser(self):
        page = self.selected_folder() / "index.html"
        page = page if page.exists() else Klipy.ROOT / "index.html"
        self.base = self.base or serve()[1]
        webbrowser.open(self.base + quote(page.relative_to(Klipy.ROOT).as_posix()))

    def job_for(self, folder):
        """PID úlohy, která zrovna zpracovává (přestříhává, nahrává, stahuje) stream ve složce, nebo None."""
        for pid, job in jobs().items():
            target, part = job.get("target", ""), job.get("part")
            if (not target.startswith("http") and Path(target).resolve() == folder) or (part and Path(part).parent == folder):
                return pid
        return None

    def delete_source(self):
        if self.picked("stream", "clip") is None:
            return
        folder = self.selected_folder()
        if not (folder / "stream.mp4").exists():
            return
        if self.job_for(folder):
            messagebox.showerror("ClipFarm", "Tenhle stream se zrovna zpracovává, nejdřív ho ukonči.")
        elif messagebox.askyesno("ClipFarm", f"Smazat zdrojové video ({size(folder / 'stream.mp4')})?\n"
                                             "Klipy zůstanou, ale stream pak už nepůjde přestříhat."):
            (folder / "stream.mp4").unlink()
            self.fill_clips()

    def delete(self):
        kind, path = self.kind(), self.picked("clip", "stream")
        if path is None:
            return
        folder = path if kind == "stream" else path.parent
        if kind == "clip" and path.name == "stream.mp4":
            self.delete_source()
        elif self.job_for(folder):
            messagebox.showerror("ClipFarm", "Tenhle stream se zrovna zpracovává, nejdřív ho ukonči.")
        elif kind == "clip" and messagebox.askyesno("ClipFarm", f"Smazat klip {path.name}?"):
            delete_clip(folder, path.name)
            self.fill_clips()
        elif kind == "stream" and messagebox.askyesno("ClipFarm", f"Smazat celý stream „{path.name}“?\n"
                                                                  "Smaže se zdrojové video i všechny klipy."):
            delete_stream(folder)
            self.fill_clips()


def selftest():
    import tempfile
    assert alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    Klipy.JOBS.mkdir(parents=True, exist_ok=True)
    job = Klipy.JOBS / f"{p.pid}.json"
    job.write_text(json.dumps({"pid": p.pid, "target": "http://test", "started": time.time()}), encoding="utf-8")
    assert jobs()[p.pid]["target"] == "http://test", "běžící job je vidět"
    flag = Klipy.JOBS / f"{p.pid}.stop"
    set_stop(p.pid)
    assert Klipy.stop_time(flag) == 0.0, "Ukončit = hned"
    set_stop(p.pid, 90)
    assert 89 * 60 < Klipy.stop_time(flag) - time.time() <= 90 * 60, "časovač"  # type: ignore[operator]
    assert end_text(p.pid, "https://kick.com/x") in ("1:30:00", "1:29:59"), end_text(p.pid, "https://kick.com/x")
    set_stop(p.pid, 0)
    assert not flag.exists() and end_text(p.pid, "https://kick.com/x") == "dokud neskončí"
    assert end_text(p.pid, "https://youtu.be/x") == "–"
    assert [minutes(t) for t in ("dokud neskončí", "", "45", "45 min", "2 h", "1 h 30 min", "1h30")] == \
        [0, 0, 45, 45, 120, 90, 90]
    for bad in ("abc", "h", "1 den"):
        try:
            minutes(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    flag.write_text("", encoding="utf-8")
    p.kill()
    p.wait()
    assert not alive(p.pid) and p.pid not in jobs() and not job.exists() and not flag.exists(), "po skončeném jobu se uklidí"
    # Zastavit: úloha i její yt-dlp (vlastní skupina procesů) musí skončit
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    job_p, ytdlp_p = (subprocess.Popen(sleeper, start_new_session=True) for _ in range(2))
    kill(job_p.pid, {"ytdlp": ytdlp_p.pid})
    assert job_p.wait(timeout=5) != 0 and ytdlp_p.wait(timeout=5) != 0, "Zastavit"
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("a\n[download] 1%\r[download] 2%\r[download] 3%\n\n")
    assert tail(f.name, 2) == ["[download] 2%", "[download] 3%"], tail(f.name, 2)
    Path(f.name).unlink()
    assert tail(None) == []
    with tempfile.TemporaryDirectory() as d:  # smazání klipu přegeneruje stránku, smazání streamu přehled
        folder = Path(d) / "kanal" / "2026-01-01 – test"
        folder.mkdir(parents=True)
        info = {"id": "x", "title": "Test", "channel,uploader": "kanal", "channel_url,uploader_url": "",
                "upload_date>%d. %m. %Y": "01. 01. 2026", "webpage_url": "https://youtu.be/x", "extractor_key": "Youtube"}
        (folder / "info.json").write_text(json.dumps(info), encoding="utf-8")
        clips = [{"file": f"0{i}.mp4", "start": 10.0 * i, "end": 10.0 * i + 5, "score": 3.0} for i in (1, 2)]
        (folder / "klipy.json").write_text(json.dumps({"min_score": 2.5, "clips": clips}), encoding="utf-8")
        for name in ("01.mp4", "02.mp4"):
            (folder / name).write_bytes(b"")
        Klipy.write_page(folder)
        delete_clip(folder, "01.mp4")
        page = (folder / "index.html").read_text(encoding="utf-8")
        assert "02.mp4" in page and "01.mp4" not in page and not (folder / "01.mp4").exists()
        delete_stream(folder)
        assert not (Path(d) / "kanal").exists() and "kanal" not in (Path(d) / "index.html").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as d:  # v seznamu je každá složka s videem, i bez klipů a stránky
        root, Klipy.ROOT = Klipy.ROOT, Path(d)
        try:
            (Path(d) / "kanal" / "nedokonceny").mkdir(parents=True)
            (Path(d) / "kanal" / "nedokonceny" / "stream.mp4").write_bytes(b"")
            (Path(d) / "kanal" / "prazdny").mkdir()
            assert [(f.name, clips, others) for _, f, _, clips, others, _ in streams()] == \
                [("nedokonceny", [], ["stream.mp4"])], streams()
        finally:
            Klipy.ROOT = root
    assert [Klipy.clips_word(n) for n in (0, 1, 3, 5)] == ["0 klipů", "1 klip", "3 klipy", "5 klipů"]
    test_server()
    print("ok")


def test_server():
    import urllib.error
    import urllib.request
    Klipy.ROOT.mkdir(parents=True, exist_ok=True)
    f = Klipy.ROOT / "_test ž.bin"
    f.write_bytes(bytes(range(256)) * 4)
    server, base = serve(0)
    try:
        url = base + quote(f.name)
        get = lambda h=None: urllib.request.urlopen(urllib.request.Request(url, headers=h or {}), timeout=5)
        assert get().read() == f.read_bytes(), "celý soubor"
        r = get({"Range": "bytes=10-19"})
        assert r.status == 206 and r.read() == bytes(range(10, 20)) and r.headers["Content-Range"] == "bytes 10-19/1024"
        assert get({"Range": "bytes=-4"}).read() == bytes(range(252, 256)), "posledních n bajtů"
        assert get({"Range": "bytes=1000-"}).read() == (bytes(range(256)) * 4)[1000:], "od n do konce"
        try:
            urllib.request.urlopen(base + "../Klipy.py", timeout=5)
            raise AssertionError("server nesmí pustit ven ze složky klipy/")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()
        f.unlink()


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        selftest()
    else:
        install_launcher()
        login_path()
        App().mainloop()
