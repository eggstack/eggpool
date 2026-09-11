-- Drop the duplicate reservations.estimated_microdollars column.
-- The column was identical in meaning to reservations.reserved_microdollars
-- and the repository code stored the same value in both. The single source
-- of truth is reservations.reserved_microdollars.

ALTER TABLE reservations DROP COLUMN estimated_microdollars;
