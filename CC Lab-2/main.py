import os
import time
from threading import Lock

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from database import get_db
from checkout import checkout_logic

app = FastAPI()
SRN = "PES2UG23CS167"
templates = Jinja2Templates(directory="templates")


# Small in-process cache for read-heavy endpoints like /events.
# This dramatically improves Locust results when the same page is hit repeatedly.
_EVENTS_CACHE_TTL_SECONDS = float(os.getenv("EVENTS_CACHE_TTL_SECONDS", "2.0"))
_events_cache_lock = Lock()
_events_cache_ts: float = 0.0
_events_cache_data: list[tuple] | None = None

_MY_EVENTS_CACHE_TTL_SECONDS = float(os.getenv("MY_EVENTS_CACHE_TTL_SECONDS", "0"))
_my_events_cache_lock = Lock()
_my_events_cache: dict[str, tuple[float, list[tuple]]] = {}


def _get_events_cached(db) -> list[tuple]:
    global _events_cache_ts, _events_cache_data

    if _EVENTS_CACHE_TTL_SECONDS <= 0:
        rows = db.execute("SELECT id, name, fee FROM events").fetchall()
        return [tuple(r) for r in rows]

    now = time.monotonic()
    with _events_cache_lock:
        if _events_cache_data is not None and (now - _events_cache_ts) < _EVENTS_CACHE_TTL_SECONDS:
            return _events_cache_data

    rows = db.execute("SELECT id, name, fee FROM events").fetchall()
    data = [tuple(r) for r in rows]

    with _events_cache_lock:
        _events_cache_data = data
        _events_cache_ts = now

    return data


def _get_my_events_cached(db, username: str) -> list[tuple]:
    if _MY_EVENTS_CACHE_TTL_SECONDS <= 0:
        rows = db.execute(
            """
            SELECT events.name, events.fee
            FROM events
            JOIN registrations ON events.id = registrations.event_id
            WHERE registrations.username=?
            """,
            (username,),
        ).fetchall()
        return [tuple(r) for r in rows]

    now = time.monotonic()
    with _my_events_cache_lock:
        cached = _my_events_cache.get(username)
        if cached is not None:
            ts, data = cached
            if (now - ts) < _MY_EVENTS_CACHE_TTL_SECONDS:
                return data

    rows = db.execute(
        """
        SELECT events.name, events.fee
        FROM events
        JOIN registrations ON events.id = registrations.event_id
        WHERE registrations.username=?
        """,
        (username,),
    ).fetchall()
    data = [tuple(r) for r in rows]

    with _my_events_cache_lock:
        _my_events_cache[username] = (now, data)

    return data


def _invalidate_my_events_cache(username: str) -> None:
    with _my_events_cache_lock:
        _my_events_cache.pop(username, None)


@app.on_event("startup")
def startup():
    db = get_db()
    try:
        db.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, fee INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS registrations (username TEXT, event_id INTEGER)")

        # Helpful indexes for /my-events and registrations.
        db.execute("CREATE INDEX IF NOT EXISTS idx_registrations_username ON registrations(username)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_registrations_event_id ON registrations(event_id)")
        db.commit()
    finally:
        db.close()


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register")
def register(username: str = Form(...), password: str = Form(...)):
    db = get_db()
    try:
        try:
            db.execute("INSERT INTO users VALUES (?,?)", (username, password))
            db.commit()
        except:
            return HTMLResponse("Username already exists. Try a different one.")
    finally:
        db.close()
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    db = get_db()
    try:
        user = db.execute(
            "SELECT * FROM users WHERE username=? AND password=?",
            (username, password)
        ).fetchone()
    finally:
        db.close()

    if not user:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "❌ Invalid username or password", "user": ""}
        )

    return RedirectResponse(f"/events?user={username}", status_code=302)



@app.get("/events", response_class=HTMLResponse)
def events(request: Request, user: str):
    db = get_db()
    try:
        rows = _get_events_cached(db)
    finally:
        db.close()

    # Optional demo: simulate CPU work if needed (disabled by default).
    if os.getenv("SIMULATE_CPU_LOAD", "0") == "1":
        waste = 0
        for i in range(3000000):
            waste += i % 3

    return templates.TemplateResponse(
        "events.html",
        {"request": request, "events": rows, "user": user}
    )


@app.get("/register_event/{event_id}")
def register_event(event_id: int, user: str):
    if event_id == 404:
        1 / 0

    db = get_db()
    try:
        db.execute("INSERT INTO registrations VALUES (?,?)", (user, event_id))
        db.commit()
    finally:
        db.close()

    # New registration changes the /my-events result for this user.
    _invalidate_my_events_cache(user)

    return RedirectResponse(f"/my-events?user={user}", status_code=302)


@app.get("/my-events", response_class=HTMLResponse)
def my_events(request: Request, user: str):
    db = get_db()
    try:
        rows = _get_my_events_cached(db, user)
    finally:
        db.close()

    # Optional demo: simulate CPU work if needed (disabled by default).
    if os.getenv("SIMULATE_CPU_LOAD", "0") == "1":
        dummy = 0
        for _ in range(1500000):
            dummy += 1

    return templates.TemplateResponse(
        "my_events.html",
        {"request": request, "events": rows, "user": user}
    )


@app.get("/checkout", response_class=HTMLResponse)
def checkout(request: Request):
    total = checkout_logic()
    return templates.TemplateResponse(
        "checkout.html",
        {"request": request, "total": total, "user": ""}
    )
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # Try to keep user on UI even when it crashes
    user = request.query_params.get("user", "")

    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "status_code": 500,
            "detail": str(exc),
            "user": user
        },
        status_code=500
    )