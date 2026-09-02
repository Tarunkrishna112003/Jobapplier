# Jobapplier

Finds live US software roles at 100 visa-sponsoring companies, fills the
application forms from your profile, and **stops at the Submit button** so you
approve each one.

Nothing is ever submitted without an explicit click.

---

## Quick start

```bash
./web.sh                      # → http://127.0.0.1:8765, then press Start
```

The run walks companies **one at a time**. For each matching role it opens the
form, fills what it can, and parks — the browser window stays open and the UI
shows every answer it entered. You press **Submit application** or **Skip**, and
only then does it move on.

CLI equivalent, if you prefer it:

```bash
./run.sh resolve      # detect which ATS backs each company (cached)
./run.sh discover     # pull live postings, score, queue them
./run.sh queue        # see what's waiting
./run.sh prep         # fill forms, stop before Submit
./run.sh review       # approve submissions one by one
./run.sh status       # pipeline counts
./run.sh export       # everything to CSV
./run.sh manual       # portals with no API: pre-filtered search links
```

---

## How it works

```
US_100_Visa_Sponsor_Companies.xlsx
        │
        ▼
  companies.py     parse 100 companies + career portal URLs
        │
        ▼
  ats.py           which ATS backs each portal?
        │            1. URL pattern    (already an ATS host)
        │            2. page sniff     (ATS link inside the HTML)
        │            3. slug probe     (ask vendor APIs by company name)
        │            4. overrides      (Amazon, Microsoft, Google, Meta, Apple)
        ▼
  connectors/      pull live postings from public job-board APIs
        │          greenhouse · lever · ashby · smartrecruiters · workday · amazon
        ▼
  match.py         score against your resume + preferences, US-only
        │
        ▼
  db.py            SQLite tracker — status per job, survives re-runs
        │
        ▼
  fill.py          Playwright: open form, resolve every field, upload resume
        │          ── STOPS HERE ──
        ▼
  review.py        the only code that clicks Submit, and only after you approve
```

### Where the answers come from

`profile/profile.json` is the answer bank. `answers.py` maps arbitrary form
labels onto it with ~50 ordered regex rules, so "Are you legally authorized to
work in the United States?" and "Do you have the right to work in the US?" both
resolve to the same value.

Anything no rule matches is left **blank** and reported as unanswered rather
than guessed at. Fields still marked `TODO_CONFIRM` are never written to a form.

---

## What it will and won't do

**Handles:**
- Forms embedded in iframes (Stripe serves a Greenhouse embed — the filler
  finds the frame with the most fields and works inside it)
- Radio groups, native selects, file uploads, fuzzy option matching
  ("Yes" → "Yes, I am legally authorized")
- Portal login/signup using `secrets.json`, with the session saved in
  `.browser-profile/` so you only log in once per company

**Won't:**
- **Solve CAPTCHAs.** Most major boards use reCAPTCHA. The filler detects one
  and says so; you solve it in the open browser window before approving.
- Guarantee every portal works. 41 of 100 companies have a machine-readable
  job API; the rest need the click-through links from `./run.sh manual`.
- Submit anything on its own.

---

## Setup

```bash
python3.10 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp secrets.example.json secrets.json     # portal signup credentials
cp profile/profile.example.json profile/profile.json   # your answer bank
cp /path/to/your_resume.pdf profile/
```

### Before your first real submit

Open `profile/profile.json` and replace every `TODO_CONFIRM`:

| Field | Why it matters |
|---|---|
| `address.street` | Asked on most long-form applications |
| `links.github` | Asked on nearly every engineering application |
| `education[].gpa` | Required by some new-grad forms |
| `preferences.earliest_start_date` | Common screening question |
| `preferences.desired_salary_number` | Some forms reject a non-numeric answer |
| `work_authorization.*` | Asked on every application; get this exactly right |

Check `work_authorization` carefully — it is the most consequential block in
the file. "Authorized to work now" and "will need sponsorship in future" can
both be true at once; forms ask them as separate questions, so set each on its
own terms. A wrong answer here is attached to your name permanently.

`voluntary_self_id` defaults to declining every EEO question. Declining is
always valid and never disadvantages an application; change it only if you
want to self-identify.

---

## Configuration

`config.yaml` controls discovery and ranking:

- `role_families` — title keywords per role type, with `must` gates and `boost` terms
- `title_exclusions` — principal/staff/director/intern etc. are dropped
- `locations.preferred` — ranked tiers; earlier tiers score higher
- `locations.exclude_countries` — **hard reject**, so a "Dublin" posting that
  means Dublin, Ireland never reaches your queue
- `scoring.min_score` — floor for entering the queue (default 0.35)

---

## Security

`secrets.json`, `profile/profile.json`, your resume PDF, `.browser-profile/`
and `data/` are all gitignored — the repo ships `.example` templates instead,
so publishing it never exposes your phone number, address or portal password. The browser
profile holds live session cookies for every portal you log into, and `data/`
holds screenshots of completed forms containing your personal information.
**Do not commit them.** The web UI binds to `127.0.0.1` only.
