"""Regime Desk, standalone copy.

Packs the dashboard page and its data into a small static site that works without Claude:
  index.html   the lock screen and a small runtime that stands in for the dashboard's own
  d3.min.js    the chart library the page uses
  key.json     the site's private key, locked with the owner's passphrase and personal link
  app.bin      the page, gzipped and encrypted
  data.bin     the data files and price snapshots, gzipped and encrypted the same way

Each .bin is locked with a fresh AES-256-GCM key, and that key is locked with the site's PUBLIC key (RSA-OAEP).
So this script needs no secret at all: it can lock, and only the owner's browser can open.

Usage: python3 build_site.py <page.html> <data dir> <site_pub.pem> <out dir> [shell.html] [d3.min.js]
"""
import base64, gzip, json, os, sys, time, urllib.request, urllib.error
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

UA = {"User-Agent": "Mozilla/5.0 (regime-desk site build)"}
FILES = ["regime_history", "regime_components", "markets", "book", "chain_tvl", "calendar", "projection", "projection_summary",
         "forward", "holdings", "momentum", "alt_phase", "alt_phase_stats", "daily_summary", "themes", "stories", "ubik_forecasts", "book_history"]
COINS = {"ubik": "ubik", "nock": "nockchain", "fables": "fables", "priors": "priors-agent"}   # page name -> CoinGecko id
TICKER = ["bitcoin", "ethereum", "solana", "hyperliquid", "binancecoin", "arbitrum", "sp500-xstock", "nasdaq-xstock",
          "nvidia-xstock", "robinhood-xstock", "pax-gold", "kinesis-silver"]

# wording that only makes sense inside Claude, and what the standalone copy says instead
SWAPS = [
    ("Prices, your book and AIXBT load live each time you open the page.", "Prices and your book load live each time you open the page. AIXBT narratives are a snapshot from the last update."),
    ("To keep this arrangement for good, say “save layout” to Claude while this page is open. Layout code:", "Your arrangement is saved on this device. Layout code:"),
    ("Your arrangement holds while the page is open. To make it the one the page always opens with, say “save layout” to Claude with the page open; Reset layout returns to it.",
     "Your arrangement is saved on this device. Reset layout returns to the default."),
    ("Open the desk in your phone's browser first, signed in to claude.ai, not inside the Claude app.", "Open this page in your phone's browser first."),
]


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < tries - 1:
                time.sleep(20 * (i + 1)); continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(5 * (i + 1)); continue
            raise


def snapshots():
    """Prices as the page's loaders expect them, so the first paint needs no network."""
    snap = {}
    try:
        ids = ",".join(list(COINS.values()) + TICKER)
        m = get(f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={ids}&sparkline=true&price_change_percentage=24h")
        snap["cg_prices"] = {"coins": [{"id": c["id"], "symbol": c["symbol"], "name": c["name"], "currentPrice": c["current_price"],
                                        "priceChangePercentage24h": c.get("price_change_percentage_24h"), "marketCap": c.get("market_cap"),
                                        "totalVolume": c.get("total_volume"), "sparkline7d": (c.get("sparkline_in_7d") or {}).get("price") or []} for c in m]}
    except Exception as e:
        print("snapshot prices", e)
    for name, cid in COINS.items():
        for key, days in (("hist_" + name, 365), ("fc_" + name, 90)):
            try:
                time.sleep(7)
                p = get(f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart?vs_currency=usd&days={days}")["prices"]
                snap[key] = {"startTs": p[0][0], "endTs": p[-1][0], "prices": [x[1] for x in p]}
            except Exception as e:
                print("snapshot", key, e)
    return snap


def seal(pub, obj):
    """File layout: 2 bytes (length of the locked key), the locked key, 12-byte nonce, the encrypted gzip."""
    key, nonce = os.urandom(32), os.urandom(12)
    locked = pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    body = AESGCM(key).encrypt(nonce, gzip.compress(json.dumps(obj, separators=(",", ":")).encode(), 9), None)
    return len(locked).to_bytes(2, "big") + locked + nonce + body


def topics():
    """AIXBT's topic list is public. Reshaped to what the page's loader expects from the connector."""
    d = get("https://api.aixbt.tech/v2/topics")["data"]
    return {"data": [{"id": t["id"], "rank": t["rank"], "name": t["name"], "summary": t.get("summary"), "trend": t.get("score"),
                      "postCount": t.get("tweetCount"), "authorCount": t.get("authorCount"), "lastSeenAt": t.get("lastSeenAt"),
                      "clusters": [{"cluster": {"id": c["id"], "name": c["name"]}, "share": c["share"]} for c in t.get("clusters") or []]}
                     for t in d["topics"]],
            "meta": {"snapshot": {"completedAt": d.get("completedAt")}}}


def main():
    page_path, data_dir, key_path, out = sys.argv[1:5]
    here = os.path.dirname(os.path.abspath(__file__))
    shell = sys.argv[5] if len(sys.argv) > 5 else os.path.join(here, "shell.html")
    d3 = sys.argv[6] if len(sys.argv) > 6 else os.path.join(here, "d3.min.js")
    key = serialization.load_pem_public_key(open(key_path, "rb").read())
    os.makedirs(out, exist_ok=True)
    page = open(page_path).read()
    for a, b in SWAPS:
        if a in page:
            page = page.replace(a, b)
        else:
            print("wording not found, left as is:", a[:50])
    files = {}
    for f in FILES:
        p = os.path.join(data_dir, f + ".csv")
        if os.path.exists(p):
            files[f] = {"csv": open(p).read()}
        else:
            print("missing", f)
    p = os.path.join(data_dir, "outlook.json")
    if os.path.exists(p):
        files["outlook"] = {"json": json.load(open(p))}
    built = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    open(os.path.join(out, "app.bin"), "wb").write(seal(key, {"page": page, "built": built}))
    snap = snapshots()
    try:
        snap["aixbt_topics"] = topics()
    except Exception as e:
        print("snapshot topics", e)
    open(os.path.join(out, "data.bin"), "wb").write(seal(key, {"files": files, "snap": snap, "built": built}))
    for src, name in ((shell, "index.html"), (d3, "d3.min.js")):
        text, dst = open(src).read(), os.path.join(out, name)     # read first: the out folder can be the folder the file already lives in
        if os.path.abspath(src) != os.path.abspath(dst):
            open(dst, "w").write(text)
    open(os.path.join(out, ".nojekyll"), "w").write("")
    json.dump({"built": built}, open(os.path.join(out, "build.json"), "w"))
    print("site built", built, {f: os.path.getsize(os.path.join(out, f)) for f in ("app.bin", "data.bin", "index.html")})


if __name__ == "__main__":
    main()
