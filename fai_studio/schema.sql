-- ADMI FAI Studio schema (PostgreSQL 13+). Idempotent: safe to run repeatedly.

CREATE TABLE IF NOT EXISTS app_meta (
    key   text PRIMARY KEY,
    value text
);

CREATE TABLE IF NOT EXISTS reports (
    id                    serial PRIMARY KEY,
    report_type           text NOT NULL DEFAULT 'AS9102 FAI',     -- see report_types.py
    report_number         text NOT NULL DEFAULT '',
    part_number           text NOT NULL DEFAULT '',
    part_name             text NOT NULL DEFAULT '',
    part_rev              text NOT NULL DEFAULT '',
    serial_number         text NOT NULL DEFAULT '',
    drawing_number        text NOT NULL DEFAULT '',
    drawing_rev           text NOT NULL DEFAULT '',
    additional_changes    text NOT NULL DEFAULT '',
    mfg_process_ref       text NOT NULL DEFAULT '',
    organization_name     text NOT NULL DEFAULT '',
    supplier_code         text NOT NULL DEFAULT '',
    customer              text NOT NULL DEFAULT '',
    po_number             text NOT NULL DEFAULT '',
    job_number            text NOT NULL DEFAULT '',
    quantity              text NOT NULL DEFAULT '',
    sample_size           text NOT NULL DEFAULT '',
    detail_or_assembly    text NOT NULL DEFAULT 'Detail',        -- 'Detail' | 'Assembly'
    fai_type              text NOT NULL DEFAULT 'Full',          -- 'Full' | 'Partial'
    partial_baseline      text NOT NULL DEFAULT '',
    partial_reason        text NOT NULL DEFAULT '',
    units                 text NOT NULL DEFAULT 'in',
    tol_1dec              text NOT NULL DEFAULT '0.1',
    tol_2dec              text NOT NULL DEFAULT '0.01',
    tol_3dec              text NOT NULL DEFAULT '0.005',
    tol_4dec              text NOT NULL DEFAULT '0.0005',
    tol_angle             text NOT NULL DEFAULT '0.5',
    status                text NOT NULL DEFAULT 'Draft',         -- Draft | In Review | Approved | Rejected
    prepared_by           text NOT NULL DEFAULT '',
    prepared_date         text NOT NULL DEFAULT '',
    reviewed_by           text NOT NULL DEFAULT '',
    reviewed_date         text NOT NULL DEFAULT '',
    customer_approval     text NOT NULL DEFAULT '',
    customer_approval_date text NOT NULL DEFAULT '',
    comments              text NOT NULL DEFAULT '',
    locked                boolean NOT NULL DEFAULT false,
    created_by            text NOT NULL DEFAULT '',
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_by            text NOT NULL DEFAULT '',
    updated_at            timestamptz NOT NULL DEFAULT now(),
    version               integer NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS components (          -- AS9102 Form 1 index of part numbers / parts list
    id               serial PRIMARY KEY,
    report_id        integer NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    seq              integer NOT NULL DEFAULT 0,
    item_no          text NOT NULL DEFAULT '',
    part_number      text NOT NULL DEFAULT '',
    part_name        text NOT NULL DEFAULT '',
    qty              text NOT NULL DEFAULT '',
    serial_number    text NOT NULL DEFAULT '',
    fai_report_number text NOT NULL DEFAULT '',
    notes            text NOT NULL DEFAULT '',
    updated_by       text NOT NULL DEFAULT '',
    updated_at       timestamptz NOT NULL DEFAULT now(),
    version          integer NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS materials (           -- AS9102 Form 2
    id                  serial PRIMARY KEY,
    report_id           integer NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    seq                 integer NOT NULL DEFAULT 0,
    category            text NOT NULL DEFAULT 'Material',   -- Material | Special Process | Functional Test | Other
    name                text NOT NULL DEFAULT '',
    spec_number         text NOT NULL DEFAULT '',
    code                text NOT NULL DEFAULT '',
    supplier            text NOT NULL DEFAULT '',
    supplier_code       text NOT NULL DEFAULT '',
    customer_approval   text NOT NULL DEFAULT '',            -- Yes | No | N/A
    coc_number          text NOT NULL DEFAULT '',
    comments            text NOT NULL DEFAULT '',
    updated_by          text NOT NULL DEFAULT '',
    updated_at          timestamptz NOT NULL DEFAULT now(),
    version             integer NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS attachments (
    id                serial PRIMARY KEY,
    report_id         integer NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    kind              text NOT NULL DEFAULT 'Other',          -- Drawing | Photo | Parts List | Notes List | Other
    filename          text NOT NULL,
    mime              text NOT NULL DEFAULT '',
    size_bytes        bigint NOT NULL DEFAULT 0,
    sha256            text NOT NULL DEFAULT '',
    page_count        integer NOT NULL DEFAULT 1,
    description       text NOT NULL DEFAULT '',
    include_in_report boolean NOT NULL DEFAULT true,
    seq               integer NOT NULL DEFAULT 0,
    data              bytea,
    uploaded_by       text NOT NULL DEFAULT '',
    uploaded_at       timestamptz NOT NULL DEFAULT now(),
    updated_by        text NOT NULL DEFAULT '',
    updated_at        timestamptz NOT NULL DEFAULT now(),
    version           integer NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS characteristics (     -- AS9102 Form 3
    id                 serial PRIMARY KEY,
    report_id          integer NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    char_no            integer NOT NULL DEFAULT 0,
    ref_location       text NOT NULL DEFAULT '',
    designator         text NOT NULL DEFAULT '',   -- Key | Critical | Major | Minor | ''
    char_type          text NOT NULL DEFAULT 'Dimension',
    requirement        text NOT NULL DEFAULT '',
    nominal            text NOT NULL DEFAULT '',
    tol_plus           text NOT NULL DEFAULT '',
    tol_minus          text NOT NULL DEFAULT '',
    lower_limit        text NOT NULL DEFAULT '',
    upper_limit        text NOT NULL DEFAULT '',
    results            text NOT NULL DEFAULT '',
    disposition        text NOT NULL DEFAULT '',   -- Accept | Reject | N/A | ''
    tooling            text NOT NULL DEFAULT '',
    nonconformance     text NOT NULL DEFAULT '',
    comments           text NOT NULL DEFAULT '',
    balloon_attachment_id integer REFERENCES attachments(id) ON DELETE SET NULL,
    balloon_page       integer,
    balloon_x          double precision,
    balloon_y          double precision,
    updated_by         text NOT NULL DEFAULT '',
    updated_at         timestamptz NOT NULL DEFAULT now(),
    version            integer NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS gages (
    id           serial PRIMARY KEY,
    gage_id      text NOT NULL DEFAULT '',
    description  text NOT NULL DEFAULT '',
    gage_type    text NOT NULL DEFAULT '',
    cal_due      text NOT NULL DEFAULT '',     -- YYYY-MM-DD
    active       boolean NOT NULL DEFAULT true,
    updated_by   text NOT NULL DEFAULT '',
    updated_at   timestamptz NOT NULL DEFAULT now(),
    version      integer NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          bigserial PRIMARY KEY,
    report_id   integer,
    table_name  text NOT NULL,
    record_id   integer,
    action      text NOT NULL,
    field       text,
    old_value   text,
    new_value   text,
    user_name   text NOT NULL DEFAULT '',
    host        text NOT NULL DEFAULT '',
    at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS coc_items (           -- Certificate of Conformance: material / process / test certifications
    id           serial PRIMARY KEY,
    report_id    integer NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    seq          integer NOT NULL DEFAULT 0,
    item_type    text NOT NULL DEFAULT 'Material',     -- Material | Process | Test | Purchased
    description  text NOT NULL DEFAULT '',
    vendor_name  text NOT NULL DEFAULT '',
    vendor_info  text NOT NULL DEFAULT '',             -- address block printed under the vendor name
    cert_lot     text NOT NULL DEFAULT '',             -- PO / cert / lot numbers
    source       text NOT NULL DEFAULT '',             -- where the row came from (BOM FN 004, note 20 …)
    updated_by   text NOT NULL DEFAULT '',
    updated_at   timestamptz NOT NULL DEFAULT now(),
    version      integer NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS vendors (             -- shared vendor list for COCs (approved supplier list)
    id           serial PRIMARY KEY,
    name         text NOT NULL DEFAULT '',
    address      text NOT NULL DEFAULT '',
    cage         text NOT NULL DEFAULT '',
    vendor_type  text NOT NULL DEFAULT '',             -- Material | Process | Hardware | Test | Other
    approved     boolean NOT NULL DEFAULT true,
    notes        text NOT NULL DEFAULT '',
    active       boolean NOT NULL DEFAULT true,
    updated_by   text NOT NULL DEFAULT '',
    updated_at   timestamptz NOT NULL DEFAULT now(),
    version      integer NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_coc_items_report ON coc_items(report_id);
CREATE INDEX IF NOT EXISTS ix_components_report ON components(report_id);
CREATE INDEX IF NOT EXISTS ix_materials_report ON materials(report_id);
CREATE INDEX IF NOT EXISTS ix_attachments_report ON attachments(report_id);
CREATE INDEX IF NOT EXISTS ix_characteristics_report ON characteristics(report_id);
CREATE INDEX IF NOT EXISTS ix_audit_report ON audit_log(report_id, at DESC);
CREATE INDEX IF NOT EXISTS ix_reports_part ON reports(part_number);

-- ---------------------------------------------------------------------
-- Live sync: every change is broadcast on channel 'fai_changes' so all
-- running workstations refresh immediately.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fai_notify() RETURNS trigger AS $$
DECLARE
    rid integer;
    rec_id integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        rec_id := OLD.id;
        IF TG_TABLE_NAME = 'reports' THEN rid := OLD.id;
        ELSIF TG_TABLE_NAME IN ('gages', 'vendors') THEN rid := NULL;
        ELSE rid := OLD.report_id; END IF;
    ELSE
        rec_id := NEW.id;
        IF TG_TABLE_NAME = 'reports' THEN rid := NEW.id;
        ELSIF TG_TABLE_NAME IN ('gages', 'vendors') THEN rid := NULL;
        ELSE rid := NEW.report_id; END IF;
    END IF;
    PERFORM pg_notify('fai_changes', json_build_object(
        't', TG_TABLE_NAME, 'op', TG_OP, 'id', rec_id, 'report_id', rid,
        'client', coalesce(current_setting('fai.client_id', true), ''))::text);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['reports','components','materials','attachments','characteristics','gages',
                             'coc_items','vendors'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%1$s_notify ON %1$s', t);
        EXECUTE format('CREATE TRIGGER trg_%1$s_notify AFTER INSERT OR UPDATE OR DELETE ON %1$s
                        FOR EACH ROW EXECUTE FUNCTION fai_notify()', t);
    END LOOP;
END $$;

UPDATE characteristics SET balloon_page = NULL, balloon_x = NULL, balloon_y = NULL
 WHERE balloon_attachment_id IS NULL AND balloon_x IS NOT NULL;

INSERT INTO app_meta(key, value) VALUES ('schema_version', '1') ON CONFLICT (key) DO NOTHING;  -- upgraded at the end
INSERT INTO app_meta(key, value) VALUES
    ('company_name', 'Aerospace & Defense Manufacturing Inc.'),
    ('company_short', 'ADMI'),
    ('cage_code', '97WZ5'),
    ('company_address', '42011 Avenida Alvarado Suite A, Temecula, CA 92590'),
    ('company_phone', ''),
    ('quality_statement', 'AS9100 / ISO 9001 Certified  |  ITAR Registered')
ON CONFLICT (key) DO NOTHING;

-- v1.2: whole-number title-block tolerance for metric drawings ("X ± 2"); blank = none
ALTER TABLE reports ADD COLUMN IF NOT EXISTS tol_0dec text NOT NULL DEFAULT '';
-- v1.2: per-balloon size (radius as a fraction of the sheet's long side); NULL = standard size
ALTER TABLE characteristics ADD COLUMN IF NOT EXISTS balloon_size double precision;

-- v1.4: offline work (check-out / check-in). While a report is checked out to a PC, only that PC changes it.
ALTER TABLE reports ADD COLUMN IF NOT EXISTS checked_out_by text NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS checked_out_host text NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS checked_out_at timestamptz;

-- v1.5: Certificate of Conformance header (per report / shipment)
ALTER TABLE reports ADD COLUMN IF NOT EXISTS coc_pack_list text NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS coc_qty_shipped text NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS coc_date_shipped text NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS coc_part_description text NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS coc_certified_by text NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS coc_title text NOT NULL DEFAULT '';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS coc_date text NOT NULL DEFAULT '';

-- v1.7: multi-part inspection (Inspection / Standard reports): number of parts, single or low & high readings,
-- serial number per part (one per line); per-characteristic readings per part as JSON (see multipart.py)
ALTER TABLE reports ADD COLUMN IF NOT EXISTS part_count integer NOT NULL DEFAULT 1;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS reading_mode text NOT NULL DEFAULT 'single';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS part_serials text NOT NULL DEFAULT '';
ALTER TABLE characteristics ADD COLUMN IF NOT EXISTS part_readings text NOT NULL DEFAULT '';

INSERT INTO app_meta(key, value) VALUES
    ('coc_statement', 'We hereby certify that the items listed below were manufactured, inspected, and tested in '
                      'accordance with all applicable drawings, specifications, purchase order requirements, and '
                      'quality standards.'),
    ('coc_footer', 'This Certificate of Conformance certifies that the above referenced products meet all applicable '
                   'purchase order, drawing, specification, and quality system requirements. Records are maintained '
                   'in accordance with AS9100 and customer requirements.'),
    ('coc_default_title', 'QC')
ON CONFLICT (key) DO NOTHING;

-- vendors already used on ADMI certificates (only added when the list is empty)
INSERT INTO vendors(name, address, cage, vendor_type)
SELECT * FROM (VALUES
    ('MCMASTER CARR', '9630 NORWALK BLVD' || chr(10) || 'SANTA FE SPRINGS CA 90670-2932', '39428', 'Hardware'),
    ('ThyssenKrupp Materials NA, Inc.', 'PO BOX 894213' || chr(10) || 'LOS ANGELES, CA 90189-4213', '', 'Material'),
    ('COAST ALUMINUM, INC.', '10628 FULTON WELLS AVE' || chr(10) || 'SANTA FE SPRINGS, CA 90670', '', 'Material'),
    ('FABWEST', '43089 BUSINESS PARK DRIVE' || chr(10) || 'TEMECULA CA 92590', '', 'Process'),
    ('TEMECULA QUALITY PLATING', '42147 ROICK DRIVE' || chr(10) || 'TEMECULA, CA 92590', '', 'Process')
) AS v(name, address, cage, vendor_type)
WHERE NOT EXISTS (SELECT 1 FROM vendors);

-- drawing reader rules: a shared library that grows without software updates (see rules.py)
CREATE TABLE IF NOT EXISTS reader_rules (
    id              bigserial PRIMARY KEY,
    name            text NOT NULL,
    rule_type       text NOT NULL,
    params          text NOT NULL DEFAULT '{}',
    enabled         boolean NOT NULL DEFAULT true,
    priority        integer NOT NULL DEFAULT 100,
    scope_text      text NOT NULL DEFAULT '',
    scope_producer  text NOT NULL DEFAULT '',
    notes           text NOT NULL DEFAULT '',
    created_by      text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text NOT NULL DEFAULT '',
    updated_at      timestamptz NOT NULL DEFAULT now(),
    version         integer NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS reader_rule_examples (
    id          bigserial PRIMARY KEY,
    rule_id     bigint REFERENCES reader_rules(id) ON DELETE CASCADE,
    filename    text NOT NULL,
    data        bytea NOT NULL,
    expected    text NOT NULL DEFAULT '[]',
    notes       text NOT NULL DEFAULT '',
    last_result text NOT NULL DEFAULT '',
    added_by    text NOT NULL DEFAULT '',
    added_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reader_rule_examples_rule ON reader_rule_examples(rule_id);
CREATE TABLE IF NOT EXISTS reader_rule_log (
    id          bigserial PRIMARY KEY,
    rule_id     bigint,
    rule_name   text NOT NULL DEFAULT '',
    action      text NOT NULL,
    before      text,
    after       text,
    user_name   text NOT NULL DEFAULT '',
    host        text NOT NULL DEFAULT '',
    at          timestamptz NOT NULL DEFAULT now()
);

-- v1.8: OCR learning memory (see ocr_learn.py) - corrections made to OCR reads, shared by every workstation
CREATE TABLE IF NOT EXISTS ocr_learned (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL DEFAULT 'whole',
    wrong       text NOT NULL,
    "right"     text NOT NULL,
    status      text NOT NULL DEFAULT 'learning',
    hits        integer NOT NULL DEFAULT 0,
    applied     integer NOT NULL DEFAULT 0,
    confirmed   integer NOT NULL DEFAULT 0,
    rejected    integer NOT NULL DEFAULT 0,
    sources     text NOT NULL DEFAULT '[]',
    notes       text NOT NULL DEFAULT '',
    created_by  text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ocr_learned_pair ON ocr_learned(kind, wrong, "right");
ALTER TABLE characteristics ADD COLUMN IF NOT EXISTS ocr_read text NOT NULL DEFAULT '';
ALTER TABLE characteristics ADD COLUMN IF NOT EXISTS ocr_fix_ids text NOT NULL DEFAULT '';

-- schema version (bump together with SCHEMA_VERSION in app.py whenever this file changes)
INSERT INTO app_meta(key, value) VALUES ('schema_version', '7')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
WHERE app_meta.value ~ '^[0-9]+$' AND app_meta.value::int < EXCLUDED.value::int;
