# Factory Management System (bedsheet / textile printing)

Local-only Windows desktop app: Python 3, CustomTkinter, SQLite. No network, telemetry or cloud.

## Run (Windows)
    cd "D:\Python\Factory POS\factory_management"
    python -m venv .venv && .venv\Scripts\activate
    pip install -r requirements.txt
    python main.py            # first launch shows "Welcome - set up your factory"
    python -m pytest          # automated tests (temporary databases only)
    build_windows.bat         # optional: PyInstaller build (see limitations)

## Where data lives
In the **same folder as the software** (next to `main.py` or `FactoryManagement.exe`): `data\factory.db`, `backups`, `recovery`, `logs`, `exports`.
Override: env var `FMS_DATA_DIR`. If that folder is not writable (e.g. Program Files) the app falls back to `%APPDATA%\FactoryManagementSystem`.
An older database in `%APPDATA%\FactoryManagementSystem` is copied (never moved or deleted) into the program folder on first start.
**When rebuilding the .exe, keep the `data` folder** (`build_windows.bat` saves and restores it, but take a backup first).
The app never creates a second empty database by accident: first-run creation is an explicit screen.

## Roles
three roles - **owner** (everything), **accountant** (all the daily work: money, stock, production, people, salaries; but no Dashboard, Insights, Partners, Profit Distribution, Reports, Settings, Users, Audit Log or Backup), **auditor** (sees everything except Dashboard, Insights, Settings and Users; changes nothing). Anyone who still had the old inventory manager / production supervisor role is switched off on upgrade (nothing deleted) - create a new user for them. The sidebar hides pages a role can't use;
action buttons a role may not use are disabled. Owner-only: approve distributions, restore backups, create backups, manage users/settings,
change ownership/profit shares. (Finalising a job is open to the accountant too.)

## Accounting rules (summary)
Money = integer paisa, quantities = milli-units (no floats). Every posting is a balanced double entry; posted entries/stock
movements/job costs/audit rows are immutable (DB triggers) - corrections are reversals. Revenue is recognised on invoices; customer
advances are a liability; stock purchases are assets until consumed; capital/withdrawals/distributions are equity; machines are
assets; salary is expensed once at accrual (payments only reduce the liability); job electricity/labor figures are management
estimates and are NOT posted (the real bill/salary is what the ledger records - no double counting). No overtime pay; absence
deduction is off unless enabled in Settings; no tax/EOBI rules are assumed. Financial year default July-June.

## Backup / restore
Daily automatic backup (SQLite online-backup API, verified, retention applies to auto backups only), manual backups (optional second
folder, e.g. USB: it gets every manual AND automatic backup; the automatic one is re-checked every 30 minutes, so a USB plugged in later still gets today's copy; an unplugged USB never blocks the local copy), backup before migrations and before restore. Restore (owner): verify -> temp copy -> re-verify -> keep the
current DB in `recovery\` -> activate. A damaged database is never overwritten automatically (Recovery screen).
SQLite is NOT encrypted at rest - enable Windows BitLocker.

## Status (honest checklist)
| Area | Implemented | Tested |
|---|---|---|
| Paths, DB pragmas, transactions, idempotency, migrations | yes | yes (automated) |
| Users/roles, Argon2, lockout, audit log | yes | yes |
| Backup/verify/restore, daily auto backup | yes | yes (incl. backup during writes, corrupt restore keeps live DB) |
| Inventory: unit follows the material (cloth m, ink litres (L), paper rolls with the meters shown too); stock arrives as price per unit -> exact total; weighted-average cost; customer fabric separate; search / low-stock filter / Excel export; edit, delete (archive), undo receipt | yes | yes (hand-computed) |
| Production & Invoices (one page): entry -> several materials in one go (all-or-nothing) -> cost per piece -> invoice (invoice no. = entry no., priced per piece, customer's advance applied automatically) -> payment (paid / part / unpaid) -> invoice PDF. Edit / return stock / delete. Estimates, quotes, machine-run, other-cost entry and finalise exist in the service layer but have no screen | yes | yes |
| Customers: add / edit / delete (archive when there is history) / receive advance | yes | yes |
| Calendar date picker on every date field (no typing); dashboard custom range follows the calendars | yes | yes (UI) |
| Machines page (hidden from the menu for now; code and electricity service kept) | yes | yes (service) |
| Accounting, invoices, receipts, advances, payables, expenses, capex | yes | yes |
| Partners, capital, profit shares, distributions | yes | yes |
| Employees, attendance (overnight), monthly payroll, advances | yes | yes |
| Dashboard (all 17+ figures from DB, ranges, chart) | yes | yes (hand-computed scenario + UI) |
| 17 screens (sidebar pages) incl. forms, search/sort/pagination | yes | smoke + flow tests on a virtual display (Linux) |
| Reports: 11 reports -> Excel/CSV/PDF; invoice & salary-slip PDF | yes | yes |
| CSV/Excel IMPORT with preview | **not built** | - |
| Refunds / credit notes, bank accounts, multi-currency | **not built** | - |
| Optional database encryption (SQLCipher) | **not built** | - |
| Failure tests: disk full, permission denied, forced kill mid-transaction | **not written** | - |
| PyInstaller .exe | script provided | **never run on Windows** |
| UI on real Windows at 1366x768 / 1920x1080 | - | **not verified** (tested at 1440x900 on Linux) |

## Known limitations
- Screens were tested on Linux (virtual display). Fonts/spacing on Windows may differ slightly; report anything clipped.
- Scrollbars use the default light style (cosmetic).
- Idle auto-lock is not implemented. One user session at a time per database file.
- Salary type: monthly only. Tax, EOBI, overtime are intentionally not modelled.
- Supplier payments: "partly paid" purchases stay fully owed until payments are recorded individually.
- docs/screenshots contains images made from invented DEMO data, not real records.


## Forgotten password
Every user has a security question (chosen at setup or in Users > Add user). On the sign-in screen, **Forgot password?** asks the question and, on a right answer, sets a new password. Five wrong answers lock recovery for 15 minutes. The answer is stored only as an Argon2id hash. Owners can also use Users > Reset password. Databases made before this version have no question yet: the owner sets one in Users > Security question.

## Attendance & Salaries (one page)
- **Today**: every active employee with IN / OUT / Absent / Leave / Undo buttons. IN and OUT stamp the current time. A shift still IN after 10 hours is closed automatically at exactly IN + 10 h, flagged AUTO OUT and counted as 10 h (the Today board and Records show a warning; fixing the OUT time on Records removes it).
- **Cost Calculator** (Production menu): a stand-alone tool for the cost of one linear metre of printing. You type paper cost, ink price and use, print width, machine power, hours, production, working days, electricity rate and monthly labour; and press Calculate cost (or Enter); it shows the cost per metre, per day and per month, where the money goes (chart and bars), the working, and a table of what the cost would be at other ink uses. Nothing is read from or written to the books; the last figures typed are remembered in `cost_calculator.json` in the data folder ("Clear calculations" empties them).
- **Month & Salaries**: per employee days came, hours, absent/leave, estimated deduction (salary / days in the month per absent day; half day = 0.5; leave is paid unless set to unpaid under Deduction rules), and Accrue / Pay / Slip / Edit buttons. From the 1st of each month the previous month is accrued automatically (switch: Auto-accrue ON/OFF; Accrue all salaries does the same for the chosen month). Employees who joined or whose salary started mid-month are never prorated or guessed: you decide when to accrue them with their own Accrue button (full salary by default; under "more options" you can type a different basic with a reason). An unpaid payslip can be corrected with Edit (the old entry is reversed, both versions are kept).
- **Records** (add a past day, correct) and **Employees** (add, change salary, advance).


## Ink in litres, paper in rolls and meters
- Ink is counted, priced and shown in **litres (L)** (1 ml = 0.001 L, so quantities stay exact). Databases from before this change are converted by migration 0011: every ink figure is divided by 1000 and the stock value does not change. The migration refuses to run (and changes nothing) if an ink figure has a fraction of a millilitre.
- Paper is received in **rolls**; every paper material has "Meters in one roll" (default 300, set when the material is created, editable, and changeable when receiving stock). Inventory shows both, for example `6 rolls (1,800 m)`.
- **Use stock** (and the wastage / adjustment form) has a *Rolls | Meters* switch for paper. 1 roll takes 1 roll; 150 m of a 300 m roll takes 0.5 roll and costs half of a roll's average cost. The screen shows the conversion and the cost live, and warns when there is not enough stock.
- Limitation: stock is stored in thousandths of a roll (0.3 m on a 300 m roll), so a length that is not a multiple of that is rounded to the nearest thousandth of a roll (100 m is booked as 0.333 roll = 99.9 m). The cost is always worked out from the rolls actually booked.
- Changing the meters per roll of a material changes the meters shown for its existing stock (the rolls stay the same).

## Partners: two ways to share profit
- **Automatic (default):** every partner's profit share equals his ownership % (capital contributed less withdrawals). A withdrawal or a new contribution changes the shares by itself; the shares always total exactly 100% (largest remainder).
- **Manual:** "Manual %..." on the Partners page lets the owner type the shares (must total 100%, effective-dated, audited). "Automatic (= ownership %)" switches back at any time; typed shares are kept.
- Approved distributions snapshot the shares they were approved with; later changes never touch them.

## Profit Distribution page
- The approve dialog shows each partner's % and amount live. After approval the new distribution is selected and the box below lists every partner with share %, amount, paid and status, a **Pay** button per partner and **Pay all unpaid** (all or nobody; cash must cover it). The page keeps both tables visible down to the minimum window size.
- Fixed: the same profit could be distributed twice under two overlapping date ranges; any overlapping approved distribution now counts against the period's distributable profit.

## Assets, supplier advance, customer stock (Oct 2026)
- **Assets** (Expenses page, second tab): a plain list of machines/equipment with their price. It is not part of the books - no cash, profit, expense or report changes (table `asset_records`, migration 0012). Wrong records are voided, never deleted. Capital purchases booked before this list existed show in orange and can be voided (that posts a reversal).
- **Reversed expenses** stay on the list in red with "REVERSED"; a checkbox hides them.
- **Suppliers**: a minus "We owe" means the supplier holds our advance. *Receive advance back* brings cash in and moves his balance to zero (not income, profit unchanged).
- **Customers**: "Stock given" column and button show what each customer gave and how much is left.
- **Use stock**: fabric is listed twice - the customer's own (this customer only, no cost) and the factory's (average cost).
- **Attendance & Salaries**: lists redraw only rows that changed (no flicker, scroll/selection kept); opening a tab shows current data; the Today board picks up outside changes by itself.

## Expenses page (Oct 2026)
- Period buttons: Today / This week (Mon-Sun) / This month / All time, plus a calendar "from - to" range.
- "Total expenses" is shown at the bottom for the chosen period; reversed entries are shown in red and are not counted.
- "Print PDF" saves the listed period as a PDF (rows, total, totals by category).
- "Direct Labor" (5100) and "Administrative Salaries" (6100) are no longer expense categories: salaries are booked from Attendance & Salaries. Old entries stay in the books.

## Simpler forms (Oct 2026)
- Production: "Meters produced" (stored in the old pieces column); invoices are priced per meter; calculation shows cost per meter.
- Receive stock / Wastage: pick the kind first (Fabric, Ink, Paper, Other); only that kind's materials are listed. "Meters in one roll" shows for paper only; customer's own stock hides price, supplier and payment. Supplier invoice no., purchase reference, storage location and reason were removed from Receive stock.
- New/Edit material: unit only for "Other"; roll length only for paper.
- Suppliers: Position column removed. Customers: short "Stock left" list (full figures in "Stock given").
- Expenses: categories without account numbers; new "Other Expenses" (6200, migration 0013).
- Use stock: a customer's own ink / paper / other stock is listed (at zero cost) before the factory's, like fabric. "More options" was removed from Receive stock.

## Business Insights page (Oct 2026)
- New page "Insights" (under Dashboard). Compare this period with the one before: Month vs month, Quarter vs quarter, Year vs year or Pick dates (calendar, compared with the equally long period before). "Earlier / Later" step back and forward.
- Shows: verdict (profit / loss), sales, expenses, net profit and margin with change %; bar and month-by-month charts; donut "where your sales go"; assets payback (won back %, profit per month, estimated time); expenses by kind with "Exceeding" (10% or more up) flags; electricity compared; stock used by material and stock costs (materials, wastage, adjustments).
- Engine: services/insights_service.py (all numbers from the books and stock records; sentences are built from those numbers). Tests: tests/test_insights.py, tests/test_insights_page.py.
- Payback: assets = non-voided entries of the Assets list. Won back = net profit since the first asset's date (capped at the assets total). Speed = net profit per day of the chosen period x 30.44 days.
- Expenses page: Assets tab has a Total at the bottom; account 6000 is now named "Chai Paani Refreshment" (migration 0014).

## Keyboard (Oct 2026)
- In every dialog Enter presses its main button (Save / Receive stock / Create invoice / Close); Esc closes it. In a multi-line box Enter still starts a new line. The cursor starts in the first box. Delete-type confirmations need a click on purpose.
- Every button can be reached with Tab (a thin ring shows where you are) and pressed with Enter or Space.


## Dashboard and Insights wording (Oct 2026)
- Dashboard boxes: Total expenses (only the entries on the Expenses page for the same dates; production cost has its own box), Customer has to pay you, You have to pay suppliers, Unpaid salaries, Owner's profit share payable.
- Insights: chart text and figures are bold; the inventory section and the machine-hours tiles are hidden for now (the figures are still calculated in `services/insights_service.py`).

## Complete Business Summary (Reports page)
The **Complete Business Summary** button on the Reports page makes one PDF of about 8 pages for a month, a financial year or any dates you choose (it needs the "export reports" permission). It only reads data.
- Page 1 key numbers and the picture in words; 2 sales and profit (this period vs the one before, six-month trend); 3 expenses (the Expenses page only); 4 salaries; 5 inventory (value and quantity at the start, received, used, lost, at the end, rebuilt from stock movements so a past month shows the stock as it was); 6 production entries, invoices and customers; 7 money position, suppliers, partners, assets and payback; 8 notes and data checks.
- Code: `reports/business_summary.py` (data + charts + PDF). Tests: `tests/test_business_summary.py`.

## Employee exit, locked cost per meter

- **Resign / Remove** (Attendance & Salaries > Employees): records the last working day and whether the person resigned or was removed. The employee leaves the Today board and no later month is accrued; attendance, payslips and advances stay on file. The last month can still be accrued and paid by hand. **Bring back** undoes it (reason required). No advance can be given to someone who has left.
- **Lock cost per meter** (Cost Calculator): type the figure you decide on (e.g. 170 when the calculation says 167.97) and press Lock. Create invoice shows it as the cost per meter for every entry, with the stock-based figure underneath, and the profit preview uses it. It is only a displayed figure: nothing is posted to the books. Clear calculations does not remove it; Unlock does.

## Registration approval (e-mailed code)
Registering a NEW factory on a computer needs a 6-digit code that is e-mailed to the software owner, who gives it to the person (by phone).
Needs the internet for that one step only; the program itself stays offline. Sent: the name typed on the screen, the code, and an anonymous computer code.
A factory that already exists is never asked again (so copying a finished data folder to another PC is not blocked by this).

One-time setup (owner):
1. Go to script.google.com > New project, paste `docs/registration_mail.gs`, change `SECRET` to a long random text.
2. Deploy > New deployment > type Web app > Execute as **Me**, Who has access **Anyone**. Allow the mail permission when asked. Copy the Web app URL.
3. Create `services/registration_local.py` containing `REGISTER_URL = "..."` (the URL) and `REGISTER_SECRET = "..."` (same text as SECRET), then rebuild the .exe. This file is in `.gitignore`, so your secret is never pushed to GitHub, but it is packed into the .exe.
A built .exe without `REGISTER_URL` refuses to register anyone (on purpose). If the URL is ever misused, deploy a new version with a new SECRET.

