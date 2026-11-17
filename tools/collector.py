"""Headroom Regime Desk collector.

Pulls public market data, works out the conditions score (trend and fuel) and the macro stress reading, and writes the
CSV/JSON files the dashboard reads. Usage:

    python3 collector.py <out_dir> [events.json] [holdings.json]

Sources: FRED (rates, dollar, credit, VIX, S&P 500, Nasdaq, Brent), Hyperliquid
(BTC/ETH/HYPE candles, BTC funding, HOOD/gold/silver perps), alternative.me
(Fear & Greed), Dexscreener + GeckoTerminal (portfolio tokens), DefiLlama
(chain TVL, DEX volume, fees, stablecoins), Kalshi + Polymarket (event prices), api.ubik.gold
(public forecast list).
"""
import collections, csv, io, json, math, os, sys, time, urllib.parse, urllib.request
from datetime import date as date_cls, datetime, timedelta, timezone

import numpy as np
import pandas as pd

UA = {"User-Agent": "headroom-regime-desk/1.0", "Accept": "application/json"}
NOW = datetime.now(timezone.utc)
TODAY = NOW.date()


def _fetch(req, tries=4):
    """One request with patience: waits and retries on rate limits, server errors and dropped connections."""
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and i < tries - 1:
                time.sleep(15 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise


def get(url, raw=False, tries=4):
    body = _fetch(urllib.request.Request(url, headers=UA), tries)
    return body.decode() if raw else json.loads(body)


def hl(body):
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **UA},
    )
    return json.loads(_fetch(req))


# ---------- raw series ----------

def fred(series, start="2015-01-01"):
    txt = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}", raw=True)
    df = pd.read_csv(io.StringIO(txt))
    df.columns = ["date", "v"]
    df["date"] = pd.to_datetime(df["date"])
    df["v"] = pd.to_numeric(df["v"], errors="coerce")
    return df.dropna().set_index("date")["v"]


def hl_daily(coin, days=800):
    now = int(time.time() * 1000)
    c = hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d",
                                              "startTime": now - days * 86400000, "endTime": now}})
    s = pd.Series({pd.to_datetime(x["t"], unit="ms"): float(x["c"]) for x in c})
    return s.sort_index()


def hl_funding_daily(coin="BTC", days=800):
    start = int(time.time() * 1000) - days * 86400000
    rows = []
    while True:
        page = hl({"type": "fundingHistory", "coin": coin, "startTime": start})
        if not page:
            break
        rows += page
        last = page[-1]["time"]
        if len(page) < 500 or last <= start:
            break
        start = last + 1
        time.sleep(0.3)
    s = pd.Series({pd.to_datetime(r["time"], unit="ms"): float(r["fundingRate"]) for r in rows})
    # hourly rate -> annualised %, daily mean
    return (s.resample("D").mean() * 24 * 365 * 100).dropna()


def fear_greed():
    d = get("https://api.alternative.me/fng/?limit=900")["data"]
    s = pd.Series({pd.to_datetime(int(x["timestamp"]), unit="s"): float(x["value"]) for x in d})
    return s.sort_index()


# ---------- scoring ----------

def pctile_series(s, invert=False, window=730, min_obs=150):
    """Percentile (0-100) of each day's value within its trailing window."""
    vals = s.values
    out = []
    for i in range(len(vals)):
        lo = max(0, i - window + 1)
        w = vals[lo:i + 1]
        if len(w) < min_obs or np.isnan(vals[i]):
            out.append(np.nan)
            continue
        p = (np.sum(w < vals[i]) + 0.5 * np.sum(w == vals[i])) / len(w) * 100
        out.append(100 - p if invert else p)
    return pd.Series(out, index=s.index)


def daily(s):
    return s.resample("D").last().ffill()


def zone(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    if x < 20: return "Headwind"
    if x < 40: return "Soft"
    if x <= 60: return "Neutral"
    if x <= 80: return "Firm"
    return "Tailwind"


def reading(p, good):
    if p is None or math.isnan(p) or not good: return ""
    if p < 20: return "headwind" if good > 0 else "calm"
    if p > 80: return "tailwind" if good > 0 else "stress"
    return "neutral"


# Why the score is built this way (tested 11 Nov 2026 on June 2018 to Nov 2026, about 110 separate four-week windows):
#   the old meter read fear as a reason to add and froth as a reason to cut. Against altcoin and BTC returns
#   2 weeks to 6 months later it had no link, or the wrong sign: its add zone was followed by more losses.
#   What had a record was trend plus fuel read as support: alts and BTC against their 200-day averages,
#   stablecoin supply growth and net dollar liquidity growth. Rank correlation with alt returns four weeks
#   later 0.15; readings above 80 were followed by a median +15% and readings below 20 by -5%, in the same
#   order in 2020-21, 2022-23 and 2024-26 (in 2018-19 the score never got above 80). The middle said nothing.
#   Rates and cross-asset inputs stay as a macro stress reading for context and are not scored.
#   Any new input has to be tested on this record before it is scored.

_CACHE = {}


def stable_supply():
    if "stab" not in _CACHE:
        st = get("https://stablecoins.llama.fi/stablecoincharts/all")
        s = pd.Series({pd.to_datetime(int(x["date"]), unit="s"): float((x.get("totalCirculatingUSD") or {}).get("peggedUSD") or 0) for x in st}).sort_index()
        _CACHE["stab"] = s[s > 0]
    return _CACHE["stab"]


def market_caps():
    """Daily market caps since 2017: BTC, and alts (the whole market minus BTC, ETH and stablecoins)."""
    if "caps" not in _CACHE:
        df = _cmc_global()
        # CoinMarketCap has a few one-day glitches (2019-12-20, 2020-02-02, 2020-05-18 to 20): drop points far off the local median
        for c in ["total", "btcd", "ethd"]:
            s = df[c].where(df[c] > 0)
            med = s.rolling(9, center=True, min_periods=4).median()
            df[c] = s.mask(np.log(s / med).abs() > 0.35).interpolate(limit_area="inside")
        df["stables"] = stable_supply().reindex(df.index).ffill().fillna(0)
        df["btc"] = df.total * df.btcd / 100
        df["alts"] = df.total * (1 - (df.btcd + df.ethd) / 100) - df.stables
        df["ethbtc"] = df.ethd / df.btcd
        _CACHE["caps"] = df
    return _CACHE["caps"].copy()


def build_regime(out):
    """Two readings. Conditions (scored): trend and fuel, where high means supportive.
    Macro stress (context only): rates and cross-asset, where high means stress. Positioning rows are watched."""
    s = {}
    s["dgs10"] = fred("DGS10"); s["dfii10"] = fred("DFII10"); s["dxy"] = fred("DTWEXBGS")
    s["hy"] = fred("BAMLH0A0HYM2"); s["vix"] = fred("VIXCLS"); s["spx"] = fred("SP500")
    s["ndx"] = fred("NASDAQCOM"); s["brent"] = fred("DCOILBRENTEU")
    btc = hl_daily("BTC"); eth = hl_daily("ETH")
    fund = hl_funding_daily("BTC"); fng = fear_greed()
    caps = market_caps()
    alts_cap, btc_cap = caps.alts.dropna(), caps.btc.dropna()
    stab = daily(stable_supply())
    netliq = (daily(fred("WALCL")) - daily(fred("WTREGEN")) - daily(fred("RRPONTSYD")) * 1000).dropna()   # $ millions

    comps = [
        # part, group, key, label, unit, raw series (daily), invert, good (1 high is supportive, -1 high is stress, 0 no direction), source, why
        ("conditions", "Trend", "alt_200d", "Altcoin market vs 200-day average", "%",
         (alts_cap / alts_cap.rolling(200, min_periods=150).mean() - 1) * 100, False, 1, "CoinMarketCap, Coin Metrics, DefiLlama", "Alts above their own trend tend to keep going"),
        ("conditions", "Trend", "btc_200d", "BTC vs 200-day average", "%",
         (btc_cap / btc_cap.rolling(200, min_periods=150).mean() - 1) * 100, False, 1, "CoinMarketCap", "BTC above its trend carries the rest"),
        ("conditions", "Fuel", "stables_30d", "Stablecoin supply, 30-day change", "%",
         stab.pct_change(30, fill_method=None) * 100, False, 1, "DefiLlama stablecoins", "New dollars entering crypto are buying power"),
        ("conditions", "Fuel", "netliq_28d", "Net dollar liquidity, 4-week change", "%",
         netliq.pct_change(28, fill_method=None) * 100, False, 1, "FRED WALCL, WTREGEN, RRPONTSYD", "Fed balance sheet minus Treasury cash and reverse repo"),
        ("stress", "Macro stress: rates", "us10y_20d", "US 10-year yield, 20-day change", "pp",
         daily(s["dgs10"]).diff(28), False, -1, "FRED DGS10", "Rising yields tighten conditions"),
        ("stress", "Macro stress: rates", "real10y_20d", "Real 10-year yield, 20-day change", "pp",
         daily(s["dfii10"]).diff(28), False, -1, "FRED DFII10", "Rising real yields weigh on risk assets"),
        ("stress", "Macro stress: rates", "dollar_20d", "Broad dollar, 20-day change", "%",
         daily(s["dxy"]).pct_change(28, fill_method=None) * 100, False, -1, "FRED DTWEXBGS", "A stronger dollar tightens conditions"),
        ("stress", "Macro stress: rates", "hy_spread", "High-yield credit spread", "%",
         daily(s["hy"]), False, -1, "FRED BAMLH0A0HYM2", "Wide spreads mean credit is nervous"),
        ("stress", "Macro stress: cross-asset", "vix", "VIX", "",
         daily(s["vix"]), False, -1, "FRED VIXCLS", "High VIX means stocks expect trouble"),
        ("stress", "Macro stress: cross-asset", "spx_200d", "S&P 500 vs 200-day average", "%",
         (daily(s["spx"]) / daily(s["spx"]).rolling(290, min_periods=200).mean() - 1) * 100, True, -1, "FRED SP500", "Stocks below trend mean risk is being cut"),
        ("stress", "Macro stress: cross-asset", "ndx_50d", "Nasdaq vs 50-day average", "%",
         (daily(s["ndx"]) / daily(s["ndx"]).rolling(72, min_periods=50).mean() - 1) * 100, True, -1, "FRED NASDAQCOM", "Tech losing short-term momentum"),
        ("stress", "Macro stress: cross-asset", "brent_20d", "Brent oil, 20-day change", "%",
         daily(s["brent"]).pct_change(28, fill_method=None) * 100, False, -1, "FRED DCOILBRENTEU", "An oil spike is a growth tax"),
        ("watched", "Positioning", "funding_7d", "BTC perp funding, 7-day average", "% a year",
         fund.rolling(7, min_periods=3).mean(), False, 0, "Hyperliquid BTC funding", "What longs pay to hold leverage"),
        ("watched", "Positioning", "fng", "Crypto Fear & Greed", "",
         daily(fng), False, 0, "alternative.me", "Mood on a 0 to 100 scale, shown as is"),
        ("watched", "Positioning", "ethbtc_30d", "ETH/BTC, 30-day change", "%",
         ((eth / btc).pct_change(30, fill_method=None)) * 100, False, 0, "Hyperliquid ETH, BTC", "Whether money is moving out along the risk curve"),
    ]
    try:
        oi = None
        for ccy in ("BTC", "ETH"):
            d = get(f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy={ccy}&period=1D")["data"]
            x = pd.Series({pd.to_datetime(int(r[0]), unit="ms").normalize(): float(r[1]) for r in d}).sort_index()
            oi = x if oi is None else oi.add(x, fill_value=0)
        comps.append(("watched", "Positioning", "oi_30d", "BTC and ETH open interest on OKX, 30-day change", "%",
                      oi.pct_change(30, fill_method=None) * 100, False, 0, "OKX", "Leverage building up; ranked against five months only"))
    except Exception as e:
        print("open interest", e)

    pct, rows = {}, []
    for part, grp, key, label, unit, raw, inv, good, src, how in comps:
        raw = raw.dropna()
        if not len(raw):
            continue
        # Fear & Greed is already a 0-100 mood scale, so it is used as is
        p = raw.copy() if key == "fng" else pctile_series(raw, invert=inv, min_obs=60 if key == "oi_30d" else 150)
        pct[key] = p
        pv = float(p.iloc[-1]) if not math.isnan(p.iloc[-1]) else float("nan")
        rows.append({"part": part, "group": grp, "metric": key, "label": label, "value": round(float(raw.iloc[-1]), 3), "unit": unit,
                     "asof": raw.index[-1].strftime("%Y-%m-%d"), "pctile": round(pv, 1), "reading": reading(pv, good),
                     "why": how, "source": src, "scored": part == "conditions", "good": good})
    pd.DataFrame(rows).to_csv(os.path.join(out, "regime_components.csv"), index=False)

    # history since 2018. A reading is carried forward at most 14 days, so a feed that dies drops out instead of freezing.
    idx = pd.date_range("2018-01-01", TODAY, freq="D")
    on = lambda keys: pd.concat([daily(pct[k]).reindex(idx).ffill(limit=14).rename(k) for k in keys if k in pct], axis=1)
    cond = on(["alt_200d", "btc_200d", "stables_30d", "netliq_28d"])
    hist = pd.DataFrame(index=idx)
    hist["trend"] = cond[[c for c in ("alt_200d", "btc_200d") if c in cond]].mean(axis=1)
    hist["fuel"] = cond[[c for c in ("stables_30d", "netliq_28d") if c in cond]].mean(axis=1)
    # the score is the plain average of the four inputs, on days where at least three exist
    hist["score"] = cond.mean(axis=1).where(cond.notna().sum(axis=1) >= 3)
    g = {}
    for name, keys in [("rates", ["us10y_20d", "real10y_20d", "dollar_20d", "hy_spread"]), ("cross", ["vix", "spx_200d", "ndx_50d", "brent_20d"])]:
        f = on(keys)
        g[name] = f.mean(axis=1).where(f.notna().sum(axis=1) >= 3)
    hist["stress"] = ((g["rates"] + g["cross"]) / 2)
    a, b = daily(alts_cap).reindex(idx), daily(btc_cap).reindex(idx)
    hist["alts"] = a / 1e9
    hist["btc"] = b / 1e9
    hist["alt_fwd14"] = (a.shift(-14) / a - 1) * 100
    hist["alt_fwd28"] = (a.shift(-28) / a - 1) * 100
    hist["btc_fwd28"] = (b.shift(-28) / b - 1) * 100
    hist = hist.dropna(subset=["score"])
    hist["zone"] = [zone(x) for x in hist["score"]]
    hist.index.name = "date"
    hist = hist.round(2).reset_index()
    hist["date"] = hist["date"].dt.strftime("%Y-%m-%d")
    hist.to_csv(os.path.join(out, "regime_history.csv"), index=False)
    return s, btc, eth, hist


# ---------- projection ----------

HORIZON = 21


def analog_deltas(series, now_val, h, band=10.0, momentum=None, mom_series=None):
    """Changes over h days from past days whose level sat within `band` of today,
    optionally also sharing the sign of today's 10-day momentum."""
    vals = series.values
    out = []
    for i in range(len(vals) - h):
        if np.isnan(vals[i]) or np.isnan(vals[i + h]) or abs(vals[i] - now_val) > band:
            continue
        if momentum is not None:
            m = mom_series.iloc[i]
            if np.isnan(m) or np.sign(m) != np.sign(momentum):
                continue
        out.append(vals[i + h] - vals[i])
    return np.array(out)


def build_projection(out, hist):
    """Analog fan: where the conditions score went over the next 1-21 days from past days that
    looked like today (similar level, same direction over the last 10 days)."""
    h = hist.copy()
    h["date"] = pd.to_datetime(h["date"])
    h = h.set_index("date")
    rows, groups = [], {}
    score = h["score"]
    now = float(score.iloc[-1])
    mom = score.diff(10)
    now_mom = float(mom.iloc[-1])
    test = analog_deltas(score, now, HORIZON, momentum=now_mom, mom_series=mom)
    use_mom = len(test) >= 40
    method = ("Past days within 10 points of today's score that were also moving the same way over 10 days"
              if use_mom else "Past days within 10 points of today's score")
    for k in range(1, HORIZON + 1):
        d = analog_deltas(score, now, k, momentum=now_mom if use_mom else None, mom_series=mom)
        q = np.percentile(d, [10, 25, 50, 75, 90]) if len(d) else [np.nan] * 5
        row = {"date": (TODAY + timedelta(days=k)).isoformat(), "h": k,
               "p10": round(float(np.clip(now + q[0], 0, 100)), 1), "p25": round(float(np.clip(now + q[1], 0, 100)), 1),
               "p50": round(float(np.clip(now + q[2], 0, 100)), 1), "p75": round(float(np.clip(now + q[3], 0, 100)), 1),
               "p90": round(float(np.clip(now + q[4], 0, 100)), 1), "n": int(len(d))}
        for col, key in [("trend", "trend_p50"), ("fuel", "fuel_p50"), ("stress", "stress_p50")]:
            gs = h[col]; gnow = float(gs.iloc[-1])
            gd = analog_deltas(gs, gnow, k) if not math.isnan(gnow) else []
            row[key] = round(float(np.clip(gnow + np.median(gd), 0, 100)), 1) if len(gd) else None
        rows.append(row)
    pd.DataFrame(rows).to_csv(os.path.join(out, "projection.csv"), index=False)

    # chance of touching each outer zone within the horizon, from the same analog days
    vals = score.values
    hits_add = hits_der = n = 0
    for i in range(len(vals) - HORIZON):
        if np.isnan(vals[i]) or abs(vals[i] - now) > 10:
            continue
        if use_mom and (np.isnan(mom.iloc[i]) or np.sign(mom.iloc[i]) != np.sign(now_mom)):
            continue
        window = vals[i + 1:i + HORIZON + 1]
        if np.isnan(window).any():
            continue
        n += 1
        hits_add += window.min() < 20
        hits_der += window.max() > 80
    last = rows[-1]
    summary = {"asof": TODAY.isoformat(), "score_now": round(now, 1), "momentum_10d": round(now_mom, 1),
               "h": HORIZON, "p10": last["p10"], "p50": last["p50"], "p90": last["p90"],
               "zone_p50": zone(last["p50"]), "p_headwind": round(hits_add / n * 100, 1) if n else None,
               "p_tailwind": round(hits_der / n * 100, 1) if n else None, "analog_days": n,
               "history_days": int(score.notna().sum()), "method": method}
    pd.DataFrame([summary]).to_csv(os.path.join(out, "projection_summary.csv"), index=False)
    return summary


def build_forward(out, s, btc):
    """What markets themselves price for the coming weeks."""
    rows = []
    days_left = max(1, (date_cls(TODAY.year + (TODAY.month == 12), TODAY.month % 12 + 1, 1) - TODAY).days - 1)
    month_end = (TODAY + timedelta(days=days_left)).isoformat()
    px = float(btc.iloc[-1])
    try:
        now = int(time.time() * 1000)
        dv = get("https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC"
                 f"&start_timestamp={now - 3 * 86400000}&end_timestamp={now}&resolution=86400")["result"]["data"]
        dvol = float(dv[-1][4])
        sig = dvol / 100 * math.sqrt(HORIZON / 365)
        rows.append({"item": "BTC implied volatility (DVOL)", "value": round(dvol, 1), "unit": "% a year", "horizon": "30 days",
                     "read": "Options price a typical three-week move of about %.0f%%." % (sig * 100), "source": "Deribit"})
        rows.append({"item": "BTC one-sigma range in 3 weeks, low end", "value": round(px * (1 - sig)), "unit": "$",
                     "horizon": (TODAY + timedelta(days=HORIZON)).isoformat(), "read": "Two in three outcomes land between the low and high end.", "source": "Deribit DVOL"})
        rows.append({"item": "BTC one-sigma range in 3 weeks, high end", "value": round(px * (1 + sig)), "unit": "$",
                     "horizon": (TODAY + timedelta(days=HORIZON)).isoformat(), "read": "", "source": "Deribit DVOL"})
    except Exception as e:
        print("dvol skip", e)
    try:
        ev = get("https://gamma-api.polymarket.com/events?slug=what-price-will-bitcoin-hit-in-"
                 + TODAY.strftime("%B").lower() + "-" + str(TODAY.year))[0]
        ms = [m for m in ev["markets"] if not m.get("closed")]
        def lvl(m): return float(m["groupItemTitle"].split()[-1].replace(",", ""))
        ups = sorted([m for m in ms if "reach" in m["question"].lower() and lvl(m) > px], key=lvl)[:2]
        downs = sorted([m for m in ms if "dip" in m["question"].lower() and lvl(m) < px], key=lvl, reverse=True)[:2]
        for m in ups + downs:
            pr = round(float(json.loads(m["outcomePrices"])[0]) * 100, 1)
            up = "reach" in m["question"].lower()
            rows.append({"item": ("BTC trades at or above $%s this month" if up else "BTC trades at or below $%s this month") % format(int(lvl(m)), ","),
                         "value": pr, "unit": "%", "horizon": month_end, "read": "", "source": "Polymarket"})
    except Exception as e:
        print("btc touch skip", e)
    vix, vix3m = float(s["vix"].iloc[-1]), float(fred("VXVCLS", "2026-01-01").iloc[-1])
    rows.append({"item": "VIX against 3-month VIX", "value": round(vix - vix3m, 2), "unit": "points", "horizon": "1 to 3 months",
                 "read": "Inverted: the market expects stress to ease from here." if vix > vix3m else "Normal slope: calm is expected to hold.",
                 "source": "FRED VIXCLS, VXVCLS"})
    for label, m, read in [
        ("Fed hikes 25bp on 9 Dec", {"venue": "Kalshi", "id": "KXFEDDECISION-26DEC-H25"}, "Mostly priced. A surprise either way moves everything."),
        ("Fed holds in January", {"venue": "Polymarket", "id": "fed-decision-in-january-20260729233815502", "question": "no change"}, "Market sees December as the last hike for now."),
        ("US recession by end of 2026", {"venue": "Polymarket", "id": "us-recession-by-end-of-2026"}, ""),
        ("Hormuz traffic normal by 30 Nov", {"venue": "Polymarket", "id": "strait-of-hormuz-traffic-returns-to-normal-by-november-30-20260810151158765"}, "Oil stays a risk-off lever while this stays low."),
        ("Russia and Ukraine ceasefire deal by 31 Dec", {"venue": "Polymarket", "id": "russia-x-ukraine-ceasefire-agreement-by", "question": "December 31, 2026"}, ""),
    ]:
        v = market_price(m)
        if v is not None:
            rows.append({"item": label, "value": v, "unit": "%", "horizon": "", "read": read, "source": m["venue"]})
    pd.DataFrame(rows).to_csv(os.path.join(out, "forward.csv"), index=False)


# ---------- markets strip ----------

def strip_row(name, sym, series, src, unit=""):
    series = series.dropna()
    d = daily(series)
    last = float(series.iloc[-1]); asof = series.index[-1]
    def chg(n):
        ref = d.get(d.index[-1] - timedelta(days=n), np.nan)
        return round((last / ref - 1) * 100, 2) if ref and not math.isnan(ref) else None
    ma50 = series.iloc[-50:].mean() if len(series) >= 50 else np.nan
    ma200 = series.iloc[-200:].mean() if len(series) >= 200 else np.nan
    v50 = round((last / ma50 - 1) * 100, 2) if not math.isnan(ma50) else None
    v200 = round((last / ma200 - 1) * 100, 2) if not math.isnan(ma200) else None
    if v50 is not None and v200 is not None:
        trend = "Uptrend" if v50 > 0 and v200 > 0 else "Downtrend" if v50 < 0 and v200 < 0 else "Mixed"
    elif v50 is not None:
        trend = "Above 50d" if v50 > 0 else "Below 50d"
    else:
        trend = ""
    return {"asset": name, "symbol": sym, "last": round(last, 4), "unit": unit, "asof": asof.strftime("%Y-%m-%d"),
            "chg_1d": chg(1), "chg_14d": chg(14), "vs_50d": v50, "vs_200d": v200, "trend": trend, "source": src}


def build_markets(out, s, btc, eth):
    rows = [
        strip_row("Bitcoin", "BTC", btc, "Hyperliquid"),
        strip_row("Ether", "ETH", eth, "Hyperliquid"),
        strip_row("ETH/BTC", "ETHBTC", eth / btc, "Hyperliquid"),
        strip_row("S&P 500", "SPX", s["spx"], "FRED"),
        strip_row("Nasdaq Composite", "NASDAQ", s["ndx"], "FRED"),
        strip_row("VIX", "VIX", s["vix"], "FRED"),
        strip_row("US 10-year yield", "US10Y", s["dgs10"], "FRED", "%"),
        strip_row("Real 10-year yield", "TIPS10Y", s["dfii10"], "FRED", "%"),
        strip_row("Broad dollar", "USD", s["dxy"], "FRED"),
        strip_row("High-yield spread", "HY", s["hy"], "FRED", "%"),
        strip_row("Brent oil", "BRENT", s["brent"], "FRED"),
    ]
    for coin, name, sym in [("xyz:GOLD", "Gold", "GOLD"), ("xyz:SILVER", "Silver", "SILVER"),
                            ("xyz:HOOD", "Robinhood (HOOD)", "HOOD"), ("HYPE", "Hyperliquid", "HYPE")]:
        try:
            rows.append(strip_row(name, sym, hl_daily(coin, 400), "Hyperliquid perp"))
        except Exception as e:
            print("skip", coin, e)
    pd.DataFrame(rows).to_csv(os.path.join(out, "markets.csv"), index=False)
    return btc


# ---------- portfolio book ----------

def build_book(out, holdings, btc, prev_hist=None):
    """Portfolio from Dexscreener. Longer-window changes come from the desk's own
    price log (book_history.csv), appended on every run, since no free source
    serves reliable daily history for these tokens."""
    addrs = ",".join(h["address"] for h in holdings)       # as written: Solana addresses are case-sensitive
    pairs = get("https://api.dexscreener.com/latest/dex/tokens/" + addrs)["pairs"] or []
    hist = pd.read_csv(prev_hist) if prev_hist and os.path.exists(prev_hist) else pd.DataFrame(
        columns=["at", "symbol", "price", "btc", "note"])
    hist["at"] = pd.to_datetime(hist["at"], utc=True)
    btc_now = float(btc.iloc[-1])
    rows, new_hist = [], []
    for h in holdings:
        ps = [p for p in pairs if p["baseToken"]["address"].lower() == h["address"].lower()
              and p["chainId"] == h["chain"]]
        if not ps:
            continue
        top = max(ps, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        price = float(top["priceUsd"])
        liq = sum((p.get("liquidity") or {}).get("usd") or 0 for p in ps)
        vol = sum((p.get("volume") or {}).get("h24") or 0 for p in ps)
        buys = sum(p["txns"]["h24"]["buys"] for p in ps); sells = sum(p["txns"]["h24"]["sells"] for p in ps)
        mine = hist[hist["symbol"] == h["symbol"]].sort_values("at")

        def back(days):
            ref = mine[mine["at"] <= pd.Timestamp(NOW) - timedelta(days=days, hours=-6)]
            if ref.empty:
                return None
            return round((price / float(ref.iloc[-1]["price"]) - 1) * 100, 1)

        rows.append({"symbol": h["symbol"], "name": h["name"],
                     "chain": "Robinhood Chain" if h["chain"] == "robinhood" else h["chain"].title(),
                     "units": h["units"], "price": price, "value": round(price * h["units"], 2),
                     "chg_24h": (top.get("priceChange") or {}).get("h24"), "chg_7d": back(7), "chg_30d": back(30),
                     "liquidity": round(liq), "volume_24h": round(vol), "buys_24h": buys, "sells_24h": sells,
                     "buy_share": round(buys / max(1, buys + sells) * 100, 1),
                     "mcap": top.get("marketCap") or top.get("fdv"), "address": h["address"]})
        new_hist.append({"at": pd.Timestamp(NOW), "symbol": h["symbol"], "price": price, "btc": btc_now, "note": "Dexscreener"})
    pd.DataFrame(rows).to_csv(os.path.join(out, "book.csv"), index=False)
    hist = pd.concat([hist, pd.DataFrame(new_hist)], ignore_index=True).sort_values(["at", "symbol"])
    hist = hist[hist["at"] >= pd.Timestamp(NOW) - timedelta(days=400)]
    hist["at"] = hist["at"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    hist.to_csv(os.path.join(out, "book_history.csv"), index=False)



# ---------- catalysts ----------

# UBIK forecasts whose topic or claim touches these words count as market-moving
MACRO_WORDS = ["interest rate", "central bank", "inflation", "oil", "diesel", "iran", "hormuz", "gulf", "ukraine",
               "putin", "russia", "tariff", "trade", "china", "chip", "nvidia", "ai spending", "memory", "binance",
               "recession", "debt", "budget", "currency", "gold", "japan", "euro", "lagarde", "stock market", "bond",
               "houthi", "saudi", "fed", "rates", "anthropic", "economy", "jobs"]

def market_price(m):
    try:
        if m["venue"] == "Kalshi":
            k = get("https://api.elections.kalshi.com/trade-api/v2/markets/" + m["id"])["market"]
            b, a = float(k["yes_bid_dollars"]), float(k["yes_ask_dollars"])
            return round((a + b) / 2 * 100, 1) if a - b <= 0.1 else round(float(k["last_price_dollars"]) * 100, 1)
        ev = get("https://gamma-api.polymarket.com/events?slug=" + urllib.parse.quote(m["id"]))
        mk = ev[0]["markets"]
        if m.get("question"):
            mk = [x for x in mk if m["question"].lower() in x["question"].lower()]
        b, a = mk[0].get("bestBid"), mk[0].get("bestAsk")
        if b is not None and a is not None and float(a) - float(b) <= 0.1:
            return round((float(a) + float(b)) / 2 * 100, 1)
        return round(float(json.loads(mk[0]["outcomePrices"])[0]) * 100, 1)
    except Exception as e:
        print("market skip", m, e)
        return None


def split_prob(sp):
    """Sum a Polymarket event's outcome prices into a bullish and a bearish bucket.
    sp: {id: event slug, bull: [outcome titles], bear: [outcome titles]}; "YES" means a binary market's yes side."""
    ev = get("https://gamma-api.polymarket.com/events?slug=" + urllib.parse.quote(sp["id"]))[0]
    ms = [m for m in ev["markets"] if not m.get("closed")]
    if sp.get("question"):
        ms = [m for m in ms if sp["question"].lower() in m["question"].lower()]
    def p(m):
        b, a = m.get("bestBid"), m.get("bestAsk")
        if b is not None and a is not None and float(a) - float(b) <= 0.1:
            return (float(a) + float(b)) / 2
        return float(json.loads(m["outcomePrices"])[0])
    def bucket(names):
        if names == ["YES"]:
            return p(ms[0]) * 100
        if names == ["NO"]:
            return (1 - p(ms[0])) * 100
        return sum(p(m) for m in ms if (m.get("groupItemTitle") or "").strip() in names) * 100
    return round(bucket(sp.get("bull", [])), 1), round(bucket(sp.get("bear", [])), 1)


def rate(e):
    """Bias = which way the outcomes that are NOT already priced lean.
    From markets where possible (bullish minus bearish surprise odds, 15 points either way),
    otherwise the desk's own call, which the event says it is."""
    p_bull = p_bear = None
    if e.get("split"):
        try:
            p_bull, p_bear = split_prob(e["split"])
        except Exception as ex:
            print("split skip", e["event"], ex)
    if e.get("bias"):
        bias, basis = e["bias"], e.get("basis", "Desk view")
    elif p_bull is not None:
        d = p_bull - p_bear
        bias = "Bullish" if d > 15 else "Bearish" if d < -15 else "Neutral"
        basis = e.get("basis", "Market odds")
    else:
        bias, basis = "Neutral", e.get("basis", "Desk view")
    return bias, basis, p_bull, p_bear


def priced_level(e, p_bull, p_bear):
    """How much of the outcome is already in prices: the market's odds on the base case
    (neither the bullish nor the bearish surprise). 75%+ is High, 50-75% Medium, below 50% Low.
    Events without a market carry the desk's own call."""
    if e.get("priced"):
        return e["priced"], None, e.get("priced_basis", "Desk view")
    if p_bull is not None:
        base = max(0.0, 100 - p_bull - p_bear)
        return ("High" if base >= 75 else "Medium" if base >= 50 else "Low"), round(base, 1), "Market odds on the base case"
    return "", None, ""


def build_calendar(out, events):
    rows = []
    for e in events:
        prob = market_price(e["market"]) if e.get("market") else None
        bias, basis, p_bull, p_bear = rate(e)
        priced, priced_pct, priced_basis = priced_level(e, p_bull, p_bear)
        rows.append({"date": e["date"], "time_cet": e.get("time_cet", ""), "event": e["event"],
                     "kind": e.get("kind", "Scheduled"), "market": e["market"]["label"] if e.get("market") else "",
                     "market_prob": prob, "ubik_prob": None, "lean": e.get("lean", ""),
                     "impact": e.get("impact", "Medium"), "bias": bias, "basis": basis,
                     "priced": priced, "priced_pct": priced_pct, "priced_basis": priced_basis,
                     "bull": e.get("bull", ""), "bear": e.get("bear", ""), "p_bull": p_bull, "p_bear": p_bear,
                     "transmission": e.get("transmission", ""), "source": e.get("source", "")})
    # UBIK public forecast list (open claims + settled record; probabilities need a holder key)
    fc, page = [], 1
    while True:
        d = get(f"https://api.ubik.gold/forecasts?page={page}")
        fc += d["data"]
        if not d["meta"]["links"].get("next") or page > 10:
            break
        page += 1
    urows = []
    for f in fc:
        topic = (f.get("current") or {}).get("title", "")
        macro = any(k in (topic + " " + f["claim"]).lower() for k in MACRO_WORDS)
        urows.append({"id": f["question_id"], "claim": f["claim"], "topic": topic, "macro": macro,
                      "deadline": f["deadline"], "status": f["status"], "outcome": f.get("outcome") or "",
                      "probability": f.get("probability"), "brier": f.get("brier")})
        if f["status"] == "open" and f["deadline"] <= (TODAY + timedelta(days=35)).isoformat() and f["deadline"] >= TODAY.isoformat():
            p = f.get("probability")
            rows.append({"date": f["deadline"], "time_cet": "", "event": f["claim"],
                         "kind": "UBIK forecast" if macro else "UBIK forecast, other",
                         "market": "", "market_prob": None, "ubik_prob": round(p * 100, 1) if p is not None else None,
                         "lean": "", "impact": "", "bias": "", "basis": "", "priced": "", "priced_pct": None, "priced_basis": "",
                         "bull": "", "bear": "", "p_bull": None, "p_bear": None,
                         "transmission": topic, "source": "api.ubik.gold " + f["question_id"]})
    pd.DataFrame(urows).to_csv(os.path.join(out, "ubik_forecasts.csv"), index=False)
    cal = pd.DataFrame(rows)
    cal = cal[cal["date"] >= TODAY.isoformat()].sort_values(["date", "time_cet"])
    cal.to_csv(os.path.join(out, "calendar.csv"), index=False)



# ---------- momentum: volume and activity per chain and per asset class ----------

CHAINS = [("ethereum", "Ethereum", "Ethereum"), ("base", "Base", "Base"), ("robinhood", "Robinhood Chain", "Robinhood Chain"),
          ("solana", "Solana", "Solana"), ("arbitrum", "Arbitrum", "Arbitrum"), ("bsc", "BNB Chain", "BSC"),
          ("hyperliquid", "Hyperliquid", "Hyperliquid L1")]
CLASSES = [("crypto", "Crypto majors", ["BTC", "ETH", "SOL", "HYPE"]), ("stocks", "US stocks", ["xyz:SP500", "xyz:XYZ100"]),
           ("ai", "AI and chips", ["xyz:NVDA", "xyz:AMD", "xyz:MU"]), ("gold", "Gold", ["xyz:GOLD"]),
           ("silver", "Silver", ["xyz:SILVER"]), ("oil", "Oil", ["xyz:CL", "xyz:BRENTOIL"])]


def _pct_last(s, window, min_obs=28):
    s = s.dropna().iloc[-window:]
    if len(s) < min_obs:
        return None
    v = s.iloc[-1]
    return float((np.sum(s.values < v) + 0.5 * np.sum(s.values == v)) / len(s) * 100)


def _flow_score(f):
    """Daily flow (volume, fees, trades): how high the last 7 days sit against the last
    six months of 7-day totals, and how the week-on-week change ranks against the past year."""
    f = f[f.index.date < TODAY].sort_index()
    w7 = f.rolling(7).sum()
    chg = w7 / w7.shift(7) - 1
    lvl, ch = _pct_last(w7, 180), _pct_last(chg, 365)
    parts = [x for x in (lvl, ch) if x is not None]
    return (float(w7.iloc[-1]) if len(w7) else None, float(chg.iloc[-1]) if len(chg) and pd.notna(chg.iloc[-1]) else None,
            round(sum(parts) / len(parts), 1) if parts else None)


def _stock_score(s):
    """Level series (TVL, stablecoins): rank of the 7-day change against the past year."""
    s = s.sort_index()
    chg = s / s.shift(7) - 1
    return float(s.iloc[-1]), (float(chg.iloc[-1]) if pd.notna(chg.iloc[-1]) else None), _pct_last(chg, 365)


def _llama_chart(url):
    d = get(url)
    return pd.Series({pd.to_datetime(int(t), unit="s"): float(v) for t, v in d.get("totalDataChart", [])}).sort_index()


def build_momentum(out):
    rows, tvls = [], {}
    for key, name, llama in CHAINS:
        q = urllib.parse.quote(llama)
        try:
            vol = _llama_chart(f"https://api.llama.fi/overview/dexs/{q}?excludeTotalDataChartBreakdown=true&excludeTotalDataChart=false&dataType=dailyVolume")
            fees = _llama_chart(f"https://api.llama.fi/overview/fees/{q}?excludeTotalDataChartBreakdown=true&excludeTotalDataChart=false&dataType=dailyFees")
            tvl = pd.Series({pd.to_datetime(x["date"], unit="s"): float(x["tvl"]) for x in get(f"https://api.llama.fi/v2/historicalChainTvl/{q}")})
            st = get(f"https://stablecoins.llama.fi/stablecoincharts/{q}")
            stab = pd.Series({pd.to_datetime(int(x["date"]), unit="s"): float((x.get("totalCirculatingUSD") or {}).get("peggedUSD") or 0) for x in st})
        except Exception as e:  # one chain failing should not sink the run
            print("momentum", name, e)
            continue
        tvls[key] = tvl
        v7, vchg, vscore = _flow_score(vol)
        f7, fchg, fscore = _flow_score(fees)
        tv, tchg, tscore = _stock_score(tvl)
        sv, schg, sscore = _stock_score(stab[stab > 0])
        act = [x for x in (fscore, tscore, sscore) if x is not None]
        rows.append({"group": "chain", "key": key, "name": name, "volume_7d": round(v7) if v7 else None,
                     "volume_chg": round(vchg * 100, 1) if vchg is not None else None, "volume_score": vscore,
                     "activity_score": round(sum(act) / len(act), 1) if act else None,
                     "tvl": round(tv), "tvl_chg": round(tchg * 100, 1) if tchg is not None else None,
                     "stables": round(sv), "stables_chg": round(schg * 100, 1) if schg is not None else None,
                     "fees_7d": round(f7) if f7 else None, "fees_chg": round(fchg * 100, 1) if fchg is not None else None,
                     "trades_7d": None, "trades_chg": None, "history_days": int(len(vol)), "price_chg_7d": None, "price_via": None})
        time.sleep(0.3)
    # TVL by day for every chain, one column each, for the last year; "tvl" stays Robinhood Chain for the book footnote
    if tvls:
        w = pd.DataFrame(tvls).sort_index()
        w.index = w.index.normalize()
        w = w[~w.index.duplicated(keep="last")].iloc[-366:].round(0)
        w.insert(0, "tvl", w["robinhood"] if "robinhood" in w else np.nan)
        w.insert(0, "date", w.index.strftime("%Y-%m-%d"))
        w.to_csv(os.path.join(out, "chain_tvl.csv"), index=False)
    now = int(time.time() * 1000)
    for key, name, coins in CLASSES:
        vol, trades, px = None, None, None
        for c in coins:
            k = hl({"type": "candleSnapshot", "req": {"coin": c, "interval": "1d", "startTime": now - 400 * 86400000, "endTime": now}})
            v = pd.Series({pd.to_datetime(x["t"], unit="ms"): float(x["v"]) * float(x["c"]) for x in k})
            n = pd.Series({pd.to_datetime(x["t"], unit="ms"): float(x["n"]) for x in k})
            if px is None:  # first market of the group stands in for its price
                px = pd.Series({pd.to_datetime(x["t"], unit="ms"): float(x["c"]) for x in k}).sort_index()
            vol = v if vol is None else vol.add(v, fill_value=0)
            trades = n if trades is None else trades.add(n, fill_value=0)
            time.sleep(0.2)
        v7, vchg, vscore = _flow_score(vol)
        n7, nchg, nscore = _flow_score(trades)
        rows.append({"group": "class", "key": key, "name": name, "volume_7d": round(v7) if v7 else None,
                     "volume_chg": round(vchg * 100, 1) if vchg is not None else None, "volume_score": vscore,
                     "activity_score": nscore, "tvl": None, "tvl_chg": None, "stables": None, "stables_chg": None,
                     "fees_7d": None, "fees_chg": None, "trades_7d": round(n7) if n7 else None,
                     "trades_chg": round(nchg * 100, 1) if nchg is not None else None, "history_days": int(len(vol)),
                     "price_chg_7d": round((px.iloc[-1] / px.iloc[-8] - 1) * 100, 1) if len(px) > 8 else None, "price_via": coins[0]})
    df = pd.DataFrame(rows)
    df["asof"] = (TODAY - timedelta(days=1)).isoformat()
    df.to_csv(os.path.join(out, "momentum.csv"), index=False)
    return df



# ---------- altcoin market phase ----------
# Five phases in cycle order: bear -> accumulation -> uptrend -> euphoria -> cooling -> bear.
# Classified each day from data available that day (no look-ahead). The label follows what the alt
# line has done over the last month, then where it sits in the cycle:
#   falling = alts down 8%+ on 30 days, or 8%+ off their 30-day high;  rising = not falling and up 5%+ on 30 days
#   hype      Euphoria: alts far above their 200-day average with greed high (and not 20% off the 30-day high)
#   uptrend   Uptrend: rising; or flat after an uptrend while still above the 200-day average
#   cooling   Cooling: falling, but not yet 10% below the 200-day average; or flat right after euphoria
#   bear      Bear: falling while more than 10% below the 200-day average; a crash (down 25% in 60 days, 30% off
#             the high); or flat that far below trend while still down 20%+ on 60 days
#   accum     Accumulation: the boring stretch after a fall: flat, with the decline behind it
# "Alts" = total crypto market cap minus BTC, ETH and stablecoins (CoinMarketCap global data,
# Coin Metrics for early ETH, DefiLlama stablecoin supply). The monthly direction needs most of five
# days to change, and runs under 7 days fold into the phase before them.

PHASES = ["accum", "uptrend", "hype", "cooling", "bear"]


def _cmc_global():
    rows = {}
    start, end = 1483228800, int(time.time())
    while start < end:
        stop = min(end, start + 1800 * 86400)
        d = get(f"https://api.coinmarketcap.com/data-api/v3/global-metrics/quotes/historical?format=chart&interval=1d&timeStart={start}&timeEnd={stop}")
        for q in d["data"]["quotes"]:
            u = q["quote"][0]
            rows[q["timestamp"][:10]] = (q["btcDominance"], q["ethDominance"], u["totalMarketCap"])
        start = stop
        time.sleep(0.5)
    df = pd.DataFrame.from_dict(rows, orient="index", columns=["btcd", "ethd", "total"]).astype(float)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    # CoinMarketCap reports no ETH dominance before Oct 2020: fill it from Coin Metrics' ETH market cap
    d = get("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=eth&metrics=CapMrktCurUSD"
            "&frequency=1d&start_time=2017-01-01&page_size=10000&paging_from=start")
    eth = pd.Series({pd.to_datetime(x["time"][:10]): float(x["CapMrktCurUSD"]) for x in d["data"]})
    fill = (eth.reindex(df.index) / df.total * 100)
    df["ethd"] = df.ethd.where(df.ethd > 0, fill)
    return df


def _direction(r):
    """3 euphoric, 2 rising, 1 flat, 0 falling; None while the history is too short."""
    if pd.isna(r.alt_tr) or pd.isna(r.r30) or pd.isna(r.r60) or pd.isna(r.dd30):
        return None
    f = r.fng7 if pd.notna(r.fng7) else 50
    if (r.alt_tr > 1.0 or (r.alt_tr > 0.35 and f >= 72)) and r.dd30 > -0.2:
        return 3
    if r.r30 < -0.08 or r.dd30 < -0.08:
        return 0
    return 2 if r.r30 > 0.05 else 1


def _phase(d, r, prev):
    deep = r.alt_tr < -0.1
    if d == 3:
        return "hype"
    if d == 2:
        return "uptrend"
    if r.r60 < -0.25 and r.dd < -0.3:
        return "bear"
    if d == 0:
        return "bear" if deep else "cooling"
    # flat: read it by what came before
    if deep:
        return "bear" if r.r60 < -0.20 else "accum"
    if prev == "uptrend":
        return "uptrend" if r.alt_tr > 0 else "accum"
    if prev == "hype":
        return "cooling"
    if prev == "cooling":
        return "accum" if abs(r.r60) < 0.10 else "cooling"
    return "accum"


def build_phases(out):
    df = market_caps()
    fg = get("https://api.alternative.me/fng/?limit=0")["data"]
    fng = pd.Series({pd.to_datetime(int(x["timestamp"]), unit="s"): float(x["value"]) for x in fg}).sort_index()
    df["fng"] = fng.reindex(df.index)
    sma = lambda x, n: x.rolling(n, min_periods=int(n * .8)).mean()
    df["alt_tr"] = df.alts / sma(df.alts, 200) - 1
    df["btc_tr"] = df.btc / sma(df.btc, 200) - 1
    df["alt_sl"] = sma(df.alts, 200).pct_change(30)
    df["btc_sl"] = sma(df.btc, 200).pct_change(30)
    df["r30"], df["r60"], df["b30"] = df.alts.pct_change(30), df.alts.pct_change(60), df.btc.pct_change(30)
    df["rel30"] = df.r30 - df.b30
    df["dom30"] = df.btcd.diff(30)
    df["eb30"] = df.ethbtc.pct_change(30)
    df["fng7"] = df.fng.rolling(7, min_periods=3).mean()
    df["dd"] = df.alts / df.alts.rolling(730, min_periods=1).max() - 1
    df["dd30"] = df.alts / df.alts.rolling(30, min_periods=10).max() - 1
    df = df.loc["2017-08-01":].copy()
    d_raw = df.apply(_direction, axis=1)
    d_sm = d_raw.rolling(5, min_periods=1).apply(lambda w: pd.Series(w).dropna().mode().iloc[-1] if pd.Series(w).notna().any() else np.nan, raw=False)
    sig, vals, prev = [], [], None
    for (_, r), d0, d1 in zip(df.iterrows(), d_raw, d_sm):
        sig.append(_phase(int(d0), r, prev) if pd.notna(d0) else None)   # today's reading on its own
        prev = _phase(int(d1), r, prev) if pd.notna(d1) else None
        vals.append(prev)
    df["raw"] = sig
    runs = []
    for k, v in enumerate(vals):
        if k == 0 or v != vals[k - 1]:
            runs.append([v, k, k])
        else:
            runs[-1][2] = k
    for j in range(1, len(runs)):
        if runs[j][2] - runs[j][1] + 1 < 7 and runs[j][2] < len(vals) - 1:  # the current run is never folded
            for k in range(runs[j][1], runs[j][2] + 1):
                vals[k] = vals[runs[j][1] - 1]
    df["phase"] = vals
    df = df[df.phase.notna()]

    # forward alt returns (history only) and episodes
    df["fwd30"] = df.alts.shift(-30) / df.alts - 1
    df["fwd90"] = df.alts.shift(-90) / df.alts - 1
    ep = (df.phase != df.phase.shift()).cumsum()
    eps = df.groupby(ep).agg(phase=("phase", "first"), start=("phase", lambda s: s.index[0]), days=("phase", "size"))
    eps["next"] = eps.phase.shift(-1)
    stats = []
    for ph in PHASES:
        e = eps[eps.phase == ph]
        done = e.iloc[:-1] if eps.index[-1] in e.index else e
        nx = e.next.dropna().value_counts(normalize=True)
        sub = df[df.phase == ph]
        stats.append({"phase": ph, "episodes": len(e), "days_total": int(len(sub)),
                      "median_days": int(done.days.median()) if len(done) else None,
                      "fwd30_median": round(float(sub.fwd30.median()) * 100, 1) if sub.fwd30.notna().any() else None,
                      "fwd30_up": round(float((sub.fwd30.dropna() > 0).mean()) * 100) if sub.fwd30.notna().any() else None,
                      "fwd90_median": round(float(sub.fwd90.median()) * 100, 1) if sub.fwd90.notna().any() else None,
                      "fwd90_up": round(float((sub.fwd90.dropna() > 0).mean()) * 100) if sub.fwd90.notna().any() else None,
                      "next_1": nx.index[0] if len(nx) else None, "next_1_share": round(float(nx.iloc[0]) * 100) if len(nx) else None,
                      "next_2": nx.index[1] if len(nx) > 1 else None, "next_2_share": round(float(nx.iloc[1]) * 100) if len(nx) > 1 else None})
    st_df = pd.DataFrame(stats)
    for c in ["median_days", "fwd30_up", "fwd90_up", "next_1_share", "next_2_share"]:
        st_df[c] = st_df[c].astype("Int64")
    st_df.to_csv(os.path.join(out, "alt_phase_stats.csv"), index=False)

    o = pd.DataFrame({"date": df.index.strftime("%Y-%m-%d"), "phase": df.phase, "signal": df.raw,
                      "alts": (df.alts / 1e9).round(1), "btc": (df.btc / 1e9).round(1), "btc_dom": df.btcd.round(2),
                      "eth_btc": (df.ethbtc * 100).round(2), "alt_vs_trend": (df.alt_tr * 100).round(1), "btc_vs_trend": (df.btc_tr * 100).round(1),
                      "alt_vs_btc_30d": (df.rel30 * 100).round(1), "dom_chg_30d": df.dom30.round(2), "fear_greed_7d": df.fng7.round(0),
                      "alt_drawdown": (df.dd * 100).round(1), "episode_day": df.groupby(ep).cumcount() + 1})
    o.to_csv(os.path.join(out, "alt_phase.csv"), index=False)
    return o




# ---------- themes: price, breadth and usage per narrative ----------

THEMES = [  # key, name, CoinGecko category, DefiLlama fee categories
    ("stocks", "Tokenized stocks", "tokenized-stock", []),
    ("rwa", "Real-world assets", "real-world-assets-rwa", ["RWA"]),
    ("ai", "AI agents", "ai-agents", ["AI Agents"]),
    ("perps", "Perp DEXs", "decentralized-perpetuals", ["Derivatives"]),
    ("pred", "Prediction markets", "prediction-markets", ["Prediction Market"]),
    ("privacy", "Privacy coins", "privacy-coins", []),
    ("memes", "Meme coins", "meme-token", ["Launchpad"]),
    ("l2", "Layer 2s", "layer-2", []),
    ("depin", "DePIN", "depin", []),
    ("restaking", "Restaking", "restaking", ["Liquid Restaking", "Restaking"]),
    ("rhmeme", "Robinhood Chain memes", "robinhood-chain-meme", []),
]


def build_themes(out):
    cats = {x["id"]: x for x in get("https://api.coingecko.com/api/v3/coins/categories")}
    fees = collections.defaultdict(lambda: [0.0, 0.0])
    try:
        f = get("https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true&dataType=dailyFees")
        for pr in f["protocols"]:
            fees[pr.get("category")][0] += pr.get("total7d") or 0
            fees[pr.get("category")][1] += pr.get("total14dto7d") or 0
    except Exception as e:
        print("themes fees", e)
    def prices(r, cg):
        """Fill one theme's price change, breadth and largest tokens. Returns False if CoinGecko refused."""
        try:
            m = get(f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&category={cg}&order=market_cap_desc&per_page=50&page=1&price_change_percentage=7d,30d", tries=2)
        except Exception as e:
            print("themes", r["name"], e)
            return False
        for col, fld in (("chg_7d", "price_change_percentage_7d_in_currency"), ("chg_30d", "price_change_percentage_30d_in_currency")):
            ok = [(x["market_cap"], x[fld]) for x in m if x.get("market_cap") and x.get(fld) is not None and x[fld] > -99]
            if ok:  # the theme's change: today's cap against what the same tokens were worth then
                r[col] = round((sum(a for a, _ in ok) / sum(a / (1 + b / 100) for a, b in ok) - 1) * 100, 1)
        ups = [x["price_change_percentage_7d_in_currency"] for x in m if x.get("price_change_percentage_7d_in_currency") is not None]
        if ups:
            r["breadth_7d"] = round(sum(1 for u in ups if u > 0) / len(ups) * 100)
            r["tokens"] = len(ups)
        r["largest"] = ", ".join(x["symbol"].upper() for x in m[:3])
        return True

    rows, missed = [], []
    for key, name, cg, llama in THEMES:
        c0 = cats.get(cg) or {}
        r = {"key": key, "name": name, "mcap": c0.get("market_cap"), "vol_24h": c0.get("volume_24h"),
             "chg_24h": round(c0["market_cap_change_24h"], 1) if c0.get("market_cap_change_24h") is not None else None,
             "chg_7d": None, "chg_30d": None, "breadth_7d": None, "tokens": None, "largest": "", "fees_7d": None, "fees_chg": None}
        if not prices(r, cg):
            missed.append((r, cg))
        if llama:
            a = sum(fees[k][0] for k in llama); b = sum(fees[k][1] for k in llama)
            if a > 0 and b > 0:
                r["fees_7d"], r["fees_chg"] = round(a), round((a / b - 1) * 100, 1)
        rows.append(r)
        time.sleep(6)   # CoinGecko's free tier allows a handful of calls a minute
    if missed:          # one more try for the ones CoinGecko refused, after letting its limit reset
        time.sleep(70)
        for r, cg in missed:
            prices(r, cg)
            time.sleep(10)
    df = pd.DataFrame(rows)
    for c in ("breadth_7d", "tokens"):
        df[c] = pd.to_numeric(df[c]).astype("Int64")
    df["asof"] = TODAY.isoformat()
    df.to_csv(os.path.join(out, "themes.csv"), index=False)
    return df



# ---------- market stories: unscheduled news that moves markets, and what it has moved ----------

def hl_hourly(coin, days=45):
    now = int(time.time() * 1000)
    k = hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": now - days * 86400000, "endTime": now}})
    return pd.Series({pd.to_datetime(x["t"], unit="ms"): float(x["c"]) for x in (k or [])}, dtype=float).sort_index()


def _odds(spec):
    """Polymarket yes-price for one outcome of an event, with its one-day change and the event's 24h volume."""
    if not spec or not spec.get("slug"):
        return None
    ev = get("https://gamma-api.polymarket.com/events?slug=" + urllib.parse.quote(spec["slug"]))
    if not ev:
        return None
    ms = [m for m in ev[0].get("markets", []) if not m.get("closed")]
    pick = (spec.get("pick") or "").lower()
    m = next((x for x in ms if pick and pick == (x.get("groupItemTitle") or "").lower()), None) or (ms[0] if ms and not pick else None)
    if not m:
        return None
    pr = json.loads(m.get("outcomePrices") or "[]")
    d1 = m.get("oneDayPriceChange")
    return {"p": round(float(pr[0]) * 100, 1) if pr else None, "d1": round(float(d1) * 100, 1) if d1 is not None else None,
            "vol": round(float(ev[0].get("volume24hr") or 0))}


def build_stories(out, stories):
    """For each story on the desk's list: what its own asset, the Nasdaq, BTC and alts have done since it broke,
    what the prediction market says, and how closely alts have followed that asset over the last 90 days."""
    def since(s, t0):
        a = s[s.index <= t0]
        return round((float(s.iloc[-1]) / float(a.iloc[-1]) - 1) * 100, 1) if len(a) and len(s) else None
    caps = market_caps()
    alts = caps.alts.dropna()                      # the row dated D is the value at 00:00 UTC on D
    alt_ret = alts.pct_change().shift(-1)          # so its change belongs to the day before
    hourly, daily_px = {}, {}
    rows = []
    for st in stories:
        t0 = pd.Timestamp(st["since"])
        t0 = t0.tz_convert(None) if t0.tzinfo else t0
        r = {"id": st["id"], "title": st["title"], "status": st.get("status", ""), "why": st.get("why", ""),
             "since": t0.strftime("%Y-%m-%dT%H:%MZ"), "since_label": st.get("since_label", ""),
             "asset_label": st.get("asset_label", ""), "asset_chg": None, "bench_label": st.get("bench_label", ""), "bench_chg": None,
             "btc_chg": None, "alts_chg": None, "odds_label": "", "odds": None, "odds_chg_1d": None, "odds_vol_24h": None,
             "odds2_label": "", "odds2": None, "next_date": st.get("next_date", ""), "next_label": st.get("next_label", ""),
             "link_label": "", "corr_alts": None, "beta_alts": None, "down_days": None, "down_alts_med": None,
             "sources": st.get("sources", "")}
        try:
            for key, coin in (("asset_chg", st.get("asset")), ("bench_chg", st.get("bench")), ("btc_chg", "BTC")):
                if coin:
                    if coin not in hourly:
                        hourly[coin] = hl_hourly(coin)
                    r[key] = since(hourly[coin], t0)
            i0 = int(np.abs((alts.index - t0).total_seconds()).argmin())      # the alt snapshot nearest to the break
            r["alts_chg"] = round((float(alts.iloc[-1]) / float(alts.iloc[i0]) - 1) * 100, 1)
            # how alts have followed this story's asset (or its benchmark) day by day
            coin = st.get("asset") or st.get("bench")
            if coin:
                if coin not in daily_px:
                    daily_px[coin] = hl_daily(coin, 400)
                z = pd.DataFrame({"x": daily_px[coin].pct_change(), "y": alt_ret}).dropna().tail(90)
                if len(z) >= 40:
                    r["link_label"] = st.get("asset_label") or st.get("bench_label")
                    r["corr_alts"] = round(float(z.x.corr(z.y)), 2)
                    r["beta_alts"] = round(float(np.polyfit(z.x, z.y, 1)[0]), 2)
                    big = z[z.x <= z.x.quantile(0.1)]          # the asset's worst tenth of days
                    r["down_days"] = len(big)
                    r["down_alts_med"] = round(float(big.y.median()) * 100, 1) if len(big) else None
        except Exception as e:
            print("story prices", st["id"], e)
        try:
            o = _odds(st.get("odds"))
            if o:
                r.update({"odds_label": st["odds"].get("label", ""), "odds": o["p"], "odds_chg_1d": o["d1"], "odds_vol_24h": o["vol"]})
            o2 = _odds(st.get("odds2"))
            if o2:
                r.update({"odds2_label": st["odds2"].get("label", ""), "odds2": o2["p"]})
        except Exception as e:
            print("story odds", st["id"], e)
        rows.append(r)
    df = pd.DataFrame(rows)
    if len(df):
        df["down_days"] = pd.to_numeric(df["down_days"]).astype("Int64")
        df["asof"] = NOW.isoformat(timespec="minutes")
    df.to_csv(os.path.join(out, "stories.csv"), index=False)
    return df


# ---------- morning brief ----------

def build_summary(out):
    """A short factual brief from this run's own outputs, for the Morning brief window."""
    rd = lambda f: pd.read_csv(os.path.join(out, f))
    hist, proj, ph, book, mk, cal, fw, comp = (rd("regime_history.csv"), rd("projection_summary.csv"), rd("alt_phase.csv"),
                                               rd("book.csv"), rd("markets.csv"), rd("calendar.csv"), rd("forward.csv"), rd("regime_components.csv"))
    rows = []
    s_now, s_prev = float(hist.score.iloc[-1]), float(hist.score.iloc[-2])
    rows.append({"label": "Conditions", "value": f"{s_now:.0f} {hist.zone.iloc[-1]}", "note": f"{s_now - s_prev:+.1f} on the day", "num": None})
    pr = proj.iloc[0]
    rows.append({"label": "In 3 weeks", "value": f"{pr.p50:.0f}", "note": f"8 in 10 paths {pr.p10:.0f} to {pr.p90:.0f}", "num": None})
    if "stress" in hist and pd.notna(hist.stress.iloc[-1]):
        rows.append({"label": "Macro stress", "value": f"{float(hist.stress.iloc[-1]):.0f}", "note": "0 calm, 100 stressed; context only", "num": None})
    try:
        sto = rd("stories.csv")
        for _, x in sto.head(3).iterrows():
            bits = []
            if pd.notna(x.get("asset_chg")):
                bits.append(f"{x['asset_label']} {x['asset_chg']:+.1f}% since it broke")
            if pd.notna(x.get("odds")):
                bits.append(f"{x['odds_label']} {x['odds']:.0f}%")
            if pd.notna(x.get("next_date")) and str(x.get("next_date")):
                bits.append(f"next {pd.Timestamp(x['next_date']).strftime('%a %-d %b')}")
            rows.append({"label": "Story", "value": x["title"], "note": "; ".join(bits), "num": None})
    except Exception as e:
        print("brief stories", e)
    names = {"accum": "Accumulation", "uptrend": "Uptrend", "hype": "Euphoria", "cooling": "Cooling", "bear": "Bear"}
    last = ph.iloc[-1]
    note = f"day {int(last.episode_day)}"
    if isinstance(last.signal, str) and last.signal != last.phase:
        note += f", leaning {names.get(last.signal, last.signal)}"
    rows.append({"label": "Altcoin phase", "value": names.get(last.phase, last.phase), "note": note, "num": None, "phase": last.phase})
    m = mk.set_index("symbol")
    for sym, lab in [("BTC", "BTC"), ("ETH", "ETH")]:
        if sym in m.index:
            r = m.loc[sym]
            rows.append({"label": lab, "value": f"${r['last']:,.0f}", "note": f"{r.chg_1d:+.1f}% 1d, {r.chg_14d:+.1f}% 2w", "num": None})
    tot = float(book.value.sum())
    prev = float((book.value / (1 + book.chg_24h / 100)).sum())
    w = book.sort_values("chg_24h")
    rows.append({"label": "Your book", "value": f"${tot:,.0f}", "note": f"{(tot / prev - 1) * 100:+.1f}% 24h; best {w.symbol.iloc[-1]} {w.chg_24h.iloc[-1]:+.1f}%, worst {w.symbol.iloc[0]} {w.chg_24h.iloc[0]:+.1f}%",
                 "num": round((tot / prev - 1) * 100, 2)})
    f = fw.set_index("item")
    for key, lab in [("Fed hikes 25bp on 9 Dec", "Fed, 9 Dec"), ("BTC trades at or below $70,000 this month", "BTC ≤ $70k in Nov")]:
        if key in f.index:
            rows.append({"label": lab, "value": f"{int(float(f.loc[key, 'value']) + 0.5)}%", "note": str(f.loc[key, "source"]), "num": None})
    fg = comp[comp.metric == "fng"]
    if len(fg):
        rows.append({"label": "Fear & Greed", "value": f"{float(fg.value.iloc[0]):.0f}", "note": "0 fear, 100 greed", "num": None})
    st = comp[comp.metric == "stables_30d"]
    if len(st):
        rows.append({"label": "Stablecoins", "value": f"{float(st.value.iloc[0]):+.1f}% in 30d", "note": f"rank {float(st.pctile.iloc[0]):.0f} of 100 over two years", "num": None})
    try:
        th = rd("themes.csv")
        th = th[th["asof"] == TODAY.isoformat()].dropna(subset=["chg_7d"]).sort_values("chg_7d")
        if len(th):
            rows.append({"label": "Themes", "value": f"{int((th.chg_7d > 0).sum())} of {len(th)} up on the week",
                         "note": f"best {th['name'].iloc[-1]} {th.chg_7d.iloc[-1]:+.1f}%, worst {th['name'].iloc[0]} {th.chg_7d.iloc[0]:+.1f}%", "num": None})
    except Exception as e:
        print("brief themes", e)
    # what is due in the next 48 hours (Amsterdam dates)
    today = datetime.now(timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("Europe/Amsterdam")).date()
    soon = cal[pd.to_datetime(cal.date).dt.date.between(today, today + timedelta(days=1))]
    due = []
    for _, r in soon.iterrows():
        ubik = str(r.kind).startswith("UBIK")
        if ubik and r.kind != "UBIK forecast":
            continue
        prob = r.ubik_prob if ubik else r.market_prob
        due.append({"date": r.date, "time": r.time_cet if isinstance(r.time_cet, str) else "", "event": r.event,
                    "source": "UBIK" if ubik else "", "prob": None if pd.isna(prob) else round(float(prob))})
    nxt = cal[(~cal.kind.str.startswith("UBIK")) & (pd.to_datetime(cal.date).dt.date >= today)].head(1)
    brief = {"date": today.isoformat(), "written_at": NOW.isoformat(timespec="seconds"), "rows": rows, "due": due[:6],
             "next": (f"{nxt.event.iloc[0]}: {pd.to_datetime(nxt.date.iloc[0]).strftime('%a %-d %b')}" + (f" {nxt.time_cet.iloc[0]}" if isinstance(nxt.time_cet.iloc[0], str) else "")) if len(nxt) else ""}
    # one flat table: a meta row, the brief rows, what is due, the next market event
    flat = [{"kind": "meta", "label": "", "value": brief["date"], "note": brief["written_at"], "num": None, "phase": "", "time": "", "source": ""}]
    flat += [{"kind": "row", "label": r["label"], "value": r["value"], "note": r["note"], "num": r["num"], "phase": r.get("phase", ""), "time": "", "source": ""} for r in rows]
    flat += [{"kind": "due", "label": x["date"], "value": x["event"], "note": "", "num": x["prob"], "phase": "", "time": x["time"], "source": x["source"]} for x in brief["due"]]
    if brief["next"]:
        flat.append({"kind": "next", "label": "", "value": brief["next"], "note": "", "num": None, "phase": "", "time": "", "source": ""})
    pd.DataFrame(flat).to_csv(os.path.join(out, "daily_summary.csv"), index=False)
    return brief


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    here = os.path.dirname(os.path.abspath(__file__))
    events = json.load(open(sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, "events.json")))
    holdings = json.load(open(sys.argv[3] if len(sys.argv) > 3 else os.path.join(here, "holdings.json")))
    prev_hist = sys.argv[4] if len(sys.argv) > 4 else None
    stories_path = sys.argv[5] if len(sys.argv) > 5 else os.path.join(here, "stories.json")
    s, btc, eth, hist = build_regime(out)
    build_projection(out, hist)
    build_markets(out, s, btc, eth)
    build_book(out, holdings, btc, prev_hist)
    build_calendar(out, events)
    build_forward(out, s, btc)
    build_momentum(out)
    build_phases(out)
    try:
        build_themes(out)
    except Exception as e:   # a blocked price feed should not sink the run
        print("themes failed", e)
    try:
        if os.path.exists(stories_path):
            build_stories(out, json.load(open(stories_path)))
    except Exception as e:   # stories are an extra; they should not sink the run
        print("stories failed", e)
    build_summary(out)
    json.dump({"ran_at": NOW.isoformat(timespec="seconds")}, open(os.path.join(out, "run.json"), "w"))
    print("done", NOW.isoformat())


if __name__ == "__main__":
    main()
