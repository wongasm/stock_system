# Sales Report

The existing `/sales_report` endpoint now reads durable SQLAlchemy tables in the application's configured database. The existing navigation, administrator login rules, navbar and shared styles are reused. The local Flask startup block now runs after all routes are registered. Inventory order tables and deduction logic are untouched.

## Sources inspected

- `app.py`: old sales view downloaded Square orders for each date/store request, including OPEN orders.
- `square_api.py`: existing per-store Square credentials (`*_ACCESS_TOKEN`, `*_LOCATION_ID`) and Orders API.
- `models.py`: SQLAlchemy database, inventory-oriented `SquareOrder`/`SquareOrderLine`, and expiring `ApiCache`. MySQL-specific existing schema helpers indicate the deployment uses MySQL; the actual connection was not available locally.
- [Sales Report workbook](https://docs.google.com/spreadsheets/d/1qCSRiaDr36iPNCTsA0H6yfcCa1hacpuhEDHCviIt3e4/edit): Line Graph and weekly tabs, including Week 0609. Weekly rows contain store/day sales, previous-week change, order count, average sale, best-selling bingsu, drinks revenue/share, waffle variants/revenue/share, specials and targets. CBD corresponds to the Lonsdale store in the graph. Glen corresponds to Glen Waverley.

The sheet is a reference, not a second additive transaction source. Its weekly totals must not be added to Square transactions (that would double count). Recompute comparisons from source amounts: percentage cells in the workbook use inconsistent scales. Historical targets and manual special-product labels are not inferred as current settings.

## Persistence and incremental sync

Three additive tables are registered before the application's existing `db.create_all()`:

- `sales_report_order`: composite store/order identity, timestamps, Melbourne business date, integer AUD cents, status, refunds and reporting detail.
- `sales_report_line`: replaceable product facts, decimal quantities and category mapping.
- `sales_report_sync`: per-store location identity, successful watermark, active window/cursor, processed count, error and worker lease.

Initial import searches all available Square history by UPDATED_AT from 1970 to a fixed run boundary. Each request retrieves at most 100 orders. Order/line replacements and the next cursor commit in one transaction. Only a fully completed run advances the watermark. Failed batches roll back and are retried from the last committed cursor; duplicates are safe. If Square expires a cursor, Rebuild all history starts a fresh search.

Subsequent runs query UPDATED_AT from the successful watermark minus a one-hour overlap. This captures updates to old orders and delayed orders, not just recently created sales. All order states are fetched so a cancellation removes the order's contribution and line facts. A three-minute database lease and conditional write lock prevent concurrent workers from committing over each other. The location ID is pinned per store to prevent accidentally mixing a changed account into existing history.

Rebuild re-reads all history and replaces matching records while retaining saved results. It does not delete records missing from a response. Historical Square deletions outside normal canceled-order updates require separate reconciliation. No inventory stock deductions run during reporting imports.

[Square search documentation](https://developer.squareup.com/docs/orders-api/manage-orders/search-orders) and [date filter reference](https://developer.squareup.com/reference/square/objects/SearchOrdersDateTimeFilter) support UPDATED_AT filtering with matching sort, pagination and inclusive boundaries.

## Operation

1. Deploy the changed files to the existing application environment and reload the application. Existing startup creates the three new tables; the database user needs CREATE permission. `requests`, Flask-SQLAlchemy and Flask-Login are required (the latter two are already app dependencies). Use a supported Python/OpenSSL runtime.
2. Sign in as an administrator, open Sales Report and click **Sync new / changed sales** to import history. Keep the page open for continuous batches. Leaving the page retains completed batches; click sync again to resume.
3. The existing `refresh_cache.py` now also runs up to ten sales batches per store each time it executes. An already configured scheduled/always-on cache job therefore resumes history and syncs changes automatically after deployment. This change does not create a new scheduler.
4. Use **Load saved report** for quick reads. Use **Rebuild all history** to reread the source after corrections or a cursor error. Last completion, partial progress and errors appear per store.

## Metric definitions and limits

- Default date range: current Monday through today in Australia/Melbourne; inclusive dates and DST-aware conversion. Comparisons use the preceding equal-length period. Day and Monday-week charts are available. Partial boundary weeks contain only selected dates.
- OPEN and COMPLETED order totals, including Square's tax/discount calculation, before refunds. DRAFT and CANCELED orders are excluded, matching the owner's reporting rule. It is not a payment settlement or accounting net-sales report.
- Refunds are completed refunds embedded in the saved order and attributed to the original order date. This is not a refund-event ledger. Product quantities/revenue are before returns.
- AUD only; another currency causes a visible batch failure instead of silently adding currencies.
- Categories reuse `ITEM_CATEGORY_MAP`, with explicit waffle names/variation labels from the workbook. Ambiguous names such as Biscoff retain their existing mapping unless the variation identifies a waffle. Product catalog mapping should be reconciled against live Square before relying on exact category shares.
- Database aggregates produce KPIs, comparisons and product mix; individual transaction detail has been replaced by an expandable weekly archive. Date ranges are limited to two years per view, with all imported history retained.
- First-import warnings distinguish incomplete history from a genuine zero. Saved results remain available during source outages.

## Validation

Nine isolated database/route/API tests cover idempotency, changed and canceled orders, Melbourne date conversion, decimal quantities, comparison periods, page rollback, cursor resume, watermark overlap, rebuild retention, busy/expired workers, stale versions, query shape, HTTP failure, authentication, administrator authorization, CSRF and date validation. Run `python -m pytest -q test_sales_reporting.py` in a test environment.

The page was rendered with synthetic data in the real navbar/shared CSS, and weekly chart grouping and daily detail were exercised in a browser with no JavaScript errors. No production database, live credentials or real Square import was available in this local checkout. Live import and production MySQL verification remain deployment checks.

## September 15 import diagnostics fix

Sync window end timestamps now use whole seconds before the first Square request. Default MySQL DATETIME columns discard fractional seconds; previously the next page could use a different end timestamp from the first page, violating Square's requirement to keep cursor queries identical. An already rejected cursor may require Rebuild all history after deployment; saved orders remain in place. This code-level defect has been reproduced by simulating MySQL timestamp precision. It has not yet been confirmed as the cause of the reported production totals.

The page now shows saved order counts and date bounds, selected-period amounts by order status, and warnings for unfinished, failed or out-of-date syncs. Date bounds are explicitly not evidence of complete coverage. Added regression tests verify cursor-bound stability and date/store totals across more than 100 orders (independent of transaction pagination). Eleven tests pass.

## OPEN orders included

At the owner's request, OPEN and COMPLETED orders now contribute to sales, order counts, averages, comparisons, charts and product detail. DRAFT and CANCELED orders do not. Refund status filtering remains COMPLETED. Existing saved OPEN orders are included immediately; missing product projections from older imports are read from their saved line-item payloads. A later sync projects those lines normally, with no duplicate contribution. No Square rebuild is required for already-saved orders. Regression coverage includes legacy payload detail, prior-period OPEN sales, state transitions, exclusions and rendered labels; all thirteen tests pass.

## Weekly archive

The transaction list and its pagination query have been removed. All weeks with saved OPEN/COMPLETED sales appear newest first, independently of the top date range and scoped to the selected store. Week boundaries are Monday–Sunday using saved Melbourne business dates. Each expansion requests one authenticated administrator-only week endpoint, showing seven daily rows per store, prior-week comparisons, totals, averages, bingsu best sellers, drinks/waffle revenue and shares, and product totals. Missing days are marked as having no saved orders. Existing sync warnings remain.

Week details load on first expansion and are reused until the page reloads; failed requests offer Retry. Both the list and details read saved data without contacting Square. No records are deleted. Fourteen tests pass, including week boundaries, filtering, authorization, input validation and lazy loading. Browser checks with sample data verified expansion and product detail.

## Refresh saved categories after editing mappings

After updating `ITEM_CATEGORY_MAP` in `square_helpers.py`, run from the deployed project with its virtualenv active:

```bash
python -m flask --app app recategorize-sales
```

Optionally use `--store Doncaster` or `--batch-size 200`. The command reads saved order payloads to preserve the existing waffle variation rule and updates only category fields in saved sales lines. It commits short batches, prints progress, makes no Square requests, preserves order amounts and sync checkpoints, and is safe to rerun after interruption. Legacy OPEN orders without projected lines already use the current mapping from their saved payloads. Reload the web app to load edited Python mappings, then refresh the report (including expanded weeks) after the command completes. Editing Square catalog categories does not change this hardcoded map. Fifteen tests pass, including category changes, store scope, idempotency and preservation of monetary values and sync state.

## Faster initial loading (October 2026)

The initial page now runs only the selected/prior-period order aggregation and the
small sync-status query. It no longer scans all historical orders for the archive
and coverage, groups product lines, or reads legacy OPEN payloads before showing
sales and store comparison.

- **Product mix and daily breakdown** loads on expansion; **Load item totals**
  opens the same section and fills the Items sold card. The card shows a dash,
  not a misleading zero, before loading.
- **Weekly breakdown · all saved history** loads the week list on expansion;
  each week still loads its details separately.
- **Saved data coverage** loads on expansion inside Sync status and history.
- Each section loads once per page, with duplicate-request protection, a loading
  state and a retry button. Reload the report after syncing or recategorizing.
  Responses use `Cache-Control: no-store`; no persistent result cache can hide
  category or status updates. All endpoints enforce administrator login and
  validate date/store filters. No schema change or new history import is needed.

Validation: 17 sales-report tests pass, including a query-count regression that
allows only two report queries on initial load and rejects payload/line reads.
Browser checks with sample data verified item totals and nested archive/week
expansion. A local in-memory SQLite benchmark with 120,000 orders and 240,000
lines compared the old eager data workload against the initial summary workload
(median of three runs, excluding rendering/network): seven days 54.5 ms → 1.2 ms;
one year 245.0 ms → 39.1 ms. Sales, order counts, prior totals, store totals and
chart values matched. These are sample-data measurements, not production MySQL
or end-to-end browser timings. Expanding a large detail range still performs its
full calculation on demand.

Deploy `sales_reporting.py`, `templates/sales_report.html`, and all four new
`sales_report_{macros,details,history,coverage}.html` templates together, then
reload the PythonAnywhere web app. Existing scheduled sync jobs remain unchanged.

## Dashboard saved-data loading (October 20, 2026)

The dashboard now reads `sales_report_order` and `sales_report_line` directly.
These are the same durable tables used by Sales Report, so existing history is
reused without a new import or schema migration. Today and the seven-day trend
use Melbourne business dates. Only today's product totals and any legacy OPEN
orders without projected lines are read for the item count and top sellers.
The initial dashboard, its summary endpoint, and the admin login redirect never
call Square, even when data is missing, old, or the URL contains `refresh=1`.

After the saved page is displayed, JavaScript updates stores whose sync
checkpoint is at least five minutes old. **Update sales** explicitly checks all
stores. Both use the existing administrator-only, CSRF-protected sales sync
endpoint. Each request saves one page, retains its cursor, and continues from
the last successful UPDATED_AT checkpoint with the existing one-hour overlap.
This catches changes/cancellations to older orders as well as new orders and
does not double count. Page updates are limited to three rounds and stop
starting new batches after 45 seconds; an in-flight request has a 60-second
browser deadline. Larger backlogs resume with another click, in Sales Report,
or in the scheduled job. Completed rounds replace the dashboard figures from
the database without reloading the page. Failures retain the displayed figures,
and expired login sessions offer a sign-in link.

Sales sync status and incomplete first imports are visible. A database with no
saved sales or completed checkpoint shows dashes rather than asserting zero
sales. Stock and invoice figures still read the local database; invoice totals
are now aggregated in SQL rather than loading every matching invoice line.

The scheduled `refresh_cache.py` job gives each store one batch per round, up to
ten rounds, before refreshing loyalty. Dashboard loyalty has its own last-good
snapshot at `api_cache.dashboard_loyalty_v1`; page views only read it. The warmer
reuses the member snapshot for both loyalty summaries instead of scanning every
member twice. Failed summaries do not overwrite the last good result. This one
dashboard snapshot is excluded from the existing 24-hour cache cleanup, and an
older week's figures are explicitly labeled with their week. Loyalty events
remain a periodic weekly snapshot, not an incremental event ledger.

Login now displays invalid credentials and form/CSRF validation errors. Startup
checks the MySQL cache column type before ALTER TABLE, avoiding repeated ALTERs
when the payload is already LONGTEXT. Authentication duration is unchanged.

Deploy `app.py`, `dashboard_data.py`, `square_api.py`, `refresh_cache.py`,
`templates/dashboard.html`, `templates/dashboard_content.html`,
`templates/login.html`, and `static/dashboard.js` together, then reload the
PythonAnywhere web app. From the deployed project with its virtualenv active,
run `python refresh_cache.py` once to seed the separate loyalty snapshot. Keep
the existing scheduled/always-on job running; an always-on command is
`python refresh_cache.py --loop 300`. Avoid creating a duplicate job if one
already exists. Sales can also update while the dashboard is open; loyalty
refresh depends on the background job. No Square history rebuild is needed.

Validation: run `python -m pytest -q test_dashboard.py test_sales_reporting.py`.
Tests cover cold/stale/forced page reads without external calls, four bounded
sales queries, Melbourne date boundaries, OPEN legacy lines, incremental updates
and cancellations, authentication/CSRF, visible login errors, invoice totals,
last-good loyalty retention, member snapshot reuse, store sync fairness, and
conditional schema widening. Browser checks with synthetic sales verified
login, automatic refresh failure, successful retry without navigation, old
refresh URLs, and expired-session recovery. Production timings and background
job status still need verification after deployment.
