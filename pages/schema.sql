-- D1 schema for the private visitor log behind Jag's AI Map.
-- One row per page view; no raw IP is ever stored (ip_hash is a per-day salted hash
-- used only for unique-visitor counts).
CREATE TABLE IF NOT EXISTS visits (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,   -- unix seconds
  path     TEXT,
  country  TEXT,               -- ISO-3166 alpha-2 (from request.cf.country)
  city     TEXT,
  region   TEXT,
  colo     TEXT,               -- Cloudflare edge PoP that served the request
  asn      TEXT,               -- network (request.cf.asOrganization)
  ua       TEXT,
  referer  TEXT,
  ip_hash  TEXT,               -- sha256(day-salt | ip), first 8 bytes; NOT reversible
  bot      TEXT                -- NULL = person; else probe | ua | dc (see functions/_middleware.js)
);
CREATE INDEX IF NOT EXISTS idx_visits_ts ON visits(ts);
CREATE INDEX IF NOT EXISTS idx_visits_country ON visits(country);
CREATE INDEX IF NOT EXISTS idx_visits_iphash ON visits(ip_hash);
CREATE INDEX IF NOT EXISTS idx_visits_bot ON visits(bot);
