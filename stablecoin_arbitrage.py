import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    import httpx
except ImportError as _exc:
    sys.exit(f"missing dependency '{_exc.name}'. run: pip install -r requirements.txt")

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwCNB"
DAI_MINT = "EjmyN6qEC1TfvaJFGjw1d11DNBaHabxjrB83TLdMNWiz"
PYUSD_MINT = "C1i4TUzptSzgFi38jBnv4ZCfXfZo7jW9wB1WvFt4F5X"

KNOWN_MINTS = {
    "USDC": USDC_MINT,
    "USDT": USDT_MINT,
    "DAI": DAI_MINT,
    "PYUSD": PYUSD_MINT,
}

DEFAULT_PAIRS = [
    ("USDC", USDC_MINT, "USDT", USDT_MINT),
    ("DAI", DAI_MINT, "USDC", USDC_MINT),
]

WATCH_INTERVAL = 30
MIN_SPREAD_BPS = 5.0

@dataclass
class DexQuote:
    dex: str
    pair: str
    direction: str
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price: float

@dataclass
class SpreadOpportunity:
    pair: str
    buy_dex: str
    buy_price: float
    sell_dex: str
    sell_price: float
    spread_bps: float

def fetch_jupiter_quote(
    client: httpx.Client,
    input_mint: str,
    output_mint: str,
    amount: int = 1_000_000,
    dex_label: Optional[str] = None,
) -> Optional[DexQuote]:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": "50",
    }
    if dex_label:
        params["dex"] = dex_label
    try:
        r = client.get(JUPITER_QUOTE_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data or "outAmount" not in data:
            return None
        out_amount = int(data["outAmount"])
        # price = units of output per unit of input
        price = out_amount / amount
        return DexQuote(
            dex=dex_label or "Jupiter",
            pair="temp",
            direction="temp",
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=amount,
            out_amount=out_amount,
            price=price,
        )
    except Exception:
        return None

def fetch_pair_quotes(client: httpx.Client, name_a: str, mint_a: str, name_b: str, mint_b: str) -> List[DexQuote]:
    quotes: List[DexQuote] = []
    pair_name = f"{name_a}/{name_b}"
    for dex in ["Orca", "Raydium"]:
        q = fetch_jupiter_quote(client, mint_a, mint_b, dex_label=dex)
        if q:
            q.pair = pair_name
            q.direction = f"{name_a}->{name_b}"
            quotes.append(q)
        q = fetch_jupiter_quote(client, mint_b, mint_a, dex_label=dex)
        if q:
            q.pair = pair_name
            q.direction = f"{name_b}->{name_a}"
            quotes.append(q)
    return quotes

def compute_spreads(quotes: List[DexQuote]) -> List[SpreadOpportunity]:
    by_pair: Dict[str, List[DexQuote]] = {}
    for q in quotes:
        by_pair.setdefault(q.pair, []).append(q)

    opportunities: List[SpreadOpportunity] = []
    for pair, qs in by_pair.items():
        if len(qs) < 2:
            continue
        # group by direction to compare same-direction quotes
        by_dir: Dict[str, List[DexQuote]] = {}
        for q in qs:
            by_dir.setdefault(q.direction, []).append(q)

        for direction, dir_qs in by_dir.items():
            if len(dir_qs) < 2:
                continue
            sorted_qs = sorted(dir_qs, key=lambda x: x.price)
            cheapest = sorted_qs[0]
            most_expensive = sorted_qs[-1]
            spread_bps = (most_expensive.price - cheapest.price) / cheapest.price * 10000
            if spread_bps >= MIN_SPREAD_BPS:
                opportunities.append(
                    SpreadOpportunity(
                        pair=pair,
                        buy_dex=cheapest.dex,
                        buy_price=cheapest.price,
                        sell_dex=most_expensive.dex,
                        sell_price=most_expensive.price,
                        spread_bps=spread_bps,
                    )
                )
    return opportunities

def scan_once(client: httpx.Client, pairs: List[Tuple[str, str, str, str]]) -> List[SpreadOpportunity]:
    all_quotes: List[DexQuote] = []
    for name_a, mint_a, name_b, mint_b in pairs:
        quotes = fetch_pair_quotes(client, name_a, mint_a, name_b, mint_b)
        all_quotes.extend(quotes)
    return compute_spreads(all_quotes)

def parse_pair(pair_str: str) -> Tuple[str, str, str, str]:
    parts = pair_str.upper().split("/")
    if len(parts) != 2:
        raise ValueError(f"invalid pair: {pair_str}")
    a, b = parts
    if a not in KNOWN_MINTS or b not in KNOWN_MINTS:
        raise ValueError(f"unknown token in pair: {pair_str}")
    return (a, KNOWN_MINTS[a], b, KNOWN_MINTS[b])

def main() -> int:
    parser = argparse.ArgumentParser(
        description="watch stablecoin spreads across DEXs",
        usage="python stablecoin_arbitrage.py [--watch] [--pairs USDC/USDT,DAI/USDC]",
    )
    parser.add_argument("--watch", action="store_true", help="loop forever")
    parser.add_argument("--pairs", default="", help="comma-separated pairs like USDC/USDT,DAI/USDC")
    args = parser.parse_args()

    if args.pairs:
        pairs: List[Tuple[str, str, str, str]] = []
        for p in args.pairs.split(","):
            pairs.append(parse_pair(p.strip()))
    else:
        pairs = DEFAULT_PAIRS

    with httpx.Client() as client:
        if args.watch:
            while True:
                ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                spreads = scan_once(client, pairs)
                print(f"[{ts}]")
                for s in spreads:
                    print(f"  {s.pair}: {s.spread_bps:.2f} bps (buy {s.buy_dex} @ {s.buy_price:.6f}, sell {s.sell_dex} @ {s.sell_price:.6f})")
                if not spreads:
                    print("  no spreads found")
                time.sleep(WATCH_INTERVAL)
        else:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            spreads = scan_once(client, pairs)
            print(f"[{ts}]")
            for s in spreads:
                print(f"  {s.pair}: {s.spread_bps:.2f} bps (buy {s.buy_dex} @ {s.buy_price:.6f}, sell {s.sell_dex} @ {s.sell_price:.6f})")
            if not spreads:
                print("  no spreads found")

    return 0

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
