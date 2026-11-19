"""The desk's daily data run, inside the repository, so the site no longer depends on anything outside it.

Once a day it:
  1. reads the owner's wallets (tools/wallet_scan.py) so the book's amounts follow them;
  2. refreshes the market stories list with a Claude call that can search the web (data/stories.json);
  3. runs the collector (tools/collector.py), which builds every data file from public sources;
  4. has Claude write the morning brief from those files;
  5. packs the data, encrypted with the site's public key, into data.bin (tools/build_site.py);
  6. keeps what it needs for tomorrow in desk_state.bin, which only this repository's jobs can open.

Private inputs (wallet addresses, hand-entered amounts) come from desk_private.bin; see tools/desk_private.py.
daily_status.json says how the run went: steps, counts and error names only, never amounts or addresses.

Env: PAPER_KEY (needed), ANTHROPIC_API_KEY (for the brief and the stories; without it both are carried over),
     FORCE=1 to run even if today's run already succeeded, DESK_MODEL to choose the model.
"""
import csv, io, json, os, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import desk_private as dp

WORK = os.environ.get("DESK_WORK", "/tmp/desk")
OUT = os.path.join(WORK, "out")
PRIVATE, STATE, STATUS = os.path.join(ROOT, "desk_private.bin"), os.path.join(ROOT, "desk_state.bin"), os.path.join(ROOT, "daily_status.json")
STORIES, EVENTS = os.path.join(ROOT, "data", "stories.json"), os.path.join(ROOT, "data", "events.json")
MODELS = [os.environ.get("DESK_MODEL") or "claude-opus-5-5", "claude-sonnet-5-5"]
NEEDED = ["regime_history", "regime_components", "markets", "book", "calendar", "projection", "projection_summary", "forward", "alt_phase", "daily_summary"]
CHAIN_NAME = {"robinhood": "Robinhood Chain", "base": "Base", "solana": "Solana", "ethereum": "Ethereum"}


def iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def read(name):
    p = os.path.join(OUT, name)
    return open(p).read() if os.path.exists(p) else ""


def rows(name):
    return list(csv.DictReader(io.StringIO(read(name))))


# ---- Claude -------------------------------------------------------------------------------------------------------
def claude(system, user, tools=None, max_tokens=6000):
    """One answer as text. Tries the preferred model, then the fallback. Raises if neither answers."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("no ANTHROPIC_API_KEY")
    last = None
    for model in dict.fromkeys(MODELS):
        msgs = [{"role": "user", "content": user}]
        try:
            for _ in range(4):
                body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": msgs}
                if tools:
                    body["tools"] = tools
                req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
                                             headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
                with urllib.request.urlopen(req, timeout=600) as r:
                    out = json.loads(r.read())
                if out.get("stop_reason") == "pause_turn":       # a long search: hand the turn back and let it continue
                    msgs.append({"role": "assistant", "content": out["content"]}); continue
                return "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text"), model
            raise RuntimeError("did not finish")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error", {})
                last = f"{model}: {e.code} {err.get('type', '')}: {(err.get('message') or '')[:140]}"
            except Exception:
                last = f"{model}: {e.code}"
        except Exception as e:
            last = f"{model}: {type(e).__name__}"
    raise RuntimeError(last or "no answer")


def json_in(text, opener):
    closer = "]" if opener == "[" else "}"
    return json.loads(text[text.index(opener): text.rindex(closer) + 1])


# ---- stories ------------------------------------------------------------------------------------------------------
STORY_RULES = """You keep a short list of market stories for a private trading desk: unscheduled news that is moving markets right now, such as a leadership crisis at a large company, a record IPO, a hack or a policy shock.

Search the web for today's top market and crypto news, then update the list.
- For each story already on the list: update "status" (two sentences at most: what is reported, by whom, and what is still open), "next_date", "next_label" and "sources". Leave "since", "asset", "bench" and the odds fields alone unless they are wrong.
- Remove a story when it is resolved or has had no market-relevant news for seven days.
- Add a story only if all three hold: at least two major outlets report it; it has either a market on Hyperliquid (coin name as its API uses it, for example xyz:NVDA) or a Polymarket event from the list below with more than $100,000 traded in 24 hours; and it can plausibly move the Nasdaq, BTC or the alt market. Keep at most five stories, the biggest first.
- Fields of a story: id (short slug), title (under 60 characters), since (UTC ISO time of the last clean price before the story broke), since_label (that moment in words), status, why (one sentence on why it matters for AI and crypto risk), asset and asset_label (the Hyperliquid coin most directly hit, or empty strings), bench and bench_label (default "xyz:XYZ100" and "Nasdaq 100"), odds and odds2 (each {"slug": the Polymarket event slug, "pick": that market's groupItemTitle exactly or "" for a single-market event, "label": a short label}, or leave out), next_date (YYYY-MM-DD or ""), next_label, sources (outlet names, comma separated).
- Text rules: say what outlets report and attribute it; nothing about anyone's private life beyond the minimum needed to explain the market move; no speculation, no advice, no price targets. Plain style, no em dashes.

Answer with the full updated list as a JSON array and nothing else."""


def refresh_stories(old, status):
    try:
        ev = json.loads(urllib.request.urlopen(urllib.request.Request(
            "https://gamma-api.polymarket.com/events?closed=false&order=volume24hr&ascending=false&limit=30", headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read())
        busy = [{"title": e.get("title"), "slug": e.get("slug"), "volume24hr": round(float(e.get("volume24hr") or 0))} for e in ev]
    except Exception:
        busy = []
    user = (f"Today is {datetime.now(timezone.utc).strftime('%A %d %B %Y')} (UTC).\n\nThe list now:\n{json.dumps(old, ensure_ascii=False)}\n\n"
            f"Polymarket's busiest events by 24-hour volume:\n{json.dumps(busy, ensure_ascii=False)}")
    text, model = claude(STORY_RULES, user, tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}], max_tokens=8000)
    new = json_in(text, "[")
    good = []
    for s in new[:5]:
        if not isinstance(s, dict) or not all(isinstance(s.get(k), str) and s.get(k) for k in ("id", "title", "since", "status", "why")):
            continue
        datetime.fromisoformat(s["since"].replace("Z", "+00:00"))
        s.setdefault("bench", "xyz:XYZ100"); s.setdefault("bench_label", "Nasdaq 100")
        for k in ("asset", "asset_label", "next_date", "next_label", "sources", "since_label"):
            s[k] = s.get(k) or ""
        good.append(s)
    if not good:
        raise RuntimeError("empty list")
    status["stories"] = {"model": model, "before": len(old), "after": len(good), "added": len({s["id"] for s in good} - {s["id"] for s in old}),
                         "removed": len({s["id"] for s in old} - {s["id"] for s in good})}
    return good


def check_story_markets(stories, old):
    """A story added by the model may name a coin or a market that does not exist; that would sink the stories file.
    Anything that cannot be priced is cleared, for new and changed fields only."""
    import collector
    known = {s["id"]: s for s in old}
    for s in stories:
        was = known.get(s["id"], {})
        if s.get("asset") and s["asset"] != was.get("asset"):
            try:
                if len(collector.hl_hourly(s["asset"], days=3)) < 5:
                    raise ValueError
            except Exception:
                s["asset"], s["asset_label"] = "", ""
        for k in ("odds", "odds2"):
            if s.get(k) and s[k] != was.get(k):
                try:
                    if collector._odds(s[k]) is None:
                        raise ValueError
                except Exception:
                    s.pop(k, None)
    return stories


# ---- the brief ----------------------------------------------------------------------------------------------------
BRIEF_RULES = """You write the morning brief of a private market desk, its "Today's read" panel, from the desk's own data files.

How the desk reads the market: the Conditions score (0 to 100) is trend plus fuel and high means supportive; its zones are Headwind (below 20), Soft, Neutral, Firm and Tailwind (above 80). Only the two outer zones have a record, so say plainly when a middle reading gives no edge. Macro stress is context only, high means stress, and it does not move the score. Rows with part "watched" in regime_components are not scored.

Write a JSON array with one object and nothing else:
- date: today, YYYY-MM-DD
- written_at: the UTC ISO time given below
- headline: one or two sentences: the conditions score and zone and the single most important thing
- body: one paragraph: the score, its trend and fuel halves and the four scored inputs; what followed past days within 10 points of today's score (given below as "similar days"); macro stress and which of its inputs stand out; a watched row if it stands out; the market stories in one or two sentences (what each has moved since it broke and what the odds say); the altcoin phase; one sentence on themes; how the owner's tokens moved, and any amount the wallet scan changed, added or removed
- forward: one paragraph: the three-week path and the share of similar days that touched Headwind or Tailwind (projection_summary), the market odds (forward), and the next catalysts from the calendar with their exact dates and Amsterdam times; check each weekday against its date
- next: the next calendar event that is not a UBIK forecast, as "Event: Ddd D Mon HH:MM"

Use only numbers from the data below. Style: plain, direct, verdict first; no em dashes; no "it's not X, it's Y"; no rhetorical questions; no lists of three for effect; no bold. Keep it informational: no advice to buy or sell, no price targets."""


def similar_days():
    h = rows("regime_history.csv")
    try:
        s = float(h[-1]["score"])
        d = [float(r["alt_fwd28"]) for r in h if r.get("score") not in ("", None) and r.get("alt_fwd28") not in ("", None) and abs(float(r["score"]) - s) <= 10]
        d.sort()
        return {"score_today": s, "days": len(d), "median_alt_change_28d_pct": round(d[len(d) // 2], 1) if d else None,
                "share_higher_pct": round(sum(x > 0 for x in d) / len(d) * 100) if d else None}
    except Exception:
        return {}


def write_brief(prev, scan, status):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cal = [r for r in rows("calendar.csv") if (r.get("date") or r.get("when") or "9999")[:10] >= today][:14]
    data = {"written_at": iso(), "today": datetime.now(timezone.utc).strftime("%A %Y-%m-%d"), "daily_summary": rows("daily_summary.csv"),
            "regime_components": rows("regime_components.csv"), "regime_history_last_days": rows("regime_history.csv")[-3:], "similar_days": similar_days(),
            "stories": rows("stories.csv"), "alt_phase_last_days": rows("alt_phase.csv")[-2:], "alt_phase_stats": rows("alt_phase_stats.csv"),
            "themes": rows("themes.csv"), "book": rows("book.csv"), "projection_summary": rows("projection_summary.csv"), "forward": rows("forward.csv"),
            "calendar_next": cal, "wallet_scan": {k: scan.get(k) for k in ("changed", "added", "removed", "manual")} if scan else None,
            "yesterdays_brief_for_format_only": prev}
    o, why = None, ""
    for attempt in (1, 2):                                   # the answer is sometimes a bare object, or cut short: read both shapes, ask twice
        text, model = claude(BRIEF_RULES, "The data:\n" + json.dumps(data, ensure_ascii=False), max_tokens=6000)
        try:
            a, b = text.find("{"), text.rfind("}")
            if a < 0 or b < a:
                raise ValueError("no object in the answer")
            cand = json.loads(text[a: b + 1])
            if not all(isinstance(cand.get(k), str) and cand.get(k) for k in ("headline", "body", "forward", "next")):
                raise ValueError("brief incomplete")
            o = cand; break
        except Exception as e:
            why = f"{type(e).__name__}: {str(e)[:80]}"; log("brief attempt", attempt, why)
    if o is None:
        raise RuntimeError("brief unreadable, " + why)
    o["date"], o["written_at"] = today, data["written_at"]
    status["brief"] = {"model": model, "chars": len(o["body"]) + len(o["forward"])}
    return [o]


# ---- the run ------------------------------------------------------------------------------------------------------
def holdings_csv(book):
    f = io.StringIO(); w = csv.writer(f, lineterminator="\n"); w.writerow(["symbol", "name", "cg_id", "chain", "units", "address", "source"])
    for h in book:
        w.writerow([h["symbol"], h["name"], h.get("cg_id", ""), CHAIN_NAME.get(h["chain"], h["chain"].title()), h["units"], h["address"], h.get("source", "manual")])
    return f.getvalue()


def run_collector(holdings_path, hist_path, stories_path):
    cmd = [sys.executable, os.path.join(HERE, "collector.py"), OUT, EVENTS, holdings_path, hist_path, stories_path]
    for attempt in (1, 2):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            tail = (p.stdout or "")[-1500:]
            if p.returncode == 0 and all(os.path.exists(os.path.join(OUT, n + ".csv")) for n in NEEDED):
                return True, tail
            log("collector failed", p.returncode, (p.stderr or "")[-600:])
        except subprocess.TimeoutExpired:
            log("collector timed out")
        if attempt == 1:
            time.sleep(120)
    return False, ""


def main():
    status = {"ran_at": iso(), "ok": False, "steps": {}, "errors": []}

    def finish():
        json.dump(status, open(STATUS, "w"), indent=1)
        log("status:", json.dumps(status)[:600])

    try:
        dp.write_pub()
    except Exception as e:
        status["errors"].append(f"key: {e}"); finish(); return
    if not os.path.exists(PRIVATE):
        status["errors"].append("waiting for desk_private.bin"); finish(); return
    try:
        priv = dp.open_file(PRIVATE)
        state = dp.open_file(STATE) if os.path.exists(STATE) else {}
    except Exception as e:
        status["errors"].append(f"private inputs: {type(e).__name__}"); finish(); return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("last_ok") == today and os.environ.get("FORCE") != "1":
        log("today's run already succeeded"); return
    os.makedirs(OUT, exist_ok=True)

    # the book: yesterday's, unless the owner's file has changed since, and then its amounts win
    book = state["holdings"] if state.get("holdings") and state.get("private_rev") == priv.get("rev") else priv["holdings"]
    hist = state.get("book_history_csv") or priv.get("book_history_csv") or "at,symbol,price,btc,note\n"
    prev_brief = state.get("outlook") or priv.get("outlook") or []

    # 1. wallets
    scan = None
    hp, wp = os.path.join(WORK, "holdings.json"), os.path.join(WORK, "wallets.json")
    json.dump(book, open(hp, "w")); json.dump(priv.get("wallets") or {}, open(wp, "w"))
    try:
        import wallet_scan
        wallet_scan.main(hp, wp, os.path.join(WORK, "scan"))
        scan = json.load(open(os.path.join(WORK, "scan", "wallet_scan.json")))
        book = json.load(open(os.path.join(WORK, "scan", "holdings.json")))
        json.dump(book, open(hp, "w"))
        status["steps"]["wallets"] = {"changed": len(scan["changed"]), "added": len(scan["added"]), "removed": len(scan["removed"]),
                                      "manual": len(scan["manual"]), "errors": len(scan["errors"])}
    except Exception as e:
        status["errors"].append(f"wallet scan: {type(e).__name__}")
    finally:
        if os.path.exists(wp):
            os.remove(wp)

    # 2. stories
    old = json.load(open(STORIES)); stories = old
    try:
        stories = check_story_markets(refresh_stories(old, status), old)
    except Exception as e:
        status["errors"].append(f"stories kept as they were: {str(e)[:160]}")
        stories = old
    sp = os.path.join(WORK, "stories.json"); json.dump(stories, open(sp, "w"))

    # 3. the data files
    histp = os.path.join(WORK, "book_history_prev.csv"); open(histp, "w").write(hist)
    ok, tail = run_collector(hp, histp, sp)
    if not ok:
        status["errors"].append("collector failed twice; the site keeps yesterday's data"); finish(); return
    status["steps"]["collector"] = {"files": len([f for f in os.listdir(OUT) if f.endswith(".csv")]), "themes": "themes failed" not in tail,
                                    "stories": os.path.exists(os.path.join(OUT, "stories.csv"))}
    if not os.path.exists(os.path.join(OUT, "stories.csv")) and stories is not old:     # the new list broke it: fall back to the old one
        try:
            import collector
            collector.build_stories(OUT, old); stories = old
            status["errors"].append("the refreshed stories could not be priced; kept the old list")
        except Exception as e:
            status["errors"].append(f"stories: {type(e).__name__}")

    # 4. the brief
    try:
        brief = write_brief(prev_brief, scan, status)
    except Exception as e:
        status["errors"].append(f"brief carried over from before: {str(e)[:160]}")
        brief = prev_brief
    json.dump(brief, open(os.path.join(OUT, "outlook.json"), "w"), ensure_ascii=False)
    open(os.path.join(OUT, "holdings.csv"), "w").write(holdings_csv(book))

    # 5. the site's data, and what tomorrow needs
    import build_site
    built, files, snap = build_site.build_data(OUT, os.path.join(HERE, "site_pub.pem"), ROOT)
    status["steps"]["site"] = {"built": built, "files": len(files), "snapshots": len(snap)}
    open(STATE, "wb").write(dp.seal({"private_rev": priv.get("rev"), "holdings": book, "book_history_csv": read("book_history.csv") or hist,
                                     "outlook": brief, "last_ok": today, "saved": iso()}))
    if stories is not old:
        json.dump(stories, open(STORIES, "w"), indent=1, ensure_ascii=False)
    status["ok"] = True
    finish()


if __name__ == "__main__":
    main()
