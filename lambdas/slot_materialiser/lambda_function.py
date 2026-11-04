
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from common import config, dbconn, response


logger = logging.getLogger()
logger.setLevel(logging.INFO)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def as_time(value):
    """Convert MySQL TIME values to Python time objects."""

    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())

        return time(
            (seconds // 3600) % 24,
            (seconds % 3600) // 60,
            seconds % 60
        )

    return value


def ist_to_utc(local_dt):
    """Convert IST datetime to UTC-naive for MySQL storage."""

    return (
        local_dt
        .replace(tzinfo=IST)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )


def utc_to_ist(utc_dt):
    """Convert UTC-naive MySQL datetime to IST."""

    return (
        utc_dt
        .replace(tzinfo=UTC)
        .astimezone(IST)
        .replace(tzinfo=None)
    )
def localize_slot(local_dt, tz):
    """
    Convert a local datetime to UTC-naive.

    Skip nonexistent DST times and use the first occurrence
    for ambiguous DST times.
    """
    valid_utc_times = []

    for fold in (0, 1):
        aware_local = local_dt.replace(tzinfo=tz, fold=fold)
        utc_dt = aware_local.astimezone(UTC)

        round_trip = utc_dt.astimezone(tz).replace(tzinfo=None)

        if round_trip == local_dt:
            utc_naive = utc_dt.replace(tzinfo=None)

            if utc_naive not in valid_utc_times:
                valid_utc_times.append(utc_naive)

    if not valid_utc_times:
        return None

    return min(valid_utc_times)

def intervals_overlap(start1, end1, start2, end2):
    """Return True when two time intervals overlap."""

    return start1 < end2 and end1 > start2


def handler(event, context):

    logger.info("Slot Materialisation Lambda started")

    logger.info(
        "Request ID: %s",
        getattr(context, "aws_request_id", "unknown")
    )

    conn = None

    try:
        event = event or {}
        days_ahead = int(event.get("days_ahead", 30))

        if not 1 <= days_ahead <= 90:
            return response(400, {
                "message": "days_ahead must be between 1 and 90"
            })

        now_ist = datetime.now(IST)
        today = now_ist.date()
        end_date = today + timedelta(days=days_ahead)
        current_ist = now_ist.replace(tzinfo=None)

        logger.info(
            "Generating draft slots from %s to %s",
            today,
            end_date
        )

        conn = dbconn(config())

        created = 0
        skipped_blackout = 0
        skipped_no_types = 0
        skipped_existing_schedule = 0
        skipped_invalid_hours = 0
        skipped_invalid_duration = 0
        skipped_overlap = 0
        skipped_lock = 0

        with conn.cursor() as cur:

            # -------------------------------------------------
            # 1. Fetch active doctor availability patterns
            # -------------------------------------------------

            cur.execute(
                """
                SELECT
                    doctor_id,
                    day_of_week,
                    start_time,
                    end_time
                FROM availability_patterns
                WHERE is_active = TRUE
                ORDER BY doctor_id, day_of_week, start_time
                """
            )

            patterns = cur.fetchall()

            logger.info(
                "Active availability patterns: %s",
                len(patterns)
            )

            # -------------------------------------------------
            # 2. Fetch consultation types selected by doctors
            #
            # is_active = TRUE in doctor_consultation_fees
            # means the doctor has selected that type.
            # -------------------------------------------------

            cur.execute(
                """
                SELECT
                    ct.consultation_type_id,
                    ct.name,
                    ct.duration_minutes,
                    dcf.doctor_id
                FROM consultation_catalogue ct
                INNER JOIN doctor_consultation_fees dcf
                    ON ct.consultation_type_id =
                       dcf.consultation_type_id
                WHERE ct.is_active = TRUE
                  AND dcf.is_active = TRUE
                ORDER BY
                    dcf.doctor_id,
                    ct.consultation_type_id
                """
            )

            consultation_rows = cur.fetchall()

            doctor_types = defaultdict(list)

            for row in consultation_rows:

                duration = int(row["duration_minutes"])

                if not 5 <= duration <= 480:
                    skipped_invalid_duration += 1

                    logger.warning(
                        "Invalid duration: doctor=%s, type=%s",
                        row["doctor_id"],
                        row["consultation_type_id"]
                    )
                    continue

                doctor_id = int(row["doctor_id"])

                doctor_types[doctor_id].append({
                    "consultation_type_id": int(
                        row["consultation_type_id"]
                    ),
                    "name": row["name"],
                    "duration": duration
                })

            logger.info(
                "Active doctor consultation selections: %s",
                len(consultation_rows)
            )

            # -------------------------------------------------
            # 3. Fetch blackout dates
            # -------------------------------------------------

            cur.execute(
                """
                SELECT doctor_id, blackout_date
                FROM doctor_blackout_dates
                WHERE blackout_date BETWEEN %s AND %s
                """,
                (today, end_date)
            )

            blackout_rows = cur.fetchall()

            blackouts = {
                (int(row["doctor_id"]), row["blackout_date"])
                for row in blackout_rows
            }

            logger.info(
                "Blackout dates fetched: %s",
                len(blackouts)
            )

            # -------------------------------------------------
            # 4. Group availability by doctor and weekday
            # -------------------------------------------------

            doctor_patterns = defaultdict(list)

            for pattern in patterns:

                key = (
                    int(pattern["doctor_id"]),
                    int(pattern["day_of_week"])
                )

                doctor_patterns[key].append(pattern)

            # -------------------------------------------------
            # 5. Generate draft slots
            # -------------------------------------------------

            for (doctor_id, weekday), day_patterns in (
                doctor_patterns.items()
            ):

                selected_types = doctor_types.get(doctor_id, [])

                if not selected_types:
                    skipped_no_types += 1

                    logger.info(
                        "Doctor %s has no active consultation types",
                        doctor_id
                    )
                    continue

                current_date = today

                while current_date <= end_date:

                    if current_date.weekday() != weekday:
                        current_date += timedelta(days=1)
                        continue

                    # Skip blackout dates.
                    if (doctor_id, current_date) in blackouts:
                        skipped_blackout += 1

                        current_date += timedelta(days=1)
                        continue

                    # -------------------------------------------------
                    # 6. Acquire a doctor/date generation lock.
                    # This serializes concurrent materialiser runs.
                    # -------------------------------------------------

                    lock_name = (
                        f"ms_{doctor_id}_{current_date.strftime('%Y%m%d')}"
                    )

                    cur.execute(
                        "SELECT GET_LOCK(%s, 10) AS lock_status",
                        (lock_name,)
                    )

                    lock_result = cur.fetchone()

                    if not lock_result or lock_result["lock_status"] != 1:
                        skipped_lock += 1

                        logger.warning(
                            "Could not acquire generation lock: %s",
                            lock_name
                        )

                        current_date += timedelta(days=1)
                        continue

                    try:

                        day_start_utc = ist_to_utc(
                            datetime.combine(current_date, time.min)
                        )

                        day_end_utc = ist_to_utc(
                            datetime.combine(
                                current_date + timedelta(days=1),
                                time.min
                            )
                        )

                        # -------------------------------------------------
                        # 7. Preserve existing schedules.
                        #
                        # If any slot already exists for this doctor/date,
                        # do not generate more slots for that date.
                        # This protects manually edited draft slots and
                        # already published/booked slots.
                        # -------------------------------------------------

                        cur.execute(
                            """
                            SELECT slot_id
                            FROM appointment_slots
                            WHERE doctor_id = %s
                              AND slot_start < %s
                              AND slot_end > %s
                            LIMIT 1
                            """,
                            (
                                doctor_id,
                                day_end_utc,
                                day_start_utc
                            )
                        )

                        if cur.fetchone():

                            skipped_existing_schedule += 1

                            logger.info(
                                "Existing schedule preserved: "
                                "doctor=%s, date=%s",
                                doctor_id,
                                current_date
                            )

                            current_date += timedelta(days=1)
                            continue

                        # No slots exist for this doctor/date.
                        occupied = []
                        display_order = 1
                        type_index = 0
                        day_created = 0

                        # -------------------------------------------------
                        # 8. Process each availability window
                        # -------------------------------------------------

                        for pattern in day_patterns:

                            start_value = as_time(
                                pattern["start_time"]
                            )

                            end_value = as_time(
                                pattern["end_time"]
                            )

                            if start_value >= end_value:

                                skipped_invalid_hours += 1

                                logger.warning(
                                    "Invalid availability: doctor=%s",
                                    doctor_id
                                )
                                continue

                            window_start = datetime.combine(
                                current_date,
                                start_value
                            )

                            window_end = datetime.combine(
                                current_date,
                                end_value
                            )

                            cursor = max(window_start, current_ist)

                            while cursor < window_end:

                                # -----------------------------------------
                                # Find an occupied interval covering cursor
                                # -----------------------------------------

                                covering = [
                                    end
                                    for start, end in occupied
                                    if start <= cursor < end
                                ]

                                if covering:
                                    cursor = max(covering)
                                    continue

                                # -----------------------------------------
                                # Find the next occupied interval
                                # -----------------------------------------

                                next_occupied = [
                                    start
                                    for start, end in occupied
                                    if start > cursor
                                ]

                                next_occupied_start = (
                                    min(next_occupied)
                                    if next_occupied
                                    else window_end
                                )

                                # -----------------------------------------
                                # Select a consultation type that fits.
                                #
                                # Selected types are cycled in catalogue
                                # order. The doctor can later rearrange
                                # or change the generated draft slots.
                                # -----------------------------------------

                                selected_type = None
                                slot_end = None

                                available_until = min(
                                    window_end,
                                    next_occupied_start
                                )

                                for offset in range(len(selected_types)):

                                    index = (
                                        type_index + offset
                                    ) % len(selected_types)

                                    candidate = selected_types[index]

                                    candidate_end = (
                                        cursor
                                        + timedelta(
                                            minutes=candidate["duration"]
                                        )
                                    )

                                    if candidate_end <= available_until:
                                        selected_type = candidate
                                        slot_end = candidate_end

                                        type_index = (
                                            index + 1
                                        ) % len(selected_types)

                                        break

                                if selected_type is None:

                                    if next_occupied_start < window_end:
                                        cursor = next_occupied_start
                                        continue

                                    break

                                # -----------------------------------------
                                # Final overlap validation
                                # -----------------------------------------

                                overlaps = any(
                                    intervals_overlap(
                                        cursor,
                                        slot_end,
                                        existing_start,
                                        existing_end
                                    )
                                    for existing_start, existing_end
                                    in occupied
                                )

                                if overlaps:
                                    skipped_overlap += 1
                                    cursor = slot_end
                                    continue

                                slot_start_utc = ist_to_utc(cursor)
                                slot_end_utc = ist_to_utc(slot_end)

                                # -----------------------------------------
                                # Insert DRAFT slot
                                # -----------------------------------------

                                cur.execute(
                                    """
                                    INSERT IGNORE INTO appointment_slots
                                    (
                                        doctor_id,
                                        consultation_type_id,
                                        slot_start,
                                        slot_end,
                                        display_order,
                                        status
                                    )
                                    VALUES (%s, %s, %s, %s, %s, 'DRAFT')
                                    """,
                                    (
                                        doctor_id,
                                        selected_type[
                                            "consultation_type_id"
                                        ],
                                        slot_start_utc,
                                        slot_end_utc,
                                        display_order
                                    )
                                )

                                if cur.rowcount == 1:

                                    created += 1
                                    day_created += 1
                                    display_order += 1

                                    occupied.append((cursor, slot_end))
                                    occupied.sort(
                                        key=lambda item: item[0]
                                    )

                                    logger.info(
                                        "Draft slot created: doctor=%s, "
                                        "date=%s, type=%s, start=%s, end=%s",
                                        doctor_id,
                                        current_date,
                                        selected_type["name"],
                                        cursor,
                                        slot_end
                                    )

                                else:
                                    skipped_overlap += 1

                                cursor = slot_end

                        logger.info(
                            "Doctor %s, date %s: draft slots created=%s",
                            doctor_id,
                            current_date,
                            day_created
                        )

                    finally:

                        # Release the named lock.
                        cur.execute(
                            "SELECT RELEASE_LOCK(%s)",
                            (lock_name,)
                        )

                    current_date += timedelta(days=1)

        # -----------------------------------------------------
        # 9. Commit
        # -----------------------------------------------------

        conn.commit()

        logger.info("Slot materialisation committed successfully")

        logger.info("========== SLOT SUMMARY ==========")
        logger.info("Draft slots created: %s", created)
        logger.info("Blackout dates skipped: %s", skipped_blackout)
        logger.info(
            "Doctors without selected types: %s",
            skipped_no_types
        )
        logger.info(
            "Existing schedules preserved: %s",
            skipped_existing_schedule
        )
        logger.info(
            "Invalid availability windows: %s",
            skipped_invalid_hours
        )
        logger.info(
            "Invalid consultation durations: %s",
            skipped_invalid_duration
        )
        logger.info("Overlapping slots skipped: %s", skipped_overlap)
        logger.info("Generation locks unavailable: %s", skipped_lock)
        logger.info("Timezone: Asia/Kolkata")
        logger.info("==================================")

        return response(200, {
            "message": "Draft slot generation completed",
            "slots_created": created,
            "blackout_dates_skipped": skipped_blackout,
            "doctors_without_selected_types": skipped_no_types,
            "existing_schedules_preserved": skipped_existing_schedule,
            "invalid_working_hours_skipped": skipped_invalid_hours,
            "invalid_durations_skipped": skipped_invalid_duration,
            "overlapping_slots_skipped": skipped_overlap,
            "generation_locks_unavailable": skipped_lock,
            "days_ahead": days_ahead
        })

    except Exception:

        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                logger.exception("Database rollback failed")

        logger.exception("Slot Materialisation Lambda failed")

        return response(500, {
            "message": "Slot materialisation failed"
        })

    finally:

        if conn is not None:
            try:
                conn.close()
                logger.info("Database connection closed")
            except Exception:
                logger.exception("Database connection close failed")
