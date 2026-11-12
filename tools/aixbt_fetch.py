"""Fetches the AIXBT data the desk shows and locks it for the site.

Runs in GitHub Actions. The AIXBT key comes from the repository secret AIXBT_API_KEY and is used only in the
request header: it is never printed or written to a file. The result is encrypted with the site's public key
(tools/site_pub.pem), so only the owner's browser can read it.

Writes:
  aixbt.bin          the encrypted data
  aixbt_status.json  what each request returned: status code, counts and field names. No content, nothing secret.
"""
import gzip, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BASE = "https://api.aixbt.tech/v2/"
KEY = os.environ.get("AIXBT_API_KEY", "").strip()
PROJECTS = "6a96f7b0571cbb6f7736bcea,6ada6259d9fb6e019731b67e,6aaee90dabb58457f6f25c33,68ebc3f58237fd4de3b7e8eb,6a64c1b5e35deca8f488e5c9"
CALLS = {   # the same requests the dashboard makes through its AIXBT connector
    "aixbt_topics": ("topics", {}, False),
    "aixbt_intel": ("intel", {"projectIds": PROJECTS, "limit": 20, "sort": "detectedAt", "activity": "none"}, True),
    "aixbt_chain_tokens": ("projects", {"tickers": "BTC,ETH,SOL,BNB,ARB,HYPE", "limit": 12, "sort": "rank"}, True),
    "aixbt_rh_buzz": ("projects", {"chain": "robinhood", "limit": 50, "sort": "reinforcedAt"}, True),
    "aixbt_base_buzz": ("projects", {"chain": "base", "limit": 50, "sort": "reinforcedAt"}, True),
    "aixbt_launches": ("projects", {"chain": "robinhood", "limit": 50, "sort": "createdAt"}, True),
}


def call(path, params, needs_key):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"User-Agent": "regime-desk-site", "Accept": "application/json"}
    if needs_key:
        headers["x-api-key"] = KEY
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < 2:
                time.sleep(10 * (i + 1)); continue
            try:
                body = json.loads(e.read())
            except Exception:
                body = {}
            return e.code, {"error": {"message": str(body.get("message") or body.get("error") or e.reason), "code": body.get("code")}}
        except Exception as e:
            if i < 2:
                time.sleep(5 * (i + 1)); continue
            return 0, {"error": {"message": type(e).__name__}}


def items(d):
    """The list inside an answer, wherever the API put it."""
    x = d.get("data") if isinstance(d, dict) else None
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("topics", "intel", "projects", "items", "results"):
            if isinstance(x.get(k), list):
                return x[k]
        for v in x.values():
            if isinstance(v, list):
                return v
    return None


def shape(name, rows):
    """Field names differ a little between AIXBT's web API and the connector the page was written for."""
    out = []
    for r in rows:
        r = dict(r)
        if name == "aixbt_topics":
            r.setdefault("trend", r.get("score")); r.setdefault("postCount", r.get("tweetCount"))
            r["clusters"] = [c if "cluster" in c else {"cluster": {"id": c.get("id"), "name": c.get("name")}, "share": c.get("share")} for c in r.get("clusters") or []]
        elif name == "aixbt_intel":
            if not isinstance(r.get("project"), dict):
                r["project"] = {"name": r.get("projectName") or r.get("project")}
            r.setdefault("hasOfficialSource", r.get("official"))
        else:
            r.setdefault("trend", r.get("score")); r.setdefault("symbol", r.get("ticker"))
        out.append(r)
    return out


def main():
    pub = serialization.load_pem_public_key(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "site_pub.pem"), "rb").read())
    snap, status = {}, {"ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "key_present": bool(KEY), "calls": {}}
    for name, (path, params, needs_key) in CALLS.items():
        if needs_key and not KEY:
            status["calls"][name] = {"skipped": "no AIXBT_API_KEY secret"}
            continue
        code, d = call(path, params, needs_key)
        rows = items(d)
        st = {"http": code, "top": sorted(d.keys()) if isinstance(d, dict) else type(d).__name__, "count": len(rows) if rows is not None else None}
        if isinstance(d, dict) and isinstance(d.get("data"), dict):
            st["data_keys"] = sorted(d["data"].keys())
        if rows:
            st["fields"] = sorted(rows[0].keys()) if isinstance(rows[0], dict) else type(rows[0]).__name__
            for sub in ("project", "cluster", "clusters"):
                v = rows[0].get(sub) if isinstance(rows[0], dict) else None
                v = v[0] if isinstance(v, list) and v else v
                if isinstance(v, dict):
                    st[sub + "_fields"] = sorted(v.keys())
        if isinstance(d, dict) and d.get("error"):
            st["error"] = d["error"]
        status["calls"][name] = st
        if code == 200 and rows is not None:
            snap[name] = {"data": shape(name, rows)}
        time.sleep(1)
    json.dump(status, open("aixbt_status.json", "w"), indent=1)
    if not snap:
        print("nothing fetched"); return 1
    key, nonce = os.urandom(32), os.urandom(12)
    locked = pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    body = AESGCM(key).encrypt(nonce, gzip.compress(json.dumps({"snap": snap, "built": status["ran_at"]}, separators=(",", ":")).encode(), 9), None)
    open("aixbt.bin", "wb").write(len(locked).to_bytes(2, "big") + locked + nonce + body)
    print("aixbt.bin written:", ", ".join(f"{k} {len(v['data'])}" for k, v in snap.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
