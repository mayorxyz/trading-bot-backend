import sqlite3, pandas as pd
c = sqlite3.connect(r"data\candidate_outcomes.db")
df = pd.read_sql("select * from candidate_outcomes", c)
f = df[df.filled == 1].copy()
comps = ["pattern_match","trend_align","mtf_alignment","sr_level_strength","wick_rejection",
         "fib_score","volume_confirm","structure_bos_align","liquidity_target","no_mss_conflict",
         "chart_pattern_align","retracement_confirm","breakout_score","elliott_score",
         "structure_retest_confirm","session_timing"]
def st(d):
    return pd.Series({"n": len(d), "r1": d.r1_24.mean(), "r15": d.r15_24.mean(),
                      "r2": d.r2_24.mean(), "mfe": d.mfe_24.mean()})
print("\nBASELINE by symbol (break-even: r1 50%, r15 40%, r2 33%)")
print(f.groupby("symbol").apply(st).round(3))
f["band"] = pd.cut(f.confluence_score, [-1, 29, 39, 49, 100], labels=["<30","30-39","40-49","50+"])
print("\nBY SCORE BAND")
print(f.groupby("band", observed=True).apply(st).round(3))
print("\nBY DIRECTION")
print(f.groupby("direction").apply(st).round(3))
rows = []
for k in comps:
    a, b = f[f[k] > 0], f[f[k] <= 0]
    rows.append({"comp": k, "n_on": len(a), "n_off": len(b),
                 "r2_on": a.r2_24.mean(), "r2_off": b.r2_24.mean(),
                 "r15_on": a.r15_24.mean(), "r15_off": b.r15_24.mean()})
t = pd.DataFrame(rows)
t["lift_r2"] = t.r2_on - t.r2_off
print("\nCOMPONENT: fired vs not fired")
print(t.sort_values("lift_r2").round(3).to_string(index=False))
