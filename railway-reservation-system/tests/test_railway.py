"""Tests mapped to the report's control-flow graph (V(G) = 5 independent paths)
plus refund rules, seat locking, waiting list and the HTTP flow."""
import re
from datetime import timedelta

import pytest

import app as rail
from werkzeug.security import generate_password_hash

PAX = [{"name": "Asha", "age": 30, "gender": "F"}]


@pytest.fixture
def db(tmp_path):
    rail.app.config.update(DATABASE=str(tmp_path / "test.db"), TESTING=True)
    with rail.app.app_context():
        yield rail.get_db()


@pytest.fixture
def client(tmp_path):
    rail.app.config.update(DATABASE=str(tmp_path / "http.db"), TESTING=True)
    return rail.app.test_client()


def make_user(db, email="u@x.com"):
    cur = db.execute("INSERT INTO users(name,email,password_hash) VALUES (?,?,?)",
                     ("U", email, generate_password_hash("secret1")))
    return cur.lastrowid


def first_schedule(db, train="12417"):
    return db.execute("SELECT * FROM schedules WHERE train_no=? ORDER BY journey_date LIMIT 1",
                      (train,)).fetchone()


# ---- refund rule -----------------------------------------------------------
@pytest.mark.parametrize("hours,pct", [(100, 75), (48.01, 75), (48, 50), (12, 50), (11.99, 0), (1, 0)])
def test_refund_percent_boundaries(hours, pct):
    assert rail.refund_percent(hours) == pct


def test_berth_cycle():
    assert [rail.berth_for(i) for i in (1, 2, 3, 7, 8, 9)] == [
        "Lower", "Middle", "Upper", "Side Lower", "Side Upper", "Lower"]


# ---- CFG path 1: happy path -> confirmed + 10-digit PNR --------------------
def test_confirmed_booking_has_pnr_seats_and_payment(db):
    uid, s = make_user(db), first_schedule(db)
    pnr, status = rail.create_booking(db, uid, s["schedule_id"], "SL", PAX * 2, "UPI")
    assert status == "CONFIRMED" and re.fullmatch(r"\d{10}", pnr)
    seats = [r[0] for r in db.execute("SELECT seat_no FROM passengers WHERE pnr=? ORDER BY passenger_id", (pnr,))]
    assert seats == [1, 2]
    assert db.execute("SELECT amount FROM payments WHERE pnr=?", (pnr,)).fetchone()[0] == s["base_fare"] * 2


# ---- CFG path 2: invalid train/date ---------------------------------------
def test_invalid_schedule_rejected(db):
    with pytest.raises(rail.RailwayError):
        rail.create_booking(db, make_user(db), 99999, "SL", PAX, "UPI")


def test_departed_train_rejected(db):
    s = first_schedule(db)
    late = rail.departure_dt(s) + timedelta(hours=1)
    with pytest.raises(rail.RailwayError):
        rail.create_booking(db, make_user(db), s["schedule_id"], "SL", PAX, "UPI", now=late)


# ---- CFG path 3: more than 6 passengers -----------------------------------
def test_more_than_six_passengers_rejected(db):
    s = first_schedule(db)
    with pytest.raises(rail.RailwayError):
        rail.create_booking(db, make_user(db), s["schedule_id"], "SL", PAX * 7, "UPI")


# ---- CFG path 4: payment failure releases seats ---------------------------
def test_payment_failure_books_nothing(db):
    uid, s = make_user(db), first_schedule(db)
    before = rail.available_seats(db, s["schedule_id"], "SL")
    with pytest.raises(rail.RailwayError):
        rail.create_booking(db, uid, s["schedule_id"], "SL", PAX, "FAIL (simulate failed payment)")
    assert rail.available_seats(db, s["schedule_id"], "SL") == before
    assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0


# ---- CFG path 5: class full -> waiting list; no double booking -------------
def test_full_class_goes_to_waiting_list_without_double_booking(db):
    uid, s = make_user(db), first_schedule(db)
    sid = s["schedule_id"]
    for _ in range(5):                                   # 5 x 6 = 30 seats (train capacity)
        assert rail.create_booking(db, uid, sid, "SL", PAX * 6, "UPI")[1] == "CONFIRMED"
    assert rail.available_seats(db, sid, "SL") == 0
    pnr, status = rail.create_booking(db, uid, sid, "SL", PAX, "UPI")
    assert status == "WAITING" and rail.waitlist_position(db, pnr) == 1
    seats = [r[0] for r in db.execute(
        "SELECT p.seat_no FROM passengers p JOIN bookings b ON b.pnr=p.pnr "
        "WHERE b.schedule_id=? AND b.status='CONFIRMED'", (sid,))]
    assert len(seats) == len(set(seats)) == 30           # every seat unique


def test_waiting_list_capacity_limit(db):
    uid, s = make_user(db), first_schedule(db)
    sid = s["schedule_id"]
    for _ in range(5):
        rail.create_booking(db, uid, sid, "SL", PAX * 6, "UPI")
    rail.create_booking(db, uid, sid, "SL", PAX * 6, "UPI")           # 6 waiting
    rail.create_booking(db, uid, sid, "SL", PAX * 4, "UPI")           # 10 waiting
    with pytest.raises(rail.RailwayError):
        rail.create_booking(db, uid, sid, "SL", PAX, "UPI")


# ---- cancellation, refunds, waiting list promotion -------------------------
@pytest.mark.parametrize("hours_before,pct", [(72, 75), (24, 50), (5, 0)])
def test_cancellation_refund_rules(db, hours_before, pct):
    uid, s = make_user(db), first_schedule(db)
    pnr, _ = rail.create_booking(db, uid, s["schedule_id"], "SL", PAX, "UPI")
    now = rail.departure_dt(s) - timedelta(hours=hours_before)
    refund, got = rail.cancel_booking(db, pnr, uid, now=now)
    fare = db.execute("SELECT total_fare FROM bookings WHERE pnr=?", (pnr,)).fetchone()[0]
    assert got == pct and refund == round(fare * pct / 100, 2)
    assert db.execute("SELECT status FROM bookings WHERE pnr=?", (pnr,)).fetchone()[0] == "CANCELLED"


def test_cancel_twice_and_wrong_user_rejected(db):
    uid, other, s = make_user(db), make_user(db, "o@x.com"), first_schedule(db)
    pnr, _ = rail.create_booking(db, uid, s["schedule_id"], "SL", PAX, "UPI")
    with pytest.raises(rail.RailwayError):
        rail.cancel_booking(db, pnr, other)
    rail.cancel_booking(db, pnr, uid)
    with pytest.raises(rail.RailwayError):
        rail.cancel_booking(db, pnr, uid)


def test_cancellation_promotes_waiting_booking(db):
    uid, s = make_user(db), first_schedule(db)
    sid = s["schedule_id"]
    pnrs = [rail.create_booking(db, uid, sid, "SL", PAX * 6, "UPI")[0] for _ in range(5)]
    wl_pnr, status = rail.create_booking(db, uid, sid, "SL", PAX * 2, "UPI")
    assert status == "WAITING"
    rail.cancel_booking(db, pnrs[0], uid)
    assert db.execute("SELECT status FROM bookings WHERE pnr=?", (wl_pnr,)).fetchone()[0] == "CONFIRMED"
    assert all(r[0] is not None for r in db.execute("SELECT seat_no FROM passengers WHERE pnr=?", (wl_pnr,)))


# ---- HTTP flow --------------------------------------------------------------
def test_http_register_login_book_cancel(client):
    client.post("/register", data={"name": "Kay", "email": "k@x.com", "password": "secret1"})
    assert client.get("/bookings").status_code == 302                 # login required
    client.post("/login", data={"email": "k@x.com", "password": "secret1"})
    page = client.get("/?source=Prayagraj&destination=Delhi").get_data(as_text=True)
    sid = re.search(r"/book/(\d+)\?cls=SL", page).group(1)
    resp = client.post(f"/book/{sid}", data={"cls": "SL", "name": ["Kay"], "age": ["21"],
                                              "gender": ["M"], "payment_mode": "UPI"}, follow_redirects=True)
    html = resp.get_data(as_text=True)
    pnr = re.search(r"PNR (\d{10})", html).group(1)
    assert "CONFIRMED" in html
    assert "Rs 0.00" not in client.get(f"/ticket/{pnr}").get_data(as_text=True)
    status = client.post("/pnr", data={"pnr": pnr}).get_data(as_text=True)
    assert "<td>K**</td>" in status and "<td>Kay</td>" not in status                    # passenger name masked
    done = client.post(f"/cancel/{pnr}", follow_redirects=True).get_data(as_text=True)
    assert "Refund" in done and "CANCELLED" in done


def test_http_admin_only_and_default_admin_login(client):
    client.post("/register", data={"name": "Kay", "email": "k@x.com", "password": "secret1"})
    client.post("/login", data={"email": "k@x.com", "password": "secret1"})
    assert client.get("/admin").status_code == 403
    client.get("/logout")
    client.post("/login", data={"email": "admin@railway.com", "password": "Admin@123"})
    assert client.get("/admin").status_code == 200
