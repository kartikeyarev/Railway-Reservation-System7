# Railway Reservation System

A web-based railway ticket booking system built for the Software Engineering mini project
(B.Tech IT, Semester V). It implements the design from the project report: role-based access,
train search, booking with a unique 10-digit PNR, seat/berth allocation, waiting list,
rule-based refunds and transactional seat locking to prevent double booking.

**Stack:** Python 3, Flask, SQLite (ACID transactions), HTML/CSS/JavaScript (Jinja templates).

## Features

| Area | What it does |
|------|--------------|
| Authentication | Register / login, salted password hashes, session tokens, Passenger and Admin roles |
| Search | By source, destination, date and class, with live seat availability and fares |
| Booking | 1 to 6 passengers per booking, simulated payment, unique 10-digit PNR, e-ticket |
| Seat allocation | Lowest free seat numbers and berth types assigned inside a locked transaction |
| Waiting list | Full classes go to a waiting list (position shown); promoted automatically on cancellation |
| Cancellation | Refund 75% (over 48 h), 50% (12 to 48 h), 0% (under 12 h); waiting tickets refund 100% |
| PNR status | Public lookup with masked passenger names |
| Admin | Add trains, add schedules, update fares, booking and revenue report |

## Run locally

```bash
git clone <your-repo-url>
cd railway-reservation-system
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py                     # open http://127.0.0.1:5000
```

The database (`railway.db`) is created automatically on first run with sample trains and
30 days of schedules. To reset it: `flask --app app init-db`.

**Default admin:** `admin@railway.com` / `Admin@123`
(set `ADMIN_PASSWORD` before the first run to change it, and set `SECRET_KEY` in production).

To demo a failed payment, choose **FAIL (simulate failed payment)** as the payment mode:
nothing is booked and no seats are held.

## Run tests

```bash
python -m pytest -q
```

21 tests cover the five independent paths of the booking control-flow graph
(V(G) = 5), the refund boundaries, waiting list limits and promotion, double-booking
prevention, and the HTTP flow (register, login, book, PNR status, cancel, admin access).

## Project structure

```
app.py            Flask app: routes + booking, cancellation, refund logic
schema.sql        3NF schema (users, trains, schedules, bookings, passengers, payments)
templates/        Jinja HTML pages
static/style.css  Styling
tests/            pytest suite
```

## How it maps to the report

| Report item | Where in the code |
|-------------|-------------------|
| Control-flow graph (valid train/date, passengers <= 6, seats available, payment) | `create_booking()` in `app.py` |
| Refund rule 75% / 50% / 0% | `refund_percent()`, `cancel_booking()` |
| Transactional locking against double booking | `BEGIN IMMEDIATE` in `create_booking()` / `cancel_booking()` |
| 3NF schema and ER diagram | `schema.sql` |
| Role-based access | `login_required`, `admin_required` |

## Notes and future scope

- Payment is simulated. A real gateway, SMS/email alerts, Tatkal and dynamic fares,
  live train status and a mobile app are listed as future scope in the report.
- To move to MySQL, keep the same schema and replace `BEGIN IMMEDIATE` with
  `SELECT ... FOR UPDATE` row locks inside a transaction.
- Forms have no CSRF tokens yet (add Flask-WTF before any real deployment).
