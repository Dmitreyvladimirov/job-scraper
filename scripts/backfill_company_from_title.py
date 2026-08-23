"""Recover the company name for imported cards that have none.

The 2026-06 Notion importer put the company in the title's trailing parentheses
("Senior Product Manager (Toggl)") and left jobs.company empty. 167 of the 375
cards ever marked applied are in that state -- 45% of the applied history. The
cost is not cosmetic: company is a hard gate on resume generation, and every
company-keyed rule reads through it (the reapplication guard, the 180-day
company-rejection cooldown that gates autogen, per-company analytics).

Dry run by default; pass --apply to write. Titles are left alone -- the
parenthetical stays where it is, since removing it is a second, separate edit
to text Dimitry can see on his own cards.

    python scripts/backfill_company_from_title.py            # show what would change
    python scripts/backfill_company_from_title.py --apply    # do it
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

import db  # noqa: E402

# Only the Notion importer's rows. See the query below for why this is not
# "every card without a company".
SOURCE = "NotionImport"

_TRAILING_PARENS = re.compile(r"\(([^)]{2,40})\)\s*$")

_LOOKS_LIKE_DOMAIN = re.compile(r"^([\w-]+)\.[a-z]{2,}$", re.IGNORECASE)

# A domain-shaped parenthetical is usually still the company, just written with
# its TLD -- Monday.com, Darrow.ai, riskified.com are all real company names.
# Only these two are the importer echoing a link it could not resolve.
_NOT_A_COMPANY = {"lnkd.in", "linkedin.com"}


def extract(title: str) -> tuple[str | None, str | None]:
    """(company, reason_skipped). Exactly one of the two is set."""
    match = _TRAILING_PARENS.search(title or "")
    if not match:
        return None, "no trailing parentheses"
    candidate = match.group(1).strip(" .")
    if not candidate:
        return None, "empty parentheses"
    if candidate.lower() in _NOT_A_COMPANY:
        return None, f"a link, not a company: {candidate!r}"
    domain = _LOOKS_LIKE_DOMAIN.match(candidate)
    if domain:
        # Drop the TLD so the value matches how the same company arrives from a
        # scraper ("Riskified", not "riskified.com") -- company is the join key for
        # the reapplication guard and the rejection cooldown, and a suffix would
        # quietly make every one of those comparisons miss.
        stem = domain.group(1)
        candidate = stem[0].upper() + stem[1:] if stem.islower() else stem
    if "," in candidate:
        # "matrix, Edtech" -- the importer glued a company and a sector together.
        # Guessing which half is the company is exactly the kind of invention this
        # codebase keeps out of the resume path, so it stays for a human.
        return None, f"ambiguous, needs a human: {candidate!r}"
    return candidate, None


def main() -> int:
    apply = "--apply" in sys.argv
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set")
        return 1

    conn = db._conn()
    try:
        with conn.cursor() as cur:
            # Scoped to the importer's own rows on purpose. The first draft of this
            # script asked for every company-less card and found 18,976 of them --
            # overwhelmingly scraped Telegram noise that was never a vacancy, where a
            # trailing parenthetical is a salary band or a location, not a company
            # ("Senior product manager (remote Europe) 90-130k"). Widening the net
            # here would write 4,000+ wrong company names to fix 167 right ones.
            cur.execute(
                "SELECT id, title, source FROM jobs "
                "WHERE (company IS NULL OR trim(company) = '') AND source = %s ORDER BY id",
                (SOURCE,),
            )
            rows = cur.fetchall()

        planned, skipped = [], []
        for row in rows:
            company, reason = extract(row["title"])
            (planned if company else skipped).append((row, company or reason))

        print(f"cards with no company from {SOURCE}: {len(rows)}")
        print(f"  recoverable: {len(planned)}")
        print(f"  skipped:     {len(skipped)}")
        for row, reason in skipped:
            print(f"    [{row['id']}] {str(row['title'])[:56]:58s} {reason}")

        print("\nfirst 10 of what would be written:")
        for row, company in planned[:10]:
            print(f"    [{row['id']}] {company:24s} <- {str(row['title'])[:52]}")

        if not apply:
            print("\nDry run. Re-run with --apply to write.")
            return 0

        with conn:
            with conn.cursor() as cur:
                for row, company in planned:
                    cur.execute("UPDATE jobs SET company = %s WHERE id = %s", (company, row["id"]))
        print(f"\nWrote company on {len(planned)} cards.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
