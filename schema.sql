-- ==============================================================================
-- Supabase Schema for Driver Fatigue & Work-Rest Compliance Platform
-- (Modeled after the LafargeHolcim Fleet Safety Blueprint)
-- ==============================================================================

-- 1. Daily Fatigue & Work-Rest Violations
CREATE TABLE IF NOT EXISTS daily_fatigue_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_name TEXT NOT NULL,
    driver_name TEXT DEFAULT 'Unassigned',
    violation_type TEXT NOT NULL,          -- 'CONTINUOUS_DRIVE_EXCEEDED', 'INSUFFICIENT_REST', 'DAILY_LIMIT_EXCEEDED'
    severity TEXT NOT NULL DEFAULT 'HIGH', -- 'CRITICAL', 'HIGH', 'WARNING'
    continuous_drive_minutes NUMERIC(6, 1) NOT NULL,
    rest_taken_minutes NUMERIC(6, 1) DEFAULT 0.0,
    speed_kmh NUMERIC(5, 1) DEFAULT 0.0,
    latitude NUMERIC(10, 7),
    longitude NUMERIC(10, 7),
    location_name TEXT,
    notes TEXT,
    violation_timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast date-range filtering in the dispatcher dashboard
CREATE INDEX IF NOT EXISTS idx_fatigue_time ON daily_fatigue_reports (violation_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_fatigue_vehicle ON daily_fatigue_reports (vehicle_name);

-- 2. Driver Safety Scorecards
CREATE TABLE IF NOT EXISTS driver_scorecards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_name TEXT UNIQUE NOT NULL,
    vehicle_name TEXT,
    safety_score INT NOT NULL DEFAULT 100,    -- 0 to 100
    compliance_grade TEXT NOT NULL DEFAULT 'A', -- A+, A, B, C, F
    shift_hours NUMERIC(5, 2) DEFAULT 0.0,
    total_km NUMERIC(8, 2) DEFAULT 0.0,
    violations_count INT DEFAULT 0,
    status TEXT DEFAULT 'Compliant',          -- 'Compliant', 'Warning', 'High Risk', 'Suspended'
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. Live Fleet Telemetry Snapshot (The 6 Core Wialon Fields)
CREATE TABLE IF NOT EXISTS fleet_live_telemetry (
    vehicle_name TEXT PRIMARY KEY,
    speed NUMERIC(5, 1) NOT NULL DEFAULT 0.0,
    latitude NUMERIC(10, 7),
    longitude NUMERIC(10, 7),
    mileage_km NUMERIC(10, 2) DEFAULT 0.0,
    ignition INT DEFAULT 0,                   -- 1 = ON, 0 = OFF (from io_239)
    engine_on BOOLEAN DEFAULT FALSE,          -- derived from pwr_ext > 13.2V
    voltage NUMERIC(5, 2) DEFAULT 0.0,        -- pwr_ext
    last_heartbeat TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 4. Work-Rest Compliance Configurations
CREATE TABLE IF NOT EXISTS compliance_configs (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed Default Compliance Rules (LafargeHolcim & Swiss Safety Standards)
INSERT INTO compliance_configs (key, value)
VALUES (
    'work_rest_thresholds',
    '{
        "max_continuous_drive_minutes": 240,
        "min_break_minutes": 30,
        "max_daily_drive_hours": 9.0,
        "speed_threshold_kmh": 5.0,
        "dispatcher_emails": ["fleet.safety@unilever.com", "dispatch.control@tpltrakker.com"],
        "daily_report_enabled": true
    }'::jsonb
)
ON CONFLICT (key) DO NOTHING;
