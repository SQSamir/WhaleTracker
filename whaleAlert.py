# Python 3.10+
# Binance-style Telegram messages for copy-trading (BTC/ETH, HUGE/MEGA only)
# – No trading via API; only WebSocket read + Telegram notify
# – Works even if TELEGRAM_TOKEN/CHAT_ID are missing (logs to console)

import asyncio, json, logging, os, ssl, time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple, Any
import aiohttp, websockets

# ---------- safe env ----------
def getenv_bool(k:str,d:bool)->bool:
    v=os.getenv(k); 
    return d if v is None else str(v).strip().lower() in ("1","true","yes","y","on")
def getenv_float(k:str,d:float)->float:
    try: return float(os.getenv(k,str(d)))
    except: return d
def getenv_int(k:str,d:int)->int:
    try: return int(os.getenv(k,str(d)))
    except: return d

# ---------- logging ----------
class ColorFormatter(logging.Formatter):
    GREY="\x1b[38;21m"; GREEN="\x1b[32;21m"; YELLOW="\x1b[33;21m"; RED="\x1b[31;21m"; BRED="\x1b[31;1m"; RESET="\x1b[0m"
    BASE="%(asctime)s - %(levelname)s - [%(name)s] %(message)s"
    MAP={logging.DEBUG:GREY+BASE+RESET, logging.INFO:GREEN+BASE+RESET,
         logging.WARNING:YELLOW+BASE+RESET, logging.ERROR:RED+BASE+RESET, logging.CRITICAL:BRED+BASE+RESET}
    def format(self, record): return logging.Formatter(self.MAP.get(record.levelno,self.BASE)).format(record)

logger=logging.getLogger("WhaleCopySignalTG")
logger.setLevel(logging.INFO)
_ch=logging.StreamHandler(); _ch.setLevel(logging.INFO); _ch.setFormatter(ColorFormatter()); logger.addHandler(_ch)
_fh=logging.FileHandler("whale_copy_signal_tg.log",encoding="utf-8"); _fh.setLevel(logging.INFO)
_fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - [%(name)s] %(message)s"))
logger.addHandler(_fh)

# ---------- config ----------
@dataclass
class SymCfg:
    big_trade: float; huge_trade: float; mega_trade: float; min_liq: float
    tick: float;  # price tickSize (visual rounding)
    stop_offset_ticks_long: int = 10   # Stop Price below Entry for BUY stop-limit
    stop_offset_ticks_short: int = 10  # Stop Price above Entry for SELL stop-limit
    limit_offset_ticks: int = 0        # optional extra ticks beyond entry (usually 0 or 1)

@dataclass
class Config:
    TELEGRAM_TOKEN: Optional[str] = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN"))
    TELEGRAM_CHAT_ID: Optional[str] = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID"))
    SYMBOLS: Dict[str, SymCfg] = field(default_factory=lambda:{
        "BTCUSDT": SymCfg(big_trade=300_000, huge_trade=2_000_000, mega_trade=10_000_000,
                          min_liq=200_000, tick=0.1, stop_offset_ticks_long=10, stop_offset_ticks_short=10, limit_offset_ticks=0),
        "ETHUSDT": SymCfg(big_trade=150_000, huge_trade=1_000_000, mega_trade=5_000_000,
                          min_liq=100_000, tick=0.01, stop_offset_ticks_long=100, stop_offset_ticks_short=100, limit_offset_ticks=0),
    })
    # Levels (ATR-first, else percent)
    USE_ATR: bool = field(default_factory=lambda: getenv_bool("USE_ATR", True))
    ATR_PERIOD: int = field(default_factory=lambda: getenv_int("ATR_PERIOD", 14))
    ATR_INTERVAL: str = field(default_factory=lambda: os.getenv("ATR_INTERVAL","1m"))
    ENTRY_OFFSET_ATR: float = field(default_factory=lambda: getenv_float("ENTRY_OFFSET_ATR",0.10))
    SL_ATR: float = field(default_factory=lambda: getenv_float("SL_ATR",1.50))
    TP1_ATR: float = field(default_factory=lambda: getenv_float("TP1_ATR",1.00))
    TP2_ATR: float = field(default_factory=lambda: getenv_float("TP2_ATR",2.00))
    TP3_ATR: float = field(default_factory=lambda: getenv_float("TP3_ATR",3.00))
    SL_PCT: float = field(default_factory=lambda: getenv_float("SL_PCT",0.5))
    TP1_PCT: float = field(default_factory=lambda: getenv_float("TP1_PCT",0.5))
    TP2_PCT: float = field(default_factory=lambda: getenv_float("TP2_PCT",1.0))
    TP3_PCT: float = field(default_factory=lambda: getenv_float("TP3_PCT",1.5))
    OI_CONFIRM: bool = field(default_factory=lambda: getenv_bool("OI_CONFIRM", True))
    OI_WINDOW_MIN: int = field(default_factory=lambda: getenv_int("OI_WINDOW_MIN", 30))
    OI_MIN_ABS_PCT: float = field(default_factory=lambda: getenv_float("OI_MIN_ABS_PCT", 0.2))
    COOLDOWN_SEC: int = field(default_factory=lambda: getenv_int("COOLDOWN_SEC", 120))
    MAX_SIGNALS_PER_HOUR: int = field(default_factory=lambda: getenv_int("MAX_SIGNALS_PER_HOUR", 8))

# ---------- helpers ----------
def fmt_usd(x:float)->str:
    ax=abs(x)
    if ax>=1e12: return f"{x/1e12:.2f}T"
    if ax>=1e9:  return f"{x/1e9:.2f}B"
    if ax>=1e6:  return f"{x/1e6:.2f}M"
    if ax>=1e3:  return f"{x/1e3:.2f}K"
    return f"{x:,.0f}"

def fmt_price_by_tick(x:float, tick:float)->str:
    # match Binance chart decimals by tick
    dec = max(0, len(str(tick).split(".")[1]) if "." in str(tick) else 0)
    fmt = f"{{:,.{dec}f}}" if dec>0 else "{:,.0f}"
    return fmt.format(x)

def pct(a:float,b:float)->str:
    try: return f"{(a-b)/b*100:+.2f}%"
    except ZeroDivisionError: return "N/A"

# ---------- core ----------
class WhaleCopySignalTG:
    def __init__(self, cfg:Config):
        self.cfg=cfg
        self.session: Optional[aiohttp.ClientSession]=None
        self._rate_sem=asyncio.Semaphore(12)
        self._last: Dict[str,float]={s:0.0 for s in cfg.SYMBOLS}
        self._cnt: Dict[str,int]={s:0 for s in cfg.SYMBOLS}
        self._win=time.time()

    # Telegram
    async def tg_send(self, text:str):
        if not (self.cfg.TELEGRAM_TOKEN and self.cfg.TELEGRAM_CHAT_ID) or not self.session:
            logger.info("[TG SKIP]\n"+text)
            return
        url=f"https://api.telegram.org/bot{self.cfg.TELEGRAM_TOKEN}/sendMessage"
        payload={"chat_id":self.cfg.TELEGRAM_CHAT_ID,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}
        try:
            async with self.session.post(url,json=payload,timeout=15) as r:
                if r.status!=200:
                    logger.error(f"Telegram {r.status}: {await r.text()}")
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    # HTTP
    async def _public(self, path:str, params:Dict[str,str]|None=None)->Tuple[int,Any]:
        url=f"https://fapi.binance.com{path}"
        async with self._rate_sem:
            try:
                async with self.session.get(url,params=params or {},timeout=15) as r:
                    r.raise_for_status()
                    return r.status, await r.json(content_type=None)
            except Exception as e:
                return 500, {"msg": str(e)}

    async def get_klines(self,sym:str, interval:str, limit:int)->List[list]:
        st,data=await self._public("/fapi/v1/klines",{"symbol":sym,"interval":interval,"limit":str(limit)})
        return data if st==200 and isinstance(data,list) else []

    async def get_oi_change_pct(self,sym:str)->float:
        lim=max(2, min(12, self.cfg.OI_WINDOW_MIN//5 + 1))
        st,data=await self._public("/futures/data/openInterestHist",{"symbol":sym,"period":"5m","limit":str(lim)})
        if st!=200 or not isinstance(data,list) or len(data)<2: return 0.0
        try:
            last=float(data[-1]["sumOpenInterest"]); prev=float(data[0]["sumOpenInterest"])
            return (last-prev)/prev*100.0 if prev else 0.0
        except: return 0.0

    # indicators
    def calc_atr(self,kl:List[list], period:int)->float:
        if len(kl)<period+1: return 0.0
        trs=[]; pc=float(kl[0][4])
        for k in kl[1:]:
            h,l,c=float(k[2]),float(k[3]),float(k[4])
            trs.append(max(h-l, abs(h-pc), abs(l-pc))); pc=c
        return sum(trs[-period:])/period if len(trs)>=period else 0.0

    # levels
    def levels_atr(self, side:str, price:float, atr:float)->Tuple[float,float,float,float,float]:
        c=self.cfg
        if side=="BUY":
            entry=price + c.ENTRY_OFFSET_ATR*atr
            sl   =entry - c.SL_ATR*atr
            tp1  =entry + c.TP1_ATR*atr
            tp2  =entry + c.TP2_ATR*atr
            tp3  =entry + c.TP3_ATR*atr
        else:
            entry=price - c.ENTRY_OFFSET_ATR*atr
            sl   =entry + c.SL_ATR*atr
            tp1  =entry - c.TP1_ATR*atr
            tp2  =entry - c.TP2_ATR*atr
            tp3  =entry - c.TP3_ATR*atr
        return entry,sl,tp1,tp2,tp3

    def levels_pct(self, side:str, price:float)->Tuple[float,float,float,float,float]:
        c=self.cfg
        if side=="BUY":
            entry=price; sl=price*(1-c.SL_PCT/100.0)
            tp1=price*(1+c.TP1_PCT/100.0); tp2=price*(1+c.TP2_PCT/100.0); tp3=price*(1+c.TP3_PCT/100.0)
        else:
            entry=price; sl=price*(1+c.SL_PCT/100.0)
            tp1=price*(1-c.TP1_PCT/100.0); tp2=price*(1-c.TP2_PCT/100.0); tp3=price*(1-c.TP3_PCT/100.0)
        return entry,sl,tp1,tp2,tp3

    # ----- Binance-style message -----
    def _stop_limit_tuple(self, symbol:str, side:str, entry:float)->Tuple[float,float,str]:
        sc=self.cfg.SYMBOLS[symbol]
        if side=="BUY":
            stop = entry - sc.stop_offset_ticks_long*sc.tick
            limit = entry + sc.limit_offset_ticks*sc.tick
            cond = f"Mark Price ≥ {fmt_price_by_tick(stop, sc.tick)}"
        else:
            stop = entry + sc.stop_offset_ticks_short*sc.tick
            limit = entry - sc.limit_offset_ticks*sc.tick
            cond = f"Mark Price ≤ {fmt_price_by_tick(stop, sc.tick)}"
        return stop, limit, cond

    def build_binance_message(self, *, symbol:str, side:str, price:float, notional:float,
                              atr:Optional[float], oi_pct:float,
                              entry:float, sl:float, tp1:float, tp2:float, tp3:float,
                              aggressor_price:Optional[float]=None, tier:str="HUGE")->str:
        sc=self.cfg.SYMBOLS[symbol]
        tick=sc.tick
        f=lambda v: fmt_price_by_tick(v,tick)

        stop_price, limit_price, cond = self._stop_limit_tuple(symbol, side, entry)

        unit="ATR" if (atr is not None and atr>0) else "%"
        tp1_lab = f"+{self.cfg.TP1_ATR} ATR" if unit=="ATR" else f"+{self.cfg.TP1_PCT}%"
        tp2_lab = f"+{self.cfg.TP2_ATR} ATR" if unit=="ATR" else f"+{self.cfg.TP2_PCT}%"
        tp3_lab = f"+{self.cfg.TP3_ATR} ATR" if unit=="ATR" else f"+{self.cfg.TP3_PCT}%"

        emoji = "🟢" if side=="BUY" else "🔴"
        whale_emoji = "🛸" if tier=="MEGA" else "🐋🚨"

        return f"""
{whale_emoji} <b>WHALE {tier} {'LONG' if side=='BUY' else 'SHORT'} — COPY LEVELS</b>

<b>Symbol:</b> {symbol}
• Whale Notional: <code>{fmt_usd(notional)}</code>
• Aggressor: <code>{'BUY' if side=='BUY' else 'SELL'}</code>{f" @ <code>${f(aggressor_price)}</code>" if aggressor_price else ""}
• ATR({self.cfg.ATR_PERIOD} {self.cfg.ATR_INTERVAL}): <code>{('$'+f(atr)) if (atr and atr>0) else 'N/A'}</code>
• Entry Offset: <code>{self.cfg.ENTRY_OFFSET_ATR:.2f} {unit}</code>
• OI Δ ~{self.cfg.OI_WINDOW_MIN}m: <code>{oi_pct:+.2f}%</code>

<b>🎯 Trading Plan (manual copy):</b>
• Entry: <code>${f(entry)}</code> {emoji}
• SL: <code>${f(sl)}</code> ({'-' if side=='BUY' else '+'}{self.cfg.SL_ATR if unit=='ATR' else self.cfg.SL_PCT} {unit})
• TP1: <code>${f(tp1)}</code> ({tp1_lab} / {pct(tp1, entry)})
• TP2: <code>${f(tp2)}</code> ({tp2_lab} / {pct(tp2, entry)})
• TP3: <code>${f(tp3)}</code> ({tp3_lab} / {pct(tp3, entry)})

<b>🧩 Binance Stop-Limit Setup:</b>
• Type: <code>Stop Limit</code> | Side: <code>{'Buy/Long' if side=='BUY' else 'Sell/Short'}</code>
• Stop Price (trigger): <code>{f(stop_price)}</code>
• Price (limit): <code>{f(limit_price)}</code>
• Condition: <code>{cond}</code>

<i>Xüsusi Qeyd: Bu siqnallar yalnız məlumat üçündür. Riskini özün idarə et.</i>
""".strip()

    # ----- WS -----
    async def websocket_loop(self):
        streams=[f"{s.lower()}@aggTrade" for s in self.cfg.SYMBOLS]+["!forceOrder@arr"]
        url=f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
        ssl_ctx=ssl.create_default_context()
        while True:
            try:
                async with websockets.connect(url, ssl=ssl_ctx, ping_interval=20, ping_timeout=10, close_timeout=5) as ws:
                    logger.info("🔗 WebSocket connected")
                    async for msg in ws:
                        try:
                            data=json.loads(msg); stream=data.get("stream",""); payload=data.get("data",{})
                            if stream.endswith("@aggTrade"): await self._on_trade(payload)
                            elif stream=="!forceOrder@arr": await self._on_liq(payload)
                        except Exception as e:
                            logger.error(f"WS message error: {e}")
            except Exception as e:
                logger.error(f"WS reconnect in 10s: {e}"); await asyncio.sleep(10)

    async def _on_liq(self, pay):
        # optional toast
        arr = pay if isinstance(pay,list) else [pay]
        for od in arr:
            o=od.get("o",{}); s=o.get("s",""); 
            if s not in self.cfg.SYMBOLS: continue
            try:
                q=float(o.get("q","0") or 0.0); p=float(o.get("p","0") or 0.0); side=o.get("S")
                notional=q*p
            except: continue
            if notional>=self.cfg.SYMBOLS[s].min_liq:
                await self.tg_send(f"💥 <b>{'SHORT' if side=='BUY' else 'LONG'} Liquidation</b> {s} | <code>{fmt_usd(notional)}</code> @ <code>${fmt_price_by_tick(p,self.cfg.SYMBOLS[s].tick)}</code>")

    # trade → detect HUGE/MEGA and send binance-style message
    async def _on_trade(self, t:dict):
        sym=t.get("s",""); 
        if sym not in self.cfg.SYMBOLS: return
        sc=self.cfg.SYMBOLS[sym]
        try:
            price=float(t.get("p","0") or 0.0); qty=float(t.get("q","0") or 0.0)
        except: return
        notional=price*qty
        is_buyer_maker=t.get("m",False)  # True → SELL aggression
        side="BUY" if not is_buyer_maker else "SELL"
        tier="MEGA" if notional>=sc.mega_trade else ("HUGE" if notional>=sc.huge_trade else None)
        if not tier: return

        # cooldown
        now=time.time()
        if now-self._win>3600: self._win=now; self._cnt={s:0 for s in self._cnt}
        if now-self._last[sym]<self.cfg.COOLDOWN_SEC or self._cnt[sym]>=self.cfg.MAX_SIGNALS_PER_HOUR:
            return

        # confirmations
        oi=await self.get_oi_change_pct(sym) if self.cfg.OI_CONFIRM else 0.0
        if self.cfg.OI_CONFIRM:
            if side=="BUY" and oi< self.cfg.OI_MIN_ABS_PCT: return
            if side=="SELL" and oi>-self.cfg.OI_MIN_ABS_PCT: return

        # levels
        atr=None; entry=price; sl=tp1=tp2=tp3=0.0
        if self.cfg.USE_ATR:
            kl=await self.get_klines(sym,self.cfg.ATR_INTERVAL, max(200,self.cfg.ATR_PERIOD+5))
            atr_val=self.calc_atr(kl,self.cfg.ATR_PERIOD)
            if atr_val>0:
                atr=atr_val
                entry,sl,tp1,tp2,tp3=self.levels_atr(side, price, atr_val)
            else:
                entry,sl,tp1,tp2,tp3=self.levels_pct(side, price)
        else:
            entry,sl,tp1,tp2,tp3=self.levels_pct(side, price)

        msg=self.build_binance_message(
            symbol=sym, side=side, price=price, notional=notional, atr=atr, oi_pct=oi,
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, aggressor_price=price, tier=tier
        )
        await self.tg_send(msg)
        self._last[sym]=now; self._cnt[sym]+=1

    async def run(self):
        ssl_ctx=ssl.create_default_context()
        async with aiohttp.ClientSession(trust_env=True, connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as s:
            self.session=s
            await self.tg_send("🤖 <b>Whale Copy-Signal Bot</b> — Binance-style messages enabled.")
            await self.websocket_loop()

# ---------- entry ----------
async def main():
    bot=WhaleCopySignalTG(Config())
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("🛑 Stopped")
    except Exception as e:
        logger.error(f"Fatal: {e}")

if __name__=="__main__":
    # Windows event loop fix (optional)
    if os.name=="nt":
        try:
            import asyncio as _a, sys
            if sys.version_info>=(3,8): _a.set_event_loop_policy(_a.WindowsSelectorEventLoopPolicy())
        except Exception: pass
    asyncio.run(main())
