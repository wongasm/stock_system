# Omnisend setup check

## Hourly points sync

Pull the latest code on PythonAnywhere from the stock_system directory. Use the
same Python interpreter/environment that successfully ran the initial import.

First preview (no Omnisend changes):

```bash
python -u omnisend_sync.py --scheduled
```

Then in PythonAnywhere → Tasks add an **hourly** task, minute **10**:

```bash
cd /home/wongasm2/stock_system && python -u omnisend_sync.py --scheduled --apply
```

If the console uses an activated virtualenv, replace `python` in the scheduled
command with the absolute path printed by `python -c 'import sys; print(sys.executable)'`
in that working console. Scheduled tasks do not inherit console activation.
The `.env` file is loaded from beside the script, not from the scheduler's directory.

This mode updates only changed `bc_loyalty_points` values for existing subscribed
contacts whose Square customer **and loyalty account** IDs match. It does not
create contacts, change subscription status, reset purchase tracking, or send
custom events. New customers must still go through the separate import process.
Unsubscribed, unlinked, conflicting and shared-email profiles are skipped.

Points changes can trigger enabled segment workflows. This command never enables
workflows; keep the loyalty email in draft until reviewed. Do not use the initial
import's `--automations-paused` flag for a live recurring points sync.

The entire refresh and sync is protected by a nonblocking file lock shared with
manual imports on this host. An overlapping run logs `skipped_already_running`.
Square refresh failures stop the run instead of uploading stale fallback data.
PythonAnywhere's task log receives UTC start/finish timestamps and final counts.
Failures log their exception class and exit nonzero, without credentials or
contact details. Completed writes remain saved. There are no immediate automatic
retries after an ambiguous write; the next hourly run re-reads each balance and
only writes differences. Check for a `finished` entry after the first run. This
does not configure alerts or purchase/return attribution.

Scheduled tasks: https://help.pythonanywhere.com/pages/ScheduledTasks
Custom property updates: https://api-docs.omnisend.com/reference/contacts#custom-properties

## Initial setup diagnostic

From the deployed stock_system directory, using the app's virtualenv:

```bash
python omnisend_setup.py --preview
```

Reads `OMNISEND_API_KEY` from the existing environment or `.env`. Tests Contacts read permission with one GET request, then summarizes local cached Square loyalty accounts and customer-directory readiness without printing contact details. No contacts are uploaded, channel statuses changed, events triggered or emails sent. The optional cache preview imports the existing Flask app and therefore runs its normal startup schema checks. The connection check alone (`python omnisend_setup.py`) does not import the app or access the database.

This is a setup diagnostic, not the customer-sync integration or a live automation. Customer-linked purchases, marketing-consent capture and synchronization, suppression handling, campaign enrollment, actual-send tracking and workflow configuration are still required. Current loyalty signup records only agreement to join Rewards. Existing email addresses are not automatically treated as subscribed. Missing purchase links must not be substituted with loyalty-profile update timestamps. Shared emails and duplicate customer links are flagged for review.

Sources: https://api-docs.omnisend.com/reference/get_contacts and https://api-docs.omnisend.com/reference/contacts

## Owner confirmation and first contact import

The owner has confirmed that loyalty enrollment requires email marketing opt-in and that all valid loyalty emails have opted in. The owner also confirmed that Omnisend workflows are disabled or drafts. Treat this as the supplied permission basis for this import; the cached data still has no individual historical consent timestamps. Do not fabricate those timestamps. This supersedes the earlier unresolved-permission assessment above.

The sync script refreshes loyalty accounts and the customer directory from Square and refuses stale fallback after a failed refresh. It skips missing/invalid emails, shared emails across the directory, duplicate customer links and unusable points. It transfers email, available first/last names, customer/account references, points and the stated permission basis. No phone/SMS subscription, visit dates, lifetime-spend estimates or marketing events are sent.

Start with up to five eligible profiles while every Omnisend workflow remains disabled:

```bash
python omnisend_sync.py --apply --automations-paused --limit 5
```

Omit `--apply` for a read-only Omnisend preview (Square snapshots are refreshed locally). After inspecting the imported profiles, remove `--limit 5` to process the full list. Existing subscribed contacts receive profile updates only; existing unsubscribed, nonSubscribed or unknown-status contacts are skipped. Existing identity conflicts are also skipped. New contacts are registered as email subscribers on the basis of the owner's confirmation. Their statusChangedAt describes the import's subscription registration, not their original opt-in date. Do not run another contact-import process concurrently. Existing contact updates never send channel fields, and newly registered statuses use the run-start timestamp so a newer unsubscribe takes precedence.

The command is deliberately manual for this initial setup. Contact changes can trigger enabled workflows even without event calls, which is why `--apply` requires `--automations-paused`. Keep workflows paused on retries too. It makes no automatic retries after failed/ambiguous writes; previously saved updates remain. A rerun looks up each email again before writing. Counts are logged without contact details or API credentials. Marketing enrollment, purchase linkage, actual-send tracking and background scheduling remain future integration work. Twenty-seven tests pass against simulated API responses; the live write check must run in the configured server environment.
