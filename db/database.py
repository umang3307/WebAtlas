import sqlite3, json, os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "webatlas.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    with open(SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def start_scan(target_domain, collector_id=""):
    conn = get_connection()
    cur = conn.execute("INSERT INTO scans (target_domain, collector_id, started_at) VALUES (?, ?, ?)",
                        (target_domain, collector_id, _now()))
    conn.commit()
    scan_id = cur.lastrowid
    conn.close()
    return scan_id


def finish_scan(scan_id):
    conn = get_connection()
    conn.execute("UPDATE scans SET finished_at = ? WHERE id = ?", (_now(), scan_id))
    conn.commit()
    conn.close()


def add_node(scan_id, node_type, label, url=None, file_type=None, discovered_via="", raw_data=None):
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO nodes (scan_id, node_type, label, file_type, url, discovered_via, raw_data, first_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (scan_id, node_type, label, file_type, url, discovered_via, json.dumps(raw_data or {}), _now()))
    conn.commit()
    node_id = cur.lastrowid
    conn.close()
    return node_id


def add_edge(scan_id, source_id, target_id, edge_type):
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO edges (scan_id, source_node_id, target_node_id, edge_type, created_at) VALUES (?, ?, ?, ?, ?)",
        (scan_id, source_id, target_id, edge_type, _now()))
    conn.commit()
    edge_id = cur.lastrowid
    conn.close()
    return edge_id


def log_scrape(scan_id, status, collector_id="", snapshot_id="", notes=""):
    conn = get_connection()
    conn.execute(
        """INSERT INTO scrape_log (scan_id, status, collector_id, snapshot_id, notes, scraped_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (scan_id, status, collector_id, snapshot_id, notes, _now()))
    conn.commit()
    conn.close()


def export_graph(scan_id):
    conn = get_connection()
    nodes = [dict(r) for r in conn.execute("SELECT * FROM nodes WHERE scan_id = ?", (scan_id,))]
    edges = [dict(r) for r in conn.execute("SELECT * FROM edges WHERE scan_id = ?", (scan_id,))]
    log = [dict(r) for r in conn.execute("SELECT * FROM scrape_log WHERE scan_id = ?", (scan_id,))]
    conn.close()
    for n in nodes:
        n["raw_data"] = json.loads(n["raw_data"]) if n["raw_data"] else {}
    return {"nodes": nodes, "edges": edges, "scrape_log": log}


if __name__ == "__main__":
    init_db()
    print(f"initialized at {DB_PATH}")
