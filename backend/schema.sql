-- claim_portal database (Postgres on 192.168.100.237).
-- Holds only portal-owned data; claim data is read live from Digital_Insurance.

CREATE TABLE IF NOT EXISTS users (
    id            serial PRIMARY KEY,
    username      text NOT NULL UNIQUE,
    full_name     text NOT NULL,
    role          text NOT NULL CHECK (role IN ('admin', 'finance', 'claims')),
    password_hash text NOT NULL,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Readable copy of the password, kept at the owner's request. Login only checks password_hash.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_plain text;

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('admin', 'finance', 'claims'));

-- Current payment status per source claim (InitialClaimRasings.Id).
-- No row means an approved claim is still at the default, "Send Payment".
CREATE TABLE IF NOT EXISTS payment_status (
    claim_id   integer PRIMARY KEY,
    claim_no   text NOT NULL,
    status     text NOT NULL,
    updated_by integer NOT NULL REFERENCES users(id),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Append-only log of every status change; username/role are copied so the
-- history stays readable even if a user is later renamed or deactivated.
CREATE TABLE IF NOT EXISTS payment_status_history (
    id           bigserial PRIMARY KEY,
    claim_id     integer NOT NULL,
    claim_no     text NOT NULL,
    old_status   text,
    new_status   text NOT NULL,
    changed_by   integer NOT NULL REFERENCES users(id),
    changed_by_username text NOT NULL,
    changed_by_role     text NOT NULL,
    changed_at   timestamptz NOT NULL DEFAULT now()
);

-- Where a change came from when it wasn't made in the portal (e.g. the Excel row it was imported from).
ALTER TABLE payment_status_history ADD COLUMN IF NOT EXISTS note text;

-- Excel-imported statuses have no portal user, so these stay NULL for them.
ALTER TABLE payment_status ALTER COLUMN updated_by DROP NOT NULL;
ALTER TABLE payment_status_history ALTER COLUMN changed_by DROP NOT NULL;

-- Which 'Customer Details' row of the Finance workbook each approved claim was matched to,
-- with the customer name from that row (the source DB has almost no names).
CREATE TABLE IF NOT EXISTS excel_matches (
    claim_id     integer PRIMARY KEY,
    claim_no     text NOT NULL,
    excel_file   text NOT NULL,
    excel_row    integer NOT NULL,
    name         text,
    excel_status text,
    matched_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS payment_status_history_claim_idx
    ON payment_status_history (claim_id, changed_at DESC);
