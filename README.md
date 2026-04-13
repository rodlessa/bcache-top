# bcache-top

A lightweight, real-time TUI for tracking which files and software are hottest on a [bcache](https://www.kernel.org/doc/html/latest/admin-guide/bcache.html)-backed filesystem.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

---

## The problem

bcache operates at the **block layer** — it has no concept of files, only sectors. The kernel exposes aggregate stats (hit rate, dirty data) via sysfs, but there's no native way to answer:

> _"Which files or applications are actually driving my cache workload?"_

**bcache-top** solves this by correlating **inotify filesystem access events** (VFS layer) with **bcache sysfs stats**, giving you a live ranked view of the hottest files on your system.

---

## Features

- 🔥 **Live top-N file ranking** by total accesses or access rate
- ⚡ **bcache sysfs panel** — hit rate, miss count, dirty data, cache mode, per-device
- 🧑‍💻 **Process attribution** — which process is reading each hot file (via `/proc/*/fd`)
- 📁 **File type categorization** — DB, media, code, libs, VMs, configs, logs, etc.
- 📊 **Sliding window rate** — configurable time window for access rate calculation
- 🔁 **Auto-watch new directories** as they're created
- ⌨️ **Keyboard controls** — reset counters, toggle sort, quit

---

## Requirements

- Linux (inotify is kernel-native)
- Python 3.10+
- bcache-backed filesystem (runs in filesystem-only mode without it)
- Root privileges recommended (for full `/proc` visibility)

```bash
pip install rich inotify-simple
```

---

## Usage

```bash
# Watch your bcache-backed mount point
sudo /path/to/venv/bin/python bcache-top.py /home

# More options
sudo /path/to/venv/bin/python bcache-top.py / --top 25 --window 30 --depth 6

# Disable process tracking (faster on large systems)
sudo /path/to/venv/bin/python bcache-top.py /mnt/data --no-proc
```

> **Note:** Always use the venv's Python binary with sudo — `sudo python` uses the system Python and won't see venv packages.
>
> ```bash
> sudo .venv/bin/python bcache-top.py /
> ```

---

## Options

| Flag            | Default | Description                                       |
| --------------- | ------- | ------------------------------------------------- |
| `mount`         | `/`     | Mount point to watch                              |
| `--top`         | `20`    | Number of top files to display                    |
| `--window`      | `60`    | Sliding window in seconds for rate calculation    |
| `--depth`       | `8`     | Max directory depth for recursive inotify watches |
| `--max-watches` | `4096`  | inotify watch limit guard                         |
| `--refresh`     | `0.5`   | TUI refresh interval in seconds                   |
| `--no-proc`     | off     | Disable `/proc` scanning for process names        |

---

## Keyboard Controls

| Key | Action                 |
| --- | ---------------------- |
| `q` | Quit                   |
| `r` | Reset all counters     |
| `1` | Sort by total accesses |
| `2` | Sort by access rate    |

---

## How it works

```
┌─────────────────────────────────────────────────────┐
│  Filesystem (ext4 / xfs / btrfs / etc.)             │
│    └── inotify → ACCESS, OPEN, CREATE events        │
│          └── AccessCounter (sliding window)         │
├─────────────────────────────────────────────────────┤
│  bcache (block layer)                               │
│    └── /sys/block/bcache*/bcache/stats_total/       │
│          └── hits, misses, dirty_data, state        │
└─────────────────────────────────────────────────────┘
         ↓ correlated and displayed in Rich TUI
```

1. **inotify** is set up recursively on the mount point, watching for `IN_ACCESS`, `IN_OPEN`, and `IN_CREATE` flags on files (not directories).
2. Every file access event increments a counter and timestamps the event for rate calculation.
3. A background thread periodically scans `/proc/*/fd` to attribute open files to process names.
4. bcache stats are read from sysfs on every refresh cycle.
5. The Rich TUI renders the top-N files table and bcache panel at the configured refresh rate.

---

## inotify watch limits

For large filesystems, you may need to raise the system watch limit:

```bash
# Check current limit
cat /proc/sys/fs/inotify/max_user_watches

# Raise temporarily
echo 32768 | sudo tee /proc/sys/fs/inotify/max_user_watches

# Raise permanently
echo "fs.inotify.max_user_watches=32768" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

---

## File type categories

| Category | Extensions                                         |
| -------- | -------------------------------------------------- |
| `db`     | `.db`, `.sqlite`, `.sqlite3`, `.sst`, `.ldb`       |
| `lib`    | `.so`, `.dll`, `.dylib`, `.a`                      |
| `media`  | `.mp4`, `.mkv`, `.mp3`, `.flac`, `.jpg`, `.png`, … |
| `code`   | `.py`, `.js`, `.go`, `.rs`, `.c`, `.sh`, …         |
| `vm`     | `.qcow2`, `.vmdk`, `.vdi`, `.raw`, `.img`          |
| `cfg`    | `.conf`, `.yaml`, `.toml`, `.json`, `.ini`, …      |
| `log`    | `.log`, `.out`, `.err`                             |
| `pkg`    | `.deb`, `.rpm`, `.tar`, `.gz`, `.zip`, …           |
| `doc`    | `.pdf`, `.docx`, `.xlsx`, `.md`, `.txt`            |

---

## Limitations

- Tracks **access events**, not actual cached blocks — files that are accessed most are the ones driving bcache, but the tool cannot directly inspect bcache's internal LRU/LFU state.
- inotify is **filesystem-level**, so it only sees files on the watched mount point. Accesses via direct I/O or O_DIRECT bypass the page cache and won't appear in bcache hits.
- Process attribution via `/proc/*/fd` is best-effort and may miss short-lived processes.

---

## License

MIT
