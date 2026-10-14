# Employee Attendance & Analytics API

A FastAPI + MongoDB service that records employee punch-in and punch-out, keeps an audit trail of manual
corrections, and serves attendance analytics. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb+srv://..."   # or set MONGO_URI and MONGO_DB in a local .env
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Needs MongoDB 6.0 or newer. Indexes are created at startup (safe to repeat). `GET /health` returns 503 when MongoDB does not answer.

## Endpoints
`/health` · `POST|GET /employees` · `POST /attendance/punch-in` · `POST /attendance/punch-out` · `GET /attendance` ·
`PATCH /attendance/{emp_code}/{date}` · `GET /analytics/employees/{emp_code}/monthly` ·
`GET /analytics/departments/summary` · `GET /analytics/leaderboard/late` ·
`GET /analytics/departments/{department}/trend` · `GET /admin/explain/{endpoint}`

## How it works
- Every instant enters through `ms_to_dt` (UTC, whole seconds) and leaves as epoch milliseconds.
- Rules R1-R5 (IST attendance date, late, overtime, work hours with half-up rounding, half day) are small pure
  functions in the "Time and rule functions" section.
- Races are settled by MongoDB: unique indexes for employee creation and punch-in, a conditional update for
  punch-out, and a guarded update for corrections.

## Assumptions where the contract is silent
- The trend range may be at most 92 days, counted inclusively.
- `moving_avg_7d` averages the unrounded daily rates and rounds once at the end.
- Records dated before `joined_on` are not filtered out.
- The trend day series is built inside MongoDB with `$range` rather than `$densify`.

## Known limitations
- If MongoDB is down at startup, index creation is skipped until the next restart.
- A `PATCH` that loses a concurrent edit returns 409 and must be retried.