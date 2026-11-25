-- Agent Duel leaderboard history (ai-news Agent Leaderboard). One snapshot per Singapore day, written by
-- /opt/ai-map/pipeline/agentduel.py on olaresone. Re-running a day replaces that day's rows (FK cascade).
CREATE DATABASE IF NOT EXISTS agentduel CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE agentduel;

CREATE TABLE IF NOT EXISTS models (
  slug        VARCHAR(120) PRIMARY KEY,            -- e.g. anthropic/claude-opus-5.5
  name        VARCHAR(160) NOT NULL,
  lab         VARCHAR(80),
  first_seen  DATE NOT NULL,
  last_seen   DATE NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS snapshots (
  id                  INT AUTO_INCREMENT PRIMARY KEY,
  snap_date           DATE NOT NULL UNIQUE,          -- Singapore date of the copy
  fetched_at          DATETIME NOT NULL,             -- UTC
  generated_all       DATETIME NULL,                 -- Agent Duel's own computed time (UTC), all votes
  generated_no_early  DATETIME NULL,                 -- ... and the "exclude early votes" view
  models_ranked       SMALLINT,
  eligible_battles    INT,                           -- sum of per-model battles / 2
  raw_json            LONGTEXT NOT NULL              -- the whole leaderboard.json, for re-parsing later
) ENGINE=InnoDB;

-- The board rows, for both vote filters.
CREATE TABLE IF NOT EXISTS ratings (
  snapshot_id   INT NOT NULL,
  dataset       ENUM('all','noEarly') NOT NULL,
  slug          VARCHAR(120) NOT NULL,
  rank_pos      SMALLINT NOT NULL,
  elo           DECIMAL(7,1) NOT NULL,
  ci_lo         DECIMAL(7,1), ci_hi DECIMAL(7,1),
  ci_plus       DECIMAL(6,1), ci_minus DECIMAL(6,1),
  tier          SMALLINT,                            -- our tier (95% ranges overlapping the tier leader's)
  p_first       DECIMAL(5,4),                        -- chance of ranking #1 (bootstrap)
  rank_lo95     SMALLINT, rank_hi95 SMALLINT,
  cost_median   DECIMAL(10,4), cost_mean DECIMAL(10,4),   -- USD per task, difficulty-adjusted
  time_median   DECIMAL(8,2),  time_mean DECIMAL(8,2),    -- minutes per task, difficulty-adjusted
  frontier_cost_median TINYINT(1) NOT NULL DEFAULT 0,     -- on Agent Duel's Pareto frontier
  frontier_cost_mean   TINYINT(1) NOT NULL DEFAULT 0,
  frontier_time_median TINYINT(1) NOT NULL DEFAULT 0,
  frontier_time_mean   TINYINT(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (snapshot_id, dataset, slug),
  KEY k_slug (slug, dataset),
  CONSTRAINT fk_r_snap FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Per-model detail page (published for all votes only).
CREATE TABLE IF NOT EXISTS model_stats (
  snapshot_id     INT NOT NULL,
  slug            VARCHAR(120) NOT NULL,
  battles         INT, voters INT,
  raw_win_rate    DECIMAL(5,4),
  cost_median     DECIMAL(10,4), cost_mean DECIMAL(10,4),
  raw_cost_median DECIMAL(10,4), raw_cost_mean DECIMAL(10,4),
  time_median     DECIMAL(8,2),  time_mean DECIMAL(8,2),
  raw_time_median DECIMAL(8,2),  raw_time_mean DECIMAL(8,2),
  tokens_per_sec  DECIMAL(8,2),
  rank_lo50 SMALLINT, rank_hi50 SMALLINT, rank_lo95 SMALLINT, rank_hi95 SMALLINT,
  served_through  VARCHAR(80), model_id VARCHAR(160), version VARCHAR(200), thinking VARCHAR(120),
  first_vote      DATE,
  PRIMARY KEY (snapshot_id, slug),
  KEY k_slug (slug),
  CONSTRAINT fk_s_snap FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS rank_probs (
  snapshot_id INT NOT NULL,
  slug        VARCHAR(120) NOT NULL,
  rank_pos    SMALLINT NOT NULL,
  prob        DECIMAL(5,4) NOT NULL,
  PRIMARY KEY (snapshot_id, slug, rank_pos),
  CONSTRAINT fk_p_snap FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS h2h (
  snapshot_id INT NOT NULL,
  slug        VARCHAR(120) NOT NULL,
  opp_slug    VARCHAR(120) NOT NULL,
  wins INT NOT NULL, ties INT NOT NULL, losses INT NOT NULL,
  win_rate    DECIMAL(5,4),
  ci_lo       DECIMAL(5,4), ci_hi DECIMAL(5,4),
  PRIMARY KEY (snapshot_id, slug, opp_slug),
  KEY k_pair (slug, opp_slug),
  CONSTRAINT fk_h_snap FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Agent Duel's weekly Elo fits as published that day (past weeks can be revised, so kept per snapshot).
CREATE TABLE IF NOT EXISTS weekly_elo (
  snapshot_id INT NOT NULL,
  slug        VARCHAR(120) NOT NULL,
  week_start  DATE NOT NULL,
  elo         DECIMAL(7,1) NOT NULL,
  ci_lo       DECIMAL(7,1), ci_hi DECIMAL(7,1),
  partial     TINYINT(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (snapshot_id, slug, week_start),
  CONSTRAINT fk_w_snap FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Handy views
CREATE OR REPLACE VIEW v_rank_history AS
  SELECT s.snap_date, r.dataset, r.slug, m.name, m.lab, r.rank_pos, r.elo, r.ci_lo, r.ci_hi, r.tier, r.p_first,
         r.cost_median, r.time_median
  FROM ratings r JOIN snapshots s ON s.id = r.snapshot_id JOIN models m ON m.slug = r.slug;

CREATE OR REPLACE VIEW v_latest AS
  SELECT v.* FROM v_rank_history v WHERE v.snap_date = (SELECT MAX(snap_date) FROM snapshots);

-- Latest published value for each model-week (the newest snapshot that contains it)
CREATE OR REPLACE VIEW v_weekly_latest AS
  SELECT w.slug, w.week_start, w.elo, w.ci_lo, w.ci_hi, w.partial, s.snap_date AS as_of
  FROM weekly_elo w JOIN snapshots s ON s.id = w.snapshot_id
  WHERE w.snapshot_id = (SELECT MAX(w2.snapshot_id) FROM weekly_elo w2 WHERE w2.slug = w.slug AND w2.week_start = w.week_start);
