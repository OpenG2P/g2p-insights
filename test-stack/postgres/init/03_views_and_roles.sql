-- =============================================================================
-- PII-safe views, materialised analytical views, and the read-only role
-- the insight backend connects with.
-- =============================================================================

\connect registry

-- -----------------------------------------------------------------------------
-- PII-safe view of individuals: no full_name, phones, fayda, GPS, address.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_individuals_safe AS
SELECT
    i.id,
    i.household_id,
    i.region_code,
    i.zone_code,
    i.woreda_code,
    i.kebele_code,
    i.locality_ea_code,
    i.gender,
    i.estimated_age,
    i.age_bracket,
    i.relationship_to_head,
    i.residency_status,
    i.citizenship_category,
    i.is_head,
    i.primary_livelihood,
    i.secondary_livelihood,
    i.employment_status,
    i.coping_strategies_index,
    i.land_access,
    i.land_size,
    i.disability_status,
    i.disability_severity,
    i.orphanhood_flag,
    i.chronic_illness_flag,
    i.displacement_status,
    i.pastoralist_classification,
    i.high_mobility_indicator,
    i.plw_status,
    i.monthly_income,
    i.pmt_score,
    i.fayda_verified,
    i.verification_method,
    i.created_at
FROM g2p_register_individuals i;

CREATE OR REPLACE VIEW v_households_safe AS
SELECT
    h.id,
    h.headship_type,
    h.household_size,
    h.female_headed,
    h.has_disability_member,
    h.has_idp_member,
    h.region_code,
    h.zone_code,
    h.woreda_code,
    h.kebele_code,
    h.pmt_score,
    h.monthly_income,
    h.coping_strategies_index,
    h.verification_method,
    h.registered_at,
    h.fayda_verified
FROM g2p_register_households h;

-- -----------------------------------------------------------------------------
-- Materialised view: pre-joined individual summary (the default analytical
-- entity). Refresh nightly in production.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW mv_individuals_summary AS
SELECT
    i.id,
    i.household_id,
    i.gender,
    i.estimated_age,
    i.age_bracket,
    i.region_code,
    i.zone_code,
    i.woreda_code,
    i.kebele_code,
    i.primary_livelihood,
    i.employment_status,
    i.coping_strategies_index,
    i.disability_status,
    i.displacement_status,
    i.pastoralist_classification,
    i.plw_status,
    i.orphanhood_flag,
    i.chronic_illness_flag,
    i.pmt_score,
    i.monthly_income,
    i.fayda_verified,
    i.verification_method,
    h.headship_type,
    h.household_size,
    h.female_headed,
    h.pmt_score        AS household_pmt_score,
    h.monthly_income   AS household_monthly_income,
    i.created_at
FROM g2p_register_individuals i
LEFT JOIN g2p_register_households h ON h.id = i.household_id;

CREATE INDEX ix_mv_ind_region   ON mv_individuals_summary(region_code);
CREATE INDEX ix_mv_ind_zone     ON mv_individuals_summary(zone_code);
CREATE INDEX ix_mv_ind_woreda   ON mv_individuals_summary(woreda_code);
CREATE INDEX ix_mv_ind_gender   ON mv_individuals_summary(gender);
CREATE INDEX ix_mv_ind_age      ON mv_individuals_summary(age_bracket);
CREATE INDEX ix_mv_ind_pmt      ON mv_individuals_summary(pmt_score);
CREATE INDEX ix_mv_ind_livelihood ON mv_individuals_summary(primary_livelihood);

-- -----------------------------------------------------------------------------
-- Materialised view: per-woreda pre-aggregates. ~hundreds of rows. Use this
-- first for any district-level question — 10⁴× faster than the source.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW mv_district_aggregates AS
SELECT
    region_code,
    zone_code,
    woreda_code,
    gender,
    age_bracket,
    COUNT(*)                                       AS total,
    AVG(pmt_score)::NUMERIC(6,2)                   AS avg_pmt,
    AVG(monthly_income)::NUMERIC(12,2)             AS avg_income,
    COUNT(*) FILTER (WHERE disability_status='YES') AS disability_count,
    COUNT(*) FILTER (WHERE plw_status)              AS plw_count,
    COUNT(*) FILTER (WHERE displacement_status='IDP') AS idp_count
FROM mv_individuals_summary
GROUP BY region_code, zone_code, woreda_code, gender, age_bracket;

CREATE INDEX ix_mv_dist_region ON mv_district_aggregates(region_code);

-- -----------------------------------------------------------------------------
-- insight_ai role — read-only, NEVER granted on raw tables that carry PII.
-- -----------------------------------------------------------------------------
DROP ROLE IF EXISTS insight_ai;
CREATE ROLE insight_ai LOGIN PASSWORD 'insight';
GRANT CONNECT ON DATABASE registry TO insight_ai;
GRANT USAGE   ON SCHEMA public TO insight_ai;

GRANT SELECT ON v_individuals_safe        TO insight_ai;
GRANT SELECT ON v_households_safe         TO insight_ai;
GRANT SELECT ON mv_individuals_summary    TO insight_ai;
GRANT SELECT ON mv_district_aggregates    TO insight_ai;

-- Explicit guardrails.
ALTER ROLE insight_ai SET statement_timeout = '30s';
ALTER ROLE insight_ai SET lock_timeout = '5s';
ALTER ROLE insight_ai SET idle_in_transaction_session_timeout = '60s';
