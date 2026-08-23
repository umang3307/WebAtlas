CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_domain TEXT NOT NULL,
    collector_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    node_type TEXT NOT NULL,
    label TEXT NOT NULL,
    file_type TEXT,
    url TEXT,
    discovered_via TEXT,
    raw_data TEXT,
    first_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    source_node_id INTEGER NOT NULL REFERENCES nodes(id),
    target_node_id INTEGER NOT NULL REFERENCES nodes(id),
    edge_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    status TEXT NOT NULL,
    collector_id TEXT,
    snapshot_id TEXT,
    notes TEXT,
    scraped_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_scan ON nodes(scan_id);
CREATE INDEX IF NOT EXISTS idx_edges_scan ON edges(scan_id);
