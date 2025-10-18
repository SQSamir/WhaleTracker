# file: whale_signal_bot_advanced.py
# Python 3.10+
import asyncio, json, ssl, time, math, os, logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import websockets, aiohttp
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# --- Logging Konfiqurasiyası ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] %(message)s',
    handlers=[
        logging.FileHandler('whale_signals.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('WhaleBot')

# --- Konfiqurasiya (Dəyişdirilməyib) ---
class Config:
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    
    SYMBOLS = ["BTCUSDT", "ETHUSDT", "ADAUSDT", "DOTUSDT", "LINKUSDT"]
    BIG_TRADE_USDT = 500_000
    HUGE_TRADE_USDT = 2_000_000
    MIN_LIQ_NOTIONAL = 100_000
    
    # ATR Settings
    ATR_MINUTES = 1
    ATR_PERIOD = 14
    
    # Risk Management
    ENTRY_OFFSET_ATR = 0.15
    SL_ATR = 1.8
    TP1_ATR, TP2_ATR, TP3_ATR = 1.0, 2.0, 3.0
    RISK_PER_TRADE_PCT = 0.5
    
    # Siqnal Təsdiq Pəncərəsi
    CONFIRM_WINDOW_SEC = 300
    OI_BUMP_PCT = 0.3
    FUNDING_TICK = 0.0001
    
    # Volume Analysis
    VOLUME_SPIKE_MULTIPLIER = 2.5
    
    # Cooldown Mechanism
    COOLDOWN_MINUTES = 30
    
    # VWAP Settings
    VWAP_BARS = 100

class SignalType(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"

@dataclass
class Signal:
    symbol: str
    signal_type: SignalType
    strength: int
    confidence: float
    price: float
    atr: float
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    volume_24h: float
    oi_change: float
    funding: float
    timestamp: datetime
    reasons: List[str]

@dataclass
class MarketState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    
    price: float = 0.0
    atr: float = 0.0
    volume_24h: float = 0.0
    avg_volume_1h: float = 0.0
    oi_change: float = 0.0
    funding: float = 0.0
    vwap: float = 0.0
    
    big_buys: List[Tuple[float, float]] = field(default_factory=list)
    big_sells: List[Tuple[float, float]] = field(default_factory=list)
    long_liq: List[Tuple[float, float]] = field(default_factory=list)
    short_liq: List[Tuple[float, float]] = field(default_factory=list)
    
    last_signal_time: Optional[datetime] = None

class AdvancedWhaleBot:
    def __init__(self, config: Config):
        self.config = config
        self.states: Dict[str, MarketState] = {symbol: MarketState() for symbol in config.SYMBOLS}
        self.cooldown_until: Dict[str, datetime] = {symbol: datetime.now() for symbol in config.SYMBOLS}
        self.session: Optional[aiohttp.ClientSession] = None
    
    # (Helper functions, initialize, close - dəyişdirilməyib)

    # --- Formatting Helpers ---
    def fmt_usd(self, x: float) -> str:
        if abs(x) >= 1e12: return f"{x/1e12:.2f}T"
        if abs(x) >= 1e9: return f"{x/1e9:.2f}B"
        if abs(x) >= 1e6: return f"{x/1e6:.2f}M"
        if abs(x) >= 1e3: return f"{x/1e3:.2f}K"
        return f"{x:,.0f}"

    def fmt_pct(self, x: float) -> str:
        return f"{x:+.2f}%"
    
    async def initialize(self):
        self.session = aiohttp.ClientSession()
        logger.info("Advanced Whale Bot initialized")
    
    async def close(self):
        if self.session:
            await self.session.close()

    async def tg_send(self, text: str, reply_markup=None):
        if not (self.config.TELEGRAM_TOKEN and self.config.TELEGRAM_CHAT_ID):
            logger.info(f"Telegram: {text[:100]}...")
            return
        
        url = f"https://api.telegram.org/bot{self.config.TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": self.config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        
        try:
            async with self.session.post(url, json=payload, timeout=5) as response:
                if response.status != 200:
                    logger.error(f"Telegram error: {response.status} - {await response.text()}")
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
    
    async def fetch_json(self, url: str, params: dict = None) -> Optional[dict]:
        for attempt in range(3):
            try:
                async with self.session.get(url, params=params, timeout=10) as response:
                    return await response.json()
            except Exception as e:
                logger.warning(f"API request failed (attempt {attempt+1}): {e}")
                await asyncio.sleep(1)
        return None
    
    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 100) -> List:
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        return await self.fetch_json(url, params) or []
    
    async def get_multiple_24h_tickers(self) -> Dict[str, dict]:
        url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        data = await self.fetch_json(url)
        if not data or not isinstance(data, list):
            return {}
        return {item['symbol']: item for item in data if item['symbol'] in self.config.SYMBOLS}
    
    async def get_oi_change_pct(self, symbol: str) -> float:
        url = "https://fapi.binance.com/futures/data/openInterestHist"
        # Son 6 * 5 dəqiqə = 30 dəqiqəlik dəyişiklik üçün
        data = await self.fetch_json(url, {"symbol": symbol, "period": "5m", "limit": 6})
        if not data or len(data) < 2:
            return 0.0
        
        last = float(data[-1]["sumOpenInterest"])
        prev = float(data[0]["sumOpenInterest"]) # 30 dəqiqə əvvəlki dəyər
        return (last - prev) / prev * 100.0 if prev != 0 else 0.0
    
    async def get_funding_rate(self, symbol: str) -> float:
        url = "https://fapi.binance.com/fapi/v1/premiumIndex"
        data = await self.fetch_json(url, {"symbol": symbol})
        return float(data.get("lastFundingRate", 0)) if data else 0.0
    
    # --- Technical Indicators ---
    def calc_atr(self, klines: List) -> float:
        if len(klines) < self.config.ATR_PERIOD:
            return 0.0
        
        trs = []
        prev_close = float(klines[0][4]) 
        
        for kline in klines[1:]:
            high, low, close = float(kline[2]), float(kline[3]), float(kline[4])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
            prev_close = close
        
        return sum(trs[-self.config.ATR_PERIOD:]) / self.config.ATR_PERIOD

    def calc_vwap(self, klines: List) -> float:
        if not klines:
            return 0.0
        
        klines_to_use = klines[-self.config.VWAP_BARS:]
        
        typical_prices = []
        volumes = []
        
        for kline in klines_to_use:
            high, low, close, volume = float(kline[2]), float(kline[3]), float(kline[4]), float(kline[5])
            typical_price = (high + low + close) / 3
            typical_prices.append(typical_price)
            volumes.append(volume * typical_price) 

        total_notional = sum(volumes)
        total_volume = sum(float(kline[5]) for kline in klines_to_use)

        if total_volume == 0:
             return 0.0
        
        return total_notional / total_volume

    def calc_avg_volume(self, klines: List) -> float:
        """Son 60 barın (1 saat) notional həcminin ortalamasını hesablayır"""
        if not klines or len(klines) < 60:
            return 0.0
        
        volumes = []
        for kline in klines[-60:]:
            price = float(kline[4])
            volume = float(kline[5])
            volumes.append(price * volume)
            
        return sum(volumes) / len(volumes)

    # --- Signal Logic ---
    def build_levels(self, side: SignalType, price: float, atr: float) -> Tuple[float, float, float, float, float]:
        if side == SignalType.LONG:
            entry = price + self.config.ENTRY_OFFSET_ATR * atr
            sl = entry - self.config.SL_ATR * atr
            tp1 = entry + self.config.TP1_ATR * atr
            tp2 = entry + self.config.TP2_ATR * atr
            tp3 = entry + self.config.TP3_ATR * atr
        else:
            entry = price - self.config.ENTRY_OFFSET_ATR * atr
            sl = entry + self.config.SL_ATR * atr
            tp1 = entry - self.config.TP1_ATR * atr
            tp2 = entry - self.config.TP2_ATR * atr
            tp3 = entry - self.config.TP3_ATR * atr
        
        return entry, sl, tp1, tp2, tp3
    
    def calculate_position_size(self, entry: float, sl: float, account_balance: float = 10000) -> Tuple[float, float]:
        risk_amount = account_balance * (self.config.RISK_PER_TRADE_PCT / 100)
        price_risk = abs(entry - sl)
        position_size = risk_amount / price_risk if price_risk > 0 else 0
        return position_size, risk_amount
    
    def analyze_signal_strength(self, state: MarketState) -> Tuple[Optional[SignalType], int, float, List[str]]:
        current_time = time.time()
        # Köhnə dataları təmizləmək
        state.big_buys = [(t, n) for t, n in state.big_buys if current_time - t <= self.config.CONFIRM_WINDOW_SEC]
        state.big_sells = [(t, n) for t, n in state.big_sells if current_time - t <= self.config.CONFIRM_WINDOW_SEC]
        state.long_liq = [(t, n) for t, n in state.long_liq if current_time - t <= self.config.CONFIRM_WINDOW_SEC]
        state.short_liq = [(t, n) for t, n in state.short_liq if current_time - t <= self.config.CONFIRM_WINDOW_SEC]
        
        total_buys = sum(n for _, n in state.big_buys)
        total_sells = sum(n for _, n in state.big_sells)
        total_long_liq = sum(n for _, n in state.long_liq)
        total_short_liq = sum(n for _, n in state.short_liq)
        
        reasons = []
        long_score = 0
        short_score = 0
        
        # Whale Activity 
        whale_net_activity = total_buys - total_sells
        total_whale_vol = total_buys + total_sells
        
        if total_whale_vol > self.config.HUGE_TRADE_USDT * 2:
            if whale_net_activity > total_whale_vol * 0.2:
                long_score += 30
                reasons.append(f"🐋 Güclü Balina Alışı: {self.fmt_usd(total_buys)} vs {self.fmt_usd(total_sells)}")
            elif whale_net_activity < -total_whale_vol * 0.2:
                short_score += 30
                reasons.append(f"🐋 Güclü Balina Satışı: {self.fmt_usd(total_sells)} vs {self.fmt_usd(total_buys)}")
            elif whale_net_activity > 0:
                long_score += 10
            elif whale_net_activity < 0:
                short_score += 10

        # Open Interest
        if state.oi_change >= self.config.OI_BUMP_PCT:
            long_score += 20
            reasons.append(f"📈 OI artımı (30m): {self.fmt_pct(state.oi_change)}") # DÜZƏLİŞ: 30d -> 30m
        elif state.oi_change <= -self.config.OI_BUMP_PCT:
            short_score += 20
            reasons.append(f"📉 OI azalması (30m): {self.fmt_pct(state.oi_change)}") # DÜZƏLİŞ: 30d -> 30m
        
        # Volume Spike
        if state.volume_24h > state.avg_volume_1h * self.config.VOLUME_SPIKE_MULTIPLIER:
            if long_score > short_score:
                long_score += 10
                reasons.append("⚡ Həcm Sıçrayışı (LONG təsdiqi)")
            elif short_score > long_score:
                short_score += 10
                reasons.append("⚡ Həcm Sıçrayışı (SHORT təsdiqi)")

        # Funding Rate
        if state.funding > self.config.FUNDING_TICK * 2:
            short_score += 15
            reasons.append(f"💰 Müsbət funding: {state.funding:.4f} (Short üçün əlverişli)")
        elif state.funding < -self.config.FUNDING_TICK * 2:
            long_score += 15
            reasons.append(f"💰 Negativ funding: {state.funding:.4f} (Long üçün əlverişli)")
        
        # Liquidation Clusters
        if total_short_liq > 2_000_000 and total_short_liq > total_long_liq * 1.5:
            long_score += 15
            reasons.append(f"🔥 Short Liq. Üstünlüyü: {self.fmt_usd(total_short_liq)}")
        if total_long_liq > 2_000_000 and total_long_liq > total_short_liq * 1.5:
            short_score += 15
            reasons.append(f"🔥 Long Liq. Üstünlüyü: {self.fmt_usd(total_long_liq)}")
        
        # Price vs VWAP
        if state.vwap > 0:
            vwap_diff = (state.price - state.vwap) / state.vwap
            if vwap_diff > 0.002:
                long_score += 10
                reasons.append("🟢 VWAP Təsdiqi (Yüksək)")
            elif vwap_diff < -0.002:
                short_score += 10
                reasons.append("🔴 VWAP Təsdiqi (Aşağı)")
        
        # Determine Signal
        if long_score >= 50 and long_score > short_score + 10:
            confidence = min(0.95, long_score / 100)
            return SignalType.LONG, long_score, confidence, reasons
        elif short_score >= 50 and short_score > long_score + 10:
            confidence = min(0.95, short_score / 100)
            return SignalType.SHORT, short_score, confidence, reasons
        
        return None, 0, 0.0, []
    
    async def check_and_send_signal(self, symbol: str):
        state = self.states[symbol]
        
        async with state.lock:
            if datetime.now() < self.cooldown_until[symbol]:
                return
            
            signal_type, strength, confidence, reasons = self.analyze_signal_strength(state)
            
            # DÜZƏLİŞ: Siqnal guard - ATR, Price və VWAP yoxlanılır.
            if signal_type and state.atr > 0 and state.price > 0 and state.vwap > 0: 
                entry, sl, tp1, tp2, tp3 = self.build_levels(signal_type, state.price, state.atr)
                position_size, risk_amount = self.calculate_position_size(entry, sl)
                
                signal = Signal(
                    symbol=symbol,
                    signal_type=signal_type,
                    strength=strength,
                    confidence=confidence,
                    price=state.price,
                    atr=state.atr,
                    entry=entry,
                    sl=sl,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                    volume_24h=state.volume_24h,
                    oi_change=state.oi_change,
                    funding=state.funding,
                    timestamp=datetime.now(),
                    reasons=reasons
                )
                
                await self.send_signal_message(signal, position_size, risk_amount)
                
                # Cooldown aktiv et
                self.cooldown_until[symbol] = datetime.now() + timedelta(minutes=self.config.COOLDOWN_MINUTES)
                state.last_signal_time = datetime.now()
                
                # Balina/Likvidasiya datalarını sıfırla, digər dataları saxla
                self.states[symbol] = MarketState(
                    lock=state.lock, 
                    price=state.price,
                    atr=state.atr,
                    volume_24h=state.volume_24h,
                    avg_volume_1h=state.avg_volume_1h,
                    vwap=state.vwap,
                    oi_change=state.oi_change,
                    funding=state.funding
                )
    
    async def send_signal_message(self, signal: Signal, position_size: float, risk_amount: float):
        emoji = "🟢" if signal.signal_type == SignalType.LONG else "🔴"
        direction = "LONG" if signal.signal_type == SignalType.LONG else "SHORT"
        
        message = f"""
{emoji} <b>WHALE SIGNAL DETECTED | {signal.symbol}</b> {emoji}

<b>Direction:</b> {direction} (Güc: {signal.strength}/100 | Güvən: {signal.confidence:.1%})

<b>Market Data:</b>
• Son Qiymət: ${signal.price:,.2f}
• ATR ({self.config.ATR_PERIOD}m): ${signal.atr:.2f}
• 24h Həcm: {self.fmt_usd(signal.volume_24h)}
• OI Dəyişikliyi (30m): {self.fmt_pct(signal.oi_change)}
• Funding Rate: {signal.funding:.4f}

<b>Ticarət Səviyyələri:</b>
🎯 Entry: ${signal.entry:,.2f} (Qiymətdən ${abs(signal.entry - signal.price):.2f} fərq)
🛑 Stop Loss: ${signal.sl:,.2f}
✅ TP1 ({self.config.TP1_ATR}R): ${signal.tp1:,.2f}
✅ TP2 ({self.config.TP2_ATR}R): ${signal.tp2:,.2f}

<b>Risk İdarəetməsi (10K Hesab):</b>
• Risk: {self.config.RISK_PER_TRADE_PCT}% (${risk_amount:,.2f})
• Mövqe Həcmi: {position_size:,.4f} contracts

<b>Siqnal Səbəbləri:</b>
{chr(10).join(f"• {reason}" for reason in signal.reasons)}

<code>Timestamp: {signal.timestamp.strftime('%Y-%m-%d %H:%M:%S')}</code>

<i>⚠️ Risk Xəbərdarlığı: Bu, maliyyə məsləhəti deyil. Öz araşdırmanızı aparın.</i>
"""
        
        keyboard = {
            "inline_keyboard": [[
                {"text": "📊 Binance", "url": f"https://www.binance.com/en/futures/{signal.symbol}"},
                {"text": "📈 TradingView", "url": f"https://www.tradingview.com/chart/?symbol=BINANCE:{signal.symbol}"}
            ]]
        }
        
        await self.tg_send(message, keyboard)
        logger.info(f"Signal sent for {signal.symbol}: {direction} (Strength: {signal.strength})")
    
    async def market_data_poller(self):
        logger.info("Starting market data poller")
        
        while True:
            try:
                tickers = await self.get_multiple_24h_tickers()
                
                for symbol in self.config.SYMBOLS:
                    state = self.states[symbol]
                    
                    async with state.lock:
                        klines = await self.get_klines(symbol, f"{self.config.ATR_MINUTES}m", 100)
                        
                        if klines:
                            state.atr = self.calc_atr(klines)
                            state.vwap = self.calc_vwap(klines)
                            state.avg_volume_1h = self.calc_avg_volume(klines)
                        
                        if symbol in tickers:
                            state.volume_24h = float(tickers[symbol].get('quoteVolume', 0)) 
                            
                        state.oi_change = await self.get_oi_change_pct(symbol)
                        state.funding = await self.get_funding_rate(symbol)
                        
                        await self.check_and_send_signal(symbol)
                        
                await asyncio.sleep(10)
                
            except Exception as e:
                logger.error(f"Market data poller error: {e}")
                await asyncio.sleep(30)
    
    async def websocket_handler(self):
        streams = ["!forceOrder@arr"] + [f"{s.lower()}@aggTrade" for s in self.config.SYMBOLS]
        url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"
        ssl_context = ssl.create_default_context()
        
        while True:
            try:
                async with websockets.connect(url, ssl=ssl_context, ping_interval=20) as ws:
                    logger.info("WebSocket connected successfully")
                    async for message in ws:
                        try:
                            data = json.loads(message)
                            stream = data.get("stream", "")
                            payload = data.get("data", {})
                            await self.process_websocket_message(stream, payload)
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON decode error: {e}")
                        except Exception as e:
                            logger.error(f"WebSocket message processing error: {e}")
            except Exception as e:
                logger.error(f"WebSocket connection error: {e}, reconnecting in 10 seconds...")
                await asyncio.sleep(10)
    
    async def process_websocket_message(self, stream: str, payload: dict):
        current_time = time.time()
        
        # DÜZƏLİŞ: Liquidation stream match
        if stream == "!forceOrder@arr":
            orders = payload if isinstance(payload, list) else [payload]
            for order in orders:
                await self.process_liquidation(order, current_time)
        
        elif stream.endswith("@aggTrade"):
            await self.process_agg_trade(stream, payload, current_time)

    async def process_liquidation(self, order: dict, timestamp: float):
        order_data = order.get("o", {})
        symbol = order_data.get("s", "")
        
        if symbol not in self.config.SYMBOLS:
            return
        
        state = self.states[symbol]
        async with state.lock:
            side = order_data.get("S")
            quantity = float(order_data.get("q", "0"))
            price = float(order_data.get("p", "0"))
            notional = quantity * price
            
            if notional < self.config.MIN_LIQ_NOTIONAL:
                return
            
            if side == "BUY":  # Short liquidation
                state.short_liq.append((timestamp, notional))
                logger.info(f"🔥 Short liquidation: {symbol} {self.fmt_usd(notional)}")
            else:  # Long liquidation   
                state.long_liq.append((timestamp, notional))
                logger.info(f"🔥 Long liquidation: {symbol} {self.fmt_usd(notional)}")
    
    async def process_agg_trade(self, stream: str, trade: dict, timestamp: float):
        symbol_lower = stream.split('@')[0]
        symbol = symbol_lower.upper()
        
        if symbol not in self.config.SYMBOLS:
            return
        
        state = self.states[symbol]
        async with state.lock:
            price = float(trade.get("p", "0"))
            quantity = float(trade.get("q", "0"))
            notional = price * quantity
            
            state.price = price 
            
            if notional >= self.config.BIG_TRADE_USDT:
                
                is_marker_maker = trade.get("m") 
                
                # DÜZƏLİŞ: AggTrade 'm' bayrağı məntiqi düzəldildi
                # Binance: m=True (Buyer is maker) => Aggressor is SELL (Təcavüzkar SATIŞ edən tərəfdir)
                if is_marker_maker:
                    state.big_sells.append((timestamp, notional)) # SELL
                    log_type = "HUGE SELL" if notional >= self.config.HUGE_TRADE_USDT else "BIG SELL"
                else:
                    state.big_buys.append((timestamp, notional)) # BUY
                    log_type = "HUGE BUY" if notional >= self.config.HUGE_TRADE_USDT else "BIG BUY"

                logger.info(f"🐳 {log_type}: {symbol} {self.fmt_usd(notional)} @ ${price:.2f}")

    async def run(self):
        await self.initialize()
        
        try:
            await self.tg_send(
                f"🤖 <b>Advanced Whale Signal Bot Started</b>\n"
                f"Monitoring: {', '.join(self.config.SYMBOLS)}\n"
                f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            await asyncio.gather(
                self.market_data_poller(),
                self.websocket_handler(),
                return_exceptions=True
            )
        
        except Exception as e:
            logger.error(f"Bot runtime error: {e}")
        finally:
            await self.close()

# --- Main Execution ---
async def main():
    config = Config()
    bot = AdvancedWhaleBot(config)
    
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":
    asyncio.run(main())