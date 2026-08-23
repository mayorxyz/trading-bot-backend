# debug_check.py — paste in same folder, run: python debug_check.py
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zigzag import get_zigzag_swings
from support_resistance import find_sr_levels
from entry import find_best_entry

result = find_best_entry(c[-1], "LONG", levels)
print("entry result:", result)

random.seed(7)
n = 300
h, l, c = [], [], []
p = 100.0
for _ in range(n):
    cl = p + random.uniform(-1.2, 1.2)
    hi = max(p, cl) + random.uniform(0, 0.8)
    lo = min(p, cl) - random.uniform(0, 0.8)
    h.append(hi); l.append(lo); c.append(cl)
    p = cl

swings = get_zigzag_swings(h, l, c)
print("swings found:", len(swings))
print("sample swings:", swings[:5])

levels = find_sr_levels(swings)
print("levels found:", len(levels))
print("sample levels:", levels[:5])

print("current price:", c[-1])