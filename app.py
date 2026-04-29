"""
AbujaCine — Seat-Aware Cinema Reservation System
Abuja, Nigeria | Flask + SQLite | Hybrid Recommender | Paystack Payments
"""


# ── Paystack Keys ──────────────────────────────────────────────────────────
from flask import Flask, render_template, request, jsonify, session, g, redirect
import sqlite3, hashlib, secrets, json, os, time, random, math
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('cinema_SECRET', secrets.token_hex(32))

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'cinema.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

SEAT_LOCKS    = {}   # { "showtime:row:col": {user_id, expires} }
LOCK_DURATION = 300  # seconds

# Paystack keys — replace with your own from dashboard.paystack.com
# These are Paystack's official test keys (safe to use for demos)
PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', 'sk_test_527e7adc01bbe74b54897f6b58cf1555e683ccaf')
PAYSTACK_PUBLIC_KEY = os.environ.get('PAYSTACK_PUBLIC_KEY', 'pk_test_b695c7bf0b597ddebfe2f70ff4aa73927dd1d1de')

# ── DB Helpers ─────────────────────────────────────────────────────────────
def get_db():
    db = getattr(g, '_db', None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
    return db

@app.teardown_appcontext
def close_db(e):
    db = getattr(g, '_db', None)
    if db: db.close()

def qdb(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rv = cur.fetchall(); cur.close()
    return (rv[0] if rv else None) if one else rv

def xdb(sql, args=()):
    db = get_db(); cur = db.execute(sql, args); db.commit()
    return cur.lastrowid

# ── Seat Quality Engine ────────────────────────────────────────────────────
def compute_seat_quality(row, col, total_rows, total_cols):
    """
    Objective seat quality score 0–10.
    Cinema design principle: central mid-distance = best.
    Formula uses 2D Gaussian decay from optimal viewing zone.
    """
    rn = row / total_rows   # 0=front, 1=back
    cn = col / total_cols   # 0=left, 1=right

    # Optimal: 45–65% back, horizontally centred
    opt_r, opt_c = 0.55, 0.50
    rd = abs(rn - opt_r)
    cd = abs(cn - opt_c)

    rs = math.exp(-3.2 * rd ** 1.4)
    cs = math.exp(-2.8 * cd ** 2)

    # Front-row neck-strain penalty
    if rn < 0.18: rs *= 0.45
    # Very back row slight penalty
    if rn > 0.88: rs *= 0.80

    q = (rs * 0.60 + cs * 0.40) * 10
    return round(min(max(q, 0.5), 10.0), 2)

def classify_seat(row, col, total_rows, total_cols):
    """Return position tags list: [center/aisle/edge, front/middle/back]."""
    tags = []
    cr = col / total_cols
    rr = row / total_rows
    if cr < 0.15 or cr > 0.85:  tags.append('edge')
    elif cr < 0.28 or cr > 0.72: tags.append('aisle')
    else: tags.append('center')
    if rr < 0.30:   tags.append('front')
    elif rr > 0.68: tags.append('back')
    else:           tags.append('middle')
    return tags

# ── Recommendation Engine ──────────────────────────────────────────────────
def get_prefs(user_id):
    p = qdb("SELECT * FROM user_preferences WHERE user_id=?", (user_id,), one=True)
    return dict(p) if p else {
        'genre_weights':'{}','seat_position_pref':'center',
        'seat_zone_pref':'middle','avg_quality_pref':7.0,'booking_count':0}

def jaccard(a, b):
    if not a and not b: return 0.0
    return len(a & b) / len(a | b) if (a | b) else 0.0

def collab_score(user_id, movie_id):
    """Jaccard-based collaborative filtering on genre booking history."""
    user_genres = set(r['genre'] for r in qdb(
        """SELECT DISTINCT m.genre FROM bookings b
           JOIN showtimes s ON b.showtime_id=s.id JOIN movies m ON s.movie_id=m.id
           WHERE b.user_id=? AND b.status='confirmed'""", (user_id,)))
    tgt = qdb("SELECT genre FROM movies WHERE id=?", (movie_id,), one=True)
    if not tgt: return 0.0
    others = qdb(
        """SELECT DISTINCT b.user_id FROM bookings b
           JOIN showtimes s ON b.showtime_id=s.id JOIN movies m ON s.movie_id=m.id
           WHERE b.user_id!=? AND m.genre=? AND b.status='confirmed'""",
        (user_id, tgt['genre']))
    if not others: return 0.0
    sims = []
    for o in others:
        og = set(r['genre'] for r in qdb(
            """SELECT DISTINCT m.genre FROM bookings b
               JOIN showtimes s ON b.showtime_id=s.id JOIN movies m ON s.movie_id=m.id
               WHERE b.user_id=? AND b.status='confirmed'""", (o['user_id'],)))
        sims.append(jaccard(user_genres, og))
    return min(sum(sims)/len(sims)*1.5, 1.0) if sims else 0.0

def seat_pref_match(available_seats, prefs):
    """
    Score how well available seats match user's subjective seat preferences.
    Returns (best_match 0–1, best_quality 0–10).
    Checks position tag (center/aisle/edge) and zone (front/middle/back).
    """
    if not available_seats: return 0.0, 0.0
    pos_pref  = prefs.get('seat_position_pref', 'center')
    zone_pref = prefs.get('seat_zone_pref', 'middle')
    avg_q_pref = float(prefs.get('avg_quality_pref', 7.0))
    best_score, best_q = 0.0, 0.0
    for s in available_seats:
        tags = json.loads(s['position_tags']) if s['position_tags'] else []
        q = float(s['quality_score'])
        # Position match score
        if pos_pref in tags:      pm = 1.0
        elif 'aisle' in tags:     pm = 0.5
        else:                     pm = 0.2
        # Zone match score
        zm = 1.0 if zone_pref in tags else 0.35
        # Quality proximity to user's learned preference
        qm = max(0, 1 - abs(q - avg_q_pref) / 10)
        combined = pm*0.40 + zm*0.30 + qm*0.30
        if combined > best_score:
            best_score, best_q = combined, q
    return best_score, best_q

def recommend(user_id, limit=12):
    """
    Full hybrid recommendation combining:
    1. Content-based (genre weights from booking history)
    2. Collaborative (Jaccard similarity)
    3. Context-aware: seat quality, seat preference match, showtime proximity, availability
    4. Rating bonus
    """
    prefs = get_prefs(user_id)
    gw    = json.loads(prefs.get('genre_weights', '{}'))
    now   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    showtimes = qdb(
        """SELECT s.*, m.title, m.genre, m.rating, m.description,
                  m.poster_url, m.duration_min, m.director, m.cast_list,
                  m.id as movie_id
           FROM showtimes s JOIN movies m ON s.movie_id=m.id
           WHERE s.showtime>=? AND m.is_active=1 ORDER BY s.showtime""", (now,))

    scored = {}
    for st in showtimes:
        mid, sid = st['movie_id'], st['id']
        avail = qdb("SELECT * FROM seats WHERE showtime_id=? AND status='available'", (sid,))
        total = qdb("SELECT COUNT(*) as c FROM seats WHERE showtime_id=?", (sid,), one=True)['c']
        if total == 0: continue
        avail_ratio = len(avail) / total
        if avail_ratio == 0: continue

        # ── 1. Content-based genre score
        genre_s = min(gw.get(st['genre'], 0.0), 1.0)

        # ── 2. Collaborative
        collab  = collab_score(user_id, mid)

        # ── 3a. Subjective seat preference match
        pref_s, best_q = seat_pref_match(avail, prefs)

        # ── 3b. Objective seat quality (avg of available seats)
        avg_q = sum(float(s['quality_score']) for s in avail) / len(avail) if avail else 0
        quality_s = avg_q / 10.0

        # ── 3c. Availability
        avail_s = min(avail_ratio * 1.2, 1.0)

        # ── 3d. Showtime proximity (2–12 hrs = ideal)
        try:
            hrs = (datetime.strptime(st['showtime'], '%Y-%m-%d %H:%M:%S') - datetime.now()).total_seconds()/3600
            if 2 <= hrs <= 12:    time_s = 1.0
            elif hrs < 2:         time_s = 0.25
            else:                 time_s = max(0.2, 1-(hrs-12)/72)
        except: time_s = 0.5

        # ── 4. Rating bonus
        rating_s = float(st['rating'] or 0) / 10.0

        # ── Weighted final score
        W = dict(genre=0.20, collab=0.15, pref=0.20, quality=0.15,
                 avail=0.10, time=0.10, rating=0.10)
        score = (W['genre']*genre_s + W['collab']*collab + W['pref']*pref_s
                 + W['quality']*quality_s + W['avail']*avail_s
                 + W['time']*time_s + W['rating']*rating_s)

        # Penalise if best seat is very poor quality or almost sold out
        if best_q < 3.0:         score *= 0.65
        if avail_ratio < 0.05:   score *= 0.55

        if mid not in scored or scored[mid]['score'] < score:
            scored[mid] = {
                'movie_id': mid, 'showtime_id': sid,
                'title': st['title'], 'genre': st['genre'],
                'rating': st['rating'], 'description': st['description'],
                'poster_url': st['poster_url'], 'duration_min': st['duration_min'],
                'director': st['director'], 'cast_list': st['cast_list'],
                'showtime': st['showtime'], 'hall_name': st['hall_name'],
                'cinema_name': st['cinema_name'], 'price': st['price'],
                'score': round(score, 4), 'available_seats': len(avail),
                'total_seats': total, 'best_quality': round(best_q, 1),
                'pref_match': round(pref_s*100), 'avail_pct': round(avail_ratio*100),
            }
    return sorted(scored.values(), key=lambda x: x['score'], reverse=True)[:limit]

def learn_preferences(user_id, movie_id, seat_id):
    """Implicit preference learning after each confirmed booking."""
    movie = qdb("SELECT genre FROM movies WHERE id=?", (movie_id,), one=True)
    seat  = qdb("SELECT * FROM seats WHERE id=?", (seat_id,), one=True)
    if not movie or not seat: return
    db = get_db()
    prefs = qdb("SELECT * FROM user_preferences WHERE user_id=?", (user_id,), one=True)
    tags = json.loads(seat['position_tags']) if seat['position_tags'] else []
    new_pos  = 'center' if 'center' in tags else ('aisle' if 'aisle' in tags else 'edge')
    new_zone = 'middle' if 'middle' in tags else ('front' if 'front' in tags else 'back')

    if prefs:
        gw = json.loads(prefs['genre_weights'])
        alpha = 0.3
        gw[movie['genre']] = round(alpha + (1-alpha)*gw.get(movie['genre'],0.0), 4)
        beta = 0.25
        new_avg_q = (1-beta)*float(prefs['avg_quality_pref']) + beta*float(seat['quality_score'])
        db.execute(
            """UPDATE user_preferences SET genre_weights=?,seat_position_pref=?,
               seat_zone_pref=?,avg_quality_pref=?,booking_count=booking_count+1
               WHERE user_id=?""",
            (json.dumps(gw), new_pos, new_zone, round(new_avg_q,2), user_id))
    else:
        gw = {movie['genre']: 0.5}
        db.execute(
            """INSERT INTO user_preferences
               (user_id,genre_weights,seat_position_pref,seat_zone_pref,avg_quality_pref,booking_count)
               VALUES (?,?,?,?,?,?)""",
            (user_id, json.dumps(gw), new_pos, new_zone, float(seat['quality_score']), 1))
    db.commit()

# ── Auth Helpers ───────────────────────────────────────────────────────────
def hash_pw(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 100000)
    return f"{salt}:{h.hex()}"

def verify_pw(stored, given):
    try:
        salt, h = stored.split(':')
        return hashlib.pbkdf2_hmac('sha256', given.encode(), salt.encode(), 100000).hex() == h
    except: return False

def auth_required(f):
    @wraps(f)
    def d(*a, **kw):
        if 'user_id' not in session: return jsonify({'error':'Login required'}), 401
        return f(*a, **kw)
    return d

def admin_required(f):
    @wraps(f)
    def d(*a, **kw):
        if not session.get('is_admin'): return jsonify({'error':'Admins only'}), 403
        return f(*a, **kw)
    return d

# ── Database Init ──────────────────────────────────────────────────────────
def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, is_admin INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS movies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, genre TEXT NOT NULL, description TEXT,
        duration_min INTEGER, rating REAL DEFAULT 0,
        poster_url TEXT, director TEXT, cast_list TEXT,
        release_year INTEGER, is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS cinemas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, location TEXT NOT NULL, address TEXT
    );
    CREATE TABLE IF NOT EXISTS halls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cinema_id INTEGER NOT NULL, name TEXT NOT NULL,
        total_rows INTEGER NOT NULL, total_cols INTEGER NOT NULL,
        capacity INTEGER NOT NULL,
        FOREIGN KEY (cinema_id) REFERENCES cinemas(id)
    );
    CREATE TABLE IF NOT EXISTS showtimes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        movie_id INTEGER NOT NULL, hall_id INTEGER NOT NULL,
        hall_name TEXT NOT NULL, cinema_name TEXT NOT NULL,
        showtime TIMESTAMP NOT NULL, price REAL NOT NULL,
        FOREIGN KEY (movie_id) REFERENCES movies(id),
        FOREIGN KEY (hall_id) REFERENCES halls(id)
    );
    CREATE TABLE IF NOT EXISTS seats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        showtime_id INTEGER NOT NULL, hall_id INTEGER NOT NULL,
        row_num INTEGER NOT NULL, col_num INTEGER NOT NULL,
        row_label TEXT NOT NULL, seat_number INTEGER NOT NULL,
        quality_score REAL NOT NULL, position_tags TEXT,
        status TEXT DEFAULT 'available',
        locked_by INTEGER, locked_until TIMESTAMP,
        FOREIGN KEY (showtime_id) REFERENCES showtimes(id)
    );
    CREATE TABLE IF NOT EXISTS bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, showtime_id INTEGER NOT NULL,
        seat_id INTEGER NOT NULL, amount REAL NOT NULL,
        status TEXT DEFAULT 'pending',
        payment_ref TEXT, paystack_ref TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (showtime_id) REFERENCES showtimes(id),
        FOREIGN KEY (seat_id) REFERENCES seats(id)
    );
    CREATE TABLE IF NOT EXISTS user_preferences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        genre_weights TEXT DEFAULT '{}',
        seat_position_pref TEXT DEFAULT 'center',
        seat_zone_pref TEXT DEFAULT 'middle',
        avg_quality_pref REAL DEFAULT 7.0,
        booking_count INTEGER DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );
    """)
    db.commit()
    if db.execute("SELECT COUNT(*) FROM movies").fetchone()[0] == 0:
        seed(db)
    db.close()

def seed(db):
    import random; random.seed(99)

    # ── Movies data (from TMDB) ───────────────────────────────────────────
    TMDB = "https://image.tmdb.org/t/p/w500"
    movies = [
        
        ("Deadpool & Wolverine", "Action · Comedy · Superhero",
         "The MCU's most unlikely duo team up to save the multiverse in this irreverent, R-rated adventure.",
         127, 8.1, "English", "2024-07-26",
         "https://image.tmdb.org/t/p/w500/8cdWjvZQUExUUTzyp4t6EDMubfO.jpg",
         "#8B1A1A", 5500, 1, 1, '["Ryan Reynolds","Hugh Jackman","Emma Corrin"]', "Shawn Levy", "R"),

        ("Inside Out 2", "Animation · Family · Comedy",
         "Riley enters her teenage years and new emotions join the team inside her head.",
         100, 7.9, "English", "2024-06-14",
         "https://image.tmdb.org/t/p/w500/vpnVM9B6NMmQpWeZvzLvDESb2QY.jpg",
         "#FF6B35", 4500, 1, 1, '["Amy Poehler","Maya Hawke","Kensington Tallman"]', "Kelsey Mann", "PG"),

        ("Alien: Romulus", "Horror · Sci-Fi · Thriller",
         "Young colonizers face the most terrifying life form in the universe aboard an abandoned space station.",
         119, 7.2, "English", "2024-08-16",
         "https://image.tmdb.org/t/p/w500/b33nnKl1GSFbao4l3fZDDqsMx0F.jpg",
         "#1a2a1a", 4000, 0, 1, '["Cailee Spaeny","David Jonsson","Archie Renaux"]', "Fede Álvarez", "R"),

        ("Twisters", "Action · Adventure · Thriller",
         "Storm chasers pursue devastating tornadoes across Oklahoma in this high-octane spectacle.",
         122, 7.1, "English", "2024-07-19",
         "https://image.tmdb.org/t/p/w500/pjnD08FlMAIXsfOLKQbvmO0f0MD.jpg",
         "#1a3a4a", 4500, 0, 0, '["Daisy Edgar-Jones","Glen Powell","Anthony Ramos"]', "Lee Isaac Chung", "PG-13"),

        ("The Wild Robot", "Animation · Family · Drama",
         "A robot shipwrecked on an uninhabited island must adapt and form bonds with native animals.",
         102, 8.3, "English", "2024-09-27",
         "https://image.tmdb.org/t/p/w500/wTnV3PCVW5O92JMrFvvrRcV39RU.jpg",
         "#2d4a2d", 4500, 1, 0, '["Lupita Nyongo","Pedro Pascal","Kit Connor"]', "Chris Sanders", "PG"),

        ("Wicked", "Musical · Drama · Fantasy",
         "The untold story of the witches of Oz — Elphaba and Glinda's unlikely friendship.",
         160, 8.0, "English", "2024-11-22",
         "https://image.tmdb.org/t/p/w500/c5UPPqLwHBmSiK9vS6hiSnDXDNR.jpg",
         "#2d1a3a", 5500, 1, 1, '["Cynthia Erivo","Ariana Grande","Jonathan Bailey"]', "Jon M. Chu", "PG"),

        ("Gladiator II", "Action · Drama · History",
         "Lucius is forced into the Colosseum after his home is conquered by tyrannical Roman emperors.",
         148, 7.4, "English", "2024-11-22",
         "https://image.tmdb.org/t/p/w500/2cxhvwyEwRlysAmRH4iodkvo0z5.jpg",
         "#3a2a1a", 5000, 1, 1, '["Paul Mescal","Denzel Washington","Pedro Pascal"]', "Ridley Scott", "R"),

        ("Moana 2", "Animation · Family · Adventure",
         "Moana journeys to the far seas of Oceania and into dangerous, long-lost waters.",
         100, 7.0, "English", "2024-11-27",
         "https://image.tmdb.org/t/p/w500/4YZpsylmjHbqeWzjKpUEF8gcLNW.jpg",
         "#0a2a3a", 4500, 0, 1, '["Auli Cravalho","Dwayne Johnson","Alan Tudyk"]', "Dana Ledoux Miller", "PG"),

        ("A Quiet Place: Day One", "Horror · Sci-Fi · Thriller",
         "Experience the day the deadly creatures first arrived on Earth and New York City fell silent.",
         99, 6.9, "English", "2024-06-28",
         "https://image.tmdb.org/t/p/w500/yrpPYKijwdMHyTGIOd1iK1h0Wb6.jpg",
         "#1a0a0a", 4000, 0, 0, '["Lupita Nyongo","Joseph Quinn","Alex Wolff"]', "Michael Sarnoski", "PG-13"),

        ("Kingdom of the Planet of the Apes", "Sci-Fi · Action · Adventure",
         "A young ape embarks on a journey that causes him to question everything he has been taught.",
         145, 7.1, "English", "2024-05-10",
         "https://image.tmdb.org/t/p/w500/gKkl37BQuKTanygYQG1pyYgLVgf.jpg",
         "#2a1a0a", 4500, 0, 0, '["Owen Teague","Freya Allan","Kevin Durand"]', "Wes Ball", "PG-13"),

        ("Despicable Me 4", "Animation · Comedy · Family",
         "Gru and his family are forced on the run after he makes a powerful new enemy.",
         95, 6.8, "English", "2024-07-03",
         "https://image.tmdb.org/t/p/w500/wWba3TaojhK7NdycyUk0dna3Pra.jpg",
         "#f5a623", 4000, 0, 0, '["Steve Carell","Kristen Wiig","Will Ferrell"]', "Chris Renaud", "PG"),

        ("Bad Boys: Ride or Die", "Action · Comedy · Crime",
         "Miami detectives Lowrey and Burnett become outlaws when the Miami PD is threatened.",
         115, 7.0, "English", "2024-06-07",
         "https://image.tmdb.org/t/p/w500/oGythE98MYleE6mZlGs5oBGkux1.jpg",
         "#1a1a0a", 4500, 0, 1, '["Will Smith","Martin Lawrence","Vanessa Hudgens"]', "Adil El Arbi", "R"),
    ]
    
    # Insert movies (extracting only needed fields from 15-element tuples)
    for m in movies:
        # Extract: title, genre, description, duration_min, rating, poster_url, director, cast_list, release_year
        movie_data = (m[0], m[1], m[2], m[3], m[4], m[7], m[13], m[12], m[6])
        db.execute(
            """INSERT INTO movies (title,genre,description,duration_min,rating,
               poster_url,director,cast_list,release_year) VALUES (?,?,?,?,?,?,?,?,?)""", movie_data)

    # ── Real Abuja cinemas ─────────────────────────────────────────────────
    cinemas_data = [
        ("Silverbird Cinemas",   "Central Business District", "Silverbird Entertainment Centre, Herbert Macaulay Way, CBD, Abuja"),
        ("Genesis Cinemas",      "Ceddi Plaza, Central Area", "Ceddi Plaza, Michael Okpara Way, Wuse Zone 5, Abuja"),
        ("Ozone Cinemas",        "Jabi Lake Mall",            "Jabi Lake Mall, Jabi, Abuja"),
        ("FilmHouse Cinemas",    "Lugbe Dunamis Mall",        "Dunamis HQ, Airport Road, Lugbe, Abuja"),
    ]
    cinema_ids = []
    for c in cinemas_data:
        cur = db.execute("INSERT INTO cinemas (name,location,address) VALUES (?,?,?)", c)
        cinema_ids.append(cur.lastrowid)

    # ── Halls per cinema ───────────────────────────────────────────────────
    hall_configs = [
        # (cinema_idx, hall_name, rows, cols)
        (0, "Hall 1 – Main",    12, 16),
        (0, "Hall 2 – Premium", 10, 14),
        (1, "Screen A",         10, 12),
        (1, "Screen B",          8, 10),
        (2, "Ozone Main",       12, 18),
        (2, "Ozone VIP",         8, 10),
        (3, "FilmHouse Gold",   10, 14),
        (3, "FilmHouse Silver",  8, 12),
    ]
    hall_ids = []
    for ci, hname, rows, cols in hall_configs:
        cur = db.execute(
            """INSERT INTO halls (cinema_id,name,total_rows,total_cols,capacity)
               VALUES (?,?,?,?,?)""",
            (cinema_ids[ci], hname, rows, cols, rows*cols))
        hall_ids.append((cur.lastrowid, ci, hname, rows, cols))

    movie_ids = [r[0] for r in db.execute("SELECT id FROM movies").fetchall()]
    cinema_names = [c[0] for c in cinemas_data]

    # ── Showtimes: next 5 days, multiple slots ─────────────────────────────
    base    = datetime.now().replace(minute=0, second=0, microsecond=0)
    times   = [10, 13, 16, 19, 22]
    prices  = [2500, 3000, 2000, 2000, 3500, 4000, 2500, 2000]  # per hall

    RL = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    showtime_records = []
    for day in range(5):
        for t in times:
            hall_info = random.choice(hall_ids)
            hid, ci, hname, rows, cols = hall_info
            mid  = random.choice(movie_ids)
            show_dt = (base + timedelta(days=day)).replace(hour=t)
            price = prices[hall_ids.index(hall_info)]
            cname = cinema_names[ci]
            cur = db.execute(
                """INSERT INTO showtimes
                   (movie_id,hall_id,hall_name,cinema_name,showtime,price)
                   VALUES (?,?,?,?,?,?)""",
                (mid, hid, hname, cname,
                 show_dt.strftime('%Y-%m-%d %H:%M:%S'), price))
            showtime_records.append((cur.lastrowid, hid, rows, cols))

    # ── Seats for each showtime ────────────────────────────────────────────
    for stid, hid, rows, cols in showtime_records:
        hall = db.execute("SELECT * FROM halls WHERE id=?", (hid,)).fetchone()
        tr, tc = hall['total_rows'], hall['total_cols']
        for r in range(1, tr+1):
            for c in range(1, tc+1):
                q    = compute_seat_quality(r, c, tr, tc)
                tags = classify_seat(r, c, tr, tc)
                # Pre-book ~25% of seats randomly
                status = 'booked' if random.random() < 0.25 else 'available'
                db.execute(
                    """INSERT INTO seats
                       (showtime_id,hall_id,row_num,col_num,row_label,
                        seat_number,quality_score,position_tags,status)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (stid, hid, r, c, RL[r-1], c, q,
                     json.dumps(tags), status))

    # Admin + demo users
    db.execute("INSERT OR IGNORE INTO users (username,email,password_hash,is_admin) VALUES (?,?,?,?)",
               ("admin","admin@abujacine.ng", hash_pw("admin123"), 1))
    db.execute("INSERT OR IGNORE INTO users (username,email,password_hash,is_admin) VALUES (?,?,?,?)",
               ("demo","demo@abujacine.ng", hash_pw("demo123"), 0))
    db.commit()

# ── Page Routes ────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', paystack_public_key=PAYSTACK_PUBLIC_KEY)

@app.route('/admin')
def admin():
    if not session.get('is_admin'): return redirect('/')
    return render_template('admin.html')

# ── Auth API ───────────────────────────────────────────────────────────────
@app.route('/api/register', methods=['POST'])
def register():
    d = request.get_json()
    u,e,p = d.get('username','').strip(), d.get('email','').strip().lower(), d.get('password','')
    if not u or not e or not p: return jsonify({'error':'All fields required'}), 400
    if len(p) < 6: return jsonify({'error':'Password min 6 characters'}), 400
    if qdb("SELECT id FROM users WHERE username=? OR email=?", (u,e), one=True):
        return jsonify({'error':'Username or email already exists'}), 409
    uid = xdb("INSERT INTO users (username,email,password_hash) VALUES (?,?,?)", (u,e,hash_pw(p)))
    session.update({'user_id':uid,'username':u,'is_admin':False})
    return jsonify({'success':True,'username':u})

@app.route('/api/login', methods=['POST'])
def login():
    d = request.get_json()
    user = qdb("SELECT * FROM users WHERE username=? OR email=?", (d.get('username',''),)*2, one=True)
    if not user or not verify_pw(user['password_hash'], d.get('password','')):
        return jsonify({'error':'Invalid credentials'}), 401
    session.update({'user_id':user['id'],'username':user['username'],'is_admin':bool(user['is_admin'])})
    return jsonify({'success':True,'username':user['username'],'is_admin':bool(user['is_admin'])})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear(); return jsonify({'success':True})

@app.route('/api/me')
def me():
    if 'user_id' not in session: return jsonify({'logged_in':False})
    return jsonify({'logged_in':True,'user_id':session['user_id'],
                    'username':session['username'],'is_admin':session.get('is_admin',False)})

# ── Movie API ──────────────────────────────────────────────────────────────
@app.route('/api/movies')
def get_movies():
    genre  = request.args.get('genre')
    search = request.args.get('search')
    sql = "SELECT * FROM movies WHERE is_active=1"
    args = []
    if genre:  sql += " AND genre=?"; args.append(genre)
    if search: sql += " AND (title LIKE ? OR description LIKE ?)"; args += [f'%{search}%']*2
    sql += " ORDER BY rating DESC"
    return jsonify([dict(m) for m in qdb(sql, args)])

@app.route('/api/movies/<int:mid>')
def get_movie(mid):
    m = qdb("SELECT * FROM movies WHERE id=?", (mid,), one=True)
    if not m: return jsonify({'error':'Not found'}), 404
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sts = qdb(
        """SELECT s.*, c.name as cinema_full_name, c.address,
                  COUNT(se.id) as total_seats,
                  SUM(CASE WHEN se.status='available' THEN 1 ELSE 0 END) as available_seats
           FROM showtimes s
           LEFT JOIN halls h ON s.hall_id=h.id
           LEFT JOIN cinemas c ON h.cinema_id=c.id
           LEFT JOIN seats se ON se.showtime_id=s.id
           WHERE s.movie_id=? AND s.showtime>=?
           GROUP BY s.id ORDER BY s.showtime""", (mid, now))
    return jsonify({'movie':dict(m),'showtimes':[dict(s) for s in sts]})

@app.route('/api/recommendations')
@auth_required
def recommendations():
    return jsonify(recommend(session['user_id']))

@app.route('/api/genres')
def genres():
    return jsonify([r['genre'] for r in qdb("SELECT DISTINCT genre FROM movies WHERE is_active=1 ORDER BY genre")])

@app.route('/api/cinemas')
def get_cinemas():
    return jsonify([dict(c) for c in qdb("SELECT * FROM cinemas")])

@app.route('/api/my-preferences')
@auth_required
def my_prefs():
    return jsonify(get_prefs(session['user_id']))

# ── Seat API ───────────────────────────────────────────────────────────────
@app.route('/api/seats/<int:sid>')
def get_seats(sid):
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    db = get_db()
    db.execute("UPDATE seats SET status='available',locked_by=NULL,locked_until=NULL WHERE showtime_id=? AND status='locked' AND locked_until<?", (sid, now))
    db.commit()
    seats = qdb(
        """SELECT id,row_num,col_num,row_label,seat_number,quality_score,
                  position_tags,status,
                  CASE WHEN locked_by=? THEN 1 ELSE 0 END as my_lock
           FROM seats WHERE showtime_id=? ORDER BY row_num,col_num""",
        (session.get('user_id',-1), sid))
    st = qdb("""SELECT s.*,m.title FROM showtimes s JOIN movies m ON s.movie_id=m.id WHERE s.id=?""", (sid,), one=True)
    if not st: return jsonify({'error':'Not found'}), 404
    return jsonify({'seats':[dict(s) for s in seats],'showtime':dict(st)})

@app.route('/api/seats/lock', methods=['POST'])
@auth_required
def lock_seat():
    d = request.get_json()
    seat_id, showtime_id = d.get('seat_id'), d.get('showtime_id')
    now = datetime.utcnow()
    db = get_db()
    # Clear expired locks globally
    db.execute("UPDATE seats SET status='available',locked_by=NULL,locked_until=NULL WHERE status='locked' AND locked_until<?", (now.strftime('%Y-%m-%d %H:%M:%S'),))
    seat = qdb("SELECT * FROM seats WHERE id=? AND showtime_id=?", (seat_id, showtime_id), one=True)
    if not seat: return jsonify({'error':'Seat not found'}), 404
    if seat['status'] == 'booked': return jsonify({'error':'Seat already booked'}), 409
    if seat['status'] == 'locked' and seat['locked_by'] != session['user_id']:
        return jsonify({'error':'Seat is held by another user — please choose another'}), 409
    # Release any other lock by this user in this showtime
    db.execute("UPDATE seats SET status='available',locked_by=NULL,locked_until=NULL WHERE showtime_id=? AND locked_by=? AND status='locked'", (showtime_id, session['user_id']))
  lock_until = (now + timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
    db.execute("UPDATE seats SET status='locked',locked_by=?,locked_until=? WHERE id=?", (session['user_id'], lock_until, seat_id))
    db.commit()
        return jsonify({'success':True,'locked_until':lock_until})

@app.route('/api/seats/unlock', methods=['POST'])
@auth_required
def unlock_seat():
    seat_id = request.get_json().get('seat_id')
    db = get_db()
    db.execute("UPDATE seats SET status='available',locked_by=NULL,locked_until=NULL WHERE id=? AND locked_by=?", (seat_id, session['user_id']))
    db.commit()
    return jsonify({'success':True})

# ── Booking API ────────────────────────────────────────────────────────────
@app.route('/api/bookings/initiate', methods=['POST'])
@auth_required
def initiate_booking():
    d = request.get_json()
    sid, seat_id = d.get('showtime_id'), d.get('seat_id')
    seat = qdb("SELECT * FROM seats WHERE id=? AND showtime_id=? AND locked_by=?", (seat_id, sid, session['user_id']), one=True)
    if not seat: return jsonify({'error':'Seat not held by you. Please select again.'}), 400
    st = qdb("SELECT * FROM showtimes WHERE id=?", (sid,), one=True)
    if not st: return jsonify({'error':'Showtime not found'}), 404
    pay_ref = f"ABC-{secrets.token_urlsafe(10).upper()}"
    amount  = float(st['price'])
    bid = xdb("INSERT INTO bookings (user_id,showtime_id,seat_id,amount,status,payment_ref) VALUES (?,?,?,?,?,?)",
              (session['user_id'], sid, seat_id, amount, 'pending', pay_ref))
    user = qdb("SELECT email FROM users WHERE id=?", (session['user_id'],), one=True)
    return jsonify({'booking_id':bid,'payment_ref':pay_ref,'amount':amount,
                    'amount_kobo':int(amount*100),'email':user['email'],
                    'public_key':PAYSTACK_PUBLIC_KEY})

@app.route('/api/bookings/verify', methods=['POST'])
@auth_required
def verify_booking():
    """Called after Paystack inline popup returns success."""
    d = request.get_json()
    bid, ps_ref = d.get('booking_id'), d.get('paystack_ref')
    booking = qdb("SELECT * FROM bookings WHERE id=? AND user_id=?", (bid, session['user_id']), one=True)
    if not booking: return jsonify({'error':'Booking not found'}), 404
    # In production, verify with Paystack API using PAYSTACK_SECRET_KEY:
    # GET https://api.paystack.co/transaction/verify/{ps_ref}
    # For the scope of this project, we trust the reference returned by Paystack JS inline.
    db = get_db()
    db.execute("UPDATE bookings SET status='confirmed',paystack_ref=? WHERE id=?", (ps_ref, bid))
    db.execute("UPDATE seats SET status='booked',locked_by=NULL,locked_until=NULL WHERE id=?", (booking['seat_id'],))
    db.commit()
    st = qdb("SELECT movie_id FROM showtimes WHERE id=?", (booking['showtime_id'],), one=True)
    if st: learn_preferences(session['user_id'], st['movie_id'], booking['seat_id'])
    det = qdb(
        """SELECT b.*,m.title,m.poster_url,s.showtime,s.hall_name,s.cinema_name,s.price,
                  se.row_label,se.seat_number,se.quality_score,se.position_tags
           FROM bookings b
           JOIN showtimes s ON b.showtime_id=s.id
           JOIN movies m ON s.movie_id=m.id
           JOIN seats se ON b.seat_id=se.id
           WHERE b.id=?""", (bid,), one=True)
    return jsonify({'success':True,'booking':dict(det)})

@app.route('/api/my-bookings')
@auth_required
def my_bookings():
    rows = qdb(
        """SELECT b.*,m.title,m.genre,m.poster_url,s.showtime,s.hall_name,s.cinema_name,s.price,
                  se.row_label,se.seat_number,se.quality_score,se.position_tags
           FROM bookings b
           JOIN showtimes s ON b.showtime_id=s.id
           JOIN movies m ON s.movie_id=m.id
           JOIN seats se ON b.seat_id=se.id
           WHERE b.user_id=? AND b.status='confirmed'
           ORDER BY b.created_at DESC""", (session['user_id'],))
    return jsonify([dict(r) for r in rows])

# ── Admin API ──────────────────────────────────────────────────────────────
@app.route('/api/admin/stats')
@admin_required
def admin_stats():
    return jsonify({
        'total_users':    qdb("SELECT COUNT(*) as c FROM users WHERE is_admin=0", one=True)['c'],
        'total_movies':   qdb("SELECT COUNT(*) as c FROM movies", one=True)['c'],
        'total_bookings': qdb("SELECT COUNT(*) as c FROM bookings WHERE status='confirmed'", one=True)['c'],
        'total_revenue':  qdb("SELECT COALESCE(SUM(amount),0) as s FROM bookings WHERE status='confirmed'", one=True)['s'],
        'popular_movies': [dict(r) for r in qdb(
            """SELECT m.title,COUNT(b.id) as bookings FROM bookings b
               JOIN showtimes s ON b.showtime_id=s.id JOIN movies m ON s.movie_id=m.id
               WHERE b.status='confirmed' GROUP BY m.id ORDER BY bookings DESC LIMIT 5""")],
    })

@app.route('/api/admin/movies', methods=['GET','POST'])
@admin_required
def admin_movies():
    if request.method == 'POST':
        d = request.get_json()
        mid = xdb("INSERT INTO movies (title,genre,description,duration_min,rating,poster_url,director,cast_list,release_year) VALUES (?,?,?,?,?,?,?,?,?)",
                  (d['title'],d['genre'],d.get('description',''),d.get('duration_min',120),d.get('rating',7.0),d.get('poster_url',''),d.get('director',''),d.get('cast_list',''),d.get('release_year',2026)))
        return jsonify({'success':True,'movie_id':mid})
    return jsonify([dict(m) for m in qdb("SELECT * FROM movies ORDER BY created_at DESC")])

@app.route('/api/admin/movies/<int:mid>', methods=['PUT','DELETE'])
@admin_required
def admin_movie(mid):
    if request.method == 'DELETE':
        xdb("UPDATE movies SET is_active=0 WHERE id=?", (mid,))
        return jsonify({'success':True})
    d = request.get_json()
    xdb("UPDATE movies SET title=?,genre=?,description=?,duration_min=?,rating=?,poster_url=?,director=?,is_active=? WHERE id=?",
        (d['title'],d['genre'],d.get('description'),d.get('duration_min',120),d.get('rating',7),d.get('poster_url'),d.get('director'),d.get('is_active',1),mid))
    return jsonify({'success':True})

@app.route('/api/admin/showtimes', methods=['GET','POST'])
@admin_required
def admin_showtimes():
    if request.method == 'POST':
        d = request.get_json()
        hall = qdb("SELECT h.*,c.name as cname FROM halls h JOIN cinemas c ON h.cinema_id=c.id WHERE h.id=?", (d['hall_id'],), one=True)
        if not hall: return jsonify({'error':'Hall not found'}), 404
        stid = xdb("INSERT INTO showtimes (movie_id,hall_id,hall_name,cinema_name,showtime,price) VALUES (?,?,?,?,?,?)",
                   (d['movie_id'],d['hall_id'],hall['name'],hall['cname'],d['showtime'],d['price']))
        db = get_db(); RL='ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        for r in range(1,hall['total_rows']+1):
            for c in range(1,hall['total_cols']+1):
                q = compute_seat_quality(r,c,hall['total_rows'],hall['total_cols'])
                tags = classify_seat(r,c,hall['total_rows'],hall['total_cols'])
                db.execute("INSERT INTO seats (showtime_id,hall_id,row_num,col_num,row_label,seat_number,quality_score,position_tags,status) VALUES (?,?,?,?,?,?,?,?,?)",
                           (stid,hall['id'],r,c,RL[r-1],c,q,json.dumps(tags),'available'))
        db.commit()
        return jsonify({'success':True,'showtime_id':stid})
    rows = qdb("SELECT s.*,m.title FROM showtimes s JOIN movies m ON s.movie_id=m.id ORDER BY s.showtime DESC LIMIT 60")
    return jsonify([dict(r) for r in rows])

@app.route('/api/admin/bookings')
@admin_required
def admin_bookings():
    rows = qdb(
        """SELECT b.*,u.username,m.title,s.showtime,s.hall_name,s.cinema_name,
                  se.row_label,se.seat_number
           FROM bookings b JOIN users u ON b.user_id=u.id
           JOIN showtimes s ON b.showtime_id=s.id
           JOIN movies m ON s.movie_id=m.id
           JOIN seats se ON b.seat_id=se.id
           ORDER BY b.created_at DESC LIMIT 100""")
    return jsonify([dict(r) for r in rows])

@app.route('/api/halls')
def get_halls():
    return jsonify([dict(h) for h in qdb("SELECT h.*,c.name as cinema_name FROM halls h JOIN cinemas c ON h.cinema_id=c.id")])

with app.app_context():
    init_db()

if __name__ == '__main__':
    init_db()

if __name__ == '__main__':
    init_db()
    print("="*55)
    print("  AbujaCine — Cinema Reservation System")
    print("  Abuja, Nigeria")
    print("  http://127.0.0.1:5000")
    print("  Admin: admin / admin123")
    print("  Demo:  demo  / demo123")
    print("="*55)
    app.run(debug=True, port=5000)
