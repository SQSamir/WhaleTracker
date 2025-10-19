# file: whale_copy_signal_telegram.py
# Python 3.10+
# Purpose: BTC/ETH üçün Binance Futures aggTrade + liquidation dinlə,
# HUGE/MEGA whale aggression üçün Entry/SL/TP1-3 planını TELEGRAM-a göndər.
# Heç bir Binance trading API istifadə olunmur. ENV olmasa da default-larla işləyir.

import asyncio
import json
import logging
import os
import ssl
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple

import aiohttp
import websockets

# =========================
# Safe getenv with defaults
# =========================
def getenv_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")

def getenv_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default

def getenv_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default

def getenv_str(key: str, default: str) -> str:
    v = os.getenv(key)
    return v if v is not None and v != "" else default

# =========================
# Logging (Console + File)
# =========================
class ColorFormatter(logging.Formatter):
    GREY = "\x1b[38;21m"; GREEN = "\x1b[32;21m"; YELLOW = "\x1b[33;21m"; RED = "\x1b[31;21m"; BRED = "\x1b[31;1m"; RESET = "\x1b[0m"
    BASE = "%(asctime)s - %(levelname)s - [%(name)s] %(message)s"
    MAP = {
        logging.DEBUG: GREY + BASE + RESET,
        logging.INFO: GREEN + BASE + RESET,
        logging.WARNING: YELLOW + BASE + RESET,
        logging.ERROR: RED + BASE + RESET,
        logging.CRITICAL: BRED + BASE + RESET,
    }
    def format(self, record):
        return logging.Formatter(self.MAP.get(record.levelno, self.BASE)).format(record)

logger = logging.getLogger("WhaleCopySignalTG")
logger.setLevel(logging.INFO)
_ch = logging.StreamHandler(); _ch.setLevel(logging.INFO); _ch.setFormatter(ColorFormatter()); logger.addHandler(_ch)
_filefmt = logging.Formatter("%(asctime)s - %(levelname)s - [%(name)s] %(message)s")
_fh = logging.FileHandler("whale_copy_signal_tg.log", encoding="utf-8"); _fh.setLevel(logging.INFO); _fh.setFormatter(_filefmt); logger.addHandler(_fh)

# =========================
# Config (with safe defaults)
# =========================
@dataclass
class Config:
    # Telegram (optional; if missing → only logs)
    TELEGRAM_TOKEN: Optional[str] = field(default_factory=lambda: os.getenv("TELEGRAM_TOKEN"))
    TELEGRAM_CHAT_ID: Optional[str] = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID"))

    # Symbols (BTC & ETH only) + thresholds in USDT notionals
    SYMBOLS: Dict[str, Dict[str, float]] = field(default_factory=lambda: {
        "BTCUSDT": {"big_trade": 300_000, "huge_trade": 2_000_000, "mega_trade": 10_000_000, "min_liq": 200_000,
                    "tick": 0.1, "step": 0.001},
        "ETHUSDT": {"big_trade": 150_000, "huge_trade": 1_000_000, "mega_trade": 5_000_000,  "min_liq": 100_000,
                    "tick": 0.01, "step": 0.001},
    })

    # Levels engine
    USE_ATR: bool = field(default_factory=lambda: getenv_bool("USE_ATR", True))
    ATR_INTERVAL: str = field(default_factory=lambda: getenv_str("ATR_INTERVAL", "1m"))
    ATR_PERIOD: int = field(default_factory=lambda: getenv_int("ATR_PERIOD", 14))
    ENTRY_OFFSET_ATR: float = field(default_factory=lambda: getenv_float("ENTRY_OFFSET_ATR", 0.10))  # 0.10 * ATR
    SL_ATR: float = field(default_factory=lambda: getenv_float("SL_ATR", 1.50))
    TP1_ATR: float = field(default_factory=lambda: getenv_float("TP1_ATR", 1.00))
    TP2_ATR: float = field(default_factory=lambda: getenv_float("TP2_ATR", 2.00))
    TP3_ATR: float = field(default_factory=lambda: getenv_float("TP3_ATR", 3.00))

    # Percent-mode fallback if ATR disabled/insufficient data
    SL_PCT: float = field(default_factory=lambda: getenv_float("SL_PCT", 0.5))   # 0.5%
    TP1_PCT: float = field(default_factory=lambda: getenv_float("TP1_PCT", 0.5))
    TP2_PCT: float = field(default_factory=lambda: getenv_float("TP2_PCT", 1.0))
    TP3_PCT: float = field(default_factory=lambda: getenv_float("TP3_PCT", 1.5))

    # OI confirmation (optional, public endpoint)
    OI_CONFIRM: bool = field(default_factory=lambda: getenv_bool("OI_CONFIRM", True))
    OI_WINDOW_MIN: int = field(default_factory=lambda: getenv_int("OI_WINDOW_MIN", 30))    # last 6x5m points
    OI_MIN_ABS_PCT: float = field(default_factory=lambda: getenv_float("OI_MIN_ABS_PCT", 0.2))

    # Signal throttling
    COOLDOWN_SEC: int = field(default_factory=lambda: getenv_int("COOLDOWN_SEC", 120))
    MAX_SIGNALS_PER_HOUR: int = field(default_factory=lambda: getenv_int("MAX_SIGNALS_PER_HOUR", 8))

# =========================
# Helpers
# =========================
def fmt_usd(x: float) -> str:
    ax = abs(x)
    if ax >= 1e12: return f"{x/1e12:.2f}T"
    if ax >= 1e9:  return f"{x/1e9:.2f}B"
    if ax >= 1e6:  return f"{x/1e6:.2f}M"
    if ax >= 1e3:  return f"{x/1e3:.2f}K"
    return f"{x:,.0f}"

def fmt_price(x: float) -> str:
    if x >= 1000: return f"{x:,.0f}"
    if x >= 100:  return f"{x:,.1f}"
    if x >= 1:    return f"{x:,.2f}"
    return f"{x:.4f}"

# =========================
# Core
# =========================
class WhaleCopySignalTG:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session: Optional[aiohttp.ClientSession] = None
        self._rate_sem = asyncio.Semaphore(12)
        self._last_signal_ts: Dict[str, float] = {s: 0.0 for s in cfg.SYMBOLS}
        self._signals_in_window: Dict[str, int] = {s: 0 for s in cfg.SYMBOLS}
        self._window_start: float = time.time()

    # ---------- Telegram ----------
    async def tg_send(self, text: str):
        if not (self.cfg.TELEGRAM_TOKEN and self.cfg.TELEGRAM_CHAT_ID) or not self.session:
            logger.info(f"[TELEGRAM SKIP] {text[:200]}")
            return
        url = f"https://api.telegram.org/bot{self.cfg.TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": self.cfg.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}

        # Split long messages defensively
        if len(text) <= 3800:
            parts = [text]
        else:
            parts: List[str] = []
            cur = ""
            for ln in text.split("\n"):
                if len(cur) + len(ln) + 1 > 3700:
                    parts.append(cur); cur = ln
                else:
                    cur = (cur + "\n" + ln) if cur else ln
            if cur: parts.append(cur)

        try:
            for p in parts:
                payload["text"] = p
                async with self.session.post(url, json=payload, timeout=15) as r:
                    if r.status != 200:
                        err = await r.text()
                        logger.error(f"Telegram {r.status}: {err[:300]}")
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    # ---------- Public HTTP ----------
    async def _public(self, path: str, params: Dict[str, str] = None) -> Tuple[int, dict]:
        url = f"https://fapi.binance.com{path}"
        async with self._rate_sem:
            try:
                async with self.session.get(url, params=params or {}, timeout=15) as r:
                    return r.status, await r.json(content_type=None)
            except Exception as e:
                return 500, {"msg": f"HTTP error: {e}"}

    async def get_klines(self, symbol: str, interval: str, limit: int) -> List[list]:
        status, data = await self._public("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": str(limit)})
        if status == 200 and isinstance(data, list):
            return data
        return []

    async def get_oi_change_pct(self, symbol: str) -> float:
        limit = max(2, min(12, self.cfg.OI_WINDOW_MIN // 5 + 1))
        status, data = await self._public("/futures/data/openInterestHist",
                                          {"symbol": symbol, "period": "5m", "limit": str(limit)})
        if status != 200 or not isinstance(data, list) or len(data) < 2:
            return 0.0
        try:
            last = float(data[-1]["sumOpenInterest"])
            prev = float(data[0]["sumOpenInterest"])
            return (last - prev) / prev * 100.0 if prev else 0.0
        except Exception:
            return 0.0

    # ---------- Indicators ----------
    def calc_atr(self, klines: List[list], period: int) -> float:
        if len(klines) < period + 1: return 0.0
        trs: List[float] = []
        prev_close = float(klines[0][4])
        for k in klines[1:]:
            high, low, close = float(k[2]), float(k[3]), float(k[4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr); prev_close = close
        if len(trs) < period: return 0.0
        return sum(trs[-period:]) / period

    def build_levels_atr(self, side: str, price: float, atr: float) -> Tuple[float, float, float, float, float]:
        off = self.cfg.ENTRY_OFFSET_ATR * atr
        if side == "BUY":
            entry = price + off
            sl = entry - self.cfg.SL_ATR * atr
            tp1 = entry + self.cfg.TP1_ATR * atr
            tp2 = entry + self.cfg.TP2_ATR * atr
            tp3 = entry + self.cfg.TP3_ATR * atr
        else:
            entry = price - off
            sl = entry + self.cfg.SL_ATR * atr
            tp1 = entry - self.cfg.TP1_ATR * atr
            tp2 = entry - self.cfg.TP2_ATR * atr
            tp3 = entry - self.cfg.TP3_ATR * atr
        return entry, sl, tp1, tp2, tp3

    def build_levels_pct(self, side: str, price: float) -> Tuple[float, float, float, float, float]:
        if side == "BUY":
            entry = price
            sl = price * (1 - self.cfg.SL_PCT / 100.0)
            tp1 = price * (1 + self.cfg.TP1_PCT / 100.0)
            tp2 = price * (1 + self.cfg.TP2_PCT / 100.0)
            tp3 = price * (1 + self.cfg.TP3_PCT / 100.0)
        else:
            entry = price
            sl = price * (1 + self.cfg.SL_PCT / 100.0)
            tp1 = price * (1 - self.cfg.TP1_PCT / 100.0)
            tp2 = price * (1 - self.cfg.TP2_PCT / 100.0)
            tp3 = price * (1 - self.cfg.TP3_PCT / 100.0)
        return entry, sl, tp1, tp2, tp3

    # ---------- Throttle ----------
    def cooldown_ok(self, symbol: str) -> bool:
        now = time.time()
        if now - self._window_start > 3600:
            self._window_start = now
            self._signals_in_window = {s: 0 for s in self._signals_in_window}
        if now - self._last_signal_ts[symbol] < self.cfg.COOLDOWN_SEC:
            return False
        if self._signals_in_window[symbol] >= self.cfg.MAX_SIGNALS_PER_HOUR:
            return False
        return True

    def touch_signal_counters(self, symbol: str):
        self._last_signal_ts[symbol] = time.time()
        self._signals_in_window[symbol] += 1

    # ---------- Message builders ----------
    async def send_whale_copy_levels(self, symbol: str, side: str, price: float, notional: float,
                                     atr: Optional[float], entry: float, sl: float, tps: Tuple[float, float, float],
                                     oi_pct: float, tier: str):
        direction = "LONG" if side == "BUY" else "SHORT"
        emoji = "🛸" if tier == "MEGA" else "🐋🚨" if tier == "HUGE" else "🐳"
        atr_line = f"• ATR({self.cfg.ATR_PERIOD}): <code>${atr:.2f}</code>\n" if atr and atr > 0 else ""
        text = (
f"""{emoji} <b>WHALE {tier} {direction} — COPY LEVELS</b>

<b>Symbol:</b> {symbol}
• Whale Notional: <code>{fmt_usd(notional)}</code>
• Aggressor: <code>{'BUY' if side=='BUY' else 'SELL'}</code>
• Price: <code>${fmt_price(price)}</code>
{atr_line}• OI Δ ~{self.cfg.OI_WINDOW_MIN}m: <code>{oi_pct:+.2f}%</code>

<b>🎯 Plan (manual copy):</b>
• Entry: <code>${fmt_price(entry)}</code>
• SL: <code>${fmt_price(sl)}</code>
• TP1: <code>${fmt_price(tps[0])}</code> ({'+' if side=='BUY' else '-'}{self.cfg.TP1_ATR if atr and atr>0 else self.cfg.TP1_PCT}{' ATR' if atr and atr>0 else '%'})
• TP2: <code>${fmt_price(tps[1])}</code> ({'+' if side=='BUY' else '-'}{self.cfg.TP2_ATR if atr and atr>0 else self.cfg.TP2_PCT}{' ATR' if atr and atr>0 else '%'})
• TP3: <code>${fmt_price(tps[2])}</code> ({'+' if side=='BUY' else '-'}{self.cfg.TP3_ATR if atr and atr>0 else self.cfg.TP3_PCT}{' ATR' if atr and atr>0 else '%'})

<i>Qeyd:</i> Bu siqnallar yalnız məlumat xarakterlidir. Riskini özün idarə et.
"""
        )
        await self.tg_send(text)

    # ---------- WS Handlers ----------
    async def websocket_handler(self):
        streams = [f"{s.lower()}@aggTrade" for s in self.cfg.SYMBOLS] + ["!forceOrder@arr"]
        url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
        ssl_ctx = ssl.create_default_context()

        while True:
            try:
                async with websockets.connect(url, ssl=ssl_ctx, ping_interval=20, ping_timeout=10) as ws:
                    logger.info("🔗 WebSocket connected")
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                            stream = data.get("stream", "")
                            payload = data.get("data", {})
                            await self._process_ws(stream, payload)
                        except json.JSONDecodeError as e:
                            logger.error(f"WS JSON decode error: {e}")
                        except Exception as e:
                            logger.error(f"WS message error: {e}")
            except Exception as e:
                logger.error(f"WS connection error: {e} — reconnect in 10s")
                await asyncio.sleep(10)

    async def _process_ws(self, stream: str, payload: dict):
        if stream.startswith("!forceOrder"):
            await self._handle_liq(payload)  # optional alert
        elif stream.endswith("@aggTrade"):
            await self._handle_trade(payload)

    async def _handle_liq(self, pay):
        orders = pay if isinstance(pay, list) else [pay]
        for od in orders:
            o = od.get("o", {})
            symbol = o.get("s", "")
            if symbol not in self.cfg.SYMBOLS:
                continue
            side = o.get("S")  # BUY => short liq, SELL => long liq
            qty = float(o.get("q", "0") or 0)
            price = float(o.get("p", "0") or 0)
            notional = qty * price
            if notional >= self.cfg.SYMBOLS[symbol]["min_liq"]:
                logger.info(f"💥 LIQ {symbol} {fmt_usd(notional)} @ ${fmt_price(price)} ({'SHORT' if side=='BUY' else 'LONG'})")
                await self.tg_send(
                    f"💥 <b>{'SHORT' if side=='BUY' else 'LONG'} Liquidation</b>\n"
                    f"{symbol} | <code>{fmt_usd(notional)}</code> @ <code>${fmt_price(price)}</code>"
                )

    async def _handle_trade(self, trade: dict):
        """
        Binance aggTrade:
          s: symbol, p: price, q: qty, m: isBuyerMaker (True => buyer is maker => SELL aggression)
        """
        symbol = trade.get("s", "")
        if symbol not in self.cfg.SYMBOLS:
            return
        try:
            price = float(trade.get("p", "0") or 0.0)
            qty = float(trade.get("q", "0") or 0.0)
        except (TypeError, ValueError):
            return
        notional = price * qty
        is_buyer_maker = trade.get("m", False)  # True => buyer is maker => SELL aggression
        side = "BUY" if not is_buyer_maker else "SELL"  # BUY => LONG plan, SELL => SHORT plan

        th = self.cfg.SYMBOLS[symbol]
        tier = "MEGA" if notional >= th["mega_trade"] else ("HUGE" if notional >= th["huge_trade"] else None)
        if not tier:
            return  # ignore smaller trades

        # Throttle signals
        if not self.cooldown_ok(symbol):
            logger.info(f"[{symbol}] cooldown/hour cap — skip")
            return

        # OI confirmation (optional)
        oi_pct = 0.0
        oi_ok = True
        if self.cfg.OI_CONFIRM:
            oi_pct = await self.get_oi_change_pct(symbol)
            if side == "BUY":
                oi_ok = oi_pct >= self.cfg.OI_MIN_ABS_PCT
            else:
                oi_ok = oi_pct <= -self.cfg.OI_MIN_ABS_PCT
        if not oi_ok:
            await self.tg_send(
                f"⚠️ <b>SKIP (OI uyğunsuzluğu)</b>\n"
                f"{tier} {('LONG' if side=='BUY' else 'SHORT')} | {symbol} | <code>{fmt_usd(notional)}</code> @ <code>${fmt_price(price)}</code>\n"
                f"OI Δ ~{self.cfg.OI_WINDOW_MIN}m: <code>{oi_pct:+.2f}%</code>"
            )
            return

        # Build levels (prefer ATR, else percent fallback)
        entry = price
        sl = tp1 = tp2 = tp3 = 0.0
        atr_val = 0.0
        if self.cfg.USE_ATR:
            kl = await self.get_klines(symbol, self.cfg.ATR_INTERVAL, max(200, self.cfg.ATR_PERIOD + 5))
            atr_val = self.calc_atr(kl, self.cfg.ATR_PERIOD)
            if atr_val > 0:
                entry, sl, tp1, tp2, tp3 = self.build_levels_atr(side, price, atr_val)
            else:
                entry, sl, tp1, tp2, tp3 = self.build_levels_pct(side, price)
        else:
            entry, sl, tp1, tp2, tp3 = self.build_levels_pct(side, price)

        # Send the manual copy plan to Telegram
        await self.send_whale_copy_levels(
            symbol=symbol, side=side, price=price, notional=notional,
            atr=atr_val if atr_val > 0 else None, entry=entry, sl=sl, tps=(tp1, tp2, tp3),
            oi_pct=oi_pct, tier=tier
        )

        self.touch_signal_counters(symbol)

    # ---------- Lifecycle ----------
    async def run(self):
        async with aiohttp.ClientSession(trust_env=True) as session:
            self.session = session
            await self.tg_send(
                "🤖 <b>Whale Copy-Signal Bot</b> (NO trading API)\n"
                f"Watching: BTCUSDT, ETHUSDT | Thresholds: HUGE/MEGA only\n"
                f"Levels: {'ATR' if self.cfg.USE_ATR else 'Percent'}-based | OI Confirm: <code>{self.cfg.OI_CONFIRM}</code>"
            )
            await self.websocket_handler()

# =========================
# Entrypoint
# =========================
async def main():
    cfg = Config()
    bot = WhaleCopySignalTG(cfg)
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("🛑 Stopped by user")
    except Exception as e:
        logger.error(f"💥 Fatal error: {e}")

if __name__ == "__main__":
    # Windows üçün bəzən selector policy lazımdır:
    # import asyncio
    # asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
