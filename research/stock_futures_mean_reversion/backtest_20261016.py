#!/usr/bin/env python3
"""Read-only exploratory NSE cash-OHLC proxy for futures positional mean-reversion.
Universe preselected before results. Data: Yahoo chart *.NS, corporate-action-adjusted OHLC,
no live orders, no changes to OpenAlgo, no futures-price or roll modelling.
"""
import csv, json, math, statistics, sys, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlencode
import requests

START="2021-10-16"
SIX_MONTH_START="2026-04-16"
END="2026-10-16"
FETCH_START="2020-09-01"
FETCH_END="2026-10-17"
SYMS=("HDFCBANK","ICICIBANK","RELIANCE","SBIN","AXISBANK","INFY")
OUT=Path("/tmp/nse_mean_revert_20261016")
OUT.mkdir(exist_ok=True)
IST=ZoneInfo("Asia/Kolkata")
def epoch(d):
    return int(datetime.strptime(d,"%Y-%m-%d").replace(tzinfo=IST).timestamp())
def fetch(sym):
    url="https://query1.finance.yahoo.com/v8/finance/chart/"+sym+".NS"
    q={"period1":epoch(FETCH_START),"period2":epoch(FETCH_END),"interval":"1d","events":"div,splits"}
    res=requests.get(url,params=q,headers={"User-Agent":"Mozilla/5.0"},timeout=35)
    res.raise_for_status()
    obj=res.json()["chart"]["result"][0]
    ts=obj["timestamp"]
    qq=obj["indicators"]["quote"][0]
    adj=obj["indicators"].get("adjclose",[{}])[0].get("adjclose",[])
    rows=[]
    for k,t in enumerate(ts):
        date=datetime.fromtimestamp(t,IST).strftime("%Y-%m-%d")
        vals=[qq[c][k] for c in ("open","high","low","close")]
        if any(v is None or v<=0 for v in vals): continue
        o,h,l,c=map(float,vals)
        # Yahoo OHLC is already split-adjusted; adjustment ratio also applies dividends.
        a=adj[k] if k<len(adj) and adj[k] else c
        fac=a/c
        if h<max(o,c) or l>min(o,c): raise ValueError(f"Malformed OHLC for {sym} {date}")
        rows.append({"d":date,"o":o*fac,"h":h*fac,"l":l*fac,"c":c*fac,
                     "raw_close":c,"adj_factor":fac})
    if len(rows)<1100: raise ValueError(f"Insufficient bars {sym}: {len(rows)}")
    dates=[r["d"] for r in rows]
    if dates!=sorted(set(dates)): raise ValueError(f"Duplicate/out-of-order dates {sym}")
    for i,r in enumerate(rows):
        if i==0: continue
        ret=r["c"]/rows[i-1]["c"]-1
        if abs(ret)>0.25: print("LARGE_RET",sym,r["d"],round(ret*100,2))
    return rows,{"symbol":sym,"rows":len(rows),"start":dates[0],"end":dates[-1],
                 "last_raw_close":round(rows[-1]["raw_close"],2),
                 "adjustment_factor_first":round(rows[0]["adj_factor"],5),
                 "adjustment_factor_last":round(rows[-1]["adj_factor"],5)}
def sma(x,window):
    out=[None]*len(x)
    for i in range(window-1,len(x)):
        out[i]=sum(x[i-window+1:i+1])/window
    return out
def rsi(close,n=14):
    out=[None]*len(close)
    diffs=[close[i]-close[i-1] for i in range(1,len(close))]
    up=sum(max(d,0) for d in diffs[:n])/n
    dn=sum(max(-d,0) for d in diffs[:n])/n
    for i in range(n,len(close)):
        if i>n:
            d=close[i]-close[i-1]
            up=(up*(n-1)+max(d,0))/n
            dn=(dn*(n-1)+max(-d,0))/n
        out[i]=100 if dn==0 else 100-100/(1+up/dn)
    return out
def atr(rows,n=14):
    tr=[None]+[max(rows[i]["h"]-rows[i]["l"],
                    abs(rows[i]["h"]-rows[i-1]["c"]),
                    abs(rows[i]["l"]-rows[i-1]["c"])) for i in range(1,len(rows))]
    out=[None]*len(rows)
    if len(rows)<=n:return out
    x=sum(tr[1:n+1])/n
    out[n]=x
    for i in range(n+1,len(rows)):
        x=(x*(n-1)+tr[i])/n
        out[i]=x
    return out
def backtest(rows, trend, sym):
    closes=[r["c"] for r in rows]
    ma20=sma(closes,20); ma200=sma(closes,200); irsi=rsi(closes); iatr=atr(rows)
    trades=[]
    i=210
    while i<len(rows)-1:
        r=rows[i]; p=rows[i-1]
        # Previous session RSI oversold and closes under 20d average.
        prev_oversold=irsi[i-1] is not None and irsi[i-1]<40 and p["c"]<ma20[i-1]
        # Last 5 sessions near trailing 20-session low: must come within 2%.
        near_low=min(z["l"] for z in rows[i-5:i+1]) <= 1.02*min(z["l"] for z in rows[i-20:i+1])
        bounce=r["c"]>p["h"] and r["c"]>p["c"]
        uptrend=(not trend) or r["c"]>ma200[i]
        if not (prev_oversold and near_low and bounce and uptrend):
            i+=1;continue
        j=i+1
        entry=rows[j]["o"]
        stop=min(z["l"] for z in rows[i-4:i+1])-0.25*iatr[i]
        risk=entry-stop
        if not (entry>0 and 0.005<=risk/entry<=0.08):
            i+=1;continue
        target=entry+1.8*risk
        exit_idx=None; exit_price=None; why=None
        mae=0.0; mfe=0.0
        # User requested positional: max 15 trading days; stop before target when same-day ambiguous.
        for k in range(j,min(j+15,len(rows))):
            bar=rows[k]
            mae=max(mae,max(0,(entry-bar["l"])/risk))
            mfe=max(mfe,max(0,(bar["h"]-entry)/risk))
            if bar["o"]<=stop:
                exit_price=bar["o"];why="stop_gap"
            elif bar["o"]>=target:
                exit_price=bar["o"];why="target_gap"
            elif bar["l"]<=stop:
                exit_price=stop;why="stop"
            elif bar["h"]>=target:
                exit_price=target;why="target"
            elif k==min(j+14,len(rows)-1):
                exit_price=bar["c"];why="time_or_end"
            if exit_price is not None:
                exit_idx=k
                break
        if exit_idx is None:raise ValueError("exit missed")
        gross=100*(exit_price/entry-1)
        cost=0.20 # roundtrip notional stress proxy, not a true futures fee schedule.
        net=gross-cost
        trades.append({"symbol":sym,"strategy":"trend_above_200sma" if trend else "no_trend_filter",
                       "signal":rows[i]["d"],"entry_date":rows[j]["d"],"exit_date":rows[exit_idx]["d"],
                       "entry":round(entry,4),"stop":round(stop,4),"target":round(target,4),
                       "exit":round(exit_price,4),"exit_reason":why,"holding_days":exit_idx-j+1,
                       "gross_pct":round(gross,5),"net_pct":round(net,5),
                       "MFE_R":round(mfe,3),"MAE_R":round(mae,3),
                       "terminal_mark":exit_idx==len(rows)-1 and why=="time_or_end"})
        i=exit_idx+1 # don't overlap positions on same symbol, don't flip on exit day
    return trades
def stats(trades, begin):
    eligible=[t for t in trades if begin<=t["entry_date"]<=END]
    sub=[t for t in eligible if not t["terminal_mark"]]
    if not sub:return {"trades":0,"open_at_end":len(eligible),"win_pct":None,"average_net_pct":None,"comp_net_pct":None,"profit_factor":None,"max_closed_dd_pct":0.0,"gross_total_pct":0,"net_sum_pct":0,"avg_hold_days":None,"avg_mfe_R":None,"avg_mae_R":None,"best_net_pct":None,"worst_net_pct":None}
    rets=[t["net_pct"] for t in sub]
    equity=1.0; high=1.0; dd=0.0
    for net in rets:
        equity*=1+net/100;high=max(high,equity);dd=max(dd,100*(high-equity)/high)
    gain=sum(x for x in rets if x>0); loss=-sum(x for x in rets if x<0)
    return {"trades":len(sub),"open_at_end":len(eligible)-len(sub),"win_pct":round(sum(x>0 for x in rets)*100/len(rets),1),
            "average_net_pct":round(statistics.mean(rets),3),
            "comp_net_pct":round((equity-1)*100,2),
            "profit_factor":round(gain/loss,2) if loss else None,
            "max_closed_dd_pct":round(dd,2),
            "gross_total_pct":round(sum(t["gross_pct"] for t in sub),2),
            "net_sum_pct":round(sum(rets),2),
            "avg_hold_days":round(statistics.mean(t["holding_days"] for t in sub),1),
            "avg_mfe_R":round(statistics.mean(t["MFE_R"] for t in sub),2),
            "avg_mae_R":round(statistics.mean(t["MAE_R"] for t in sub),2),
            "best_net_pct":round(max(rets),2),"worst_net_pct":round(min(rets),2),
            "stop_exits":sum(t["exit_reason"].startswith("stop") for t in sub),
            "target_exits":sum(t["exit_reason"].startswith("target") for t in sub),
            "time_exits":sum(t["exit_reason"].startswith("time") for t in sub),
            "open_terminal_mark":sum(t["terminal_mark"] for t in sub)}
def main():
    alltrades=[];report=[];dataaudit=[];benchmarks={}
    for sym in SYMS:
        rows,audit=fetch(sym);dataaudit.append(audit)
        baseline5=[z for z in rows if z["d"]>=START]
        baseline6=[z for z in rows if z["d"]>=SIX_MONTH_START]
        benchmarks[sym]={"five_year_pct":round(100*(baseline5[-1]["c"]/baseline5[0]["c"]-1),2),
                         "six_month_pct":round(100*(baseline6[-1]["c"]/baseline6[0]["c"]-1),2)}
        for trend in (True,False):
            tt=backtest(rows,trend,sym); alltrades+=tt
            rec={"symbol":sym,"variant":"TREND" if trend else "ALL"}
            rec["5_year"]=stats(tt,START);rec["6_month"]=stats(tt,SIX_MONTH_START)
            report.append(rec)
            print(json.dumps(rec),flush=True)
    payload={"scope":{"start":START,"6_month_start":SIX_MONTH_START,"end":END,
                      "universe":list(SYMS),
                      "price_source":"Yahoo Finance daily chart adjusted cash OHLC; NOT NSE futures",
                      "entry":"signal close: prev RSI14<40, prev close<20SMA, low previous5 within2% rolling20 low, close>prior high; entry next session open",
                      "stop":"min signal-4 to signal low minus 0.25*ATR14",
                      "target":"1.8R profit; stop or target intraday, conservative stop priority, max hold 15 trading days",
                      "cost":"0.20% round trip notional cost, no financing or futures rollover",
                      "price_adjustment":"OHLC scaled by Yahoo adjusted-close/close (includes dividends, price NOT futures)",
                      "trades_nonoverlapping":"same stock only",
                      "performance":"per-trade compounded return as if all-in with no leverage, not a real futures portfolio",
                      "trend_variant":"close > 200-day SMA on signal day",
                      "ALL_variant":"no 200-day trend filter",
                      "limitation":"six fixed NSE symbols selected at present: historical survivorship bias; 5y results cash-proxy only"},
             "data_audit":dataaudit,"benchmarks":benchmarks,"results":report}
    (OUT/"report.json").write_text(json.dumps(payload,indent=2))
    with (OUT/"trades.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(alltrades[0]));w.writeheader();w.writerows(alltrades)
    print("OUTPUT",OUT,"TRADES",len(alltrades))
if __name__=="__main__":main()
