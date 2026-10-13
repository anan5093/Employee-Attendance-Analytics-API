"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=5000,)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo = timezone.utc)

def ensure_indexes():
    """Created at startup, safe to run again. Each index is explained in DECISIONS.md."""
    emps, logs = db.employees, db.attendance_logs
    emps.create_index("emp_code", unique=True)                         # one employee per code
    emps.create_index([("department", 1), ("emp_code", 1)])            # GET /employees?department=
    emps.create_index([("department", 1), ("joined_on", 1)])           # headcount per department
    emps.create_index("joined_on")                                     # headcount without a department
    logs.create_index([("emp_code", 1), ("date", 1)], unique=True)     # one record per employee per day
    logs.create_index([("date", -1), ("emp_code", 1)])                 # list sort + month range scans
    logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])  # list filtered by status
    logs.create_index([("emp_code", 1), ("punch_in", -1)])            # punch-out: latest punch-in


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:
        print(f"WARNING: could not create indexes: {exc}")

    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)        
# --------------------------------------------------------------------------- #
# Time and rules (my own version)
# --------------------------------------------------------------------------- #

def ms_to_dt(ms: int) -> datetime:
     """Epoch miliseconds -> aware UTC datetime, cut to whole seconds."""
     return datetime.fromtimestamp(ms // 1000, tz = timezone.utc)

def dt_to_ms(dt: datetime) -> int:
    """Aware datetime -> epoch miliseconds, whole seconds only."""
    return (dt - EPOCH) // timedelta(seconds=1)* 1000

def instant_or_now(punched_at):
    """The one door for request times: the given ms, or 'now', cut to whole second."""
    if punched_at is not None:
        return ms_to_dt(punched_at)
    return ms_to_dt(dt_to_ms(datetime.now(timezone.utc)))  

def parse_hhmm(text: str) -> time:
    """'09:30' -> a time of day (hour 9, minute 30)."""
    return time.fromisoformat(text)

def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: a shift is overnight when it ends at or before the time it starts."""
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)

def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calender date a punch-in belongs to."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        day = day - timedelta(days= 1)
    return day.isoformat() 

def shift_window(day: str, shift_start: str, shift_end: str):
    """The shift's start and end moments (IST) for the record date 'day' ."""
    d = date.fromisoformat(day)
    start = datetime.combine(d, parse_hhmm(shift_start), tzinfo = IST)
    end_day = d + timedelta(days=1) if is_overnight(shift_start, shift_end) else d
    end = datetime.combine(end_day, parse_hhmm(shift_end), tzinfo= IST)
    return start, end

def late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    """R2: more than 10:00 after shift start -> whole minutes since shift start, else 0. """
    start, _ = shift_window(attendance_date(punch_in, shift_start, shift_end), shift_start, shift_end)
    seconds = (punch_in - start) // timedelta(seconds=1)
    return seconds // 60 if seconds > 600 else 0 

def overtime_minutes(punch_out: datetime, record_date: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, only if 30 or more, else 0."""
    _, end = shift_window(record_date, shift_start, shift_end)
    seconds = (punch_out - end) // timedelta(seconds=1)
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0

def work_hours(total_seconds: int) -> float:
    """R4: seconds /3600, 2 decimals, half-up. """
    hours = Decimal(total_seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding= ROUND_HALF_UP))



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))

def is_half_day(hours: float) -> bool:
    """R5: half day when the ROUNDED work hours are below 4.50. """
    return hours < 4.5

def to_ms(value):
    """A stored  datetime (or None) -> epoch milliseconds (or None)."""
    return None if value is None else dt_to_ms(value)

def round_half_up(value, places: int) -> float:
    """R8: round half-up to `places` decimals (MongoDB's $round rounds ties to even, so we do it here)."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))

def attendance_to_api(doc: dict) -> dict:
    """A stored attendance document -> the exact shape the contract returns."""
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for field, change in entry["changes"].items():
            if field in ("punch_in", "punch_out"):
                change = {"from": to_ms(change["from"]), "to": to_ms(change["to"])} 
            changes[field] = change
        history.append({
            "at": dt_to_ms(entry["at"]),
            "by": entry["by"], "reason": entry["reason"], "changes": changes,})

    return{
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    } 

def employee_to_api(doc: dict) -> dict:
    """A stored employee document -> the shape the contract returns."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }              


# --------------------------------------------------------------------------- #
# 4. Small validation helpers (bad input -> 422)
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"


def check_date(value: Optional[str]) -> Optional[str]:
    """None stays None; otherwise the text must be a real YYYY-MM-DD date."""
    if value is None:
        return None
    try:
        if not DATE_RE.fullmatch(value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"invalid date: {value}")
    return value


def month_bounds(month: str):
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise HTTPException(422, "invalid month")
    last_day = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def working_days(month: str, joined_on: str) -> int:
    """R7: Monday-Friday days in the month, counted from joined_on if that is later."""
    first, last = month_bounds(month)
    start = max(first, joined_on)
    if start > last:
        return 0
    d0, d1 = date.fromisoformat(start), date.fromisoformat(last)
    return sum(
        1
        for i in range((d1 - d0).days + 1)
        if (d0 + timedelta(days=i)).weekday() < 5
    )


def check_trend_range(date_from: str, date_to: str):
    frm, to = check_date(date_from), check_date(date_to)
    days = (date.fromisoformat(to) - date.fromisoformat(frm)).days + 1
    if days < 1:
        raise HTTPException(422, "'to' must not be before 'from'")
    if days > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return frm, to, days

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
presenceStatus = Literal["PRESENT", "ABSENT","LEAVE", "WFH", "ON_DUTY"]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]

def _real_date(value: str) -> str:
    date.fromisoformat(value)
    return value

DateStr = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_real_date)]



class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: presenceStatus = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None

class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)    


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")

    except PyMongoError:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}    

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)   # total reflects the filter
    cursor = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    ts = instant_or_now(body.punched_at)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date(ts, emp["shift_start"], emp["shift_end"]),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)

    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_to_api(doc)     



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = instant_or_now(body.punched_at)

    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
        },
        sort=[("punch_in", -1)],
    )

    if rec is None:
        raise HTTPException(404, "no punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    seconds = (ts - rec["punch_in"]) // timedelta(seconds=1)

    if seconds <= 0 or seconds > 86400:
        raise HTTPException(
            422,
            "punch_at must be after punch_in and within 24 hours",
        )

    hours = work_hours(seconds)

    updates = {
        "punch_out": ts,
        "work_hours": hours,
        "overtime_minutes": overtime_minutes(
            ts,
            rec["date"],
            emp["shift_start"],
            emp["shift_end"],
        ),
        "half_day": is_half_day(hours),
    }

    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": updates},
    )

    if result.matched_count == 0:
        raise HTTPException(409, "already punched out")

    rec.update(updates)

    return attendance_to_api(rec)

    
def attendance_find_spec(emp_code, date_from, date_to, status, page, page_size):
    """The one place that builds the list query. The endpoint and the explain endpoint both use it."""
    date_from, date_to = check_date(date_from), check_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return {
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"])
    cursor = (
        db.attendance_logs.find(spec["filter"])
        .sort(list(spec["sort"].items()))
        .skip(spec["skip"])
        .limit(spec["limit"])
    )
    return {"items": [attendance_to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    check_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")
    ss, se = emp["shift_start"], emp["shift_end"]
 
    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status
 
    # 1. the state after the change
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE records cannot have punch times")
        new_in = new_out = None
        derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status needs a punch_in")
        if attendance_date(new_in, ss, se) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        derived = {
            "work_hours": None,
            "late_minutes": late_minutes(new_in, ss, se),
            "overtime_minutes": 0,
            "half_day": False,
        }
        if new_out is not None:
            seconds = (new_out - new_in) // timedelta(seconds=1)
            if seconds <= 0 or seconds > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            hours = work_hours(seconds)
            derived.update(
                work_hours=hours,
                overtime_minutes=overtime_minutes(new_out, date, ss, se),
                half_day=is_half_day(hours),
            )
 
    # 2. a request that changes nothing is rejected
    if new_status == old_status and new_in == old_in and new_out == old_out:
        raise HTTPException(422, "nothing to change")
 
    # 3. what actually changed (derived fields included)
    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    old_vals = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if old_vals[k] != v}
    entry = {"at": instant_or_now(None), "by": body.regularized_by, "reason": body.reason, "changes": changes}
 
    # 4. write only if nobody changed the record since we read it (optimistic concurrency)
    guard = {"_id": rec["_id"], "status": old_status, "punch_in": old_in, "punch_out": old_out}
    guard["history"] = {"$size": len(rec["history"])} if "history" in rec else {"$exists": False}
    result = db.attendance_logs.update_one(guard, {"$set": new_vals, "$push": {"history": entry}})
    if result.matched_count == 0:
        raise HTTPException(409, "record was changed by someone else, please retry")
 
    rec.update(new_vals)
    rec["history"] = list(rec.get("history") or []) + [entry]
    return attendance_to_api(rec)
 
 
# --------------------------------------------------------------------------- #
# 7. Analytics queries (built here, run by the endpoints and by the explain endpoint)
# --------------------------------------------------------------------------- #
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
IS_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
# $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Monday-Friday is 2..6 (R7)
IS_WEEKDAY = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
DAY_VALUE = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}      # half day counts 0.5 (R5)
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}                            # missing means 0
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}
PRESENT_WEEKDAY_VALUE = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, DAY_VALUE, 0]}
PRESENT_ANY_VALUE = {"$cond": [IS_PRESENT, DAY_VALUE, 0]}
 
 
def monthly_query(emp_code: str, month: str):
    first, last = month_bounds(month)
    return "attendance_logs", [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_WEEKDAY_VALUE},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE_MIN},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
 
 
def summary_query(month: str, department: Optional[str]):
    """Starts from `employees`, so employees with zero logs are still counted (R9)."""
    first, last = month_bounds(month)
    emp_match = {"joined_on": {"$lte": last}}
    if department is not None:
        emp_match["department"] = department
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    one_employee = [   # runs once per employee over that employee's logs of the month
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_WEEKDAY_VALUE},
            # hours kept as whole hundredths so the final average is exact (no float drift)
            "work_centi": {"$sum": {"$cond": [has_hours, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
            "work_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE_MIN},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    zero = {"present_days": 0, "work_centi": 0, "work_count": 0, "late_count": 0,
            "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}
    return "employees", [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": one_employee, "as": "logs"}},
        {"$set": {"p": {"$ifNull": [{"$first": "$logs"}, {"$literal": zero}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$p.present_days"},
            "work_centi": {"$sum": "$p.work_centi"},
            "work_count": {"$sum": "$p.work_count"},
            "late_count": {"$sum": "$p.late_count"},
            "total_late_minutes": {"$sum": "$p.total_late_minutes"},
            "leave_count": {"$sum": "$p.leave_count"},
            "on_duty_count": {"$sum": "$p.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]
 
 
def leaderboard_query(month: str, limit: int, department: Optional[str]):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                      # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})   # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},   # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return "attendance_logs", pipeline
 
 
def trend_query(department: str, frm: str, days: int):
    """One row per calendar day. The day series is generated inside MongoDB with $range, so every
    day exists even when there are no logs (gap filling by construction, no Python loop)."""
    start = datetime.combine(date.fromisoformat(frm), time(0, 0), tzinfo=timezone.utc)
    return "employees", [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        {"$project": {"_id": 0, "codes": 1, "joined": 1, "day": {"$map": {
            "input": {"$range": [0, days]}, "as": "i",
            "in": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}}}}}},
        {"$unwind": "$day"},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date", "codes": "$codes"},
                     "pipeline": [
                         {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$d"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                         {"$group": {"_id": None, "present_count": {"$sum": PRESENT_ANY_VALUE},
                                     "late_count": {"$sum": IS_LATE}}},
                     ], "as": "facts"}},
        {"$set": {"f": {"$ifNull": [{"$first": "$facts"}, {"$literal": {"present_count": 0, "late_count": 0}}]}}},
        {"$set": {
            "present_count": "$f.present_count",
            "late_count": "$f.late_count",
            # R9: employees whose joined_on is on or before this day
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None]}}},
        # mean of the non-null rates over this row and up to 6 rows before it, inside the range
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]
 
 
# --------------------------------------------------------------------------- #
# 7b. Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    _, pipeline = monthly_query(emp_code, month)
    row = next(iter(db.attendance_logs.aggregate(pipeline)), {})   # no records -> all zeros
    wd = working_days(month, emp["joined_on"])
    present = float(row.get("present_days", 0))
    pct = None if wd == 0 else round_half_up(Decimal(str(present)) / Decimal(wd) * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }
 
 
@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    _, pipeline = summary_query(month, department)
    items = []
    for row in db.employees.aggregate(pipeline):
        count = int(row["work_count"])
        avg = None
        if count > 0:   # average over RECORDS (sum of hours / number of records), not over employees
            avg = round_half_up(Decimal(int(round(row["work_centi"]))) / Decimal(100) / Decimal(count), 2)
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": float(row["present_days"]),
            "avg_work_hours": avg,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}
 
 
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    _, pipeline = leaderboard_query(month, limit, department)
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in db.attendance_logs.aggregate(pipeline)
    ]
    return {"month": month, "items": items}
 
 
@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    frm, _, days = check_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    _, pipeline = trend_query(department, frm, days)
    items = []
    for r in db.employees.aggregate(pipeline):
        rate, moving = r.get("attendance_rate"), r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": None if rate is None else round_half_up(rate, 4),
            "moving_avg_7d": None if moving is None else round_half_up(moving, 4),
        })
    return {"department": department, "items": items}
 
 
# --------------------------------------------------------------------------- #
# 8. Explain endpoint: the plan of the SAME query the real endpoint runs
# --------------------------------------------------------------------------- #
def _require(**params):
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")
 
 
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        spec = attendance_find_spec(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": spec["filter"], "sort": spec["sort"],
                   "skip": spec["skip"], "limit": spec["limit"]}
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            collection, pipeline = monthly_query(emp_code, month)
        elif endpoint == "department_summary":
            _require(month=month)
            collection, pipeline = summary_query(month, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            collection, pipeline = leaderboard_query(month, limit, department)
        else:
            _require(department=department, **{"from": from_}, to=to)
            frm, _, days = check_trend_range(from_, to)
            collection, pipeline = trend_query(department, frm, days)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}



# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
