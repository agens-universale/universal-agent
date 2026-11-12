-- Удостоверение аренды не повторяется после уборки надгробия.
ALTER TABLE gc_threads ADD COLUMN lease_token text;
