# Omnisend setup check

From the deployed stock_system directory, using the app's virtualenv:

```bash
python omnisend_setup.py --preview
```

Reads `OMNISEND_API_KEY` from the existing environment or `.env`. Tests Contacts read permission with one GET request, then summarizes local cached Square loyalty accounts and customer-directory readiness without printing contact details. No contacts are uploaded, channel statuses changed, events triggered or emails sent. The optional cache preview imports the existing Flask app and therefore runs its normal startup schema checks. The connection check alone (`python omnisend_setup.py`) does not import the app or access the database.

This is a setup diagnostic, not the customer-sync integration or a live automation. Customer-linked purchases, marketing-consent capture and synchronization, suppression handling, campaign enrollment, actual-send tracking and workflow configuration are still required. Current loyalty signup records only agreement to join Rewards. Existing email addresses are not automatically treated as subscribed. Missing purchase links must not be substituted with loyalty-profile update timestamps. Shared emails and duplicate customer links are flagged for review.

Sources: https://api-docs.omnisend.com/reference/get_contacts and https://api-docs.omnisend.com/reference/contacts
