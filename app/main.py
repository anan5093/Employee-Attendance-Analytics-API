"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import AfterValidator, BaseModel, Field, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=5000,)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo = timezone.utc)

def ensure_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)


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
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
presenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
HHMM = Annotated[str, Field(pattern=r"^([01]\d2[0-3]):[0-5]\d$")]

def _real_date(value: str) -> str:
    date.fromisofromat(value)
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
        raise HTTPException(404, "employess not found")
    ts = instant_or_now(body.punched_at)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort = [("Punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    seconds = (ts - rec["punch_in"]) // timedelta(seconds=1)
    if seconds <= 0 or seconds > 86400:
        raise HTTPException(422, "punch_at must be after punch_in and within 24 hours")
    hours = work_hours(seconds)
    updates = {
        "punch_out": ts,
        "work_hours": hours,
        "overtime_minutes": overtime_minutes(ts, rec["data"], emp["shift_start"], emp["shift_end"]),
        "half_day": is_half_day(hours),
    }
    result = db.attendance_logs.update_one({"_id": rec["_id"], "punch_out": None}, {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update(updates)
    return attendance_to_api(rec)
    

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
