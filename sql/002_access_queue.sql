-- Access queue (also created automatically on first login; run this only if you prefer to do it by hand)
USE foodresq;
CREATE TABLE IF NOT EXISTS access_queue (
    token       CHAR(43)     NOT NULL PRIMARY KEY,
    role        VARCHAR(10)  NOT NULL,
    status      VARCHAR(10)  NOT NULL,
    created_at  DATETIME(3)  NOT NULL,
    last_seen   DATETIME(3)  NOT NULL,
    INDEX idx_status_created (status, created_at),
    INDEX idx_last_seen (last_seen)
) ENGINE=InnoDB;
