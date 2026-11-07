# Plan and open business rules

## Phases
1. DONE – inspect, plan.  2. DONE (core) – foundation.
3. DONE – inventory ledger, electricity, job costing + pricing.
4. DONE – accounting, partners, distributions.
5. DONE – payroll + attendance.
6. DONE – dashboard data layer.
7. DONE – UI (17 pages), reports/exports, UI tests.
8. Next – imports, refunds/credit notes, failure-injection tests, Windows verification + packaging.
4. Partners/profit distribution, payroll, sales/payments.
5. UI + dashboard + reports.  6. Hardening.  7. Packaging/docs.

## Assumptions made (configurable later)
- Money stored as integer minor units (paisa); quantities as Decimal text with fixed scale.
- Weighted-average cost; negative stock blocked by default.
- Timestamps UTC ISO-8601; displayed in local time (Asia/Karachi).
- Roles: owner, accountant, inventory_manager, production_supervisor, auditor. Owner-only: distributions, restore, user mgmt, ownership change, job finalisation.

## Questions that materially affect money/inventory (need your answer before Phase 3-4)
1. Is paper priced/consumed per meter only, or must individual physical rolls (with remainders) be tracked?
2. Fiscal year start (July 1 for Pakistan, or January 1)?
3. Salary: which types do you really use (monthly/daily/piece-rate)? Overtime rate rule?
4. Electricity: do you have per-machine meters, or use an hourly rate?
5. Single cash account, or cash + bank accounts?
6. Does "Inventory Responsiveness" (from your notes) mean low-stock alerts?
