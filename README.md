# ConCrew - Volunteer Tracker

Standalone volunteer hour tracking + reward fulfillment + scheduling app for
the con.

## Roles

| Role       | Can do |
|------------|--------|
| `admin`    | Everything: manage users/departments/tiers, view all volunteers, approve/reject any hours, fulfill/unfulfill any claim, export CSV, view history, manage the calendar |
| `assigner` | Log + approve/reject hours for their assigned department(s), view their roster, fulfill claims, manage calendar slots for their department(s), view history |
| `constore` | View the claims matrix and mark items fulfilled (single-person signoff) — no hour-logging access |
| `volunteer`| View their own hours, tier progress, reward checklist, profile/QR code, and the shift calendar |

Reward fulfillment is **single-person signoff** — any admin, assigner, or
constore user can mark an item fulfilled. Only admins can undo a fulfillment
(prevents accidental/malicious un-signing at the merch table).

## First-run setup

The very first time the app is opened with no con and no admin account yet,
every request redirects to `/setup` — a wizard that creates both the con
info (name, dates, contact email) and the first admin account in one step.
You'll only see this once; after that it behaves like a normal login-gated
app.

## Feature tour

**Calendar & shift scheduling** — Admins/assigners define shift "boxes" on
a per-day calendar (date, department, start/end time, capacity). Volunteers
browse the calendar and request specific slots; admins/assigners approve or
deny requests, and the calendar shows who's confirmed for what at a glance.

**Click-and-drag availability** — Separately from requesting a specific
shift box, volunteers can click-and-drag (or touch-and-drag on mobile)
across a day/time grid to mark themselves generally available. This is
informational for staff (shown on the calendar under each day as "who's
around"), not a shift request — it doesn't get approved or denied.

**Shift eligibility** — Volunteers only see and can request open shifts for
departments they've been explicitly approved for. Admins manage this for
every department; assigners manage it only for the department(s) they run,
from Manage → Shift Eligibility. A volunteer with no approvals sees a
message telling them to ask staff for access rather than an empty calendar.

**Volunteer homepage** — Shows pending and confirmed shift requests at a
glance, alongside hours/tier progress and the reward checklist.

**Admin/assigner homepage** — Shows pending hour approvals *and* pending
shift requests needing a decision, scoped to what that assigner manages.

**User profile / homepage** — Every user has a self-service profile page to
update their name, badge name, email, and password. It also shows their
auto-generated `usercode` (e.g. `FBX4821`) and a QR code. Scanning the QR
takes staff straight to a pre-filled "log hours" form for that volunteer —
handy for a check-in table.

**Reward claim search** — The reward/claims page no longer dumps every
volunteer at once; staff search by name or usercode first, then see that
volunteer's claim status and can mark items fulfilled.

**History page** — Admins and assigners get a searchable log of recent
actions (hour approvals/rejections, reward fulfillments, shift approvals,
user creation/deletion) — filterable by staff name, detail text, or action
type.

**User management** — Admins can create accounts, reset passwords (shown
once), deactivate accounts (blocks login, keeps all history), and delete
accounts that have no hours/claims/shift/audit history attached (otherwise
the delete is blocked with a suggestion to deactivate instead, so records
never silently vanish). "Kiosk" accounts auto-generate a username and
password for shared check-in stations without needing an email address.
Login accepts either an email or a kiosk username.

**Mobile-friendly nav** — The navbar collapses behind a hamburger menu on
small screens, with admin/assigner tools grouped under a "Manage" dropdown
to keep the bar from overflowing.

## Local setup (Windows dev)

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env
# edit .env, set a real SECRET_KEY

set FLASK_APP=run.py
flask db init
flask db migrate -m "initial schema"
flask db upgrade

python run.py         # http://localhost:5000
```

On first load you'll hit the setup wizard automatically. For a quick test
environment with sample data (Furpocalypse 2026, con dates 7/16–7/20/2026,
several departments, the full reward tier structure, sample calendar slots,
and test logins for every role) skip the wizard and run instead:

```powershell
python seed.py
python run.py
```

Test logins created by `seed.py` (change these passwords before real use):

- `admin@example.com` / `changeme`
- `assigner@example.com` / `changeme` (manages Con Ops dept)
- `constore@example.com` / `changeme`
- `volunteer@example.com` / `changeme`

## Docker (same on Windows Docker Desktop or Ubuntu)

```bash
cp .env.example .env
docker compose up --build
```

Then, in a second terminal, run migrations + seed inside the container once:

```bash
docker compose exec web flask db upgrade
docker compose exec web python seed.py
```

The SQLite file lives in the `./instance` volume so it survives container
rebuilds. When you're ready for a heavier-duty deployment, set
`DATABASE_URL` in `.env` to a Postgres connection string — the app doesn't
need any code changes, SQLAlchemy handles the swap.

## Deploying to Ubuntu

Same `docker-compose.yml` and `Dockerfile` work unchanged:

```bash
git clone <your repo> volunteer-tracker
cd volunteer-tracker
cp .env.example .env   # set a real SECRET_KEY
docker compose up -d --build
docker compose exec web flask db upgrade
docker compose exec web python seed.py   # first run only
```

Put this behind whatever reverse proxy you're already using for Con
Planner / Errand (nginx, Caddy, Tailscale, etc.) and point a subdomain at
port 5000.

## Reward structure (seeded by seed.py)

Tiers cascade automatically — the sync logic grants every item at or below
a volunteer's current tier, so each tier below only needs to define the
*new* items it introduces:

- **4h** — Special Volunteer lanyard/patch/badge
- **8h** — + Event T-Shirt (current year)
- **12h** — + Convention Hoodie, $10 hotel restaurant voucher
- **24h** — + Event glass, con-themed lanyard & patch, complimentary basic
  registration (next year), staff application eligibility

To change thresholds or items, edit `tier_items` in `seed.py` and re-run it
(it's idempotent — safe to run repeatedly), or edit rows directly via
`flask shell` once you're comfortable with the models.

## What's intentionally NOT in this version

- Weighted hours (flat hours only for now — `HourEntry.hours` is a single
  float, so adding a weighting multiplier later is a small model change,
  not a rebuild)
- Editing departments/tiers/reward items through the UI (use `seed.py` /
  `flask shell` for now — the admin Tiers page is read-only)
- Approved shift requests don't auto-create `HourEntry` rows -- scheduling
  and actual worked-hour logging are deliberately separate
- Availability blocks are informational only -- marking yourself available
  doesn't request or confirm a shift by itself
- Self-service hour submission by volunteers (only admins/assigners log
  hours, keeping a single source of truth for what happened)
- Editing a user's role/departments after creation through the UI (delete
  and recreate, or use `flask shell`, until that's prioritized)

## Architecture notes

- `HourEntry` (raw logged time, pending/approved/rejected) is kept
  separate from `RewardClaim` (eligibility + fulfillment state) so
  approving hours and handing out a t-shirt are independently auditable
  events — see `AuditLog` for the trail of who approved what and who
  fulfilled what.
- `sync_reward_claims()` runs after every hour approval and creates
  `eligible` claim rows for any newly-unlocked items. It never downgrades
  existing claims automatically — if hours get corrected downward after a
  claim was already fulfilled, resolve that manually via the admin panel.
- `ShiftSlot` (the calendar "boxes" staff post) and `ShiftRequest` (a
  volunteer's ask to work one) are separate from `AvailabilityBlock` (a
  volunteer's free-form "I'm around then") and from `HourEntry` (what
  actually got worked) — four different concepts that all look like
  "calendar stuff" but answer different questions.
- `VolunteerDepartmentEligibility` gates which `ShiftSlot`s a volunteer can
  see/request. This is enforced server-side in `calendar/routes.py`, not
  just hidden in the template, so a volunteer can't request an
  unauthorized department's shift by hand-crafting the POST.
- User deletion is deliberately conservative: `User.has_history()` blocks
  hard deletes for anyone with hours, claims, shift requests, or audit log
  entries on record, since deleting them would either cascade-destroy that
  history or orphan foreign keys. Deactivation is the safe path for anyone
  who's actually done anything in the system.
- CSRF protection is enabled globally (`Flask-WTF`'s `CSRFProtect`), so
  every POST form — including the plain HTML ones outside the login page —
  carries a hidden `csrf_token` field. The availability-grid AJAX calls
  send it as a form field in the `fetch()` body.
