#!/usr/bin/env python3
"""
bcache-top — Lightweight bcache file-access tracker
====================================================
Correlates filesystem inotify access events with bcache sysfs stats
to show which files/software are hottest in the cache.

Usage:
    sudo python3 bcache-top.py [MOUNT_POINT] [OPTIONS]

Requirements:
    pip install rich inotify-simple
"""

import os
import sys
import time
import signal
import threading
import argparse
import subprocess
from pathlib import Path
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Optional

# ── Dependency check ────────────────────────────────────────────────────────
try:
    import inotify_simple
except ImportError:
    print("Error: inotify-simple not found. Run: pip install inotify-simple")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    from rich.columns import Columns
    from rich.progress_bar import ProgressBar
    from rich import box
    from rich.style import Style
    from rich.align import Align
except ImportError:
    print("Error: rich not found. Run: pip install rich")
    sys.exit(1)

# ── Constants ────────────────────────────────────────────────────────────────
VERSION = "1.0.0"
REFRESH_HZ = 2  # TUI refresh rate
WINDOW_SECS = 60  # sliding window for rate calculation
MAX_WATCHES = 4096  # inotify watch limit guard
MAX_TOP = 30  # max rows in table
INOTIFY_FLAGS = (
    inotify_simple.flags.ACCESS
    | inotify_simple.flags.OPEN
    | inotify_simple.flags.CREATE
    | inotify_simple.flags.CLOSE_NOWRITE
)

# File-type categories by extension
CATEGORIES = {
    "db": {".db", ".sqlite", ".sqlite3", ".mdb", ".ldb", ".sst"},
    "media": {
        ".mp4",
        ".mkv",
        ".avi",
        ".mp3",
        ".flac",
        ".ogg",
        ".wav",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
    },
    "code": {
        ".py",
        ".js",
        ".ts",
        ".go",
        ".rs",
        ".c",
        ".cpp",
        ".h",
        ".java",
        ".rb",
        ".php",
        ".sh",
        ".bash",
    },
    "lib": {".so", ".dll", ".dylib", ".a"},
    "pkg": {".deb", ".rpm", ".tar", ".gz", ".bz2", ".xz", ".zip", ".zst"},
    "cfg": {".conf", ".cfg", ".ini", ".toml", ".yaml", ".yml", ".json", ".xml"},
    "log": {".log", ".out", ".err"},
    "vm": {".qcow2", ".vmdk", ".vdi", ".raw", ".img"},
    "doc": {".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".txt", ".md"},
}

CATEGORY_ICONS = {
    "db": "🗄️ ",
    "media": "🎬",
    "code": "🧑‍💻",
    "lib": "📦",
    "pkg": "📁",
    "cfg": "⚙️ ",
    "log": "📋",
    "vm": "💿",
    "doc": "📄",
    "other": "📎",
}

CATEGORY_COLORS = {
    "db": "cyan",
    "media": "magenta",
    "code": "green",
    "lib": "yellow",
    "pkg": "blue",
    "cfg": "bright_white",
    "log": "bright_black",
    "vm": "bright_cyan",
    "doc": "white",
    "other": "bright_black",
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def categorize(path: str) -> str:
    ext = Path(path).suffix.lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "other"


def human_bytes(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}P"


def human_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def shorten_path(path: str, max_len: int = 55) -> str:
    if len(path) <= max_len:
        return path
    parts = Path(path).parts
    # Keep last 3 components, prefix with ...
    tail = str(Path(*parts[-3:])) if len(parts) >= 3 else path[-max_len:]
    return "…/" + tail


# ── bcache sysfs reader ──────────────────────────────────────────────────────
class BcacheStats:
    """Reads live bcache metrics from /sys/block/bcache* and /sys/fs/bcache/"""

    def __init__(self):
        self.devices = self._discover_devices()

    def _discover_devices(self) -> list[str]:
        devs = []
        block = Path("/sys/block")
        if block.exists():
            devs = sorted(
                str(p) for p in block.iterdir() if p.name.startswith("bcache")
            )
        return devs

    def _read(self, path: str, default="") -> str:
        try:
            return Path(path).read_text().strip()
        except Exception:
            return default

    def _read_int(self, path: str) -> int:
        v = self._read(path, "0")
        # bcache reports values like "1.5G" sometimes — parse safely
        try:
            return int(v)
        except ValueError:
            return 0

    def get_device_stats(self, dev_path: str) -> dict:
        b = f"{dev_path}/bcache"
        st = f"{b}/stats_total"
        stats = {
            "device": Path(dev_path).name,
            "state": self._read(f"{b}/state", "unknown"),
            "hits": self._read_int(f"{st}/cache_hits"),
            "misses": self._read_int(f"{st}/cache_misses"),
            "bypass_hits": self._read_int(f"{st}/bypassed"),
            "dirty_data": self._read(f"{b}/dirty_data", "0"),
            "writeback": self._read(f"{b}/writeback_running", "0"),
            "cache_mode": self._read(f"{b}/cache_mode", "unknown"),
        }
        total = stats["hits"] + stats["misses"]
        stats["hit_rate"] = (stats["hits"] / total * 100) if total else 0.0

        # Try to find the backing cache device
        cache_dir = Path(b) / "cache"
        if cache_dir.exists():
            for c in cache_dir.iterdir():
                stats["cache_size"] = self._read(
                    f"{c}/cache_replacement_policy", "unknown"
                )
                try:
                    avail = self._read(f"{c}/../cache_available_percent", "")
                    stats["avail_pct"] = int(avail) if avail else None
                except Exception:
                    stats["avail_pct"] = None
                break

        return stats

    def all_stats(self) -> list[dict]:
        # Re-discover in case devices appear/disappear
        self.devices = self._discover_devices()
        return [self.get_device_stats(d) for d in self.devices]

    def is_available(self) -> bool:
        return len(self.devices) > 0


# ── Process tracker ──────────────────────────────────────────────────────────
class ProcessTracker:
    """Maps open file paths to process names via /proc/*/fd"""

    def __init__(self):
        self._cache: dict[str, str] = {}
        self._last_refresh = 0.0
        self._ttl = 3.0  # seconds between full /proc scans

    def refresh(self, paths_of_interest: set[str]):
        now = time.monotonic()
        if now - self._last_refresh < self._ttl:
            return
        self._last_refresh = now
        new_cache: dict[str, str] = {}
        try:
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.name.isdigit():
                    continue
                try:
                    comm = (pid_dir / "comm").read_text().strip()
                    fd_dir = pid_dir / "fd"
                    for fd in fd_dir.iterdir():
                        try:
                            target = os.readlink(fd)
                            if target in paths_of_interest:
                                new_cache[target] = comm
                        except OSError:
                            pass
                except (PermissionError, FileNotFoundError, ProcessLookupError):
                    pass
        except Exception:
            pass
        self._cache = new_cache

    def get_proc(self, path: str) -> str:
        return self._cache.get(path, "")


# ── Access counter ────────────────────────────────────────────────────────────
class AccessCounter:
    """Thread-safe file access counter with sliding-window rate tracking."""

    def __init__(self, window_secs: int = WINDOW_SECS):
        self._lock = threading.Lock()
        self._total: defaultdict[str, int] = defaultdict(int)
        self._times: defaultdict[str, deque] = defaultdict(lambda: deque(maxlen=10_000))
        self._window = window_secs
        self.total_events = 0

    def record(self, path: str):
        now = time.monotonic()
        with self._lock:
            self._total[path] += 1
            self._times[path].append(now)
            self.total_events += 1

    def _rate(self, path: str, now: float) -> float:
        """Accesses per second over the sliding window."""
        times = self._times[path]
        cutoff = now - self._window
        # Count events within window
        count = sum(1 for t in times if t >= cutoff)
        return count / self._window

    def top_files(self, n: int = MAX_TOP) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            files = []
            for path, total in self._total.items():
                rate = self._rate(path, now)
                files.append(
                    {
                        "path": path,
                        "total": total,
                        "rate": rate,
                        "cat": categorize(path),
                    }
                )
            files.sort(key=lambda x: x["total"], reverse=True)
            return files[:n]

    def reset(self):
        with self._lock:
            self._total.clear()
            self._times.clear()
            self.total_events = 0


# ── inotify watcher ──────────────────────────────────────────────────────────
class FileWatcher:
    """
    Recursively watches a mount point with inotify for ACCESS/OPEN events.
    Maps watch descriptors back to paths for event attribution.
    """

    def __init__(
        self,
        mount: str,
        counter: AccessCounter,
        max_watches: int = MAX_WATCHES,
        max_depth: int = 8,
    ):
        self.mount = str(Path(mount).resolve())
        self.counter = counter
        self.max_watches = max_watches
        self.max_depth = max_depth
        self._inotify = inotify_simple.INotify()
        self._wd_to_path: dict[int, str] = {}
        self._watch_count = 0
        self._running = False
        self._thread = None
        self.errors: list[str] = []

    def _add_watch(self, path: str) -> Optional[int]:
        if self._watch_count >= self.max_watches:
            return None
        try:
            wd = self._inotify.add_watch(path, INOTIFY_FLAGS)
            self._wd_to_path[wd] = path
            self._watch_count += 1
            return wd
        except PermissionError:
            return None
        except OSError as e:
            self.errors.append(f"watch error {path}: {e}")
            return None

    def _walk_and_watch(self):
        """Recursively add watches up to max_depth."""
        root = Path(self.mount)
        self._add_watch(str(root))

        def recurse(p: Path, depth: int):
            if depth > self.max_depth or self._watch_count >= self.max_watches:
                return
            try:
                for child in p.iterdir():
                    if child.is_symlink():
                        continue
                    if child.is_dir():
                        self._add_watch(str(child))
                        recurse(child, depth + 1)
            except (PermissionError, OSError):
                pass

        recurse(root, 1)

    def _event_loop(self):
        while self._running:
            try:
                events = self._inotify.read(timeout=500)
                for event in events:
                    if event.wd not in self._wd_to_path:
                        continue
                    if not event.name:
                        continue
                    parent = self._wd_to_path[event.wd]
                    full = os.path.join(parent, event.name)
                    # Only count file accesses (not directory traversal)
                    if not (event.mask & inotify_simple.flags.ISDIR):
                        self.counter.record(full)
                    # If a new dir is created, watch it
                    if (
                        event.mask & inotify_simple.flags.CREATE
                        and event.mask & inotify_simple.flags.ISDIR
                    ):
                        self._add_watch(full)
            except Exception:
                pass

    @property
    def watch_count(self) -> int:
        return self._watch_count

    def start(self):
        self._running = True
        self._walk_and_watch()
        self._thread = threading.Thread(target=self._event_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        self._inotify.close()


# ── TUI renderer ─────────────────────────────────────────────────────────────
def make_bcache_panel(bcache: BcacheStats) -> Panel:
    stats_list = bcache.all_stats()

    if not stats_list:
        return Panel(
            Text(
                "⚠  No bcache devices found in /sys/block/bcache*\n"
                "Running in filesystem-only mode — file access tracking is active.",
                style="yellow",
            ),
            title="[bold]bcache Status[/]",
            border_style="yellow",
        )

    rows = []
    for s in stats_list:
        hit_bar_filled = int(s["hit_rate"] / 5)  # 0-20 blocks
        hit_bar = "█" * hit_bar_filled + "░" * (20 - hit_bar_filled)
        hr_color = (
            "green"
            if s["hit_rate"] >= 80
            else "yellow"
            if s["hit_rate"] >= 50
            else "red"
        )

        state_icon = {"clean": "✅", "dirty": "🟡", "inconsistent": "🔴"}.get(
            s["state"], "❓"
        )

        rows.append(
            f"  [{hr_color}]{s['device']}[/]  {state_icon} {s['state']}  "
            f"Mode: [cyan]{s['cache_mode']}[/]  "
            f"Hit rate: [{hr_color}]{s['hit_rate']:.1f}%[/] [{hr_color}]{hit_bar}[/]  "
            f"Hits: [green]{human_count(s['hits'])}[/]  "
            f"Misses: [red]{human_count(s['misses'])}[/]  "
            f"Dirty: [yellow]{s['dirty_data']}[/]"
            + (
                f"  Cache avail: {s['avail_pct']}%"
                if s.get("avail_pct") is not None
                else ""
            )
        )

    content = "\n".join(rows)
    return Panel(
        content,
        title="[bold bright_blue]⚡ bcache[/]",
        border_style="bright_blue",
        padding=(0, 1),
    )


def make_files_table(
    files: list[dict],
    proc_tracker: ProcessTracker,
    window: int,
    total_events: int,
    sort_by: str = "total",
) -> Table:

    table = Table(
        box=box.SIMPLE_HEAD,
        border_style="bright_black",
        header_style="bold bright_white",
        show_footer=False,
        expand=True,
    )

    table.add_column("#", style="bright_black", width=4, justify="right")
    table.add_column("Type", style="white", width=7)
    table.add_column("File Path", min_width=30, no_wrap=True)
    table.add_column("Process", style="cyan", width=16, no_wrap=True)
    table.add_column(f"Rate/{window}s", justify="right", width=10)
    table.add_column("Total", justify="right", width=8)
    table.add_column("Accesses", width=22)

    if not files:
        table.add_row(
            "",
            "",
            "[bright_black italic]Watching for file accesses…[/]",
            "",
            "",
            "",
            "",
        )
        return table

    max_total = max(f["total"] for f in files) or 1

    for i, f in enumerate(files, 1):
        cat = f["cat"]
        icon = CATEGORY_ICONS.get(cat, "📎")
        color = CATEGORY_COLORS.get(cat, "white")
        proc = proc_tracker.get_proc(f["path"])

        bar_len = 18
        filled = max(1, int(f["total"] / max_total * bar_len))
        bar = f"[{color}]{'█' * filled}[/][bright_black]{'░' * (bar_len - filled)}[/]"

        rate_str = f"{f['rate']:.1f}/s" if f["rate"] >= 0.1 else "<0.1/s"
        rate_color = (
            "green"
            if f["rate"] >= 5
            else "yellow"
            if f["rate"] >= 1
            else "bright_black"
        )

        table.add_row(
            str(i),
            f"[{color}]{icon} {cat[:4]}[/]",
            f"[{color}]{shorten_path(f['path'])}[/]",
            f"[cyan]{proc}[/]" if proc else "[bright_black]—[/]",
            f"[{rate_color}]{rate_str}[/]",
            f"[bold]{human_count(f['total'])}[/]",
            bar,
        )

    return table


def make_header(
    watcher: FileWatcher, counter: AccessCounter, mount: str, start_time: float
) -> Text:
    elapsed = int(time.monotonic() - start_time)
    h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
    uptime = f"{h:02d}:{m:02d}:{s:02d}"

    t = Text()
    t.append(" bcache-top ", style="bold black on bright_blue")
    t.append(f" v{VERSION} ", style="bright_black")
    t.append("│ ", style="bright_black")
    t.append("Mount: ", style="bright_black")
    t.append(mount, style="bold white")
    t.append("  Watches: ", style="bright_black")
    t.append(str(watcher.watch_count), style="cyan")
    t.append(f"/{MAX_WATCHES}", style="bright_black")
    t.append("  Events: ", style="bright_black")
    t.append(human_count(counter.total_events), style="bold green")
    t.append("  Uptime: ", style="bright_black")
    t.append(uptime, style="yellow")
    t.append(f"  [{datetime.now().strftime('%H:%M:%S')}]", style="bright_black")
    return t


def make_legend() -> Text:
    t = Text()
    t.append(" [q] quit  ", style="bright_black")
    t.append("[r] reset  ", style="bright_black")
    t.append("[1] sort:total  ", style="bright_black")
    t.append("[2] sort:rate  ", style="bright_black")
    t.append("Tracking: ACCESS · OPEN · CREATE", style="bright_black italic")
    return t


# ── Main ─────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="bcache-top — file access tracker for bcache-backed filesystems",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sudo python3 bcache-top.py /mnt/data
  sudo python3 bcache-top.py /home --top 20 --window 30
  sudo python3 bcache-top.py / --depth 5 --max-watches 2048

Notes:
  • Requires read access to the mount point and /proc (run as root for full visibility)
  • inotify watches are added recursively up to --depth
  • bcache sysfs stats require /sys/block/bcache* to exist
        """,
    )
    p.add_argument(
        "mount", nargs="?", default="/", help="Mount point to watch (default: /)"
    )
    p.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of top files to display (default: 20)",
    )
    p.add_argument(
        "--window",
        type=int,
        default=60,
        help="Sliding window in seconds for rate calc (default: 60)",
    )
    p.add_argument(
        "--depth", type=int, default=8, help="Max directory depth to watch (default: 8)"
    )
    p.add_argument(
        "--max-watches",
        type=int,
        default=MAX_WATCHES,
        help=f"inotify watch limit guard (default: {MAX_WATCHES})",
    )
    p.add_argument(
        "--refresh",
        type=float,
        default=1 / REFRESH_HZ,
        help=f"TUI refresh interval seconds (default: {1 / REFRESH_HZ})",
    )
    p.add_argument(
        "--no-proc",
        action="store_true",
        help="Disable /proc scanning for process names",
    )
    return p.parse_args()


def main():
    args = parse_args()

    # Validate mount point
    mount = str(Path(args.mount).resolve())
    if not Path(mount).is_dir():
        print(f"Error: '{mount}' is not a directory.")
        sys.exit(1)

    console = Console()

    # Warn if not root
    if os.geteuid() != 0:
        console.print(
            "[yellow]Warning:[/] Running without root. "
            "Process tracking and some paths may be limited."
        )
        time.sleep(1)

    # Setup components
    counter = AccessCounter(window_secs=args.window)
    bcache = BcacheStats()
    proc_tracker = ProcessTracker()
    watcher = FileWatcher(
        mount, counter, max_watches=args.max_watches, max_depth=args.depth
    )

    console.print(
        f"[bold bright_blue]bcache-top[/] Starting watcher on [cyan]{mount}[/]…"
    )
    watcher.start()
    console.print(f"[green]✓[/] Watching [cyan]{watcher.watch_count}[/] directories")

    if not bcache.is_available():
        console.print("[yellow]⚠ No bcache devices detected — file tracking only[/]")

    start_time = time.monotonic()
    sort_by = "total"
    running = True

    def shutdown(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Non-blocking key input (best-effort on terminals) ────────────────────
    import select, tty, termios

    def check_keypress() -> Optional[str]:
        try:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setraw(fd)
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0)
                if r:
                    return sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass
        return None

    # ── Main render loop ──────────────────────────────────────────────────────
    with Live(
        console=console, refresh_per_second=1 / args.refresh, screen=True
    ) as live:
        while running:
            # Key handling
            key = check_keypress()
            if key == "q":
                break
            elif key == "r":
                counter.reset()
            elif key == "1":
                sort_by = "total"
            elif key == "2":
                sort_by = "rate"

            # Gather data
            top_files = counter.top_files(n=args.top)
            if top_files and not args.no_proc:
                paths = {f["path"] for f in top_files}
                proc_tracker.refresh(paths)

            # Sort
            if sort_by == "rate":
                top_files.sort(key=lambda x: x["rate"], reverse=True)

            # Build layout
            layout = Layout()
            layout.split_column(
                Layout(name="header", size=1),
                Layout(name="bcache", size=4),
                Layout(name="files", ratio=1),
                Layout(name="legend", size=1),
            )

            layout["header"].update(make_header(watcher, counter, mount, start_time))
            layout["bcache"].update(make_bcache_panel(bcache))
            layout["legend"].update(make_legend())

            sort_label = (
                "total accesses" if sort_by == "total" else f"rate/{args.window}s"
            )
            files_panel = Panel(
                make_files_table(
                    top_files, proc_tracker, args.window, counter.total_events, sort_by
                ),
                title=f"[bold]🔥 Hot Files[/]  [bright_black]sorted by {sort_label}[/]",
                border_style="bright_blue",
                padding=(0, 0),
            )
            layout["files"].update(files_panel)

            live.update(layout)
            time.sleep(args.refresh)

    watcher.stop()
    console.print("\n[bold bright_blue]bcache-top[/] exited. Goodbye.")


if __name__ == "__main__":
    main()
