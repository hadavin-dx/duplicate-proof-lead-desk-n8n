-- Applied by db.init_schema: WAL is set first outside any transaction; this text runs inside
-- "BEGIN IMMEDIATE; ... PRAGMA user_version=1; COMMIT;".
-- A lead book kept from a previous run is brought up to this schema by db._migrate: it adds missing columns, the view,
-- indexes and triggers, never new CHECKs, so a kept book keeps the CHECKs it was created with.
-- Per-connection pragmas (foreign_keys, recursive_triggers, synchronous) are set by db.connect, not here.
CREATE TABLE IF NOT EXISTS receipts (
  receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL CHECK (source IN ('form','webhook')),
  source_ref TEXT NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 128),
  received_at INTEGER NOT NULL,
  payload TEXT NOT NULL,                  -- canonical JSON text of the request's payload object, never changes
  attempts INTEGER NOT NULL DEFAULT 1,    -- how many times this request number arrived
  outcome TEXT CHECK (outcome IN ('new','duplicate','review')),   -- NULL = open; this is the "verdict"
  outcome_lead_id TEXT,
  outcome_reason TEXT,
  differing_fields TEXT,                  -- JSON array of field NAMES, always set with a duplicate verdict (may be '[]')
  decided_at INTEGER,
  payload_conflict INTEGER NOT NULL DEFAULT 0,   -- 1 = the request number came again with a different payload (the first answer stands)
  UNIQUE (source, source_ref),
  CHECK (outcome IS NULL OR outcome <> 'new' OR outcome_lead_id IS NOT NULL)   -- a `new` verdict always names its lead
);
CREATE TRIGGER IF NOT EXISTS receipts_immutable
  BEFORE UPDATE OF receipt_id, source, source_ref, received_at, payload ON receipts
  BEGIN SELECT RAISE(ABORT,'receipt fields never change'); END;
CREATE TRIGGER IF NOT EXISTS receipts_outcome_once
  BEFORE UPDATE OF outcome, outcome_lead_id, outcome_reason, differing_fields, decided_at ON receipts
  WHEN OLD.outcome IS NOT NULL
  BEGIN SELECT RAISE(ABORT,'a verdict is written once'); END;     -- the first write (OLD.outcome NULL) passes; one UPDATE sets all five columns
CREATE TRIGGER IF NOT EXISTS receipts_no_delete
  BEFORE DELETE ON receipts
  BEGIN SELECT RAISE(ABORT,'receipts are never deleted'); END;

CREATE TABLE IF NOT EXISTS leads (
  lead_id TEXT NOT NULL PRIMARY KEY,             -- 'L' || 12 random hex characters
  receipt_id INTEGER NOT NULL UNIQUE REFERENCES receipts(receipt_id),
  source TEXT NOT NULL,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  email TEXT NOT NULL,                    -- as typed, trimmed
  email_key TEXT NOT NULL CHECK (length(email_key) BETWEEN 3 AND 254),   -- trim + lower case
  phone TEXT, company TEXT, message TEXT, -- NULL when empty
  created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS leads_email_key_uq ON leads(email_key);
CREATE TRIGGER IF NOT EXISTS leads_no_update BEFORE UPDATE ON leads
  BEGIN SELECT RAISE(ABORT,'lead lines are write-once'); END;
CREATE TRIGGER IF NOT EXISTS leads_no_delete BEFORE DELETE ON leads
  BEGIN SELECT RAISE(ABORT,'lead lines are write-once'); END;

CREATE TABLE IF NOT EXISTS followups (
  lead_id TEXT NOT NULL PRIMARY KEY REFERENCES leads(lead_id),
  state TEXT NOT NULL CHECK (state IN ('pending','sending','sent','failed','unknown')),
  ai_label TEXT CHECK (ai_label IN ('hot','warm','cold')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at INTEGER NOT NULL,
  sent_at INTEGER,
  crm_done INTEGER NOT NULL DEFAULT 0,    -- 0 = not asked, 2 = a put was started (intent mark, written first), 1 = the CRM row was put
  auto_attempts INTEGER NOT NULL DEFAULT 0,  -- automatic tries since the last person-started Retry (cap in gate.py)
  ambiguous_prior INTEGER NOT NULL DEFAULT 0, -- 1 = an earlier send may have left (crash or unclear end): never send again blind
  send_started INTEGER NOT NULL DEFAULT 0    -- 1 = written just BEFORE outbox.send; a claim clears it; 0 on a `sending` row = nothing was sent
);
CREATE TRIGGER IF NOT EXISTS followups_sent_final BEFORE UPDATE ON followups
  WHEN OLD.state = 'sent'
  BEGIN SELECT RAISE(ABORT,'a sent follow-up is final'); END;

CREATE TRIGGER IF NOT EXISTS followups_no_delete BEFORE DELETE ON followups
  BEGIN SELECT RAISE(ABORT,'follow-up lines are never deleted'); END;   -- also stops INSERT OR REPLACE (recursive_triggers=ON)

-- approval mode (DESK_LATER_MODE=approval): one decision per later message, ever; the first writer wins
CREATE TABLE IF NOT EXISTS later_decisions (
  receipt_id    INTEGER NOT NULL PRIMARY KEY REFERENCES receipts(receipt_id),   -- the later message
  decision      TEXT NOT NULL CHECK (decision IN ('approved','denied')),
  approver_id   TEXT NOT NULL CHECK (length(approver_id) BETWEEN 1 AND 32),     -- from the approvers list (constants.py)
  approver_name TEXT NOT NULL CHECK (length(approver_name) BETWEEN 1 AND 64),   -- the list's name at decision time
  channel       TEXT NOT NULL CHECK (channel IN ('portal')),
  decided_at    INTEGER NOT NULL                                                -- desk clock, whole seconds UTC
);
CREATE TRIGGER IF NOT EXISTS later_decisions_no_update BEFORE UPDATE ON later_decisions
  BEGIN SELECT RAISE(ABORT,'a decision is never changed'); END;
CREATE TRIGGER IF NOT EXISTS later_decisions_no_delete BEFORE DELETE ON later_decisions
  BEGIN SELECT RAISE(ABORT,'a decision is never deleted'); END;   -- also stops INSERT OR REPLACE (recursive_triggers=ON)

-- lead owner, lead stage and reminder (DESK_LEADFLOW_FILE): written only when the feature is on; all times are
-- INTEGER epoch seconds from the desk clock
CREATE TABLE IF NOT EXISTS lead_state (
  lead_id        TEXT NOT NULL PRIMARY KEY REFERENCES leads(lead_id),
  owner_id       TEXT NOT NULL,
  owner_name     TEXT NOT NULL CHECK (length(owner_name) BETWEEN 1 AND 64),
  assigned_at    INTEGER NOT NULL,
  step           TEXT NOT NULL,
  step_rank      INTEGER NOT NULL CHECK (step_rank >= 0),
  changed_at     INTEGER NOT NULL,
  crm_owner_done INTEGER NOT NULL DEFAULT 0 CHECK (crm_owner_done IN (0,1)),
  crm_step_done  INTEGER NOT NULL DEFAULT -1,   -- the step_rank last copied to the CRM; -1 = none
  crm_tries      INTEGER NOT NULL DEFAULT 0,    -- failed copy tries since the last success
  crm_error      TEXT);                         -- class name of the last copy error, NULL after a success
CREATE TABLE IF NOT EXISTS nudges (
  lead_id    TEXT NOT NULL PRIMARY KEY REFERENCES leads(lead_id),
  state      TEXT NOT NULL CHECK (state IN ('pending','sending','sent','failed','unknown')),
  attempts   INTEGER NOT NULL DEFAULT 1,   -- claims made; the ownership token of the record write (as followups)
  updated_at INTEGER NOT NULL,
  sent_at    INTEGER,                      -- NULL until sent (same as followups.sent_at)
  last_error TEXT,
  handled    INTEGER NOT NULL DEFAULT 0 CHECK (handled IN (0,1)));
CREATE TRIGGER IF NOT EXISTS lead_state_no_delete BEFORE DELETE ON lead_state
  BEGIN SELECT RAISE(ABORT,'lead state lines are never deleted'); END;   -- also stops INSERT OR REPLACE (recursive_triggers=ON)
CREATE TRIGGER IF NOT EXISTS lead_state_owner_immutable BEFORE UPDATE OF owner_id, owner_name, assigned_at ON lead_state
  BEGIN SELECT RAISE(ABORT,'a lead owner never changes'); END;
CREATE TRIGGER IF NOT EXISTS lead_state_no_regress BEFORE UPDATE OF step_rank, step ON lead_state
  WHEN NEW.step_rank < OLD.step_rank OR (NEW.step_rank = OLD.step_rank AND NEW.step <> OLD.step)
  BEGIN SELECT RAISE(ABORT,'a lead stage only moves forward'); END;
CREATE TRIGGER IF NOT EXISTS nudges_no_delete BEFORE DELETE ON nudges
  BEGIN SELECT RAISE(ABORT,'reminder lines are never deleted'); END;   -- also stops INSERT OR REPLACE (recursive_triggers=ON)
CREATE TRIGGER IF NOT EXISTS nudges_sent_final BEFORE UPDATE ON nudges
  WHEN OLD.state = 'sent'
  BEGIN SELECT RAISE(ABORT,'a sent reminder is final'); END;

CREATE INDEX IF NOT EXISTS receipts_open_ix ON receipts(received_at) WHERE outcome IS NULL;
CREATE INDEX IF NOT EXISTS receipts_time_ix ON receipts(received_at DESC, receipt_id DESC);
CREATE INDEX IF NOT EXISTS followups_active_ix ON followups(updated_at) WHERE state IN ('pending','sending');

DROP VIEW IF EXISTS review_items;
CREATE VIEW review_items AS      -- the static part of the "review list"; 7 columns in every branch
  SELECT 'R' || receipt_id AS item_id, 'receipt' AS item_type, 'review' AS kind, outcome_reason AS reason,
         NULL AS lead_id, received_at AS since, source
    FROM receipts WHERE outcome = 'review'
  UNION ALL
  SELECT 'D' || receipt_id, 'receipt',
         CASE WHEN outcome = 'duplicate' AND differing_fields <> '[]' THEN 'duplicate_with_new_info'
              ELSE 'replay_payload_differs' END,
         CASE WHEN outcome = 'duplicate' AND differing_fields <> '[]' THEN differing_fields
              ELSE 'same request number, different payload' END,
         outcome_lead_id, received_at, source
    FROM receipts WHERE (outcome = 'duplicate' AND differing_fields <> '[]') OR payload_conflict = 1
  UNION ALL
  SELECT 'F' || f.lead_id, 'followup', f.state, f.last_error, f.lead_id, f.updated_at, l.source
    FROM followups f JOIN leads l USING (lead_id) WHERE f.state IN ('failed','unknown')
  UNION ALL
  SELECT 'N' || n.lead_id, 'reminder', 'reminder_to_check', n.state, n.lead_id, n.updated_at, l.source
    FROM nudges n JOIN leads l ON l.lead_id = n.lead_id WHERE n.state IN ('failed','unknown') AND n.handled = 0;
