from flask import Flask, render_template, send_file, request
import qrcode
import io
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import gspread
from google.oauth2.service_account import Credentials
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_auth_requests
from flask import request, Response
from flask import session
from flask_session import Session

import threading
import queue
import csv
import os
import uuid
import logging
import random
import ipaddress


# =========================
# ⚙️ SETUP
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", "abc123")  # set APP_SECRET_KEY in prod

# ✅ Server-side session config (this is the Flask session used only for
# anti-forwarding / anti-duplicate token tracking — NOT a "class session"
# concept. There is no "pick your session/lab" feature in this version.)
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = "./flask_session"
app.config["SESSION_PERMANENT"] = True
app.config["SESSION_USE_SIGNER"] = True

Session(app)

MASTER_FILE = "attendance_master.csv"   # rows that have been confirmed uploaded to Sheets
LOCAL_FILE = "attendance_log.csv"       # append-only durability log, written in background
FINGERPRINT_FILE = "fingerprints.log"   # persists fingerprint_set across restarts

# Hosts like Render run their servers on UTC regardless of the region you
# pick (e.g. "Oregon" is just where the hardware is, not what timezone the
# clock uses) — so every date/time recorded or displayed must be converted
# to IST explicitly, rather than relying on datetime.now() to already be
# in the right timezone.
IST = ZoneInfo("Asia/Kolkata")

def now_ist():
    return datetime.now(IST)

# =========================
# 📁 FILE SETUP
# =========================
def create_file_if_not_exists(file):
    if not os.path.exists(file):
        with open(file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Student ID", "Date", "Time", "Session"])

create_file_if_not_exists(MASTER_FILE)
create_file_if_not_exists(LOCAL_FILE)

# =========================
# 🔐 GOOGLE SHEETS
# =========================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

# On Render (or any host), set an env var GOOGLE_CREDENTIALS_JSON containing
# the *entire contents* of your service account's credentials.json file.
# Locally, just keep using a credentials.json file next to this script —
# never commit that file to Git.
_creds_json_env = os.environ.get("GOOGLE_CREDENTIALS_JSON")
if _creds_json_env:
    import json
    creds_info = json.loads(_creds_json_env)
    creds = Credentials.from_service_account_info(creds_info, scopes=scope)
else:
    creds = Credentials.from_service_account_file("credentials.json", scopes=scope)

client = gspread.authorize(creds)

SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "Attendance sheet Course CSF111")
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Sheet1")

# The list of sessions students can choose from on the mark page. Change
# this anytime via the SESSION_OPTIONS env var (comma-separated), no code
# change needed — e.g. "Lecture,Lab A,Lab B".
SESSION_OPTIONS = [
    s.strip() for s in os.environ.get(
        "SESSION_OPTIONS",
        "L1/P1(RAUNAK), L2/P2(ABHISEK), L3/P3(SANDESH)"
    ).split(",") if s.strip()
]

def session_category(session_name):
    """
    Each dropdown option (L1/P1, L2/P2, ...) is now a single combined
    slot — there's no separate "session" vs "tutorial" concept anymore, so
    every option maps to the same category. This is what the
    duplicate-prevention logic keys on: a student can submit at most ONE
    of these options per day, regardless of which one (L1/P1 vs L3/P3)
    they pick — it's the single daily slot that's limited, not the exact
    option chosen.
    """
    return "attendance"

# =========================
# 🗓️ SESSION SCHEDULE (weekday + time gating)
# =========================
# Which SESSION_OPTIONS are shown/accepted depends on the current IST
# weekday and time. Configure this with the SESSION_SCHEDULE_JSON env var,
# or just edit SESSION_SCHEDULE below — no other code needs to change.
#
# Format: {"<exact session option>": {"days": ["Mon", "Wed", ...], "start": "HH:MM", "end": "HH:MM"}}
# - "days" uses 3-letter names: Mon, Tue, Wed, Thu, Fri, Sat, Sun
# - "start"/"end" are 24h IST clock times — the actual class start/end.
#   SESSION_BUFFER_MINUTES (below) is applied automatically on both sides,
#   so leave these as the real class times, not the padded window.
# - A session name with NO entry here is always available (unrestricted) —
#   so you only need to add entries for the ones you actually want to gate.
#
# NOTE: Abhisek (3-4pm) and Sandesh (4-5pm) are back-to-back, so their
# buffered windows would normally overlap by 20 min around 4:00 — Abhisek
# has no end-buffer and Sandesh has no start-buffer here so the cutover at
# 4:00 PM stays clean (only one of them ever shows at a time).
_DAY_NAME_TO_INDEX = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}

# How many minutes early/late a student can still mark attendance, relative
# to each session's configured start/end (e.g. 10 = window opens 10 min
# before class and stays open 10 min after). Applies to every entry below
# unless a session sets its own "start_buffer"/"end_buffer" override.
SESSION_BUFFER_MINUTES = int(os.environ.get("SESSION_BUFFER_MINUTES", "10"))

_session_schedule_env = os.environ.get("SESSION_SCHEDULE_JSON")
if _session_schedule_env:
    import json as _json
    SESSION_SCHEDULE = _json.loads(_session_schedule_env)
else:
    SESSION_SCHEDULE = {
        "L1/P1(RAUNAK)":  {"days": ["Mon", "Wed", "Fri"], "start": "12:00", "end": "13:00"},
        "L2/P2(ABHISEK)": {"days": ["Tue", "Thu", "Fri"], "start": "15:00", "end": "16:00", "end_buffer": 0},
        "L3/P3(SANDESH)": {"days": ["Tue", "Thu", "Fri"], "start": "16:00", "end": "17:00", "start_buffer": 0},
    }


def _time_str_to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_session_available(session_name, dt):
    """True if `session_name` should be selectable at IST datetime `dt`,
    including SESSION_BUFFER_MINUTES of slack before/after the configured
    start/end (or a per-session "start_buffer"/"end_buffer" override, e.g.
    for back-to-back classes that shouldn't overlap). Sessions with no
    schedule entry are always available."""
    rule = SESSION_SCHEDULE.get(session_name)
    if not rule:
        return True

    allowed_days = {_DAY_NAME_TO_INDEX[d] for d in rule.get("days", [])}
    if allowed_days and dt.weekday() not in allowed_days:
        return False

    start = rule.get("start")
    end = rule.get("end")
    if start and end:
        now_minutes = dt.hour * 60 + dt.minute
        start_buffer = rule.get("start_buffer", SESSION_BUFFER_MINUTES)
        end_buffer = rule.get("end_buffer", SESSION_BUFFER_MINUTES)
        window_start = _time_str_to_minutes(start) - start_buffer
        window_end = _time_str_to_minutes(end) + end_buffer
        if not (window_start <= now_minutes < window_end):
            return False

    return True


def get_available_sessions(dt=None):
    """SESSION_OPTIONS filtered down to whatever's currently in its
    scheduled weekday/time window (or unrestricted)."""
    dt = dt or now_ist()
    return [s for s in SESSION_OPTIONS if is_session_available(s, dt)]

sheet = client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
STUDENT_MAP_SHEET = client.open(SPREADSHEET_NAME).worksheet("StudentMap")
FINGERPRINT_SHEET = client.open(SPREADSHEET_NAME).worksheet(
    os.environ.get("FINGERPRINT_WORKSHEET_NAME", "Fingerprints")
)
# NOTE: there is no answer/grading step in this version — the question,
# if any, lives entirely in the embedded Google Form and is graded there,
# not by this app. CORRECT_ANSWERS_SHEET / QUESTION_SHEET have been removed.



def sheets_call_with_retry(func, *args, max_retries=5, base_delay=1.5, **kwargs):
    """
    Wrap any gspread call with exponential backoff + jitter.
    Protects bursts of ~200 scans from tripping Google Sheets API rate limits
    (default quota is per-minute, so a burst upload can otherwise fail outright).
    """
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            wait = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            logging.warning(f"Sheets API error (attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait:.1f}s")
            time.sleep(wait)
        except Exception as e:
            wait = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            logging.warning(f"Sheets call failed (attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"Sheets call failed after {max_retries} retries")


# =========================
# ⚡ IN-MEMORY STATE (single source of truth — see NOTES at bottom)
# =========================
records_cache = []
attendance_set = set()          # (student_id, date) already recorded — sheet + pending
attendance_lock = threading.Lock()   # cheap in-memory lock, NOT a disk file lock

submission_queue = queue.Queue()     # rows waiting to be persisted + uploaded
fingerprint_set = set()
fingerprint_lock = threading.Lock()
fingerprint_write_queue = queue.Queue()  # fp_keys waiting to be persisted to FINGERPRINT_FILE

student_map_cache = {}       # student_id -> {"name": ..., "email": ...}
email_to_student_cache = {}  # lowercased email -> student_id (for Google Sign-In lookup)
student_map_last_fetch = 0
STUDENT_MAP_CACHE_TTL = 300  # roster changes rarely, so a longer TTL is fine

BATCH_SIZE = 100         # upload as soon as this many rows are queued...
FLUSH_INTERVAL = 15      # ...or at least this often (seconds), whichever comes first

# =========================
# 💾 FINGERPRINT PERSISTENCE (survives server restarts)
# =========================
def load_fingerprint_set():
    if not os.path.exists(FINGERPRINT_FILE):
        return
    try:
        with open(FINGERPRINT_FILE, "r") as f:
            for line in f:
                key = line.strip()
                if key:
                    fingerprint_set.add(key)
        logging.info(f"Loaded {len(fingerprint_set)} fingerprint entries from local disk")
    except Exception as e:
        logging.error(f"Failed to load {FINGERPRINT_FILE}: {e}")


def load_fingerprint_set_from_sheet():
    """
    Loads previously-seen device fingerprints from the Fingerprints
    worksheet. This is the piece that makes device-level duplicate
    protection survive a restart on hosts with ephemeral local disk (e.g.
    Render) — the Sheet is the durable source of truth here, the same way
    it already is for attendance itself. The local file is kept too, purely
    as a faster warm-cache for local runs; it's fine if it's empty/stale.
    """
    try:
        values = sheets_call_with_retry(FINGERPRINT_SHEET.get_all_values)
    except Exception as e:
        logging.error(f"Failed to load fingerprints from Sheet: {e}")
        return
    if not values:
        return
    loaded = 0
    with fingerprint_lock:
        for row in values[1:]:  # skip header row
            if row and row[0].strip():
                fingerprint_set.add(row[0].strip())
                loaded += 1
    logging.info(f"Loaded {loaded} fingerprint entries from the Fingerprints sheet")


FP_BATCH_SIZE = 100
FP_FLUSH_INTERVAL = 15

def fingerprint_writer_loop():
    """
    Drains fingerprint_write_queue, writes each key to the local file right
    away (cheap, fast, fine as a warm-cache), and batches uploads to the
    Fingerprints sheet the same way attendance rows are batched — every
    FP_BATCH_SIZE keys or FP_FLUSH_INTERVAL seconds, whichever comes first.
    This is what makes the "already submitted from this device" check
    survive a restart on hosts with ephemeral disk.
    """
    buffer = []
    last_flush = time.time()

    while True:
        try:
            timeout = max(0.5, FP_FLUSH_INTERVAL - (time.time() - last_flush))
            fp_key = fingerprint_write_queue.get(timeout=timeout)
            buffer.append(fp_key)
            try:
                with open(FINGERPRINT_FILE, "a") as f:
                    f.write(fp_key + "\n")
            except Exception as e:
                logging.error(f"Fingerprint local persist error: {e}")
        except queue.Empty:
            pass

        should_flush = len(buffer) >= FP_BATCH_SIZE or (time.time() - last_flush) >= FP_FLUSH_INTERVAL

        if should_flush and buffer:
            to_upload = buffer
            buffer = []
            last_flush = time.time()
            try:
                sheets_call_with_retry(FINGERPRINT_SHEET.append_rows, [[k] for k in to_upload])
                logging.info(f"Uploaded {len(to_upload)} fingerprint entries to Sheet")
            except Exception as e:
                logging.error(f"Fingerprint sheet flush error, re-queuing {len(to_upload)}: {e}")
                for k in to_upload:
                    fingerprint_write_queue.put(k)

# =========================
# 🔁 CACHE REFRESH (also the source of truth for de-duplication)
# =========================
def _parse_records_from_values(all_values):
    """
    Manually maps raw sheet values to records by column name, instead of
    using gspread's get_all_records(). get_all_records() is strict about
    the header row — a single duplicate header, a blank header cell, or
    leftover columns from an older sheet schema can make it silently fail
    to parse or skip rows entirely, which is exactly what causes /stats to
    undercount without any visible error. This approach only requires
    "Student ID", "Date", and "Time" to exist in the header row; "Session"
    is optional so older rows recorded before the session field existed
    still parse fine (they just show an empty session).
    """
    if not all_values:
        return []

    header = all_values[0]
    try:
        sid_idx = header.index("Student ID")
        date_idx = header.index("Date")
        time_idx = header.index("Time")
    except ValueError as e:
        logging.error(f"Sheet header is missing an expected column: {e}. "
                       f"Actual header row: {header}")
        return []

    session_idx = header.index("Session") if "Session" in header else None

    max_idx = max(sid_idx, date_idx, time_idx, session_idx or 0)
    records = []
    for row in all_values[1:]:
        if len(row) <= max_idx:
            row = row + [""] * (max_idx + 1 - len(row))  # pad short rows
        student_id = row[sid_idx].strip()
        date_val = row[date_idx].strip()
        if not student_id or not date_val:
            continue
        records.append({
            "Student ID": student_id,
            "Date": date_val,
            "Time": row[time_idx].strip(),
            "Session": row[session_idx].strip() if session_idx is not None else "",
        })
    return records


def _do_cache_refresh():
    global records_cache
    all_values = sheets_call_with_retry(sheet.get_all_values)
    data = _parse_records_from_values(all_values)
    temp_set = set()

    for row in data:
        student = str(row.get("Student ID", "")).strip()
        date = str(row.get("Date", "")).strip()
        session_val = str(row.get("Session", "")).strip()
        category = session_category(session_val) if session_val else ""
        if student and date:
            temp_set.add((student, date, category))

    records_cache[:] = data
    logging.info(f"Cache refresh: loaded {len(data)} rows from the Sheet")

    with attendance_lock:
        # merge rather than clobber: anything queued locally but not yet
        # reflected in the sheet snapshot must stay marked as taken
        attendance_set.update(temp_set)


def load_local_log_into_attendance_set():
    """
    Rebuilds attendance_set from the local durability log (LOCAL_FILE) too,
    not just the Sheet. Without this, a restart happening between a
    successful /submit and that row's next Sheets upload (up to
    FLUSH_INTERVAL seconds later) would "forget" the submission — the
    student could then submit again as if for the first time.
    """
    if not os.path.exists(LOCAL_FILE):
        return
    try:
        with open(LOCAL_FILE, "r") as f:
            reader = csv.reader(f)
            next(reader, None)
            rows = [row for row in reader if row and len(row) >= 2]

        with attendance_lock:
            for row in rows:
                session_val = row[3].strip() if len(row) > 3 else ""
                category = session_category(session_val) if session_val else ""
                attendance_set.add((row[0], row[1], category))
        logging.info(f"Loaded {len(rows)} rows from local log into attendance_set")
    except Exception as e:
        logging.error(f"Failed to load {LOCAL_FILE} into attendance_set: {e}")


def refresh_student_map():
    """
    Loads Student ID -> {name, email} from the StudentMap worksheet, plus
    the reverse email -> Student ID lookup used by Google Sign-In to
    resolve who's actually submitting. Refreshed on its own TTL (see
    STUDENT_MAP_CACHE_TTL) since a class roster changes far less often
    than attendance itself.
    """
    global student_map_cache, email_to_student_cache, student_map_last_fetch
    try:
        rows = sheets_call_with_retry(STUDENT_MAP_SHEET.get_all_records)
        new_map = {}
        new_email_map = {}
        for row in rows:
            sid = str(row.get("Student ID", "")).strip()
            email = str(row.get("Student Email-ID", "")).strip()
            if sid:
                new_map[sid] = {
                    "name": str(row.get("Student Name", "")).strip(),
                    "email": email,
                }
                if email:
                    new_email_map[email.lower()] = sid
        student_map_cache = new_map
        email_to_student_cache = new_email_map
        student_map_last_fetch = time.time()
    except Exception as e:
        logging.error(f"Student map refresh error: {e}")


def verify_google_identity(token_str):
    """
    Verifies a Google Sign-In ID token (JWT) and resolves it to a Student
    ID from the roster. Returns (student_id, email, error_message) — on
    success error_message is None; on failure student_id/email are None.
    """
    if not GOOGLE_OAUTH_CLIENT_ID:
        return None, None, "Google Sign-In isn't configured on the server."

    if not token_str:
        return None, None, "Please sign in with your Google account first."

    try:
        idinfo = google_id_token.verify_oauth2_token(
            token_str, google_auth_requests.Request(), GOOGLE_OAUTH_CLIENT_ID
        )
    except Exception as e:
        logging.warning(f"Google ID token verification failed: {e}")
        return None, None, "Google sign-in could not be verified. Please try signing in again."

    if not idinfo.get("email_verified"):
        return None, None, "Your Google account's email isn't verified."

    email = str(idinfo.get("email", "")).strip()
    hd = str(idinfo.get("hd", "")).strip().lower()

    if ALLOWED_EMAIL_DOMAIN:
        domain_ok = hd == ALLOWED_EMAIL_DOMAIN or email.lower().endswith("@" + ALLOWED_EMAIL_DOMAIN)
        if not domain_ok:
            return None, None, f"Please sign in with your @{ALLOWED_EMAIL_DOMAIN} university account."

    if time.time() - student_map_last_fetch > STUDENT_MAP_CACHE_TTL:
        refresh_student_map()

    student_id = email_to_student_cache.get(email.lower())
    if not student_id:
        return None, None, "This Google account isn't found in the class roster. Contact your instructor."

    return student_id, email, None


def refresh_cache_loop():
    while True:
        try:
            _do_cache_refresh()
        except Exception as e:
            logging.error(f"Cache refresh error: {e}")

        if time.time() - student_map_last_fetch > STUDENT_MAP_CACHE_TTL:
            refresh_student_map()

        # prune fingerprint_set of stale (yesterday-or-older) entries so it
        # doesn't grow forever, then rewrite the persisted file to match
        today = now_ist().strftime("%Y-%m-%d")
        with fingerprint_lock:
            stale = {k for k in fingerprint_set if not k.endswith(today)}
            fingerprint_set.difference_update(stale)
            current_keys = list(fingerprint_set)

        if stale:
            try:
                with open(FINGERPRINT_FILE, "w") as f:
                    for key in current_keys:
                        f.write(key + "\n")
            except Exception as e:
                logging.error(f"Fingerprint file rewrite error: {e}")

        time.sleep(240)

# =========================
# 📤 BACKGROUND WRITER + BATCH UPLOAD
# =========================
def writer_and_flush_loop():
    """
    Drains submission_queue, appends to the durability log immediately,
    and batches uploads to Google Sheets either every BATCH_SIZE rows or
    every FLUSH_INTERVAL seconds — whichever happens first. This keeps the
    /submit request path free of disk and network I/O entirely.
    """
    buffer = []
    last_flush = time.time()

    while True:
        try:
            timeout = max(0.5, FLUSH_INTERVAL - (time.time() - last_flush))
            row = submission_queue.get(timeout=timeout)
            buffer.append(row)

            # write-through to the local durability log right away (cheap, local disk)
            with open(LOCAL_FILE, "a", newline="") as f:
                csv.writer(f).writerow(row)

        except queue.Empty:
            pass

        should_flush = len(buffer) >= BATCH_SIZE or (time.time() - last_flush) >= FLUSH_INTERVAL

        if should_flush and buffer:
            to_upload = buffer
            buffer = []
            last_flush = time.time()
            try:
                sheets_call_with_retry(sheet.append_rows, to_upload)
                with open(MASTER_FILE, "a", newline="") as f:
                    csv.writer(f).writerows(to_upload)
                logging.info(f"Uploaded {len(to_upload)} rows to Google Sheets")

                # Refresh records_cache/attendance_set right away instead of
                # waiting for the next scheduled 4-minute refresh — without
                # this, /stats could lag up to ~4 minutes behind a
                # submission that already succeeded in the Sheet.
                try:
                    _do_cache_refresh()
                except Exception as e:
                    logging.error(f"Post-upload cache refresh failed: {e}")

            except Exception as e:
                logging.error(f"Flush error, re-queuing {len(to_upload)} rows: {e}")
                # put them back so we retry next cycle instead of losing data
                for r in to_upload:
                    submission_queue.put(r)

# =========================
# 🚀 THREADS
# =========================
def start_background_threads():
    threading.Thread(target=refresh_cache_loop, daemon=True).start()
    threading.Thread(target=writer_and_flush_loop, daemon=True).start()
    threading.Thread(target=fingerprint_writer_loop, daemon=True).start()

# Populate the dedup cache synchronously once at startup so the very first
# requests aren't racing an empty attendance_set.
try:
    _do_cache_refresh()
except Exception as e:
    logging.error(f"Initial cache load failed: {e}")

# Also merge in anything durably logged locally but not yet confirmed
# uploaded — closes the restart-forgets-a-pending-row gap.
load_local_log_into_attendance_set()

# Restore fingerprint_set so device-level dedup survives a restart too.
load_fingerprint_set()
load_fingerprint_set_from_sheet()

try:
    refresh_student_map()
except Exception as e:
    logging.error(f"Initial student map load failed: {e}")

start_background_threads()

# =========================
# 🔁 TOKEN
# =========================
def generate_token():
    return str(int(time.time())) + "-" + str(uuid.uuid4())[:8]

BASE_URL = os.environ.get("APP_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "http://10.70.112.87:8000"
TOKEN_TTL_SECONDS = 12   # how long the QR itself stays scannable — keeps forwarding hard
ANSWER_WINDOW_SECONDS = 360  # once a student has scanned in, how long they get to submit

# The real Google Form URL. Kept server-side only and injected into
# mark.html as an iframe src ONLY after a request has already passed the
# same token/campus checks /mark itself enforces — so the raw link never
# sits in a static page a student could view-source or bookmark before
# scanning. (Anyone deliberately opening devtools on an already-scanned
# page can still find it in the DOM — that's an accepted, unavoidable
# limit of anything rendered client-side.)
GOOGLE_FORM_URL = os.environ.get("GOOGLE_FORM_URL", "").strip()

# =========================
# 🔐 GOOGLE SIGN-IN (identity verification)
# =========================
# Students authenticate with their university Google account instead of
# freely typing a Student ID. This closes two problems that a client-side
# device fingerprint can never fully close: (1) the same person submitting
# from multiple browsers/incognito on one phone, since each of those wipes
# any client-stored fingerprint, and (2) someone typing in a Student ID
# that isn't their own.
#
# GOOGLE_OAUTH_CLIENT_ID: create this in Google Cloud Console → APIs &
# Services → Credentials → Create Credentials → OAuth client ID → type
# "Web application" → add your app's URL under Authorized JavaScript
# origins (e.g. https://your-app.onrender.com). This is a DIFFERENT
# credential from the service-account credentials.json used for Sheets.
#
# ALLOWED_EMAIL_DOMAIN: e.g. "university.edu" — only Google accounts on
# this domain are accepted. Leave unset to allow any Google account
# (not recommended for real use).
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
ALLOWED_EMAIL_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "").strip().lower()

# =========================
# 🌐 CAMPUS NETWORK RESTRICTION
# =========================
# Comma-separated list of IPv4 CIDR blocks allowed to reach /mark and
# /submit — e.g. "10.70.16.0/22,192.168.1.0/24". Leave the env var unset
# (default "") to disable this check entirely, which is useful for local
# testing before you know your campus Wi-Fi's actual subnet.
#
# HOW TO FIND YOUR CAMPUS SUBNET: connect a laptop to the same classroom
# Wi-Fi students use, then run `ipconfig` (Windows) and look at the IPv4
# Address + Subnet Mask. E.g. IP 10.70.16.159 with mask 255.255.252.0 is
# CIDR 10.70.16.0/22.
CAMPUS_IP_RANGES = [
    r.strip() for r in os.environ.get("CAMPUS_IP_RANGES", "").split(",") if r.strip()
]

# Set to True only if this app sits behind a reverse proxy (e.g. nginx, or
# Render's own proxy) that sets X-Forwarded-For — otherwise every request
# would appear to come from the proxy's own IP and the campus check would
# block everyone. Render sets a RENDER env var automatically, so this
# defaults to True there and False for local/manual runs.
_default_trust_proxy = "true" if os.environ.get("RENDER") else "false"
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", _default_trust_proxy).lower() == "true"

_campus_networks = []
for _cidr in CAMPUS_IP_RANGES:
    try:
        _campus_networks.append(ipaddress.ip_network(_cidr, strict=False))
    except ValueError:
        logging.error(f"Ignoring invalid CAMPUS_IP_RANGES entry: {_cidr}")


def get_client_ip():
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr


def is_on_campus_network():
    """True if the campus check is disabled, or the requester's IP falls
    inside one of the configured CIDR blocks."""
    if not _campus_networks:
        return True
    try:
        client_ip = ipaddress.ip_address(get_client_ip())
    except ValueError:
        return False
    return any(client_ip in net for net in _campus_networks)

# =========================
# 🔐 SESSION INIT CHECK
# =========================
@app.before_request
def make_session_permanent():
    session.permanent = True
    if "init_check" not in session:
        session["init_check"] = str(uuid.uuid4())

# =========================
# 🏠 HOME
# =========================
def require_admin():
    """Returns a 401 Response if not authenticated as admin, else None."""
    auth = request.authorization
    if not auth or auth.password != os.environ.get("ADMIN_PASSWORD", "admin123"):
        return Response(
            "Login required", 401,
            {"WWW-Authenticate": 'Basic realm="Admin Access"'}
        )
    return None


@app.route("/healthz")
def healthz():
    """Cheap, no-auth endpoint for uptime pingers (e.g. UptimeRobot, cron-job.org)
    to hit every ~10 minutes on Render's free tier, reducing how often the
    instance spins down from inactivity. Doesn't touch Sheets or sessions."""
    return "OK", 200


@app.route("/")
def index():
    denied = require_admin()
    if denied:
        return denied
    return render_template("index.html")

# =========================
# 📷 QR
# =========================
@app.route("/qr")
def qr():
    token = generate_token()
    qr_data = f"{BASE_URL}/mark?token={token}"

    # High error correction (~30% damage tolerance, vs the ~15% default)
    # plus a larger box_size means students farther from the screen, at an
    # angle, or with a lower-quality camera are far less likely to produce
    # a misread that lands on "Invalid QR" instead of a clean scan. Token
    # data is short, so there's plenty of headroom to use the highest
    # correction level without the pattern becoming denser/harder to read.
    qr_maker = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=14,
        border=4,
    )
    qr_maker.add_data(qr_data)
    qr_maker.make(fit=True)
    qr_img = qr_maker.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

def check_token(token):
    """
    Returns "valid", "expired", or "invalid". Shared by /mark and /submit so
    the TTL is enforced end-to-end, not just at initial page load — a token
    that was fine when the form loaded can still expire before someone gets
    around to submitting it (which is exactly what makes forwarding hard).
    """
    if not token:
        return "invalid"
    try:
        token_time = int(token.split("-")[0])
    except Exception:
        return "invalid"
    if abs(int(time.time()) - token_time) > TOKEN_TTL_SECONDS:
        return "expired"
    return "valid"


def validate_and_track_token(token, allow_start=True):
    """
    Two-stage check:
    - First time this exact token is seen in a session, it must pass the
      SHORT scan-freshness check (TOKEN_TTL_SECONDS) — this is what makes
      forwarding a link/photo impractical.
    - Once accepted, the session gets its own longer ANSWER_WINDOW_SECONDS
      clock, independent of the raw token's age, so filling in the student
      ID and submitting doesn't race the scan window.

    allow_start=False (used by /submit) means this token must have ALREADY
    been validated via /mark in this same session — it can't be used to
    start a fresh window by POSTing directly to /submit.

    Returns "ok", "invalid", or "expired".
    """
    if not token:
        return "invalid"

    if session.get("mark_token") == token:
        started = session.get("mark_started", 0)
        if time.time() - started > ANSWER_WINDOW_SECONDS:
            return "expired"
        return "ok"

    if not allow_start:
        return "invalid"

    status = check_token(token)
    if status != "valid":
        return status

    session["mark_token"] = token
    session["mark_started"] = int(time.time())
    return "ok"


# =========================
# 📝 MARK (direct student-ID entry — no session/lab picker, no question)
# =========================
@app.route("/mark", methods=["GET"])
def mark():
    if not is_on_campus_network():
        return "<h2>🚫 Please connect to the classroom Wi-Fi to mark attendance.</h2>"

    token = request.args.get("token")
    status = validate_and_track_token(token, allow_start=True)
    if status == "invalid":
        return "<h2>❌ Invalid QR</h2>"
    if status == "expired":
        return "<h2>⏱ QR Expired — please rescan the code on screen.</h2>"

    elapsed = time.time() - session.get("mark_started", time.time())
    remaining_seconds = max(0, int(ANSWER_WINDOW_SECONDS - elapsed))

    available_sessions = get_available_sessions()
    if not available_sessions:
        return "<h2>⏱ No attendance session is open right now. Please check back during your class slot.</h2>"

    return render_template("mark.html",
        token=token,
        remaining_seconds=remaining_seconds,
        session_options=available_sessions,
        google_client_id=GOOGLE_OAUTH_CLIENT_ID,
        google_form_url=GOOGLE_FORM_URL
    )

# =========================
# ✅ SUBMIT (no disk/network I/O in the request path)
# =========================
@app.route("/submit", methods=["POST"])
def submit():
    if not is_on_campus_network():
        return "<h2>🚫 Please connect to the classroom Wi-Fi to mark attendance.</h2>"

    token = request.form.get("token", "").strip()
    status = validate_and_track_token(token, allow_start=False)
    if status == "invalid":
        return "<h2>❌ Invalid QR</h2>"
    if status == "expired":
        return "<h2>⏱ Time's up for this attendance window — please rescan the code and try again.</h2>"

    google_id_token_str = request.form.get("google_id_token", "").strip()
    student_id, verified_email, id_error = verify_google_identity(google_id_token_str)
    if id_error:
        return f"<h2>❌ {id_error}</h2>"

    fingerprint = request.form.get("fingerprint")
    session_choice = request.form.get("session", "").strip()
    today = now_ist().strftime("%Y-%m-%d")
    now = now_ist().strftime("%H:%M:%S")

    if session_choice not in SESSION_OPTIONS:
        return "<h2>❌ Please select a valid session.</h2>"

    # Re-check the weekday/time window here too — the dropdown on /mark
    # already hides out-of-window options, but that's client-side only.
    # Without this check a student could still POST an old `session` value
    # straight to /submit outside its allowed window.
    if not is_session_available(session_choice, now_ist()):
        return "<h2>❌ That session is not open right now.</h2>"

    category = session_category(session_choice)

    # Keying by category (not the exact session) means a student can
    # legitimately submit once per day for ANY one of the S/T options,
    # but not for two different ones on the same day.
    fp_key = f"{fingerprint}_{category}_{today}"

    # Check-only (no mutation yet) — a device that's already used its slot
    # for this category/day gets rejected here without anything further
    # happening.
    with fingerprint_lock:
        if fp_key in fingerprint_set:
            return "<h2>❌ Attendance already marked from this device today</h2>"

    done_list = session.get("attendance_done_sessions", [])
    session_marker = f"{category}_{today}"
    if session_marker in done_list:
        return "<h2>⚠️ This device has already submitted attendance today.</h2>"

    row = [student_id, today, now, session_choice]

    # single fast in-memory lock: check-and-mark is atomic, no disk reads
    with attendance_lock:
        if (student_id, today, category) in attendance_set:
            return duplicate_response()
        attendance_set.add((student_id, today, category))

    done_list.append(session_marker)
    session["attendance_done_sessions"] = done_list
    submission_queue.put(row)   # background thread handles disk + Sheets

    # Attendance has now been recorded (queued), so only now mark the
    # fingerprint as used and persist it.
    with fingerprint_lock:
        fingerprint_set.add(fp_key)
    fingerprint_write_queue.put(fp_key)  # persisted in the background, not here

    return "<h2>✅ Attendance marked. Thank you!</h2>"


def duplicate_response():
    return """<h2>⚠️ Attendance already marked</h2>"""

# =========================
# 📊 STATS (date + session breakdown)
# =========================
def build_day_stats(date_str, session_filter=None):
    """Shared by /stats (HTML view) and /stats/download (CSV export) so
    both always report the exact same numbers. session_filter, if given,
    restricts attendees/total to that one session — session_stats always
    covers all sessions for the day regardless, so the summary table stays
    a full breakdown even when a single session is selected."""
    if time.time() - student_map_last_fetch > STUDENT_MAP_CACHE_TTL:
        refresh_student_map()

    total_attended = 0
    attendees = []
    # session name -> {"total": n}
    session_stats = {}

    for row in records_cache:
        sheet_date = str(row.get("Date", "")).strip()

        if sheet_date == date_str:
            student_id = str(row.get("Student ID", "")).strip()
            info = student_map_cache.get(student_id, {})
            session_val = str(row.get("Session", "")).strip()

            stat = session_stats.setdefault(session_val, {"total": 0})
            stat["total"] += 1

            if session_filter and session_val != session_filter:
                continue

            total_attended += 1
            attendees.append({
                "student_id": student_id,
                "name": info.get("name") or "Unknown",
                "email": info.get("email", ""),
                "time": row.get("Time", ""),
                "session": session_val,
            })

    return total_attended, attendees, session_stats


@app.route("/stats", methods=["GET", "POST"])
def stats():
    try:
        _do_cache_refresh()
    except Exception as e:
        logging.error(f"Stats: on-demand cache refresh failed, showing last-known data: {e}")

    today_date = now_ist().strftime("%Y-%m-%d")
    date_selected = request.values.get("date", today_date)
    session_selected = request.values.get("session", "").strip()

    total_attended, attendees, session_stats = build_day_stats(date_selected, session_selected or None)

    return render_template(
        "stats.html",
        attended=total_attended,
        date=date_selected,
        attendees=attendees,
        session_stats=session_stats,
        session_options=SESSION_OPTIONS,
        session_selected=session_selected
    )


@app.route("/stats/download")
def download_stats_csv():
    date_str = request.args.get("date") or now_ist().strftime("%Y-%m-%d")
    session_filter = request.args.get("session", "").strip() or None

    _, attendees, _ = build_day_stats(date_str, session_filter)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Student ID", "Name", "Email", "Session", "Time"])
    for a in attendees:
        writer.writerow([a["student_id"], a["name"], a["email"], a["session"], a["time"]])

    csv_data = buf.getvalue()
    filename = f"attendance_{date_str}.csv"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )




# =========================
# 🚀 RUN
# =========================
if __name__ == "__main__":
    HOST = "0.0.0.0"
    PORT = int(os.environ.get("PORT") or os.environ.get("APP_PORT", "8000"))

    # Flask's built-in dev server is single-process and not meant to hold
    # up under ~200 near-simultaneous requests reliably (connection queuing,
    # dropped keep-alives, etc). waitress is a pure-Python production WSGI
    # server that works out of the box on Windows (unlike gunicorn) and
    # handles bursts like this comfortably with a modest thread pool.
    #
    # Install once with:  pip install waitress
    try:
        from waitress import serve
        WAITRESS_THREADS = int(os.environ.get("WAITRESS_THREADS", "48"))
        logging.info(f"Starting with waitress on {HOST}:{PORT} (threads={WAITRESS_THREADS})")
        serve(app, host=HOST, port=PORT, threads=WAITRESS_THREADS)
    except ImportError:
        logging.warning(
            "waitress is not installed — falling back to Flask's dev server, "
            "which is NOT recommended for ~200 simultaneous scans. "
            "Run: pip install waitress"
        )
        app.run(host=HOST, port=PORT, threaded=True, debug=False)
