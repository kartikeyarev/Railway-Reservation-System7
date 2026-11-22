"""Railway Reservation System - Flask + SQLite.

Implements the report's design: role-based auth, train search, booking with
unique 10-digit PNR, seat/berth allocation, waiting list, rule-based refunds,
and transactional seat locking (BEGIN IMMEDIATE) to prevent double booking.
(For MySQL, replace BEGIN IMMEDIATE with SELECT ... FOR UPDATE row locks.)
"""
import os
import random
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["DATABASE"] = os.environ.get("RAIL_DB", os.path.join(BASE_DIR, "railway.db"))

CLASS_MULTIPLIER = {"SL": 1.0, "3A": 2.0, "2A": 3.0}
MAX_PASSENGERS = 6
MAX_WAITLIST = 10          # max passengers allowed on the waiting list per class
PAYMENT_MODES = ["UPI", "Card", "NetBanking", "FAIL (simulate failed payment)"]
BERTHS = ["Lower", "Middle", "Upper", "Lower", "Middle", "Upper", "Side Lower", "Side Upper"]


class RailwayError(Exception):
    """Business-rule failure that is safe to show to the user."""


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        # isolation_level=None -> we control transactions explicitly
        db = sqlite3.connect(app.config["DATABASE"], isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone():
            init_db(db)
        g.db = db
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(db):
    with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
        db.executescript(f.read())
    seed(db)


def seed(db):
    admin_pw = os.environ.get("ADMIN_PASSWORD", "Admin@123")
    db.execute(
        "INSERT INTO users(name,email,password_hash,phone,role) VALUES (?,?,?,?,?)",
        ("Administrator", "admin@railway.com", generate_password_hash(admin_pw), "0000000000", "admin"),
    )
    trains = [
        ("12417", "Prayagraj Express", "Prayagraj", "New Delhi", 30, "22:30", "06:15", 620),
        ("12418", "Prayagraj Express (Return)", "New Delhi", "Prayagraj", 30, "21:00", "05:30", 620),
        ("12559", "Shiv Ganga Express", "Varanasi", "New Delhi", 30, "18:45", "07:50", 540),
        ("12301", "Howrah Rajdhani", "Howrah", "New Delhi", 30, "16:55", "10:05", 1450),
        ("12951", "Mumbai Rajdhani", "Mumbai Central", "New Delhi", 30, "17:00", "08:30", 1580),
        ("12002", "Bhopal Shatabdi", "New Delhi", "Bhopal", 30, "06:00", "14:25", 980),
    ]
    today = date.today()
    for no, name, src, dst, seats, dep, arr, fare in trains:
        db.execute("INSERT INTO trains VALUES (?,?,?,?,?)", (no, name, src, dst, seats))
        for d in range(1, 31):
            db.execute(
                "INSERT INTO schedules(train_no,journey_date,departure,arrival,base_fare) VALUES (?,?,?,?,?)",
                (no, (today + timedelta(days=d)).isoformat(), dep, arr, fare),
            )


@app.cli.command("init-db")
def init_db_command():
    """Delete and re-create the database with sample data."""
    path = app.config["DATABASE"]
    if os.path.exists(path):
        os.remove(path)
    with app.app_context():
        get_db()
    print("Database initialised at", path)


# --------------------------------------------------------------------------
# Business logic (kept free of Flask so it is easy to unit test)
# --------------------------------------------------------------------------
def departure_dt(row):
    return datetime.strptime(f"{row['journey_date']} {row['departure']}", "%Y-%m-%d %H:%M")


def refund_percent(hours_before):
    """Refund rule from the report: >48h -> 75%, 12-48h -> 50%, <12h -> 0%."""
    if hours_before > 48:
        return 75
    if hours_before >= 12:
        return 50
    return 0


def fare_for(base_fare, travel_class):
    return round(base_fare * CLASS_MULTIPLIER[travel_class], 2)


def berth_for(seat_no):
    return BERTHS[(seat_no - 1) % len(BERTHS)]


def process_payment(mode, _amount):
    """Simulated gateway: any mode starting with FAIL is declined."""
    return not mode.upper().startswith("FAIL")


def generate_pnr(db):
    while True:
        pnr = str(random.randint(10**9, 10**10 - 1))
        if not db.execute("SELECT 1 FROM bookings WHERE pnr=?", (pnr,)).fetchone():
            return pnr


def _seat_counts(db, schedule_id, travel_class, status):
    return db.execute(
        "SELECT COUNT(*) FROM passengers p JOIN bookings b ON b.pnr=p.pnr "
        "WHERE b.schedule_id=? AND b.travel_class=? AND b.status=?",
        (schedule_id, travel_class, status),
    ).fetchone()[0]


def available_seats(db, schedule_id, travel_class):
    total = db.execute(
        "SELECT t.total_seats FROM schedules s JOIN trains t ON t.train_no=s.train_no "
        "WHERE s.schedule_id=?", (schedule_id,),
    ).fetchone()[0]
    return total - _seat_counts(db, schedule_id, travel_class, "CONFIRMED")


def waiting_passengers(db, schedule_id, travel_class):
    return _seat_counts(db, schedule_id, travel_class, "WAITING")


def waitlist_position(db, pnr):
    return db.execute(
        "SELECT COUNT(*) FROM bookings WHERE status='WAITING' AND "
        "(schedule_id, travel_class) = (SELECT schedule_id, travel_class FROM bookings WHERE pnr=?) "
        "AND rowid <= (SELECT rowid FROM bookings WHERE pnr=?)", (pnr, pnr),
    ).fetchone()[0]


def allocate_seats(db, schedule_id, travel_class, pnr):
    """Assign the lowest free seat numbers (and berths) to a booking's passengers."""
    total = db.execute(
        "SELECT t.total_seats FROM schedules s JOIN trains t ON t.train_no=s.train_no "
        "WHERE s.schedule_id=?", (schedule_id,),
    ).fetchone()[0]
    taken = {r[0] for r in db.execute(
        "SELECT p.seat_no FROM passengers p JOIN bookings b ON b.pnr=p.pnr "
        "WHERE b.schedule_id=? AND b.travel_class=? AND b.status='CONFIRMED' "
        "AND p.seat_no IS NOT NULL", (schedule_id, travel_class))}
    free = (n for n in range(1, total + 1) if n not in taken)
    for p in db.execute("SELECT passenger_id FROM passengers WHERE pnr=? ORDER BY passenger_id", (pnr,)).fetchall():
        seat = next(free)
        db.execute("UPDATE passengers SET seat_no=?, berth=? WHERE passenger_id=?",
                   (seat, berth_for(seat), p["passenger_id"]))


def promote_waitlist(db, schedule_id, travel_class):
    """After seats are freed, confirm waiting bookings in booking order."""
    waiting = db.execute(
        "SELECT pnr FROM bookings WHERE schedule_id=? AND travel_class=? AND status='WAITING' "
        "ORDER BY rowid", (schedule_id, travel_class)).fetchall()
    for w in waiting:
        n = db.execute("SELECT COUNT(*) FROM passengers WHERE pnr=?", (w["pnr"],)).fetchone()[0]
        if n <= available_seats(db, schedule_id, travel_class):
            db.execute("UPDATE bookings SET status='CONFIRMED' WHERE pnr=?", (w["pnr"],))
            allocate_seats(db, schedule_id, travel_class, w["pnr"])


def _clean_passengers(passengers):
    cleaned = []
    for p in passengers:
        name = (p.get("name") or "").strip()
        gender = (p.get("gender") or "").strip().upper()
        try:
            age = int(p.get("age"))
        except (TypeError, ValueError):
            raise RailwayError("Passenger age must be a number.")
        if not name:
            raise RailwayError("Passenger name is required.")
        if not 1 <= age <= 120:
            raise RailwayError("Passenger age must be between 1 and 120.")
        if gender not in ("M", "F", "O"):
            raise RailwayError("Passenger gender must be M, F or O.")
        cleaned.append({"name": name, "age": age, "gender": gender})
    return cleaned


def create_booking(db, user_id, schedule_id, travel_class, passengers, pay_mode, now=None):
    """Book tickets. Follows the control-flow graph in the report:
    valid train/date -> passengers <= 6 -> seats available? (else waiting list)
    -> payment successful? (else abort, seats released) -> confirm + PNR.
    Returns (pnr, status)."""
    now = now or datetime.now()
    if travel_class not in CLASS_MULTIPLIER:
        raise RailwayError("Invalid travel class.")
    if not 1 <= len(passengers) <= MAX_PASSENGERS:
        raise RailwayError(f"You can book 1 to {MAX_PASSENGERS} passengers per PNR.")
    passengers = _clean_passengers(passengers)

    db.execute("BEGIN IMMEDIATE")          # write lock: serialises concurrent bookings
    try:
        sch = db.execute(
            "SELECT s.*, t.train_name FROM schedules s JOIN trains t ON t.train_no=s.train_no "
            "WHERE s.schedule_id=?", (schedule_id,)).fetchone()
        if not sch or departure_dt(sch) <= now:
            raise RailwayError("Train or journey date is not valid for booking.")

        n = len(passengers)
        fare = fare_for(sch["base_fare"], travel_class) * n
        if available_seats(db, schedule_id, travel_class) >= n:
            status = "CONFIRMED"
        else:
            if waiting_passengers(db, schedule_id, travel_class) + n > MAX_WAITLIST:
                raise RailwayError("Class is full and the waiting list is closed.")
            status = "WAITING"

        if not process_payment(pay_mode, fare):
            raise RailwayError("Payment failed. No seats were held; please retry.")

        pnr = generate_pnr(db)
        db.execute("INSERT INTO bookings VALUES (?,?,?,?,?,?,?)",
                   (pnr, user_id, schedule_id, travel_class, status, fare, now.isoformat(timespec="seconds")))
        for p in passengers:
            db.execute("INSERT INTO passengers(pnr,name,age,gender) VALUES (?,?,?,?)",
                       (pnr, p["name"], p["age"], p["gender"]))
        db.execute("INSERT INTO payments(pnr,amount,mode,status,paid_on) VALUES (?,?,?,?,?)",
                   (pnr, fare, pay_mode.split(" ")[0], "PAID", now.isoformat(timespec="seconds")))
        if status == "CONFIRMED":
            allocate_seats(db, schedule_id, travel_class, pnr)
        db.execute("COMMIT")
        return pnr, status
    except BaseException:
        db.execute("ROLLBACK")             # releases any held seats
        raise


def cancel_booking(db, pnr, user_id=None, now=None):
    """Cancel a booking, apply refund rule, release seats, promote waiting list.
    Returns (refund_amount, refund_percent)."""
    now = now or datetime.now()
    db.execute("BEGIN IMMEDIATE")
    try:
        b = db.execute(
            "SELECT b.*, s.journey_date, s.departure FROM bookings b "
            "JOIN schedules s ON s.schedule_id=b.schedule_id WHERE b.pnr=?", (pnr,)).fetchone()
        if not b or (user_id is not None and b["user_id"] != user_id):
            raise RailwayError("Booking not found.")
        if b["status"] == "CANCELLED":
            raise RailwayError("This booking is already cancelled.")
        hours = (departure_dt(b) - now).total_seconds() / 3600
        if hours <= 0:
            raise RailwayError("The train has already departed; cancellation is not possible.")

        pct = 100 if b["status"] == "WAITING" else refund_percent(hours)  # unconfirmed tickets refund fully
        refund = round(b["total_fare"] * pct / 100, 2)
        was_confirmed = b["status"] == "CONFIRMED"
        db.execute("UPDATE bookings SET status='CANCELLED' WHERE pnr=?", (pnr,))
        db.execute("UPDATE payments SET status=?, refund_amount=? WHERE pnr=?",
                   ("REFUNDED" if refund > 0 else "FORFEITED", refund, pnr))
        if was_confirmed:
            promote_waitlist(db, b["schedule_id"], b["travel_class"])
        db.execute("COMMIT")
        return refund, pct
    except BaseException:
        db.execute("ROLLBACK")
        raise


def search_trains(db, source, destination, journey_date, travel_class=""):
    sql = ("SELECT s.*, t.train_name, t.source, t.destination FROM schedules s "
           "JOIN trains t ON t.train_no=s.train_no WHERE t.source LIKE ? AND t.destination LIKE ?")
    args = [f"%{source}%", f"%{destination}%"]
    if journey_date:
        sql += " AND s.journey_date=?"
        args.append(journey_date)
    sql += " ORDER BY s.journey_date, s.departure LIMIT 60"
    now = datetime.now()
    results = []
    for r in db.execute(sql, args).fetchall():
        if departure_dt(r) <= now:
            continue
        row = dict(r)
        classes = [travel_class] if travel_class in CLASS_MULTIPLIER else list(CLASS_MULTIPLIER)
        row["classes"] = [{
            "cls": c,
            "avail": available_seats(db, r["schedule_id"], c),
            "fare": fare_for(r["base_fare"], c),
        } for c in classes]
        results.append(row)
    return results


# --------------------------------------------------------------------------
# Auth decorators
# --------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*a, **kw):
        if session.get("role") != "admin":
            abort(403)
        return view(*a, **kw)
    return wrapped


# --------------------------------------------------------------------------
# Routes: public + auth
# --------------------------------------------------------------------------
@app.route("/")
def index():
    db = get_db()
    q = {k: request.args.get(k, "").strip() for k in ("source", "destination", "date", "cls")}
    results = None
    if any(q.values()):
        results = search_trains(db, q["source"], q["destination"], q["date"], q["cls"])
    cities = sorted({r[0] for r in db.execute(
        "SELECT source FROM trains UNION SELECT destination FROM trains")})
    return render_template("index.html", q=q, results=results, cities=cities,
                           classes=list(CLASS_MULTIPLIER), today=date.today().isoformat())


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        name, email, pw = f.get("name", "").strip(), f.get("email", "").strip().lower(), f.get("password", "")
        if not name or "@" not in email or len(pw) < 6:
            flash("Enter a name, a valid email and a password of at least 6 characters.", "error")
        else:
            try:
                get_db().execute(
                    "INSERT INTO users(name,email,password_hash,phone) VALUES (?,?,?,?)",
                    (name, email, generate_password_hash(pw), f.get("phone", "").strip()))
                flash("Registration successful. Please log in.", "ok")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("That email is already registered.", "error")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session.update(user_id=user["user_id"], user_name=user["name"], role=user["role"])
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("admin") if user["role"] == "admin" else url_for("index"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# --------------------------------------------------------------------------
# Routes: booking, PNR, cancellation
# --------------------------------------------------------------------------
@app.route("/book/<int:schedule_id>", methods=["GET", "POST"])
@login_required
def book(schedule_id):
    db = get_db()
    travel_class = request.values.get("cls", "SL")
    sch = db.execute("SELECT s.*, t.train_name, t.source, t.destination FROM schedules s "
                     "JOIN trains t ON t.train_no=s.train_no WHERE s.schedule_id=?", (schedule_id,)).fetchone()
    if not sch or travel_class not in CLASS_MULTIPLIER:
        abort(404)
    if request.method == "POST":
        f = request.form
        rows = zip(f.getlist("name"), f.getlist("age"), f.getlist("gender"))
        passengers = [{"name": n, "age": a, "gender": g_} for n, a, g_ in rows if n.strip() or a.strip()]
        try:
            pnr, status = create_booking(db, session["user_id"], schedule_id, travel_class,
                                         passengers, f.get("payment_mode", "UPI"))
            flash("Booking confirmed!" if status == "CONFIRMED" else
                  "Class is full: you are on the waiting list.", "ok")
            return redirect(url_for("ticket", pnr=pnr))
        except RailwayError as e:
            flash(str(e), "error")
    return render_template("book.html", sch=sch, cls=travel_class, fare=fare_for(sch["base_fare"], travel_class),
                           avail=available_seats(db, schedule_id, travel_class),
                           modes=PAYMENT_MODES, max_pax=MAX_PASSENGERS)


def _load_booking(pnr):
    db = get_db()
    b = db.execute(
        "SELECT b.*, s.journey_date, s.departure, s.arrival, t.train_no, t.train_name, t.source, t.destination, "
        "u.name AS booked_by FROM bookings b JOIN schedules s ON s.schedule_id=b.schedule_id "
        "JOIN trains t ON t.train_no=s.train_no JOIN users u ON u.user_id=b.user_id WHERE b.pnr=?", (pnr,)).fetchone()
    return db, b


@app.route("/ticket/<pnr>")
@login_required
def ticket(pnr):
    db, b = _load_booking(pnr)
    if not b or (b["user_id"] != session["user_id"] and session.get("role") != "admin"):
        abort(404)
    pax = db.execute("SELECT * FROM passengers WHERE pnr=? ORDER BY passenger_id", (pnr,)).fetchall()
    pay = db.execute("SELECT * FROM payments WHERE pnr=?", (pnr,)).fetchone()
    hours = (departure_dt(b) - datetime.now()).total_seconds() / 3600
    can_cancel = b["status"] != "CANCELLED" and hours > 0
    preview = 100 if b["status"] == "WAITING" else refund_percent(hours)
    wl = waitlist_position(db, pnr) if b["status"] == "WAITING" else None
    return render_template("ticket.html", b=b, pax=pax, pay=pay, can_cancel=can_cancel,
                           preview=preview, wl=wl)


@app.route("/cancel/<pnr>", methods=["POST"])
@login_required
def cancel(pnr):
    owner = None if session.get("role") == "admin" else session["user_id"]
    try:
        refund, pct = cancel_booking(get_db(), pnr, owner)
        flash(f"Booking cancelled. Refund: Rs {refund:.2f} ({pct}%).", "ok")
    except RailwayError as e:
        flash(str(e), "error")
    return redirect(url_for("ticket", pnr=pnr))


@app.route("/bookings")
@login_required
def bookings():
    rows = get_db().execute(
        "SELECT b.*, s.journey_date, s.departure, t.train_no, t.train_name, t.source, t.destination "
        "FROM bookings b JOIN schedules s ON s.schedule_id=b.schedule_id "
        "JOIN trains t ON t.train_no=s.train_no WHERE b.user_id=? ORDER BY b.rowid DESC",
        (session["user_id"],)).fetchall()
    return render_template("bookings.html", rows=rows)


@app.route("/pnr", methods=["GET", "POST"])
def pnr_status():
    b = pax = wl = None
    pnr = request.values.get("pnr", "").strip()
    if pnr:
        db, b = _load_booking(pnr)
        if b:
            pax = db.execute("SELECT * FROM passengers WHERE pnr=? ORDER BY passenger_id", (pnr,)).fetchall()
            wl = waitlist_position(db, pnr) if b["status"] == "WAITING" else None
        else:
            flash("No booking found for that PNR.", "error")
    return render_template("pnr.html", b=b, pax=pax, wl=wl, pnr=pnr)


# --------------------------------------------------------------------------
# Routes: admin
# --------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    report = {
        "by_status": db.execute("SELECT status, COUNT(*) c FROM bookings GROUP BY status").fetchall(),
        "revenue": db.execute("SELECT COALESCE(SUM(amount - refund_amount),0) FROM payments").fetchone()[0],
        "by_train": db.execute(
            "SELECT t.train_no, t.train_name, COUNT(b.pnr) c FROM trains t "
            "LEFT JOIN schedules s ON s.train_no=t.train_no "
            "LEFT JOIN bookings b ON b.schedule_id=s.schedule_id AND b.status!='CANCELLED' "
            "GROUP BY t.train_no ORDER BY c DESC").fetchall(),
    }
    trains = db.execute("SELECT * FROM trains ORDER BY train_no").fetchall()
    return render_template("admin.html", report=report, trains=trains)


@app.route("/admin/train", methods=["POST"])
@admin_required
def admin_add_train():
    f = request.form
    try:
        get_db().execute("INSERT INTO trains VALUES (?,?,?,?,?)",
                         (f["train_no"].strip(), f["train_name"].strip(), f["source"].strip(),
                          f["destination"].strip(), int(f["total_seats"])))
        flash("Train added.", "ok")
    except (sqlite3.IntegrityError, ValueError, KeyError):
        flash("Could not add train (duplicate number or invalid data).", "error")
    return redirect(url_for("admin"))


@app.route("/admin/schedule", methods=["POST"])
@admin_required
def admin_add_schedule():
    f = request.form
    try:
        get_db().execute(
            "INSERT INTO schedules(train_no,journey_date,departure,arrival,base_fare) VALUES (?,?,?,?,?)",
            (f["train_no"], f["journey_date"], f["departure"], f["arrival"], float(f["base_fare"])))
        flash("Schedule added.", "ok")
    except (sqlite3.IntegrityError, ValueError, KeyError):
        flash("Could not add schedule (duplicate date or invalid data).", "error")
    return redirect(url_for("admin"))


@app.route("/admin/fare", methods=["POST"])
@admin_required
def admin_update_fare():
    f = request.form
    try:
        cur = get_db().execute("UPDATE schedules SET base_fare=? WHERE train_no=? AND journey_date>=?",
                               (float(f["base_fare"]), f["train_no"], date.today().isoformat()))
        flash(f"Fare updated on {cur.rowcount} upcoming schedule(s).", "ok")
    except (sqlite3.IntegrityError, ValueError, KeyError):
        flash("Invalid fare.", "error")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
