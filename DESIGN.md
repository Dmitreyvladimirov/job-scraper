# JobScraper review and kanban UI

Status: implementation-ready fallback design for DIM-19  
Inputs: DIM-23 through DIM-26 and the existing `/dashboard` visual language  
Artifacts: [`design/review-queue.html`](design/review-queue.html) and
[`design/kanban.html`](design/kanban.html)

## Direction

The tracker extends the existing dashboard instead of introducing a second
visual system. It keeps the dark background, compact typography, rounded
panels, and green success accent already used by `dashboard.py`.

The interface is optimized for one user reviewing a small stream of
high-signal jobs. Company, role, ATS score, source, and age are visible before
any action. Destructive or terminal actions always require an explicit reason.

## Design tokens

| Token | Value | Use |
|---|---:|---|
| Page | `#0f1117` | App background |
| Surface | `#1c1f2b` | Cards and columns |
| Raised | `#242836` | Hovered/dragged cards |
| Border | `#2a2d3a` | Dividers and controls |
| Text | `#f8fafc` | Primary text |
| Muted | `#94a3b8` | Metadata |
| Success | `#4ade80` | Accept, applied, positive score |
| Amber | `#fbbf24` | Reapplication warning |
| Danger | `#f87171` | Reject and failed mutation |
| Info | `#60a5fa` | Active drag target and links |

Spacing uses a 4 px base unit. Cards use 16 px padding, columns use 12 px gaps,
and all interactive targets are at least 40 px high. The content width is
fluid; review cards cap at 980 px while kanban scrolls horizontally below
1100 px.

## Shared shell

The header contains the product label and three stable destinations:
Dashboard, Review, and Kanban. The active destination has a filled surface.
The pending-review count is shown beside Review, not repeated as a large KPI.

Browser pages use `Referrer-Policy: same-origin`. The dashboard token must not
appear in links, query strings, DOM attributes, or rendered error messages.
Mutation requests send it only through `X-Dashboard-Token`.

## Review queue

Each card has three scan bands:

1. Role and company, with source and age directly below.
2. ATS score and short evidence chips. The score is prominent but does not
   visually overpower the role.
3. Reapplication warning, when present, followed by Open job, Reject, and
   Accept actions.

Accept is the primary action. Reject opens a focused dialog with a required
reason and optional notes. Suggested reason vocabulary:

- role_mismatch
- location_or_work_auth
- compensation
- company_or_product
- seniority
- duplicate_or_already_applied
- other

On success, remove the card and announce the result in a polite live region.
On failure, retain the card and show an inline error. Empty state copy:
“Review queue is clear.”

The amber reapplication guard is informational. It states the normalized
company, previous terminal outcome, and date. It never disables Accept.

## Kanban

Columns follow the canonical status vocabulary supplied by `db.py`; the
prototype uses New, Applied, Recruiter reply, Screen, Interview, Offer, and
Rejected. Each header shows a count. Cards show role, company, ATS score, and
last-change age.

Drag behavior:

1. Keep the original column and index in client state.
2. Highlight valid targets; do not imply client-side validation is sufficient.
3. Optimistically place the card and send the target status.
4. If the server rejects the mutation, restore the original position and show
   an error toast.
5. A move to a terminal/rejected status opens the same required-reason dialog
   before the request.

Keyboard parity is required for implementation: a focused card can enter move
mode, choose a status, confirm, or cancel. Drag is enhancement, not the only
status-change mechanism.

## States to implement

| State | Review | Kanban |
|---|---|---|
| Loading | Three skeleton cards | Skeleton in each visible column |
| Empty | Clear-queue message | Empty-column drop zone |
| Mutation pending | Disable card actions | Card at 70% opacity |
| Mutation failed | Inline card error | Rollback plus error toast |
| Auth unavailable | Read-only content; mutation controls disabled | Same |
| Server unavailable | Preserve current UI and allow retry | Preserve board |

## Accessibility and responsive behavior

- Text and essential icons meet WCAG AA contrast on their surfaces.
- Focus rings use a 2 px `#60a5fa` outline with 2 px offset.
- Dialog focus is trapped and returns to the invoking control.
- Status, errors, and successful actions are announced with `aria-live`.
- At widths below 720 px, review actions stack and remain full width.
- Kanban keeps 280 px columns and horizontal scrolling; it does not compress
  cards into unreadable narrow columns.
- Respect `prefers-reduced-motion`; no status meaning depends on animation.

## Implementation boundary

These files are design artifacts, not production templates. DIM-23 and DIM-24
should translate the structure into server-rendered templates while preserving
the existing `/dashboard` route unchanged. DIM-25 owns networked drag behavior,
server validation, and rollback. DIM-26 owns the batched guard query.

