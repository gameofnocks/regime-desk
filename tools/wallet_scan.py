"""Keeps the book's token amounts in step with the owner's wallets, read straight from the chains.

Usage: python3 wallet_scan.py holdings.json wallets.json out_dir
  holdings.json  the book: a list of {symbol, name, chain, address, units, cg_id?, source?}
  wallets.json   {"evm": ["0x...", ...], "solana": ["...", ...]}  (never stored in the dashboard or any repository)
Writes into out_dir:
  holdings.json  the book with amounts from the wallets
  holdings.csv   the same as the dashboard's holdings file
  wallet_scan.json  what changed, what was added or removed, what stayed on a hand-entered amount, and any errors

Rules
  A token found in the wallets takes the wallets' total and is marked source "wallets".
  A token not found in any wallet keeps its hand-entered amount (source "manual"): it may sit in a wallet that is not listed.
  A token that was read from the wallets before and is now gone is dropped from the book.
  A new Solana token worth at least MIN_NEW_USD is added. New tokens on other chains are not discovered: no free
    source lists every token in a wallet there, so those are added by hand once and tracked from then on.
  If a chain cannot be read, its tokens are left exactly as they were.
"""
import csv, json, os, sys, time, urllib.error, urllib.request

RPC = {"base": "https://mainnet.base.org", "robinhood": "https://rpc.mainnet.chain.robinhood.com", "ethereum": "https://ethereum-rpc.publicnode.com",
       "solana": "https://api.mainnet-beta.solana.com"}
SOL_PROGRAMS = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]
CHAIN_NAME = {"robinhood": "Robinhood Chain", "base": "Base", "solana": "Solana", "ethereum": "Ethereum"}
MIN_NEW_USD = 250
UA = {"User-Agent": "Mozilla/5.0 (regime-desk)", "Content-Type": "application/json"}


def post(url, method, params, tries=4):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=UA), timeout=30) as r:
                d = json.loads(r.read())
            if "error" in d:
                raise RuntimeError(str(d["error"])[:120])
            return d["result"]
        except (urllib.error.URLError, TimeoutError, ConnectionError, RuntimeError):
            if i == tries - 1:
                raise
            time.sleep(4 * (i + 1))


def evm_balance(chain, token, wallets):
    dec = int(post(RPC[chain], "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]), 16)
    total = 0
    for w in wallets:
        raw = post(RPC[chain], "eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + w[2:].lower()}, "latest"])
        total += int(raw, 16) if raw and raw != "0x" else 0
        time.sleep(0.15)
    return total / 10 ** dec


def solana_balances(wallets):
    out = {}
    for w in wallets:
        for prog in SOL_PROGRAMS:
            res = post(RPC["solana"], "getTokenAccountsByOwner", [w, {"programId": prog}, {"encoding": "jsonParsed"}])
            for a in res.get("value", []):
                info = a["account"]["data"]["parsed"]["info"]
                amt = float(info["tokenAmount"]["uiAmountString"] or 0)
                if amt > 0:
                    out[info["mint"]] = out.get(info["mint"], 0) + amt
            time.sleep(1.2)
    return out


def dex(mints):
    """Price, ticker and name of Solana tokens from Dexscreener (deepest pool)."""
    best = {}
    for i in range(0, len(mints), 25):
        url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(mints[i:i + 25])
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            for p in json.loads(r.read()).get("pairs") or []:
                m = p["baseToken"]["address"]; liq = (p.get("liquidity") or {}).get("usd") or 0
                if p.get("chainId") == "solana" and p.get("priceUsd") and (m not in best or liq > best[m][0]):
                    best[m] = (liq, float(p["priceUsd"]), p["baseToken"]["symbol"], p["baseToken"]["name"])
    return best


def main(holdings_path, wallets_path, out):
    book = json.load(open(holdings_path)); wallets = json.load(open(wallets_path))
    evm, sol = wallets.get("evm") or [], wallets.get("solana") or []
    rep = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "wallets": len(evm) + len(sol), "changed": [], "added": [], "removed": [],
           "manual": [], "errors": []}
    sol_bal = None
    if sol:
        try:
            sol_bal = solana_balances(sol)
        except Exception as e:
            rep["errors"].append("solana: " + str(e)[:100])
    new_book = []
    for h in book:
        chain, was = h["chain"], float(h["units"])
        try:
            if chain == "solana":
                if sol_bal is None:
                    raise RuntimeError("solana not read")
                found = sol_bal.get(h["address"], 0)
            elif chain in RPC and evm:
                found = evm_balance(chain, h["address"], evm)
            else:
                found = None
        except Exception as e:
            rep["errors"].append(f"{h['symbol']}: {str(e)[:100]}"); new_book.append(h); continue
        if found:
            units = round(found, 6)
            if abs(units - was) > max(1e-6, abs(was) * 1e-9):
                rep["changed"].append({"symbol": h["symbol"], "from": was, "to": units})
            new_book.append(dict(h, units=int(units) if units == int(units) else units, source="wallets"))
        elif h.get("source") == "wallets" and found is not None:
            rep["removed"].append({"symbol": h["symbol"], "was": was})
        else:
            rep["manual"].append(h["symbol"]); new_book.append(dict(h, source="manual"))
    if sol_bal:
        known = {h["address"] for h in book if h["chain"] == "solana"}
        fresh = [m for m in sol_bal if m not in known]
        try:
            info = dex(fresh) if fresh else {}
        except Exception as e:
            info = {}; rep["errors"].append("prices for new tokens: " + str(e)[:100])
        for m in fresh:
            if m in info and sol_bal[m] * info[m][1] >= MIN_NEW_USD:
                _, price, sym, name = info[m]
                sym = sym.upper()
                if any(h["symbol"] == sym for h in new_book):
                    sym = sym + "-SOL"
                new_book.append({"address": m, "chain": "solana", "name": name, "symbol": sym, "units": round(sol_bal[m], 6), "source": "wallets"})
                rep["added"].append({"symbol": sym, "units": round(sol_bal[m], 6), "value_usd": round(sol_bal[m] * price)})
    os.makedirs(out, exist_ok=True)
    json.dump(new_book, open(os.path.join(out, "holdings.json"), "w"), indent=1)
    with open(os.path.join(out, "holdings.csv"), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n"); w.writerow(["symbol", "name", "cg_id", "chain", "units", "address", "source"])
        for h in new_book:
            w.writerow([h["symbol"], h["name"], h.get("cg_id", ""), CHAIN_NAME.get(h["chain"], h["chain"].title()), h["units"], h["address"], h.get("source", "manual")])
    rep["book_changed"] = bool(rep["changed"] or rep["added"] or rep["removed"])
    json.dump(rep, open(os.path.join(out, "wallet_scan.json"), "w"), indent=1)
    print(json.dumps(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
