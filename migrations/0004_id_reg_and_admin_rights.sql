-- ============================================================================
-- Migration 0004: ID registration and admin rights tables
--
-- Adds to the D1 database ("vesterosbg-db"):
--
--   id_reg
--     Stores registry / identification registrations. Each row is a single
--     opaque "id" belonging to an owner.
--
--   admin_rights
--     Grants named rights to users. A user may hold many rights; a right
--     is a named key with an integer "value" (0 = denied, 1 = allowed,
--     2 = full access).
--
-- Semantics:
--   - id_reg.id          opaque registry identifier (e.g. magistrat UUID)
--   - id_reg.reg_type    classification of the registry entry
--   - id_reg.reg_value   the opaque ID value
--   - id_reg.is_active   1 = active, 0 = revoked
--
--   - admin_rights.user_id    OS user / session identifier
--   - admin_rights.right_name name of the right (e.g. "manage_reports")
--   - admin_rights.right_value integer: 0=deny, 1=allow, 2=full
--
-- Indexes are created on frequently-queried columns to keep lookups fast.
-- ============================================================================
CREATE TABLE IF NOT EXISTS id_reg (
    id        TEXT PRIMARY KEY,
    owner_id  TEXT NOT NULL,
    reg_type  TEXT NOT NULL,
    reg_value TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_id_reg_owner_active
    ON id_reg(owner_id, is_active);

CREATE INDEX IF NOT EXISTS idx_id_reg_type_value
    ON id_reg(reg_type, reg_value);

CREATE TABLE IF NOT EXISTS admin_rights (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    right_name TEXT NOT NULL,
    right_value INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_admin_rights_user
    ON admin_rights(user_id, right_name);

CREATE INDEX IF NOT EXISTS idx_admin_rights_value
    ON admin_rights(right_value);
