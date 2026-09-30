-- Fatigue Driving alert emails
-- ============================
-- Paste this whole file into Supabase -> SQL Editor -> New query, and press Run. It is safe to run twice.
--
-- One row per (fatigue violation x recipient). The primary key is what makes an alert fire exactly
-- once: archive_day() replaces a whole day's violations every time it runs, and catch_up() re-archives
-- days whenever the limits change, so the same violation is offered to the queue many times. Inserts
-- use "on conflict do nothing", which means the second and later offers are dropped by Postgres and a
-- violation that has already been sent is never sent again.
--
-- Row level security is switched on with no policies: the public (anon) key sees nothing, while the
-- service-role key the portal connects with bypasses RLS entirely.

create table if not exists public.portal_fatigue_alerts (
    violation_key   text not null,          -- "<vehicle_id>:<type>:<time_unix>", same identity as reviews
    email           text not null,
    day             date not null,          -- PKT day of the violation, for ordering and clean-up
    vehicle         text,
    driver          text,
    violation_time  text,                   -- PKT timestamp as the portal shows it
    status          text not null default 'queued'
                    check (status in ('queued', 'sent', 'failed', 'obsolete')),
    attempts        int  not null default 0,
    error           text,
    queued_at       timestamptz not null default now(),
    sent_at         timestamptz,
    primary key (violation_key, email)
);

-- The worker's own query: everything still waiting, oldest day first.
create index if not exists portal_fatigue_alerts_pending_idx
    on public.portal_fatigue_alerts (status, day)
    where status = 'queued';

alter table public.portal_fatigue_alerts enable row level security;

-- Newer Supabase projects do not open tables made in the SQL editor to the API automatically. The
-- portal connects with the service-role key, so that role (and only that role) gets access.
grant select, insert, update, delete on public.portal_fatigue_alerts to service_role;
