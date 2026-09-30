-- Violations archive for the portal
-- ================================
-- Paste this whole file into Supabase -> SQL Editor -> New query, and press Run. It is safe to run twice.
--
-- The portal calculates each finished day (00:00-24:00 PKT) once, just after midnight, and stores the
-- result here. Reports for 1D / 7D / 15D / 30D then read these rows instead of downloading telemetry.
--
--   portal_violations     one row per violation, everything the portal shows for it
--   portal_archive_days   one row per calculated day, so a day with no violations is told apart from
--                         a day that was never calculated, and the limits used are on record
--
-- Row level security is switched on with no policies: the public (anon) key sees nothing, while the
-- portal's service-role key bypasses it as usual.

create table if not exists public.portal_violations (
    id                        text primary key,        -- stable per violation, so a recalculated day overwrites
    day                       date not null,           -- PKT calendar day the violation falls in
    time_unix                 bigint not null,
    time_pkt                  timestamp not null,      -- wall-clock time in Pakistan
    type                      text not null,           -- FATIGUE_DRIVING, OVERSPEED, NIGHT_DRIVING, ...
    type_label                text not null,
    source                    text not null,           -- telemetry (calculated) | wialon_notification
    vehicle_id                bigint not null,
    vehicle                   text not null,
    driver                    text,                    -- driver bound to the vehicle when the day was archived
    speed_kmh                 numeric,
    continuous_drive_minutes  numeric,                 -- Fatigue Driving only
    rest_minutes              numeric,                 -- Fatigue Driving only
    period                    text,                    -- Day | Night: which fatigue limit applied
    lat                       double precision,
    lon                       double precision,
    address                   text,                    -- Wialon's address, for Wialon alerts
    location                  text,                    -- display location, resolved when archived
    details                   text,
    telemetry                 jsonb,                   -- speed / ignition / odometer at the violation
    text_names_other_unit     boolean not null default false,
    archived_at               timestamptz not null default now()
);

create index if not exists portal_violations_day_idx on public.portal_violations (day);
create index if not exists portal_violations_type_day_idx on public.portal_violations (type, day);
create index if not exists portal_violations_vehicle_day_idx on public.portal_violations (vehicle_id, day);

create table if not exists public.portal_archive_days (
    day               date primary key,
    status            text not null,                   -- done | failed
    violations        integer not null default 0,
    fatigue           integer not null default 0,
    limits            jsonb,                           -- fatigue limits the day was calculated with
    fleet_size        integer,                         -- vehicles in the account that day
    vehicles_checked  integer,                         -- vehicles whose telemetry was read for fatigue
    messages_read     bigint,
    seconds           integer,
    error             text,
    built_at          timestamptz not null default now()
);

alter table public.portal_violations enable row level security;
alter table public.portal_archive_days enable row level security;

-- Reviews: a person's verdict on a violation, Genuine or False. Kept apart from portal_violations
-- because the nightly job replaces a day's rows when it recalculates it; keyed by vehicle, type and
-- second (the key the portal uses to merge Wialon's double-fired alerts), which recalculation keeps.
create table if not exists public.portal_violation_reviews (
    vehicle_id   bigint not null,
    type         text not null,
    time_unix    bigint not null,
    verdict      text not null check (verdict in ('GENUINE', 'FALSE')),
    reviewed_at  timestamptz not null default now(),
    primary key (vehicle_id, type, time_unix)
);

create index if not exists portal_violation_reviews_time_idx on public.portal_violation_reviews (time_unix);

alter table public.portal_violation_reviews enable row level security;

-- Newer Supabase projects do not open tables made in the SQL editor to the API automatically. The
-- portal connects with the service-role key, so that role (and only that role) gets access.
grant select, insert, update, delete on public.portal_violations to service_role;
grant select, insert, update, delete on public.portal_archive_days to service_role;
grant select, insert, update, delete on public.portal_violation_reviews to service_role;
