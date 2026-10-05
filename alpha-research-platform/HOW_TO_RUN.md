# How to run Alpha Research

## Start the existing installation

1. Make sure PostgreSQL is running.
2. Open PowerShell and run:

   ```powershell
   Set-Location .\alpha-research-platform
   .\scripts\Start-Research.ps1
   ```

   If already inside the project folder, run only the launcher command.

3. Leave that window open and visit **http://127.0.0.1:8765** in your browser.

The launcher applies database migrations before starting the server. It uses the
project's virtual environment, or the bundled Codex Python runtime if the virtual
environment's original Python installation is missing. Dependencies must already
be installed in `.venv`.

If PowerShell blocks the script, run this command from the project folder:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-Research.ps1
```

This execution-policy option applies to that PowerShell process only.

## First-time installation on another computer

Install Python 3.11 or later and PostgreSQL. From the project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On macOS/Linux, use `python3 -m venv .venv`, then `source .venv/bin/activate`
and `python -m pip install -r requirements.txt`. Run `python -m alembic upgrade head`
and `python -m alpha_platform.cli serve` after configuring PostgreSQL.

If `.env` does not exist, copy `.env.example` to `.env`. Keep an existing `.env`:
it contains your database configuration.

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
```

Edit the `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, and
`POSTGRES_PASSWORD` values to match your database. Alternatively, set
`DATABASE_URL`. The database must already exist; migrations create its tables.
The migration user needs permission to create the `pgcrypto` and `pg_trgm`
extensions.

If using the included Docker setup instead of an installed PostgreSQL service,
start Docker Desktop and run:

```powershell
docker compose up -d postgres
```

Use either the installed service or the container on port 5432. For an existing
Docker database volume, use the credentials that initialized that volume.

Then run `.\scripts\Start-Research.ps1` and open the browser address above.

## Use the workspace

1. Open **Sync with BRAIN**, sign in with your BRAIN email and password, and
   complete biometric verification if requested.
2. Select the region, delay, universe, dataset, coverage filters, and field count
   you need. Sync a focused selection rather than the entire catalogue.
3. Open **Research Labs** and create a project. Manual mode requires no LLM API
   key. For provider-assisted research, configure the provider in **LLM Integration**.
4. Save the project, review its expressions and settings, then start simulations.
   Saving alone does not launch simulations.
5. Review progress, errors, and completed results in the project or **Simulation
   Matrix**. Validate qualifying alphas before using them further.

### Browse synced data

Open **Data Explorer**, select a scope such as **GLB · Delay 1 · TOP3000**, choose
a category such as **Fundamental**, and click **Open fields** beside **Global
Fundamental Data**. The breadcrumb shows the current scope, category and dataset.
You can also click **Browse data** beside a saved scope in **Sync with BRAIN**.

Dataset rows show how many fields are actually synced, not the total available
on BRAIN. Use the instrument/date coverage filters and search to narrow results.
Fields also show BRAIN's reported alpha count. The default bounds are **>5** and
**<500**, so only counts 6–499 qualify. Blank bounds remove that constraint;
fields with an unavailable count cannot satisfy an active bound. These counts
describe reported usage, not alpha quality. **Export dataset as JSON** exports
all saved fields matching the current filters, across all pages, together with
dataset metadata and scope. It does not export historical market observations.
If a dataset has no downloaded fields, open it and choose **Sync this dataset**
to configure a download. Review the limits: sync refreshes that scope's saved
selection; it does not automatically download every dataset.

PostgreSQL stores datasets in `catalog_dataset` and fields in `catalog_field`,
with compound keys including the market scope. Each row retains its original
metadata alongside indexed category, dataset and coverage columns. Search,
filtering and pagination run in the database. Existing JSON snapshots are kept
as compatibility backups, and are no longer loaded for catalogue browsing.

The default local daily limit is **5,000 simulations per UTC day**. It is configured
with `DAILY_SIMULATION_BUDGET=5000` in `.env`. BRAIN's reported quota can reduce
the available amount. Campaign concurrency can be selected from **1 to 200**;
existing projects retain their selected value, and BRAIN's limits still apply.
After four candidates qualify, the app stops dispatching new work. Already
running simulations finish and may produce additional qualifying candidates.

## Develop an existing alpha

To retrieve alphas you have already submitted on BRAIN, sign in through **Sync
with BRAIN**, then open **Alphas → Submitted history → Sync all submitted
alphas**. The importer downloads every account-visible submitted page. It shows
the actual record count and download progress; you can stop after the current
page and resume later. A failed or interrupted refresh retains the last complete
saved history. **Refresh all submitted alphas** updates existing records and adds
new ones. Search, region and delay filters only narrow the table, while **Export
saved history as JSON** exports all saved records for the current account.

Use **Inspect** to view BRAIN's full returned record. **Develop alpha** copies
the expression and settings of a supported regular alpha into a new research
plan. Sync that alpha's catalogue scope first if it is not yet available. The
imported in-sample metrics can serve as a reference, but the app cannot assume
matching historical sample periods, parent correlation, or an absent score.
Importing history starts no simulations and uses no simulation quota. A renewed
BRAIN login may be needed if your saved session has expired.

In **Research Labs → New research**, choose **Develop an existing alpha**, or use
**Develop alpha** beside a saved simulation result. Paste the complete parent
expression, choose its catalogue scope and simulation settings, and enter your
objective and thresholds. No LLM provider key is required for this mode.

Set research windows such as `40, 20, 60, 126`. The first window is the base
experiment; the others test nearby alternatives. Save to generate a reviewable
plan without launching simulations. Missing parent fields are looked up on BRAIN
in the selected scope; authentication is required for those lookups.

The plan isolates additive components, then changes one ranked component at a
time into a trend slope, trend residual, or historical standardized reading.
Slope uses `rettype=2`; residual uses `rettype=0`. A two-rank additive expression
also receives a joint-strength experiment. Explicit subindustry neutralization
is retained. These are research hypotheses, not claims of predictive performance.

After you start, component diagnostics finish before structural revisions begin.
Diagnostics do not count toward the four qualifying candidates. Follow-up windows
run only for base branches with positive Sharpe and fitness and turnover within
your threshold. The plan is capped at your attempt limit or 200 candidates,
whichever is lower. Already simulated structures remain subject to duplicate checks.

Parent comparisons use saved local results under exactly matching settings. If
none exist, baseline metrics remain unknown; the app does not resimulate the
submitted parent merely to obtain a comparison. Candidate review shows metric
differences when available. Parent return correlation, independent period tests,
and submissions made outside this app are not inferred. This mode preserves
documented parent fields; field substitutions remain available in manual research.

Final submission is your decision. This mode generates and evaluates candidates;
it does not automatically submit them as production alphas.

## Stop and restart

To stop new campaign work, use **Stop after current work** and wait for current
simulations to finish. To shut down the server, press **Ctrl+C** in its PowerShell
window. Start it again with the same launcher and refresh the browser.

Saved projects and results remain in PostgreSQL. An interrupted campaign does
not automatically resubmit simulations. Inspect its saved runs before using
**Continue queued experiments**. Unknown remote outcomes block safe resumption.
Stopping tracking of an individual alpha does not cancel an already accepted
simulation on BRAIN.

Provider keys entered through the interface must be entered again after a server
restart. BRAIN may also require renewed authentication.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Browser cannot open the app | Keep the server window open and check its startup message. |
| Database connection or migration fails | Start PostgreSQL and check `.env`, database existence, and migration permissions. |
| Port 8765 is already in use | Check whether the app is already running. Avoid starting a second server. |
| Virtual environment points to missing Python | The launcher tries bundled Codex Python; if unavailable, recreate `.venv` and reinstall requirements. |
| BRAIN returns 401 | Sign in again through **Sync with BRAIN**. |
| BRAIN returns 429 | Follow the displayed wait instructions; repeated submissions will not speed up processing. |
| Catalogue sync stops midway | Retry the same settings to reuse saved pages. Changing the selection starts a different checkpoint. Temporary progress-file locks do not stop a download. |
| App daily limit reached | Check `DAILY_SIMULATION_BUDGET` and restart after changing it. The maximum is 5,000. |
| Simulation outcome is uncertain | Inspect saved runs and BRAIN before continuing; avoid submitting the same alpha again blindly. |

For a database and configuration check with a working virtual environment:

```powershell
.\.venv\Scripts\python.exe -m alpha_platform.cli doctor
```

Keep `.env` and cached authentication files private.
