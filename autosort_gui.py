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

import queue
import subprocess
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from tkinter import Tk, StringVar, BooleanVar, IntVar, ttk, filedialog, messagebox

import discogs_app as core


POLL_SECONDS_DEFAULT = 300  # 5 minutes


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

  rows_sorted = core.sort_rows(rows, "title")
  lines = core.generate_txt_lines(rows_sorted, dividers=False, align=False, show_country=False)
  return BuildResult(username=username, rows_sorted=rows_sorted, lines=lines)


class App:
  def __init__(self, root: Tk) -> None:
    self.root = root
    root.title("Discogs Auto-Sort")

    self.v_token = StringVar(value="")
    self.v_user_agent = StringVar(value="VinylSorter/1.0 (+contact)")
    self.v_output_dir = StringVar(value=str(Path.cwd()))
    self.v_per_page = IntVar(value=100)
    self.v_json = BooleanVar(value=False)
    self.v_poll = IntVar(value=POLL_SECONDS_DEFAULT)

    self.v_search = StringVar(value="")
    self.v_match = StringVar(value="")

    # Holds the most recent build for export/printing
    self._last_result: BuildResult | None = None
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
    pad = {"padx": 6, "pady": 4}

    frm = ttk.Frame(root)
    frm.grid(row=0, column=0, sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)

    row = 0
    ttk.Label(frm, text="Token (optional)").grid(row=row, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=self.v_token, width=44).grid(row=row, column=1, sticky="ew", **pad)
    row += 1

    ttk.Label(frm, text="User-Agent").grid(row=row, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=self.v_user_agent, width=44).grid(row=row, column=1, sticky="ew", **pad)
    row += 1

    out_row = ttk.Frame(frm)
    out_row.grid(row=row, column=0, columnspan=2, sticky="ew", **pad)
    out_row.columnconfigure(1, weight=1)
    ttk.Label(out_row, text="Output Dir").grid(row=0, column=0, sticky="w")
    ttk.Entry(out_row, textvariable=self.v_output_dir).grid(row=0, column=1, sticky="ew", padx=4)
    ttk.Button(out_row, text="Browse", command=self._choose_dir).grid(row=0, column=2, sticky="e")
    row += 1

    opt = ttk.Frame(frm)
    opt.grid(row=row, column=0, columnspan=2, sticky="ew", **pad)
    ttk.Label(opt, text="Poll seconds").grid(row=0, column=0, sticky="w")
    ttk.Spinbox(opt, from_=15, to=3600, textvariable=self.v_poll, width=8).grid(row=0, column=1, padx=6)
    ttk.Checkbutton(opt, text="Also JSON", variable=self.v_json).grid(row=0, column=2, padx=6, sticky="w")
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
    ttk.Button(btn, text="Refresh Now", command=self._refresh_now).grid(row=0, column=0, padx=4)
    ttk.Button(btn, text="Export TXT/CSV", command=self._export_files).grid(row=0, column=1, padx=4)
    ttk.Button(btn, text="Print…", command=self._print_current).grid(row=0, column=2, padx=4)
    ttk.Button(btn, text="Stop", command=self._stop_app).grid(row=0, column=3, padx=4)
    row += 1

    nb = ttk.Notebook(frm)
    nb.grid(row=row, column=0, columnspan=2, sticky="nsew", **pad)
    frm.rowconfigure(row, weight=1)

    import tkinter as tk

    order_fr = ttk.Frame(nb)
    nb.add(order_fr, text="Shelf Order")
    order_fr.rowconfigure(0, weight=1)
    order_fr.columnconfigure(0, weight=1)
    self.order_text = tk.Text(order_fr, height=18, width=90)
    self.order_text.grid(row=0, column=0, sticky="nsew")
    self.order_text.tag_configure("search_match", background="#fff3b0")

    log_fr = ttk.Frame(nb)
    nb.add(log_fr, text="Log")
    log_fr.rowconfigure(0, weight=1)
    log_fr.columnconfigure(0, weight=1)
    self.log = tk.Text(log_fr, height=18, width=90)
    self.log.grid(row=0, column=0, sticky="nsew")

  def _choose_dir(self) -> None:
    directory = filedialog.askdirectory(initialdir=self.v_output_dir.get() or str(Path.cwd()))
    if directory:
      self.v_output_dir.set(directory)

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
      return

    # Always show the full list; search only highlights matches.
    self.order_text.insert("end", "\n".join(result.lines) + "\n")
    self.order_text.see("1.0")
    self._highlight_search()

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
      user_agent=self.v_user_agent.get().strip() or "VinylSorter/1.0 (+contact)",
      output_dir=self.v_output_dir.get().strip() or str(Path.cwd()),
      per_page=max(1, min(int(self.v_per_page.get() or 100), 100)),
      write_json=bool(self.v_json.get()),
      poll_seconds=max(15, int(self.v_poll.get() or POLL_SECONDS_DEFAULT)),
    )

  def _refresh_now(self) -> None:
    # Wake the watcher and force immediate check
    self._log("Manual refresh requested.")
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
        f.write("\n".join(result.lines) + "\n")
        tmp_path = f.name
      subprocess.run(["lpr", tmp_path], check=True)
      self._log("Sent to printer via lpr.")
    except Exception as e:
      messagebox.showerror("Print", f"Printing failed: {e}")

  def _watch_loop(self) -> None:
    """Background thread: poll collection count; rebuild on change or manual refresh."""
    self._log("Watcher started.")
    while not self._stop.is_set():
      cfg = self._get_cfg()
      try:
        token = core.get_token(cfg.token or None)
        headers = core.discogs_headers(token, cfg.user_agent)
        ident = core.get_identity(headers)
        username = ident.get("username")
        if not username:
          raise RuntimeError("Could not determine username from token.")

        count = get_collection_count(headers, username)
        if self._last_count is None:
          self._last_count = count
          self._log(f"Initial collection count: {count}")
          # Build once on startup
          self._log("Building shelf order…")
          result = build_once(cfg, self._log)
          self.result_q.put(result)
          self._last_built_at = time.time()
          self._log(f"Build complete. Items: {len(result.rows_sorted)}")
        else:
          if count != self._last_count:
            self._log(f"Collection changed: {self._last_count} → {count}")
            self._last_count = count
            self._log("Rebuilding shelf order…")
            result = build_once(cfg, self._log)
            self.result_q.put(result)
            self._last_built_at = time.time()
            self._log(f"Build complete. Items: {len(result.rows_sorted)}")

      except Exception as e:
        self._log(f"Error: {e}")
        self._log(traceback.format_exc())

      # Wait for next poll or manual refresh
      self._wake.clear()
      self._wake.wait(timeout=cfg.poll_seconds)

    self._log("Watcher stopped.")


def main() -> None:
  root = Tk()
  try:
    root.call("tk", "scaling", 1.2)
  except Exception:
    pass
  App(root)
  root.mainloop()


if __name__ == "__main__":
  main()
