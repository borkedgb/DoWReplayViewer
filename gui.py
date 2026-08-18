import os
import sys
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD
from PIL import Image, ImageTk

from dow_replay_report import generate_report
from map_data import MAP_ASSET_BASE_URL, MAP_IMAGE_FILES

PORTRAIT_SIZE = (96, 96)
CARD_PORTRAIT_SIZE = (72, 72)
MAP_IMAGE_SIZE = (220, 220)

PALETTE = {
    "bg": "#eef1f5",
    "card": "#ffffff",
    "border": "#d9dce2",
    "text": "#1a1d23",
    "muted": "#6b7280",
    "accent": "#2563eb",
    "badge": "#e8edf7",
}

PLAYER_ACCENTS = ["#2563eb", "#ea580c", "#16a34a", "#9333ea"]


def load_thumbnail(path, size=PORTRAIT_SIZE):
    if not path or not os.path.exists(path):
        return None
    img = Image.open(path)
    img.thumbnail(size)
    return ImageTk.PhotoImage(img)


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def resource_path(*parts):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def cached_map_image(map_codename):
    filename = MAP_IMAGE_FILES.get(map_codename)
    if not filename:
        return None
    cache_dir = app_dir() / "map_cache"
    cache_path = cache_dir / filename
    if cache_path.exists():
        return cache_path
    try:
        cache_dir.mkdir(exist_ok=True)
        url = MAP_ASSET_BASE_URL + urllib.parse.quote(filename)
        with urllib.request.urlopen(url, timeout=6) as resp:
            cache_path.write_bytes(resp.read())
        return cache_path
    except Exception:
        return None


def fmt_duration(seconds):
    if not seconds:
        return "?"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def make_scrollable_tree(parent, columns, show="headings"):
    container = tk.Frame(parent)
    container.pack(fill="both", expand=True)
    container.grid_rowconfigure(0, weight=1)
    container.grid_columnconfigure(0, weight=1)

    tree = ttk.Treeview(container, columns=columns, show=show)
    vsb = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    return tree


class ReplayApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DoW Replay Viewer")
        self.root.geometry("1060x820")
        self._thumb_refs = []

        try:
            self.root.iconbitmap(default=str(resource_path("assets", "icon.ico")))
        except tk.TclError:
            pass

        self._setup_style()
        p = PALETTE

        header = tk.Frame(root, bg=p["bg"])
        header.pack(fill="x", padx=16, pady=(16, 8))

        self.drop_label = tk.Label(
            header, text="Drop a .rec file here, or click to browse",
            font=self.font_base, height=3, bg=p["badge"], fg=p["text"],
            highlightbackground=p["border"], highlightthickness=1, cursor="hand2",
        )
        self.drop_label.pack(fill="x")
        self.drop_label.bind("<Button-1>", lambda e: self.browse())

        self.status = tk.Label(header, text="", anchor="w", font=self.font_muted,
                                bg=p["bg"], fg=p["muted"])
        self.status.pack(fill="x", pady=(6, 0))

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        self.summary_tab = ttk.Frame(self.notebook)
        self.players_tab = ttk.Frame(self.notebook)
        self.build_order_tab = ttk.Frame(self.notebook)
        self.chat_tab = ttk.Frame(self.notebook)
        for tab, label in (
            (self.summary_tab, "Summary"), (self.players_tab, "Player Stats"),
            (self.build_order_tab, "Build Order"), (self.chat_tab, "Chat"),
        ):
            self.notebook.add(tab, text=label)

        self.root.drop_target_register(DND_FILES)
        self.drop_label.drop_target_register(DND_FILES)
        self.root.dnd_bind("<<Drop>>", self.on_drop)
        self.drop_label.dnd_bind("<<Drop>>", self.on_drop)

    def _setup_style(self):
        p = PALETTE
        self.root.configure(bg=p["bg"])

        family = "Segoe UI"
        self.font_base = tkfont.Font(family=family, size=10)
        self.font_muted = tkfont.Font(family=family, size=9)
        self.font_title = tkfont.Font(family=family, size=19, weight="bold")
        self.font_section = tkfont.Font(family=family, size=12, weight="bold")
        self.font_stat = tkfont.Font(family=family, size=18, weight="bold")
        self.font_name = tkfont.Font(family=family, size=13, weight="bold")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=p["bg"])
        style.configure("TNotebook", background=p["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(16, 8), font=self.font_base)
        style.configure("Treeview", rowheight=24, font=self.font_base,
                         background=p["card"], fieldbackground=p["card"], borderwidth=0)
        style.configure("Treeview.Heading", font=(family, 10, "bold"))
        style.map("Treeview", background=[("selected", p["accent"])],
                  foreground=[("selected", "#ffffff")])

    def browse(self):
        path = filedialog.askopenfilename(filetypes=[("DoW replay", "*.rec"), ("All files", "*.*")])
        if path:
            self.load_replay(path)

    def on_drop(self, event):
        path = self.root.tk.splitlist(event.data)[0]
        self.load_replay(path)

    def load_replay(self, path):
        self.status.config(text=f"Parsing {path} ...")
        self.root.update_idletasks()
        try:
            report, out_dir = generate_report(path)
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Failed to parse replay", str(e))
            self.status.config(text="")
            return
        self.status.config(text=f"Loaded {path} - report written to {out_dir}")
        self.render(report)

    def render(self, report):
        self._thumb_refs.clear()
        self._render_summary(report)
        self._render_players(report)
        self._render_build_order(report)
        self._render_chat(report)
        self._refresh_geometry()

    def _refresh_geometry(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        self.root.geometry(f"{w}x{h + 1}")
        self.root.update_idletasks()
        self.root.geometry(f"{w}x{h}")

    def _clear(self, frame):
        for child in frame.winfo_children():
            child.destroy()

    # --- Summary tab -----------------------------------------------------

    def _render_summary(self, report):
        p = PALETTE
        frame = self.summary_tab
        self._clear(frame)

        outer = tk.Frame(frame, bg=p["bg"])
        outer.pack(fill="both", expand=True)

        map_info = report.get("map", {})
        settings = report.get("lobby_settings", {}).get("settings", {})
        result = report.get("result", {})

        # --- header card -------------------------------------------------
        header = tk.Frame(outer, bg=p["card"], highlightbackground=p["border"],
                           highlightthickness=1)
        header.pack(fill="x")
        header_inner = tk.Frame(header, bg=p["card"])
        header_inner.pack(fill="x", padx=16, pady=16)
        header_inner.grid_columnconfigure(1, weight=1)

        map_codename = map_info.get("map_name")
        map_path = cached_map_image(map_codename) if map_codename else None
        map_thumb = load_thumbnail(map_path, MAP_IMAGE_SIZE) if map_path else None
        if map_thumb:
            self._thumb_refs.append(map_thumb)
            tk.Label(header_inner, image=map_thumb, bg=p["card"]).grid(
                row=0, column=0, rowspan=4, padx=(0, 20), sticky="n")

        map_display = map_info.get("map_display_name") or map_codename or "Unknown map"
        tk.Label(header_inner, text=map_display, font=self.font_title,
                  bg=p["card"], fg=p["text"]).grid(row=0, column=1, sticky="w")
        tk.Label(header_inner, text=os.path.basename(report.get("file", "")),
                  font=self.font_muted, bg=p["card"], fg=p["muted"]).grid(
            row=1, column=1, sticky="w", pady=(0, 10))

        badges = tk.Frame(header_inner, bg=p["card"])
        badges.grid(row=2, column=1, sticky="w")
        badge_texts = [
            f"Duration  {fmt_duration(report.get('duration_seconds'))}",
            f"Game speed  {settings.get('gamespeed', '?')}",
            f"AI difficulty  {settings.get('aidifficulty', '?')}",
        ]
        if settings.get("enablecheats"):
            badge_texts.append("Cheats on")
        for text in badge_texts:
            tk.Label(badges, text=text, font=self.font_muted, bg=p["badge"], fg=p["text"],
                     padx=10, pady=4).pack(side="left", padx=(0, 8))

        if result.get("known"):
            result_box = tk.Frame(header_inner, bg=p["card"])
            result_box.grid(row=3, column=1, sticky="w", pady=(14, 0))
            tk.Label(result_box, text=f"{result['winner']} defeated {result['loser']}",
                      font=self.font_section, bg=p["card"], fg=p["accent"]).pack(anchor="w")
            tk.Label(result_box, text="Guessed from the filename, not verified against the replay",
                      font=self.font_muted, bg=p["card"], fg=p["muted"]).pack(anchor="w")

        # --- player cards -------------------------------------------------
        players = report.get("players", [])
        cards = tk.Frame(outer, bg=p["bg"])
        cards.pack(fill="both", expand=True, pady=(16, 0))
        for i in range(len(players)):
            cards.grid_columnconfigure(i, weight=1, uniform="card")

        for i, pl in enumerate(players):
            self._render_player_card(cards, pl, i)

    def _render_player_card(self, parent, pl, col):
        p = PALETTE
        accent = PLAYER_ACCENTS[col % len(PLAYER_ACCENTS)]

        card = tk.Frame(parent, bg=p["card"], highlightbackground=p["border"],
                         highlightthickness=1)
        pad = (0, 8) if col == 0 else (8, 0)
        card.grid(row=0, column=col, sticky="nsew", padx=pad)

        tk.Frame(card, bg=accent, height=5).pack(fill="x")

        body = tk.Frame(card, bg=p["card"])
        body.pack(fill="both", expand=True, padx=16, pady=16)

        head = tk.Frame(body, bg=p["card"])
        head.pack(fill="x")

        thumb = load_thumbnail(pl.get("portrait", {}).get("saved_as"), CARD_PORTRAIT_SIZE)
        if thumb:
            self._thumb_refs.append(thumb)
            tk.Label(head, image=thumb, bg=p["card"]).pack(side="left", padx=(0, 12))

        name_col = tk.Frame(head, bg=p["card"])
        name_col.pack(side="left", fill="both", expand=True)
        tk.Label(name_col, text=pl.get("name") or "Unknown", font=self.font_name,
                  bg=p["card"], fg=p["text"], anchor="w").pack(fill="x")
        race = (pl.get("race") or "?").replace("_race", "").replace("_", " ").title()
        tk.Label(name_col, text=race, font=self.font_muted, bg=p["card"], fg=p["muted"],
                  anchor="w").pack(fill="x")
        if pl.get("commander_skin_name"):
            tk.Label(name_col, text=pl["commander_skin_name"], font=self.font_muted,
                      bg=p["card"], fg=p["muted"], anchor="w").pack(fill="x")

        stats_row = tk.Frame(body, bg=p["card"])
        stats_row.pack(fill="x", pady=(16, 12))
        for value, label in (
            (pl.get("recorded_apm", "?"), "APM"),
            (pl.get("recorded_commands", "?"), "Commands"),
        ):
            stat = tk.Frame(stats_row, bg=p["card"])
            stat.pack(side="left", padx=(0, 28))
            tk.Label(stat, text=str(value), font=self.font_stat, bg=p["card"],
                      fg=p["text"]).pack(anchor="w")
            tk.Label(stat, text=label, font=self.font_muted, bg=p["card"],
                      fg=p["muted"]).pack(anchor="w")

        if pl.get("stats_error"):
            tk.Label(body, text=pl["stats_error"], font=self.font_muted, bg=p["card"],
                      fg=p["muted"], wraplength=280, justify="left").pack(anchor="w")
            return

        categories = pl.get("activity_by_category") or {}
        total = sum(categories.values()) or 1
        bar_style = f"Player{col}.Horizontal.TProgressbar"
        ttk.Style().configure(bar_style, background=accent, troughcolor=p["badge"],
                               borderwidth=0, thickness=8)
        for cat, count in categories.items():
            row = tk.Frame(body, bg=p["card"])
            row.pack(fill="x", pady=3)
            label = cat.replace("_", " ").title().replace(" And ", " and ")
            tk.Label(row, text=label, font=self.font_muted,
                      bg=p["card"], fg=p["muted"], width=17, anchor="w").pack(side="left")
            ttk.Progressbar(row, style=bar_style, maximum=total, value=count,
                             length=100).pack(side="left", fill="x", expand=True, padx=(2, 8))
            tk.Label(row, text=str(count), font=self.font_muted, bg=p["card"],
                      fg=p["text"], width=4, anchor="e").pack(side="left")

    # --- Player Stats tab -------------------------------------------------

    def _render_players(self, report):
        frame = self.players_tab
        self._clear(frame)
        players = report.get("players", [])

        for i, p in enumerate(players):
            box = tk.LabelFrame(frame, text=p.get("name") or f"Player {i}")
            box.grid(row=0, column=i, sticky="nsew", padx=4, pady=4)
            frame.grid_columnconfigure(i, weight=1)
            frame.grid_rowconfigure(0, weight=1)

            tree = make_scrollable_tree(box, columns=("count",), show="tree headings")
            tree.heading("#0", text="Opcode")
            tree.heading("count", text="Count")
            tree.column("count", width=70, anchor="e")

            for cmd_name, count in (p.get("opcode_breakdown") or {}).items():
                tree.insert("", "end", text=cmd_name, values=(count,))

    # --- Build Order tab ---------------------------------------------------

    def _render_build_order(self, report):
        frame = self.build_order_tab
        self._clear(frame)
        players = report.get("players", [])

        for i, p in enumerate(players):
            box = tk.LabelFrame(frame, text=p.get("name") or f"Player {i}")
            box.grid(row=0, column=i, sticky="nsew", padx=4, pady=4)
            frame.grid_columnconfigure(i, weight=1)
            frame.grid_rowconfigure(0, weight=1)

            cols = ("time", "cmd", "kind", "x", "y")
            tree = make_scrollable_tree(box, columns=cols)
            for col, label, width in (
                ("time", "Time (s)", 70), ("cmd", "Command", 140),
                ("kind", "Kind", 120), ("x", "X", 50), ("y", "Y", 50),
            ):
                tree.heading(col, text=label)
                tree.column(col, width=width, anchor="w")

            for ev in p.get("build_order") or []:
                tree.insert("", "end", values=(
                    ev["time_seconds"], ev["cmd_name"], ev["kind"], ev["x"], ev["y"]))

    # --- Chat tab ----------------------------------------------------------

    def _render_chat(self, report):
        frame = self.chat_tab
        self._clear(frame)

        cols = ("time", "alias", "message")
        tree = make_scrollable_tree(frame, columns=cols)
        for col, label, width in (
            ("time", "Time (s)", 80), ("alias", "Player", 160), ("message", "Message", 600),
        ):
            tree.heading(col, text=label)
            tree.column(col, width=width, anchor="w")

        for msg in report.get("chat_messages") or []:
            tree.insert("", "end", values=(
                msg.get("timestamp_seconds"), msg.get("alias"), msg.get("message")))


def main():
    root = TkinterDnD.Tk()
    app = ReplayApp(root)
    if len(sys.argv) > 1:
        root.after(100, lambda: app.load_replay(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
