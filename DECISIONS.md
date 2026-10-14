1. Indexes

Here’s what I actually created: two unique indexes — one on `employees.emp_code`, the other on `attendance_logs (emp_code, date)`. These block duplicates right out of the gate. Everything else is there for a specific query:

- `(date desc, emp_code)` speeds up sorted lists and month-based searches.
- `(status, date desc, emp_code)` is for filtering by status.
- `(emp_code, punch_in desc)` finds the latest punch-in (for punch-out logic).
- On `employees`, both `(department, emp_code)` and `(department, joined_on)`, plus a plain `joined_on` index, cover listing and headcount queries.

I skipped adding a covering index for the leaderboard. That’d slow down every single write, and honestly, scans by date are already fast enough.

How this connects to the code:

| Index | Where it’s used |
|---|---|
| unique `emp_code` | When creating employees (kicks back 409 on duplicates), all employee lookups |
| unique `(emp_code, date)` | For `punch_in` (409), PATCH, analytics by month, joins in summaries |
| `(date -1, emp_code 1)` | Sorting and filtering in `GET /attendance`, also for leaderboard’s month range |
| `(status 1, date -1, emp_code 1)` | When filtering attendance by status |
| `(emp_code 1, punch_in -1)` | Find latest punch-in before punch-out |
| `(department, emp_code)` | When listing employees in a department (sorted) |
| `(department, joined_on)`, `joined_on` | For headcount in summaries and trends |

What I left out: a covering index on `(date, late_minutes, emp_code)` for leaderboard — not worth the write penalty.

2. Punch-in Race

So, imagine two punch-in requests hit at the exact same moment. Here’s what happens:

Both skip any pre-insert lookups and just try to insert. Thanks to the unique `(emp_code, date)` index, only one actually gets written. The “loser” triggers a `DuplicateKeyError`, and I return HTTP 409.

Step-by-step:

1. Both requests validate and confirm the employee exists.
2. Each builds its record and calls `insert_one` — no prior checks.
3. The DB enforces the unique constraint; only one wins.
4. The other blows up with `DuplicateKeyError`, and that triggers a 409 response.

Same idea for punch-out — I only update if the record’s `punch_out` is still empty. If not, the update doesn’t match, and I return 409.

And yes, I tested this: ran 20 threads, got one success, nineteen 409s, one record created.

3. Ties

If two employees tie on late minutes right at the leaderboard cutoff — say, your limit is 2 — both (or all) of them make it. I use Mongo’s `$rank`. So if you have scores like 90, 60, 60, 20, and `limit=2`, the ranks are 1, 2, 2, and 4, which means all ranked at 2 or better are included. That’s three rows.

Rows come back sorted by total (desc), then `emp_code` (asc). I don’t arbitrarily drop a tied person just to match a list length.

Example? The Ops department in a big dataset ended up with more than “limit” rows due to ties.

4. Headcount

To make sure employees with zero logs still count in department summaries, I always start from the `employees` collection — filtered by join date — and join logs afterward. So, if someone never clocked in, they get an empty logs array, but I use `$ifNull` to treat that as zero, and they’re still counted.

If I started from logs, people with no logs wouldn’t show up at all. Filtering with `joined_on <= last day of the month` keeps things clean — no early counts for late hires.

5. One change for 100x data

  If I had to scale everything up by 100 times, I’d build a rollup collection: store daily department totals, update it on every punch and PATCH, so analytics just grab a few rows instead of chewing through millions.

  What this affects:

- Every time you PATCH a log, you also fix up the rollup total — keeps things accurate.
- Other scale tweaks: switch to cursor-based pagination (skip gets slow when you go deep) and maybe add a covering index for whatever analytics query is the hottest.
- What breaks first at scale? Deep skips, big `$lookup` joins for trends — those start to drag pretty quickly.