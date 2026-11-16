"""Paper book: a simulated $100,000 account that trades on UBIK's forecasts.

Two kinds of position:
  contracts  YES or NO on a Polymarket or Kalshi market that UBIK lists as the same question as its forecast (or one
             that contains it, or is contained by it), bought only when UBIK's view beats the price after costs
  assets     a commodity, currency, index or crypto that UBIK says the outcome affects, traded long or short only when
             the move UBIK's view implies, net of what the market already prices, pays at least twice the stop

Every hour (--mark) it re-prices the book and applies stops, targets and settlements. Once a day (--trade, or --auto
after 13:00 UTC) it also scans the open forecasts for new trades; a Claude review (tools/paper_review.py) checks each
contract's wording against the forecast and estimates how far each asset would move. Without ANTHROPIC_API_KEY the
contracts still trade on the rules alone and assets are skipped.

State lives in paper_state.enc (AES-GCM under PAPER_KEY); the page reads paper.bin, sealed with the site's public key
like the rest of the desk. paper_status.json says whether the run worked, with no positions or prices in it.

Env: UBIK_KEY (a holder's key), PAPER_KEY (any long passphrase), ANTHROPIC_API_KEY (optional).
Usage: python3 tools/paper_engine.py [--mark | --trade | --auto] [--dry]
"""
import base64, gzip, hashlib, json, math, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
UA = {"User-Agent": "Mozilla/5.0 (regime-desk paper book)", "Accept": "application/json"}
STATE, SEALED, STATUS = os.path.join(ROOT, "paper_state.enc"), os.path.join(ROOT, "paper.bin"), os.path.join(ROOT, "paper_status.json")

# ---- the rules, in one place --------------------------------------------------------------------------------------
START_CASH = 100_000.0
SHRINK = 0.6               # weight on UBIK's figure; the rest on the market's (UBIK is good, not infallible)
MIN_EDGE = 0.05            # contracts: fair value minus the price paid, after fees, at least 5 points (was 7 until 17 Nov 2026)
MIN_ROI = 0.15             # contracts: expected return on the stake at least 15%
PRICE_BAND = (0.05, 0.95)  # no contracts priced at the extremes, where small errors decide everything
KELLY = 0.25               # quarter Kelly
MAX_CONTRACT = 0.03        # at most 3% of equity in one contract
MAX_CONTRACTS = 0.25       # at most 25% of equity in contracts
DEPTH_SHARE = 0.2          # take at most 20% of what is offered within 2 cents of the best price
ASSET_KINDS = {"commodity", "currency", "index", "crypto"}   # liquid proxies; single stocks are watched, not traded
MIN_RR = 2.0               # assets: target at least twice the stop
ATR_STOP = 2.0             # assets: stop two average true ranges away
RISK_PER_TRADE = 0.0075    # assets: lose at most 0.75% of equity at the stop
MAX_ASSET = 0.15           # at most 15% of equity in one asset
MAX_ASSETS = 0.60          # at most 60% of equity gross in assets
MAX_OPEN = 15
COST = {"commodity": 0.0005, "index": 0.0003, "currency": 0.0002, "crypto": 0.001}   # slippage and fees, each way
TRADE_HOUR_UTC = 13

YAHOO = {"BRENT": "BZ=F", "LCO": "BZ=F", "WTI": "CL=F", "CL": "CL=F", "NG": "NG=F", "NATGAS": "NG=F", "TTF": "TTF=F",
         "GOLD": "GC=F", "XAU": "GC=F", "GC": "GC=F", "SILVER": "SI=F", "XAG": "SI=F", "COPPER": "HG=F", "HG": "HG=F",
         "ZW": "ZW=F", "WHEAT": "ZW=F", "ZC": "ZC=F", "CORN": "ZC=F", "ZS": "ZS=F", "SOY": "ZS=F", "CC": "CC=F",
         "COCOA": "CC=F", "KC": "KC=F", "COFFEE": "KC=F", "SB": "SB=F", "SUGAR": "SB=F", "URANIUM": "UX=F",
         "SPX": "^GSPC", "SP500": "^GSPC", "S&P500": "^GSPC", "NDX": "^NDX", "NASDAQ100": "^NDX", "DJI": "^DJI", "DAX": "^GDAXI",
         "CAC": "^FCHI", "CAC40": "^FCHI", "FTSE": "^FTSE", "FTSE100": "^FTSE", "N225": "^N225", "NIKKEI": "^N225",
         "HSI": "^HSI", "KOSPI": "^KS11", "SX5E": "^STOXX50E", "STOXX50": "^STOXX50E", "NIFTY": "^NSEI", "NIFTY50": "^NSEI",
         "IBEX": "^IBEX", "FTSEMIB": "FTSEMIB.MI", "ASX200": "^AXJO", "TSX": "^GSPTSE", "SSE": "000001.SS", "BIST100": "XU100.IS"}


def now():
    return datetime.now(timezone.utc)


def iso(t=None):
    return (t or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def get(url, headers=None, tries=3, raw=False):
    h = dict(UA, **(headers or {}))
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                body = r.read()
                return (json.loads(body), dict(r.headers)) if raw else json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(6 * (i + 1)); continue
            raise
        except Exception:
            if i < tries - 1:
                time.sleep(3 * (i + 1)); continue
            raise


# ---- state, encrypted at rest -------------------------------------------------------------------------------------
def _aes():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    k = os.environ.get("PAPER_KEY", "")
    if len(k) < 16:
        sys.exit("PAPER_KEY is missing or shorter than 16 characters")
    return AESGCM(hashlib.sha256(("regime-desk paper book\n" + k).encode()).digest())


def load_state():
    if not os.path.exists(STATE):
        return {"started": iso(), "cash": START_CASH, "positions": [], "closed": [], "equity": [], "scan": [], "traded_on": None, "seq": 0}
    raw = open(STATE, "rb").read()
    return json.loads(gzip.decompress(_aes().decrypt(raw[:12], raw[12:], None)))


def save_state(s):
    nonce = os.urandom(12)
    open(STATE, "wb").write(nonce + _aes().encrypt(nonce, gzip.compress(json.dumps(s, separators=(",", ":")).encode(), 9), None))


def seal_for_site(obj):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    pub = serialization.load_pem_public_key(open(os.path.join(HERE, "site_pub.pem"), "rb").read())
    key, nonce = os.urandom(32), os.urandom(12)
    locked = pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    body = AESGCM(key).encrypt(nonce, gzip.compress(json.dumps(obj, separators=(",", ":")).encode(), 9), None)
    open(SEALED, "wb").write(len(locked).to_bytes(2, "big") + locked + nonce + body)


# ---- UBIK ---------------------------------------------------------------------------------------------------------
def ubik_open(status):
    key = os.environ.get("UBIK_KEY", "").strip()
    hdr = {"Authorization": "Bearer " + key} if key else {}
    out, page = [], 1
    while True:
        d, h = get(f"https://api.ubik.gold/forecasts?status=open&per_page=100&page={page}&include=history,details,related", hdr, raw=True)
        status["ubik_key"] = h.get("x-ubik-key") or h.get("X-Ubik-Key") or ("none" if not key else "unknown")
        out += d.get("data") or []
        if page >= (d.get("meta") or {}).get("pages", 1):
            break
        page += 1
    return out


def ubik_one(fid):
    key = os.environ.get("UBIK_KEY", "").strip()
    try:
        return get(f"https://api.ubik.gold/forecasts/{fid}", {"Authorization": "Bearer " + key} if key else {}).get("data")
    except Exception:
        return None


# ---- prediction markets -------------------------------------------------------------------------------------------
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def quote_contract(venue, mid):
    """Best bid and ask for YES and NO, the depth near the ask, and whether it has settled and how."""
    if venue == "Polymarket":
        m = get(f"https://clob.polymarket.com/markets/{mid}")
        toks = {t["outcome"].upper(): t for t in m.get("tokens") or []}
        if "YES" not in toks or "NO" not in toks:
            return None
        q = {"closed": bool(m.get("closed")), "result": None, "ends": m.get("end_date_iso"), "tokens": {s: toks[s]["token_id"] for s in ("YES", "NO")}}
        if q["closed"]:
            win = [s for s in ("YES", "NO") if toks[s].get("winner")]
            q["result"] = win[0] if win else None
        for side in ("YES", "NO"):
            b = get(f"https://clob.polymarket.com/book?token_id={toks[side]['token_id']}")
            asks = sorted(((_f(a["price"]), _f(a["size"])) for a in b.get("asks") or []), key=lambda x: x[0])
            bids = sorted(((_f(a["price"]), _f(a["size"])) for a in b.get("bids") or []), key=lambda x: -x[0])
            ask = asks[0][0] if asks else None
            q[side] = {"ask": ask, "bid": bids[0][0] if bids else None,
                       "depth": sum(s for p, s in asks if ask is not None and p <= ask + 0.02), "fee": 0.0}
        return q
    if venue == "Kalshi":
        m = get(f"https://api.elections.kalshi.com/trade-api/v2/markets/{urllib.parse.quote(mid)}")["market"]
        pick = lambda *ks: next((_f(m[k]) for k in ks if m.get(k) not in (None, "")), None)
        ya, yb = pick("yes_ask_dollars"), pick("yes_bid_dollars")
        na, nb = pick("no_ask_dollars"), pick("no_bid_dollars")
        if ya is None and m.get("yes_ask") is not None:   # older shape, in cents
            ya, yb, na, nb = [(_f(m.get(k)) or 0) / 100 for k in ("yes_ask", "yes_bid", "no_ask", "no_bid")]
        res = (m.get("result") or "").upper()
        fee = lambda p: 0.07 * p * (1 - p) if p else 0.0
        return {"closed": m.get("status") in ("closed", "settled", "finalized", "determined"), "result": res if res in ("YES", "NO") else None,
                "ends": m.get("close_time"),
                "YES": {"ask": ya or None, "bid": yb or None, "depth": pick("yes_ask_size_fp", "yes_ask_size") or 0, "fee": fee(ya)},
                "NO": {"ask": na or None, "bid": nb or None, "depth": pick("no_ask_size_fp", "no_ask_size") or 0, "fee": fee(na)}}
    return None


# ---- asset prices -------------------------------------------------------------------------------------------------
def yahoo_symbol(a):
    s = (a.get("symbol") or "").upper().replace(" ", "")
    kind = a.get("kind")
    if kind == "crypto":
        return f"{s}-USD"
    if kind == "currency" and len(s) == 6 and s.isalpha():
        return f"{s}=X"
    return YAHOO.get(s)


_px = {}
def price_history(sym):
    """Last price and 14-day average true range (as a fraction of price) from Yahoo's daily chart."""
    if sym in _px:
        return _px[sym]
    d = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=2mo&interval=1d")
    r = d["chart"]["result"][0]; q = r["indicators"]["quote"][0]
    rows = [(h, l, c) for h, l, c in zip(q["high"], q["low"], q["close"]) if None not in (h, l, c)]
    last = _f(r["meta"].get("regularMarketPrice")) or rows[-1][2]
    trs = [max(h - l, abs(h - rows[i - 1][2]), abs(l - rows[i - 1][2])) for i, (h, l, c) in enumerate(rows) if i]
    atr = sum(trs[-14:]) / max(1, len(trs[-14:]))
    _px[sym] = {"price": last, "atr_pct": atr / last if last else None}
    return _px[sym]


# ---- the book -----------------------------------------------------------------------------------------------------
def equity(s):
    e = s["cash"]
    for p in s["positions"]:
        e += p["qty"] * (p.get("mark") or p["entry"]) * (1 if p["kind"] == "contract" or p["dir"] > 0 else -1)
    return e


def exposure(s, kind):
    return sum(p["qty"] * (p.get("mark") or p["entry"]) for p in s["positions"] if p["kind"] == kind)


def close(s, p, price, why):
    p.update(exit=price, closed=iso(), why_closed=why)
    gross = p["qty"] * price
    if p["kind"] == "contract":
        s["cash"] += gross
        p["pnl"] = gross - p["qty"] * p["entry"]
    else:
        cost = gross * COST.get(p["class"], 0.0005)
        s["cash"] += (gross - cost) if p["dir"] > 0 else -(gross + cost)
        p["pnl"] = (price - p["entry"]) * p["qty"] * p["dir"] - cost
    s["positions"].remove(p); s["closed"].append(p)
    log(f"close {p['label']}: {why}, pnl {p['pnl']:+.0f}")


def mark(s, forecasts, status):
    """Re-price everything; settle, stop, take profit, or close when UBIK's view has turned."""
    by_id = {f["question_id"]: f for f in forecasts}
    for p in list(s["positions"]):
        try:
            if p["kind"] == "contract":
                q = quote_contract(p["venue"], p["market"])
                if not q:
                    continue
                if q["closed"] and q["result"]:
                    close(s, p, 1.0 if q["result"] == p["side"] else 0.0, "settled " + q["result"]); continue
                bid = q[p["side"]]["bid"]
                if bid is not None:
                    p["mark"] = bid
                f = by_id.get(p["forecast"]) or ubik_one(p["forecast"])
                if f and f.get("probability") is not None and bid is not None:
                    yb, ya = q["YES"]["bid"], q["YES"]["ask"]
                    fair = fair_for(f["probability"], p["inverted"], (yb + ya) / 2 if yb is not None and ya is not None else None, p["side"])
                    p["fair"] = fair
                    if bid >= fair - 0.01:
                        close(s, p, bid, "price reached fair value"); continue
                    if fair - bid < -0.05:
                        close(s, p, bid, "UBIK turned against it"); continue
            else:
                px = price_history(p["yahoo"])["price"]
                p["mark"] = px
                hit_stop = px <= p["stop"] if p["dir"] > 0 else px >= p["stop"]
                hit_target = px >= p["target"] if p["dir"] > 0 else px <= p["target"]
                f = by_id.get(p["forecast"])
                if hit_stop:
                    close(s, p, px, "stop"); continue
                if hit_target:
                    close(s, p, px, "target"); continue
                if f is None:      # no longer open: settled, so the catalyst is spent
                    close(s, p, px, "forecast settled"); continue
                if p.get("deadline") and now().date() > datetime.fromisoformat(p["deadline"]).date() + timedelta(days=2):
                    close(s, p, px, "past the deadline"); continue
                if f.get("probability") is not None and (f["probability"] - p["market_prob"]) * p["sign"] <= 0:
                    close(s, p, px, "UBIK turned against it"); continue
        except Exception as e:
            status["errors"].append(f"mark {p['kind']}: {type(e).__name__}")
    status["marked"] = len(s["positions"])


def fair_for(p_ubik, inverted, mid, side):
    """UBIK's figure for this market's YES (flipped when the market asks the opposite), pulled toward the market's."""
    q = 1 - p_ubik if inverted else p_ubik
    q = SHRINK * q + (1 - SHRINK) * (mid if mid is not None else q)
    return q if side == "YES" else 1 - q


def contract_candidates(s, forecasts, status):
    held = {(p.get("venue"), p.get("market")) for p in s["positions"] if p["kind"] == "contract"}
    out = []
    for f in forecasts:
        p_u = f.get("probability")
        if p_u is None or f.get("awaiting"):
            continue
        for m in f.get("markets") or []:
            rel = m.get("relation")
            if rel not in ("same", "broader", "narrower") or (m.get("source"), m.get("id")) in held:
                continue
            try:
                q = quote_contract(m["source"], m["id"])
            except Exception as e:
                status["errors"].append(f"quote {m.get('source')}: {type(e).__name__}"); continue
            if not q or q["closed"]:
                continue
            mid = None
            if q["YES"]["bid"] is not None and q["YES"]["ask"] is not None:
                mid = (q["YES"]["bid"] + q["YES"]["ask"]) / 2
            inv = bool(m.get("inverted"))
            # broader: the market's YES contains the forecast's (so it is at least as likely): only YES is supported.
            # narrower: the market's YES sits inside the forecast's (so it is at most as likely): only NO is supported.
            sides = {"same": ("YES", "NO"), "broader": ("YES",), "narrower": ("NO",)}[rel]
            for side in sides:
                ask, fee = q[side]["ask"], q[side]["fee"]
                if ask is None or not (PRICE_BAND[0] <= ask <= PRICE_BAND[1]):
                    continue
                fair = fair_for(p_u, inv, mid, side)
                cost = ask + fee
                edge = fair - cost
                row = {"type": "contract", "forecast": f["question_id"], "claim": f["claim"], "venue": m["source"], "market": m["id"],
                       "question": m.get("question"), "relation": rel, "inverted": inv, "side": side, "ubik": p_u,
                       "market_mid": mid, "ask": ask, "fee": round(fee, 4), "fair": round(fair, 4), "edge": round(edge, 4),
                       "roi": round(edge / cost, 3), "depth": q[side]["depth"], "rule": f.get("resolution_rule", "")[:1200],
                       "ends": q.get("ends"), "deadline": f.get("deadline")}
                row["pass"] = edge >= MIN_EDGE and edge / cost >= MIN_ROI
                row["why"] = "edge after costs %.0f points, %.0f%% expected return" % (edge * 100, edge / cost * 100)
                out.append(row)
    return out


def asset_candidates(s, forecasts, status):
    held = {(p.get("forecast"), p.get("symbol")) for p in s["positions"] if p["kind"] == "asset"}
    out = []
    for f in forecasts:
        p_u = f.get("probability")
        if p_u is None or f.get("awaiting"):
            continue
        venue_prob = None
        for m in f.get("markets") or []:
            if m.get("relation") == "same" and m.get("probability") is not None:
                venue_prob = 1 - m["probability"] if m.get("inverted") else m["probability"]; break
        for a in f.get("assets") or []:
            if (f["question_id"], a.get("symbol")) in held:
                continue
            row = {"type": "asset", "forecast": f["question_id"], "claim": f["claim"], "symbol": a.get("symbol"), "name": a.get("name"),
                   "class": a.get("kind"), "why_linked": a.get("why"), "ubik": p_u, "venue_prob": venue_prob, "deadline": f.get("deadline"),
                   "reasoning": (f.get("reasoning") or "")[:800], "rises_if": f.get("rises_if"), "falls_if": f.get("falls_if")}
            if a.get("kind") not in ASSET_KINDS:
                row.update({"pass": False, "why": "single stock: watched, not traded" if a.get("kind") == "stock" else f"{a.get('kind')}: not traded yet"}); out.append(row); continue
            sym = yahoo_symbol(a)
            if not sym:
                row.update({"pass": False, "why": "no price feed for this symbol"}); out.append(row); continue
            try:
                ph = price_history(sym)
            except Exception as e:
                status["errors"].append(f"price {a.get('kind')}: {type(e).__name__}")
                row.update({"pass": False, "why": "price feed did not answer"}); out.append(row); continue
            row.update({"yahoo": sym, "price": ph["price"], "atr_pct": ph["atr_pct"], "pass": None})
            out.append(row)
    return out


def size_assets(s, rows, status):
    """With Claude's move estimates in hand: the expected move net of what is priced, the stop, the target, the size."""
    for r in rows:
        if r.get("pass") is False or "move_if_yes_pct" not in r:
            if r.get("pass") is None:
                r.update({"pass": False, "why": r.get("why") or "no review"})
            continue
        m = r["venue_prob"] if r["venue_prob"] is not None else r.get("market_prob")
        if m is None:
            r.update({"pass": False, "why": "no view of what the market prices"}); continue
        spread = (r["move_if_yes_pct"] - r["move_if_no_pct"]) / 100.0          # YES outcome minus NO outcome
        priced = m * r["move_if_yes_pct"] / 100 + (1 - m) * r["move_if_no_pct"] / 100
        exp = (r["ubik"] - m) * spread * r.get("confidence", 0.5)              # what UBIK's edge is worth, haircut
        sign = 1 if exp > 0 else -1
        # the best case: UBIK is right and the outcome lands the way it leans
        best = ((r["move_if_yes_pct"] / 100 - priced) if r["ubik"] >= m else (r["move_if_no_pct"] / 100 - priced)) * sign
        stop = ATR_STOP * (r["atr_pct"] or 0.02)
        rr = best / stop if stop else 0
        r.update({"market_prob": m, "expected_pct": round(exp * 100, 2), "best_pct": round(best * 100, 2), "stop_pct": round(stop * 100, 2),
                  "rr": round(rr, 2), "dir": sign})
        if abs(exp) < 0.005:
            r.update({"pass": False, "why": "expected move under 0.5%"}); continue
        if rr < MIN_RR:
            r.update({"pass": False, "why": f"reward/risk {rr:.1f}, needs {MIN_RR:.0f}"}); continue
        r.update({"pass": True, "why": f"{'long' if sign > 0 else 'short'}: expected {exp * 100:+.1f}%, best {best * 100:.1f}% vs stop {stop * 100:.1f}%"})


def open_positions(s, contracts, assets, status, dry):
    eq = equity(s)
    for r in sorted([c for c in contracts if c.get("pass")], key=lambda c: -c["edge"]):
        if len(s["positions"]) >= MAX_OPEN:
            break
        cost = r["ask"] + r["fee"]
        kelly = max(0.0, (r["fair"] - cost) / (1 - cost)) * KELLY
        stake = min(kelly * eq, MAX_CONTRACT * eq, MAX_CONTRACTS * eq - exposure(s, "contract"), s["cash"] * 0.9)
        depth = r["depth"] or 200          # a venue that does not show depth: assume a thin book
        qty = math.floor(min(stake / cost, depth * DEPTH_SHARE))
        if qty < 1:
            r["why"] += "; no room or depth to size it"; r["pass"] = False; continue
        if dry:
            r["why"] += f"; would buy {qty}"; continue
        s["seq"] += 1
        s["positions"].append({"id": s["seq"], "kind": "contract", "label": f"{r['side']} · {(r['question'] or r['market'])[:70]}", "venue": r["venue"],
                               "market": r["market"], "question": r["question"], "side": r["side"], "inverted": r["inverted"],
                               "forecast": r["forecast"], "claim": r["claim"], "qty": qty, "entry": cost, "mark": r["ask"], "fair": r["fair"],
                               "opened": iso(), "why": r["why"], "review": r.get("review_reason"), "dir": 1})
        s["cash"] -= qty * cost
        r["why"] += f"; bought {qty} at {cost:.3f}"
        log("open", s["positions"][-1]["label"], qty)
    for r in sorted([a for a in assets if a.get("pass")], key=lambda a: -a["rr"]):
        if len(s["positions"]) >= MAX_OPEN:
            break
        px = r["price"]; risk = RISK_PER_TRADE * eq
        notional = min(risk / (r["stop_pct"] / 100), MAX_ASSET * eq, MAX_ASSETS * eq - exposure(s, "asset"))
        if notional < 500:
            r["why"] += "; no room to size it"; r["pass"] = False; continue
        qty = notional / px
        if dry:
            r["why"] += f"; would trade ${notional:,.0f}"; continue
        cost = notional * COST.get(r["class"], 0.0005)
        s["seq"] += 1
        d = r["dir"]
        s["positions"].append({"id": s["seq"], "kind": "asset", "label": f"{'LONG' if d > 0 else 'SHORT'} {r['symbol']}", "symbol": r["symbol"],
                               "yahoo": r["yahoo"], "class": r["class"], "dir": d, "sign": d * (1 if r["move_if_yes_pct"] >= r["move_if_no_pct"] else -1),
                               "qty": qty, "entry": px, "mark": px, "stop": px * (1 - d * r["stop_pct"] / 100),
                               "target": px * (1 + d * r["best_pct"] / 100), "market_prob": r["market_prob"], "forecast": r["forecast"],
                               "claim": r["claim"], "deadline": r["deadline"], "opened": iso(), "why": r["why"], "review": r.get("review_reason")})
        s["cash"] += (-notional - cost) if d > 0 else (notional - cost)
        r["why"] += f"; traded ${notional:,.0f}"
        log("open", s["positions"][-1]["label"], round(notional))


def site_files(s, status):
    eq = equity(s)
    closed = s["closed"]
    wins = [p for p in closed if p.get("pnl", 0) > 0]
    summary = {"built": iso(), "started": s["started"], "start_cash": START_CASH, "equity": round(eq, 2), "cash": round(s["cash"], 2),
               "pnl": round(eq - START_CASH, 2), "pnl_pct": round((eq / START_CASH - 1) * 100, 2), "open": len(s["positions"]),
               "contracts_gross": round(exposure(s, "contract"), 2), "assets_gross": round(exposure(s, "asset"), 2),
               "closed": len(closed), "hit_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
               "realized": round(sum(p.get("pnl", 0) for p in closed), 2), "traded_on": s.get("traded_on"),
               "reviewed": s.get("scan_reviewed"), "ubik_key": status.get("ubik_key"),
               "rules": {"shrink": SHRINK, "min_edge": MIN_EDGE, "kelly": KELLY, "min_rr": MIN_RR, "risk_per_trade": RISK_PER_TRADE}}
    return {"built": iso(), "files": {"paper_summary": {"json": summary}, "paper_positions": {"json": s["positions"]},
                                      "paper_closed": {"json": closed[-300:]}, "paper_equity": {"json": s["equity"][-24 * 120:]},
                                      "paper_scan": {"json": s.get("scan", [])}}}


def main():
    args = set(sys.argv[1:])
    dry = "--dry" in args
    t = now()
    status = {"run": iso(t), "mode": "mark", "errors": [], "review": None}
    if len(os.environ.get("PAPER_KEY", "")) < 16:      # not set up yet: say so and stop without failing the job
        status["errors"].append("waiting for the PAPER_KEY secret")
        json.dump(status, open(STATUS, "w"), indent=1); log("no PAPER_KEY yet; nothing to do"); return
    s = load_state()
    try:
        forecasts = ubik_open(status)
    except Exception as e:
        forecasts = []
        status["errors"].append(f"ubik: {type(e).__name__}")
    status["forecasts"] = len(forecasts)
    status["with_probability"] = sum(1 for f in forecasts if f.get("probability") is not None)
    mark(s, forecasts, status)

    trade = "--trade" in args or ("--auto" in args and t.hour >= TRADE_HOUR_UTC and s.get("traded_on") != t.strftime("%Y-%m-%d"))
    if trade and forecasts:
        status["mode"] = "trade"
        contracts = contract_candidates(s, forecasts, status)
        assets = asset_candidates(s, forecasts, status)
        try:
            import paper_review
            status["review"] = paper_review.review(contracts, assets, status)
        except Exception as e:
            status["errors"].append(f"review: {type(e).__name__}")
            status["review"] = False
        if not status["review"]:
            for a in assets:
                if a.get("pass") is None:
                    a.update({"pass": False, "why": "assets need the Claude review"})
        size_assets(s, assets, status)
        open_positions(s, contracts, assets, status, dry)
        keep = ("type", "forecast", "claim", "venue", "market", "question", "relation", "side", "ubik", "market_mid", "ask", "fair", "edge",
                "symbol", "name", "class", "market_prob", "expected_pct", "best_pct", "stop_pct", "rr", "pass", "why", "review_reason")
        s["scan"] = [{k: r.get(k) for k in keep if r.get(k) is not None} for r in contracts + assets]
        s["scan_at"] = iso()
        s["scan_reviewed"] = bool(status["review"])
        if not dry:
            s["traded_on"] = t.strftime("%Y-%m-%d")
        status["candidates"] = len(contracts) + len(assets)
        # counts only, so the public status file can say why nothing traded without showing what was looked at
        reason = lambda r: re.sub(r"[-+]?\d[\d.,]*%?", "#", (r.get("why") or "").split(";")[0])[:60]
        hist = {}
        for r in contracts + assets:
            if not r.get("pass"):
                k = r["type"] + ": " + reason(r); hist[k] = hist.get(k, 0) + 1
        status["why_not"] = dict(sorted(hist.items(), key=lambda kv: -kv[1])[:12])
        exps = sorted((abs(a["expected_pct"]) for a in assets if a.get("expected_pct") is not None), reverse=True)
        status["assets_expected_pct_top5"] = exps[:5]
        status["contracts_seen"] = len(contracts)
        status["contracts_positive_edge"] = sum(1 for c in contracts if (c.get("edge") or 0) > 0)
        status["contracts_best_edge_pts"] = round(max([c.get("edge") or -1 for c in contracts] or [0]) * 100, 1)
        rels = {}
        for f in forecasts:
            for m in f.get("markets") or []:
                rels[m.get("relation")] = rels.get(m.get("relation"), 0) + 1
        status["market_relations"] = rels
        status["assets_by_class"] = {k: sum(1 for a in assets if a.get("class") == k) for k in {a.get("class") for a in assets}}
        status["passed"] = sum(1 for r in contracts + assets if r.get("pass"))

    eq = equity(s)
    s["equity"].append([iso(t), round(eq, 2), round(s["cash"], 2), round(exposure(s, "contract") + exposure(s, "asset"), 2)])
    status["open"] = len(s["positions"])
    if not dry:
        save_state(s)
        seal_for_site(site_files(s, status))
    json.dump(status, open(STATUS, "w"), indent=1)
    log(f"done: {status['mode']}, equity {eq:,.0f}, {len(s['positions'])} open, errors {len(status['errors'])}")
    if dry:
        print(json.dumps(s.get("scan", [])[:40], indent=1, default=str))


if __name__ == "__main__":
    main()
