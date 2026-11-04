"""Anomaly screening applied to claims between approval and Finance.

Each rule is one set-based query over the digital_insurance replica inside claim_portal,
so a full screening run is a fixed handful of statements regardless of claim count -
never a query per claim. The source SQL Server is not touched at all.

A run is atomic: every rule writes into one staging table, and the swap into
claim_flags happens once at the end inside the caller's transaction. A failed run
therefore leaves the previous flags in place rather than a half-updated set.

Flags are keyed on (claim_id, rule_code) and upserted, so first_seen_at survives a
re-run while severity and detail are refreshed; flags a run no longer produces are
deleted. Reviews in claim_flag_reviews are keyed the same way and are never touched
here, so acknowledging a flag survives re-screening.
"""
import json
import logging

from .db import portal_cursor

log = logging.getLogger("screening")

# Severity ladder, worst first. Used to order the UI and to pick a claim's overall risk.
SEVERITY_ORDER = ["blocker", "warn", "info"]

# Each rule carries the words Finance reads, not the rule's internal name:
#   short - fits in the Risk cell of the grid
#   title - names the actual problem, for the filter and the flag popup
RULES = {
    "DUP_SERVICE_CLAIM": {
        "short": "Claimed twice",
        "title": "Claimed again in the same plan period",
    },
    "DUP_SERVICE_CROSS_SUB": {
        "short": "Claimed again",
        "title": "Claimed again after re-subscribing to the same service",
    },
    "DUP_IMEI": {
        "short": "Same phone",
        "title": "Same handset (IMEI) claimed by other people",
    },
    "SHARED_PAYOUT_ACCOUNT": {
        "short": "Shared account",
        "title": "One bank account being paid for several mobile numbers",
    },
    "SHARED_SUBSCRIPTION": {
        "short": "Shared policy",
        "title": "One subscription claimed from several mobile numbers",
    },
    "PLACEHOLDER_IDENTIFIER": {
        "short": "Fake details",
        "title": "Fake account number or IMEI",
    },
    "AMOUNT_OVER_COVERAGE": {
        "short": "Over cover",
        "title": "Paid more than the product covers",
    },
    "BLACKLIST_MATCH": {
        "short": "Blacklisted",
        "title": "Claimant is on the blacklist",
    },
    "MISSING_CLAIMANT_NAME": {
        "short": "No name",
        "title": "No customer name on the payout",
    },
}

# ---------------------------------------------------------------------------
# One normalised row per claim, built once and reused by every rule below.
# Indexed on the keys the rules group by, which turns each rule into a hash
# join over a few hundred rows instead of a repeated sequential scan.
# ---------------------------------------------------------------------------
BASE_SQL = """
CREATE TEMP TABLE screen_claims ON COMMIT DROP AS
SELECT
    i."Id"                                        AS claim_id,
    btrim(coalesce(i."ClaimNumber", ''))          AS claim_no,
    i."Status"                                    AS claim_status,
    (i."Status" = 'Approved')                     AS is_approved,
    portal_norm_text(i."ServiceName")             AS service_key,
    btrim(i."ServiceName")                        AS service_name,
    portal_norm_text(i."Category")                AS category_key,
    btrim(i."Category")                           AS category,
    portal_norm_msisdn(i."MobileNumber")          AS msisdn,
    btrim(i."MobileNumber")                       AS mobile_raw,
    nullif(btrim(coalesce(i."SubscriptionId", '')), '') AS subscription_id,
    portal_norm_account(i."AccountNumber")        AS account_key,
    btrim(i."AccountNumber")                      AS account_raw,
    i."ApprovedDateTime"                          AS approved_at,
    i."ClaimDateTime"                             AS claim_at,
    -- Claimant name: the source rarely has one, so fall back to the name matched
    -- from the Finance workbook, exactly as the claims list does.
    coalesce(nullif(btrim(coalesce(i."CustomerName", '')), ''), x.name) AS claimant_name,
    -- Settled, not merely approved. A claim that was rejected and re-submitted is one
    -- entitlement paid once, so only a paid counterpart makes a repeat a duplicate.
    (ps.status = 'Paid')                          AS is_paid,
    p.imei_raw,
    p.imei_key,
    p.payable
FROM digital_insurance."InitialClaimRasings" i
LEFT JOIN excel_matches x ON x.claim_id = i."Id"
LEFT JOIN payment_status ps ON ps.claim_id = i."Id"
LEFT JOIN LATERAL (
    -- At most one processed row per claim in practice; TOP-1 by id makes a stray
    -- duplicate harmless instead of multiplying the claim.
    SELECT
        nullif(btrim(coalesce(pc."IMEINumber", '')), '') AS imei_raw,
        CASE WHEN portal_imei_is_usable(pc."IMEINumber")
             THEN portal_digits(pc."IMEINumber") END     AS imei_key,
        -- Amounts are text at source; anything that isn't a plain number becomes NULL.
        CASE WHEN btrim(coalesce(pc."PayableAmount", '')) ~ '^[0-9]+(\\.[0-9]+)?$'
             THEN btrim(pc."PayableAmount")::numeric END AS payable
    FROM digital_insurance."ProcessedClaims" pc
    WHERE pc."InitialClaimRasingId" = i."Id"
    ORDER BY pc."id" DESC
    LIMIT 1
) p ON true;

CREATE INDEX ON screen_claims (subscription_id, service_key);
CREATE INDEX ON screen_claims (msisdn, service_key);
CREATE INDEX ON screen_claims (account_key);
CREATE INDEX ON screen_claims (imei_key);
ANALYZE screen_claims;

CREATE TEMP TABLE screen_staging (
    claim_id          integer NOT NULL,
    claim_no          text    NOT NULL,
    rule_code         text    NOT NULL,
    severity          text    NOT NULL,
    summary           text    NOT NULL,
    detail            jsonb   NOT NULL,
    related_claim_ids integer[] NOT NULL DEFAULT '{}'
) ON COMMIT DROP;
"""

# Entitlement period per service: the service's own, else its category's.
# period_days (the billing period) is deliberately NOT a fallback - a "Daily" plan is
# charged daily, it does not entitle a claim per day - so an unconfigured service has
# no period and the duplicate rule can only note that a repeat exists.
WINDOW_SQL = """
CREATE TEMP TABLE screen_windows ON COMMIT DROP AS
SELECT sp.service_key,
       sp.service_name,
       sp.plan,
       sp.period_days,
       sp.coverage_amount,
       sp.allow_duplicate,
       coalesce(sp.claim_period, cw.claim_period) AS claim_period,
       CASE WHEN sp.claim_period IS NOT NULL THEN 'service'
            WHEN cw.claim_period IS NOT NULL THEN 'category'
            ELSE NULL END                          AS period_source
FROM claim_service_plans sp
LEFT JOIN claim_category_windows cw
       ON cw.category_key = portal_norm_text(sp.category);

CREATE INDEX ON screen_windows (service_key);
ANALYZE screen_windows;
"""

# --- Rule 1 -----------------------------------------------------------------
# A subscription being paid more than once for the same service inside one
# entitlement period.
#
# Three decisions make this match the business rule rather than merely detect repeats:
#  * Periods are CALENDAR buckets, not rolling days. 18 September and 8 October are
#    20 days apart but are two different months, so they are two entitlements and
#    nothing is reported. Only two claims inside the same bucket are a duplicate.
#  * The earlier claim must have been PAID. A claim that was rejected and then
#    re-submitted and approved is the same entitlement being settled once, not a
#    duplicate, so rejected claims are never counterparts here.
#  * With no period configured the rule cannot judge anything, so it reports the
#    repeat as a warning with the gap, and never as a blocker.
R_DUP_SERVICE_CLAIM = """
INSERT INTO screen_staging (claim_id, claim_no, rule_code, severity, summary, detail, related_claim_ids)
WITH pairs AS (
    SELECT a.claim_id,
           a.claim_no,
           w.service_name,
           a.service_key,
           w.claim_period,
           w.period_source,
           w.plan,
           w.period_days,
           b.claim_id AS other_id,
           b.claim_no AS other_no,
           b.is_paid  AS other_paid,
           -- Same calendar bucket? NULL period makes this NULL, never true.
           (portal_period_key(a.approved_at, w.claim_period) IS NOT NULL
            AND portal_period_key(a.approved_at, w.claim_period)
              = portal_period_key(b.approved_at, w.claim_period)) AS same_period,
           portal_period_key(a.approved_at, w.claim_period) AS period_key,
           abs(extract(epoch FROM (a.approved_at - b.approved_at)) / 86400)::int AS gap_days
    FROM screen_claims a
    JOIN screen_claims b
      ON b.subscription_id = a.subscription_id
     AND b.service_key     = a.service_key
     AND b.claim_id       <> a.claim_id
    LEFT JOIN screen_windows w ON w.service_key = a.service_key
    WHERE a.is_approved AND b.is_approved
      AND a.subscription_id IS NOT NULL
      AND a.service_key     IS NOT NULL
      AND a.approved_at     IS NOT NULL
      AND b.approved_at     IS NOT NULL
), agg AS (
    SELECT claim_id, claim_no, service_name, service_key, claim_period, period_source,
           plan, period_days, period_key,
           count(*)                                                  AS repeats,
           min(gap_days)                                             AS nearest_gap_days,
           count(*) FILTER (WHERE same_period)                       AS same_period_repeats,
           count(*) FILTER (WHERE same_period AND other_paid)        AS same_period_paid,
           count(*) FILTER (WHERE other_paid)                        AS paid_repeats_any,
           min(gap_days) FILTER (WHERE same_period AND other_paid)   AS paid_gap_days,
           array_agg(other_id ORDER BY other_id)                     AS other_ids,
           array_agg(other_no ORDER BY other_id)                     AS other_nos,
           array_remove(array_agg(CASE WHEN same_period AND other_paid THEN other_no END), NULL)
                                                                     AS same_period_paid_nos
    FROM pairs
    GROUP BY claim_id, claim_no, service_name, service_key, claim_period, period_source,
             plan, period_days, period_key
)
SELECT agg.claim_id,
       agg.claim_no,
       'DUP_SERVICE_CLAIM',
       CASE WHEN agg.same_period_paid  > 0 THEN 'blocker'
            ELSE 'warn' END,
       CASE
         WHEN agg.same_period_paid > 0 THEN
            format('Already PAID for %s in %s (%s) - claiming again',
                   agg.service_name, portal_period_label(agg.claim_period),
                   array_to_string(agg.same_period_paid_nos, ', '))
         WHEN agg.same_period_repeats > 0 THEN
            format('Second approved claim for %s in %s, neither paid yet',
                   agg.service_name, portal_period_label(agg.claim_period))
         ELSE
            format('Repeat approved claim for %s, %s days apart - no entitlement period set for this service',
                   agg.service_name, agg.nearest_gap_days)
       END,
       jsonb_build_object(
           'service', agg.service_name,
           'entitlement_period', agg.claim_period,
           'entitlement_period_source', agg.period_source,
           'period_of_this_claim', agg.period_key,
           'repeats_in_this_period', agg.same_period_repeats,
           'already_paid_in_this_period', agg.same_period_paid,
           'already_paid_claim_nos', to_jsonb(agg.same_period_paid_nos),
           'days_since_paid_claim', agg.paid_gap_days,
           'other_approved_claims', agg.repeats,
           'nearest_gap_days', agg.nearest_gap_days,
           'other_claim_nos', to_jsonb(agg.other_nos),
           'billing_plan', agg.plan,
           'billing_period_days', agg.period_days
       ),
       agg.other_ids
FROM agg
-- A repeat in a DIFFERENT period is the customer's next entitlement, so it is not
-- reported at all. Only same-period repeats, or services with no period to judge
-- against, reach Finance.
WHERE agg.same_period_repeats > 0
   OR (agg.claim_period IS NULL AND agg.paid_repeats_any > 0);
"""

# --- Rule 2 -----------------------------------------------------------------
# Cancelling a subscription and taking a new one does NOT create a duplicate: the
# customer paid a fresh premium, so they have earned a fresh entitlement. Claiming
# again two or seven months later under a new subscription is normal business.
#
# What is not normal is being PAID inside an entitlement period and then re-subscribing
# to claim again inside that SAME period - using the subscription boundary to get a
# second payout for one period. That, and only that, is what this rule reports:
#   same number, same service, two different subscriptions, both approved,
#   both falling in the same calendar period, and the earlier one already paid.
#
# So unlike rule 1 there is no "no period configured" fallback. Without a period there
# is nothing to be inside, and a new subscription is legitimate on its face, so an
# unconfigured service produces no flag here rather than a warning about a 200-day gap.
R_DUP_SERVICE_CROSS_SUB = """
INSERT INTO screen_staging (claim_id, claim_no, rule_code, severity, summary, detail, related_claim_ids)
WITH pairs AS (
    SELECT a.claim_id,
           a.claim_no,
           w.service_name,
           a.service_key,
           w.claim_period,
           w.period_source,
           a.subscription_id AS this_sub,
           b.subscription_id AS other_sub,
           b.claim_id        AS other_id,
           b.claim_no        AS other_no,
           b.is_paid         AS other_paid,
           portal_period_key(a.approved_at, w.claim_period) AS period_key,
           abs(extract(epoch FROM (a.approved_at - b.approved_at)) / 86400)::int AS gap_days
    FROM screen_claims a
    JOIN screen_claims b
      ON b.msisdn      = a.msisdn
     AND b.service_key = a.service_key
     AND b.claim_id   <> a.claim_id
     AND coalesce(b.subscription_id, '') <> coalesce(a.subscription_id, '')
    JOIN screen_windows w ON w.service_key = a.service_key
    WHERE a.is_approved AND b.is_approved
      AND a.msisdn      IS NOT NULL
      AND a.service_key IS NOT NULL
      AND a.approved_at IS NOT NULL
      AND b.approved_at IS NOT NULL
      -- A configured period, and both claims inside the same one. Everything else is
      -- a legitimate new entitlement and is not reported.
      AND w.claim_period IS NOT NULL
      AND portal_period_key(a.approved_at, w.claim_period)
        = portal_period_key(b.approved_at, w.claim_period)
), agg AS (
    SELECT claim_id, claim_no, service_name, claim_period, period_source, period_key, this_sub,
           count(*)                                           AS repeats,
           min(gap_days)                                      AS nearest_gap_days,
           count(*) FILTER (WHERE other_paid)                  AS paid_repeats,
           array_agg(other_id ORDER BY other_id)               AS other_ids,
           array_agg(other_no ORDER BY other_id)               AS other_nos,
           array_remove(array_agg(CASE WHEN other_paid THEN other_no END), NULL) AS paid_nos,
           array_remove(array_agg(DISTINCT other_sub), NULL)   AS other_subs
    FROM pairs
    GROUP BY claim_id, claim_no, service_name, claim_period, period_source, period_key, this_sub
)
SELECT agg.claim_id,
       agg.claim_no,
       'DUP_SERVICE_CROSS_SUB',
       CASE WHEN agg.paid_repeats > 0 THEN 'blocker' ELSE 'warn' END,
       CASE WHEN agg.paid_repeats > 0
            THEN format('Already PAID for %s in %s (%s), then re-subscribed and claimed again in the same period',
                        agg.service_name, portal_period_label(agg.claim_period),
                        array_to_string(agg.paid_nos, ', '))
            ELSE format('Two approved claims for %s in %s under different subscriptions, neither paid yet',
                        agg.service_name, portal_period_label(agg.claim_period))
       END,
       jsonb_build_object(
           'service', agg.service_name,
           'entitlement_period', agg.claim_period,
           'entitlement_period_source', agg.period_source,
           'period_of_this_claim', agg.period_key,
           'claims_in_this_period', agg.repeats,
           'already_paid_in_this_period', agg.paid_repeats,
           'already_paid_claim_nos', to_jsonb(agg.paid_nos),
           'nearest_gap_days', agg.nearest_gap_days,
           'other_claim_nos', to_jsonb(agg.other_nos),
           'this_subscription', agg.this_sub,
           'other_subscriptions', to_jsonb(agg.other_subs)
       ),
       agg.other_ids
FROM agg;
"""

# --- Rule 3 -----------------------------------------------------------------
# One handset identity on several claims. imei_key is only set for a plausible IMEI
# (14-16 digits, >2 distinct digits, valid Luhn at 15), so typed-in filler lands in
# PLACEHOLDER_IDENTIFIER instead of generating a false duplicate here.
R_DUP_IMEI = """
INSERT INTO screen_staging (claim_id, claim_no, rule_code, severity, summary, detail, related_claim_ids)
WITH grp AS (
    SELECT imei_key,
           count(*)                                        AS claims,
           count(*) FILTER (WHERE is_approved)              AS approved_claims,
           array_agg(claim_id ORDER BY claim_id)            AS ids,
           array_agg(claim_no ORDER BY claim_id)            AS nos
    FROM screen_claims
    WHERE imei_key IS NOT NULL AND is_approved
    GROUP BY imei_key
    HAVING count(*) > 1
)
SELECT c.claim_id,
       c.claim_no,
       'DUP_IMEI',
       -- Another approved claim on the same handset is the strong case; otherwise
       -- the counterpart was rejected or is still open, so flag for review only.
       CASE WHEN g.approved_claims - (CASE WHEN c.is_approved THEN 1 ELSE 0 END) > 0
            THEN 'blocker' ELSE 'warn' END,
       format('IMEI %s appears on %s claims (%s approved)', g.imei_key, g.claims, g.approved_claims),
       jsonb_build_object(
           'imei', g.imei_key,
           'claims', g.claims,
           'approved_claims', g.approved_claims,
           'other_claim_nos', to_jsonb(array_remove(g.nos, c.claim_no))
       ),
       array_remove(g.ids, c.claim_id)
FROM grp g
JOIN screen_claims c ON c.imei_key = g.imei_key AND c.is_approved;
"""

# --- Rule 4 -----------------------------------------------------------------
# One payout account receiving APPROVED claims for several different mobile numbers.
#
# Rejected claims are ignored: a rejected claim on the account was never paid, so it is
# not evidence of anything. And one person legitimately uses one account for several of
# their own numbers, so the claimant names decide:
#   names disagree  -> different people paid into one account: blocker
#   names agree     -> the same customer's own account: not reported at all
#   names unknown   -> cannot verify, so a warning rather than an accusation
# Blank names ('N/A N/A' and similar) count as unknown, never as a different person.
R_SHARED_PAYOUT_ACCOUNT = """
INSERT INTO screen_staging (claim_id, claim_no, rule_code, severity, summary, detail, related_claim_ids)
WITH grp AS (
    SELECT account_key,
           count(DISTINCT msisdn)                       AS mobiles,
           count(*)                                     AS claims,
           array_agg(claim_id ORDER BY claim_id)         AS ids,
           array_agg(claim_no ORDER BY claim_id)         AS nos,
           array_agg(DISTINCT mobile_raw)                AS mobile_list,
           array_remove(array_agg(DISTINCT claimant_name), NULL) AS names,
           portal_all_names_match(array_agg(DISTINCT claimant_name)) AS names_match
    FROM screen_claims
    WHERE account_key IS NOT NULL
      AND msisdn      IS NOT NULL
      AND is_approved
      AND NOT portal_is_placeholder(account_key)
    GROUP BY account_key
    HAVING count(DISTINCT msisdn) > 1
)
SELECT c.claim_id,
       c.claim_no,
       'SHARED_PAYOUT_ACCOUNT',
       CASE WHEN g.names_match IS FALSE THEN 'blocker' ELSE 'warn' END,
       CASE WHEN g.names_match IS FALSE
            THEN format('Payout account %s is paid for %s different people: %s',
                        g.account_key, g.mobiles, array_to_string(g.names, ' / '))
            ELSE format('Payout account %s is used by %s mobile numbers across %s approved claims, and there are no names recorded to check against',
                        g.account_key, g.mobiles, g.claims) END,
       jsonb_build_object(
           'account', g.account_key,
           'distinct_mobiles', g.mobiles,
           'approved_claims', g.claims,
           'mobile_numbers', to_jsonb(g.mobile_list),
           'claimant_names', to_jsonb(g.names),
           'names_agree', g.names_match,
           'other_claim_nos', to_jsonb(array_remove(g.nos, c.claim_no))
       ),
       array_remove(g.ids, c.claim_id)
FROM grp g
JOIN screen_claims c
  ON c.account_key = g.account_key AND c.msisdn IS NOT NULL AND c.is_approved
-- Names that agree mean one customer using their own account: nothing to report.
WHERE g.names_match IS NOT TRUE;
"""

# --- Rule 5 -----------------------------------------------------------------
# One subscription raising claims from several mobile numbers - the subscription
# identity and the claimant disagree.
R_SHARED_SUBSCRIPTION = """
INSERT INTO screen_staging (claim_id, claim_no, rule_code, severity, summary, detail, related_claim_ids)
WITH grp AS (
    SELECT subscription_id,
           count(DISTINCT msisdn)               AS mobiles,
           count(*)                             AS claims,
           array_agg(claim_id ORDER BY claim_id) AS ids,
           array_agg(claim_no ORDER BY claim_id) AS nos,
           array_agg(DISTINCT mobile_raw)        AS mobile_list
    FROM screen_claims
    WHERE subscription_id IS NOT NULL AND msisdn IS NOT NULL AND is_approved
    GROUP BY subscription_id
    HAVING count(DISTINCT msisdn) > 1
)
SELECT c.claim_id,
       c.claim_no,
       'SHARED_SUBSCRIPTION',
       'warn',
       format('Subscription is shared by %s different mobile numbers across %s claims',
              g.mobiles, g.claims),
       jsonb_build_object(
           'subscription_id', g.subscription_id,
           'distinct_mobiles', g.mobiles,
           'claims', g.claims,
           'mobile_numbers', to_jsonb(g.mobile_list),
           'other_claim_nos', to_jsonb(array_remove(g.nos, c.claim_no))
       ),
       array_remove(g.ids, c.claim_id)
FROM grp g
JOIN screen_claims c ON c.subscription_id = g.subscription_id
                     AND c.msisdn IS NOT NULL AND c.is_approved;
"""

# --- Rule 6 -----------------------------------------------------------------
# The identifiers a payout depends on are filler: a repeated-digit account, or an
# IMEI recorded on a claim that can't be a real one. Reported together because the
# finding for Finance is the same: this claim's identifiers were never captured.
R_PLACEHOLDER_IDENTIFIER = """
INSERT INTO screen_staging (claim_id, claim_no, rule_code, severity, summary, detail, related_claim_ids)
SELECT claim_id,
       claim_no,
       'PLACEHOLDER_IDENTIFIER',
       'warn',
       concat_ws('; ',
           CASE WHEN account_bad THEN format('payout account %s is not a real account number', coalesce(nullif(account_raw, ''), '(blank)')) END,
           CASE WHEN imei_bad    THEN format('IMEI %s is not a valid handset identifier', coalesce(nullif(imei_raw, ''), '(blank)')) END),
       jsonb_build_object(
           'account', account_raw,
           'account_is_placeholder', account_bad,
           'imei', imei_raw,
           'imei_is_invalid', imei_bad
       ),
       '{}'::integer[]
FROM (
    SELECT claim_id, claim_no, account_raw, imei_raw,
           (account_key IS NULL OR portal_is_placeholder(account_key)) AS account_bad,
           -- Only an attempted IMEI counts. '-' and blank mean "not applicable"
           -- (health, car and most non-handset claims never carry one), so they are
           -- a missing value rather than a bad one; six digits or more is an attempt.
           (length(portal_digits(imei_raw)) >= 6 AND imei_key IS NULL) AS imei_bad
    FROM screen_claims
) t
WHERE account_bad OR imei_bad;
"""

# --- Rule 7 -----------------------------------------------------------------
# Approved payout above the product's stated coverage. Only evaluated where the
# catalogue actually gives a coverage amount, so a missing product never produces
# a false positive.
R_AMOUNT_OVER_COVERAGE = """
INSERT INTO screen_staging (claim_id, claim_no, rule_code, severity, summary, detail, related_claim_ids)
SELECT c.claim_id,
       c.claim_no,
       'AMOUNT_OVER_COVERAGE',
       'blocker',
       format('Approved amount %s exceeds the %s coverage of %s',
              trim(to_char(c.payable, 'FM999999999990.00')),
              w.service_name,
              trim(to_char(w.coverage_amount, 'FM999999999990.00'))),
       jsonb_build_object(
           'service', w.service_name,
           'payable_amount', c.payable,
           'coverage_amount', w.coverage_amount,
           'excess_amount', c.payable - w.coverage_amount
       ),
       '{}'::integer[]
FROM screen_claims c
JOIN screen_windows w ON w.service_key = c.service_key
WHERE c.payable IS NOT NULL
  AND w.coverage_amount IS NOT NULL
  AND c.payable > w.coverage_amount;
"""

# --- Rule 8 -----------------------------------------------------------------
# Blacklisted claimant, by phone or by name. Name matching is on the normalised
# form so punctuation and casing don't let an entry slip past; it is deliberately
# an exact normalised match rather than fuzzy, because a near-name match on a
# blacklist would block innocent claimants.
R_BLACKLIST_MATCH = """
INSERT INTO screen_staging (claim_id, claim_no, rule_code, severity, summary, detail, related_claim_ids)
WITH bl AS (
    SELECT portal_norm_msisdn("PhoneNumber") AS msisdn,
           portal_norm_text("Name")          AS name_key,
           btrim(coalesce("Name", ''))       AS name_raw
    FROM digital_insurance."TestClaimBlacklists"
)
SELECT c.claim_id,
       c.claim_no,
       'BLACKLIST_MATCH',
       'blocker',
       format('Claimant is blacklisted (matched on %s)',
              string_agg(DISTINCT m.matched_on, ' and ')),
       jsonb_build_object(
           'matched_on', to_jsonb(array_agg(DISTINCT m.matched_on)),
           'blacklist_names', to_jsonb(array_remove(array_agg(DISTINCT m.name_raw), NULL))
       ),
       '{}'::integer[]
FROM screen_claims c
JOIN LATERAL (
    SELECT 'mobile number' AS matched_on, bl.name_raw FROM bl
     WHERE bl.msisdn IS NOT NULL AND bl.msisdn = c.msisdn
    UNION ALL
    SELECT 'name' AS matched_on, bl.name_raw FROM bl
     WHERE bl.name_key IS NOT NULL AND bl.name_key = portal_norm_text(c.claimant_name)
) m ON true
GROUP BY c.claim_id, c.claim_no;
"""

# --- Rule 9 -----------------------------------------------------------------
# A payout with nobody named on it. Informational: it is a data-quality problem
# that makes every other identity check weaker, not evidence of fraud by itself.
R_MISSING_CLAIMANT_NAME = """
INSERT INTO screen_staging (claim_id, claim_no, rule_code, severity, summary, detail, related_claim_ids)
SELECT claim_id,
       claim_no,
       'MISSING_CLAIMANT_NAME',
       'info',
       CASE WHEN claimant_name IS NULL
            THEN 'No claimant name on this claim, in the source or the Finance workbook'
            ELSE format('Claimant name %s carries no information', claimant_name) END,
       jsonb_build_object('claimant_name', claimant_name),
       '{}'::integer[]
FROM screen_claims
WHERE is_approved
  AND portal_name_is_blank(claimant_name);
"""

# Order matters only for the run summary; the rules are independent.
RULE_SQL = [
    ("DUP_SERVICE_CLAIM", R_DUP_SERVICE_CLAIM),
    ("DUP_SERVICE_CROSS_SUB", R_DUP_SERVICE_CROSS_SUB),
    ("DUP_IMEI", R_DUP_IMEI),
    ("SHARED_PAYOUT_ACCOUNT", R_SHARED_PAYOUT_ACCOUNT),
    ("SHARED_SUBSCRIPTION", R_SHARED_SUBSCRIPTION),
    ("PLACEHOLDER_IDENTIFIER", R_PLACEHOLDER_IDENTIFIER),
    ("AMOUNT_OVER_COVERAGE", R_AMOUNT_OVER_COVERAGE),
    ("BLACKLIST_MATCH", R_BLACKLIST_MATCH),
    ("MISSING_CLAIMANT_NAME", R_MISSING_CLAIMANT_NAME),
]

# Replace claim_flags with what this run produced: refresh what still applies,
# insert what is new, drop what no longer holds. first_seen_at is kept on update so
# "flagged since" stays truthful across runs.
SWAP_SQL = """
INSERT INTO claim_flags
    (claim_id, claim_no, rule_code, severity, summary, detail, related_claim_ids, run_id, computed_at)
SELECT claim_id, claim_no, rule_code, severity, summary, detail, related_claim_ids, %(run_id)s, now()
FROM screen_staging
ON CONFLICT (claim_id, rule_code) DO UPDATE SET
    claim_no          = EXCLUDED.claim_no,
    severity          = EXCLUDED.severity,
    summary           = EXCLUDED.summary,
    detail            = EXCLUDED.detail,
    related_claim_ids = EXCLUDED.related_claim_ids,
    run_id            = EXCLUDED.run_id,
    computed_at       = now();

DELETE FROM claim_flags f
WHERE f.run_id IS DISTINCT FROM %(run_id)s;
"""


def screen_once():
    """Run every rule and replace claim_flags with the result. Returns {rule: count}."""
    with portal_cursor() as cur:
        cur.execute(
            "INSERT INTO screening_runs (started_at) VALUES (now()) RETURNING id"
        )
        run_id = cur.fetchone()["id"]
        try:
            cur.execute(BASE_SQL)
            cur.execute(WINDOW_SQL)
            cur.execute("SELECT count(*) AS n FROM screen_claims")
            claims = cur.fetchone()["n"]

            counts = {}
            for code, sql in RULE_SQL:
                cur.execute(sql)
                counts[code] = cur.rowcount

            cur.execute(SWAP_SQL, {"run_id": run_id})
            cur.execute("SELECT count(*) AS n FROM claim_flags")
            total = cur.fetchone()["n"]

            cur.execute(
                """UPDATE screening_runs
                      SET finished_at = now(), claims_screened = %s,
                          flags_written = %s, rule_counts = %s
                    WHERE id = %s""",
                (claims, total, json.dumps(counts), run_id),
            )
            return counts
        except Exception as exc:
            # The transaction is about to roll back, so record the failure on its own
            # connection - otherwise the error row would be rolled back with it.
            _record_failure(run_id, exc)
            raise


def _record_failure(run_id, exc):
    try:
        with portal_cursor() as cur:
            cur.execute(
                "UPDATE screening_runs SET finished_at = now(), error = %s WHERE id = %s",
                (str(exc)[:2000], run_id),
            )
    except Exception:
        log.exception("could not record screening failure for run %s", run_id)


def flags_by_claim(claim_ids=None):
    """{claim_id: [flag, ...]} worst severity first, for the claims list."""
    sql = """
        SELECT claim_id, rule_code, severity, summary, detail, related_claim_ids,
               acknowledged, review_reason, reviewed_by_username, reviewed_at
        FROM claim_flag_state
        {where}
        ORDER BY claim_id,
                 array_position(ARRAY['blocker','warn','info'], severity),
                 rule_code
    """
    with portal_cursor() as cur:
        if claim_ids is None:
            cur.execute(sql.format(where=""))
        else:
            ids = list({int(i) for i in claim_ids})
            if not ids:
                return {}
            cur.execute(sql.format(where="WHERE claim_id = ANY(%s)"), (ids,))
        out = {}
        for row in cur.fetchall():
            out.setdefault(row["claim_id"], []).append(dict(row))
        return out


def risk_summary(flags):
    """Collapse one claim's flags into the fields the grid shows."""
    open_flags = [f for f in flags if not f["acknowledged"]]
    counts = {s: sum(1 for f in open_flags if f["severity"] == s) for s in SEVERITY_ORDER}
    worst = next((s for s in SEVERITY_ORDER if counts[s]), None)
    # flags_by_claim() already orders worst-first, so the first open flag is the one
    # whose name the grid should show.
    top = next((f["rule_code"] for f in open_flags if f["severity"] == worst), None)
    return {
        "risk": worst,
        "risk_rule": top,
        "risk_label": RULES.get(top, {}).get("short") if top else None,
        "open_flags": len(open_flags),
        "total_flags": len(flags),
        "counts": counts,
        "rules": [f["rule_code"] for f in open_flags],
    }


def review_flag(claim_id, rule_code, action, reason, user):
    """Acknowledge or re-open one flag. Append-only; the flag itself is untouched."""
    with portal_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM claim_flags WHERE claim_id = %s AND rule_code = %s",
            (claim_id, rule_code),
        )
        if not cur.fetchone():
            return None
        cur.execute(
            """INSERT INTO claim_flag_reviews
                 (claim_id, rule_code, action, reason, reviewed_by,
                  reviewed_by_username, reviewed_by_role)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id, reviewed_at""",
            (claim_id, rule_code, action, reason.strip(), user["id"],
             user["username"], user["role"]),
        )
        return dict(cur.fetchone())


def review_history(claim_id):
    with portal_cursor() as cur:
        cur.execute(
            """SELECT rule_code, action, reason, reviewed_by_username,
                      reviewed_by_role, reviewed_at
                 FROM claim_flag_reviews WHERE claim_id = %s
                ORDER BY reviewed_at DESC, id DESC""",
            (claim_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def latest_run():
    with portal_cursor() as cur:
        cur.execute(
            """SELECT id, started_at, finished_at, claims_screened, flags_written,
                      rule_counts, error
                 FROM screening_runs ORDER BY id DESC LIMIT 1"""
        )
        row = cur.fetchone()
        return dict(row) if row else None


def overview():
    """Counts for the screening summary: flags per rule and per severity."""
    with portal_cursor() as cur:
        cur.execute(
            """SELECT rule_code, severity,
                      count(*)                                  AS total,
                      count(*) FILTER (WHERE NOT acknowledged
                                        OR acknowledged IS NULL) AS open
                 FROM claim_flag_state
                GROUP BY rule_code, severity
                ORDER BY array_position(ARRAY['blocker','warn','info'], severity), rule_code"""
        )
        rules = [dict(r) | {"title": RULES.get(r["rule_code"], {}).get("title", r["rule_code"]),
                            "short": RULES.get(r["rule_code"], {}).get("short", r["rule_code"])}
                 for r in cur.fetchall()]
        cur.execute(
            """SELECT count(DISTINCT claim_id) AS claims
                 FROM claim_flag_state
                WHERE NOT acknowledged OR acknowledged IS NULL"""
        )
        return {"rules": rules, "flagged_claims": cur.fetchone()["claims"],
                "last_run": latest_run()}
