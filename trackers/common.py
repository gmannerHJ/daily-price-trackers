"""Shared helpers for the daily trackers. Standard library only."""
import datetime
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# The repository that publishes this dataset; inside GitHub Actions it is taken from the
# environment, so a fork or a renamed repository identifies itself correctly.
REPOSITORY = os.environ.get("GITHUB_REPOSITORY") or "gmannerHJ/daily-price-trackers"
USER_AGENT = os.environ.get(
    "TRACKER_USER_AGENT",
    "daily-price-trackers/1.0 (daily snapshot of public price lists; +https://github.com/%s)" % REPOSITORY,
)
ARCHIVE_AFTER_DAYS = 30  # older daily snapshots are stored gzip-compressed
LOOKBACK_SNAPSHOTS = 14  # how far back to look for a source that missed days
MIN_INTERVAL = 1.0  # seconds between requests to the same host
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("TRACKER_DATA_DIR") or os.path.join(ROOT, "data")

_last_request = {}


def http_get_json(url, timeout=60, retries=2):
    """GET a JSON document, at most one request per second per host."""
    host = urllib.parse.urlsplit(url).netloc
    last_error = None
    for attempt in range(retries + 1):
        wait = _last_request.get(host, 0) + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        if attempt:
            time.sleep(5 * attempt)
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            _last_request[host] = time.monotonic()
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            _last_request[host] = time.monotonic()
            last_error = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            _last_request[host] = time.monotonic()
            last_error = exc
    raise last_error


def utc_today():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(value):
    """Parse a number from the source; None when absent or not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def snapshot_dates(directory):
    """Dates that have a snapshot, as plain .jsonl or archived .jsonl.gz."""
    if not os.path.isdir(directory):
        return []
    out = set()
    for name in os.listdir(directory):
        for suffix in (".jsonl", ".jsonl.gz"):
            stem = name[: -len(suffix)] if name.endswith(suffix) else ""
            if len(stem) == 10 and stem[4] == "-" and stem[7] == "-" and stem.replace("-", "").isdigit():
                out.add(stem)
    return sorted(out)


def read_snapshot(directory, date):
    path = os.path.join(directory, date + ".jsonl")
    if os.path.exists(path):
        return read_jsonl(path)
    with gzip.open(path + ".gz", "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def archive_old_snapshots(directory, today, keep_plain_days=ARCHIVE_AFTER_DAYS):
    """Compress snapshots older than keep_plain_days so the checkout stays small."""
    limit = (
        datetime.date.fromisoformat(today) - datetime.timedelta(days=keep_plain_days)
    ).isoformat()
    for date in snapshot_dates(directory):
        path = os.path.join(directory, date + ".jsonl")
        if date < limit and os.path.exists(path):
            with open(path, "rb") as src, open(path + ".gz", "wb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as dst:
                    dst.write(src.read())
            os.remove(path)


def run_tracker(name, sources, key_fields, price_fields, sort_key, min_ratio=0.5):
    """Fetch every source, write today's snapshot, and update changes.jsonl.

    sources: {source_name: callable returning a list of row dicts}
    A source that fails, or returns fewer than min_ratio of the rows it had in
    the previous snapshot, is left out of today's snapshot and is not compared,
    so an outage is never recorded as a mass removal.
    """
    directory = os.path.join(DATA, name)
    today = utc_today()
    previous_dates = [d for d in snapshot_dates(directory) if d < today]
    previous_date = previous_dates[-1] if previous_dates else None
    # Each source is compared with the latest earlier snapshot that contains it,
    # so a source that was unreachable for a few days is compared across the gap.
    prev_by_source, prev_date_of = {}, {}
    for date in reversed(previous_dates[-LOOKBACK_SNAPSHOTS:]):
        if all(source in prev_by_source for source in sources):
            break
        found = {}
        for row in read_snapshot(directory, date):
            if row["source"] not in prev_by_source:
                found.setdefault(row["source"], {})[tuple(row[f] for f in key_fields)] = row
        for source, items in found.items():
            prev_by_source[source] = items
            prev_date_of[source] = date

    rows, status = [], {}
    for source, fetch in sources.items():
        try:
            got = fetch()
            seen, unique = set(), []
            for row in got:
                key = tuple(row[f] for f in key_fields)
                if key not in seen:
                    seen.add(key)
                    unique.append(row)
            before = len(prev_by_source.get(source, {}))
            if not unique:
                raise RuntimeError("source returned no usable rows")
            if before and len(unique) < before * min_ratio:
                raise RuntimeError(
                    "row count fell from %d to %d; treated as a source fault" % (before, len(unique))
                )
            rows.extend(unique)
            status[source] = {"ok": True, "rows": len(unique)}
        except Exception as exc:  # one failing source must not stop the others
            status[source] = {"ok": False, "rows": 0, "error": "%s: %s" % (type(exc).__name__, exc)}
            print("[%s] source %s failed: %s" % (name, source, status[source]["error"]))

    if not rows:
        raise SystemExit("[%s] every source failed; nothing written" % name)

    rows.sort(key=sort_key)
    write_jsonl(os.path.join(directory, today + ".jsonl"), rows)
    archive_old_snapshots(directory, today)

    changes = []
    cur_by_source = {}
    for row in rows:
        cur_by_source.setdefault(row["source"], {})[tuple(row[f] for f in key_fields)] = row
    for source in sorted(cur_by_source):
        if source not in prev_by_source:
            continue  # first appearance of a source is not a list of "new" items
        old, new = prev_by_source[source], cur_by_source[source]
        for key in sorted(new.keys() - old.keys(), key=repr):
            entry = {"date": today, "previous_date": prev_date_of[source], "type": "new"}
            entry.update(new[key])
            changes.append(entry)
        for key in sorted(old.keys() - new.keys(), key=repr):
            entry = {"date": today, "previous_date": prev_date_of[source], "type": "removed"}
            entry.update(old[key])
            changes.append(entry)
        for key in sorted(old.keys() & new.keys(), key=repr):
            for field in price_fields:
                if old[key].get(field) != new[key].get(field):
                    entry = {"date": today, "previous_date": prev_date_of[source], "type": "price_change"}
                    entry.update({f: new[key][f] for f in key_fields})
                    entry.update({"field": field, "old": old[key].get(field), "new": new[key].get(field)})
                    changes.append(entry)

    changes_path = os.path.join(directory, "changes.jsonl")
    kept = [c for c in read_jsonl(changes_path) if c.get("date") != today]
    write_jsonl(changes_path, kept + changes)

    runs_path = os.path.join(directory, "runs.jsonl")
    runs = [r for r in read_jsonl(runs_path) if r.get("date") != today]
    runs.append(
        {
            "date": today,
            "fetched_at": utc_now_iso(),
            "previous_date": previous_date,
            "rows": len(rows),
            "changes": len(changes),
            "sources": status,
        }
    )
    write_jsonl(runs_path, runs)

    counts = {}
    for change in changes:
        counts[change["type"]] = counts.get(change["type"], 0) + 1
    print(
        "[%s] %s: %d rows %s; previous snapshot %s; changes %s"
        % (name, today, len(rows), json.dumps(status, sort_keys=True), previous_date, json.dumps(counts, sort_keys=True))
    )
    return rows, changes, status
