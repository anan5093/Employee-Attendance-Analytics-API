# Code Review: Test Log

Part A of the assessment: review the starter code and list every defect found, with a failing input for each.

## Approach

- Each defect was confirmed by running the code, not just reading it.
- Expected behaviour comes from `openapi.yaml` (the contract) and `DATA_MODEL.md`, not from the code.
- `app/main.py` was not modified, so the evidence stays reproducible.
- Throwaway scripts live in `dev/` and test data in an Atlas dev database. Neither is part of the submission.
- Method for every test: write Expected from the contract, run it, record Got, then name the Problem.

---

## Setup checks

| # | Purpose | Action | Expected | Got | Finding |
|---|---|---|---|---|---|
| S1 | Check the code can reach the cloud database | `python dev/smoke_test.py` | `{'ok': 1}` and a version of 6.0+ | `KeyError: 'MONGO_URI'` | `.env` held `MONGODB_URI`, but the app only reads `MONGO_URI`. Renamed it and added `MONGO_DB`. |
| S2 | Re-check after the fix | `python dev/smoke_test.py` | Successful ping | `{'ok': 1}`, server 8.0.34 | Atlas, the database user and the network rule all work. |
| S3 | Load 6 employees and 9 records | `python sample_seed.py` | `loaded 6...`, `loaded 9...` | `localhost:27017` connection refused | The script ignored the Atlas URI and fell back to localhost, because a stray `.env` in `dev/` was the only one it found. Moved it to the repo root; the second run printed 6 and 9. |
| S4 | Confirm the data is in Atlas | One-line Python count | `attendance_dev 6 9` | `attendance_dev 6 9` | Database is ready. |
| S5 | Start the unmodified starter | `uvicorn app.main:app --port 8000 --reload` | `Application startup complete.` | Same | Starter runs. |

---

## Tests

### Group A: Real API calls (Tests 1-13)

| # | Purpose | Action | Expected (contract) | Got | Verdict |
|---|---|---|---|---|---|
| 1 | Check the app is alive | `GET /health` | `ok` only if the database answers | `{"status":"ok"}` | Passed the happy path. The real flaw appeared in Test 21. |
| 2 | Pagination starts at page 1 (rule R10) | `GET /employees?page=1&page_size=2` | EMP0001 and EMP0002 | EMP0003 and EMP0004 | **Defect:** page 1 skips the first page (`skip = page * page_size`). |
| 3 | Date format of instants | Inspected `created_at` from Test 2 | Integer in epoch ms | `"2026-02-01T04:30:00"` | **Defect:** dates leave the API as strings. |
| 4 | Shape of attendance records | `GET /attendance` | 10 fields on every record, no `id`, epoch-ms instants, same-date rows sorted by `emp_code` A to Z | Extra `id`; date strings; EMP0002 record missing `half_day` and `history`; order EMP0006, EMP0004, EMP0001 on one day | **Four defects:** extra field, wrong format, no defaults for legacy records, no sort tie-break. |
| 5 | Employee that doesn't exist | `POST /attendance/punch-in` with EMP9999 | 404 with a JSON message | 500 Internal Server Error; log: `'NoneType' object is not subscriptable` | **Defect:** the code never checks the employee exists. |
| 6 | Status that must be rejected | Punch-in with `status: "ABSENT"` | 422 (only PRESENT, WFH, ON_DUTY allowed) | 201, record created | **Defect:** any text is accepted as a status. |
| 7 | Late minutes (rule R2) | Inspected `late_minutes` from Test 6 | 471 (server time 17:21 IST vs 09:30 start) | 141 | **Defect:** UTC clock compared with an IST shift time. The gap is exactly 330 minutes, the IST offset. |
| 8 | Impossible page number | `GET /attendance?page=0` | 422 | 200 with an empty list | **Defect:** no validation on `page`. |
| 9 | Filter | `GET /employees?department=Sales` | 2 employees, total 2 | Empty list, total 6 | **Two defects:** wrong skip, and `total` ignores the filter. |
| 10 | Unit of `punched_at` | Punch-in with `1783312500` (seconds) | 422 (below 100000000000 looks like seconds) | 201 with date 1970-01-21 | **Defect:** the code divides by 1000 without checking the size, and keeps fractions of a second. |
| 11 | Input validation on employees | Create employee with `shift_start: "abc"`, `email: "x"`, `joined_on: "nope"` | 422 | 201, saved in the database | **Defect:** no validation. The first punch-in for that employee would crash. |
| 12 | Illogical date range | `date_from` after `date_to` | 422 | 200, empty list | **Defect:** no range check. |
| 13 | First attempt at the duplicate punch-in race | Five `curl` commands with `&` | Exactly one 201 and four 409 | One 201 and four 409 | **Inconclusive.** Each curl starts a few milliseconds apart, so the first finished before the rest began. It matched the contract by luck. |

### Group B: Helper functions called directly (Tests 14-20)

`compute_*` functions were called straight from Python, so one function's output couldn't be confused with another's.

| # | Purpose | Action | Expected (contract) | Got | Verdict |
|---|---|---|---|---|---|
| 14 | Late minutes with a UTC time | `compute_late_minutes(04:35, "09:30")` | 35 (04:35 UTC = 10:05 IST) | 0 | **Defect:** compares raw clock numbers, no IST conversion. |
| 15 | Grace-period boundary | `compute_late_minutes(09:40:01, "09:30")` | 10 (601 s is strictly over 10:00) | 0 | **Defect:** rounds down to whole minutes first, then checks whether it's more than 10. |
| 16 | A tie in work hours | `compute_work_hours` on 32418 s | 9.01 | 9.01 | No defect for this input. `round()` is right on some ties. |
| 17 | Overtime minimum (rule R3) | `compute_overtime(18:45, "18:30")` | 0 (15 min, below the 30 minimum) | 15 | **Defect:** the 30-minute minimum is missing. |
| 18 | Mixed time-zone types | Same call with a naive time | A number | `TypeError: can't subtract offset-naive and offset-aware datetimes` | **Defect:** the function mixes kinds of datetime and crashes. |
| 20 | A tie that `round()` gets wrong | `compute_work_hours` on 450 s and 54 s | 0.13 and 0.02 | 0.12 and 0.01 | **Defect:** Python's `round()` doesn't reliably round exact halves up (rule R4). |

### Group C: Race condition and health (Tests 19 and 21)

| # | Purpose | Action | Expected (contract) | Got | Verdict |
|---|---|---|---|---|---|
| 19 | Prove the race that Test 13 couldn't | `dev/race_test.py` with 20 threads released at the same instant | One 201, nineteen 409, 1 record | Two 201, eighteen 409, 2 records | **Defect proven:** the code checks first and then inserts, and the database has no unique index. Two requests both saw "no record" and both inserted. |
| 21 | `/health` behaviour with a broken database | Second server on port 8001 with `MONGO_URI=mongodb://localhost:1`, then `curl -i /health` | 503 | 200 OK | **Defect:** `/health` never touches the database. |

---

## Defects grouped by root cause

| Root cause | Tests | Fix |
|---|---|---|
| Wrong pagination math | 2, 9 | Skip `(page - 1) * page_size`, and count with the filter applied |
| No time-zone handling | 3, 4, 7, 10, 14, 18 | One convention: aware UTC inside the app, IST only where rules need it, epoch-ms at the API boundary |
| No input validation | 6, 8, 10, 11, 12 | Strict types and constraints on every request |
| Check-then-insert, no unique index | 13, 19 | Unique index on `(emp_code, date)`, with `DuplicateKeyError` mapped to 409 |
| Missing existence checks | 5 | Look up the employee first and return 404 |
| Rule formulas implemented wrongly | 15, 17, 20 | Rewrite per R2, R3 and R4, with integers and `Decimal` |
| Response shape ignores the contract | 3, 4 | Convert instants to epoch-ms, drop `id`, fill legacy defaults, sort with a tie-break |
| Health check doesn't check | 21 | Ping the database and return 503 on failure |