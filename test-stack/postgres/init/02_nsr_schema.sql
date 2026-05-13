-- =============================================================================
-- Test schema modelled around OpenG2P NSR extensions
-- (openg2p-registry-nsr-extensions). This is a SIMPLIFIED stand-alone copy
-- intended only for local end-to-end testing — it captures the analytical
-- shape (demographics, geo, livelihood, vulnerability, programs, shocks)
-- without pulling in the full registry-core dependency chain.
--
-- Source of truth for the actual schema: the SQLAlchemy models in
-- /repos/openg2p-registry-nsr-extensions/openg2p-registry-nsr-extension/src.
-- =============================================================================

\connect registry

-- -----------------------------------------------------------------------------
-- Enums — extracted from .../register_domain/models/enums.py
-- -----------------------------------------------------------------------------
CREATE TYPE gender_enum                 AS ENUM ('M','F','O');
CREATE TYPE headship_type_enum          AS ENUM ('FEMALE_HEADED','CHILD_HEADED','ELDERLY_HEADED','DISABLED_HEADED','MALE_HEADED');
CREATE TYPE residency_status_enum       AS ENUM ('USUAL_MEMBER','TEMPORARY','ABSENT');
CREATE TYPE relationship_to_head_enum   AS ENUM ('SPOUSE','CHILD','PARENT','OTHER');
CREATE TYPE preferred_contact_method_enum AS ENUM ('CALL','SMS','THROUGH_KEBELE');
CREATE TYPE citizenship_category_enum   AS ENUM ('CITIZEN','REFUGEE','IDP','RETURNEE','RESIDENT');
CREATE TYPE disability_status_enum      AS ENUM ('YES','NO','UNKNOWN');
CREATE TYPE disability_severity_enum    AS ENUM ('MILD','MODERATE','SEVERE');
CREATE TYPE displacement_status_enum    AS ENUM ('IDP','RETURNEE','REFUGEE','HOST_COMMUNITY');
CREATE TYPE pastoralist_class_enum      AS ENUM ('PASTORALIST','SEMI_PASTORALIST','SETTLED');
CREATE TYPE dwelling_type_enum          AS ENUM ('PERMANENT','SEMI','TEMPORARY');
CREATE TYPE tenure_status_enum          AS ENUM ('OWNED','RENTED');
CREATE TYPE employment_status_enum      AS ENUM ('EMPLOYED','SELF_EMPLOYED','UNEMPLOYED','STUDENT','OTHER');
CREATE TYPE shock_type_enum             AS ENUM ('DROUGHT','FLOOD','CONFLICT','ILLNESS','OTHER');
CREATE TYPE livelihood_enum             AS ENUM ('CROP_FARMING','LIVESTOCK','PASTORALIST','MIXED','WAGE_LABOR','BUSINESS','OTHER');
CREATE TYPE rid_status_enum             AS ENUM ('PENDING','VERIFIED','REJECTED');
CREATE TYPE verification_method_enum    AS ENUM ('FAYDA','MANUAL','UNVERIFIED');

-- -----------------------------------------------------------------------------
-- Households
-- -----------------------------------------------------------------------------
CREATE TABLE g2p_register_households (
    id                       BIGSERIAL PRIMARY KEY,
    rid                      VARCHAR(64),
    headship_type            headship_type_enum,
    household_size           INTEGER,
    female_headed            BOOLEAN,
    has_disability_member    BOOLEAN,
    has_idp_member           BOOLEAN,
    region_code              VARCHAR(16),
    zone_code                VARCHAR(16),
    woreda_code              VARCHAR(16),
    kebele_code              VARCHAR(16),
    pmt_score                NUMERIC(6,2),
    monthly_income           NUMERIC(12,2),
    coping_strategies_index  INTEGER,
    verification_method      verification_method_enum,
    registered_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    fayda_verified           BOOLEAN     NOT NULL DEFAULT false,
    address_line             VARCHAR(256),               -- PII
    contact_phone            VARCHAR(32),                -- PII
    head_full_name           VARCHAR(256)                -- PII
);
CREATE INDEX ix_hh_region   ON g2p_register_households(region_code);
CREATE INDEX ix_hh_zone     ON g2p_register_households(zone_code);
CREATE INDEX ix_hh_woreda   ON g2p_register_households(woreda_code);
CREATE INDEX ix_hh_pmt      ON g2p_register_households(pmt_score);
CREATE INDEX ix_hh_registered_at ON g2p_register_households(registered_at);

-- -----------------------------------------------------------------------------
-- Individuals — the central analytical table.
-- -----------------------------------------------------------------------------
CREATE TABLE g2p_register_individuals (
    id                          BIGSERIAL PRIMARY KEY,
    household_id                BIGINT REFERENCES g2p_register_households(id) ON DELETE CASCADE,
    rid                         VARCHAR(64),
    rid_status                  rid_status_enum DEFAULT 'PENDING',

    -- PII
    full_name                   VARCHAR(256),
    primary_phone               VARCHAR(32),
    alternate_phone             VARCHAR(32),
    address_descriptor          VARCHAR(512),
    contact_person_name         VARCHAR(256),
    gps_latitude                NUMERIC(9,6),
    gps_longitude               NUMERIC(9,6),

    -- Geo (safe)
    region_code                 VARCHAR(16),
    zone_code                   VARCHAR(16),
    woreda_code                 VARCHAR(16),
    kebele_code                 VARCHAR(16),
    locality_ea_code            VARCHAR(16),

    -- Demographics
    gender                      gender_enum,
    estimated_age               INTEGER,
    age_bracket                 VARCHAR(8),            -- denormalized for fast group-by
    relationship_to_head        relationship_to_head_enum,
    residency_status            residency_status_enum,
    citizenship_category        citizenship_category_enum,
    is_head                     BOOLEAN DEFAULT false,

    -- Livelihood
    primary_livelihood          livelihood_enum,
    secondary_livelihood        livelihood_enum,
    employment_status           employment_status_enum,
    coping_strategies_index     INTEGER,
    land_access                 BOOLEAN,
    land_size                   NUMERIC(8,2),

    -- Vulnerability
    disability_status           disability_status_enum DEFAULT 'UNKNOWN',
    disability_severity         disability_severity_enum,
    orphanhood_flag             BOOLEAN DEFAULT false,
    chronic_illness_flag        BOOLEAN DEFAULT false,
    displacement_status         displacement_status_enum,
    pastoralist_classification  pastoralist_class_enum,
    high_mobility_indicator     BOOLEAN DEFAULT false,
    plw_status                  BOOLEAN DEFAULT false,   -- pregnant/lactating

    -- Per-individual financials (often nullable; head's row typically populated)
    monthly_income              NUMERIC(12,2),
    pmt_score                   NUMERIC(6,2),

    fayda_token                 VARCHAR(64),             -- PII
    fayda_verified              BOOLEAN DEFAULT false,
    verification_method         verification_method_enum DEFAULT 'UNVERIFIED',

    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_ind_household       ON g2p_register_individuals(household_id);
CREATE INDEX ix_ind_region          ON g2p_register_individuals(region_code);
CREATE INDEX ix_ind_zone            ON g2p_register_individuals(zone_code);
CREATE INDEX ix_ind_woreda          ON g2p_register_individuals(woreda_code);
CREATE INDEX ix_ind_kebele          ON g2p_register_individuals(kebele_code);
CREATE INDEX ix_ind_gender          ON g2p_register_individuals(gender);
CREATE INDEX ix_ind_age_bracket     ON g2p_register_individuals(age_bracket);
CREATE INDEX ix_ind_pmt             ON g2p_register_individuals(pmt_score);
CREATE INDEX ix_ind_disability      ON g2p_register_individuals(disability_status);
CREATE INDEX ix_ind_livelihood      ON g2p_register_individuals(primary_livelihood);
CREATE INDEX ix_ind_created_at      ON g2p_register_individuals(created_at);

-- -----------------------------------------------------------------------------
-- Individual shocks
-- -----------------------------------------------------------------------------
CREATE TABLE g2p_register_individual_shocks (
    id              BIGSERIAL PRIMARY KEY,
    individual_id   BIGINT REFERENCES g2p_register_individuals(id) ON DELETE CASCADE,
    shock_type      shock_type_enum NOT NULL,
    severity        VARCHAR(16),
    occurred_at     DATE,
    csi_at_shock    INTEGER,
    csi_after       INTEGER,
    notes           TEXT
);
CREATE INDEX ix_shock_individual ON g2p_register_individual_shocks(individual_id);
CREATE INDEX ix_shock_type       ON g2p_register_individual_shocks(shock_type);
CREATE INDEX ix_shock_occurred   ON g2p_register_individual_shocks(occurred_at);

-- -----------------------------------------------------------------------------
-- Individual programs (current + legacy)
-- -----------------------------------------------------------------------------
CREATE TABLE g2p_register_individual_programs (
    id              BIGSERIAL PRIMARY KEY,
    individual_id   BIGINT REFERENCES g2p_register_individuals(id) ON DELETE CASCADE,
    program_code    VARCHAR(32) NOT NULL,             -- e.g. PSNP, CBHI, PSSN
    enrolled_at     DATE,
    is_legacy       BOOLEAN DEFAULT false,
    status          VARCHAR(16) DEFAULT 'ACTIVE'
);
CREATE INDEX ix_indprog_individual ON g2p_register_individual_programs(individual_id);
CREATE INDEX ix_indprog_code       ON g2p_register_individual_programs(program_code);

-- -----------------------------------------------------------------------------
-- Household housing & assets — simplified to commonly-queried fields
-- -----------------------------------------------------------------------------
CREATE TABLE g2p_register_household_housing (
    id                BIGSERIAL PRIMARY KEY,
    household_id      BIGINT REFERENCES g2p_register_households(id) ON DELETE CASCADE,
    dwelling_type     dwelling_type_enum,
    tenure_status     tenure_status_enum,
    rooms             INTEGER,
    roof_material     VARCHAR(32),
    wall_material     VARCHAR(32),
    floor_material    VARCHAR(32),
    water_source      VARCHAR(32),
    electricity       BOOLEAN,
    sanitation_type   VARCHAR(32)
);
CREATE INDEX ix_house_household ON g2p_register_household_housing(household_id);
