# Final fix report
1. courses.js receipt lines: money(), muted for discount lines.
2. courses.js localTime() (fr-FR local, null-guarded) for last_success_at.
3. courses.py summary: total_compare added; test assertion "13.00" in test_comparison_period; header tile shows variation.
4. mailsync.py sync_once: final `except Exception` -> global_error; test_unexpected_error_is_recorded_not_raised.
5. courses.js OUTCOMES.slip = "ticket CB enregistré".
6. ui.js receiptHref(): keeps from/to/cmp/acc from location.hash.
7. _transactions.py receipt_id subquery: AND r.status = 'ok'.
8. mailsync.py unused `import re` removed.
9. superu.py "CHEQUE DEJEUNER" + parametrized case.
10. courses.js alert pill opens/scrolls to details#courses-aisles.
Commands: pytest -q -> 460 passed (69s); node --check courses.js ui.js -> OK.
