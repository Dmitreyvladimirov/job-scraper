"""Resolve a company's Comeet board credentials from its public careers page.

Comeet boards need a (uid, token) pair rather than a single slug. Both are
published in the company's own careers markup — this walks the usual places:
  1. a comeet.com/jobs/{slug}/{uid} link on the careers page,
  2. COMPANY_DATA = {... "token": "..."} on the Comeet-hosted page,
  3. comeetInit({uid, token}) for sites using the JS API embed.

Usage:
    python scripts/find_comeet_board.py "Checkmarx" https://www.checkmarx.com/company/careers/
    python scripts/find_comeet_board.py --batch companies.tsv     # name<TAB>careers_url

Prints ready-to-paste seed rows; writes nothing.
"""
import argparse
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import ats_boards  # noqa: E402
import filters  # noqa: E402

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

_JOBS_LINK = re.compile(r"comeet\.com/jobs/([A-Za-z0-9_-]+)/([0-9A-Za-z.]+)")
_TOKEN = re.compile(r'"token"\s*:\s*"([A-Za-z0-9]{8,64})"')
_INIT = re.compile(r'comeetInit\s*\(\s*\{(.{0,400}?)\}', re.DOTALL)
_UID_KV = re.compile(r'uid["\']?\s*[:=]\s*["\']([0-9A-Za-z.]+)["\']')


def _get(url: str) -> str:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20, allow_redirects=True)
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


def resolve(careers_url: str) -> tuple[str, str] | None:
    """-> (uid, token) or None. Accepts either the company's own careers page or
    a Comeet-hosted URL (comeet.com/jobs/{slug}/{uid}) directly — the hosted page
    always carries the token in COMPANY_DATA, while company sites often load the
    board via JS and expose nothing (Checkmarx-class)."""
    direct = _JOBS_LINK.search(careers_url)
    if direct:
        hosted = _get(f"https://www.comeet.com/jobs/{direct.group(1)}/{direct.group(2)}")
        t = _TOKEN.search(hosted or "")
        if t:
            return direct.group(2), t.group(1)
        return None

    html = _get(careers_url)
    if not html:
        return None

    uid = token = ""
    m = _JOBS_LINK.search(html)
    if m:
        uid = m.group(2)
    if not uid:
        init = _INIT.search(html)
        if init:
            kv = _UID_KV.search(init.group(1))
            if kv:
                uid = kv.group(1)
    t = _TOKEN.search(html)
    if t:
        token = t.group(1)

    # The company site often links to the Comeet-hosted page without exposing the
    # token; that page always carries it in COMPANY_DATA.
    if uid and not token and m:
        hosted = _get(f"https://www.comeet.com/jobs/{m.group(1)}/{uid}")
        t2 = _TOKEN.search(hosted or "")
        if t2:
            token = t2.group(1)

    return (uid, token) if uid and token else None


def check(name: str, careers_url: str) -> dict | None:
    pair = resolve(careers_url)
    if not pair:
        print(f"miss   {name:26} could not resolve uid/token from {careers_url}")
        return None
    uid, token = pair
    slug = f"{uid}:{token}"
    try:
        postings = ats_boards.list_comeet(slug, timeout=15)
    except Exception as e:
        print(f"err    {name:26} {uid} -> {str(e)[:60]}")
        return None
    pm = [p for p in postings if filters.passes_role_filter({"title": p["title"]})]
    print(f"OK     {name:26} comeet {uid:10} {len(postings):3} postings, {len(pm)} PM"
          + (f"  [{pm[0]['title'][:40]}]" if pm else ""))
    return {"name": name, "ats": "comeet", "slug": slug,
            "postings": len(postings), "pm": len(pm)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?")
    ap.add_argument("careers_url", nargs="?")
    ap.add_argument("--batch", help="TSV file: name<TAB>careers_url per line")
    args = ap.parse_args()

    rows = []
    if args.batch:
        for line in Path(args.batch).read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            name, url = line.split("\t")[:2]
            rows.append((name.strip(), url.strip()))
    elif args.name and args.careers_url:
        rows.append((args.name, args.careers_url))
    else:
        ap.error("give name + careers_url, or --batch file.tsv")

    found = []
    for name, url in rows:
        r = check(name, url)
        if r:
            found.append(r)
        time.sleep(0.5)

    print(f"\n{len(found)}/{len(rows)} boards resolved")
    for f in found:
        print(f'  {{"name": "{f["name"]}", "ats": "comeet", "slug": "{f["slug"]}", '
              f'"layer": 2, "region": "IL", "provenance": "comeet_2026-08-20"}},')


if __name__ == "__main__":
    main()
