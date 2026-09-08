#!/usr/bin/env python3
"""Refresh the figures in cadden-stats.svg and cadden-langs.svg from the GitHub API.

The cards are hand-designed SVGs, so this rewrites only clearly delimited regions
rather than regenerating the files:

  cadden-stats.svg   the text of #v-contrib, #v-repos, #v-langs, #v-prs
  cadden-langs.svg   everything between <!--gen:donut--> and <!--gen:legend--> markers

Exit codes: 0 = wrote changes or already current, 1 = failed to fetch or render.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

USER = os.environ.get("STATS_USER", "cadzchua")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
PROFILE_TOKEN = os.environ.get("PROFILE_TOKEN", "")
REJECTED_TOKENS: set[str] = set()
ROOT = Path(__file__).resolve().parents[2]

STATS_SVG = ROOT / "cadden-stats.svg"
LANGS_SVG = ROOT / "cadden-langs.svg"

# Donut geometry, must match the card: <circle r="52"> inside translate(150,135)
RADIUS = 52.0
CIRCUM = 2 * math.pi * RADIUS

# Legend grid, must match the card's coordinates
LEGEND_ROWS_Y = (232, 258, 284, 310)
COL_X = (
    {"swatch": 34, "label": 46, "pct": 140},
    {"swatch": 164, "label": 176, "pct": 270},
)

# Linguist colours for languages likely to show up here. Anything unmapped falls
# through to FALLBACK_COLOURS below.
LANG_COLOURS = {
    "TypeScript": "#3178c6", "JavaScript": "#f1e05a", "Python": "#3572a5",
    "Java": "#b07219", "HTML": "#e34c26", "CSS": "#563d7c", "C": "#555555",
    "C++": "#f34b7d", "C#": "#178600", "Go": "#00add8", "Rust": "#dea584",
    "Shell": "#89e051", "Dockerfile": "#384d54", "Jupyter Notebook": "#da5b0b",
    "Kotlin": "#a97bff", "Swift": "#f05138", "Ruby": "#701516", "PHP": "#4f5d95",
    "Vue": "#41b883", "Svelte": "#ff3e00", "SCSS": "#c6538c", "Makefile": "#427819",
}
OTHER_COLOUR = "#7d8590"
# A language with no Linguist colour mapped above must NOT fall back to OTHER_COLOUR,
# or it renders identically to the Other slice sitting next to it in the same legend.
FALLBACK_COLOURS = ("#e8a838", "#6ea8fe", "#c084fc", "#5fd3bc", "#f6768e", "#a3be8c")
TOP_N = 6


def fetch(req: urllib.request.Request) -> bytes:
    """Retry temporary network/server failures, without retrying rejected credentials."""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                print(
                    f"GitHub returned HTTP {exc.code}; check token permissions/rate limits "
                    f"(remaining={exc.headers.get('X-RateLimit-Remaining', 'unknown')}, "
                    f"reset={exc.headers.get('X-RateLimit-Reset', 'unknown')}).",
                    file=sys.stderr,
                )
            if exc.code not in (500, 502, 503, 504) or attempt == 2:
                raise
            # Respect server backoff; leave long waits to the next workflow run.
            try:
                delay = max(2 ** attempt, float(exc.headers.get("Retry-After", "0")))
            except ValueError:
                raise exc
            if delay > 30:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            delay = 2 ** attempt
        print(f"temporary request failure; retrying in {delay:g}s ({attempt + 2}/3)", file=sys.stderr)
        time.sleep(delay)
    raise AssertionError("unreachable")


def api(path: str, params: dict | None = None) -> dict | list:
    url = "https://api.github.com/" + path.lstrip("/")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USER}-profile-stats",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN and TOKEN not in REJECTED_TOKENS:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    try:
        return json.loads(fetch(req))
    except urllib.error.HTTPError as exc:
        if exc.code != 401 or "Authorization" not in headers:
            raise
        REJECTED_TOKENS.add(TOKEN)
        print("GITHUB_TOKEN was rejected; retrying public REST data without authentication", file=sys.stderr)
        return api(path, params)


def all_repos() -> list[dict]:
    repos, page = [], 1
    while True:
        batch = api(f"users/{USER}/repos", {"per_page": 100, "page": page})
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def graphql_contributions(since: dt.datetime, until: dt.datetime, token: str) -> int:
    """Total contributions in a window. Needs a token; Actions supplies one."""
    query = """
      query($login:String!,$from:DateTime!,$to:DateTime!){
        user(login:$login){
          contributionsCollection(from:$from,to:$to){
            contributionCalendar{ totalContributions }
          }
        }
      }"""
    payload = json.dumps({
        "query": query,
        "variables": {
            "login": USER,
            "from": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"{USER}-profile-stats",
        },
    )
    body = json.loads(fetch(req))
    if "errors" in body:
        raise RuntimeError(body["errors"])
    total = body["data"]["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"]
    if type(total) is not int or total < 0:
        raise ValueError("GraphQL returned an invalid contribution total")
    return total


def calendar_contributions() -> int:
    """Fallback: sum the public contribution calendar. No token required.
    With no date range the endpoint returns GitHub's trailing-year calendar."""
    url = f"https://github.com/users/{USER}/contributions"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"})
    markup = fetch(req).decode("utf-8", "replace")
    days = re.findall(r"<tool-tip\b[^>]*>(.*?)</tool-tip>", markup, re.DOTALL)
    if not days:
        raise RuntimeError("contribution calendar markup not recognised")
    total = 0
    for tip in days:
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", tip)).split())
        m = re.match(r"(?:No|(\d[\d,]*)) contributions? on \S", text)
        if not m:
            raise RuntimeError("contribution calendar tooltip not recognised; keeping existing cards")
        if m.group(1):
            total += int(m.group(1).replace(",", ""))
    return total


def contributions_trailing_year() -> int:
    """Prefer the optional profile token, then Actions token, then public calendar."""
    for token in dict.fromkeys((PROFILE_TOKEN, TOKEN)):
        if not token or token in REJECTED_TOKENS:
            continue
        try:
            until = dt.datetime.now(dt.timezone.utc)
            return graphql_contributions(until - dt.timedelta(days=365), until, token)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 401:
                REJECTED_TOKENS.add(token)
            source = "PROFILE_TOKEN" if token == PROFILE_TOKEN else "GITHUB_TOKEN"
            print(f"GraphQL with {source} failed ({exc}); trying contribution fallback", file=sys.stderr)
    print("using public contribution calendar", file=sys.stderr)
    return calendar_contributions()


def collect() -> dict:
    profile = api(f"users/{USER}")
    repos = all_repos()
    sources = [r for r in repos if not r["fork"]]

    byte_totals: dict[str, int] = {}
    for repo in sources:
        for lang, count in api(f"repos/{USER}/{repo['name']}/languages").items():
            byte_totals[lang] = byte_totals.get(lang, 0) + count

    prs = api("search/issues", {"q": f"author:{USER} type:pr", "per_page": 1})["total_count"]
    contributions = contributions_trailing_year()

    return {
        # trailing 12 months, matching GitHub's own graph and the snake. search/commits
        # only indexes default branches of public non-fork repos, so it reported well
        # under half of this.
        "contributions": contributions,
        "repos": profile["public_repos"],
        "languages": len(byte_totals),
        "prs": prs,
        "byte_totals": byte_totals,
    }


def language_slices(byte_totals: dict[str, int]) -> list[dict]:
    """Top N languages by bytes, with the remainder folded into one 'Other' slice."""
    total = sum(byte_totals.values())
    if total == 0:
        return []
    ranked = sorted(byte_totals.items(), key=lambda kv: kv[1], reverse=True)
    head, tail = ranked[:TOP_N], ranked[TOP_N:]

    slices = []
    unmapped = 0
    for name, count in head:
        colour = LANG_COLOURS.get(name)
        if colour is None:
            colour = FALLBACK_COLOURS[unmapped % len(FALLBACK_COLOURS)]
            unmapped += 1
        slices.append({"name": name, "pct": count / total * 100, "colour": colour})
    if tail:
        slices.append({
            "name": "Other",
            "pct": sum(c for _, c in tail) / total * 100,
            "colour": OTHER_COLOUR,
        })
    return slices


def render_donut(slices: list[dict]) -> str:
    lines = [
        '  <g transform="translate(150,135) rotate(-90)" fill="none" stroke-width="28">',
        '    <circle r="52" stroke="#161b22"/>',
    ]
    offset = 0.0
    for i, s in enumerate(slices):
        length = s["pct"] / 100 * CIRCUM
        begin = 0.4 + i * 0.15
        lines.append(
            f'    <circle r="52" stroke="{s["colour"]}" stroke-dasharray="0 {CIRCUM:.1f}" '
            f'stroke-dashoffset="{-offset:.1f}">\n'
            f'      <animate attributeName="stroke-dasharray" to="{length:.1f} {CIRCUM:.1f}" '
            f'dur="1.1s" begin="{begin:.2f}s" fill="freeze"/></circle>'
        )
        offset += length
    lines.append("  </g>")
    return "\n".join(lines)


def render_legend(slices: list[dict]) -> str:
    lines = ['  <g class="mono" font-size="10">']
    for i, s in enumerate(slices):
        col = COL_X[i % 2]
        y = LEGEND_ROWS_Y[i // 2]
        delay = 0.9 + i * 0.08
        lines.append(
            f'    <g class="r" style="animation-delay:{delay:.2f}s">\n'
            f'      <circle cx="{col["swatch"]}" cy="{y - 3}" r="4.5" fill="{s["colour"]}"/>'
            f'<text x="{col["label"]}" y="{y}" fill="#e6edf3">{html.escape(s["name"])}</text>'
            f'<text x="{col["pct"]}" y="{y}" text-anchor="end" fill="#7d8590">{s["pct"]:.1f}%</text>\n'
            f'    </g>'
        )
    lines.append("  </g>")
    return "\n".join(lines)


def replace_between(text: str, marker: str, body: str) -> str:
    pattern = re.compile(f"(<!--gen:{marker}-->\n).*?(\n\\s*<!--/gen:{marker}-->)", re.DOTALL)
    if not pattern.search(text):
        raise ValueError(f"marker gen:{marker} not found - card structure changed?")
    return pattern.sub(lambda m: m.group(1) + body + m.group(2), text)


def set_text_by_id(svg: str, node_id: str, value: str) -> str:
    pattern = re.compile(f'(<text id="{node_id}"[^>]*>).*?(</text>)', re.DOTALL)
    if not pattern.search(svg):
        raise ValueError(f"#{node_id} not found - card structure changed?")
    return pattern.sub(lambda m: m.group(1) + html.escape(value) + m.group(2), svg)


def write_cards(cards: dict[Path, str]) -> None:
    """Validate and stage every card before replacing any existing SVG."""
    for content in cards.values():
        ET.fromstring(content)
    staged: dict[Path, Path] = {}
    try:
        for path, content in cards.items():
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                staged[path] = Path(handle.name)
                handle.write(content)
        for path, temporary in staged.items():
            os.replace(temporary, path)
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def refresh() -> int:
    data = collect()

    slices = language_slices(data["byte_totals"])
    if not slices:
        print("no language data returned; leaving cards untouched", file=sys.stderr)
        return 1

    stats = STATS_SVG.read_text(encoding="utf-8")
    original_stats = stats
    stats = set_text_by_id(stats, "v-contrib", str(data["contributions"]))
    stats = set_text_by_id(stats, "v-repos", str(data["repos"]))
    stats = set_text_by_id(stats, "v-langs", str(data["languages"]))
    stats = set_text_by_id(stats, "v-prs", str(data["prs"]))

    langs = LANGS_SVG.read_text(encoding="utf-8")
    original_langs = langs
    langs = replace_between(langs, "donut", render_donut(slices))
    langs = replace_between(langs, "legend", render_legend(slices))

    changed = {}
    if stats != original_stats:
        changed[STATS_SVG] = stats
    if langs != original_langs:
        changed[LANGS_SVG] = langs
    write_cards(changed)

    print(
        f"contributions(12mo)={data['contributions']} repos={data['repos']} "
        f"languages={data['languages']} prs={data['prs']}"
    )
    for s in slices:
        print(f"  {s['name']:<14} {s['pct']:5.1f}%")
    print("changed: " + (", ".join(path.name for path in changed) if changed else "nothing (already current)"))
    return 0


def main() -> int:
    try:
        return refresh()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ET.ParseError) as exc:
        print(f"failed to refresh stats: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
