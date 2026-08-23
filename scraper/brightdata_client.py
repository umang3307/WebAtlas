import os, time, requests
from dotenv import load_dotenv

load_dotenv()
API_BASE = "https://api.brightdata.com"
API_TOKEN = os.environ.get("BRIGHT_DATA_API_TOKEN", "")
POLL_INTERVAL_S = 5
MAX_POLL_ATTEMPTS = 90
MAX_RETRIES = 3


def _headers():
    return {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}


class CollectorNotFoundError(RuntimeError):
    """Raised when Bright Data 404s a trigger call for a given collector_id
    -- meaning the collector doesn't exist (deleted, or never finished
    being built). Not worth retrying; the caller should invalidate the
    cached id and recreate the collector."""
    pass


def trigger_collector(collector_id, inputs):
    url = f"{API_BASE}/dca/trigger?collector={collector_id}"
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(url, json=inputs, headers=_headers(), timeout=30)
            if resp.status_code == 404:
                raise CollectorNotFoundError(
                    f"collector {collector_id} not found (404) -- it may never have "
                    f"finished building, or was deleted"
                )
            if resp.status_code >= 500:
                raise requests.exceptions.RequestException(f"server error {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            return data.get("collection_id") or data.get("snapshot_id")
        except CollectorNotFoundError:
            raise  # don't retry -- retrying a nonexistent collector wastes time
        except requests.exceptions.RequestException as e:
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"trigger_collector failed: {last_err}")


def poll_results(snapshot_id):
    url = f"{API_BASE}/dca/dataset?id={snapshot_id}"
    for _ in range(MAX_POLL_ATTEMPTS):
        try:
            resp = requests.get(url, headers=_headers(), timeout=30)
            if resp.status_code == 202:
                time.sleep(POLL_INTERVAL_S)
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                return data
            time.sleep(POLL_INTERVAL_S)
        except requests.exceptions.RequestException:
            time.sleep(POLL_INTERVAL_S)
    return None


def run_collector(collector_id, target_url):
    inputs = [{"url": target_url}]
    snapshot_id = trigger_collector(collector_id, inputs)
    records = poll_results(snapshot_id)
    return records, snapshot_id