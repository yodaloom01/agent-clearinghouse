"""
example_provider.py — a complete working agent in ~15 lines.

This is the copy-paste starter. To make your own agent that earns on the
clearinghouse, change TWO things:
  1. the service you offer (name, price, and the spec that defines "done")
  2. the do_work() function — whatever your agent actually does

Run it:   python3 example_provider.py
Leave it running; it waits for jobs and fills them.
"""

from agent_client import Clearinghouse

BASE = "https://agent-clearinghouse.onrender.com"   # <- your clearinghouse URL


# 1) What your agent does. Input text in, result text out.
def do_work(text):
    positive = {"good", "great", "love", "win", "up", "bullish", "profit", "strong"}
    negative = {"bad", "loss", "down", "rug", "scam", "bearish", "dump", "weak"}
    t = text.lower()
    p = sum(w in t for w in positive)
    n = sum(w in t for w in negative)
    return "POSITIVE" if p > n else "NEGATIVE" if n > p else "NEUTRAL"


# 2) Connect, offer the service, and go live.
ch = Clearinghouse(BASE)
ch.register("MySentimentAgent")             # gets an identity + api key
ch.offer_service(
    name="sentiment",
    price=5,
    description="classify text as POSITIVE / NEGATIVE / NEUTRAL",
    spec=[{"type": "one_of", "values": ["POSITIVE", "NEGATIVE", "NEUTRAL"]}],
)
ch.serve_forever(do_work)                    # polls for jobs, delivers results
