"""Trading Brain: NIFTY50 F&O Top-20 six-month research runner.

Research only. This module deliberately separates data acquisition from modelling.
It will not silently substitute web prices or fabricate missing Definedge data.

Expected input directory (CSV):
  universe.csv       symbol,option_volume,option_oi,bid_ask_spread,trading_frequency,underlying_liquidity
  opportunities.csv  timestamp,symbol,side,pnf,dsmart,rs,fast,option_momentum,liquidity,index_direction,trend_strength,realized_volatility,breadth,days_to_expiry,time_of_day,mfe,mae,realized_return,giveback,holding_minutes

The acquisition layer should generate these from Definedge historical/live evidence.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
import numpy as np
import pandas as pd

FEATURES = ["pnf","dsmart","rs","fast","option_momentum","liquidity","index_direction","trend_strength","realized_volatility","breadth","days_to_expiry","time_of_day"]


def z(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    sd = s.std(ddof=0)
    return (s-s.mean())/sd if sd and np.isfinite(sd) else pd.Series(0.0,index=s.index)


def select_top20(u: pd.DataFrame) -> pd.DataFrame:
    req={"symbol","option_volume","option_oi","bid_ask_spread","trading_frequency","underlying_liquidity"}
    missing=req-set(u.columns)
    if missing: raise ValueError(f"universe.csv missing {sorted(missing)}")
    x=u.copy()
    # Equal-weight transparent V1 liquidity score. Spread is a cost, hence negative.
    x["liquidity_score"]=(z(x.option_volume)+z(x.option_oi)-z(x.bid_ask_spread)+z(x.trading_frequency)+z(x.underlying_liquidity))/5
    return x.sort_values("liquidity_score",ascending=False).head(20)


def fit_ols(train: pd.DataFrame, features: list[str], target="mfe"):
    d=train[features+[target]].apply(pd.to_numeric,errors="coerce").dropna()
    if len(d) < len(features)+10: return None
    X=np.c_[np.ones(len(d)),d[features].to_numpy(float)]
    y=d[target].to_numpy(float)
    beta=np.linalg.lstsq(X,y,rcond=None)[0]
    return dict(zip(["intercept"]+features,beta.tolist()))


def metrics(d: pd.DataFrame):
    r=pd.to_numeric(d.realized_return,errors="coerce").dropna() if "realized_return" in d else pd.Series(dtype=float)
    pos=r[r>0].sum(); neg=-r[r<0].sum()
    return {"n":int(len(d)),"mean_mfe":float(pd.to_numeric(d.mfe,errors="coerce").mean()),"mean_mae":float(pd.to_numeric(d.mae,errors="coerce").mean()),"expectancy":float(r.mean()) if len(r) else None,"profit_factor":float(pos/neg) if neg>0 else None}


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--input",required=True); ap.add_argument("--output",required=True); a=ap.parse_args()
    inp=Path(a.input); out=Path(a.output); out.mkdir(parents=True,exist_ok=True)
    u=pd.read_csv(inp/"universe.csv"); top=select_top20(u); top.to_csv(out/"selected_top20.csv",index=False)
    opp=pd.read_csv(inp/"opportunities.csv"); opp["timestamp"]=pd.to_datetime(opp.timestamp); opp=opp[opp.symbol.isin(top.symbol)].sort_values("timestamp")
    if opp.empty: raise ValueError("No opportunities for selected Top-20 symbols")
    cut=opp.timestamp.quantile(.67); train=opp[opp.timestamp<=cut]; valid=opp[opp.timestamp>cut]
    report={"selected_symbols":top.symbol.tolist(),"discovery_end":str(cut),"discovery":metrics(train),"validation":metrics(valid),"models":{},"regimes":{}}
    usable=[f for f in FEATURES if f in opp.columns]
    for side in ["BULL","BEAR"]:
        tr=train[train.side.astype(str).str.upper()==side]; va=valid[valid.side.astype(str).str.upper()==side]
        report["models"][side]={"mfe_ols":fit_ols(tr,usable,"mfe"),"discovery":metrics(tr),"validation":metrics(va)}
    regime_cols=[c for c in ["index_direction","trend_strength","realized_volatility","breadth","days_to_expiry"] if c in opp.columns]
    if regime_cols:
        # Quantile-state summaries are descriptive; validation remains chronological.
        for c in regime_cols:
            try:
                states=pd.qcut(pd.to_numeric(opp[c],errors="coerce"),3,labels=["LOW","MID","HIGH"],duplicates="drop")
                report["regimes"][c]={str(k):metrics(opp.loc[idx]) for k,idx in states.groupby(states).groups.items()}
            except Exception: pass
    (out/"research_report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    opp.to_csv(out/"analysis_opportunities.csv",index=False)
    print(json.dumps({"status":"complete","symbols":len(top),"opportunities":len(opp),"output":str(out)}))

if __name__=="__main__": main()
