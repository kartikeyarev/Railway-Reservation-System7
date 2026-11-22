-- Railway Reservation System: 6 tables in 3NF (matches ER diagram in the report)
PRAGMA foreign_keys = ON;

CREATE TABLE users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,              -- salted hash, never plain text
    phone         TEXT,
    role          TEXT NOT NULL DEFAULT 'passenger' CHECK (role IN ('passenger', 'admin'))
);

CREATE TABLE trains (
    train_no    TEXT PRIMARY KEY,
    train_name  TEXT NOT NULL,
    source      TEXT NOT NULL,
    destination TEXT NOT NULL,
    total_seats INTEGER NOT NULL CHECK (total_seats > 0)   -- seats per class
);

CREATE TABLE schedules (
    schedule_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    train_no     TEXT NOT NULL REFERENCES trains(train_no) ON DELETE CASCADE,
    journey_date TEXT NOT NULL,               -- YYYY-MM-DD
    departure    TEXT NOT NULL,               -- HH:MM
    arrival      TEXT NOT NULL,               -- HH:MM
    base_fare    REAL NOT NULL CHECK (base_fare > 0),
    UNIQUE (train_no, journey_date)
);

CREATE TABLE bookings (
    pnr          TEXT PRIMARY KEY CHECK (length(pnr) = 10),
    user_id      INTEGER NOT NULL REFERENCES users(user_id),
    schedule_id  INTEGER NOT NULL REFERENCES schedules(schedule_id),
    travel_class TEXT NOT NULL CHECK (travel_class IN ('SL', '3A', '2A')),
    status       TEXT NOT NULL CHECK (status IN ('CONFIRMED', 'WAITING', 'CANCELLED')),
    total_fare   REAL NOT NULL,
    booked_on    TEXT NOT NULL
);

CREATE TABLE passengers (
    passenger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pnr          TEXT NOT NULL REFERENCES bookings(pnr) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    age          INTEGER NOT NULL CHECK (age BETWEEN 1 AND 120),
    gender       TEXT NOT NULL CHECK (gender IN ('M', 'F', 'O')),
    seat_no      INTEGER,                     -- NULL while on waiting list
    berth        TEXT
);

CREATE TABLE payments (
    payment_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    pnr           TEXT NOT NULL UNIQUE REFERENCES bookings(pnr),
    amount        REAL NOT NULL,
    mode          TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('PAID', 'REFUNDED', 'FORFEITED')),
    refund_amount REAL NOT NULL DEFAULT 0,
    paid_on       TEXT NOT NULL
);

CREATE INDEX idx_sched_date    ON schedules(journey_date);
CREATE INDEX idx_book_sched    ON bookings(schedule_id, travel_class, status);
CREATE INDEX idx_book_user     ON bookings(user_id);
CREATE INDEX idx_pass_pnr      ON passengers(pnr);
