# debug_check.py - synthetic sanity check for zigzag/S-R/entry helpers.
import os
import random
import sys


from tbb.indicators.zigzag import get_zigzag_swings
from tbb.indicators.zigzag import get_zigzag_swings
from tbb.indicators.support_resistance import find_sr_levels
from tbb.engines.entry import find_best_entry
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

result = find_best_entry(c[-1], "LONG", levels)
print("entry result:", result)

print("current price:", c[-1])
