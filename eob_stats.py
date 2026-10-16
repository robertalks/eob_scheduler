"""Thermostat statistics + small web dashboard, run inside eob_scheduler.py.

Every `stats.poll_interval_seconds` the collector asks the thermostat for
its status over the scheduler's existing MQTT connection (the same
request_status() the scheduler already uses -- see eob_lib.py) and stores
measured temperature, target, relay state and the tariff (from the same
TariffSchedule the scheduler drives the device with) in SQLite.

The dashboard is served on http://<host>:<port>/ with JSON under /api/:
  /api/status            latest reading, tariff now + next change
  /api/history?hours=24  readings for the chart
  /api/daily?days=14     heater-on minutes per day, split low/high tariff

Config (config.yaml):
  stats:
    enabled: true
    db_file: "eob_stats.db"
    poll_interval_seconds: 60
    web:
      host: "0.0.0.0"
      port: 8080

Only the Python standard library is used here (sqlite3, http.server).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


class StatsStore:
    def __init__(self, path: str, schedule, interval: int = 60):
        self.schedule, self.interval = schedule, interval
        self.tz = schedule.tz
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS readings(
              ts INTEGER PRIMARY KEY, measured REAL, target REAL,
              relay INTEGER, off_mode INTEGER, low_tariff INTEGER);
            CREATE TABLE IF NOT EXISTS switches(ts INTEGER PRIMARY KEY, relay INTEGER);
        """)
        row = self._query("SELECT relay FROM readings ORDER BY ts DESC LIMIT 1")
        self._last_relay = row[0][0] if row else None
        self._next_change = (0, None)  # (computed_at, value) cache

    def _query(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def add(self, status: dict, ts: int | None = None) -> bool:
        """Store one parsed status. Returns True if the relay switched."""
        ts = int(ts or time.time())
        relay = int(bool(status["relay_on"]))
        low = int(self.schedule.is_low_tariff(datetime.fromtimestamp(ts, self.tz)))
        switched = self._last_relay is not None and relay != self._last_relay
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO readings VALUES(?,?,?,?,?,?)",
                (ts, status["measured_temp_c"], status.get("target_temp_c"), relay,
                 int(bool(status.get("off_mode"))), low))
            if self._last_relay is None or switched:
                self._db.execute("INSERT OR REPLACE INTO switches VALUES(?,?)", (ts, relay))
            self._db.commit()
        self._last_relay = relay
        return switched

    # ---- API data
    def _next_tariff_change(self):
        at, val = self._next_change
        if time.time() - at > 60:
            nxt = self.schedule.next_change()
            val = None if nxt is None else {
                "ts": int(nxt.timestamp()), "to_low": self.schedule.is_low_tariff(nxt)}
            self._next_change = (time.time(), val)
        return val

    def status(self) -> dict:
        r = self._query("SELECT ts,measured,target,relay,off_mode FROM readings "
                        "ORDER BY ts DESC LIMIT 1")
        out = {"ok": bool(r), "low_tariff": self.schedule.is_low_tariff(),
               "next_tariff_change": self._next_tariff_change()}
        if r:
            ts, m, t, rel, off = r[0]
            sw = self._query("SELECT ts FROM switches ORDER BY ts DESC LIMIT 1")
            out.update(ts=ts, age=int(time.time()) - ts, measured=m, desired=t,
                       relay=bool(rel), mode="off" if off else "on",
                       since=sw[0][0] if sw else None)
        return out

    def history(self, hours: float) -> dict:
        since = int(time.time() - hours * 3600)
        rows = self._query("SELECT ts,measured,target,relay,low_tariff FROM readings "
                           "WHERE ts>=? ORDER BY ts", (since,))
        return {"since": since, "interval": self.interval, "rows": [list(r) for r in rows]}

    def daily(self, days: int) -> list:
        """Heater-on minutes per local day, split by tariff. Each reading
        counts until the next one, capped at 3 poll intervals so gaps
        (scheduler down, broker unreachable) don't count as heating."""
        today = datetime.now(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=days - 1)
        rows = self._query("SELECT ts,relay,low_tariff FROM readings WHERE ts>=? "
                           "ORDER BY ts", (int(start.timestamp()) - 3 * self.interval,))
        out = {}
        for d in range(days):
            k = (start + timedelta(days=d)).strftime("%Y-%m-%d")
            out[k] = {"day": k, "low": 0.0, "high": 0.0, "unknown": 0.0}
        cap = 3 * self.interval
        for (t0, rel, low), (t1, _, _) in zip(rows, rows[1:]):
            if not rel:
                continue
            t1 = min(t1, t0 + cap)
            while t0 < t1:  # split across local midnight
                day = datetime.fromtimestamp(t0, self.tz)
                midnight = (day.replace(hour=0, minute=0, second=0, microsecond=0)
                            + timedelta(days=1))
                seg = min(t1, int(midnight.timestamp())) - t0
                k = day.strftime("%Y-%m-%d")
                if k in out:
                    out[k]["unknown" if low is None else ("low" if low else "high")] += seg / 60
                t0 += seg
        return [{k: (round(v) if isinstance(v, float) else v) for k, v in d.items()}
                for d in out.values()]


class StatsCollector:
    """Background thread polling client.get_status() into a StatsStore."""

    def __init__(self, client, store: StatsStore, interval: int = 60):
        self.client, self.store, self.interval = client, store, interval
        self._stop = threading.Event()

    def poll_once(self) -> None:
        try:
            status = self.client.get_status()
        except NotImplementedError:
            return
        except Exception:
            logger.exception("stats: status request failed")
            return
        if "measured_temp_c" not in status or "relay_on" not in status:
            logger.warning("stats: no usable status this round (%s)", status or "timeout")
            return
        if self.store.add(status):
            logger.info("Heater relay switched %s (measured %.1fC, target %.1fC)",
                        "ON" if status["relay_on"] else "OFF",
                        status["measured_temp_c"], status.get("target_temp_c") or 0)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval)

    def start(self) -> None:
        threading.Thread(target=self._run, name="eob-stats", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EOB water heater</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--mute:#6f6d68;--line:#e3e1dc;
--temp:#c2410c;--target:#6f6d68;--on:rgba(194,65,12,.16);--nt:rgba(37,99,235,.09);
--low:#2563eb;--high:#c2410c;--unk:#a8a29e}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1e1c;--ink:#ecebe7;
--mute:#9b9890;--line:#33312e;--temp:#fb923c;--target:#9b9890;--on:rgba(251,146,60,.18);
--nt:rgba(96,165,250,.12);--low:#60a5fa;--high:#fb923c;--unk:#6f6d68}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:18px;margin:0 0 14px;font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile,.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.lab{color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.val{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}
.sub{color:var(--mute);font-size:13px}
.on{color:var(--temp)}
.card{margin-top:14px}.row{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.seg button{border:1px solid var(--line);background:none;color:var(--ink);padding:4px 10px;border-radius:6px;cursor:pointer;font:inherit;font-size:13px}
.seg button.act{background:var(--ink);color:var(--bg)}
svg{width:100%;display:block}svg text{fill:var(--mute);font-size:11px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--mute);margin-top:6px}
.sw{display:inline-block;width:12px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.stale{color:#b91c1c}
</style></head><body><main>
<h1>Water heater · BT725</h1>
<div class="tiles">
 <div class="tile"><div class="lab">Measured</div><div class="val" id="m">–</div><div class="sub" id="age"></div></div>
 <div class="tile"><div class="lab">Target</div><div class="val" id="d">–</div><div class="sub" id="mode"></div></div>
 <div class="tile"><div class="lab">Heater</div><div class="val" id="r">–</div><div class="sub" id="since"></div></div>
 <div class="tile"><div class="lab">Tariff</div><div class="val" id="t">–</div><div class="sub" id="next"></div></div>
</div>
<div class="card"><div class="row"><b>Temperature</b>
 <div class="seg" id="rng"><button data-h="6">6 h</button><button data-h="24" class="act">24 h</button><button data-h="168">7 d</button></div></div>
 <svg id="ch" height="260" role="img" aria-label="Temperature chart"></svg>
 <div class="legend"><span><i class="sw" style="background:var(--temp)"></i>measured</span>
 <span><i class="sw" style="background:var(--target)"></i>target (hidden while forced off)</span>
 <span><i class="sw" style="background:var(--on)"></i>heater on</span>
 <span><i class="sw" style="background:var(--nt)"></i>low tariff</span></div></div>
<div class="card"><div class="row"><b>Heater on-time per day</b><span class="sub" id="tot"></span></div>
 <svg id="bars" height="200" role="img" aria-label="Daily heater on-time"></svg>
 <div class="legend"><span><i class="sw" style="background:var(--low)"></i>low tariff</span>
 <span><i class="sw" style="background:var(--high)"></i>high tariff</span>
 <span><i class="sw" style="background:var(--unk)"></i>tariff unknown</span></div></div>
</main><script>
const $=id=>document.getElementById(id),NS="http://www.w3.org/2000/svg";
let H=24;try{H=+localStorage.getItem("h")||24}catch(e){}
const fmtT=s=>new Date(s*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",hourCycle:"h23"});
const nice=r=>{const m=10**Math.floor(Math.log10(r)),f=r/m;return (f<=1?1:f<=2?2:f<=5?5:10)*m};
const dur=s=>{s=Math.max(0,s|0);const h=s/3600|0,m=(s%3600)/60|0;return h?`${h} h ${m} min`:`${m} min`};
function el(t,a,p){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);p&&p.appendChild(e);return e}
async function status(){const s=await (await fetch("api/status")).json();
 $("t").textContent=s.low_tariff==null?"–":(s.low_tariff?"low":"high");
 $("next").textContent=s.next_tariff_change?(s.next_tariff_change.to_low?"low":"high")+" from "+fmtT(s.next_tariff_change.ts):"";
 if(!s.ok){$("age").textContent="no data yet";return}
 $("m").textContent=s.measured.toFixed(1)+" °C";$("d").textContent=s.desired?s.desired.toFixed(1)+" °C":"off";
 $("mode").textContent=s.mode==="off"?"forced off":"";$("age").textContent="updated "+dur(s.age)+" ago";
 $("age").className="sub"+(s.age>300?" stale":"");
 $("r").textContent=s.relay?"ON":"off";$("r").className="val"+(s.relay?" on":"");
 $("since").textContent=s.since?"for "+dur(Date.now()/1000-s.since):"";
}
async function chart(){const d=await (await fetch("api/history?hours="+H)).json(),svg=$("ch");svg.innerHTML="";
 const W=svg.clientWidth||900,Ht=260,L=34,R=8,T=8,B=22,now=Date.now()/1000,t0=now-H*3600,rows=d.rows,gap=3*d.interval;
 if(!rows.length){el("text",{x:W/2,y:Ht/2,"text-anchor":"middle"},svg).textContent="no readings in this range";return}
 let lo=Infinity,hi=-Infinity;rows.forEach(r=>{lo=Math.min(lo,r[1]);hi=Math.max(hi,r[1]);if(r[2]){lo=Math.min(lo,r[2]);hi=Math.max(hi,r[2])}});
 const st=nice((hi-lo+2)/5);lo=Math.floor((lo-1)/st)*st;hi=Math.ceil((hi+1)/st)*st;
 const x=t=>L+(t-t0)/(now-t0)*(W-L-R),y=v=>T+(hi-v)/(hi-lo)*(Ht-T-B);
 const band=(i,col)=>{for(let k=0;k<rows.length;k++){if(!rows[k][i])continue;let j=k;
   while(j+1<rows.length&&rows[j+1][i]&&rows[j+1][0]-rows[j][0]<=gap)j++;
   const e=Math.min(rows[j][0]+d.interval,now);el("rect",{x:x(rows[k][0]),y:T,width:Math.max(1,x(e)-x(rows[k][0])),height:Ht-T-B,fill:col},svg);k=j}};
 band(4,"var(--nt)");band(3,"var(--on)");
 for(let v=lo;v<=hi;v+=st){el("line",{x1:L,x2:W-R,y1:y(v),y2:y(v),stroke:"var(--line)"},svg);
   el("text",{x:L-5,y:y(v)+4,"text-anchor":"end"},svg).textContent=v}
 const step=H<=6?3600:H<=24?(W<600?6:3)*3600:86400;let tt=Math.ceil(t0/step)*step;
 for(;tt<now;tt+=step){const lab=step>=86400?new Date(tt*1000).toLocaleDateString([], {weekday:"short",day:"numeric"}):fmtT(tt);
   el("text",{x:x(tt),y:Ht-6,"text-anchor":"middle"},svg).textContent=lab}
 const path=i=>{let p="",prev=null;rows.forEach(r=>{if(i===2&&!r[2]){prev=null;return}p+=(prev===null||r[0]-prev>gap?"M":"L")+x(r[0]).toFixed(1)+" "+y(r[i]).toFixed(1);prev=r[0]});return p};
 el("path",{d:path(2),fill:"none",stroke:"var(--target)","stroke-width":1.5,"stroke-dasharray":"5 4","vector-effect":"non-scaling-stroke"},svg);
 el("path",{d:path(1),fill:"none",stroke:"var(--temp)","stroke-width":2,"vector-effect":"non-scaling-stroke"},svg)}
async function bars(){const d=await (await fetch("api/daily?days=14")).json(),svg=$("bars");svg.innerHTML="";
 const W=svg.clientWidth||900,Ht=200,L=34,R=8,T=8,B=22,n=d.length,mx=Math.max(60,...d.map(v=>v.low+v.high+v.unknown));
 const top=Math.ceil(mx/60)*60,bw=(W-L-R)/n,y=v=>T+(1-v/top)*(Ht-T-B);
 for(let h=0;h<=top/60;h+=Math.max(1,Math.ceil(top/60/4))){el("line",{x1:L,x2:W-R,y1:y(h*60),y2:y(h*60),stroke:"var(--line)"},svg);
   el("text",{x:L-5,y:y(h*60)+4,"text-anchor":"end"},svg).textContent=h+"h"}
 let tl=0,th=0;d.forEach((v,i)=>{let base=0;tl+=v.low;th+=v.high;
  [["low","var(--low)"],["high","var(--high)"],["unknown","var(--unk)"]].forEach(([k,c])=>{if(!v[k])return;
   const r=el("rect",{x:L+i*bw+bw*.18,width:bw*.64,y:y(base+v[k]),height:y(base)-y(base+v[k]),fill:c},svg);
   el("title",{},r).textContent=`${v.day}: ${k} ${dur(v[k]*60)}`;base+=v[k]});
  if(bw>=34||(n-1-i)%2==0)el("text",{x:L+i*bw+bw/2,y:Ht-6,"text-anchor":"middle"},svg).textContent=+v.day.slice(8)+"."+(+v.day.slice(5,7))+"."});
 $("tot").textContent=`14 days: ${dur((tl+th)*60)} on`+(tl+th?`, ${Math.round(100*tl/((tl+th)||1))}% in low tariff`:"")}
$("rng").querySelectorAll("button").forEach(b=>{b.classList.toggle("act",+b.dataset.h===H);b.onclick=()=>{H=+b.dataset.h;
 try{localStorage.setItem("h",H)}catch(e){}$("rng").querySelectorAll("button").forEach(o=>o.classList.toggle("act",o===b));chart()}});
function all(){status().catch(()=>{});chart().catch(()=>{});bars().catch(()=>{})}
all();setInterval(all,60000);
let rz;addEventListener("resize",()=>{clearTimeout(rz);rz=setTimeout(()=>{chart();bars()},200)});
</script></body></html>"""


def _make_handler(store: StatsStore):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, PAGE, "text/html; charset=utf-8")
                if u.path == "/api/status":
                    data = store.status()
                elif u.path == "/api/history":
                    data = store.history(min(float(q.get("hours", ["24"])[0]), 24 * 31))
                elif u.path == "/api/daily":
                    data = store.daily(max(1, min(int(q.get("days", ["14"])[0]), 366)))
                else:
                    return self._send(404, "not found", "text/plain")
                self._send(200, json.dumps(data), "application/json")
            except Exception as e:
                logger.exception("stats web: %s failed", u.path)
                self._send(500, json.dumps({"error": str(e)}), "application/json")

        def log_message(self, *args):
            pass
    return Handler


def start_web(store: StatsStore, host: str = "0.0.0.0", port: int = 8080):
    srv = ThreadingHTTPServer((host, port), _make_handler(store))
    threading.Thread(target=srv.serve_forever, name="eob-stats-web", daemon=True).start()
    logger.info("Stats dashboard on http://%s:%s/", host, port)
    return srv


def start_from_config(config: dict, client, schedule):
    """Called by eob_scheduler.py. Returns (collector, server) or None."""
    cfg = config.get("stats") or {}
    if not cfg.get("enabled", False):
        return None
    interval = int(cfg.get("poll_interval_seconds", 60))
    store = StatsStore(cfg.get("db_file", "eob_stats.db"), schedule, interval)
    collector = StatsCollector(client, store, interval)
    collector.start()
    web = cfg.get("web") or {}
    srv = None
    if web.get("enabled", True):
        srv = start_web(store, web.get("host", "0.0.0.0"), int(web.get("port", 8080)))
    return collector, srv
