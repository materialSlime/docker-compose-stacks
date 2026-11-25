"""Disk report collector.

Every INTERVAL seconds:
  * size of /vault (capacity / used / free) and of each top-level folder (du)
  * Google Drive quota (rclone about) and size of each top-level folder (rclone size)
  * keeps its own history (STATE file, KEEP_DAYS) to compute growth rates and
    "days until full" with a linear fit over the last FIT_DAYS days
Outputs:
  * Prometheus metrics on :PORT/metrics
  * JSON summary for Home Assistant at OUT_JSON
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VAULT = os.environ.get("VAULT", "/vault")
REMOTES = [r for r in os.environ.get("REMOTES", "gdrive:").split(",") if r]
INTERVAL = int(os.environ.get("INTERVAL", "21600"))
STATE = os.environ.get("STATE", "/data/history.json")
OUT_JSON = os.environ.get("OUT_JSON", "/ha/summary.json")
PORT = int(os.environ.get("PORT", "9105"))
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "730"))
FIT_DAYS = int(os.environ.get("FIT_DAYS", "30"))
SKIP = {"lost+found"}
DAY = 86400

_metrics = "# no scan finished yet\n"
_history = []  # same list main() loads and scan_once() appends to; served on /history


def history_rows(loc, kind):
    """Flat rows for Grafana (Infinity plugin) from the full, never-expiring history.
    kind=totals -> {time, used, free, capacity}; kind=dirs -> {time, dir, bytes} (long format)."""
    rows = []
    for s in list(_history):
        d = s["locations"].get(loc)
        if not d:
            continue
        t = datetime.fromtimestamp(s["ts"], timezone.utc).isoformat(timespec="seconds")
        if kind == "dirs":
            rows += [{"time": t, "dir": k, "bytes": v} for k, v in sorted(d["dirs"].items())]
        else:
            rows.append({"time": t, "used": d["used"], "free": d["free"], "capacity": d["capacity"]})
    return rows


def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def run(cmd, timeout=1800):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


# ---------------------------------------------------------------- scanning
def scan_vault():
    st = os.statvfs(VAULT)
    cap = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    dirs = {}
    for name in sorted(os.listdir(VAULT)):
        path = os.path.join(VAULT, name)
        if name in SKIP or os.path.islink(path) or not os.path.isdir(path):
            continue
        rc, out, err = run(["du", "-sbx", path])
        if out.strip():
            dirs[name] = int(out.split()[0])
        if rc != 0:
            log(f"du {name}: exit {rc} (partial size) {err.strip()[:200]}")
    return {"capacity": cap, "used": used, "free": free, "dirs": dirs}


def rclone_json(args):
    rc, out, err = run(["rclone", *args, "--json"], timeout=1800)
    if rc != 0:
        raise RuntimeError(f"rclone {' '.join(args)}: {err.strip()[:300]}")
    return json.loads(out)


def scan_remote(remote):
    about = rclone_json(["about", remote])
    rc, out, err = run(["rclone", "lsf", remote, "--dirs-only", "--max-depth", "1"], timeout=600)
    if rc != 0:
        raise RuntimeError(f"rclone lsf {remote}: {err.strip()[:300]}")
    dirs = {}
    for d in [x.rstrip("/") for x in out.splitlines() if x.strip()]:
        dirs[d] = rclone_json(["size", f"{remote}{d}", "--fast-list"])["bytes"]
    root_files = rclone_json(["size", remote, "--max-depth", "1"])["bytes"]  # depth 1 = files in root only
    if root_files:
        dirs["(files in root)"] = root_files
    if about.get("trashed"):
        dirs["(trash)"] = about["trashed"]
    cap = about.get("total") or 0
    used = about.get("used", 0) + about.get("trashed", 0) + about.get("other", 0)
    free = about.get("free", max(cap - used, 0))
    return {"capacity": cap, "used": used, "free": free, "dirs": dirs}


# ---------------------------------------------------------------- history maths
def load_history():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_history(h):
    cutoff = time.time() - KEEP_DAYS * DAY
    h[:] = [s for s in h if s["ts"] >= cutoff]
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(h, f)
    os.replace(tmp, STATE)


def series(h, loc, key, dirname=None):
    pts = []
    for s in h:
        d = s["locations"].get(loc)
        if not d:
            continue
        v = d["dirs"].get(dirname) if dirname else d.get(key)
        if v is not None:
            pts.append((s["ts"], v))
    return pts


def rate(pts, window_days):
    """bytes/day over roughly the last `window_days` (None if not enough history)."""
    if len(pts) < 2:
        return None
    t_now, v_now = pts[-1]
    target = t_now - window_days * DAY
    older = [p for p in pts if p[0] <= t_now - window_days * DAY * 0.5]
    if not older:
        return None
    t0, v0 = min(older, key=lambda p: abs(p[0] - target))
    span = (t_now - t0) / DAY
    return (v_now - v0) / span if span > 0 else None


def fit_slope(pts, days):
    """least-squares slope (bytes/day) over the last `days`."""
    t_end = pts[-1][0] if pts else 0
    p = [(t, v) for t, v in pts if t >= t_end - days * DAY]
    if len(p) < 3 or (p[-1][0] - p[0][0]) < 2 * DAY:
        return None
    n = len(p)
    mt = sum(t for t, _ in p) / n
    mv = sum(v for _, v in p) / n
    den = sum((t - mt) ** 2 for t, _ in p)
    if den == 0:
        return None
    return sum((t - mt) * (v - mv) for t, v in p) / den * DAY


# ---------------------------------------------------------------- outputs
def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def build(h, durations, errors):
    now = h[-1]["ts"]
    latest = h[-1]["locations"]
    summary = {"updated": datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(timespec="seconds"),
               "interval_hours": INTERVAL / 3600, "errors": errors, "locations": {}}
    m = []

    def metric(name, help_, typ="gauge"):
        m.append(f"# HELP {name} {help_}\n# TYPE {name} {typ}")

    rows = {k: [] for k in ("capacity", "used", "free", "g1", "g7", "g30", "days", "ts", "dur", "dir", "dg1", "dg7", "dg30")}
    for loc, d in latest.items():
        L = f'location="{esc(loc)}"'
        used_pts = series(h, loc, "used")
        g1, g7, g30 = rate(used_pts, 1), rate(used_pts, 7), rate(used_pts, 30)
        slope = fit_slope(series(h, loc, "free"), FIT_DAYS)
        days = d["free"] / -slope if slope is not None and slope < 0 else None
        info = {"capacity": d["capacity"], "used": d["used"], "free": d["free"],
                # same as df: reserved blocks are neither used nor available
                "used_pct": round(100 * d["used"] / (d["used"] + d["free"]), 1) if d["used"] + d["free"] else None,
                "growth_per_day": {"1d": g1, "7d": g7, "30d": g30},
                "days_until_full": round(days, 1) if days is not None else None,
                "full_date": datetime.fromtimestamp(now + days * DAY).date().isoformat() if days is not None and days < 36500 else None,
                "scanned": datetime.fromtimestamp(h[-1]["ts"]).astimezone().isoformat(timespec="seconds"),
                "dirs": []}
        rows["capacity"].append(f"disk_report_capacity_bytes{{{L}}} {d['capacity']}")
        rows["used"].append(f"disk_report_used_bytes{{{L}}} {d['used']}")
        rows["free"].append(f"disk_report_free_bytes{{{L}}} {d['free']}")
        for key, g in (("g1", g1), ("g7", g7), ("g30", g30)):
            if g is not None:
                rows[key].append(f"disk_report_growth_{key[1:]}d_bytes_per_day{{{L}}} {g:.0f}")
        if days is not None:
            rows["days"].append(f"disk_report_days_until_full{{{L}}} {days:.1f}")
        rows["ts"].append(f"disk_report_last_scan_timestamp_seconds{{{L}}} {now:.0f}")
        if loc in durations:
            rows["dur"].append(f"disk_report_scan_duration_seconds{{{L}}} {durations[loc]:.1f}")
        for name, size in d["dirs"].items():
            D = f'{L},dir="{esc(name)}"'
            pts = series(h, loc, None, name)
            dg = {w: rate(pts, w) for w in (1, 7, 30)}
            info["dirs"].append({"name": name, "bytes": size, "growth_1d": dg[1], "growth_7d": dg[7], "growth_30d": dg[30]})
            rows["dir"].append(f"disk_report_dir_bytes{{{D}}} {size}")
            for w in (1, 7, 30):
                if dg[w] is not None:
                    rows[f"dg{w}"].append(f"disk_report_dir_growth_{w}d_bytes_per_day{{{D}}} {dg[w]:.0f}")
        info["dirs"].sort(key=lambda x: (x["growth_7d"] if x["growth_7d"] is not None else float("-inf"), x["bytes"]), reverse=True)
        summary["locations"][loc] = info

    meta = [("capacity", "disk_report_capacity_bytes", "Total capacity"),
            ("used", "disk_report_used_bytes", "Used space"),
            ("free", "disk_report_free_bytes", "Free space available"),
            ("g1", "disk_report_growth_1d_bytes_per_day", "Growth of used space, last ~1 day"),
            ("g7", "disk_report_growth_7d_bytes_per_day", "Growth of used space, last ~7 days"),
            ("g30", "disk_report_growth_30d_bytes_per_day", "Growth of used space, last ~30 days"),
            ("days", "disk_report_days_until_full", f"Days until full (linear fit over {FIT_DAYS} days)"),
            ("ts", "disk_report_last_scan_timestamp_seconds", "Unix time of last successful scan"),
            ("dur", "disk_report_scan_duration_seconds", "Duration of last scan"),
            ("dir", "disk_report_dir_bytes", "Size of a top-level folder"),
            ("dg1", "disk_report_dir_growth_1d_bytes_per_day", "Folder growth, last ~1 day"),
            ("dg7", "disk_report_dir_growth_7d_bytes_per_day", "Folder growth, last ~7 days"),
            ("dg30", "disk_report_dir_growth_30d_bytes_per_day", "Folder growth, last ~30 days")]
    for key, name, help_ in meta:
        if rows[key]:
            metric(name, help_)
            m.extend(rows[key])
    metric("disk_report_scan_errors", "Locations that failed in the last scan")
    m.append(f"disk_report_scan_errors {len(errors)}")
    return "\n".join(m) + "\n", summary


def write_json(summary):
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w") as f:
        json.dump(summary, f, indent=1)
    os.replace(tmp, OUT_JSON)


# ---------------------------------------------------------------- main loop
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/history":
            q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            body = json.dumps(history_rows(q.get("loc", "vault"), q.get("kind", "totals"))).encode()
            ctype = "application/json"
        elif path in ("/metrics", "/"):
            body = _metrics.encode()
            ctype = "text/plain; version=0.0.4"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def scan_once(h):
    global _metrics
    locs, durations, errors = {}, {}, []
    t = time.time()
    try:
        locs["vault"] = scan_vault()
    except Exception as e:  # noqa: BLE001
        errors.append(f"vault: {e}")
    durations["vault"] = time.time() - t
    for r in REMOTES:
        name = r.rstrip(":")
        t = time.time()
        try:
            locs[name] = scan_remote(r)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
        durations[name] = time.time() - t
    if errors:
        log("errors:", errors)
    if not locs:
        return
    # keep last known values for a location that failed this time
    if h:
        for loc, d in h[-1]["locations"].items():
            locs.setdefault(loc, d)
    h.append({"ts": time.time(), "locations": locs})
    save_history(h)
    _metrics, summary = build(h, durations, errors)
    write_json(summary)
    log("scan done:", {k: f"{v['free'] / 1e9:.0f} GB free" for k, v in locs.items()},
        "durations:", {k: f"{v:.0f}s" for k, v in durations.items()})


def main():
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    h = load_history()
    _history[:] = h
    h = _history
    if h:
        global _metrics
        _metrics, _ = build(h, {}, [])
    threading.Thread(target=ThreadingHTTPServer(("", PORT), Handler).serve_forever, daemon=True).start()
    log(f"collector up: vault={VAULT} remotes={REMOTES} every {INTERVAL / 3600:g}h, {len(h)} samples in history")
    while True:
        start = time.time()
        try:
            scan_once(h)
        except Exception as e:  # noqa: BLE001
            log("scan failed:", e)
        time.sleep(max(60, INTERVAL - (time.time() - start)))


if __name__ == "__main__":
    main()
