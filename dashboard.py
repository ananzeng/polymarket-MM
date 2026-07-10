"""
Web dashboard that runs the LP maker bot and shows live PnL, reward rate (market CP),
and order scoring, with Start/Stop control.

Run: venv/bin/python dashboard.py    then open http://localhost:8000
Ctrl+C stops the bot, cancels all orders, and flattens inventory.
"""
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import lp_maker as lm
from pnl import userFills
from py_clob_client_v2 import TradeParams

logger = logging.getLogger(__name__)

PORT = int(os.getenv("DASHBOARD_PORT", "8000"))
STATS_INTERVAL = 12          # seconds between stats collections
RATE_WINDOW = 900            # sliding window (s) for the reward-rate measurement


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
            self.maker._cancelAll()
            self.maker._flattenInventory()
            logger.info("Bot stopped, orders cancelled")


class Stats:
    def __init__(self, bot: BotManager):
        self.bot = bot
        self.funder = os.environ["POLYMARKET_FUNDER"].lower()
        self.rewardHist = deque(maxlen=600)   # (ts, cumRewardToday)
        self.samples = deque(maxlen=2000)     # (ts, rewardToday, tradingToday, net) for the chart
        self.snapshot = {}

    def loop(self):
        while True:
            try:
                self._collect()
            except Exception as e:
                logger.error("stats error: %s", e)
            time.sleep(STATS_INTERVAL)

    def _rewardToday(self) -> float:
        d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = self.bot.client.get_total_earnings_for_user_for_day(d)
        return sum(float(r.get("earnings", 0) or 0) for r in rows) if rows else 0.0

    def _collect(self):
        now = time.time()
        nowUtc = datetime.now(timezone.utc)

        reward = self._rewardToday()
        self.rewardHist.append((now, reward))
        base = next((s for s in self.rewardHist if s[0] >= now - RATE_WINDOW), self.rewardHist[0])
        dt = now - base[0]
        ratePerSec = (reward - base[1]) / dt if dt >= 30 else 0.0

        trades = self.bot.client.get_trades(TradeParams(market=self.bot.market["conditionId"]))
        fills = userFills(trades, self.funder)
        dayStart = datetime(nowUtc.year, nowUtc.month, nowUtc.day, tzinfo=timezone.utc).timestamp()
        todayFills = [f for f in fills if f[0] >= dayStart]
        tradingToday = sum((px * sz if side == "SELL" else -px * sz)
                           for _, oc, side, sz, px in todayFills)

        pos = {"Yes": 0.0, "No": 0.0}
        cashAll = 0.0
        for _, oc, side, sz, px in fills:
            if side == "BUY":
                cashAll -= px * sz
                pos[oc] += sz
            else:
                cashAll += px * sz
                pos[oc] -= sz
        mid = (self.bot.maker.lastMid if self.bot.maker and self.bot.maker.lastMid else 0.5)
        tradingAll = cashAll + pos["Yes"] * mid + pos["No"] * (1 - mid)

        net = reward + tradingToday
        self.samples.append((now, round(reward, 4), round(tradingToday, 4), round(net, 4)))

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
            },
            "rewards": {
                "today": round(reward, 4),
                "per5min": round(ratePerSec * 300, 4),
                "perHour": round(ratePerSec * 3600, 4),
                "perDay": round(ratePerSec * 86400, 2),
                "windowSec": round(dt),
            },
            "pnl": {
                "tradingToday": round(tradingToday, 4),
                "netToday": round(net, 4),
                "fillsToday": len(todayFills),
                "tradingAllTime": round(tradingAll, 4),
                "capital": lm.orderSize,
                "netCpPerDay": round((net / elapsedH * 24) / lm.orderSize * 100, 2),
            },
            "history": [list(s) for s in self.samples],
        }


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
#chart{width:100%;height:260px;background:#1a212b;border-radius:10px;margin-top:12px}
.legend{font-size:12px;color:#8a93a2;margin-top:6px}
.legend span{margin-right:16px}
</style></head><body>
<h1>polymarket-MM dashboard</h1><div id=market>loading…</div>
<div class=bar>
<button id=btnStart onclick="ctl('start')">Start</button>
<button id=btnStop class=stop onclick="ctl('stop')">Stop</button>
<span id=status class=badge></span> <span id=mode class=badge></span> <span id=cool class=badge></span>
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
<canvas id=chart></canvas>
<div class=legend><span style="color:#3ddc84">■ rewards</span><span style="color:#2E86DE">■ trading</span><span style="color:#b07cd8">■ net</span></div>
<script>
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
 draw(s.history);
}
function draw(h){
 const cv=document.getElementById('chart');const W=cv.width=cv.clientWidth;const H=cv.height=260;
 const g=cv.getContext('2d');g.clearRect(0,0,W,H);
 if(!h||h.length<2)return;
 const t0=h[0][0],t1=h[h.length-1][0];
 let vals=[];h.forEach(s=>{vals.push(s[1],s[2],s[3])});
 let lo=Math.min(...vals,0),hi=Math.max(...vals,0);if(hi-lo<1e-6){hi=lo+1}
 const X=t=>(t-t0)/(t1-t0||1)*(W-20)+10;
 const Y=v=>H-14-(v-lo)/(hi-lo)*(H-28);
 g.strokeStyle='#3a4454';g.beginPath();g.moveTo(10,Y(0));g.lineTo(W-10,Y(0));g.stroke();
 const line=(idx,color,w)=>{g.strokeStyle=color;g.lineWidth=w;g.beginPath();
  h.forEach((s,i)=>{i?g.lineTo(X(s[0]),Y(s[idx])):g.moveTo(X(s[0]),Y(s[idx]))});g.stroke()};
 line(3,'#b07cd8',2.4);line(2,'#2E86DE',1.4);line(1,'#3ddc84',1.4);g.lineWidth=1;
}
setInterval(refresh,3000);refresh();
</script></body></html>"""


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
                self._send(200, json.dumps(stats.snapshot))
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
