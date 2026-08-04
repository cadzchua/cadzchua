#!/usr/bin/env python3
"""Refresh the figures in cadden-stats.svg and cadden-langs.svg from the GitHub API.

The cards are hand-designed SVGs, so this rewrites only clearly delimited regions
rather than regenerating the files:

  cadden-stats.svg   the text of #v-commits, #v-repos, #v-langs, #v-prs
  cadden-langs.svg   everything between <!--gen:donut--> and <!--gen:legend--> markers

It also bumps the ?v= cache tag on those two images in README.md, because GitHub's
image proxy keys on URL: without a new tag it will keep serving the old card.

Exit codes: 0 = wrote changes or already current, 1 = failed to fetch.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER = os.environ.get("STATS_USER", "cadzchua")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
ROOT = Path(__file__).resolve().parents[2]

STATS_SVG = ROOT / "cadden-stats.svg"
LANGS_SVG = ROOT / "cadden-langs.svg"
README = ROOT / "README.md"

# Donut geometry, must match the card: <circle r="52"> inside translate(150,135)
RADIUS = 52.0
CIRCUM = 2 * math.pi * RADIUS

# Legend grid, must match the card's coordinates
LEGEND_ROWS_Y = (232, 258, 284, 310)
COL_X = (
    {"swatch": 34, "label": 46, "pct": 140},
    {"swatch": 164, "label": 176, "pct": 270},
)

# Linguist colours for languages likely to show up here; anything unmapped
# still renders, just in the neutral grey.
LANG_COLOURS = {
    "TypeScript": "#3178c6", "JavaScript": "#f1e05a", "Python": "#3572a5",
    "Java": "#b07219", "HTML": "#e34c26", "CSS": "#563d7c", "C": "#555555",
    "C++": "#f34b7d", "C#": "#178600", "Go": "#00add8", "Rust": "#dea584",
    "Shell": "#89e051", "Dockerfile": "#384d54", "Jupyter Notebook": "#da5b0b",
    "Kotlin": "#a97bff", "Swift": "#f05138", "Ruby": "#701516", "PHP": "#4f5d95",
    "Vue": "#41b883", "Svelte": "#ff3e00", "SCSS": "#c6538c", "Makefile": "#427819",
}
OTHER_COLOUR = "#7d8590"
TOP_N = 6


def api(path: str, params: dict | None = None) -> dict:
    url = "https://api.github.com/" + path.lstrip("/")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USER}-profile-stats",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


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


def collect() -> dict:
    profile = api(f"users/{USER}")
    repos = all_repos()
    sources = [r for r in repos if not r["fork"]]

    byte_totals: dict[str, int] = {}
    for repo in sources:
        for lang, count in api(f"repos/{USER}/{repo['name']}/languages").items():
            byte_totals[lang] = byte_totals.get(lang, 0) + count

    commits = api("search/commits", {"q": f"author:{USER}", "per_page": 1})["total_count"]
    prs = api("search/issues", {"q": f"author:{USER} type:pr", "per_page": 1})["total_count"]

    return {
        "commits": commits,
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

    slices = [
        {"name": name, "pct": count / total * 100, "colour": LANG_COLOURS.get(name, OTHER_COLOUR)}
        for name, count in head
    ]
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
            f'<text x="{col["label"]}" y="{y}" fill="#e6edf3">{s["name"]}</text>'
            f'<text x="{col["pct"]}" y="{y}" text-anchor="end" fill="#7d8590">{s["pct"]:.1f}%</text>\n'
            f'    </g>'
        )
    lines.append("  </g>")
    return "\n".join(lines)


def replace_between(text: str, marker: str, body: str) -> str:
    pattern = re.compile(f"(<!--gen:{marker}-->\n).*?(\n\\s*<!--/gen:{marker}-->)", re.DOTALL)
    if not pattern.search(text):
        raise SystemExit(f"marker gen:{marker} not found - card structure changed?")
    return pattern.sub(lambda m: m.group(1) + body + m.group(2), text)


def set_text_by_id(svg: str, node_id: str, value: str) -> str:
    pattern = re.compile(f'(<text id="{node_id}"[^>]*>).*?(</text>)', re.DOTALL)
    if not pattern.search(svg):
        raise SystemExit(f"#{node_id} not found - card structure changed?")
    return pattern.sub(lambda m: m.group(1) + value + m.group(2), svg)


def bump_cache_tags(readme: str, names: list[str]) -> str:
    """GitHub's image proxy keys on URL, so a changed card needs a changed ?v=."""
    for name in names:
        pattern = re.compile(rf"({re.escape(name)}\?v=)(\d+)")
        readme = pattern.sub(lambda m: m.group(1) + str(int(m.group(2)) + 1), readme)
    return readme


def main() -> int:
    try:
        data = collect()
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError) as exc:
        print(f"failed to fetch stats: {exc}", file=sys.stderr)
        return 1

    slices = language_slices(data["byte_totals"])
    if not slices:
        print("no language data returned; leaving cards untouched", file=sys.stderr)
        return 1

    stats = STATS_SVG.read_text(encoding="utf-8")
    original_stats = stats
    stats = set_text_by_id(stats, "v-commits", str(data["commits"]))
    stats = set_text_by_id(stats, "v-repos", str(data["repos"]))
    stats = set_text_by_id(stats, "v-langs", str(data["languages"]))
    stats = set_text_by_id(stats, "v-prs", str(data["prs"]))

    langs = LANGS_SVG.read_text(encoding="utf-8")
    original_langs = langs
    langs = replace_between(langs, "donut", render_donut(slices))
    langs = replace_between(langs, "legend", render_legend(slices))

    changed = []
    if stats != original_stats:
        STATS_SVG.write_text(stats, encoding="utf-8", newline="\n")
        changed.append(STATS_SVG.name)
    if langs != original_langs:
        LANGS_SVG.write_text(langs, encoding="utf-8", newline="\n")
        changed.append(LANGS_SVG.name)

    if changed:
        readme = README.read_text(encoding="utf-8")
        README.write_text(bump_cache_tags(readme, changed), encoding="utf-8", newline="\n")

    print(
        f"commits={data['commits']} repos={data['repos']} "
        f"languages={data['languages']} prs={data['prs']}"
    )
    for s in slices:
        print(f"  {s['name']:<14} {s['pct']:5.1f}%")
    print("changed: " + (", ".join(changed) if changed else "nothing (already current)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
