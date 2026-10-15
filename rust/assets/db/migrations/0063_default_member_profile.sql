ALTER TABLE policy_state
    ADD COLUMN default_member_profile_id TEXT
        REFERENCES policy_profiles(id) ON DELETE RESTRICT;

CREATE TRIGGER policy_state_default_member_profile_revision_update
AFTER UPDATE OF default_member_profile_id ON policy_state
WHEN OLD.default_member_profile_id IS NOT NEW.default_member_profile_id
BEGIN
    UPDATE policy_state SET revision = revision + 1 WHERE singleton = 1;
END;
