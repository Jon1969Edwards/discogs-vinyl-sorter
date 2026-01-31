#!/usr/bin/env python3
"""Discogs Auto-Sort GUI

A fresh, minimal GUI that:
- Watches your Discogs collection for changes (polls item count).
- Regenerates shelf order automatically when it changes.
- Provides a "Refresh Now" button to force an immediate re-check + rebuild.

It reuses the core logic in discogs_app.py for fetching, sorting, and writing outputs.
Token discovery order:
- GUI Token field (if provided)
- DISCOGS_TOKEN env var
- .env (python-dotenv), via discogs_app.get_token

Outputs (default):
- vinyl_shelf_order.txt
- vinyl_shelf_order.csv
- optional JSON if enabled

Note: This is a polling-based approach because Discogs doesn’t provide push webhooks for
personal collections.
"""

from __future__ import annotations

import csv
import concurrent.futures
import queue
import subprocess
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from tkinter import Tk, StringVar, BooleanVar, IntVar, ttk, filedialog, messagebox
import tkinter as tk
import tkinter.font as tkfont

import discogs_app as core


try:
  import ttkbootstrap as ttkb  # type: ignore
except Exception:
  ttkb = None  # type: ignore


POLL_SECONDS_DEFAULT = 300  # 5 minutes
PRICE_FETCH_LIMIT_DEFAULT = 250
PRICE_FETCH_WORKERS_DEFAULT = 6


class RoundedButton(tk.Canvas):
  def __init__(
    self,
    parent: tk.Misc,
    text: str,
    command: callable,
    fill: str,
    text_fill: str,
    hover_fill: str | None = None,
    radius: int = 12,
    padding_x: int = 14,
    padding_y: int = 8,
    font: tuple[str, int] = ("TkDefaultFont", 12),
  ) -> None:
    self._text = text
    self._command = command
    self._fill = fill
    self._text_fill = text_fill
    self._hover_fill = hover_fill or fill
    self._radius = max(6, int(radius))
    self._padding_x = max(8, int(padding_x))
    self._padding_y = max(6, int(padding_y))
    self._font = font
    self._pressed = False

    fnt = tkfont.Font(font=font)
    text_w = int(fnt.measure(text))
    text_h = int(fnt.metrics("linespace"))
    w = max(110, text_w + (2 * self._padding_x))
    h = max(34, text_h + (2 * self._padding_y))
    super().__init__(parent, width=w, height=h, highlightthickness=0, bd=0)
    # IMPORTANT: tkinter.Widget uses `_w` internally for the widget path name.
    # Do not overwrite it.
    self._width = w
    self._height = h

    # Match parent background so the rounded shape looks natural.
    try:
      self.configure(bg=str(parent.cget("background")))
    except Exception:
      pass

    self._shape_ids: list[int] = []
    self._text_id: int | None = None
    self._draw(self._fill, self._text_fill)

    self.configure(cursor="hand2")
    self.bind("<Enter>", self._on_enter)
    self.bind("<Leave>", self._on_leave)
    self.bind("<ButtonPress-1>", self._on_press)
    self.bind("<ButtonRelease-1>", self._on_release)

  def set_colors(self, fill: str, text_fill: str, hover_fill: str | None = None) -> None:
    self._fill = fill
    self._text_fill = text_fill
    self._hover_fill = hover_fill or fill
    self._draw(self._fill, self._text_fill)

  def set_canvas_bg(self, bg: str) -> None:
    try:
      self.configure(bg=bg)
    except Exception:
      pass

  def _rounded_rect(self, x0: int, y0: int, x1: int, y1: int, r: int, fill: str) -> None:
    self._shape_ids.append(self.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=fill))
    self._shape_ids.append(self.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, outline=fill))
    self._shape_ids.append(self.create_arc(x0, y0, x0 + 2 * r, y0 + 2 * r, start=90, extent=90, fill=fill, outline=fill))
    self._shape_ids.append(self.create_arc(x1 - 2 * r, y0, x1, y0 + 2 * r, start=0, extent=90, fill=fill, outline=fill))
    self._shape_ids.append(self.create_arc(x0, y1 - 2 * r, x0 + 2 * r, y1, start=180, extent=90, fill=fill, outline=fill))
    self._shape_ids.append(self.create_arc(x1 - 2 * r, y1 - 2 * r, x1, y1, start=270, extent=90, fill=fill, outline=fill))

  def _draw(self, fill: str, text_fill: str) -> None:
    for i in getattr(self, "_shape_ids", []):
      try:
        self.delete(i)
      except Exception:
        pass
    self._shape_ids = []
    if self._text_id is not None:
      try:
        self.delete(self._text_id)
      except Exception:
        pass
      self._text_id = None
    self._rounded_rect(0, 0, self._width, self._height, self._radius, fill)
    self._text_id = self.create_text(
      self._width // 2,
      self._height // 2,
      text=self._text,
      fill=text_fill,
      font=self._font,
    )

  def _on_enter(self, _evt=None) -> None:
    if not self._pressed:
      self._draw(self._hover_fill, self._text_fill)

  def _on_leave(self, _evt=None) -> None:
    self._pressed = False
    self._draw(self._fill, self._text_fill)

  def _on_press(self, _evt=None) -> None:
    self._pressed = True

  def _on_release(self, _evt=None) -> None:
    if not self._pressed:
      return
    self._pressed = False
    try:
      self._command()
    finally:
      self._draw(self._hover_fill, self._text_fill)


@dataclass
class AutoConfig:
  token: str
  user_agent: str
  output_dir: str
  per_page: int
  write_json: bool
  poll_seconds: int


def get_collection_count(headers: dict[str, str], username: str) -> int:
  """Fetch collection size cheaply via pagination metadata."""
  url = f"{core.API_BASE}/users/{username}/collection/folders/0/releases"
  data = core.api_get(url, headers, params={"page": "1", "per_page": "1"}).json()
  return int(data.get("pagination", {}).get("items", 0))


@dataclass
class BuildResult:
  username: str
  rows_sorted: list[core.ReleaseRow]
  lines: list[str]


def build_once(cfg: AutoConfig, log: callable) -> BuildResult:
  token = core.get_token(cfg.token or None)
  headers = core.discogs_headers(token, cfg.user_agent)
  ident = core.get_identity(headers)
  username = ident.get("username")
  if not username:
    raise RuntimeError("Could not determine username from token.")

  log(f"User: {username}")

  out_dir = Path(cfg.output_dir)
  out_dir.mkdir(parents=True, exist_ok=True)

  rows = core.collect_lp_rows(
    headers=headers,
    username=username,
    per_page=max(1, min(int(cfg.per_page), 100)),
    max_pages=None,
    extra_articles=[],
    lp_strict=False,
    lp_probable=False,
    debug_stats=None,
    last_name_first=True,
    lnf_allow_3=False,
    lnf_exclude=set(),
    lnf_safe_bands=True,
    collect_exclusions=False,
  )

  if not rows:
    log("No matching LPs found.")
    return BuildResult(username=username, rows_sorted=[], lines=[])

  # Canonical sort: smart artist sorting + file Various Artists by title
  rows_sorted = core.sort_rows(list(rows), "title")
  lines = core.generate_txt_lines(rows_sorted, dividers=False, align=False, show_country=False)
  return BuildResult(username=username, rows_sorted=rows_sorted, lines=lines)


class App:
  def __init__(self, root: Tk) -> None:
    self.root = root
    root.title("Discogs Auto-Sort")
    try:
      root.minsize(900, 620)
    except Exception:
      pass

    self.style = ttk.Style()
    self._has_bootstrap = ttkb is not None
    if not self._has_bootstrap:
      try:
        # 'clam' tends to look a bit more modern/cross-platform than the default.
        if "clam" in self.style.theme_names():
          self.style.theme_use("clam")
      except Exception:
        pass

    self.v_token = StringVar(value="")
    self.v_show_token = BooleanVar(value=False)
    self.v_output_dir = StringVar(value=str(Path.cwd()))
    self.v_per_page = IntVar(value=100)
    self.v_json = BooleanVar(value=False)
    self.v_poll = IntVar(value=POLL_SECONDS_DEFAULT)
    self.v_show_prices = BooleanVar(value=False)

    self.v_theme = StringVar(value="light")
    self.v_price_sort = StringVar(value="shelf")

    self.v_search = StringVar(value="")
    self.v_match = StringVar(value="")
    self.v_status = StringVar(value="Starting…")

    # Holds the most recent build for export/printing
    self._last_result: BuildResult | None = None
    self._price_cache: dict[int, float | None] = {}
    self._price_lock = threading.Lock()
    self._prices_inflight = False
    self._prices_enabled = False
    self.result_q: queue.Queue[BuildResult] = queue.Queue()

    self._stop = threading.Event()
    self._wake = threading.Event()

    self._last_count: int | None = None
    self._last_built_at: float | None = None

    self.log_q: queue.Queue[str] = queue.Queue()

    self._build_ui(root)
    self._pump_queues()

    # Start watching immediately
    threading.Thread(target=self._watch_loop, daemon=True).start()

  def _build_ui(self, root: Tk) -> None:
    pad = {"padx": 8, "pady": 6}

    frm = ttk.Frame(root)
    frm.grid(row=0, column=0, sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)

    row = 0
    header = ttk.Frame(frm)
    header.grid(row=row, column=0, columnspan=2, sticky="ew", **pad)
    header.columnconfigure(0, weight=1)
    ttk.Label(header, text="Discogs Auto-Sort", font=("TkDefaultFont", 16, "bold")).grid(row=0, column=0, sticky="w")
    ttk.Label(header, text="LPs only • Watches your collection and rebuilds automatically", foreground="#555").grid(row=1, column=0, sticky="w")

    theme_btn = ttk.Menubutton(header, text="Theme")
    theme_btn.grid(row=0, column=1, sticky="e")
    theme_menu = tk.Menu(theme_btn, tearoff=0)
    theme_menu.add_radiobutton(label="Light", value="light", variable=self.v_theme, command=self._apply_theme)
    theme_menu.add_radiobutton(label="Dark", value="dark", variable=self.v_theme, command=self._apply_theme)
    theme_btn["menu"] = theme_menu
    row += 1

    settings = ttk.LabelFrame(frm, text="Settings")
    settings.grid(row=row, column=0, columnspan=2, sticky="ew", **pad)
    settings.columnconfigure(1, weight=1)
    srow = 0

    ttk.Label(settings, text="Token").grid(row=srow, column=0, sticky="w", **pad)
    self.token_entry = ttk.Entry(settings, textvariable=self.v_token, width=44, show="•")
    self.token_entry.grid(row=srow, column=1, sticky="ew", **pad)
    ttk.Checkbutton(settings, text="Show", variable=self.v_show_token, command=self._toggle_token_visibility).grid(row=srow, column=2, sticky="w", **pad)
    srow += 1

    out_row = ttk.Frame(settings)
    out_row.grid(row=srow, column=0, columnspan=3, sticky="ew", **pad)
    out_row.columnconfigure(1, weight=1)
    ttk.Label(out_row, text="Output Dir").grid(row=0, column=0, sticky="w")
    ttk.Entry(out_row, textvariable=self.v_output_dir).grid(row=0, column=1, sticky="ew", padx=4)
    ttk.Button(out_row, text="Browse", command=self._choose_dir).grid(row=0, column=2, sticky="e")
    ttk.Button(out_row, text="Open", command=self._open_output_dir).grid(row=0, column=3, sticky="e", padx=(6, 0))
    srow += 1

    opt = ttk.Frame(settings)
    opt.grid(row=srow, column=0, columnspan=3, sticky="ew", **pad)
    ttk.Label(opt, text="Poll seconds").grid(row=0, column=0, sticky="w")
    ttk.Spinbox(opt, from_=15, to=3600, textvariable=self.v_poll, width=8).grid(row=0, column=1, padx=6)
    ttk.Checkbutton(opt, text="Also JSON", variable=self.v_json).grid(row=0, column=2, padx=6, sticky="w")
    ttk.Checkbutton(opt, text="Prices (SEK)", variable=self.v_show_prices, command=self._on_prices_toggle).grid(row=0, column=3, padx=6, sticky="w")
    srow += 1

    row += 1

    search_row = ttk.Frame(frm)
    search_row.grid(row=row, column=0, columnspan=2, sticky="ew", **pad)
    search_row.columnconfigure(1, weight=1)
    ttk.Label(search_row, text="Search").grid(row=0, column=0, sticky="w")
    search_entry = ttk.Entry(search_row, textvariable=self.v_search)
    search_entry.grid(row=0, column=1, sticky="ew", padx=6)
    ttk.Button(search_row, text="Clear", command=lambda: self.v_search.set("")).grid(row=0, column=2, sticky="e")
    ttk.Label(search_row, textvariable=self.v_match).grid(row=0, column=3, sticky="e", padx=6)
    self.v_search.trace_add("write", lambda *_: self._on_search_change())
    row += 1

    btn = ttk.Frame(frm)
    btn.grid(row=row, column=0, columnspan=2, sticky="w", **pad)

    self.btn_refresh = RoundedButton(btn, "Refresh", self._refresh_now, fill="#2563eb", text_fill="#ffffff", hover_fill="#1d4ed8")
    self.btn_export = RoundedButton(btn, "Export", self._export_files, fill="#6b7280", text_fill="#ffffff", hover_fill="#4b5563")
    self.btn_print = RoundedButton(btn, "Print", self._print_current, fill="#16a34a", text_fill="#ffffff", hover_fill="#15803d")
    self.btn_stop = RoundedButton(btn, "Stop", self._stop_app, fill="#dc2626", text_fill="#ffffff", hover_fill="#b91c1c")
    for i, b in enumerate([self.btn_refresh, self.btn_export, self.btn_print, self.btn_stop]):
      b.grid(row=0, column=i, padx=(0, 10))
    row += 1

    nb = ttk.Notebook(frm)
    nb.grid(row=row, column=0, columnspan=2, sticky="nsew", **pad)
    frm.rowconfigure(row, weight=1)

    order_fr = ttk.Frame(nb)
    nb.add(order_fr, text="Shelf Order")
    order_fr.rowconfigure(0, weight=1)
    order_fr.columnconfigure(0, weight=1)

    order_wrap = ttk.Frame(order_fr)
    order_wrap.grid(row=0, column=0, sticky="nsew")
    order_wrap.rowconfigure(0, weight=1)
    order_wrap.columnconfigure(0, weight=1)

    order_scroll = ttk.Scrollbar(order_wrap, orient="vertical")
    order_scroll.grid(row=0, column=1, sticky="ns")

    self.order_text = tk.Text(
      order_wrap,
      height=18,
      width=90,
      wrap="none",
      yscrollcommand=order_scroll.set,
      font=("Menlo", 12),
    )
    self.order_text.grid(row=0, column=0, sticky="nsew")
    order_scroll.config(command=self.order_text.yview)
    self.order_text.tag_configure("search_match", background="#fff3b0")

    prices_fr = ttk.Frame(nb)
    nb.add(prices_fr, text="Prices")
    prices_fr.rowconfigure(1, weight=1)
    prices_fr.columnconfigure(0, weight=1)

    prices_bar = ttk.Frame(prices_fr)
    prices_bar.grid(row=0, column=0, sticky="ew", padx=2, pady=(2, 6))
    prices_bar.columnconfigure(1, weight=1)
    ttk.Label(prices_bar, text="Sort").grid(row=0, column=0, sticky="w", padx=(2, 6))
    price_sort = ttk.Combobox(
      prices_bar,
      textvariable=self.v_price_sort,
      values=["shelf", "price_desc", "price_asc"],
      width=12,
      state="readonly",
    )
    price_sort.grid(row=0, column=1, sticky="w")
    ttk.Label(prices_bar, text="(price sorts only this tab)", foreground="#777").grid(row=0, column=2, sticky="e", padx=(10, 2))
    price_sort.bind("<<ComboboxSelected>>", lambda *_: self._render_prices(self._last_result or BuildResult(username="", rows_sorted=[], lines=[])))

    prices_wrap = ttk.Frame(prices_fr)
    prices_wrap.grid(row=1, column=0, sticky="nsew")
    prices_wrap.rowconfigure(0, weight=1)
    prices_wrap.columnconfigure(0, weight=1)

    prices_scroll = ttk.Scrollbar(prices_wrap, orient="vertical")
    prices_scroll.grid(row=0, column=1, sticky="ns")
    self.prices_text = tk.Text(
      prices_wrap,
      height=18,
      width=90,
      wrap="none",
      yscrollcommand=prices_scroll.set,
      font=("Menlo", 12),
    )
    self.prices_text.grid(row=0, column=0, sticky="nsew")
    prices_scroll.config(command=self.prices_text.yview)

    log_fr = ttk.Frame(nb)
    nb.add(log_fr, text="Log")
    log_fr.rowconfigure(0, weight=1)
    log_fr.columnconfigure(0, weight=1)
    log_wrap = ttk.Frame(log_fr)
    log_wrap.grid(row=0, column=0, sticky="nsew")
    log_wrap.rowconfigure(0, weight=1)
    log_wrap.columnconfigure(0, weight=1)
    log_scroll = ttk.Scrollbar(log_wrap, orient="vertical")
    log_scroll.grid(row=0, column=1, sticky="ns")
    self.log = tk.Text(log_wrap, height=18, width=90, yscrollcommand=log_scroll.set, font=("Menlo", 12))
    self.log.grid(row=0, column=0, sticky="nsew")
    log_scroll.config(command=self.log.yview)

    # Status bar
    status = ttk.Label(frm, textvariable=self.v_status, anchor="w")
    status.grid(row=row + 1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))

    # Apply initial theme after widgets exist
    self._apply_theme()

  def _apply_theme(self) -> None:
    mode = (self.v_theme.get() or "light").strip().lower()
    dark = mode == "dark"

    # Switch ttk theme when ttkbootstrap is installed.
    if ttkb is not None:
      try:
        # ttkbootstrap attaches a style object to the Window.
        if hasattr(self.root, "style") and getattr(self.root, "style") is not None:
          self.root.style.theme_use("darkly" if dark else "flatly")  # type: ignore[attr-defined]
        else:
          ttk.Style().theme_use("darkly" if dark else "flatly")
      except Exception:
        pass
    else:
      # Best-effort styling for stdlib ttk. Works best with the 'clam' theme.
      try:
        if "clam" in ttk.Style().theme_names():
          ttk.Style().theme_use("clam")
      except Exception:
        pass

      bg = "#1e1e1e" if dark else "#f5f5f5"
      fg = "#e6e6e6" if dark else "#000000"
      field_bg = "#2a2a2a" if dark else "#ffffff"
      field_fg = fg

      s = ttk.Style()
      try:
        s.configure("TFrame", background=bg)
        s.configure("TLabel", background=bg, foreground=fg)
        s.configure("TButton", background=bg, foreground=fg)
        s.configure("TCheckbutton", background=bg, foreground=fg)
        s.configure("TRadiobutton", background=bg, foreground=fg)
        s.configure("TMenubutton", background=bg, foreground=fg)
        s.configure("TLabelframe", background=bg, foreground=fg)
        s.configure("TLabelframe.Label", background=bg, foreground=fg)
        s.configure("TEntry", fieldbackground=field_bg, foreground=field_fg)
        s.configure("TSpinbox", fieldbackground=field_bg, foreground=field_fg)
        s.configure("TCombobox", fieldbackground=field_bg, foreground=field_fg)
        s.configure("TNotebook", background=bg)
        s.configure("TNotebook.Tab", background=bg, foreground=fg)
      except Exception:
        pass

    # Tk Text widgets: explicitly set colors (ttk themes don't style tk.Text).
    if dark:
      text_bg = "#111111"
      text_fg = "#e6e6e6"
      insert = "#e6e6e6"
      match_bg = "#264f78"
    else:
      text_bg = "#ffffff"
      text_fg = "#000000"
      insert = "#000000"
      match_bg = "#fff3b0"
    try:
      self.order_text.configure(background=text_bg, foreground=text_fg, insertbackground=insert)
      self.log.configure(background=text_bg, foreground=text_fg, insertbackground=insert)
      self.prices_text.configure(background=text_bg, foreground=text_fg, insertbackground=insert)
      self.order_text.tag_configure("search_match", background=match_bg)
    except Exception:
      pass

    # Best-effort for the toplevel background.
    try:
      self.root.configure(background=("#1e1e1e" if dark else "#f5f5f5"))
    except Exception:
      pass

    # Rounded button palette + canvas background
    try:
      canvas_bg = "#1e1e1e" if dark else "#f5f5f5"
      for b in [self.btn_refresh, self.btn_export, self.btn_print, self.btn_stop]:
        b.set_canvas_bg(canvas_bg)
      if dark:
        self.btn_refresh.set_colors("#3b82f6", "#ffffff", "#2563eb")
        self.btn_export.set_colors("#6b7280", "#ffffff", "#4b5563")
        self.btn_print.set_colors("#22c55e", "#ffffff", "#16a34a")
        self.btn_stop.set_colors("#ef4444", "#ffffff", "#dc2626")
      else:
        self.btn_refresh.set_colors("#2563eb", "#ffffff", "#1d4ed8")
        self.btn_export.set_colors("#6b7280", "#ffffff", "#4b5563")
        self.btn_print.set_colors("#16a34a", "#ffffff", "#15803d")
        self.btn_stop.set_colors("#dc2626", "#ffffff", "#b91c1c")
    except Exception:
      pass

  def _choose_dir(self) -> None:
    directory = filedialog.askdirectory(initialdir=self.v_output_dir.get() or str(Path.cwd()))
    if directory:
      self.v_output_dir.set(directory)

  def _open_output_dir(self) -> None:
    path = self.v_output_dir.get().strip() or str(Path.cwd())
    try:
      subprocess.run(["open", path], check=False)
    except Exception:
      pass

  def _toggle_token_visibility(self) -> None:
    try:
      self.token_entry.configure(show="" if self.v_show_token.get() else "•")
    except Exception:
      pass

  def _log(self, msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    self.log_q.put(f"[{ts}] {msg}\n")

  def _pump_queues(self) -> None:
    try:
      while True:
        line = self.log_q.get_nowait()
        self.log.insert("end", line)
        self.log.see("end")
    except queue.Empty:
      pass

    try:
      while True:
        result = self.result_q.get_nowait()
        self._last_result = result
        self._render_order(result)
    except queue.Empty:
      pass

    self.root.after(100, self._pump_queues)

  def _render_order(self, result: BuildResult) -> None:
    self.order_text.delete("1.0", "end")
    if not result.lines:
      self.order_text.insert("end", "(No matching LPs found.)\n")
      self.v_match.set("")
      self._render_prices(result)
      return

    # Always show the full list; search only highlights matches.
    self.order_text.insert("end", "\n".join(result.lines) + "\n")
    self.order_text.see("1.0")
    self._highlight_search()
    self._render_prices(result)

  def _on_prices_toggle(self) -> None:
    if self.v_show_prices.get():
      if not messagebox.askyesno(
        "Prices",
        "This will fetch Discogs price data in SEK and may take a while for large collections. Continue?",
      ):
        self.v_show_prices.set(False)
        self._prices_enabled = False
        return
      self._prices_enabled = True
    if self._last_result is not None:
      self._render_order(self._last_result)
    if not self.v_show_prices.get():
      self._prices_enabled = False

  def _maybe_fetch_prices_async(self) -> None:
    if self._prices_inflight:
      return
    if not self._prices_enabled:
      return
    result = self._last_result
    if not result or not result.rows_sorted:
      return

    # Snapshot Tk variables on the UI thread (Tkinter variables are not thread-safe).
    token = (self.v_token.get() or "").strip()
    user_agent = core.get_user_agent(None)
    release_ids: list[int] = []
    for r in result.rows_sorted:
      rid = r.release_id
      if isinstance(rid, int) and rid not in self._price_cache:
        release_ids.append(rid)

    if not release_ids:
      return

    self._prices_inflight = True
    self._log(f"Starting price fetch thread (pending: {len(release_ids)})")
    threading.Thread(target=self._fetch_prices_task, args=(token, user_agent, release_ids), daemon=True).start()

  def _fetch_prices_task(self, token: str, user_agent: str, release_ids: list[int]) -> None:
    try:
      token_resolved = core.get_token(token or None)
      headers = core.discogs_headers(token_resolved, user_agent)

      with self._price_lock:
        missing = [rid for rid in release_ids if rid not in self._price_cache]
      if not missing:
        return

      if len(missing) > PRICE_FETCH_LIMIT_DEFAULT:
        self._log(f"Common price fetch capped at {PRICE_FETCH_LIMIT_DEFAULT} (pending: {len(missing)})")
        missing = missing[:PRICE_FETCH_LIMIT_DEFAULT]

      workers = min(max(1, PRICE_FETCH_WORKERS_DEFAULT), 10)
      self._log(f"Fetching prices (SEK) for {len(missing)} releases… (workers: {workers})")

      fetched = 0

      def fetch_one(rid: int) -> tuple[int, float | None]:
        # Use /releases/{id} lowest_price: tends to be more consistently present.
        return rid, core.get_release_lowest_price(headers, rid, curr_abbr="SEK")

      with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_one, rid): rid for rid in missing}
        for fut in concurrent.futures.as_completed(futures):
          if not self._prices_enabled:
            break
          rid = futures.get(fut)
          try:
            rid2, price = fut.result()
          except Exception:
            # Cache failures as None so we don't spin forever.
            rid2, price = int(rid) if rid is not None else -1, None
          if rid2 >= 0:
            with self._price_lock:
              if rid2 not in self._price_cache:
                self._price_cache[rid2] = price
          fetched += 1
          if fetched % 25 == 0:
            self._log(f"Prices fetched: {fetched}/{len(missing)}")
      self._log("Price fetch complete.")
    except BaseException as e:
      self._log(f"Price fetch failed: {e}")
      self._log(traceback.format_exc())
    finally:
      self._prices_inflight = False
      self._log("Price fetch thread finished.")
      try:
        if self._last_result is not None:
          self.root.after(0, lambda: self._render_order(self._last_result))
      except Exception:
        pass

  def _render_prices(self, result: BuildResult) -> None:
    # Show prices in a separate tab to avoid cluttering the main shelf order.
    try:
      self.prices_text.delete("1.0", "end")
    except Exception:
      return

    if not self.v_show_prices.get():
      self.prices_text.insert("end", "(Enable Prices (SEK) to fetch and display prices here.)\n")
      return

    if not result.rows_sorted:
      self.prices_text.insert("end", "(No items.)\n")
      return

    with self._price_lock:
      cache_snapshot = dict(self._price_cache)
    total = len(result.rows_sorted)
    cached = sum(1 for r in result.rows_sorted if isinstance(r.release_id, int) and r.release_id in cache_snapshot)
    priced = sum(1 for r in result.rows_sorted if isinstance(r.release_id, int) and isinstance(cache_snapshot.get(r.release_id), (int, float)))
    self.prices_text.insert("end", f"Prices (SEK) — {priced} with price • {cached}/{total} fetched\n\n")

    rows = list(result.rows_sorted)
    mode = (self.v_price_sort.get() or "shelf").strip().lower()
    if mode in {"price_desc", "price_asc"}:
      def _p(row: core.ReleaseRow) -> float | None:
        rid = row.release_id
        if isinstance(rid, int) and rid in self._price_cache:
          val = self._price_cache.get(rid)
          return float(val) if isinstance(val, (int, float)) else None
        return None

      if mode == "price_asc":
        rows.sort(key=lambda r: (_p(r) is None, _p(r) if _p(r) is not None else 0.0, r.sort_artist, r.sort_title))
      else:
        rows.sort(key=lambda r: (_p(r) is None, -(_p(r) if _p(r) is not None else 0.0), r.sort_artist, r.sort_title))

    for r in rows:
      rid = r.release_id
      if not isinstance(rid, int):
        price_txt = "—"
      elif rid not in cache_snapshot:
        price_txt = "…"
      else:
        p = cache_snapshot.get(rid)
        price_txt = f"{p:.0f}" if isinstance(p, (int, float)) else "—"
      self.prices_text.insert("end", f"{r.artist_display} — {r.title} ({r.year or ''})  ~{price_txt} SEK\n")

    # Ensure background fetch is running.
    self._maybe_fetch_prices_async()

  def _highlight_search(self) -> None:
    # Highlight matches within the displayed text without filtering out lines.
    self.order_text.tag_remove("search_match", "1.0", "end")
    q = (self.v_search.get() or "").strip()
    if not q:
      if self._last_result is not None:
        self.v_match.set(f"{len(self._last_result.lines)} items")
      else:
        self.v_match.set("")
      return

    start = "1.0"
    matches = 0
    first_match: str | None = None
    while True:
      idx = self.order_text.search(q, start, stopindex="end", nocase=True)
      if not idx:
        break
      if first_match is None:
        first_match = idx
      end = f"{idx}+{len(q)}c"
      self.order_text.tag_add("search_match", idx, end)
      matches += 1
      start = end

    self.v_match.set(f"{matches} matches" if matches != 1 else "1 match")
    if first_match is not None:
      self.order_text.see(first_match)
      try:
        self.order_text.mark_set("insert", first_match)
      except Exception:
        pass

  def _on_search_change(self) -> None:
    self._highlight_search()

  def _get_cfg(self) -> AutoConfig:
    return AutoConfig(
      token=self.v_token.get().strip(),
      user_agent=core.get_user_agent(None),
      output_dir=self.v_output_dir.get().strip() or str(Path.cwd()),
      per_page=max(1, min(int(self.v_per_page.get() or 100), 100)),
      write_json=bool(self.v_json.get()),
      poll_seconds=max(15, int(self.v_poll.get() or POLL_SECONDS_DEFAULT)),
    )

  def _refresh_now(self) -> None:
    # Wake the watcher and force immediate check
    self._log("Manual refresh requested.")
    self.v_status.set("Refresh requested…")
    self._wake.set()

  def _stop_app(self) -> None:
    if messagebox.askyesno("Stop", "Stop auto-watching and close the app?"):
      self._stop.set()
      self.root.after(200, self.root.destroy)

  def _export_files(self) -> None:
    result = self._last_result
    if not result or not result.rows_sorted:
      messagebox.showinfo("Export", "No shelf order available yet. Wait for the first build, then try again.")
      return

    cfg = self._get_cfg()
    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    txt_path = out_dir / "vinyl_shelf_order.txt"
    csv_path = out_dir / "vinyl_shelf_order.csv"
    core.write_txt(result.rows_sorted, txt_path, dividers=False, align=False, show_country=False)
    core.write_csv(result.rows_sorted, csv_path)
    self._log(f"Exported: {txt_path.name}")
    self._log(f"Exported: {csv_path.name}")

    if cfg.write_json:
      json_path = out_dir / "vinyl_shelf_order.json"
      core.write_json(result.rows_sorted, json_path)
      self._log(f"Exported: {json_path.name}")

    messagebox.showinfo("Export", f"Wrote files to:\n{out_dir}")
    self.v_status.set(f"Exported to: {out_dir}")

    # Optional: additional outputs that include common price (median) if enabled.
    if self.v_show_prices.get() and result.rows_sorted:
      try:
        base_lines = core.generate_txt_lines(result.rows_sorted, dividers=False, align=False, show_country=False)
        lines_with_prices: list[str] = []
        for r, line in zip(result.rows_sorted, base_lines):
          rid = r.release_id
          if isinstance(rid, int) and rid in self._price_cache:
            p = self._price_cache.get(rid)
            if p is None:
              lines_with_prices.append(f"{line} [~— SEK]")
            else:
              lines_with_prices.append(f"{line} [~{p:.0f} SEK]")
          else:
            lines_with_prices.append(f"{line} [~… SEK]")

        txtp = out_dir / "vinyl_shelf_order_with_prices.txt"
        with txtp.open("w", encoding="utf-8") as f:
          f.write("\n".join(lines_with_prices) + "\n")
        self._log(f"Exported: {txtp.name}")

        csvp = out_dir / "vinyl_shelf_order_with_prices.csv"
        with csvp.open("w", newline="", encoding="utf-8") as f:
          writer = csv.writer(f)
          writer.writerow([
            "Artist",
            "Title",
            "Year",
            "Label",
            "CatNo",
            "Country",
            "Format",
            "DiscogsURL",
            "Notes",
            "CommonPriceSEK",
          ])
          for r in result.rows_sorted:
            rid = r.release_id
            p = self._price_cache.get(rid) if isinstance(rid, int) else None
            writer.writerow(
              [
                r.artist_display,
                r.title,
                r.year or "",
                r.label,
                r.catno,
                r.country,
                r.format_str,
                r.discogs_url,
                r.notes,
                f"{p:.2f}" if isinstance(p, (int, float)) else "",
              ]
            )
        self._log(f"Exported: {csvp.name}")
      except Exception as e:
        self._log(f"Price export failed: {e}")

  def _print_current(self) -> None:
    result = self._last_result
    if not result or not result.lines:
      messagebox.showinfo("Print", "Nothing to print yet. Wait for the first build.")
      return

    if not messagebox.askyesno("Print", "Send the current shelf order to your default printer?"):
      return

    if subprocess.run(["sh", "-lc", "command -v lpr"], capture_output=True).returncode != 0:
      messagebox.showerror("Print", "Could not find 'lpr' command on this system.")
      return

    try:
      with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        # Print what the user currently sees (includes common prices when enabled).
        try:
          f.write(self.order_text.get("1.0", "end").rstrip() + "\n")
        except Exception:
          f.write("\n".join(result.lines) + "\n")
        tmp_path = f.name
      subprocess.run(["lpr", tmp_path], check=True)
      self._log("Sent to printer via lpr.")
      self.v_status.set("Sent to printer.")
    except Exception as e:
      messagebox.showerror("Print", f"Printing failed: {e}")
      self.v_status.set("Print failed.")

  def _watch_loop(self) -> None:
    """Background thread: poll collection count; rebuild on change or manual refresh."""
    self._log("Watcher started.")
    self.v_status.set("Watching for changes…")
    while not self._stop.is_set():
      cfg = self._get_cfg()
      try:
        token = core.get_token(cfg.token or None)
        headers = core.discogs_headers(token, cfg.user_agent)
        try:
          ident = core.get_identity(headers)
        except RuntimeError as e:
          if '429' in str(e):
            self._log("Rate limit reached. Please wait and try again.")
            self.v_status.set("Rate limit reached. Waiting…")
            # Temporarily disable Refresh button if present
            if hasattr(self, 'btn_refresh'):
              self.btn_refresh.config(state='disabled')
              self.root.after(5000, lambda: self.btn_refresh.config(state='normal'))
            # Wait a bit longer before next poll
            time.sleep(5)
            continue
          else:
            raise
        username = ident.get("username")
        if not username:
          raise RuntimeError("Could not determine username from token.")

        count = get_collection_count(headers, username)
        if self._last_count is None:
          self._last_count = count
          self._log(f"Initial collection count: {count}")
          # Build once on startup
          self._log("Building shelf order…")
          self.v_status.set("Building…")
          result = build_once(cfg, self._log)
          self.result_q.put(result)
          self._last_built_at = time.time()
          self._log(f"Build complete. Items: {len(result.rows_sorted)}")
          self.v_status.set(f"Built {len(result.rows_sorted)} items. Polling every {cfg.poll_seconds}s")
        else:
          if count != self._last_count:
            self._log(f"Collection changed: {self._last_count} → {count}")
            self._last_count = count
            self._log("Rebuilding shelf order…")
            self.v_status.set("Rebuilding…")
            result = build_once(cfg, self._log)
            self.result_q.put(result)
            self._last_built_at = time.time()
            self._log(f"Build complete. Items: {len(result.rows_sorted)}")
            self.v_status.set(f"Built {len(result.rows_sorted)} items. Polling every {cfg.poll_seconds}s")
          else:
            self.v_status.set(f"No changes. Polling every {cfg.poll_seconds}s")

      except Exception as e:
        self._log(f"Error: {e}")
        self._log(traceback.format_exc())
        self.v_status.set("Error (see Log tab).")

      # Wait for next poll or manual refresh
      self._wake.clear()
      self._wake.wait(timeout=cfg.poll_seconds)

    self._log("Watcher stopped.")


def main() -> None:
  if ttkb is not None:
    # Modern-looking theme/colors (optional dependency).
    root = ttkb.Window(themename="flatly")
  else:
    root = Tk()
  try:
    root.call("tk", "scaling", 1.2)
  except Exception:
    pass
  App(root)
  root.mainloop()


if __name__ == "__main__":
  main()
