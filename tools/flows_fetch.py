"""Money flows for the Regime Desk: where buying and selling pressure sits, by the hour.

Public data only, no keys:
  Binance spot (its public market-data mirror): for the busiest USDT pairs, hourly traded dollars and how much of
    that was buyers lifting the offer. Net flow of a coin = buyer-initiated dollars minus seller-initiated dollars.
  DefiLlama: stablecoin supply and DEX volume per chain.

Optional, with the repository secret NANSEN_API_KEY: on-chain buy and sell volume per token on Robinhood Chain and
Base, from Nansen's token screener. The key is used only in the request header and is never printed or written.

Usage:
  python3 flows_fetch.py out_dir [--state FILE | --state-url URL] [--seal site_pub.pem flows.bin]
      writes flows_market.csv, flows_coins.csv, flows_chains.csv and flows_status.json into out_dir
      --state      a small public file of hourly stablecoin readings this script keeps, so it can show 1h and 4h changes
      --state-url  the same file read over the web (read only), for runs that do not keep it themselves
      --seal       also writes the three files encrypted with the site's public key
  python3 flows_fetch.py --nansen site_pub.pem nansen.bin
      writes the Nansen rankings encrypted, plus nansen_status.json (status codes, counts and field names only)
"""
import csv, gzip, io, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (regime-desk)"}
BINANCE = "https://data-api.binance.vision/api/v3/"
N_COINS = 100                                            # busiest USDT pairs by 24h dollars traded
TOP = 8                                                  # rows per side in the most bought / most sold tables
FRAMES = [("1h", 1, 48), ("4h", 4, 30), ("1d", 24, 30), ("1w", 168, 5)]   # name, hours in a window, windows kept for the chart
# not assets: dollar and euro stand-ins, and wrapped copies that would count a coin twice
SKIP = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1", "XUSD", "PYUSD", "USDE", "BFUSD", "RLUSD", "USDS", "USDD",
        "EUR", "EURI", "AEUR", "EURC", "GBP", "TRY", "BRL", "ARS", "UAH", "PLN", "RON", "ZAR", "MXN", "COP", "CZK", "JPY",
        "WBTC", "WBETH", "BNSOL", "PAXG"}
CHAINS = [("Ethereum", "Ethereum"), ("Tron", "Tron"), ("Solana", "Solana"), ("BSC", "BNB Chain"), ("Hyperliquid L1", "Hyperliquid"),
          ("Base", "Base"), ("Arbitrum", "Arbitrum"), ("Robinhood Chain", "Robinhood Chain")]   # DefiLlama's name, the desk's name
NANSEN_CHAINS = [("rh", "robinhood"), ("base", "base")]
NANSEN_FRAMES = ["6h", "24h", "7d"]                      # the 1h window is left to the live connector: a job cannot keep it fresh
BOOK = [("ubik", "robinhood", "0x812486EAea648819853F8E372dc9f1516C7868Bd"), ("priors", "robinhood", "0xeDBf91223639800BCd5756815CAf908Df3b890bE"),
        ("nock", "base", "0x9B5E262cF9bb04869ab40b19AF91D2dc85761722")]   # token contracts the desk follows
BOOK_FRAMES = ["6h", "1d", "7d"]
SEGMENTS = [("Smart Trader", "smart_trader"), ("Top PnL", "top_pnl"), ("Whale", "whale"), ("Public Figure", "public_figure"),
            ("Fresh", "fresh_wallets"), ("Exchange", "exchange")]
HOUR = 3600 * 1000


def get(url, tries=4, data=None, headers=None):
    """One request with patience: waits and retries on rate limits, server errors and dropped connections."""
    h = dict(UA); h.update(headers or {})
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=40) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and i < tries - 1:
                time.sleep(10 * (i + 1)); continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i < tries - 1:
                time.sleep(5 * (i + 1)); continue
            raise


def iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def to_csv(cols, rows):
    f = io.StringIO(); w = csv.writer(f, lineterminator="\n"); w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])
    return f.getvalue()


# ---------- exchange flows ----------

def exchange(status):
    tick = get(BINANCE + "ticker/24hr")
    uni = []
    for t in tick:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in SKIP or not re.fullmatch(r"[A-Z0-9]+", base) or re.search(r"(UP|DOWN|BULL|BEAR)$", base) or float(t["quoteVolume"]) <= 0:
            continue
        uni.append((float(t["quoteVolume"]), base))
    uni = [b for _, b in sorted(uni, reverse=True)[:N_COINS + 15]]
    now = int(time.time() * 1000)
    hours = {}                      # coin -> {hour start ms: (dollars traded, net dollars, open, close)}
    for b in uni:
        try:
            k = get(BINANCE + f"klines?symbol={b}USDT&interval=1h&limit=1000")
        except Exception as e:      # one coin missing does not spoil the rest
            status.setdefault("coin_errors", []).append(f"{b}: {str(e)[:60]}")
            continue
        hh = {int(c[0]): (float(c[7]), 2 * float(c[10]) - float(c[7]), float(c[1]), float(c[4])) for c in k if int(c[6]) < now}
        px = [x[3] for x in hh.values() if x[3] > 0]
        if len(px) > 48 and max(px) / min(px) < 1.015:       # held within 1.5% for weeks: a dollar stand-in, not an asset
            status.setdefault("pegged_skipped", []).append(b)
            continue
        if hh and len(hours) < N_COINS:
            hours[b] = hh
    if "BTC" not in hours or not hours["BTC"]:
        raise RuntimeError("no Binance data for BTC")
    last = max(hours["BTC"])        # start of the last finished hour
    status.update({"coins": len(hours), "last_hour": iso(last), "history_hours": len(hours["BTC"])})
    market, coins = [], []
    for name, h, keep in FRAMES:
        have = min(keep, len(hours["BTC"]) // h)
        for j in range(have - 1, -1, -1):
            end = last - j * h * HOUR                     # start of the window's last hour
            span = [end - i * HOUR for i in range(h)]
            vol = net = btc = eth = alt = 0.0; n = pos = 0; per = []
            for b, hh in hours.items():
                got = [hh[t] for t in span if t in hh]
                if not got:
                    continue
                v = sum(x[0] for x in got); f = sum(x[1] for x in got)
                vol += v; net += f; n += 1; pos += f > 0
                if b == "BTC": btc += f
                elif b == "ETH": eth += f
                else: alt += f
                if j == 0:
                    first = hh[min(t for t in span if t in hh)][2]; lastc = hh[max(t for t in span if t in hh)][3]
                    per.append({"symbol": b, "vol_usd": round(v), "net_usd": round(f), "net_pct": round(f / v * 100, 2) if v else None,
                                "px_chg": round((lastc / first - 1) * 100, 2) if first else None})
            market.append({"tf": name, "t": iso(end + HOUR), "vol_usd": round(vol), "net_usd": round(net), "btc_net": round(btc),
                           "eth_net": round(eth), "alt_net": round(alt), "n": n, "n_pos": pos})
            if j == 0:
                per.sort(key=lambda r: r["net_usd"])
                for side, rows in (("in", per[::-1][:TOP]), ("out", per[:TOP])):
                    for i, r in enumerate(rows):
                        if (r["net_usd"] > 0) == (side == "in") and r["net_usd"] != 0:
                            coins.append(dict(r, tf=name, side=side, rank=i + 1))
    return market, coins


# ---------- chains ----------

def at(points, when):
    """Value of a daily series at a moment, on a straight line between the two readings around it."""
    a = [p for p in points if p[0] <= when]; b = [p for p in points if p[0] > when]
    if not a:
        return None
    if not b:
        return a[-1][1]
    (t0, v0), (t1, v1) = a[-1], b[0]
    return v0 + (v1 - v0) * (when - t0) / (t1 - t0)


def chains(state, status):
    now = time.time()
    cur = {x["name"]: (x.get("totalCirculatingUSD") or {}).get("peggedUSD") for x in get("https://stablecoins.llama.fi/stablecoinchains")}
    snaps = sorted((float(t), v) for t, v in (state.get("stables") or {}).items())

    def snap(name, ago):                 # this script's own earlier reading, nearest to `ago` seconds back
        best = None
        for t, v in snaps:
            d = abs((now - ago) - t)
            if d <= 35 * 60 and name in v and (best is None or d < best[0]):
                best = (d, v[name])
        return best[1] if best else None
    rows = []
    for name, label in CHAINS:
        r = {"chain": label}
        try:
            # The daily history and the live reading come from two DefiLlama feeds that do not agree on level,
            # so a change is always taken inside one of them: this script's own hourly readings where it has
            # them, otherwise the daily history (last full UTC day, last seven).
            ch = get("https://stablecoins.llama.fi/stablecoincharts/" + urllib.parse.quote(name))
            pts = [(float(p["date"]), (p.get("totalCirculatingUSD") or {}).get("peggedUSD")) for p in ch]
            pts = [p for p in pts if p[1]]
            lt, lv = pts[-1]
            r["stables_usd"] = round(lv)
            for k, ago in (("st_chg_1h", 3600), ("st_chg_4h", 4 * 3600), ("st_chg_1d", 86400), ("st_chg_1w", 7 * 86400)):
                v = snap(name, ago) if cur.get(name) else None
                if v:
                    r[k] = round(cur[name] - v)
                elif ago >= 86400:
                    b = at(pts, lt - ago); r[k] = round(lv - b) if b else None
        except Exception as e:
            status.setdefault("chain_errors", []).append(f"{name} stablecoins: {str(e)[:60]}")
        try:
            d = get("https://api.llama.fi/overview/dexs/" + urllib.parse.quote(name) + "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
            r.update({"dex_1d": round(d.get("total24h") or 0) or None, "dex_1d_prev": round(d.get("total48hto24h") or 0) or None,
                      "dex_1w": round(d.get("total7d") or 0) or None, "dex_1w_prev": round(d.get("total14dto7d") or 0) or None})
        except Exception as e:
            status.setdefault("chain_errors", []).append(f"{name} dex: {str(e)[:60]}")
        rows.append(r); time.sleep(0.3)
    # keep this hour's reading for later runs (eight days of them)
    keep = {str(int(t)): v for t, v in snaps if t > now - 8 * 86400}
    keep[str(int(now))] = {n: round(cur[n]) for n, _ in CHAINS if cur.get(n)}
    state["stables"] = keep
    status["chains"] = len(rows); status["stable_readings_kept"] = len(keep)
    return rows


# ---------- sealing (same format as the site's other files) ----------

def seal(pub_path, obj):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    pub = serialization.load_pem_public_key(open(pub_path, "rb").read())
    key, nonce = os.urandom(32), os.urandom(12)
    locked = pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    body = AESGCM(key).encrypt(nonce, gzip.compress(json.dumps(obj, separators=(",", ":")).encode(), 9), None)
    return len(locked).to_bytes(2, "big") + locked + nonce + body


# ---------- Nansen (optional) ----------

def num(v):
    try:
        f = float(v); return f if f == f else None
    except (TypeError, ValueError):
        return None


def nansen(pub, out):
    key = os.environ.get("NANSEN_API_KEY", "").strip()
    status = {"ran_at": iso(time.time() * 1000), "key_present": bool(key), "calls": {}}
    snap = {}
    if key:
        for short, chain in NANSEN_CHAINS:
            for tf in NANSEN_FRAMES:
                name = f"nansen_{short}_{tf}"
                body = {"chains": [chain], "timeframe": tf, "pagination": {"page": 1, "per_page": 25},
                        "filters": {"liquidity": {"min": 25000}}, "order_by": [{"field": "volume", "direction": "DESC"}]}
                st = {}
                try:
                    d = get("https://api.nansen.ai/api/v1/token-screener", tries=2, data=json.dumps(body).encode(),
                            headers={"apiKey": key, "Content-Type": "application/json", "Accept": "application/json"})
                    rows = d.get("data") if isinstance(d, dict) else d
                    st = {"http": 200, "top": sorted(d.keys()) if isinstance(d, dict) else type(d).__name__, "count": len(rows) if isinstance(rows, list) else None}
                    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                        st["fields"] = sorted(rows[0].keys())
                        g = lambda r, *ks: next((r[k] for k in ks if r.get(k) is not None), None)
                        snap[name] = {"asof": status["ran_at"], "rows": [{
                            "chain": g(r, "chain"), "address": g(r, "token_address", "tokenAddress"), "symbol": g(r, "token_symbol", "symbol"),
                            "mcap": num(g(r, "market_cap_usd", "market_cap")), "liquidity": num(g(r, "liquidity", "liquidity_usd")),
                            "px_chg": (lambda p: None if p is None else round(p * 100, 2))(num(g(r, "price_change"))),
                            "buy_usd": num(g(r, "buy_volume")), "sell_usd": num(g(r, "sell_volume")), "vol_usd": num(g(r, "volume")),
                            "net_usd": num(g(r, "netflow", "net_flow"))} for r in rows]}
                except urllib.error.HTTPError as e:
                    st = {"http": e.code, "error": e.read()[:200].decode("utf-8", "replace")}
                except Exception as e:
                    st = {"error": str(e)[:160]}
                status["calls"][name] = st
                time.sleep(1)
        for short, chain, addr in BOOK:          # each followed token: net dollars by kind of labelled wallet
            for tf in BOOK_FRAMES:
                name = f"nansen_tok_{short}_{tf}"
                st = {}
                try:
                    d = get("https://api.nansen.ai/api/v1/tgm/flow-intelligence", tries=2,
                            data=json.dumps({"chain": chain, "token_address": addr, "timeframe": tf}).encode(),
                            headers={"apiKey": key, "Content-Type": "application/json", "Accept": "application/json"})
                    rows = d.get("data") if isinstance(d, dict) else d
                    r = rows[0] if isinstance(rows, list) and rows else rows if isinstance(rows, dict) else None
                    st = {"http": 200, "top": sorted(d.keys()) if isinstance(d, dict) else type(d).__name__, "fields": sorted(r.keys()) if isinstance(r, dict) else None}
                    if isinstance(r, dict):
                        out_rows = []
                        for label, pre in SEGMENTS:
                            pick = lambda *words: next((num(v) for k, v in r.items() if k.startswith(pre) and all(w in k for w in words)), None)
                            net, avg, cnt = pick("net", "flow"), pick("avg", "flow"), pick("wallet")
                            out_rows.append({"segment": label, "net_usd": net or 0, "vs_avg": round(abs(net / avg), 1) if net and avg else None,
                                             "wallets": int(cnt) if net and cnt else None})
                        snap[name] = {"asof": status["ran_at"], "rows": out_rows}
                except urllib.error.HTTPError as e:
                    st = {"http": e.code, "error": e.read()[:200].decode("utf-8", "replace")}
                except Exception as e:
                    st = {"error": str(e)[:160]}
                status["calls"][name] = st
                time.sleep(1)
    json.dump(status, open("nansen_status.json", "w"), indent=1)
    if not snap:
        print("no Nansen data" + ("" if key else " (no NANSEN_API_KEY secret)")); return 0
    open(out, "wb").write(seal(pub, {"snap": snap, "built": status["ran_at"]}))
    print("nansen written:", len(snap), "sets")
    return 0


def main(argv):
    if argv and argv[0] == "--nansen":
        return nansen(argv[1], argv[2])
    out = argv[0]; os.makedirs(out, exist_ok=True)
    opt = {argv[i]: argv[i + 1:i + 3] for i in range(1, len(argv)) if argv[i].startswith("--")}
    state, state_path = {}, (opt.get("--state") or [None])[0]
    try:
        if state_path and os.path.exists(state_path):
            state = json.load(open(state_path))
        elif "--state-url" in opt:
            state = get(opt["--state-url"][0], tries=2)
    except Exception as e:
        print("no earlier readings:", str(e)[:80])
    status = {"ran_at": iso(time.time() * 1000)}
    files = {}
    try:
        market, coins = exchange(status)
        files["flows_market"] = to_csv(["tf", "t", "vol_usd", "net_usd", "btc_net", "eth_net", "alt_net", "n", "n_pos"], market)
        files["flows_coins"] = to_csv(["tf", "side", "rank", "symbol", "vol_usd", "net_usd", "net_pct", "px_chg"], coins)
    except Exception as e:
        status["exchange_error"] = str(e)[:200]
    try:
        rows = chains(state, status)
        for r in rows:
            r["asof"] = status["ran_at"]
        files["flows_chains"] = to_csv(["chain", "stables_usd", "st_chg_1h", "st_chg_4h", "st_chg_1d", "st_chg_1w", "dex_1d", "dex_1d_prev",
                                        "dex_1w", "dex_1w_prev", "asof"], rows)
        if state_path:
            json.dump(state, open(state_path, "w"), separators=(",", ":"))
    except Exception as e:
        status["chains_error"] = str(e)[:200]
    for k, v in files.items():
        open(os.path.join(out, k + ".csv"), "w").write(v)
    json.dump(status, open(os.path.join(out, "flows_status.json"), "w"), indent=1)
    print(json.dumps(status))
    if "--seal" in opt and files:
        pub, target = opt["--seal"]
        open(target, "wb").write(seal(pub, {"files": {k: {"csv": v} for k, v in files.items()}, "built": status["ran_at"]}))
        print("sealed", target)
    return 0 if len(files) == 3 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
