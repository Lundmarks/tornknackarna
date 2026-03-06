import httpx

JSONBIN_KEY = os.environ["JSONBIN_KEY"]
HEADERS = {"X-Master-Key": JSONBIN_KEY, "Content-Type": "application/json"}
BASE = "https://api.jsonbin.io/v3"

def create_bin(data: dict, name: str) -> str:
    """Create a new bin, return its ID."""
    r = httpx.post(f"{BASE}/b", json=data,
                   headers={**HEADERS, "X-Bin-Name": name, "X-Bin-Private": "false"})
    r.raise_for_status()
    return r.json()["metadata"]["id"]

def update_bin(bin_id: str, data: dict):
    """Overwrite an existing bin."""
    r = httpx.put(f"{BASE}/b/{bin_id}", json=data, headers=HEADERS)
    r.raise_for_status()
