#!/usr/bin/env python3
"""Generate README.md from SelfHost Alternatives site data.

Reads saas.yml, apps.json and extra_apps.json (same sources as the site's
/alternatives/ pages) and writes a curated awesome-list README.

Usage:
  DATA_DIR=/path/to/directory/site/data python scripts/generate_readme.py
  python scripts/generate_readme.py --data-dir /path/to/data --out README.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
from collections import OrderedDict
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required. Install with: pip install -r requirements.txt")

SITE = "https://selfhostalternatives.com"
REPO = "https://github.com/selfhostalternatives/awesome-selfhost-alternatives"

# Match the site's alternatives-page thresholds (config/site.yml seo.*).
MIN_APPS_FOR_PAGE = 3
MAX_APPS_ON_PAGE = 30
# Cap listed alternatives per SaaS product so the README stays readable.
MAX_ALTS = 8
# Keep the README around or under 2,000 lines: include every SaaS product
# with at least one direct alternative match, then fill from saas.yml order
# (the file is curated with popular products first).
TARGET_SAAS = 160

SCORE_ACTIVITY = {
    "active": 30,
    "maintained": 22,
    "slowing": 10,
    "unknown": 8,
    "stale": 3,
    "archived": 0,
}


def find_data_dir(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not (p / "saas.yml").exists() or not (p / "apps.json").exists():
            sys.exit(f"DATA_DIR {p} must contain saas.yml and apps.json")
        return p
    here = Path(__file__).resolve()
    candidates = []
    if os.environ.get("DATA_DIR"):
        candidates.append(Path(os.environ["DATA_DIR"]).expanduser())
    candidates.extend(
        [
            Path("/workspace/directory/site/data"),
            here.parents[2] / "directory" / "site" / "data",
            here.parents[1] / "data",
        ]
    )
    for c in candidates:
        if (c / "saas.yml").exists() and (c / "apps.json").exists():
            return c.resolve()
    sys.exit(
        "Could not find site data (saas.yml + apps.json). "
        "Pass --data-dir or set DATA_DIR."
    )


def repo_key(a: dict) -> str | None:
    m = re.match(
        r"https?://(github\.com|gitlab\.com|codeberg\.org)/(.+?)(?:\.git)?/?$",
        (a.get("source_code_url") or "").strip(),
    )
    if not m:
        return None
    path = (
        re.sub(r"/-/.*$", "", m.group(2))
        if m.group(1) == "gitlab.com"
        else "/".join(m.group(2).split("/")[:2])
    )
    return f"{m.group(1)}/{path}".lower()


def load_apps(data_dir: Path) -> list[dict]:
    apps = json.loads((data_dir / "apps.json").read_text())
    for a in apps:
        a.setdefault("source", "awesome-selfhosted")
    extra_path = data_dir / "extra_apps.json"
    if extra_path.exists():
        try:
            extra_doc = json.loads(extra_path.read_text())
        except Exception:
            extra_doc = {"apps": []}
        known_repos = {k for k in (repo_key(a) for a in apps) if k} | {
            ("github.com/" + a["github_repo"]).lower()
            for a in apps
            if a.get("github_repo")
        }
        known_slugs = {a["slug"] for a in apps}
        for x in extra_doc.get("apps") or []:
            k = repo_key(x)
            if (k and k in known_repos) or x.get("slug") in known_slugs:
                continue
            if k:
                known_repos.add(k)
            known_slugs.add(x["slug"])
            apps.append(x)
    return apps


def days_since(d, today: dt.date) -> int | None:
    try:
        return max(0, (today - dt.date.fromisoformat(str(d)[:10])).days) if d else None
    except ValueError:
        return None


def health_of(a: dict, days: int | None) -> str:
    if a.get("archived"):
        return "archived"
    if days is None:
        return "unknown"
    if days <= 90:
        return "active"
    if days <= 365:
        return "maintained"
    if days <= 730:
        return "slowing"
    return "stale"


def score_and_rank(apps: list[dict], today: dt.date) -> None:
    """Same 0-100 score the site uses: stars, maintenance health, Docker."""
    for a in apps:
        a["health"] = health_of(a, days_since(a.get("last_activity"), today))
        d = a.get("docker") or {}
        docker_ok = bool(
            d.get("platform_listed")
            or d.get("dockerfile")
            or d.get("compose_file")
            or d.get("docker_hub_image")
        )
        pop = min(50.0, 12.5 * math.log10((a.get("stars") or 0) + 1))
        act = SCORE_ACTIVITY.get(a["health"], 8)
        dock = 20 if d.get("docker_hub_image") else 12 if docker_ok else 0
        total = round(pop + act + dock)
        a["rank"] = total + math.log10((a.get("stars") or 0) + 10) / 100


def mention_re(names: list[str]) -> re.Pattern:
    alt = "|".join(re.escape(n) for n in names)
    flags = re.I if all(len(n) > 4 for n in names) else 0
    return re.compile(
        rf"((alternatives? to|replacement for|similar to|\blike|instead of|inspired by|clone of|compatible with)[^.;()]{{0,70}}\b({alt})\b"
        rf"|\b({alt})[- ](alternative|clone|replacement)"
        rf"|\b(open[- ]source|self[- ]hosted|selfhosted|FOSS|libre)\s+({alt})\s*(?:[.;,!)\]]|$))",
        flags,
    )


def build_saas_pages(saas_list: list[dict], apps: list[dict]) -> list[dict]:
    """Replicate the site's /alternatives/ matching and ranking."""
    apps_by_slug = {a["slug"]: a for a in apps}
    pages = []
    for idx, s in enumerate(saas_list):
        names = [s["name"]] + list(s.get("also_mentions") or [])
        mre = mention_re(names)
        kre = (
            re.compile("|".join(s["keywords"]), re.I) if s.get("keywords") else None
        )
        excl = set(s.get("exclude") or [])
        include = [sl for sl in (s.get("include") or []) if sl not in excl]
        direct, related = [], []
        seen: set[str] = set()
        for a in apps:
            if a["slug"] in excl or a["health"] == "archived":
                continue
            text = a.get("description") or ""
            alt_hit = any(
                n.lower() == x.lower()
                for n in names
                for x in (a.get("alternative_to") or [])
            )
            if alt_hit or mre.search(text):
                direct.append(a)
                seen.add(a["slug"])
            elif set(a.get("tags") or []) & set(s.get("tags") or []) and a.get(
                "source"
            ) == "awesome-selfhosted":
                hay = f"{a['name']} {text} {' '.join(a.get('topics') or [])}"
                if kre is None or kre.search(hay):
                    related.append(a)
                    seen.add(a["slug"])
        for sl in include:
            if sl in seen:
                continue
            a = apps_by_slug.get(sl)
            if not a or a["health"] == "archived":
                continue
            related.append(a)
            seen.add(sl)
        direct.sort(key=lambda a: -a["rank"])
        related.sort(key=lambda a: -a["rank"])
        lst = (direct + related)[:MAX_APPS_ON_PAGE]
        if len(lst) < MIN_APPS_FOR_PAGE:
            continue
        page = dict(s)
        page.update(apps=lst, n_direct=len(direct), idx=idx)
        pages.append(page)
    return pages


def select_pages(pages: list[dict]) -> list[dict]:
    """Direct-match SaaS first, then fill from curated saas.yml order."""
    chosen: list[dict] = []
    seen: set[str] = set()
    for p in pages:
        if p["n_direct"] >= 1:
            chosen.append(p)
            seen.add(p["slug"])
    for p in pages:
        if len(chosen) >= TARGET_SAAS:
            break
        if p["slug"] not in seen:
            chosen.append(p)
            seen.add(p["slug"])
    chosen.sort(key=lambda p: p["idx"])
    return chosen


def github_anchor(title: str, used: dict[str, int]) -> str:
    """GitHub-style heading slug (spaces to hyphens, then strip punctuation)."""
    text = title.strip().lower().replace(" ", "-")
    text = re.sub(r"[^\w-]", "", text, flags=re.UNICODE)
    n = used.get(text, 0)
    used[text] = n + 1
    return text if n == 0 else f"{text}-{n}"


def md_link_text(text: str) -> str:
    return (text or "").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def one_line(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def ensure_period(text: str) -> str:
    if not text:
        return text
    if text[-1] in ".!?":
        return text
    return text + "."


def app_url(a: dict) -> str:
    for key in ("website_url", "source_code_url"):
        u = (a.get(key) or "").strip()
        if u:
            return u
    return ""


def license_text(a: dict) -> str:
    licenses = a.get("licenses") or []
    if isinstance(licenses, str):
        licenses = [licenses]
    licenses = [x for x in licenses if x]
    return ", ".join(licenses)


def format_app(a: dict) -> str | None:
    name = a.get("name")
    if not name:
        return None
    url = app_url(a)
    desc = ensure_period(one_line(a.get("description") or ""))
    lic = license_text(a)
    compare = f"{SITE}/app/{a['slug']}/"
    label = md_link_text(name)
    head = f"- [{label}]({url})" if url else f"- {label}"
    parts = []
    if desc:
        parts.append(desc)
    mid = " ".join(p for p in (desc, lic) if p)
    if mid:
        return f"{head} - {mid} · [compare]({compare})" if desc else f"{head} {lic} · [compare]({compare})"
    return f"{head} · [compare]({compare})"


def group_by_category(pages: list[dict]) -> OrderedDict:
    groups: OrderedDict[str, list] = OrderedDict()
    for p in pages:
        cat = (p.get("tags") or ["Other"])[0] or "Other"
        groups.setdefault(cat, []).append(p)
    return groups


def render(pages: list[dict], generated_on: str) -> str:
    groups = group_by_category(pages)
    used: dict[str, int] = {}
    cat_anchors = {}
    saas_anchors = {}
    for cat, items in groups.items():
        cat_anchors[cat] = github_anchor(cat, used)
        for p in items:
            title = f"{p['name']} alternatives"
            saas_anchors[p["slug"]] = github_anchor(title, used)

    n_saas = len(pages)
    n_apps = len({a["slug"] for p in pages for a in p["apps"][:MAX_ALTS]})

    lines = []
    a = lines.append
    a("# Awesome Self-Host Alternatives")
    a("")
    a("[![Awesome](https://awesome.re/badge.svg)](https://awesome.re)")
    a("")
    a(
        "A curated list of **open-source, self-hosted alternatives** to popular SaaS apps, "
        "from [SelfHost Alternatives](https://selfhostalternatives.com)."
    )
    a("")
    a(
        f"This snapshot covers {n_saas} SaaS products and {n_apps} unique apps. "
        "Each section lists the top self-hosted options, ranked the same way the site ranks them "
        "(GitHub stars, recent maintenance, and Docker support). "
        "Descriptions, licenses and homepage links come only from the catalog; anything missing is left out rather than guessed."
    )
    a("")
    a(
        "The [full directory](https://selfhostalternatives.com/alternatives/) has more alternatives, "
        "side-by-side comparisons, hardware estimates and maintenance status. "
        f"Generated on {generated_on}."
    )
    a("")
    a("## Contents")
    a("")
    for cat, items in groups.items():
        a(f"- [{cat}](#{cat_anchors[cat]})")
        for p in items:
            a(f"  - [{p['name']} alternatives](#{saas_anchors[p['slug']]})")
    a("")

    for cat, items in groups.items():
        a(f"## {cat}")
        a("")
        for p in items:
            title = f"{p['name']} alternatives"
            page_url = f"{SITE}/alternatives/{p['slug']}/"
            a(f"### [{title}]({page_url})")
            a("")
            for app in p["apps"][:MAX_ALTS]:
                row = format_app(app)
                if row:
                    a(row)
            a("")

    a("## Contributing")
    a("")
    a(
        "- To suggest an app for the directory, use the "
        f"[SelfHost Alternatives submission form]({SITE}/submit/) "
        "or [open an issue or pull request](https://github.com/selfhostalternatives/awesome-selfhost-alternatives/issues) in this repository."
    )
    a(
        "- To regenerate this README from a checkout of the site data:"
    )
    a("")
    a("```bash")
    a("pip install -r requirements.txt")
    a("DATA_DIR=/path/to/directory/site/data python scripts/generate_readme.py")
    a("```")
    a("")
    a("Please do not invent stars, licenses, descriptions or URLs. The generator only prints fields that already exist in the data.")
    a("")
    a("## License")
    a("")
    a(
        "This list is released under [CC-BY-SA-4.0](LICENSE). "
        "It is adapted from the SelfHost Alternatives catalog, which is itself an adaptation of:"
    )
    a("")
    a(
        "- [awesome-selfhosted-data](https://github.com/awesome-selfhosted/awesome-selfhosted-data), "
        "© the [awesome-selfhosted contributors](https://github.com/awesome-selfhosted/awesome-selfhosted-data/blob/master/AUTHORS), "
        "licensed under [CC-BY-SA-3.0](https://creativecommons.org/licenses/by-sa/3.0/)"
    )
    a(
        "- [awesome-sysadmin](https://github.com/awesome-foss/awesome-sysadmin), licensed under "
        "[CC-BY-SA-3.0](https://creativecommons.org/licenses/by-sa/3.0/)"
    )
    a("- GitHub repositories tagged self-hosted, plus GitHub and Docker Hub metadata used by SelfHost Alternatives")
    a("")
    a(
        "If you reuse this list, credit the awesome-selfhosted contributors and "
        "[SelfHost Alternatives](https://selfhostalternatives.com), and share your adaptation under CC-BY-SA."
    )
    a("")
    a("Product names are trademarks of their respective owners. This list is not affiliated with any listed SaaS product or app.")
    a("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None, help="Path to the site data/ directory")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parents[1] / "README.md"),
        help="Output README path",
    )
    args = parser.parse_args()
    data_dir = find_data_dir(args.data_dir)
    today = dt.date.today()
    apps = load_apps(data_dir)
    score_and_rank(apps, today)
    saas_list = yaml.safe_load((data_dir / "saas.yml").read_text())
    pages = select_pages(build_saas_pages(saas_list, apps))
    text = render(pages, today.isoformat())
    out = Path(args.out)
    out.write_text(text)
    n_lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    n_apps = len({a["slug"] for p in pages for a in p["apps"][:MAX_ALTS]})
    n_rows = sum(1 for p in pages for _ in p["apps"][:MAX_ALTS])
    print(
        f"Wrote {out} ({n_lines} lines, {len(pages)} SaaS products, "
        f"{n_rows} listed alternatives, {n_apps} unique apps) from {data_dir}"
    )
    if n_lines > 2200:
        print(f"WARNING: README is {n_lines} lines; consider lowering TARGET_SAAS or MAX_ALTS", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
