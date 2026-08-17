# Handoff: JobScraper Review UI (Card Review + Kanban + Stats)

## Overview
Read-write extension of the existing `dashboard.py` (FastAPI + Chart.js on Railway): Card Review of scraper findings, Kanban funnel, mandatory rejection reason, stats additions, sources control, dark theme toggle. Sole user: Dimitry. Target stack per `SPEC_FRONTEND.md`: **FastAPI + Jinja2 + HTMX, no SPA**.

## About the Design Files
Files in this bundle are **design references created in HTML** — prototypes showing intended look and behavior, not production code. The task is to **recreate them as Jinja2 templates + HTMX fragments** in the JobScraper repo, following `SPEC_FRONTEND.md` (routes, data model) and existing code conventions. Open `Review UI Explorations.dc.html` in a browser; each option carries a visible id badge (1a, 2b, …).

## Fidelity
**High-fidelity.** Colors, typography, spacing are final and come from the bundled Classical design system (`_ds/classical-*/styles.css` — all values are CSS variables `--color-*`, `--font-*`, `--space-*`, `--radius-*`, `--shadow-*`). Port `styles.css` into `static/` and reference the variables; do not re-derive hex values by eye.

## Chosen directions (per option id in the HTML)
- **1a** Card Review — full-width rows, Notion-style inline expand (expanded row shows score breakdown, matched/missed keywords, why apply / why not, actions). States included: cooldown warning, incomplete-description warning, 🇷🇺 Russia warning, degraded pre-v1.1 card (NULL keywords/why_apply).
- **1c** ATS breakdown — plain color-coded numbers + **penalty reason line** under Penalty (field `penalty_reason`, see spec updates). Alternatives 1d/1e kept for reference.
- **1f** Kanban desktop — 7 hairline-parted columns, drag between columns. **1g** Ledger variant (inline status dropdown) kept as alternative; its dropdown mechanic is the desktop equivalent of mobile 1j.
- **1h** Rejection dialog — 5 selectable reasons, `geo_restricted_auto` shown read-only, submit blocked until a reason is picked (server also validates, 400).
- **1i / 1j / 1k** Mobile (390px) — review list; kanban via bottom-sheet status picker (1j, recommended); swipe variant (1k, optional — needs undo).
- **1l** Stats — rejection reasons bar list (hatched = auto-LLM, solid = manual), qualified→applied conversion.
- **2a** Dark theme — same layout, tokens flipped: ground `#1b1918` (warm near-black), text `#e8e4dd`, hairlines = text at 14%, accent one ramp step lighter (`--color-accent-400`). One class on `<body>`, toggle in nav, persisted in `localStorage`.
- **2b** Sources panel — per-source Fetched / Qualified / Yield% / Applied-from + 30d sparkline + on/off toggle (off = excluded from future runs; found vacancies stay).
- **2c** Stats period bar — presets 7d / 30d / Quarter / All time + custom calendar range; one selection re-queries all charts (single HTMX swap of the stats fragment).

## Interactions & Behavior
- Row expand (1a): plain details-toggle; actions live only in the expanded state (deliberate — forces seeing the breakdown before Apply).
- Kanban drag (1f): needs a small drag lib (e.g. SortableJS) posting to `POST /jobs/{id}/status`; HTMX swaps the card fragment.
- Status dropdown / bottom sheet (1g/1j): pure HTMX — GET fragment, POST status.
- Reject flow: any move to `rejected` opens the reason form (`GET /jobs/{id}/reject-form`); POST without valid reason → 400, form shows inline error.
- Hover/pressed/focus states come from the design system stylesheet (accent ramp, 2px accent `:focus-visible`) — do not restyle.
- Mobile tap targets ≥ 44px (buttons and status chips already sized so in mocks).

## Design Tokens
All in `_ds/classical-10239a4e-138c-4e2e-b164-ed85c94c7633/styles.css`. Key facts: ground `#f3f2f2`, text `#201f1d`, single accent `#b68235` with 100–900 ramps; headings Cormorant Garamond (≤600 weight), body Lora; color used as stroke/border, never fill; tabular numerals (`font-feature-settings:'tnum'`) on all figures; radius 4px scale; hairline dividers `var(--color-divider)`. Dark theme values in §2a above.
Score color coding: ≥85% of axis max → `--color-accent-700`, decent → `--color-accent-500`, weak/penalty → `--color-neutral-600`.

## Assets
No images. Icons: Lucide (https://lucide.dev), inline SVG, stroke-width 2.

## Files
- `Review UI Explorations.dc.html` — all options, browsable (badges 1a…2c). Needs `support.js` + `_ds/` alongside (included).
- `Current Dashboard (recreation).dc.html` — faithful recreation of today's dark dashboard, baseline reference.
- `_ds/classical-…/styles.css` — the design-system stylesheet (port this).
- `SPEC_UPDATES.md` — **required reading**: deltas to apply to `SPEC_FRONTEND.md` before implementation.

## Screenshots
`screenshots/` — viewport captures per option: 10-1i-1j-mobile.png, 11-1k-mobile-swipe.png, 12-1l-stats-additions.png, 01-2a-dark-theme.png, 02-2b-sources.png, 03-2c-stats-period.png, 04-1a-card-review.png, 05-1a-card-review-expanded.png, 06-1c-1e-ats-breakdowns.png, 07-1f-kanban-board.png, 08-1g-kanban-ledger.png, 09-1h-rejection-dialog.png. The HTML file remains the source of truth.
