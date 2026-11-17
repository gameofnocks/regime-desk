"""Replay of the paper book's asset rules on UBIK's settled forecasts. It never touches the live book.

For every settled forecast it rebuilds what the book would have seen on the day the forecast was issued:
  the forecast's claim and UBIK's first figure, each linked asset with its price and normal daily range at that
  moment, and a Claude review asked the same question as the live one (how far the asset moves on YES and on NO, what
  the market already expects, how much this event drives it), told to judge as of the issue date.
The live sizing rules then give each candidate a direction, a stop and a target, and the replay walks the real daily
prices: stop, target, UBIK changing its mind, or the forecast settling. Every candidate is followed, also the ones the
rules reject, so the result shows what looser or tighter thresholds would have done. Single stocks are replayed too
(the live book only watches them) and reported apart.

Limits, also written into the result:
  the sample is small (UBIK's record starts in October 2026);
  the review model may know things from after the issue date, which would flatter the result;
  prices are daily bars, so a stop and a target inside one day are counted as the stop;
  the contract side cannot be replayed: no settled forecast had a market asking the same question.

Writes paper_backtest.json (totals only, public) and paper_backtest.bin (every simulated trade, sealed for the site).
Env: ANTHROPIC_API_KEY (needed), UBIK_KEY (optional; settled forecasts are public).
Usage: python3 tools/paper_backtest.py [--selftest]   (--selftest invents the review's numbers to check the mechanics
       and writes nothing into the repository)
"""
import gzip, json, os, statistics, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import paper_engine as pe
import paper_review as pr

SELFTEST = "--selftest" in sys.argv
OUT_PUBLIC = os.path.join("/tmp" if SELFTEST else ROOT, "paper_backtest.json")
OUT_SEALED = os.path.join("/tmp" if SELFTEST else ROOT, "paper_backtest.bin")
STOCK_COST = 0.0005
AS_OF = ("This is a replay. Each item carries as_of, the date the forecast was issued. Judge everything as it stood on that "
         "date: prices, expectations and news up to then only. Use nothing you know from after it; if you recognise how "
         "the event ended, ignore that and answer as an informed observer would have on the as_of date.\n\n")


def ts(s):
    return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()


def bars(sym, a, b):
    """Daily bars (start, open, high, low, close) from 45 days before a to a week after b."""
    u = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
         f"?period1={int(a) - 45 * 86400}&period2={int(b) + 7 * 86400}&interval=1d")
    r = pe.get(u)["chart"]["result"][0]; q = r["indicators"]["quote"][0]
    return [(t, o, h, l, c) for t, o, h, l, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"]) if None not in (o, h, l, c)]


def candidates(forecasts, note):
    rows = []
    for f in forecasts:
        hist = [h for h in (f.get("history") or []) if h.get("probability") is not None]
        if not hist or not f.get("resolved_at") or f.get("outcome") not in ("yes", "no"):
            continue
        t0, t1 = ts(hist[0]["at"]), ts(f["resolved_at"])
        for a in f.get("assets") or []:
            kind = a.get("kind")
            sym = pe.yahoo_symbol(a) if kind in pe.ASSET_KINDS else ((a.get("symbol") or "").upper().replace(" ", "") if kind == "stock" else None)
            if not sym:
                note["no_price_feed"] += 1; continue
            try:
                b = bars(sym, t0, t1); time.sleep(0.2)
            except Exception:
                note["price_failed"] += 1; continue
            before = [x for x in b if x[0] <= t0]
            after = [x for x in b if x[0] > t0]
            if len(before) < 15 or not after or after[0][0] >= t1:      # settled before the next session opened
                note["no_room"] += 1; continue
            trs = [max(before[i][2] - before[i][3], abs(before[i][2] - before[i - 1][4]), abs(before[i][3] - before[i - 1][4])) for i in range(1, len(before))][-14:]
            entry = after[0][1]
            rows.append({"type": "asset", "forecast": f["question_id"], "claim": f["claim"], "symbol": a.get("symbol"), "name": a.get("name"),
                         "class": kind, "why_linked": a.get("why"), "ubik": hist[0]["probability"], "venue_prob": None, "deadline": f.get("deadline"),
                         "yahoo": sym, "price": entry, "atr_pct": (sum(trs) / len(trs)) / entry, "pass": None,
                         "as_of": hist[0]["at"][:10], "outcome": f["outcome"], "_t0": t0, "_t1": t1, "_bars": after,
                         "_hist": [(ts(h["at"]), h["probability"]) for h in hist[1:]]})
    return rows


def ask(key, items):
    payload = {"contracts": [], "assets": [{"key": i, "as_of": a["as_of"], "forecast": a["claim"], "deadline": a.get("deadline"), "venue_prob": None,
                                            "asset": f"{a.get('name')} ({a.get('symbol')}, {a.get('class')})",
                                            "how_the_outcome_touches_it": a.get("why_linked"), "price": round(a["price"], 6),
                                            "daily_atr_pct": round(a["atr_pct"] * 100, 2)} for i, a in enumerate(items)]}
    body = {"model": pr.MODEL, "max_tokens": 8000, "system": pr.SYSTEM,
            "messages": [{"role": "user", "content": AS_OF + pr.ASK + json.dumps(payload, ensure_ascii=False)}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        out = json.loads(r.read())
    text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    return json.loads(text[text.find("{"): text.rfind("}") + 1]).get("assets", [])


def review(rows, note):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if SELFTEST:           # invented numbers, only to exercise the code path
        import random
        rnd = random.Random(7)
        for r in rows:
            r.update({"move_if_yes_pct": rnd.uniform(-6, 6), "move_if_no_pct": rnd.uniform(-6, 6), "market_prob": rnd.uniform(0.2, 0.8), "confidence": rnd.uniform(0.2, 0.9)})
        return True
    if not key:
        note["errors"].append("no ANTHROPIC_API_KEY"); return False
    for i in range(0, len(rows), 15):
        items = rows[i:i + 15]
        try:
            ans = ask(key, items); note["batches_ok"] += 1
        except Exception as e:
            note["batches_failed"] += 1; note["errors"].append(f"review batch: {type(e).__name__} {getattr(e, 'code', '')}".strip()); continue
        for v in ans:
            try:
                x = items[int(v["key"])]
                x.update({"move_if_yes_pct": float(v["move_if_yes_pct"]), "move_if_no_pct": float(v["move_if_no_pct"]),
                          "market_prob": max(0.01, min(0.99, float(v["market_prob"]))), "confidence": max(0.0, min(1.0, float(v.get("confidence", 0.5)))),
                          "review_reason": v.get("reason")})
            except (KeyError, ValueError, IndexError, TypeError):
                continue
    return note["batches_ok"] > 0


def simulate(r):
    """Walk the daily bars with the live exit rules. Returns the trade's result as fractions of the entry price."""
    d, px = r["dir"], r["price"]
    cost = pe.COST.get(r["class"], STOCK_COST)
    stop = px * (1 - d * r["stop_pct"] / 100)
    target = px * (1 + d * r["best_pct"] / 100) if r["best_pct"] > 0 else None
    sign = d * (1 if r["move_if_yes_pct"] >= r["move_if_no_pct"] else -1)        # +1: the position wants YES
    turned = next((t for t, p in r["_hist"] if (p - r["market_prob"]) * sign <= 0), None)
    exit_px, why = None, None
    held = [b for b in r["_bars"]]
    for i, (t, o, h, l, c) in enumerate(held):
        if i and t >= r["_t1"]:
            exit_px, why = o, "forecast settled"; break
        if i and turned is not None and t > turned:
            exit_px, why = o, "UBIK turned against it"; break
        if (l <= stop) if d > 0 else (h >= stop):
            exit_px, why = stop, "stop"; break
        if target is not None and ((h >= target) if d > 0 else (l <= target)):
            exit_px, why = target, "target"; break
    if exit_px is None:
        exit_px, why = held[-1][4], "forecast settled"
    settle = next((o for t, o, h, l, c in held[1:] if t >= r["_t1"]), held[-1][4])
    rules = d * (exit_px / px - 1) - 2 * cost
    hold = d * (settle / px - 1) - 2 * cost
    share = min(pe.RISK_PER_TRADE / (r["stop_pct"] / 100), pe.MAX_ASSET) if r["stop_pct"] else 0     # of the account, as the live sizing would
    return {"exit": why, "ret_rules_pct": round(rules * 100, 2), "ret_hold_pct": round(hold * 100, 2),
            "r_multiple": round(rules * 100 / r["stop_pct"], 2) if r["stop_pct"] else None, "account_pct": round(rules * share * 100, 3),
            "leaned_right": (r["outcome"] == "yes") == (r["ubik"] >= r["market_prob"])}


def stats(rows):
    if not rows:
        return {"n": 0}
    ret = [r["ret_rules_pct"] for r in rows]; hold = [r["ret_hold_pct"] for r in rows]
    return {"n": len(rows), "won": sum(x > 0 for x in ret), "mean_ret_pct": round(statistics.mean(ret), 2), "median_ret_pct": round(statistics.median(ret), 2),
            "mean_hold_pct": round(statistics.mean(hold), 2), "direction_right": sum(x > 0 for x in hold),
            "account_pct": round(sum(r["account_pct"] for r in rows), 2), "forecasts": len({r["forecast"] for r in rows}),
            "exits": {k: sum(r["exit"] == k for r in rows) for k in ("stop", "target", "UBIK turned against it", "forecast settled")}}


def main():
    note = {"no_price_feed": 0, "price_failed": 0, "no_room": 0, "errors": [], "batches_ok": 0, "batches_failed": 0}
    key = os.environ.get("UBIK_KEY", "").strip()
    d = pe.get("https://api.ubik.gold/forecasts?status=settled&per_page=100&include=history,details,related",
               {"Authorization": "Bearer " + key} if key else {})
    settled = d.get("data") or []
    same = sum(1 for f in settled for m in (f.get("markets") or []) if m.get("relation") in ("same", "broader", "narrower"))
    rows = candidates(settled, note)
    pe.log(f"settled {len(settled)}, asset links priced {len(rows)}, skipped {note['no_price_feed'] + note['price_failed'] + note['no_room']}")
    reviewed = review(rows, note)
    rows = [r for r in rows if "move_if_yes_pct" in r]
    pe.size_assets({}, rows, {})
    done = []
    for r in rows:
        if "dir" not in r:
            continue
        r.update(simulate(r))
        r["passes_rules"] = bool(r.get("pass"))
        done.append(r)
    groups = {"tradable kinds (commodity, currency, index, crypto)": [r for r in done if r["class"] in pe.ASSET_KINDS],
              "single stocks (watched only in the live book)": [r for r in done if r["class"] == "stock"]}
    result = {"run": pe.iso(), "selftest": SELFTEST, "review_model": pr.MODEL, "reviewed": bool(reviewed), "settled_forecasts": len(settled),
              "first_settled": min((f.get("resolved_at") or "9")[:10] for f in settled) if settled else None,
              "contract_markets_that_could_be_replayed": same, "candidates_simulated": len(done), "skipped": {k: note[k] for k in ("no_price_feed", "price_failed", "no_room")},
              "review_batches": {"ok": note["batches_ok"], "failed": note["batches_failed"]}, "errors": note["errors"],
              "rules": {"min_expected_pct": 0.5, "min_reward_to_risk": pe.MIN_RR, "stop_in_daily_ranges": pe.ATR_STOP, "risk_per_trade_pct": pe.RISK_PER_TRADE * 100},
              "groups": {}, "limits": [
                  "Small sample: UBIK's settled record is a few weeks old, and several candidates share one forecast, so they are not independent.",
                  "The review model may know how events ended; it was told to judge as of the issue date, which cannot be verified.",
                  "Daily bars: a stop and a target inside the same day count as the stop.",
                  "account_pct adds each trade's result at the live position size and ignores the limits on how many positions are open at once."]}
    for name, g in groups.items():
        exp = lambda r: abs(r["expected_pct"])
        result["groups"][name] = {
            "all candidates": stats(g),
            "pass today's rules": stats([r for r in g if r["passes_rules"]]),
            "by expected move": {"0.5% or more": stats([r for r in g if exp(r) >= 0.5]), "0.25% to 0.5%": stats([r for r in g if 0.25 <= exp(r) < 0.5]),
                                 "under 0.25%": stats([r for r in g if exp(r) < 0.25])},
            "by reward to risk": {"2 or more": stats([r for r in g if r["rr"] >= 2]), "1 to 2": stats([r for r in g if 1 <= r["rr"] < 2]),
                                  "under 1": stats([r for r in g if r["rr"] < 1])},
            "looser rule: expected 0.25%+ and reward to risk 1+": stats([r for r in g if exp(r) >= 0.25 and r["rr"] >= 1]),
            "UBIK leaned the right way against the review's market figure": {"yes": stats([r for r in g if r["leaned_right"]]), "no": stats([r for r in g if not r["leaned_right"]])}}
    json.dump(result, open(OUT_PUBLIC, "w"), indent=1)
    keep = ("forecast", "claim", "symbol", "name", "class", "as_of", "outcome", "ubik", "market_prob", "move_if_yes_pct", "move_if_no_pct", "confidence",
            "expected_pct", "best_pct", "stop_pct", "rr", "dir", "passes_rules", "why", "exit", "ret_rules_pct", "ret_hold_pct", "r_multiple", "account_pct",
            "leaned_right", "review_reason")
    trades = [{k: r.get(k) for k in keep} for r in done]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    pub = serialization.load_pem_public_key(open(os.path.join(HERE, "site_pub.pem"), "rb").read())
    k, nonce = os.urandom(32), os.urandom(12)
    locked = pub.encrypt(k, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    body = AESGCM(k).encrypt(nonce, gzip.compress(json.dumps({"built": pe.iso(), "files": {"paper_backtest": {"json": result}, "paper_backtest_trades": {"json": trades}}},
                                                             separators=(",", ":")).encode(), 9), None)
    open(OUT_SEALED, "wb").write(len(locked).to_bytes(2, "big") + locked + nonce + body)
    pe.log(f"done: {len(done)} simulated, errors {len(note['errors'])}")
    print(json.dumps(result["groups"], indent=1))
    if SELFTEST:
        print(json.dumps(trades[:3], indent=1))


if __name__ == "__main__":
    main()
