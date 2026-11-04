-- Claim screening: anomaly checks applied between "Approved" and Finance.
-- Applied by `python manage.py init-db` (idempotent; safe to re-run).
--
-- Everything here lives in claim_portal. Claim data is read from the digital_insurance
-- replica in this same database, so screening never touches the source SQL Server and
-- every rule can be expressed as one set-based query instead of a per-claim round trip.

-- ---------------------------------------------------------------------------
-- Normalisation helpers. Shared by the rules and the catalogue import so that
-- "0300-1234567" and "923001234567" always reduce to the same key.
-- ---------------------------------------------------------------------------

-- Trigram similarity, for comparing claimant names that are spelled differently.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Lower-case, collapse every run of non-alphanumerics to one space. Used for
-- service and person names: 'Jazz Mobile Insurance-Daily-Gold' -> 'jazz mobile insurance daily gold'.
CREATE OR REPLACE FUNCTION portal_norm_text(v text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT NULLIF(btrim(regexp_replace(lower(coalesce(v, '')), '[^a-z0-9]+', ' ', 'g')), '')
$$;

-- Digits only.
CREATE OR REPLACE FUNCTION portal_digits(v text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT regexp_replace(coalesce(v, ''), '[^0-9]', '', 'g')
$$;

-- Last 10 digits of a Pakistani MSISDN. 923001234567, 03001234567 and 3001234567
-- all reduce to 3001234567. NULL when there aren't 10 digits to take.
CREATE OR REPLACE FUNCTION portal_norm_msisdn(v text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN length(portal_digits(v)) >= 10
                THEN right(portal_digits(v), 10) END
$$;

-- Payout account / IBAN: strip punctuation, upper-case. Keeps letters because
-- IBANs (PK57TMFB...) are accounts too.
CREATE OR REPLACE FUNCTION portal_norm_account(v text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT NULLIF(upper(regexp_replace(coalesce(v, ''), '[^A-Za-z0-9]', '', 'g')), '')
$$;

-- How many distinct characters a normalised string uses. '1111111111' -> 1.
CREATE OR REPLACE FUNCTION portal_distinct_chars(v text) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT count(DISTINCT ch)::integer
    FROM regexp_split_to_table(coalesce(v, ''), '') AS ch
    WHERE ch <> ''
$$;

-- Obvious filler rather than a real identifier: empty, or long but built from one or
-- two repeated characters ('11111111111111', '2222222222222', '-', '0000...').
-- Deliberately conservative: a real account number uses more than two distinct digits.
CREATE OR REPLACE FUNCTION portal_is_placeholder(v text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN portal_norm_account(v) IS NULL THEN true
        WHEN length(portal_norm_account(v)) >= 6
             AND portal_distinct_chars(portal_norm_account(v)) <= 2 THEN true
        ELSE false
    END
$$;

-- Luhn checksum, used to tell a real IMEI from a typed-in sequence.
-- A 15-digit IMEI's last digit is a Luhn check digit, so this rejects
-- '678910111213141516'-style filler without maintaining a blacklist.
CREATE OR REPLACE FUNCTION portal_luhn_ok(v text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    d text := portal_digits(v);
    total integer := 0;
    n integer;
    i integer;
    pos integer;
BEGIN
    IF length(d) < 2 THEN
        RETURN false;
    END IF;
    -- Walk right to left; double every second digit.
    FOR i IN 1 .. length(d) LOOP
        pos := length(d) - i + 1;
        n := substr(d, pos, 1)::integer;
        IF i % 2 = 0 THEN
            n := n * 2;
            IF n > 9 THEN
                n := n - 9;
            END IF;
        END IF;
        total := total + n;
    END LOOP;
    RETURN total % 10 = 0;
END;
$$;

-- A usable IMEI: 15 digits (14 and 16 appear in the wild as TAC-only or SVN-suffixed),
-- more than two distinct digits, and a valid Luhn check at 15.
CREATE OR REPLACE FUNCTION portal_imei_is_usable(v text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT length(portal_digits(v)) BETWEEN 14 AND 16
       AND portal_distinct_chars(portal_digits(v)) > 2
       AND (length(portal_digits(v)) <> 15 OR portal_luhn_ok(v))
$$;

-- A name that carries no information: blank, 'N/A', 'NA N/A', 'null', 'test', '-'.
CREATE OR REPLACE FUNCTION portal_name_is_blank(v text) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT portal_norm_text(v) IS NULL
        OR portal_norm_text(v) ~ '^(n ?a|na|n|null|none|nil|test|tester|customer|user|unknown|xyz|abc|dummy)( (n ?a|na|n|null|none|nil|test|tester|customer|user|unknown|xyz|abc|dummy))*$'
$$;

-- Do two claimant names look like the same person? NULL when either name is missing,
-- so "we could not tell" never masquerades as "they match". A bank account title and a
-- customer record rarely agree exactly, so a trigram similarity of 0.45 counts as a
-- match, as does one name being a prefix of the other ('Muhammad Bilal' / 'M Bilal Khan').
CREATE OR REPLACE FUNCTION portal_names_match(a text, b text) RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN portal_name_is_blank(a) OR portal_name_is_blank(b) THEN NULL
        WHEN portal_norm_text(a) = portal_norm_text(b)                       THEN true
        WHEN portal_norm_text(a) LIKE portal_norm_text(b) || '%'             THEN true
        WHEN portal_norm_text(b) LIKE portal_norm_text(a) || '%'             THEN true
        WHEN similarity(portal_norm_text(a), portal_norm_text(b)) >= 0.45    THEN true
        ELSE false
    END
$$;

-- Do all the names in a group belong to one person? NULL when fewer than two usable
-- names are known. Used to tell a customer reusing their own payout account from
-- several different people being paid into one account.
CREATE OR REPLACE FUNCTION portal_all_names_match(names text[]) RETURNS boolean
LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
DECLARE
    real_names text[];
    i integer;
    j integer;
    verdict boolean;
BEGIN
    SELECT array_agg(n) INTO real_names
      FROM unnest(coalesce(names, '{}'::text[])) AS n
     WHERE NOT portal_name_is_blank(n);

    IF real_names IS NULL OR array_length(real_names, 1) < 2 THEN
        RETURN NULL;  -- nothing to compare
    END IF;

    FOR i IN 1 .. array_length(real_names, 1) - 1 LOOP
        FOR j IN i + 1 .. array_length(real_names, 1) LOOP
            verdict := portal_names_match(real_names[i], real_names[j]);
            IF verdict IS NOT TRUE THEN
                RETURN false;
            END IF;
        END LOOP;
    END LOOP;
    RETURN true;
END;
$$;

-- The calendar bucket a timestamp falls in, for the chosen entitlement period.
-- Entitlements are counted per calendar period, NOT as a rolling number of days:
-- "once a month" means once in September, so the 18th of September and the 8th of
-- October are two different entitlements even though they are 20 days apart.
-- A NULL or unrecognised period yields NULL, which means "no period configured".
CREATE OR REPLACE FUNCTION portal_period_key(ts timestamp, period text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE lower(btrim(coalesce(period, '')))
        WHEN 'day'     THEN to_char(ts, 'YYYY-MM-DD')
        WHEN 'week'    THEN to_char(ts, 'IYYY-"W"IW')
        WHEN 'month'   THEN to_char(ts, 'YYYY-MM')
        WHEN 'quarter' THEN to_char(ts, 'YYYY-"Q"Q')
        WHEN 'half'    THEN to_char(ts, 'YYYY') ||
                            CASE WHEN extract(month FROM ts) <= 6 THEN '-H1' ELSE '-H2' END
        WHEN 'year'    THEN to_char(ts, 'YYYY')
    END
$$;

-- How a period reads in a sentence: 'the same month', 'the same calendar year'.
CREATE OR REPLACE FUNCTION portal_period_label(period text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE lower(btrim(coalesce(period, '')))
        WHEN 'day'     THEN 'the same day'
        WHEN 'week'    THEN 'the same week'
        WHEN 'month'   THEN 'the same month'
        WHEN 'quarter' THEN 'the same quarter'
        WHEN 'half'    THEN 'the same half-year'
        WHEN 'year'    THEN 'the same year'
    END
$$;


-- ---------------------------------------------------------------------------
-- Configuration: what a product's plan is, and how long a claim entitlement lasts.
-- ---------------------------------------------------------------------------

-- One row per distinct claim ServiceName, keyed on its normalised form.
-- `period_days` is the BILLING period from the product catalogue.
-- `claim_period` is the ENTITLEMENT period - the calendar bucket in which a
-- subscription may be paid once. These are not the same thing: a "Daily" plan is
-- charged daily, it does not entitle a claim per day. claim_period is therefore left
-- NULL until someone sets it, and with no period the duplicate rule can only report
-- that a repeat exists rather than judge it.
CREATE TABLE IF NOT EXISTS claim_service_plans (
    service_key       text PRIMARY KEY,
    service_name      text NOT NULL,
    plan              text,
    period_days       integer,
    claim_window_days integer,
    coverage_amount   numeric(15,2),
    allow_duplicate   boolean,
    category          text,
    source            text NOT NULL DEFAULT 'unknown',
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT claim_service_plans_source_check
        CHECK (source IN ('mysql', 'excel', 'keyword', 'manual', 'unknown'))
);

-- Entitlements are counted per calendar period, so the window is a period name rather
-- than a number of days. claim_window_days is left in place for older installs but is
-- no longer read by any rule.
ALTER TABLE claim_service_plans ADD COLUMN IF NOT EXISTS claim_period text;

COMMENT ON COLUMN claim_service_plans.period_days IS 'Billing period from the catalogue - NOT a claim entitlement.';
COMMENT ON COLUMN claim_service_plans.claim_period IS 'Entitlement period (day/week/month/quarter/half/year); NULL = not configured.';
COMMENT ON COLUMN claim_service_plans.claim_window_days IS 'Superseded by claim_period; no longer used.';

-- Per-category fallback used when a service has no period of its own.
-- Seeded with the categories present in the data and a NULL period, so the
-- unset ones are visible rather than assumed.
CREATE TABLE IF NOT EXISTS claim_category_windows (
    category_key text PRIMARY KEY,
    window_days  integer,
    note         text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE claim_category_windows ADD COLUMN IF NOT EXISTS claim_period text;

DO $$
BEGIN
    -- Only the six calendar buckets portal_period_key() understands are accepted,
    -- so a typo is refused at write time instead of silently disabling the rule.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claim_service_plans_period_check') THEN
        ALTER TABLE claim_service_plans ADD CONSTRAINT claim_service_plans_period_check
            CHECK (claim_period IS NULL OR claim_period IN ('day','week','month','quarter','half','year'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claim_category_windows_period_check') THEN
        ALTER TABLE claim_category_windows ADD CONSTRAINT claim_category_windows_period_check
            CHECK (claim_period IS NULL OR claim_period IN ('day','week','month','quarter','half','year'));
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- Screening results
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS screening_runs (
    id              bigserial PRIMARY KEY,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    claims_screened integer,
    flags_written   integer,
    rule_counts     jsonb,
    error           text
);

-- One row per (claim, rule). Re-screening upserts, so first_seen_at survives and
-- a flag that no longer applies is deleted by the run that superseded it.
CREATE TABLE IF NOT EXISTS claim_flags (
    id                bigserial PRIMARY KEY,
    claim_id          integer NOT NULL,
    claim_no          text NOT NULL,
    rule_code         text NOT NULL,
    severity          text NOT NULL,
    summary           text NOT NULL,
    detail            jsonb NOT NULL DEFAULT '{}'::jsonb,
    related_claim_ids integer[] NOT NULL DEFAULT '{}',
    run_id            bigint REFERENCES screening_runs(id) ON DELETE SET NULL,
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    computed_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT claim_flags_unique UNIQUE (claim_id, rule_code),
    CONSTRAINT claim_flags_severity_check CHECK (severity IN ('blocker', 'warn', 'info'))
);

CREATE INDEX IF NOT EXISTS claim_flags_claim_idx    ON claim_flags (claim_id);
CREATE INDEX IF NOT EXISTS claim_flags_severity_idx ON claim_flags (severity, rule_code);

-- Append-only record of Finance/Admin acknowledging or re-opening a flag.
-- Username and role are copied so the trail survives a user being renamed.
CREATE TABLE IF NOT EXISTS claim_flag_reviews (
    id                bigserial PRIMARY KEY,
    claim_id          integer NOT NULL,
    rule_code         text NOT NULL,
    action            text NOT NULL,
    reason            text NOT NULL,
    reviewed_by       integer REFERENCES users(id),
    reviewed_by_username text NOT NULL,
    reviewed_by_role  text NOT NULL,
    reviewed_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT claim_flag_reviews_action_check CHECK (action IN ('acknowledged', 'reopened'))
);

CREATE INDEX IF NOT EXISTS claim_flag_reviews_claim_idx
    ON claim_flag_reviews (claim_id, rule_code, reviewed_at DESC);

-- Latest review per (claim, rule): what the UI treats as the flag's current state.
CREATE OR REPLACE VIEW claim_flag_state AS
SELECT f.claim_id,
       f.rule_code,
       f.severity,
       f.summary,
       f.detail,
       f.related_claim_ids,
       f.first_seen_at,
       f.computed_at,
       r.action = 'acknowledged' AS acknowledged,
       r.reason    AS review_reason,
       r.reviewed_by_username,
       r.reviewed_at
FROM claim_flags f
LEFT JOIN LATERAL (
    SELECT action, reason, reviewed_by_username, reviewed_at
    FROM claim_flag_reviews v
    WHERE v.claim_id = f.claim_id AND v.rule_code = f.rule_code
    ORDER BY v.reviewed_at DESC, v.id DESC
    LIMIT 1
) r ON true;
