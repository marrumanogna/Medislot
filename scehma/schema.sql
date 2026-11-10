-- =========================================================
-- MEDISLOT - CURRENT RDS MYSQL SCHEMA
-- Target: MySQL 8.0.16+
--
-- This is the clean-install schema for the current MediSlot
-- design. It reflects the consultation catalogue + doctor fee
-- separation and the current appointment_slots foreign key.
--
-- Application DATETIME values (slot_start/slot_end) are stored
-- as UTC. The application displays them in Asia/Kolkata (IST).
--
-- IMPORTANT:
-- 1. Run this only against a NEW/EMPTY database.
-- 2. This is not an ALTER/migration script for an existing RDS.
-- 3. Do not include migration backup tables in a clean schema.
-- =========================================================

CREATE DATABASE IF NOT EXISTS medislot
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE medislot;

-- =========================================================
-- 1. USERS
-- =========================================================
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    first_name VARCHAR(100) NULL,
    last_name VARCHAR(100) NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('PATIENT', 'DOCTOR') NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_users_email (email),
    KEY idx_users_role_active (role, is_active)
) ENGINE=InnoDB;

-- =========================================================
-- 2. DOCTOR PROFILES
-- =========================================================
CREATE TABLE IF NOT EXISTS doctor_profiles (
    user_id BIGINT UNSIGNED NOT NULL,
    full_name VARCHAR(201) NOT NULL,
    licence_number VARCHAR(100) NOT NULL,
    specialization VARCHAR(120) NOT NULL,
    qualifications JSON NOT NULL,
    experience_years SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    hospital_name VARCHAR(200) NOT NULL,
    hospital_address VARCHAR(500) NOT NULL,

    -- City used by patient search (Milestone 4). Optional; derived from
    -- hospital_address at registration when not supplied.
    hospital_city VARCHAR(100) NULL,

    languages_spoken JSON NOT NULL,
    bio TEXT NULL,
    consultation_fee DECIMAL(10,2) NULL,
    photo_key VARCHAR(1024) NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    verification_status VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    verified_at DATETIME NULL,
    verified_by BIGINT UNSIGNED NULL,
    rejection_reason TEXT NULL,
    licence_document_key VARCHAR(500) NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    -- Surname (last word of full_name), stored so it can be indexed.
    name_last_word VARCHAR(201)
        GENERATED ALWAYS AS (SUBSTRING_INDEX(full_name, ' ', -1)) STORED,

    PRIMARY KEY (user_id),
    UNIQUE KEY uq_doctor_licence_number (licence_number),

    -- Doctor search indexes (Milestone 4). See docs/milestone-4.md.
    KEY idx_doctor_spec_city (specialization, hospital_city),
    KEY idx_doctor_city (hospital_city),
    KEY idx_doctor_hospital_name (hospital_name),
    KEY idx_doctor_full_name (full_name),
    KEY idx_doctor_surname (name_last_word),
    CONSTRAINT fk_doctor_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE,
    CONSTRAINT chk_doctor_experience
        CHECK (experience_years <= 80),
    CONSTRAINT chk_doctor_fee
        CHECK (consultation_fee IS NULL OR consultation_fee >= 0)
) ENGINE=InnoDB;

-- =========================================================
-- 3. PATIENTS
-- =========================================================
CREATE TABLE IF NOT EXISTS patients (
    patient_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    date_of_birth DATE NULL,
    gender VARCHAR(20) NULL,
    phone VARCHAR(20) NULL,
    address TEXT NULL,
    city VARCHAR(100) NULL,
    emergency_contact VARCHAR(20) NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (patient_id),
    UNIQUE KEY uq_patients_user_id (user_id),
    CONSTRAINT fk_patient_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE
) ENGINE=InnoDB;

-- =========================================================
-- 4. STATIC CONSULTATION CATALOGUE
-- Consultation names and durations are platform-defined.
-- Doctors do not create/edit these rows.
-- =========================================================
CREATE TABLE IF NOT EXISTS consultation_catalogue (
    consultation_type_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(120) NOT NULL,
    duration_minutes SMALLINT UNSIGNED NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    capacity SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (consultation_type_id),
    UNIQUE KEY uq_consultation_catalogue_name (name),
    CONSTRAINT chk_catalogue_duration
        CHECK (duration_minutes BETWEEN 5 AND 480)
) ENGINE=InnoDB;

-- Stable catalogue IDs used by the current MediSlot data.
INSERT INTO consultation_catalogue
    (consultation_type_id, name, duration_minutes, is_active)
VALUES
    (1, 'Quick Consult', 15, 1),
    (2, 'Standard Consult', 30, 1),
    (3, 'Detailed Consult', 60, 1),
    (4, 'Health Camp Session', 60, 1)
ON DUPLICATE KEY UPDATE
    name = VALUES(name),
    duration_minutes = VALUES(duration_minutes),
    is_active = VALUES(is_active);

-- =========================================================
-- 5. DOCTOR-SPECIFIC CONSULTATION FEES
-- One fee per doctor and catalogue consultation type.
-- =========================================================
CREATE TABLE IF NOT EXISTS doctor_consultation_fees (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,
    consultation_type_id BIGINT UNSIGNED NOT NULL,
    fee DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    capacity SMALLINT UNSIGNED NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_doctor_consultation_fee
        (doctor_id, consultation_type_id),
    CONSTRAINT fk_doctor_fee_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctor_profiles(user_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_doctor_fee_catalogue
        FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_catalogue(consultation_type_id),
    CONSTRAINT chk_doctor_consultation_fee
        CHECK (fee >= 0)
) ENGINE=InnoDB;

-- =========================================================
-- 6. AVAILABILITY PATTERNS
-- day_of_week: 0=Monday ... 6=Sunday, as used by Python
-- date.weekday(). Times are interpreted as IST by the app.
-- =========================================================
CREATE TABLE IF NOT EXISTS availability_patterns (
    pattern_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,
    day_of_week TINYINT UNSIGNED NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (pattern_id),
    KEY idx_availability_doctor_day
        (doctor_id, day_of_week, is_active),
    CONSTRAINT fk_availability_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctor_profiles(user_id)
        ON DELETE CASCADE,
    CONSTRAINT chk_availability_day
        CHECK (day_of_week BETWEEN 0 AND 6),
    CONSTRAINT chk_availability_time
        CHECK (start_time < end_time)
) ENGINE=InnoDB;

-- =========================================================
-- 7. DOCTOR BLACKOUT DATES
-- =========================================================
CREATE TABLE IF NOT EXISTS doctor_blackout_dates (
    blackout_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,
    blackout_date DATE NOT NULL,
    reason VARCHAR(255) NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (blackout_id),
    UNIQUE KEY uq_doctor_blackout_date (doctor_id, blackout_date),
    CONSTRAINT fk_blackout_doctor
        FOREIGN KEY (doctor_id) REFERENCES doctor_profiles(user_id)
        ON DELETE CASCADE
) ENGINE=InnoDB;

-- =========================================================
-- 8. APPOINTMENT SLOTS
-- =========================================================
CREATE TABLE IF NOT EXISTS appointment_slots (
    slot_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,

    -- NULL while the slot is in DRAFT status
    consultation_type_id BIGINT UNSIGNED NULL,

    slot_start DATETIME NOT NULL,
    slot_end DATETIME NOT NULL,

    -- Display order is independent of actual appointment time
    display_order INT NOT NULL DEFAULT 0,

    status ENUM('DRAFT', 'AVAILABLE', 'BOOKED', 'BLOCKED')
        NOT NULL DEFAULT 'DRAFT',

    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (slot_id),

    -- Prevent duplicate start times for the same doctor
    UNIQUE KEY uq_doctor_slot_start (doctor_id, slot_start),

    KEY idx_doctor_slot_status
        (doctor_id, status, slot_start),

    KEY idx_doctor_display_order
        (doctor_id, display_order),

    CONSTRAINT fk_slot_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctor_profiles(user_id)
        ON DELETE CASCADE,

    CONSTRAINT fk_slot_catalogue
        FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_catalogue(consultation_type_id),

    CONSTRAINT chk_slot_time
        CHECK (slot_start < slot_end),

    CONSTRAINT chk_slot_display_order
        CHECK (display_order >= 0)
) ENGINE=InnoDB;

-- =========================================================
-- 9. SPECIALIZATION OPTIONS
-- =========================================================
CREATE TABLE IF NOT EXISTS specialization_options (
    specialization VARCHAR(120) NOT NULL,
    sort_order INT NOT NULL,
    PRIMARY KEY (specialization)
) ENGINE=InnoDB;

INSERT INTO specialization_options (specialization, sort_order)
VALUES
    ('General Medicine', 1),
    ('Cardiology', 2),
    ('Dermatology', 3),
    ('Dentistry', 4),
    ('ENT', 5),
    ('Gynaecology', 6),
    ('Neurology', 7),
    ('Orthopaedics', 8),
    ('Paediatrics', 9),
    ('Psychiatry', 10)
ON DUPLICATE KEY UPDATE sort_order = VALUES(sort_order);

-- =========================================================
-- 10. EXPERIENCE OPTIONS
-- =========================================================
CREATE TABLE IF NOT EXISTS experience_options (
    experience_years TINYINT UNSIGNED NOT NULL,
    PRIMARY KEY (experience_years)
) ENGINE=InnoDB;

INSERT INTO experience_options (experience_years)
VALUES
    (0),(1),(2),(3),(4),(5),(6),(7),(8),(9),
    (10),(11),(12),(13),(14),(15),(16),(17),(18),(19),
    (20),(21),(22),(23),(24),(25),(26),(27),(28),(29),
    (30),(31),(32),(33),(34),(35),(36),(37),(38),(39),
    (40),(41),(42),(43),(44),(45),(46),(47),(48),(49),
    (50),(51),(52),(53),(54),(55),(56),(57),(58),(59),
    (60),(61),(62),(63),(64),(65),(66),(67),(68),(69),
    (70),(71),(72),(73),(74),(75),(76),(77),(78),(79),(80)
ON DUPLICATE KEY UPDATE experience_years = VALUES(experience_years);

-- =========================================================
-- 11. QUALIFICATION OPTIONS
-- =========================================================
CREATE TABLE IF NOT EXISTS qualification_options (
    qualification VARCHAR(100) NOT NULL,
    sort_order INT NOT NULL,
    PRIMARY KEY (qualification)
) ENGINE=InnoDB;

INSERT INTO qualification_options (qualification, sort_order)
VALUES
    ('MBBS', 1),
    ('BDS', 2),
    ('BAMS', 3),
    ('BHMS', 4),
    ('MD', 5),
    ('MS', 6),
    ('DNB', 7),
    ('DM', 8),
    ('MCh', 9),
    ('MDS', 10),
    ('PhD', 11),
    ('Other', 12)
ON DUPLICATE KEY UPDATE sort_order = VALUES(sort_order);

-- =========================================================
-- 12. LANGUAGE OPTIONS
-- =========================================================
CREATE TABLE IF NOT EXISTS language_options (
    language VARCHAR(50) NOT NULL,
    sort_order INT NOT NULL,
    PRIMARY KEY (language)
) ENGINE=InnoDB;

INSERT INTO language_options (language, sort_order)
VALUES
    ('English', 1),
    ('Hindi', 2),
    ('Telugu', 3),
    ('Tamil', 4),
    ('Kannada', 5),
    ('Malayalam', 6),
    ('Marathi', 7),
    ('Urdu', 8)
ON DUPLICATE KEY UPDATE sort_order = VALUES(sort_order);

-- =========================================================
-- 13. SCHEMA MIGRATION LOG
-- Written by the db-migrate Lambda (lambdas/db_migrate). A clean install
-- from this file already contains every migration's changes; the Lambda
-- detects that and only records them.
-- =========================================================
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id VARCHAR(100) NOT NULL,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (migration_id)
) ENGINE=InnoDB;

-- =========================================================
-- MILESTONE 5: APPOINTMENTS (system of record) AND REFUND LEDGER
-- No foreign keys: history must survive profile deletion.
-- =========================================================
CREATE TABLE IF NOT EXISTS appointments (
    appointment_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slot_id BIGINT UNSIGNED NOT NULL,
    doctor_id BIGINT UNSIGNED NOT NULL,
    patient_id BIGINT UNSIGNED NOT NULL,
    consultation_type_id BIGINT UNSIGNED NOT NULL,
    slot_start DATETIME NOT NULL,
    slot_end DATETIME NOT NULL,
    fee DECIMAL(10,2) NOT NULL,
    status ENUM('CONFIRMED', 'COMPLETED', 'CANCELLED_BY_PATIENT', 'CANCELLED_BY_DOCTOR', 'NO_SHOW')
        NOT NULL DEFAULT 'CONFIRMED',
    active_slot_id BIGINT UNSIGNED NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    booked_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    cancelled_at TIMESTAMP NULL,
    cancel_reason VARCHAR(255) NULL,
    PRIMARY KEY (appointment_id),
    UNIQUE KEY uq_appt_patient_active_slot (patient_id, active_slot_id),
    UNIQUE KEY uq_appt_patient_idem (patient_id, idempotency_key),
    KEY idx_appt_slot_status (slot_id, status),
    KEY idx_appt_patient_start (patient_id, slot_start),
    KEY idx_appt_doctor_start (doctor_id, slot_start)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS refund_ledger (
    refund_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    appointment_id BIGINT UNSIGNED NOT NULL,
    patient_id BIGINT UNSIGNED NOT NULL,
    fee DECIMAL(10,2) NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    refund_percent TINYINT UNSIGNED NOT NULL,
    tier ENUM('T1', 'T2', 'T3', 'T4', 'DOCTOR_CANCELLED', 'NO_SHOW') NOT NULL,
    reason VARCHAR(255) NULL,
    created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (refund_id),
    UNIQUE KEY uq_refund_appointment (appointment_id),
    KEY idx_refund_patient (patient_id, refund_id),
    CONSTRAINT chk_refund_amount CHECK (amount >= 0 AND amount <= fee)
) ENGINE=InnoDB;

-- =========================================================
-- END OF MEDISLOT SCHEMA
-- =========================================================