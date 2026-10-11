"""Build the static site in docs/ from the data in data/. Standard library only.

    python build_pages.py [--base-url https://OWNER.github.io/REPO/] [--out docs]

The base URL is needed for sitemap.xml, feed.xml and canonical links. When it is
not given it is derived from the repository name: GITHUB_REPOSITORY inside GitHub
Actions, otherwise the default in trackers/common.py.
"""
import argparse
import csv
import html
import io
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "trackers"))
import common  # noqa: E402

SITE_TITLE = "Daily price trackers"
SITE_DESCRIPTION = (
    "Daily snapshots of public language-model API prices and GPU rental prices, "
    "with a dated record of every change."
)
PIPELINE_NOTE = (
    "This site is produced by an automated pipeline run by one person with AI agents. "
    "Nothing is edited by hand. Check the provider's own price page before relying on a number."
)
MAX_CHANGE_ROWS = 2000

TRACKERS = [
    {
        "name": "llm_prices",
        "slug": "llm-prices",
        "title": "Language-model API prices",
        "description": "List prices per million tokens for language models, by provider, recorded once a day.",
        "group_field": "provider",
        "group_label": "Provider",
        "columns": [
            ("model_id", "Model"),
            ("context_length", "Context (tokens)"),
            ("prompt_usd_per_mtok", "Prompt USD / 1M tokens"),
            ("completion_usd_per_mtok", "Completion USD / 1M tokens"),
            ("modality", "Modality"),
        ],
        "fields": [
            "source", "provider", "model_id", "context_length",
            "prompt_usd_per_mtok", "completion_usd_per_mtok", "modality",
        ],
        "key_fields": ["source", "provider", "model_id"],
        "sources": {
            "litellm": ("LiteLLM price list", "https://github.com/BerriAI/litellm"),
            "models.dev": ("models.dev", "https://models.dev/"),
        },
    },
    {
        "name": "gpu_rental_prices",
        "slug": "gpu-rental-prices",
        "title": "GPU rental prices",
        "description": "Hourly list prices for rented GPU instances, on-demand and interruptible, recorded once a day.",
        "group_field": None,
        "group_label": None,
        "columns": [
            ("instance_type", "Instance type"),
            ("gpu_model", "GPU"),
            ("gpu_count", "GPUs"),
            ("vram_gb", "VRAM per GPU (GB)"),
            ("price_usd_per_hour", "USD / hour"),
            ("pricing", "Pricing"),
            ("region", "Region"),
        ],
        "fields": [
            "source", "provider", "instance_type", "gpu_model", "gpu_count", "vram_gb",
            "price_usd_per_hour", "pricing", "region",
        ],
        "key_fields": ["source", "instance_type", "pricing", "region"],
        "sources": {
            "azure": ("Azure Retail Prices API", "https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices"),
            "verda": ("Verda public instance types", "https://verda.com/"),
        },
    },
]

CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1a1a1a;--muted:#5f6368;--line:#d9dce1;--link:#0b57d0;--head:#f3f4f6}
@media (prefers-color-scheme:dark){:root{--bg:#121416;--fg:#e6e6e6;--muted:#9aa0a6;--line:#33373d;--link:#8ab4f8;--head:#1c1f23}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main,header,footer{max-width:1100px;margin:0 auto;padding:0 16px}
header{padding-top:16px}
nav a{margin-right:14px}
a{color:var(--link)}
h1{font-size:1.5rem;margin:.8em 0 .3em}
h2{font-size:1.15rem;margin:1.4em 0 .4em}
p{margin:.5em 0}
.muted{color:var(--muted);font-size:.9rem}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:6px;margin:.6em 0}
table{border-collapse:collapse;width:100%;font-size:.9rem;font-variant-numeric:tabular-nums}
th,td{padding:6px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{background:var(--head);position:sticky;top:0}
td.n,th.n{text-align:right}
tr:last-child td{border-bottom:0}
input[type=search]{width:100%;max-width:360px;padding:8px;font:inherit;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
ul.cols{columns:3 220px;padding-left:1.2em}
footer{border-top:1px solid var(--line);margin-top:2em;padding-top:1em;padding-bottom:2em}
"""

FILTER_JS = """
(function(){var q=document.getElementById('q');if(!q)return;
var rows=document.querySelectorAll('tbody tr');
q.addEventListener('input',function(){var v=q.value.toLowerCase();
for(var i=0;i<rows.length;i++){rows[i].style.display=rows[i].textContent.toLowerCase().indexOf(v)<0?'none':''}});})();
"""


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def fmt(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return ("%.6f" % value).rstrip("0").rstrip(".")
    return str(value)


def slugify(text, used):
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "x"
    base, n = slug, 2
    while slug in used:
        slug = "%s-%d" % (base, n)
        n += 1
    used.add(slug)
    return slug


class Site:
    def __init__(self, out, base_url, repo_url=""):
        self.out = out
        self.base_url = base_url
        self.repo_url = repo_url
        self.pages = []  # (relative path, last modified date)

    def write(self, rel, text):
        path = os.path.join(self.out, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def page(self, rel, title, description, body, lastmod, script=False):
        depth = rel.count("/")
        up = "../" * depth
        canonical = ""
        if self.base_url:
            canonical = '<link rel="canonical" href="%s">\n' % esc(self.url(rel))
        feed = ""
        if self.base_url:
            feed = '<link rel="alternate" type="application/atom+xml" title="Changes" href="%sfeed.xml">\n' % up
        nav = ['<a href="%s">Home</a>' % (up or "./")]
        for tracker in TRACKERS:
            nav.append('<a href="%s%s/">%s</a>' % (up, tracker["slug"], esc(tracker["title"])))
        full_title = title if title == SITE_TITLE else "%s - %s" % (title, SITE_TITLE)
        doc = (
            "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>%s</title>\n<meta name=\"description\" content=\"%s\">\n%s%s"
            "<style>%s</style>\n</head>\n<body>\n<header><nav>%s</nav></header>\n<main>\n%s\n</main>\n"
            "<footer class=\"muted\"><p>%s</p><p>Data: CC BY 4.0. Code: MIT. "
            "Sources and their licences are listed on the <a href=\"%s#sources\">home page</a>.%s</p></footer>\n%s"
            "</body>\n</html>\n"
        ) % (
            esc(full_title), esc(description), canonical, feed, CSS, " ".join(nav), body,
            esc(PIPELINE_NOTE), up or "./",
            ' <a href="%s">Source code and data</a>.' % esc(self.repo_url) if self.repo_url else "",
            "<script>%s</script>\n" % FILTER_JS if script else "",
        )
        self.write(rel, doc)
        self.pages.append((rel, lastmod))

    def url(self, rel):
        if rel.endswith("index.html"):
            rel = rel[: -len("index.html")]
        return self.base_url + rel


def table(columns, rows, numeric):
    head = "".join(
        '<th class="n">%s</th>' % esc(label) if field in numeric else "<th>%s</th>" % esc(label)
        for field, label in columns
    )
    body = []
    for row in rows:
        cells = "".join(
            '<td class="n">%s</td>' % esc(fmt(row.get(field))) if field in numeric
            else "<td>%s</td>" % esc(fmt(row.get(field)))
            for field, _ in columns
        )
        body.append("<tr>%s</tr>" % cells)
    return '<div class="scroll"><table><thead><tr>%s</tr></thead><tbody>\n%s\n</tbody></table></div>' % (
        head, "\n".join(body)
    )


def to_csv(fields, rows):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow([fmt(row.get(f)) for f in fields])
    return buf.getvalue()


NUMERIC = {
    "context_length", "prompt_usd_per_mtok", "completion_usd_per_mtok",
    "gpu_count", "vram_gb", "price_usd_per_hour", "old", "new", "rows",
}


def change_summary(changes):
    counts = {"price_change": 0, "new": 0, "removed": 0}
    for change in changes:
        counts[change["type"]] = counts.get(change["type"], 0) + 1
    return "price changes: %d, new: %d, removed: %d" % (counts["price_change"], counts["new"], counts["removed"])


def build_tracker(site, tracker):
    directory = os.path.join(common.DATA, tracker["name"])
    dates = common.snapshot_dates(directory)
    if not dates:
        return None
    first, latest = dates[0], dates[-1]
    rows = common.read_snapshot(directory, latest)
    changes = common.read_jsonl(os.path.join(directory, "changes.jsonl"))
    runs = {r["date"]: r for r in common.read_jsonl(os.path.join(directory, "runs.jsonl"))}
    run = runs.get(latest, {})
    slug = tracker["slug"]

    # Downloads
    site.write(slug + "/latest.csv", to_csv(tracker["fields"], rows))
    site.write(slug + "/latest.json", json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n")
    change_fields = ["date", "previous_date", "type"] + tracker["fields"] + ["field", "old", "new"]
    site.write(slug + "/changes.csv", to_csv(change_fields, changes))
    site.write(slug + "/changes.json", json.dumps(changes, ensure_ascii=False, separators=(",", ":")) + "\n")

    # Latest tables: one page per source, or per source and group
    by_source = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)
    listing = []
    for source in sorted(by_source):
        source_rows = by_source[source]
        label, link = tracker["sources"].get(source, (source, None))
        source_slug = slugify(source, set())
        listing.append("<h2>%s <span class=\"muted\">(%d rows)</span></h2>" % (esc(label), len(source_rows)))
        if tracker["group_field"]:
            groups = {}
            for row in source_rows:
                groups.setdefault(row[tracker["group_field"]], []).append(row)
            used, items = set(), []
            for group in sorted(groups, key=lambda g: str(g).lower()):
                rel = "%s/%s/%s.html" % (slug, source_slug, slugify(group, used))
                title = "%s: %s (%s)" % (tracker["title"], group, label)
                body = (
                    "<h1>%s</h1>\n<p class=\"muted\">%s %s, as listed by %s. Snapshot of %s (UTC). %d rows.</p>\n"
                    "<p><input type=\"search\" id=\"q\" placeholder=\"Filter rows\" aria-label=\"Filter rows\"></p>\n%s"
                ) % (
                    esc(title), esc(tracker["group_label"]), esc(group), esc(label), esc(latest),
                    len(groups[group]), table(tracker["columns"], groups[group], NUMERIC),
                )
                site.page(
                    rel, title,
                    "%s for %s as listed by %s, snapshot of %s." % (tracker["title"], group, label, latest),
                    body, latest, script=True,
                )
                items.append(
                    '<li><a href="%s">%s</a> <span class="muted">%d</span></li>'
                    % (esc(rel[len(slug) + 1:]), esc(group), len(groups[group]))
                )
            listing.append('<ul class="cols">%s</ul>' % "\n".join(items))
        else:
            rel = "%s/%s/index.html" % (slug, source_slug)
            title = "%s: %s" % (tracker["title"], label)
            body = (
                "<h1>%s</h1>\n<p class=\"muted\">Snapshot of %s (UTC). %d rows. Empty cells are values the source does not give.</p>\n"
                "<p><input type=\"search\" id=\"q\" placeholder=\"Filter rows\" aria-label=\"Filter rows\"></p>\n%s"
            ) % (esc(title), esc(latest), len(source_rows), table(tracker["columns"], source_rows, NUMERIC))
            site.page(rel, title, "%s from %s, snapshot of %s." % (tracker["title"], label, latest), body, latest, script=True)
            listing.append('<p><a href="%s/">Latest table (%d rows)</a></p>' % (esc(source_slug), len(source_rows)))

    # Changes page
    by_date = {}
    for change in changes:
        by_date.setdefault(change["date"], []).append(change)
    parts = ["<h1>%s: changes</h1>" % esc(tracker["title"])]
    if len(dates) < 2:
        parts.append(
            "<p>History starts on %s. That is the first snapshot, so there is nothing to compare it with yet. "
            "Changes are listed here from the second daily snapshot onward.</p>" % esc(first)
        )
    else:
        parts.append(
            "<p class=\"muted\">%d daily snapshots from %s to %s. Each day is compared with the previous snapshot. "
            "<a href=\"changes.csv\">CSV</a> · <a href=\"changes.json\">JSON</a></p>" % (len(dates), esc(first), esc(latest))
        )
        if not changes:
            parts.append("<p>No changes have been recorded since %s.</p>" % esc(first))
    change_columns = (
        [("type", "Type")] + [(f, f) for f in tracker["key_fields"]] + [("field", "Field"), ("old", "Old"), ("new", "New")]
    )
    shown = 0
    for date in sorted(by_date, reverse=True):
        day = by_date[date]
        parts.append('<h2 id="d%s">%s <span class="muted">%s</span></h2>' % (esc(date), esc(date), esc(change_summary(day))))
        if shown >= MAX_CHANGE_ROWS:
            continue
        display = []
        for change in day[: MAX_CHANGE_ROWS - shown]:
            item = dict(change)
            if change["type"] != "price_change":
                item["field"], item["old"], item["new"] = None, None, None
            display.append(item)
        shown += len(display)
        parts.append(table(change_columns, display, NUMERIC))
        if len(display) < len(day):
            parts.append('<p class="muted">%d more rows for this date are in the CSV and JSON files.</p>' % (len(day) - len(display)))
    site.page(
        slug + "/changes.html", tracker["title"] + ": changes",
        "Dated record of price changes, new entries and removed entries in the %s dataset." % tracker["title"].lower(),
        "\n".join(parts), latest,
    )

    # Tracker index
    source_notes = []
    for source, state in sorted((run.get("sources") or {}).items()):
        if not state.get("ok"):
            source_notes.append(
                "<p><strong>%s was not read in this snapshot</strong> (the source could not be fetched); its rows are absent rather than carried over.</p>"
                % esc(tracker["sources"].get(source, (source,))[0])
            )
    body = (
        "<h1>%s</h1>\n<p>%s</p>\n<p class=\"muted\">Latest snapshot: %s (UTC), fetched %s. %d rows. "
        "History starts %s; %d daily snapshot%s so far.</p>\n%s"
        "<p><a href=\"changes.html\">Changes by date</a> · <a href=\"latest.csv\">latest.csv</a> · "
        "<a href=\"latest.json\">latest.json</a> · <a href=\"changes.csv\">changes.csv</a> · "
        "<a href=\"changes.json\">changes.json</a></p>\n%s"
    ) % (
        esc(tracker["title"]), esc(tracker["description"]), esc(latest), esc(run.get("fetched_at", "")),
        len(rows), esc(first), len(dates), "" if len(dates) == 1 else "s", "".join(source_notes), "\n".join(listing),
    )
    site.page(slug + "/index.html", tracker["title"], tracker["description"], body, latest)

    return {
        "tracker": tracker, "first": first, "latest": latest, "dates": dates, "rows": len(rows),
        "by_source": {s: len(r) for s, r in by_source.items()}, "by_date": by_date, "runs": runs,
    }


def build_feed(site, built):
    entries = []
    for info in built:
        tracker = info["tracker"]
        first_run = info["runs"].get(info["first"], {})
        entries.append(
            (
                first_run.get("fetched_at") or info["first"] + "T00:00:00Z",
                "%s: first snapshot, %d rows (%s)" % (tracker["title"], first_run.get("rows", 0), info["first"]),
                site.url(tracker["slug"] + "/index.html"),
                site.url(tracker["slug"] + "/index.html") + "#first-" + info["first"],
            )
        )
        for date, day in info["by_date"].items():
            run = info["runs"].get(date, {})
            link = site.url(tracker["slug"] + "/changes.html") + "#d" + date
            entries.append(
                (
                    run.get("fetched_at") or date + "T00:00:00Z",
                    "%s: %s (%s)" % (tracker["title"], change_summary(day), date),
                    link, link,
                )
            )
    entries.sort(reverse=True)
    updated = entries[0][0] if entries else "1970-01-01T00:00:00Z"
    out = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        "<title>%s: changes</title>" % esc(SITE_TITLE),
        "<subtitle>%s</subtitle>" % esc(SITE_DESCRIPTION),
        '<link rel="self" href="%s"/>' % esc(site.base_url + "feed.xml"),
        '<link href="%s"/>' % esc(site.base_url),
        "<id>%s</id>" % esc(site.base_url),
        "<updated>%s</updated>" % esc(updated),
        "<author><name>%s</name></author>" % esc(SITE_TITLE),
    ]
    for when, title, link, entry_id in entries[:100]:
        out += [
            "<entry>", "<title>%s</title>" % esc(title), '<link href="%s"/>' % esc(link),
            "<id>%s</id>" % esc(entry_id), "<updated>%s</updated>" % esc(when),
            "<summary>%s</summary>" % esc(title), "</entry>",
        ]
    out.append("</feed>")
    site.write("feed.xml", "\n".join(out) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("SITE_BASE_URL", ""))
    parser.add_argument("--out", default=os.path.join(ROOT, "docs"))
    args = parser.parse_args()
    base_url = args.base_url
    repository = common.REPOSITORY
    repo_url = "https://github.com/%s" % repository if "/" in repository else ""
    if not base_url and "/" in repository:
        owner, repo = repository.split("/", 1)
        base_url = "https://%s.github.io/%s/" % (owner.lower(), repo)
    if base_url and not base_url.endswith("/"):
        base_url += "/"

    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out)
    site = Site(args.out, base_url, repo_url)
    built = [info for info in (build_tracker(site, t) for t in TRACKERS) if info]
    if not built:
        raise SystemExit("no data to build from")

    sections = []
    for info in built:
        tracker = info["tracker"]
        per_source = ", ".join(
            "%s %d" % (tracker["sources"].get(s, (s,))[0], n) for s, n in sorted(info["by_source"].items())
        )
        sections.append(
            "<h2><a href=\"%s/\">%s</a></h2>\n<p>%s</p>\n<p class=\"muted\">Latest snapshot %s (UTC): %d rows (%s). "
            "History starts %s. <a href=\"%s/changes.html\">Changes</a> · <a href=\"%s/latest.csv\">CSV</a> · "
            "<a href=\"%s/latest.json\">JSON</a></p>"
            % (
                tracker["slug"], esc(tracker["title"]), esc(tracker["description"]), esc(info["latest"]),
                info["rows"], esc(per_source), esc(info["first"]), tracker["slug"], tracker["slug"], tracker["slug"],
            )
        )
    source_items = []
    for tracker in TRACKERS:
        for source, (label, link) in tracker["sources"].items():
            source_items.append('<li><a href="%s" rel="nofollow">%s</a> (%s)</li>' % (esc(link), esc(label), esc(tracker["title"].lower())))
    feed_line = '<p><a href="feed.xml">Atom feed of changes</a></p>' if base_url else ""
    body = (
        "<h1>%s</h1>\n<p>%s</p>\n<p>A list price can be looked up at any time, but what it was last month cannot be "
        "looked up afterwards unless someone wrote it down. These pages write it down once a day.</p>\n%s\n%s\n"
        "<h2 id=\"sources\">Sources and licences</h2>\n<ul>%s</ul>\n"
        "<p>LiteLLM and models.dev are MIT-licensed projects; their price lists are reused here with attribution. "
        "Azure prices come from the Azure Retail Prices API, which Microsoft documents as open to unauthenticated use. "
        "Verda prices come from its public instance-types endpoint. The compiled data on this site is published under "
        "CC BY 4.0 and the code under the MIT licence. The copyright and permission notices of the two MIT-licensed "
        "lists are in <a href=\"NOTICE.txt\">NOTICE.txt</a>.</p>\n"
        "<p>Provider and model names are shown as the source lists give them. The lists cover resellers and "
        "aggregators as well as first-party providers, and a value that looks implausible is shown as the source lists it.</p>"
    ) % (esc(SITE_TITLE), esc(SITE_DESCRIPTION), "\n".join(sections), feed_line, "\n".join(source_items))
    latest_overall = max(info["latest"] for info in built)
    site.page("index.html", SITE_TITLE, SITE_DESCRIPTION, body, latest_overall)

    site.write(".nojekyll", "")
    with open(os.path.join(ROOT, "NOTICE"), encoding="utf-8") as fh:
        site.write("NOTICE.txt", fh.read())
    if base_url:
        build_feed(site, built)
        urls = "\n".join(
            "<url><loc>%s</loc><lastmod>%s</lastmod></url>" % (esc(site.url(rel)), esc(lastmod))
            for rel, lastmod in sorted(site.pages)
        )
        site.write(
            "sitemap.xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n%s\n</urlset>\n' % urls,
        )
        site.write("robots.txt", "User-agent: *\nAllow: /\nSitemap: %ssitemap.xml\n" % base_url)
    else:
        site.write("robots.txt", "User-agent: *\nAllow: /\n")
        print("no base URL: sitemap.xml and feed.xml were not written")
    print("built %d pages in %s" % (len(site.pages), args.out))


if __name__ == "__main__":
    main()
