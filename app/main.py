"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo = timezone.utc)

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# --------------------------------------------------------------------------- #
# Time and rules (my own version)
# --------------------------------------------------------------------------- #

def ms_to_dt(ms: int) -> datetime:
     """Epoch miliseconds -> aware UTC datetime, cut to whole seconds."""
     return datetime.fromtimestamp(ms // 1000, tz = timezone.utc)

def dt_to_ms(dt: datetime) -> int:
    """Aware datetime -> epoch miliseconds, whole seconds only."""
    return (dt - EPOCH) // timedelta(seconds=1)* 1000

def parse_hhmm(text: str) -> time:
    """'09:30' -> a time of day (hour 9, minute 30)."""
    return time.fromisoformat(text)

def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: a shift is overnight when it ends at or before the time it starts."""
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)

def attendence_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
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
    start, _ = shift_window(attendence_date(punch_in, shift_start, shift_end), shift_start, shift_end)
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


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
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
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = page * page_size
    total = db.employees.count_documents({})
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
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
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


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
