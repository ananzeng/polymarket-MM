"""
Web dashboard that runs the LP maker bot and shows live PnL, reward rate (market CP),
and order scoring, with Start/Stop control.

Run: venv/bin/python dashboard.py    then open http://localhost:8000
Ctrl+C stops the bot, cancels all orders, and flattens inventory.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import lp_maker as lm
from pnl import userFills, fillsToCashAndPos, rewardForDay, dayStartUtc, netCpPerDay
from py_clob_client_v2 import TradeParams, AssetType

logger = logging.getLogger(__name__)

PORT = int(os.getenv("DASHBOARD_PORT", "8000"))
STATS_INTERVAL = 12          # seconds between stats collections
RATE_WINDOW = 900            # sliding window (s) for the reward-rate measurement
RATE_LOG_INTERVAL = int(os.getenv("REWARD_RATE_LOG_INTERVAL", "300"))  # snapshot cadence (s)
DB_PATH = os.path.join(lm.logDir, "dashboard.db")
CHART_WINDOW_HOURS = int(os.getenv("CHART_WINDOW_HOURS", "48"))  # rolling time window shown on charts
RECENT_FILLS_LIMIT = 20
SAMPLES_MAXLEN = int(CHART_WINDOW_HOURS * 3600 / STATS_INTERVAL) + 50


class BotManager:
    def __init__(self):
        self.market = lm.resolveMarket()
        self.client = lm.initClobClient()
        self.maker = None
        self.thread = None
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        with self.lock:
            if self.running():
                return
            self.maker = lm.PolymarketMaker(self.client, self.market)
            self.thread = threading.Thread(target=self.maker.run, daemon=True)
            self.thread.start()
            logger.info("Bot started")

    def stop(self):
        with self.lock:
            if not self.maker:
                return
            self.maker.stopEvent.set()
            if self.thread:
                self.thread.join(timeout=lm.pollInterval + 15)
            self.maker.shutdown()
            logger.info("Bot stopped, orders cancelled")


class Stats:
    def __init__(self, bot: BotManager):
        self.bot = bot
        self.funder = lm.getFunder()
        self.rewardHist = deque(maxlen=600)   # (ts, cumRewardToday)
        self.samples = deque(maxlen=SAMPLES_MAXLEN)  # (ts, rewardToday, accountValue) for the chart
        self.rateSnapshots = deque(maxlen=2000)  # (ts, per5min, perHour, perDay) for the rate chart
        self.lastRateLogTs = 0.0
        self.snapshot = {}
        self.snapshotJson = "{}"
        os.makedirs(lm.logDir, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS samples(
                epoch REAL PRIMARY KEY, isoTime TEXT, reward REAL, accountValue REAL);
            CREATE TABLE IF NOT EXISTS rateSnapshots(
                epoch REAL PRIMARY KEY, isoTime TEXT, per5min REAL, perHour REAL, perDay REAL);
        """)
        self.db.commit()
        self._loadDb()

    def loop(self):
        while True:
            try:
                self._collect()
            except Exception as e:
                logger.error("stats error: %s", e)
            time.sleep(STATS_INTERVAL)

    def _accountValue(self, mid: float) -> float:
        """USDC balance plus this market's Yes/No holdings valued at mid."""
        usdc = lm.getBalanceShares(self.bot.client, assetType=AssetType.COLLATERAL)
        yesBal = lm.getBalanceShares(self.bot.client, self.bot.market["yesToken"])
        noBal = lm.getBalanceShares(self.bot.client, self.bot.market["noToken"])
        return usdc + yesBal * mid + noBal * (1 - mid)

    def _loadDb(self):
        """Preload recent rows within the chart window so charts survive a restart."""
        cutoff = time.time() - CHART_WINDOW_HOURS * 3600
        for epoch, reward, accountValue in self.db.execute(
                "SELECT epoch, reward, accountValue FROM samples WHERE epoch>=? ORDER BY epoch", (cutoff,)):
            self.samples.append((epoch, reward, accountValue))
        for epoch, per5min, perHour, perDay in self.db.execute(
                "SELECT epoch, per5min, perHour, perDay FROM rateSnapshots WHERE epoch>=? ORDER BY epoch", (cutoff,)):
            self.rateSnapshots.append((epoch, per5min, perHour, perDay))

    def _logRateSnapshot(self, ts: float, per5min: float, perHour: float, perDay: float):
        iso = datetime.fromtimestamp(ts, lm.TZ_UTC8).isoformat()
        self.db.execute("INSERT OR REPLACE INTO rateSnapshots VALUES(?,?,?,?,?)",
                        (ts, iso, per5min, perHour, perDay))
        self.db.commit()
        self.rateSnapshots.append((ts, per5min, perHour, perDay))

    def _logSample(self, ts: float, reward: float, accountValue: float):
        iso = datetime.fromtimestamp(ts, lm.TZ_UTC8).isoformat()
        self.db.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?,?)",
                        (ts, iso, reward, accountValue))
        self.db.commit()
        self.samples.append((ts, reward, accountValue))

    def _collect(self):
        now = time.time()
        nowUtc = datetime.now(timezone.utc)
        dayStart = dayStartUtc(nowUtc)

        reward = rewardForDay(self.bot.client, nowUtc.strftime("%Y-%m-%d"))
        self.rewardHist.append((now, reward))
        # Only look back within the current UTC day: rewardForDay resets to 0 at UTC midnight,
        # so a base from the previous day would make the rate spike sharply negative.
        base = next((s for s in self.rewardHist if s[0] >= max(now - RATE_WINDOW, dayStart)),
                    self.rewardHist[0])
        dt = now - base[0]
        ratePerSec = (reward - base[1]) / dt if dt >= 30 else 0.0
        per5min, perHour, perDay = ratePerSec * 300, ratePerSec * 3600, ratePerSec * 86400

        if now - self.lastRateLogTs >= RATE_LOG_INTERVAL:
            self._logRateSnapshot(now, round(per5min, 4), round(perHour, 4), round(perDay, 2))
            self.lastRateLogTs = now

        trades = self.bot.client.get_trades(TradeParams(market=self.bot.market["conditionId"]))
        fills = userFills(trades, self.funder)
        todayFills = [f for f in fills if f[0] >= dayStart]

        mid = (self.bot.maker.lastMid if self.bot.maker and self.bot.maker.lastMid else 0.5)

        # Mark-to-market any position still open at this instant (e.g. sampled between a fill
        # and its automatic flatten a few seconds later) instead of valuing it at zero.
        cashToday, posToday = fillsToCashAndPos(todayFills)
        tradingToday = cashToday + posToday["Yes"] * mid + posToday["No"] * (1 - mid)

        cashAll, pos = fillsToCashAndPos(fills)
        recentFills = [
            {"time": datetime.fromtimestamp(ts, lm.TZ_UTC8).strftime("%m-%d %H:%M:%S"),
             "outcome": oc, "side": side, "size": round(sz, 2), "price": px}
            for ts, oc, side, sz, px in reversed(fills[-RECENT_FILLS_LIMIT:])
        ]

        tradingAll = cashAll + pos["Yes"] * mid + pos["No"] * (1 - mid)
        accountValue = self._accountValue(mid)

        net = reward + tradingToday
        self._logSample(now, round(reward, 4), round(accountValue, 4))

        maker = self.bot.maker
        elapsedH = max((now - dayStart) / 3600, 0.1)
        self.snapshot = {
            "time": nowUtc.strftime("%H:%M:%S UTC"),
            "bot": {
                "running": self.bot.running(),
                "market": self.bot.market["question"],
                "mode": "bait-layer" if lm.baitEnabled else "simple",
                "dryRun": lm.dryRun,
                "mid": maker.lastMid if maker else None,
                "quotes": maker.lastQuotes if maker else {},
                "scoring": maker.lastScoring if maker else {},
                "scoringAge": round(now - maker.lastScoringTs) if maker and maker.lastScoringTs else None,
                "cooldown": max(0, round(maker.cooldownUntil - now)) if maker else 0,
                "wsConnected": (maker.fillFeed.connected if (maker and maker.fillFeed) else None),
            },
            "rewards": {
                "today": round(reward, 4),
                "per5min": round(per5min, 4),
                "perHour": round(perHour, 4),
                "perDay": round(perDay, 2),
                "windowSec": round(dt),
            },
            "pnl": {
                "tradingToday": round(tradingToday, 4),
                "netToday": round(net, 4),
                "fillsToday": len(todayFills),
                "tradingAllTime": round(tradingAll, 4),
                "capital": lm.orderSize,
                "netCpPerDay": round(netCpPerDay(net, elapsedH, lm.orderSize), 2),
                "accountValue": round(accountValue, 2),
            },
            "history": [list(s) for s in self.samples],
            "rateHistory": [list(s) for s in self.rateSnapshots],
            "recentFills": recentFills,
        }
        # Serialize once per collection; /api/stats polls faster than the data changes.
        self.snapshotJson = json.dumps(self.snapshot)


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>polymarket-MM</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:-apple-system,Helvetica,sans-serif;background:#0f1419;color:#e6e6e6;margin:0;padding:18px}
h1{font-size:16px;margin:0 0 4px} #market{color:#8a93a2;font-size:13px}
.bar{margin:12px 0}
button{background:#2E86DE;color:#fff;border:0;border-radius:6px;padding:8px 22px;font-size:14px;cursor:pointer;margin-right:8px}
button.stop{background:#c0392b} button:disabled{opacity:.4;cursor:default}
.badge{padding:3px 10px;border-radius:10px;font-size:12px}
.on{background:#14532d;color:#3ddc84} .off{background:#4a1d1d;color:#ff5d5d}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-top:12px}
.card{background:#1a212b;border-radius:10px;padding:12px 16px;min-width:190px;flex:1}
.card h2{font-size:11px;color:#8a93a2;margin:0 0 8px;text-transform:uppercase;letter-spacing:.5px}
.big{font-size:22px;font-weight:600}.sub{font-size:12px;color:#8a93a2;margin-top:4px}
.pos{color:#3ddc84}.neg{color:#ff5d5d}
table{border-collapse:collapse;font-size:13px}td{padding:2px 10px 2px 0;color:#c9d1dc}
#chart,#rewardChart,#rateChart{width:100%;height:260px;background:#1a212b;border-radius:10px;margin-top:12px;cursor:crosshair}
.legend{font-size:12px;color:#8a93a2;margin-top:6px}
.legend span{margin-right:16px}
h3{font-size:13px;color:#8a93a2;margin:20px 0 0;font-weight:500}
.fillsBox{background:#1a212b;border-radius:10px;padding:6px 14px;margin-top:12px;max-height:260px;overflow-y:auto}
.fillsBox table{width:100%}
.fillsBox th{text-align:left;color:#6b7482;font-weight:500;font-size:11px;padding:6px 10px 6px 0;position:sticky;top:0;background:#1a212b}
.fillsBox td{padding:4px 10px 4px 0}
.buy{color:#2E86DE}.sell{color:#3ddc84}
</style></head><body>
<h1>polymarket-MM dashboard</h1><div id=market>loading…</div>
<div class=bar>
<button id=btnStart onclick="ctl('start')">Start</button>
<button id=btnStop class=stop onclick="ctl('stop')">Stop</button>
<span id=status class=badge></span> <span id=mode class=badge></span> <span id=ws class=badge></span> <span id=cool class=badge></span>
</div>
<div class=cards>
<div class=card><h2>Reward rate (market CP)</h2><div class=big id=r5>–</div>
<div class=sub id=rmore></div></div>
<div class=card><h2>Rewards today</h2><div class=big id=rtoday>–</div><div class=sub id=rtime></div></div>
<div class=card><h2>Trading PnL today</h2><div class=big id=ptrade>–</div><div class=sub id=pfills></div></div>
<div class=card><h2>Net today / CP</h2><div class=big id=pnet>–</div><div class=sub id=pcp></div></div>
<div class=card><h2>Quotes</h2><table id=quotes></table><div class=sub id=mid></div></div>
<div class=card><h2>Scoring (earning?)</h2><div id=scoring class=big style="font-size:16px">–</div><div class=sub id=sage></div></div>
</div>
<h3>Recent fills (from Polymarket trade records)</h3>
<div class=fillsBox><table><thead><tr><th>Time</th><th>Outcome</th><th>Side</th><th>Size</th><th>Price</th><th>Value</th></tr></thead>
<tbody id=fillsBody></tbody></table></div>
<h3>Account value, rewards &amp; reward rate (last __CHART_WINDOW_H__h)</h3>
<canvas id=chart></canvas>
<div class=legend><span style="color:#2E86DE">■ account value</span></div>
<canvas id=rewardChart></canvas>
<div class=legend><span style="color:#3ddc84">■ rewards</span></div>
<canvas id=rateChart></canvas>
<div class=legend><span style="color:#f0a63a">■ $/5min (snapshot every 5min)</span></div>
<script>
const CHART_WINDOW_H = __CHART_WINDOW_H__;
async function ctl(a){await fetch('/api/'+a,{method:'POST'});setTimeout(refresh,600)}
function fmt(x,d=4){return (x>=0?'+':'')+x.toFixed(d)}
function cls(el,x){el.className='big '+(x>=0?'pos':'neg')}
async function refresh(){
 let s;try{s=await(await fetch('/api/stats')).json()}catch(e){return}
 if(!s.bot)return;
 document.getElementById('market').textContent=s.bot.market;
 const st=document.getElementById('status');
 st.textContent=s.bot.running?(s.bot.dryRun?'RUNNING (dry)':'RUNNING'):'STOPPED';
 st.className='badge '+(s.bot.running?'on':'off');
 document.getElementById('mode').textContent=s.bot.mode;
 document.getElementById('mode').className='badge on';
 const w=document.getElementById('ws');
 w.textContent=s.bot.wsConnected==null?'':(s.bot.wsConnected?'WS ✓':'WS ✗');
 w.className='badge '+(s.bot.wsConnected==null?'':(s.bot.wsConnected?'on':'off'));
 const c=document.getElementById('cool');
 c.textContent=s.bot.cooldown>0?('cooldown '+s.bot.cooldown+'s'):'';c.className=s.bot.cooldown>0?'badge off':'badge';
 document.getElementById('btnStart').disabled=s.bot.running;
 document.getElementById('btnStop').disabled=!s.bot.running;
 document.getElementById('r5').textContent='$'+s.rewards.per5min.toFixed(4)+' / 5min';
 document.getElementById('rmore').textContent='$'+s.rewards.perHour.toFixed(3)+'/hr · $'+s.rewards.perDay.toFixed(2)+'/day · window '+s.rewards.windowSec+'s';
 document.getElementById('rtoday').textContent='$'+s.rewards.today.toFixed(4);
 document.getElementById('rtime').textContent='as of '+s.time;
 const pt=document.getElementById('ptrade');pt.textContent='$'+fmt(s.pnl.tradingToday);cls(pt,s.pnl.tradingToday);
 document.getElementById('pfills').textContent=s.pnl.fillsToday+' fills today · all-time trading $'+fmt(s.pnl.tradingAllTime,2);
 const pn=document.getElementById('pnet');pn.textContent='$'+fmt(s.pnl.netToday);cls(pn,s.pnl.netToday);
 document.getElementById('pcp').textContent='net CP '+fmt(s.pnl.netCpPerDay,2)+'%/day on $'+s.pnl.capital;
 const q=document.getElementById('quotes');
 q.innerHTML=Object.entries(s.bot.quotes).map(([k,v])=>'<tr><td>'+k+'</td><td>'+v+'</td></tr>').join('');
 document.getElementById('mid').textContent=s.bot.mid?('mid '+s.bot.mid.toFixed(4)):'';
 const sc=document.getElementById('scoring');
 const ent=Object.entries(s.bot.scoring);
 sc.textContent=ent.length?ent.map(([k,v])=>k+' '+(v?'✅':'❌')).join('  '):'–';
 document.getElementById('sage').textContent=s.bot.scoringAge!=null?('checked '+s.bot.scoringAge+'s ago'):'';
 const fb=document.getElementById('fillsBody');
 fb.innerHTML=(s.recentFills&&s.recentFills.length)?s.recentFills.map(f=>
   '<tr><td>'+f.time+'</td><td>'+f.outcome+'</td><td class="'+(f.side==='BUY'?'buy':'sell')+'">'+f.side+'</td>'
   +'<td>'+f.size+'</td><td>'+f.price+'</td><td>$'+(f.size*f.price).toFixed(2)+'</td></tr>'
 ).join(''):'<tr><td colspan=6 style="color:#6b7482">No fills yet</td></tr>';
 renderChart('chart', s.history, [{idx:2,color:'#2E86DE',w:1.8,label:'account value'}], false);
 renderChart('rewardChart', s.history, [{idx:1,color:'#3ddc84',w:1.4,label:'rewards'}]);
 renderChart('rateChart', s.rateHistory, [{idx:1,color:'#f0a63a',w:1.8,label:'$/5min'}]);
}
function fmtTime(ts){return new Date(ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}
function fmtTimeSec(ts){return new Date(ts*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function fmtMoney(v){return (v>=0?'$':'-$')+Math.abs(v).toFixed(2)}
function fmtMoneyPrecise(v){return (v>=0?'$':'-$')+Math.abs(v).toFixed(4)}
const lastData={}, hoverIdx={};
function renderChart(id, hist, series, zeroBase=true){
 const cv=document.getElementById(id);const W=cv.width=cv.clientWidth;const H=cv.height=260;
 const g=cv.getContext('2d');g.clearRect(0,0,W,H);
 const L=52,R=10,T=10,B=22;
 const t1=Date.now()/1000, t0=t1-CHART_WINDOW_H*3600;
 const pts=(hist||[]).filter(s=>s[0]>=t0);
 lastData[id]={hist,series,zeroBase,pts};
 g.font='11px -apple-system,sans-serif';
 if(!pts.length){
   g.fillStyle='#6b7482';g.fillText('No data in the last '+CHART_WINDOW_H+'h yet', L, H/2);
   return;
 }
 let vals=zeroBase?[0]:[];pts.forEach(s=>series.forEach(sr=>vals.push(s[sr.idx])));
 let lo=Math.min(...vals),hi=Math.max(...vals);if(hi-lo<1e-6){hi=lo+1}
 if(!zeroBase){const pad=(hi-lo)*0.1;lo-=pad;hi+=pad}
 const X=t=>L+(t-t0)/(t1-t0)*(W-L-R);
 const Y=v=>H-B-(v-lo)/(hi-lo)*(H-T-B);
 for(let i=0;i<=4;i++){
   const v=lo+(hi-lo)*i/4, y=Y(v);
   g.strokeStyle='#232a35';g.beginPath();g.moveTo(L,y);g.lineTo(W-R,y);g.stroke();
   g.fillStyle='#6b7482';g.fillText(fmtMoney(v),2,y+3);
 }
 for(let i=0;i<=6;i++){
   const t=t0+(t1-t0)*i/6, x=X(t);
   g.strokeStyle='#1e242d';g.beginPath();g.moveTo(x,T);g.lineTo(x,H-B);g.stroke();
   g.fillStyle='#6b7482';g.fillText(fmtTime(t),Math.min(Math.max(x-18,L),W-R-34),H-6);
 }
 if(lo<0&&hi>0){g.strokeStyle='#3a4454';g.beginPath();g.moveTo(L,Y(0));g.lineTo(W-R,Y(0));g.stroke();}
 series.forEach(sr=>{
   g.strokeStyle=sr.color;g.lineWidth=sr.w||1.5;g.beginPath();
   pts.forEach((s,i)=>{i?g.lineTo(X(s[0]),Y(s[sr.idx])):g.moveTo(X(s[0]),Y(s[sr.idx]))});
   g.stroke();
 });
 const hoverI=hoverIdx[id];
 if(hoverI!=null&&pts[hoverI]){
   const s=pts[hoverI],x=X(s[0]);
   g.strokeStyle='#4a5468';g.lineWidth=1;g.setLineDash([3,3]);
   g.beginPath();g.moveTo(x,T);g.lineTo(x,H-B);g.stroke();g.setLineDash([]);
   series.forEach(sr=>{
     g.fillStyle=sr.color;g.beginPath();g.arc(x,Y(s[sr.idx]),3.2,0,Math.PI*2);g.fill();
     g.strokeStyle='#0f1419';g.lineWidth=1;g.stroke();
   });
   const lines=[fmtTimeSec(s[0])].concat(series.map(sr=>sr.label+' '+fmtMoneyPrecise(s[sr.idx])));
   g.font='11px -apple-system,sans-serif';
   const tw=Math.max(...lines.map(l=>g.measureText(l).width))+16;
   const th=lines.length*15+8;
   let tx=x+10;if(tx+tw>W-R)tx=x-10-tw;
   const ty=T+4;
   g.fillStyle='rgba(26,33,43,0.95)';g.beginPath();g.rect(tx,ty,tw,th);g.fill();
   g.strokeStyle='#333d4b';g.lineWidth=1;g.stroke();
   lines.forEach((l,i)=>{g.fillStyle=i===0?'#8a93a2':'#e6e6e6';g.fillText(l,tx+8,ty+15+i*15)});
 }
}
function bindHover(id){
 const cv=document.getElementById(id);
 cv.addEventListener('mousemove',e=>{
   const d=lastData[id];if(!d||!d.pts||!d.pts.length)return;
   const rect=cv.getBoundingClientRect();
   const mx=(e.clientX-rect.left)*(cv.width/rect.width);
   const L=52,R=10,t1=Date.now()/1000,t0=t1-CHART_WINDOW_H*3600;
   const pts=d.pts;
   const X=t=>L+(t-t0)/(t1-t0)*(cv.width-L-R);
   let bestI=0,bestD=Infinity;
   pts.forEach((s,i)=>{const dist=Math.abs(X(s[0])-mx);if(dist<bestD){bestD=dist;bestI=i}});
   if(bestI===hoverIdx[id])return;
   hoverIdx[id]=bestI;
   renderChart(id,d.hist,d.series,d.zeroBase);
 });
 cv.addEventListener('mouseleave',()=>{
   if(hoverIdx[id]==null)return;
   hoverIdx[id]=null;
   const d=lastData[id];if(d)renderChart(id,d.hist,d.series,d.zeroBase);
 });
}
bindHover('chart');bindHover('rewardChart');bindHover('rateChart');
setInterval(refresh,3000);refresh();
</script></body></html>"""

PAGE = PAGE.replace("__CHART_WINDOW_H__", str(CHART_WINDOW_HOURS))


def makeHandler(bot: BotManager, stats: Stats):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                self._send(200, PAGE, "text/html")
            elif self.path == "/api/stats":
                self._send(200, stats.snapshotJson)
            else:
                self._send(404, "{}")

        def do_POST(self):
            if self.path == "/api/start":
                bot.start()
                self._send(200, '{"ok":true}')
            elif self.path == "/api/stop":
                threading.Thread(target=bot.stop, daemon=True).start()
                self._send(200, '{"ok":true}')
            else:
                self._send(404, "{}")

    return Handler


def main():
    lm.setupLogging()
    bot = BotManager()
    stats = Stats(bot)
    threading.Thread(target=stats.loop, daemon=True).start()
    bot.start()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), makeHandler(bot, stats))
    logger.info("Dashboard at http://localhost:%d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        bot.stop()
        server.server_close()
        logger.info("Done")


if __name__ == "__main__":
    main()
