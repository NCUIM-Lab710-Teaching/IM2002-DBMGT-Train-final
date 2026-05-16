"""
TransitFlow — PostgreSQL / Relational Database Layer
=====================================================
This module handles all queries to PostgreSQL.

TWO ROLES ARE SERVED HERE:
  1. Relational  → dual-network transit (metro + national rail),
                   availability, fares, bookings, seat selection
  2. Vector      → policy document similarity search (pgvector)
"""

from __future__ import annotations

import json
import random
import string
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from skeleton.config import PG_DSN, VECTOR_TOP_K, VECTOR_SIMILARITY_THRESHOLD

# 設定標準 logging 模組
logger = logging.getLogger("relational_queries")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

ph = PasswordHasher()


def _connect():
    """Return a new psycopg2 connection with autocommit enabled."""
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def _gen_booking_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"BK-{suffix}"


def _gen_payment_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"PM-{suffix}"


# ── NATIONAL RAIL AVAILABILITY ────────────────────────────────────────────────

def query_national_rail_availability(
    origin_id: str,
    destination_id: str,
    travel_date: Optional[str] = None,
) -> list[dict]:
    """
    Return national rail schedules that serve both origin and destination stations
    in the correct order.
    """
    sql = """
        SELECT
            sch.schedule_id,
            sch.line,
            sch.service_type,
            sch.direction,
            sch.first_train_time::text,
            sch.last_train_time::text,
            sch.frequency_min,
            orig.stop_order AS origin_stop_order,
            dest.stop_order AS destination_stop_order
        FROM national_rail_schedules sch
        JOIN national_rail_schedule_stops orig ON sch.schedule_id = orig.schedule_id
        JOIN national_rail_schedule_stops dest ON sch.schedule_id = dest.schedule_id
        WHERE orig.station_id = %s
          AND dest.station_id = %s
          AND orig.stop_order < dest.stop_order
    """
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (origin_id, destination_id))
                return [dict(row) for row in cur.fetchall()]
    except psycopg2.Error as e:
        logger.error(f"DB Error in query_national_rail_availability: {e}")
        return []


def query_national_rail_fare(
    schedule_id: str,
    fare_class: str,
    stops_travelled: int,
) -> Optional[dict]:
    """Calculate the fare for a national rail journey."""
    sql = """
        SELECT
            fare_class,
            base_fare_usd,
            per_stop_rate_usd
        FROM national_rail_fare_classes
        WHERE schedule_id = %s AND fare_class = %s
    """
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (schedule_id, fare_class))
                row = cur.fetchone()
                if row:
                    base = float(row['base_fare_usd'])
                    rate = float(row['per_stop_rate_usd'])
                    total = base + (stops_travelled * rate)
                    row_dict = dict(row)
                    row_dict['base_fare_usd'] = base
                    row_dict['per_stop_rate_usd'] = rate
                    row_dict['total_fare_usd'] = round(total, 2)
                    return row_dict
                return None
    except psycopg2.Error as e:
        logger.error(f"DB Error in query_national_rail_fare: {e}")
        return None


# ── METRO SCHEDULES & FARE ────────────────────────────────────────────────────

def query_metro_schedules(origin_id: str, destination_id: str) -> list[dict]:
    """Return metro schedules that serve both origin and destination in the correct order."""
    sql = """
        SELECT
            sch.schedule_id,
            sch.line,
            sch.direction,
            sch.first_train_time::text,
            sch.last_train_time::text,
            sch.base_fare_usd,
            sch.per_stop_rate_usd,
            sch.frequency_min,
            orig.stop_order AS origin_stop_order,
            dest.stop_order AS destination_stop_order
        FROM metro_schedules sch
        JOIN metro_schedule_stops orig ON sch.schedule_id = orig.schedule_id
        JOIN metro_schedule_stops dest ON sch.schedule_id = dest.schedule_id
        WHERE orig.station_id = %s
          AND dest.station_id = %s
          AND orig.stop_order < dest.stop_order
    """
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (origin_id, destination_id))
                return [dict(row) for row in cur.fetchall()]
    except psycopg2.Error as e:
        logger.error(f"DB Error in query_metro_schedules: {e}")
        return []


def query_metro_fare(schedule_id: str, stops_travelled: int) -> Optional[dict]:
    """Calculate the metro fare for a single-ticket journey."""
    sql = """
        SELECT
            base_fare_usd,
            per_stop_rate_usd
        FROM metro_schedules
        WHERE schedule_id = %s
    """
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (schedule_id,))
                row = cur.fetchone()
                if row:
                    base = float(row['base_fare_usd'])
                    rate = float(row['per_stop_rate_usd'])
                    total = base + (stops_travelled * rate)
                    row_dict = dict(row)
                    row_dict['base_fare_usd'] = base
                    row_dict['per_stop_rate_usd'] = rate
                    row_dict['total_fare_usd'] = round(total, 2)
                    return row_dict
                return None
    except psycopg2.Error as e:
        logger.error(f"DB Error in query_metro_fare: {e}")
        return None


# ── SEAT SELECTION ────────────────────────────────────────────────────────────

def query_available_seats(
    schedule_id: str,
    travel_date: str,
    fare_class: str,
    origin_station_id: str = None,
    destination_station_id: str = None
) -> list[dict]:
    """
    Return available seats using collision test logic (區間碰撞測試).
    數學碰撞條件：已售出區間的起點 < 查詢區間的終點 AND 已售出區間的終點 > 查詢區間的起點
    """
    if not origin_station_id or not destination_station_id:
        logger.warning("query_available_seats called without origin/destination, falling back to full trip block.")
        # Fallback query without origin/dest: assumes if a seat is booked at all on that date, it's unavailable.
        sql_fallback = """
            SELECT s.seat_id, s.coach, s.row, s.col as column
            FROM seats s
            JOIN coaches c ON s.layout_id = c.layout_id AND s.coach = c.coach
            JOIN seat_layouts sl ON s.layout_id = sl.layout_id
            WHERE sl.schedule_id = %s
              AND c.fare_class = %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM national_rail_bookings b
                  WHERE b.schedule_id = sl.schedule_id
                    AND b.travel_date = %s::date
                    AND b.seat_id = s.seat_id
                    AND b.coach = s.coach
                    AND b.status IN ('confirmed', 'completed')
              )
        """
        try:
            with _connect() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(sql_fallback, (schedule_id, fare_class, travel_date))
                    return [dict(row) for row in cur.fetchall()]
        except psycopg2.Error as e:
            logger.error(f"DB Error in fallback query_available_seats: {e}")
            return []

    # Accurate collision logic
    sql = """
        WITH search_route AS (
            SELECT 
                orig.stop_order AS search_orig_order,
                dest.stop_order AS search_dest_order
            FROM national_rail_schedule_stops orig
            JOIN national_rail_schedule_stops dest ON orig.schedule_id = dest.schedule_id
            WHERE orig.schedule_id = %s
              AND orig.station_id = %s
              AND dest.station_id = %s
        )
        SELECT s.seat_id, s.coach, s.row, s.col as column
        FROM seats s
        JOIN coaches c ON s.layout_id = c.layout_id AND s.coach = c.coach
        JOIN seat_layouts sl ON s.layout_id = sl.layout_id
        WHERE sl.schedule_id = %s
          AND c.fare_class = %s
          AND NOT EXISTS (
              SELECT 1
              FROM national_rail_bookings b
              JOIN national_rail_schedule_stops b_orig ON b.schedule_id = b_orig.schedule_id AND b.origin_station_id = b_orig.station_id
              JOIN national_rail_schedule_stops b_dest ON b.schedule_id = b_dest.schedule_id AND b.destination_station_id = b_dest.station_id
              CROSS JOIN search_route sr
              WHERE b.schedule_id = sl.schedule_id
                AND b.travel_date = %s::date
                AND b.seat_id = s.seat_id
                AND b.coach = s.coach
                AND b.status IN ('confirmed', 'completed')
                -- 碰撞條件：已售出區間的起點 < 查詢區間的終點 AND 已售出區間的終點 > 查詢區間的起點
                AND b_orig.stop_order < sr.search_dest_order
                AND b_dest.stop_order > sr.search_orig_order
          )
    """
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (schedule_id, origin_station_id, destination_station_id, schedule_id, fare_class, travel_date))
                return [dict(row) for row in cur.fetchall()]
    except psycopg2.Error as e:
        logger.error(f"DB Error in query_available_seats: {e}")
        return []


def auto_select_adjacent_seats(available_seats: list[dict], count: int) -> list[str]:
    """Select `count` seats that are as close together as possible (same row preferred)."""
    if not available_seats or count <= 0:
        return []
    if count >= len(available_seats):
        return [s["seat_id"] for s in available_seats[:count]]

    from collections import defaultdict
    rows: dict[int, list[dict]] = defaultdict(list)
    for seat in available_seats:
        rows[seat["row"]].append(seat)

    for row_seats in sorted(rows.values(), key=lambda s: s[0]["row"]):
        if len(row_seats) >= count:
            sorted_by_col = sorted(row_seats, key=lambda s: s["column"])
            return [s["seat_id"] for s in sorted_by_col[:count]]

    sorted_seats = sorted(available_seats, key=lambda s: (s["row"], s["column"]))
    return [s["seat_id"] for s in sorted_seats[:count]]


# ── USER & BOOKING QUERIES ────────────────────────────────────────────────────

def query_user_profile(user_email: str) -> Optional[dict]:
    """Return a user's profile by email."""
    sql = """
        SELECT user_id, legacy_id, first_name, surname, full_name, email, phone, date_of_birth::text, registered_at::text, is_active
        FROM users
        WHERE email = %s
    """
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (user_email,))
                row = cur.fetchone()
                if row:
                    row["user_id"] = str(row["user_id"])
                    return dict(row)
                return None
    except psycopg2.Error as e:
        logger.error(f"DB Error in query_user_profile: {e}")
        return None


def query_user_bookings(user_email: str) -> dict:
    """
    Return a user's combined booking history (national rail + metro).
    聚合成樹狀結構：主檔 (狀態、總金額) -> 票券明細
    """
    user = query_user_profile(user_email)
    if not user:
        return {"error": "User not found"}
    
    user_id = user["user_id"]
    
    nr_sql = """
        SELECT 
            b.booking_id,
            b.travel_date::text,
            b.departure_time::text,
            b.ticket_type,
            b.fare_class,
            b.coach,
            b.seat_id,
            b.amount_usd,
            b.status,
            b.booked_at::text,
            orig.name AS origin_name,
            dest.name AS destination_name,
            p.payment_id,
            p.status AS payment_status,
            p.paid_at::text
        FROM national_rail_bookings b
        JOIN national_rail_stations orig ON b.origin_station_id = orig.station_id
        JOIN national_rail_stations dest ON b.destination_station_id = dest.station_id
        LEFT JOIN payments p ON b.booking_id = p.booking_ref AND p.booking_type = 'rail'
        WHERE b.user_id = %s
        ORDER BY b.booked_at DESC
    """
    
    metro_sql = """
        SELECT 
            m.trip_id,
            m.travel_date::text,
            m.ticket_type,
            m.amount_usd,
            m.status,
            m.purchased_at::text,
            orig.name AS origin_name,
            dest.name AS destination_name,
            p.payment_id,
            p.status AS payment_status,
            p.paid_at::text
        FROM metro_travels m
        JOIN metro_stations orig ON m.origin_station_id = orig.station_id
        JOIN metro_stations dest ON m.destination_station_id = dest.station_id
        LEFT JOIN payments p ON m.trip_id = p.booking_ref AND p.booking_type = 'metro'
        WHERE m.user_id = %s
        ORDER BY m.purchased_at DESC
    """
    
    result = {"national_rail": [], "metro": []}
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(nr_sql, (user_id,))
                for row in cur.fetchall():
                    node = {
                        "booking_id": row["booking_id"],
                        "total_amount_usd": float(row["amount_usd"]),
                        "status": row["status"],
                        "booked_at": row["booked_at"],
                        "payment": {
                            "payment_id": row["payment_id"],
                            "payment_status": row["payment_status"],
                            "paid_at": row["paid_at"]
                        },
                        "ticket_details": {
                            "travel_date": row["travel_date"],
                            "departure_time": row["departure_time"],
                            "origin": row["origin_name"],
                            "destination": row["destination_name"],
                            "ticket_type": row["ticket_type"],
                            "fare_class": row["fare_class"],
                            "coach": row["coach"],
                            "seat_id": row["seat_id"]
                        }
                    }
                    result["national_rail"].append(node)
                    
                cur.execute(metro_sql, (user_id,))
                for row in cur.fetchall():
                    node = {
                        "trip_id": row["trip_id"],
                        "total_amount_usd": float(row["amount_usd"]),
                        "status": row["status"],
                        "purchased_at": row["purchased_at"],
                        "payment": {
                            "payment_id": row["payment_id"],
                            "payment_status": row["payment_status"],
                            "paid_at": row["paid_at"]
                        },
                        "ticket_details": {
                            "travel_date": row["travel_date"],
                            "origin": row["origin_name"],
                            "destination": row["destination_name"],
                            "ticket_type": row["ticket_type"]
                        }
                    }
                    result["metro"].append(node)
        return result
    except psycopg2.Error as e:
        logger.error(f"DB Error in query_user_bookings: {e}")
        return result


def query_payment_info(booking_id: str) -> Optional[dict]:
    """Return payment record for a booking or metro trip."""
    sql = "SELECT payment_id, booking_ref, booking_type, amount_usd, method, status, paid_at::text FROM payments WHERE booking_ref = %s"
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (booking_id,))
                row = cur.fetchone()
                if row:
                    row["amount_usd"] = float(row["amount_usd"])
                    return dict(row)
                return None
    except psycopg2.Error as e:
        logger.error(f"DB Error in query_payment_info: {e}")
        return None


# ── TRANSACTIONAL OPERATIONS ──────────────────────────────────────────────────

def execute_booking(
    user_id: str,
    schedule_id: str,
    origin_station_id: str,
    destination_station_id: str,
    travel_date: str,
    fare_class: str,
    seat_id: str,
    ticket_type: str = "single",
) -> tuple[bool, dict | str]:
    """
    Create a national rail booking for a logged-in user.
    高併發防護：使用 SELECT ... FOR UPDATE 悲觀鎖定座位，確保碰撞檢查正確無超賣。
    """
    try:
        avail_sql = """
            SELECT orig.stop_order AS o_order, dest.stop_order AS d_order
            FROM national_rail_schedule_stops orig
            JOIN national_rail_schedule_stops dest ON orig.schedule_id = dest.schedule_id
            WHERE orig.schedule_id = %s AND orig.station_id = %s AND dest.station_id = %s
        """
        
        with _connect() as conn:
            # 必須明確關閉 autocommit 以建立 Transaction
            conn.autocommit = False
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(avail_sql, (schedule_id, origin_station_id, destination_station_id))
                    stops_res = cur.fetchone()
                    if not stops_res or stops_res['o_order'] >= stops_res['d_order']:
                        return False, "Invalid origin or destination for this schedule."
                    
                    o_order = stops_res['o_order']
                    d_order = stops_res['d_order']
                    stops_travelled = d_order - o_order
                    
                    fare_sql = "SELECT base_fare_usd, per_stop_rate_usd FROM national_rail_fare_classes WHERE schedule_id = %s AND fare_class = %s"
                    cur.execute(fare_sql, (schedule_id, fare_class))
                    fare_res = cur.fetchone()
                    if not fare_res:
                        return False, "Invalid fare class for this schedule."
                    
                    amount_usd = float(fare_res['base_fare_usd']) + (stops_travelled * float(fare_res['per_stop_rate_usd']))
                    
                    time_sql = "SELECT first_train_time FROM national_rail_schedules WHERE schedule_id = %s"
                    cur.execute(time_sql, (schedule_id,))
                    departure_time = cur.fetchone()['first_train_time']
                    
                    target_seat_id = seat_id
                    target_coach = ""
                    
                    if seat_id.lower() == 'any':
                        avail_seats = query_available_seats(schedule_id, travel_date, fare_class, origin_station_id, destination_station_id)
                        if not avail_seats:
                            return False, "No seats available."
                        target_seat_id = avail_seats[0]['seat_id']
                        target_coach = avail_seats[0]['coach']
                    else:
                        coach_sql = "SELECT coach FROM seats WHERE seat_id = %s"
                        cur.execute(coach_sql, (target_seat_id,))
                        c_res = cur.fetchone()
                        if not c_res:
                            return False, "Invalid seat_id."
                        target_coach = c_res['coach']
                        
                    # 【悲觀鎖】鎖定目標座位紀錄
                    lock_sql = """
                        SELECT 1 FROM seats s
                        JOIN seat_layouts sl ON s.layout_id = sl.layout_id
                        WHERE sl.schedule_id = %s AND s.seat_id = %s
                        FOR UPDATE
                    """
                    cur.execute(lock_sql, (schedule_id, target_seat_id))
                    
                    # 【雙重檢查】碰撞測試
                    check_overlap_sql = """
                        SELECT 1
                        FROM national_rail_bookings b
                        JOIN national_rail_schedule_stops b_orig ON b.schedule_id = b_orig.schedule_id AND b.origin_station_id = b_orig.station_id
                        JOIN national_rail_schedule_stops b_dest ON b.schedule_id = b_dest.schedule_id AND b.destination_station_id = b_dest.station_id
                        WHERE b.schedule_id = %s
                          AND b.travel_date = %s::date
                          AND b.seat_id = %s
                          AND b.status IN ('confirmed', 'completed')
                          AND b_orig.stop_order < %s
                          AND b_dest.stop_order > %s
                    """
                    cur.execute(check_overlap_sql, (schedule_id, travel_date, target_seat_id, d_order, o_order))
                    if cur.fetchone():
                        conn.rollback()
                        return False, "Seat was just taken by another user during checkout."
                    
                    # 寫入訂單
                    booking_id = _gen_booking_id()
                    insert_b_sql = """
                        INSERT INTO national_rail_bookings (
                            booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
                            travel_date, departure_time, ticket_type, fare_class, coach, seat_id,
                            stops_travelled, amount_usd, status
                        ) VALUES (%s, %s, %s, %s, %s, %s::date, %s, %s, %s, %s, %s, %s, %s, 'confirmed')
                    """
                    cur.execute(insert_b_sql, (
                        booking_id, user_id, schedule_id, origin_station_id, destination_station_id,
                        travel_date, departure_time, ticket_type, fare_class, target_coach, target_seat_id,
                        stops_travelled, amount_usd
                    ))
                    
                    # 寫入付款紀錄
                    payment_id = _gen_payment_id()
                    insert_p_sql = """
                        INSERT INTO payments (payment_id, booking_ref, booking_type, amount_usd, method, status)
                        VALUES (%s, %s, 'rail', %s, 'credit_card', 'paid')
                    """
                    cur.execute(insert_p_sql, (payment_id, booking_id, amount_usd))
                    
                    conn.commit()
                    
                    return True, {
                        "booking_id": booking_id,
                        "payment_id": payment_id,
                        "seat_id": target_seat_id,
                        "amount_usd": round(amount_usd, 2)
                    }
            except psycopg2.Error as e:
                conn.rollback()
                logger.error(f"DB Transaction Error in execute_booking: {e}")
                return False, f"Database error: {str(e)}"
    except Exception as e:
        logger.error(f"Error in execute_booking: {e}")
        return False, str(e)


def execute_cancellation(booking_id: str, user_id: str) -> tuple[bool, dict | str]:
    """
    Cancel a national rail booking owned by the given user.
    依據時間差動態計算退款政策 (RF001 / RF002).
    """
    sql_check = """
        SELECT b.status, b.travel_date, b.departure_time, b.amount_usd, s.service_type
        FROM national_rail_bookings b
        JOIN national_rail_schedules s ON b.schedule_id = s.schedule_id
        WHERE b.booking_id = %s AND b.user_id = %s
    """
    try:
        with _connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # FOR UPDATE 鎖定這筆訂單避免 race condition
                    cur.execute(sql_check + " FOR UPDATE OF b", (booking_id, user_id))
                    row = cur.fetchone()
                    if not row:
                        conn.rollback()
                        return False, "Booking not found or does not belong to user."
                    
                    if row['status'] != 'confirmed':
                        conn.rollback()
                        return False, f"Cannot cancel booking with status: {row['status']}"
                    
                    travel_date = row['travel_date']
                    departure_time = row['departure_time']
                    service_type = row['service_type']
                    amount = float(row['amount_usd'])
                    
                    # 以 UTC 為基準計算時間差
                    dep_dt = datetime.combine(travel_date, departure_time).replace(tzinfo=timezone.utc)
                    now_dt = datetime.now(timezone.utc)
                    
                    diff = (dep_dt - now_dt).total_seconds() / 3600.0
                    
                    refund_pct = 0.0
                    fee = 0.0
                    
                    if service_type == 'normal':
                        if diff >= 48:
                            refund_pct = 1.0
                            fee = 0.0
                        elif 24 <= diff < 48:
                            refund_pct = 0.75
                            fee = 0.50
                        elif 2 <= diff < 24:
                            refund_pct = 0.50
                            fee = 0.50
                    elif service_type == 'express':
                        if diff >= 48:
                            refund_pct = 1.0
                            fee = 1.0
                        elif 24 <= diff < 48:
                            refund_pct = 0.50
                            fee = 1.0
                            
                    refund_amount = (amount * refund_pct) - fee
                    if refund_amount < 0:
                        refund_amount = 0.0
                        
                    # 更新為 cancelled
                    upd_sql = "UPDATE national_rail_bookings SET status = 'cancelled' WHERE booking_id = %s"
                    cur.execute(upd_sql, (booking_id,))
                    
                    # 建立退款明細
                    if refund_amount > 0:
                        payment_id = _gen_payment_id()
                        ins_pay_sql = """
                            INSERT INTO payments (payment_id, booking_ref, booking_type, amount_usd, method, status)
                            VALUES (%s, %s, 'rail', %s, 'credit_card', 'refunded')
                        """
                        cur.execute(ins_pay_sql, (payment_id, booking_id, refund_amount))
                        
                    conn.commit()
                    
                    return True, {
                        "refund_amount_usd": round(refund_amount, 2),
                        "fee_deducted_usd": round(fee, 2),
                        "note": f"Cancelled with {refund_pct*100}% policy. Hours to departure: {round(diff, 1)}"
                    }
            except psycopg2.Error as e:
                conn.rollback()
                logger.error(f"DB Transaction Error in execute_cancellation: {e}")
                return False, f"Database error: {str(e)}"
    except Exception as e:
        logger.error(f"Error in execute_cancellation: {e}")
        return False, str(e)


# ── AUTHENTICATION QUERIES ────────────────────────────────────────────────────

def register_user(
    email: str,
    first_name: str,
    surname: str,
    year_of_birth: int,
    password: str,
    secret_question: str,
    secret_answer: str,
) -> tuple[bool, str]:
    """Register a new user using argon2."""
    try:
        pw_hash = ph.hash(password)
        ans_hash = ph.hash(secret_answer.lower().strip())
        
        with _connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    ins_u_sql = """
                        INSERT INTO users (first_name, surname, email, date_of_birth)
                        VALUES (%s, %s, %s, %s)
                        RETURNING user_id
                    """
                    dob = f"{year_of_birth}-01-01"
                    cur.execute(ins_u_sql, (first_name, surname, email, dob))
                    user_id = cur.fetchone()[0]
                    
                    ins_c_sql = """
                        INSERT INTO user_credentials (user_id, password_hash, secret_question, secret_answer_hash)
                        VALUES (%s, %s, %s, %s)
                    """
                    cur.execute(ins_c_sql, (user_id, pw_hash, secret_question, ans_hash))
                    
                    conn.commit()
                    return True, str(user_id)
            except psycopg2.IntegrityError:
                conn.rollback()
                return False, "Email already registered."
            except psycopg2.Error as e:
                conn.rollback()
                logger.error(f"DB Error in register_user: {e}")
                return False, "Database error during registration."
    except Exception as e:
        logger.error(f"Error in register_user: {e}")
        return False, str(e)


def login_user(email: str, password: str) -> Optional[dict]:
    """Verify credentials via argon2."""
    sql = """
        SELECT u.user_id, u.email, u.full_name, u.first_name, u.surname, u.phone, u.date_of_birth::text, u.is_active, c.password_hash
        FROM users u
        JOIN user_credentials c ON u.user_id = c.user_id
        WHERE u.email = %s
    """
    try:
        with _connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (email,))
                row = cur.fetchone()
                if row:
                    try:
                        if ph.verify(row['password_hash'], password):
                            del row['password_hash']
                            row['user_id'] = str(row['user_id'])
                            return dict(row)
                    except VerifyMismatchError:
                        return None
                return None
    except psycopg2.Error as e:
        logger.error(f"DB Error in login_user: {e}")
        return None


def get_user_secret_question(email: str) -> Optional[str]:
    """Return the secret question for a registered email."""
    sql = """
        SELECT c.secret_question
        FROM users u
        JOIN user_credentials c ON u.user_id = c.user_id
        WHERE u.email = %s
    """
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (email,))
                row = cur.fetchone()
                if row:
                    return row[0]
                return None
    except psycopg2.Error as e:
        logger.error(f"DB Error in get_user_secret_question: {e}")
        return None


def verify_secret_answer(email: str, answer: str) -> bool:
    """Return True if the provided answer matches the stored argon2 hash."""
    sql = """
        SELECT c.secret_answer_hash
        FROM users u
        JOIN user_credentials c ON u.user_id = c.user_id
        WHERE u.email = %s
    """
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (email,))
                row = cur.fetchone()
                if row:
                    try:
                        return ph.verify(row[0], answer.lower().strip())
                    except VerifyMismatchError:
                        return False
                return False
    except psycopg2.Error as e:
        logger.error(f"DB Error in verify_secret_answer: {e}")
        return False


def update_password(email: str, new_password: str) -> bool:
    """Update the password for a user using argon2."""
    try:
        pw_hash = ph.hash(new_password)
        sql = """
            UPDATE user_credentials
            SET password_hash = %s, credentials_updated_at = NOW()
            WHERE user_id = (SELECT user_id FROM users WHERE email = %s)
        """
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (pw_hash, email))
                return cur.rowcount > 0
    except psycopg2.Error as e:
        logger.error(f"DB Error in update_password: {e}")
        return False
    except Exception as e:
        logger.error(f"Error in update_password: {e}")
        return False


# ── VECTOR / RAG QUERIES — do not modify ─────────────────────────────────────

def query_policy_vector_search(embedding: list[float], top_k: int = VECTOR_TOP_K) -> list[dict]:
    """Find the most relevant policy documents for a given query embedding."""
    sql = """
        SELECT
            title,
            category,
            content,
            1 - (embedding <=> %s::vector) AS similarity
        FROM policy_documents
        WHERE 1 - (embedding <=> %s::vector) > %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (vec_str, vec_str, VECTOR_SIMILARITY_THRESHOLD, vec_str, top_k))
            return [dict(row) for row in cur.fetchall()]


def store_policy_document(
    title: str,
    category: str,
    content: str,
    embedding: list[float],
    source_file: str = "",
) -> int:
    """Insert a policy document with its embedding into the database."""
    sql = """
        INSERT INTO policy_documents (title, category, content, embedding, source_file)
        VALUES (%s, %s, %s, %s::vector, %s)
        RETURNING id
    """
    vec_str = "[" + ",".join(str(x) for x in embedding) + "]"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (title, category, content, vec_str, source_file))
            return cur.fetchone()[0]
