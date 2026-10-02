#!/usr/bin/env python3
"""ClipFarm okno: vložíš odkaz -> spustí Klipy.py, ukazuje běžící nahrávání, Ukončit a nastříhat.

Spuštění: python3 ClipFarm.py  (zároveň přidá/aktualizuje ClipFarm v nabídce aplikací GNOME).
Po přesunutí složky ho spusť jednou ručně, ať ikona v nabídce ukazuje na nové místo.
Self-check: python3 ClipFarm.py --test
"""
import itertools
import os
import shutil
import signal
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent
KLIPY = HERE / "Klipy.py"
LOGS = HERE / "klipy" / "_logy"
LAUNCHER = Path.home() / ".local/share/applications/clipfarm.desktop"


def install_launcher():
    """ClipFarm do nabídky aplikací, s cestou na tenhle soubor (ať je kdekoli)."""
    LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER.write_text("[Desktop Entry]\nType=Application\nName=ClipFarm\nComment=Automatické klipy ze streamů\n"
                        f'Exec=python3 "{Path(__file__).resolve()}"\nIcon=camera-video\nTerminal=false\n'
                        "Categories=AudioVideo;\n", encoding="utf-8")


def login_path():
    """Appka z nabídky nemá PATH z ~/.bashrc (nvm -> node, který yt-dlp potřebuje na YouTube)."""
    if not shutil.which("node"):
        path = subprocess.run(["bash", "-lc", "echo $PATH"], capture_output=True, text=True, check=False).stdout.strip()
        if path:
            os.environ["PATH"] = path


def running():
    """{pid: url} všech běžících Klipy.py, i těch spuštěných z terminálu."""
    jobs = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            args = [a.decode(errors="replace") for a in (d / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        for a, url in itertools.pairwise(args):
            if a.endswith("Klipy.py") and url.startswith("http"):
                jobs[int(d.name)] = url
    return jobs


def started(pid):
    """Kdy proces začal (unix čas)."""
    boot = next(int(l.split()[1]) for l in Path("/proc/stat").read_text().splitlines() if l.startswith("btime"))
    ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    return boot + ticks / os.sysconf("SC_CLK_TCK")


def tail(path, n=12):
    """Posledních n neprázdných řádků logu (yt-dlp přepisuje průběh přes \\r)."""
    try:
        with open(path, "rb") as f:
            f.seek(max(f.seek(0, 2) - 8192, 0))
            text = f.read().decode(errors="replace")
    except OSError:
        return []
    return [l for l in text.replace("\r", "\n").splitlines() if l.strip()][-n:]


def log_of(pid):
    """Kam proces píše výstup (log soubor), pokud do souboru."""
    try:
        target = os.readlink(f"/proc/{pid}/fd/1")
    except OSError:
        return None
    return target if os.path.isfile(target) else None


def recording(pid):
    """Kolik MB už proces (přes svůj yt-dlp/ffmpeg) nahrál, nebo None, když zrovna nenahrává."""
    try:
        group = os.getpgid(pid)
    except OSError:
        return None
    for d in Path("/proc").iterdir():
        try:
            if not d.name.isdigit() or os.getpgid(int(d.name)) != group:
                continue
            for fd in (d / "fd").iterdir():
                target = os.readlink(fd)
                if target.endswith(".part"):
                    return os.path.getsize(target) / 1e6
        except OSError:
            continue
    return None


def stop(pid):
    """= Ctrl+C: ukončí nahrávání a Klipy.py hned nastříhá, co je nahrané."""
    os.killpg(os.getpgid(pid), signal.SIGINT)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ClipFarm")
        self.geometry("900x560")
        self.procs = []  # (Popen, url) spuštěné z okna

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Odkaz (YouTube video nebo kick.com/kanál):").pack(anchor="w")
        self.url = ttk.Entry(top)
        self.url.pack(side="left", fill="x", expand=True)
        self.url.bind("<Return>", lambda _: self.start())
        ttk.Button(top, text="▶ Spustit", command=self.start).pack(side="left", padx=(8, 0))

        self.tree = ttk.Treeview(self, columns=("url", "time", "state"), show="headings", height=6)
        for col, label, width in (("url", "Stream", 280), ("time", "Běží", 100), ("state", "Stav", 470)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, stretch=col != "time")
        self.tree.pack(fill="x", padx=10)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.show_log())

        bar = ttk.Frame(self, padding=10)
        bar.pack(fill="x")
        ttk.Button(bar, text="⏹ Ukončit a nastříhat", command=self.stop).pack(side="left")
        ttk.Button(bar, text="📂 Otevřít klipy", command=self.open_clips).pack(side="left", padx=8)
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="left", padx=8)

        self.log = tk.Text(self, height=12, wrap="none", state="disabled", font=("monospace", 9))
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.refresh()

    def start(self):
        url = self.url.get().strip()
        if not url.startswith("http"):
            messagebox.showerror("ClipFarm", "Vlož odkaz začínající http(s)://")
            return
        LOGS.mkdir(parents=True, exist_ok=True)
        with (LOGS / f"{time.strftime('%Y-%m-%d %H.%M.%S')}.log").open("w") as log:
            p = subprocess.Popen([sys.executable, "-u", str(KLIPY), url], cwd=HERE, stdout=log,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        self.procs.append((p, url))
        self.url.delete(0, "end")
        self.refresh(again=False)

    def selected(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def stop(self):
        pid = self.selected()
        if pid is None:
            messagebox.showinfo("ClipFarm", "Vyber v seznamu, co ukončit.")
        elif messagebox.askyesno("ClipFarm", "Ukončit nahrávání a nastříhat, co je nahrané?"):
            stop(pid)

    def open_clips(self):
        index = HERE / "klipy" / "index.html"
        subprocess.Popen(["xdg-open", str(index if index.exists() else HERE)])

    def show_log(self):
        pid = self.selected()
        lines = tail(log_of(pid)) if pid and log_of(pid) else []
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(lines))
        self.log.configure(state="disabled")

    def refresh(self, again=True):
        jobs = running()
        for p, url in [x for x in self.procs if x[0].poll() is not None]:  # dokončené z okna
            self.procs.remove((p, url))
            self.status.configure(text=f"✔ Hotovo: {url}" if p.returncode == 0 else f"✖ Chyba ({p.returncode}): {url}")
        for iid in set(self.tree.get_children()) - {str(p) for p in jobs}:
            self.tree.delete(iid)
        for pid, url in jobs.items():
            try:
                secs = int(time.time() - started(pid))
            except OSError:
                continue
            log, mb = log_of(pid), recording(pid)
            state = f"⏺ nahrávám · {mb:.0f} MB" if mb is not None else \
                (tail(log, 1) or ["…"])[0] if log else "(běží v terminálu)"
            h, m = divmod(secs // 60, 60)
            row = (url, f"{h} h {m:02d} min" if h else f"{m} min", state)
            if self.tree.exists(str(pid)):
                self.tree.item(str(pid), values=row)
            else:
                self.tree.insert("", "end", iid=str(pid), values=row)
        self.show_log()
        if again:
            self.after(2000, self.refresh)


def selftest():
    import tempfile
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "Klipy.py", "http://test"],
                         start_new_session=True, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    assert running().get(p.pid) == "http://test", running()
    assert 0 <= time.time() - started(p.pid) < 5
    assert recording(p.pid) is None, "nic nenahrává"
    stop(p.pid)
    assert p.wait(timeout=5) != 0 and p.pid not in running(), "Ukončit musí proces zastavit"
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("a\n[download] 1%\r[download] 2%\r[download] 3%\n\n")
    assert tail(f.name, 2) == ["[download] 2%", "[download] 3%"], tail(f.name, 2)
    Path(f.name).unlink()
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        selftest()
    else:
        install_launcher()
        login_path()
        App().mainloop()
