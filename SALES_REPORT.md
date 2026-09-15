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
- Database aggregates produce KPIs, comparisons and product mix; transaction detail is paginated at 50 records. Date ranges are limited to two years per view, with all imported history retained.
- First-import warnings distinguish incomplete history from a genuine zero. Saved results remain available during source outages.

## Validation

Nine isolated database/route/API tests cover idempotency, changed and canceled orders, Melbourne date conversion, decimal quantities, comparison periods, page rollback, cursor resume, watermark overlap, rebuild retention, busy/expired workers, stale versions, query shape, HTTP failure, authentication, administrator authorization, CSRF and date validation. Run `python -m pytest -q test_sales_reporting.py` in a test environment.

The page was rendered with synthetic data in the real navbar/shared CSS, and weekly chart grouping and daily detail were exercised in a browser with no JavaScript errors. No production database, live credentials or real Square import was available in this local checkout. Live import and production MySQL verification remain deployment checks.

## September 15 import diagnostics fix

Sync window end timestamps now use whole seconds before the first Square request. Default MySQL DATETIME columns discard fractional seconds; previously the next page could use a different end timestamp from the first page, violating Square's requirement to keep cursor queries identical. An already rejected cursor may require Rebuild all history after deployment; saved orders remain in place. This code-level defect has been reproduced by simulating MySQL timestamp precision. It has not yet been confirmed as the cause of the reported production totals.

The page now shows saved order counts and date bounds, selected-period amounts by order status, and warnings for unfinished, failed or out-of-date syncs. Date bounds are explicitly not evidence of complete coverage. Added regression tests verify cursor-bound stability and date/store totals across more than 100 orders (independent of transaction pagination). Eleven tests pass.

## OPEN orders included

At the owner's request, OPEN and COMPLETED orders now contribute to sales, order counts, averages, comparisons, charts and product detail. DRAFT and CANCELED orders do not. Refund status filtering remains COMPLETED. Existing saved OPEN orders are included immediately; missing product projections from older imports are read from their saved line-item payloads. A later sync projects those lines normally, with no duplicate contribution. No Square rebuild is required for already-saved orders. Regression coverage includes legacy payload detail, prior-period OPEN sales, state transitions, exclusions and rendered labels; all thirteen tests pass.
