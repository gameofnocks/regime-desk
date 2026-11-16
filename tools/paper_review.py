"""The daily Claude review for the paper book (tools/paper_engine.py).

One request to the Anthropic API with every candidate of the day:
  contracts  does the market's question really resolve with the forecast (or contain it, or sit inside it, as UBIK's
             relation says)? A mismatch is vetoed with a reason.
  assets     how far would the asset move, in percent over the forecast's life, if the outcome is YES and if it is NO;
             what the market already expects; how sure the estimate is. The engine turns that into a trade or a pass.

Returns True when a review came back, False when it could not run (no key, or the API failed): then contracts trade on
the rules alone and assets are skipped.
"""
import json, os, urllib.request

MODEL = os.environ.get("PAPER_MODEL", "claude-sonnet-5-5")

SYSTEM = (
    "You review a paper-trading book that trades on UBIK's forecasts. You are careful and sceptical, and you write one "
    "short plain sentence per reason. Answer with JSON only, no prose around it.")

ASK = """Today's candidates are below as JSON.

For each item in "contracts": decide whether the prediction market's question resolves consistently with the forecast
under the stated relation (same: same event; broader: the market's YES includes the forecast's YES; narrower: the
market's YES sits inside the forecast's YES; inverted: the market's YES is the forecast's NO). Read the forecast's
resolution rule. Veto when dates, thresholds, sources or wording differ enough that one can resolve without the other.

For each item in "assets": estimate, for the life of the forecast up to its deadline:
  move_if_yes_pct  the asset's percent move if the forecast resolves YES (signed, e.g. -3.5)
  move_if_no_pct   its percent move if it resolves NO (signed)
  market_prob      what the market currently prices as the chance of YES (0-1); use venue_prob when it is given
  confidence       0-1, how much this event, rather than everything else, will drive the asset in that window
Be realistic: one event rarely moves an index more than 1-2% or a currency more than 1-3%; commodities and crypto can
move more on direct supply or regulatory news. If the link is weak, give small moves and low confidence.

Return exactly:
{"contracts": [{"key": <key>, "ok": true|false, "reason": "..."}],
 "assets": [{"key": <key>, "move_if_yes_pct": n, "move_if_no_pct": n, "market_prob": n, "confidence": n, "reason": "..."}]}

CANDIDATES:
"""


def _ask(key, payload):
    body = {"model": MODEL, "max_tokens": 8000, "temperature": 0, "system": SYSTEM,
            "messages": [{"role": "user", "content": ASK + json.dumps(payload, ensure_ascii=False)}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        out = json.loads(r.read())
    text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def review(contracts, assets, status, batch=15):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    todo_c = [c for c in contracts if c.get("pass")]
    todo_a = [a for a in assets if a.get("pass") is None]
    if not key:
        status["errors"].append("review: no ANTHROPIC_API_KEY")
        return False
    status["review_items"] = len(todo_c) + len(todo_a)
    ok = fails = 0
    jobs = [("contracts", todo_c[i:i + batch]) for i in range(0, len(todo_c), batch)] + \
           [("assets", todo_a[i:i + batch]) for i in range(0, len(todo_a), batch)]
    for kind, items in jobs:
        if kind == "contracts":
            payload = {"contracts": [{"key": i, "forecast": c["claim"], "resolution_rule": c.get("rule"), "forecast_deadline": c.get("deadline"),
                                      "market": c.get("question"), "venue": c["venue"], "market_ends": c.get("ends"), "relation": c["relation"],
                                      "inverted": c["inverted"], "side": c["side"]} for i, c in enumerate(items)], "assets": []}
        else:
            payload = {"contracts": [], "assets": [{"key": i, "forecast": a["claim"], "deadline": a.get("deadline"), "ubik_probability": a["ubik"],
                                                    "venue_prob": a.get("venue_prob"), "asset": f"{a.get('name')} ({a.get('symbol')}, {a.get('class')})",
                                                    "why_ubik_links_it": a.get("why_linked"), "ubik_reasoning": a.get("reasoning"),
                                                    "price": a.get("price"), "daily_atr_pct": round((a.get("atr_pct") or 0) * 100, 2)} for i, a in enumerate(items)]}
        try:
            ans = _ask(key, payload)
        except Exception as e:
            fails += 1
            detail = type(e).__name__
            if hasattr(e, "code"):      # the API's status and error type, never the request or the key
                try:
                    err = json.loads(e.read()).get("error", {})
                    detail += f" {e.code} {err.get('type', '')}: {(err.get('message') or '')[:120]}"
                except Exception:
                    detail += f" {e.code}"
            status["errors"].append(f"review batch: {detail}")
            for x in items:     # an unreviewed contract does not trade; an unreviewed asset is skipped
                x.update({"pass": False, "why": "the Claude review did not answer for it"})
            continue
        ok += 1
        for v in ans.get(kind, []):
            try:
                x = items[int(v["key"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            x["review_reason"] = v.get("reason")
            if kind == "contracts":
                if not v.get("ok"):
                    x.update({"pass": False, "why": "Claude vetoed: " + (v.get("reason") or "")})
                continue
            try:
                x.update({"move_if_yes_pct": float(v["move_if_yes_pct"]), "move_if_no_pct": float(v["move_if_no_pct"]),
                          "confidence": max(0.0, min(1.0, float(v.get("confidence", 0.5))))})
                if x.get("venue_prob") is None and v.get("market_prob") is not None:
                    x["market_prob"] = max(0.01, min(0.99, float(v["market_prob"])))
            except (KeyError, TypeError, ValueError):
                x.update({"pass": False, "why": "review gave no usable estimate"})
    status["review_model"] = MODEL
    status["review_batches"] = {"ok": ok, "failed": fails}
    return ok > 0 or not jobs
