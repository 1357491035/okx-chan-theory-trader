#!/usr/bin/env python3
"""
缠论+MACD+BOLL+KDJ+RSI OKX合约短线交易系统 v2.1
核心框架：15分钟定方向 → 5分钟找背驰 → 1分钟确认入场
v2.1: 集成DeepSeek AI Agent(行情分析+大环境+仓位管理+复盘)
"""

import json
import os
import time
import logging
import subprocess
import re
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

import ccxt
import pandas as pd
import ta
from ai_agent import ai_enhanced_decision, review_trades, notify_trade_open, notify_trade_close

# ─── 日志配置 ───
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / 'trading_v2.log', encoding='utf-8')
    ]
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / 'config.json'
with open(CONFIG_PATH, 'r') as f:
    CONFIG = json.load(f)

PAPER_TRADE_FILE = Path(__file__).parent / 'paper_trades_v2.json'

API_DELAY_BETWEEN_SYMBOLS = 1.0
API_DELAY_BETWEEN_REQUESTS = 0.3
API_RETRY_MAX = 3
API_RETRY_BACKOFF = 5.0
TIME_STOP_MINUTES = 999999  # 禁用时间止损
TRAILING_STOP_TRIGGER = 0.5
TRAILING_STOP_STEP = 0.3
DAILY_LOSS_LIMIT_PCT = 0.05


class OKXExchange:
    def __init__(self, api_key=None, api_secret=None, passphrase=None, testnet=True):
        self.exchange = ccxt.okx({
            'apiKey': api_key or CONFIG['okx']['api_key'],
            'secret': api_secret or CONFIG['okx']['api_secret'],
            'password': passphrase or CONFIG['okx']['passphrase'],
            'enableRateLimit': True,
            'rateLimit': 200,
            'options': {'defaultType': 'swap'},
            'proxies': {
                'http': 'http://127.0.0.1:7890',
                'https': 'http://127.0.0.1:7890',
            },
        })
        if testnet and CONFIG['okx'].get('testnet', False):
            self.exchange.set_sandbox_mode(True)
            log.info("🔧 已启用OKX模拟盘模式")
        else:
            log.info("🔧 已启用OKX实盘模式")

    def _retry_request(self, func, *args, **kwargs):
        for attempt in range(API_RETRY_MAX):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt < API_RETRY_MAX - 1:
                    wait = API_RETRY_BACKOFF * (attempt + 1)
                    log.warning(f"⏳ 请求失败({attempt+1}/{API_RETRY_MAX}), {wait}s后重试: {e}")
                    time.sleep(wait)
                else:
                    raise e

    def get_balance(self):
        balance = self._retry_request(self.exchange.fetch_balance)
        usdt = balance.get('USDT', {})
        return {'total': float(usdt.get('total', 0)), 'free': float(usdt.get('free', 0)), 'used': float(usdt.get('used', 0))}

    def get_klines(self, symbol, timeframe, limit=200):
        ohlcv = self._retry_request(self.exchange.fetch_ohlcv, symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.astype({'open': float, 'high': float, 'low': float, 'close': float, 'volume': float})
        return df

    def set_leverage(self, symbol, leverage):
        try:
            pos_mode = CONFIG['trading'].get('position_mode', 'net')
            params = {'mgnMode': 'isolated'}
            if pos_mode != 'net':
                params['posSide'] = 'long'
            self.exchange.set_leverage(leverage, symbol, params=params)
            if pos_mode != 'net':
                self.exchange.set_leverage(leverage, symbol, params={'mgnMode': 'isolated', 'posSide': 'short'})
            log.info(f"📊 {symbol} 杠杆={leverage}x")
        except Exception as e:
            log.warning(f"设置杠杆失败: {e}")

    def place_market_order(self, symbol, side, amount):
        try:
            pos_mode = CONFIG['trading'].get('position_mode', 'net')
            params = {'tdMode': 'isolated'}
            if pos_mode != 'net':
                params['posSide'] = 'long' if side == 'buy' else 'short'
            order = self.exchange.create_order(symbol, 'market', side, amount, params=params)
            log.info(f"✅ 市价{side} {symbol} 数量{amount}")
            return order
        except Exception as e:
            log.error(f"❌ 下单失败: {e}")
            return None

    def place_order_with_tp_sl(self, symbol, side, amount, stop_loss_price, take_profit_price):
        try:
            pos_mode = CONFIG['trading'].get('position_mode', 'net')
            params = {'tdMode': 'isolated'}
            if pos_mode != 'net':
                params['posSide'] = 'long' if side == 'buy' else 'short'
            params['attachAlgoOrds'] = [{'slTriggerPx': str(stop_loss_price), 'slOrdPx': '-1', 'tpTriggerPx': str(take_profit_price), 'tpOrdPx': '-1'}]
            order = self.exchange.create_order(symbol, 'market', side, amount, params=params)
            log.info(f"✅ 市价{side} {symbol} 数量{amount} (带止盈止损)")
            return order
        except Exception as e:
            log.error(f"❌ 带止盈止损下单失败: {e}")
            return None

    def get_positions(self, symbol=None):
        positions = self._retry_request(self.exchange.fetch_positions, symbols=[symbol] if symbol else None)
        result = []
        for p in positions:
            amt = float(p.get('contracts', 0) or 0)
            if amt > 0:
                result.append({'symbol': p['symbol'], 'side': p['side'], 'amount': amt, 'entry_price': float(p.get('entryPrice', 0)), 'unrealized_pnl': float(p.get('unrealizedPnl', 0)), 'leverage': p.get('leverage', 1)})
        return result

    def close_position(self, symbol, side, amount):
        try:
            pos_mode = CONFIG['trading'].get('position_mode', 'net')
            close_side = 'sell' if side == 'long' else 'buy'
            params = {'tdMode': 'isolated', 'reduceOnly': True}
            if pos_mode != 'net':
                params['posSide'] = side
            order = self.exchange.create_order(symbol, 'market', close_side, amount, params=params)
            log.info(f"🔒 平仓 {symbol} {side}")
            return order
        except Exception as e:
            log.error(f"❌ 平仓失败: {e}")
            return None


class TechnicalAnalysis:
    @staticmethod
    def calc_macd(df, fast=12, slow=26, signal=9):
        macd = ta.trend.MACD(df['close'], window_slow=slow, window_fast=fast, window_sign=signal)
        df = df.copy()
        df['macd_dif'] = macd.macd()
        df['macd_dea'] = macd.macd_signal()
        df['macd_hist'] = macd.macd_diff()
        df['macd_above_zero'] = df['macd_dif'] > 0
        df['macd_golden_cross'] = (df['macd_dif'] > df['macd_dea']) & (df['macd_dif'].shift(1) <= df['macd_dea'].shift(1))
        df['macd_death_cross'] = (df['macd_dif'] < df['macd_dea']) & (df['macd_dif'].shift(1) >= df['macd_dea'].shift(1))
        return df

    @staticmethod
    def calc_boll(df, window=20, std_dev=2):
        boll = ta.volatility.BollingerBands(df['close'], window=window, window_dev=std_dev)
        df = df.copy()
        df['boll_upper'] = boll.bollinger_hband()
        df['boll_mid'] = boll.bollinger_mavg()
        df['boll_lower'] = boll.bollinger_lband()
        df['boll_width'] = (df['boll_upper'] - df['boll_lower']) / df['boll_mid']
        df['boll_position'] = (df['close'] - df['boll_lower']) / (df['boll_upper'] - df['boll_lower'])
        df['boll_mid_slope'] = df['boll_mid'].diff(5) / df['boll_mid'].shift(5)
        return df

    @staticmethod
    def calc_kdj(df, k_window=9, d_smooth=3):
        df = df.copy()
        low_min = df['low'].rolling(window=k_window).min()
        high_max = df['high'].rolling(window=k_window).max()
        rsv = (df['close'] - low_min) / (high_max - low_min) * 100
        rsv = rsv.fillna(50)
        k = [50.0]; d = [50.0]
        for i in range(1, len(rsv)):
            k.append(2/3 * k[-1] + 1/3 * rsv.iloc[i])
            d.append(2/3 * d[-1] + 1/3 * k[-1])
        df['kdj_k'] = k; df['kdj_d'] = d
        df['kdj_j'] = 3 * pd.Series(k) - 2 * pd.Series(d)
        df['kdj_golden_cross'] = (df['kdj_k'] > df['kdj_d']) & (df['kdj_k'].shift(1) <= df['kdj_d'].shift(1))
        df['kdj_death_cross'] = (df['kdj_k'] < df['kdj_d']) & (df['kdj_k'].shift(1) >= df['kdj_d'].shift(1))
        df['kdj_overbought'] = df['kdj_k'] > 80
        df['kdj_oversold'] = df['kdj_k'] < 20
        return df

    @staticmethod
    def calc_rsi(df, window=14):
        df = df.copy()
        rsi = ta.momentum.RSIIndicator(df['close'], window=window)
        df['rsi'] = rsi.rsi()
        df['rsi_overbought'] = df['rsi'] > 70
        df['rsi_oversold'] = df['rsi'] < 30
        df['rsi_bullish_divergence'] = (df['low'] < df['low'].shift(10)) & (df['rsi'] > df['rsi'].shift(10)) & (df['rsi'] < 40)
        df['rsi_bearish_divergence'] = (df['high'] > df['high'].shift(10)) & (df['rsi'] < df['rsi'].shift(10)) & (df['rsi'] > 60)
        return df

    @staticmethod
    def calc_volume(df):
        df = df.copy()
        df['vol_ma20'] = df['volume'].rolling(20).mean()
        df['vol_ratio'] = df['volume'] / df['vol_ma20']
        df['vol_surge'] = df['vol_ratio'] > 2.0
        return df

    @staticmethod
    def calc_chanlun_simple(df):
        df = df.copy()
        for i in range(1, len(df) - 1):
            if df.iloc[i]['high'] <= df.iloc[i-1]['high'] and df.iloc[i]['low'] >= df.iloc[i-1]['low']:
                if i >= 2 and df.iloc[i-2]['high'] < df.iloc[i-1]['high']:
                    df.iat[i, df.columns.get_loc('high')] = max(df.iloc[i]['high'], df.iloc[i-1]['high'])
                    df.iat[i, df.columns.get_loc('low')] = max(df.iloc[i]['low'], df.iloc[i-1]['low'])
                else:
                    df.iat[i, df.columns.get_loc('high')] = min(df.iloc[i]['high'], df.iloc[i-1]['high'])
                    df.iat[i, df.columns.get_loc('low')] = min(df.iloc[i]['low'], df.iloc[i-1]['low'])
        df['fractal_top'] = False; df['fractal_bottom'] = False
        for i in range(1, len(df) - 1):
            if df.iloc[i]['high'] > df.iloc[i-1]['high'] and df.iloc[i]['high'] > df.iloc[i+1]['high']:
                df.iat[i, df.columns.get_loc('fractal_top')] = True
            if df.iloc[i]['low'] < df.iloc[i-1]['low'] and df.iloc[i]['low'] < df.iloc[i+1]['low']:
                df.iat[i, df.columns.get_loc('fractal_bottom')] = True
        df['macd_hist_abs'] = df['macd_hist'].abs()
        df['macd_area'] = df['macd_hist_abs'].rolling(5).sum()
        df['divergence_bull'] = (df['low'] < df['low'].shift(10)) & (df['macd_area'] < df['macd_area'].shift(10)) & (df['macd_hist'] < 0)
        df['divergence_bear'] = (df['high'] > df['high'].shift(10)) & (df['macd_area'] < df['macd_area'].shift(10)) & (df['macd_hist'] > 0)
        return df

    @classmethod
    def full_analysis(cls, df):
        df = cls.calc_macd(df)
        df = cls.calc_boll(df)
        df = cls.calc_kdj(df)
        df = cls.calc_rsi(df)
        df = cls.calc_volume(df)
        df = cls.calc_chanlun_simple(df)
        return df


class SignalGenerator:
    @staticmethod
    def generate_signal(df_15m, df_5m, df_1m):
        signal = {'action': 'HOLD', 'strength': 0, 'reason': ''}
        last_15m = df_15m.iloc[-1]
        if (last_15m['boll_mid_slope'] > 0 and last_15m['close'] > last_15m['boll_mid'] and last_15m['macd_above_zero'] and last_15m['rsi'] > 40):
            trend = 'UP'
        elif (last_15m['boll_mid_slope'] < 0 and last_15m['close'] < last_15m['boll_mid'] and not last_15m['macd_above_zero'] and last_15m['rsi'] < 60):
            trend = 'DOWN'
        else:
            trend = 'NEUTRAL'

        last_5m = df_5m.iloc[-1]; prev_5m = df_5m.iloc[-2]
        last_1m = df_1m.iloc[-1]

        # 做多
        ls = 0; lr = []
        if trend == 'UP': ls += 30; lr.append('15m趋势向上')
        elif trend == 'NEUTRAL': ls += 10; lr.append('15m趋势中性')
        if last_5m['macd_golden_cross'] and last_5m['macd_above_zero']: ls += 25; lr.append('5m零轴上金叉')
        elif last_5m['macd_golden_cross']: ls += 15; lr.append('5m金叉')
        if last_5m['divergence_bull']: ls += 20; lr.append('5m底背驰')
        if last_5m['boll_position'] < 0.2 or (prev_5m['close'] < prev_5m['boll_mid'] and last_5m['close'] > last_5m['boll_mid']): ls += 15; lr.append('5m BOLL下轨/回踩中轨')
        elif last_5m['boll_position'] < 0.35: ls += 8; lr.append('5m BOLL偏低')
        if last_5m['kdj_golden_cross'] and last_5m['kdj_k'] < 30: ls += 15; lr.append('5m KDJ低位金叉')
        elif last_5m['kdj_golden_cross']: ls += 8; lr.append('5m KDJ金叉')
        if last_5m['kdj_j'] > 20 and prev_5m.get('kdj_j', 50) < 20: ls += 12; lr.append('5m KDJ低位拐头')
        if last_5m['rsi_oversold']: ls += 10; lr.append('5m RSI超卖')
        if last_5m['rsi_bullish_divergence']: ls += 15; lr.append('5m RSI底背离')
        if last_5m['vol_surge']: ls += 8; lr.append(f'5m放量({last_5m["vol_ratio"]:.1f}x)')
        if last_1m['kdj_golden_cross'] or last_1m['macd_golden_cross'] or last_1m['fractal_bottom']: ls += 10; lr.append('1m确认信号')

        # 做空
        ss = 0; sr = []
        if trend == 'DOWN': ss += 30; sr.append('15m趋势向下')
        elif trend == 'NEUTRAL': ss += 10; sr.append('15m趋势中性')
        if last_5m['macd_death_cross'] and not last_5m['macd_above_zero']: ss += 25; sr.append('5m零轴下死叉')
        elif last_5m['macd_death_cross']: ss += 15; sr.append('5m死叉')
        if last_5m['divergence_bear']: ss += 20; sr.append('5m顶背驰')
        if last_5m['boll_position'] > 0.8: ss += 15; sr.append('5m BOLL上轨')
        elif last_5m['boll_position'] > 0.7: ss += 8; sr.append('5m BOLL偏高')
        if last_5m['kdj_death_cross'] and last_5m['kdj_k'] > 70: ss += 15; sr.append('5m KDJ高位死叉')
        elif last_5m['kdj_death_cross']: ss += 8; sr.append('5m KDJ死叉')
        if last_5m['kdj_j'] < 80 and prev_5m.get('kdj_j', 50) > 80: ss += 12; sr.append('5m KDJ高位拐头')
        if last_5m['rsi_overbought']: ss += 10; sr.append('5m RSI超买')
        if last_5m['rsi_bearish_divergence']: ss += 15; sr.append('5m RSI顶背离')
        if last_5m['vol_surge']: ss += 8; sr.append(f'5m放量({last_5m["vol_ratio"]:.1f}x)')
        if last_1m['kdj_death_cross'] or last_1m['macd_death_cross'] or last_1m['fractal_top']: ss += 10; sr.append('1m确认信号')

        min_strength = CONFIG['trading'].get('min_signal_strength', 60)
        if ls >= min_strength and ls > ss:
            signal['action'] = 'LONG'; signal['strength'] = min(ls, 100); signal['reason'] = ' | '.join(lr)
        elif ss >= min_strength and ss > ls:
            signal['action'] = 'SHORT'; signal['strength'] = min(ss, 100); signal['reason'] = ' | '.join(sr)
        return signal


class NewsSentimentAnalyzer:
    HIGH_IMPACT_BEAR = {'hack', 'exploit', 'rug pull', 'sec lawsuit', 'ban crypto', 'exchange collapsed', 'delisted', 'flash crash', '暴跌', '崩盘', '黑客攻击', '跑路', '下架', '封禁'}
    HIGH_IMPACT_BULL = {'etf approved', 'institutional buying', 'scc approval', 'major partnership', 'listed on', 'whale accumulation massive', 'ETF通过', '获批', '上线', '巨鲸增持'}
    MED_IMPACT_BEAR = {'whale dump', 'whale sell', 'ceo sells', 'regulation crackdown', 'sec investigation', 'country ban', 'elon sells', 'sells bitcoin', '巨鲸抛售', '抛售', '监管', '调查', '罚款'}
    MED_IMPACT_BULL = {'whale buy', 'whale accumulation', 'partnership announced', 'upgrade completed', 'halving', 'elon buys', 'musk crypto', 'country adopts', 'treasury buys', '巨鲸买入', '增持', '合作', '升级', '减半', '采用'}
    LOW_IMPACT_BEAR = {'bearish', 'dump', 'plunge', 'correction', 'pullback', '利空', '下跌', '回调'}
    LOW_IMPACT_BULL = {'bullish', 'surge', 'rally', 'breakout', 'soar', '利好', '上涨', '突破'}
    SYMBOL_NAMES = {
        'BTC/USDT:USDT': ['Bitcoin', 'BTC'], 'ETH/USDT:USDT': ['Ethereum', 'ETH'],
        'SOL/USDT:USDT': ['Solana', 'SOL'], 'DOGE/USDT:USDT': ['Dogecoin', 'DOGE'],
        'XRP/USDT:USDT': ['XRP', 'Ripple'], 'ADA/USDT:USDT': ['Cardano', 'ADA'],
        'AVAX/USDT:USDT': ['Avalanche', 'AVAX'], 'LINK/USDT:USDT': ['Chainlink', 'LINK'],
        'DOT/USDT:USDT': ['Polkadot', 'DOT'], 'UNI/USDT:USDT': ['Uniswap', 'UNI'],
        'APT/USDT:USDT': ['Aptos', 'APT'], 'ARB/USDT:USDT': ['Arbitrum', 'ARB'],
        'OP/USDT:USDT': ['Optimism', 'OP'], 'PEPE/USDT:USDT': ['PEPE'],
        'WIF/USDT:USDT': ['dogwifhat', 'WIF'], 'FIL/USDT:USDT': ['Filecoin', 'FIL'],
        'NEAR/USDT:USDT': ['NEAR Protocol', 'NEAR'], 'SUI/USDT:USDT': ['Sui', 'SUI'],
    }

    def __init__(self):
        self.cache = {}; self.cache_ttl = 300

    def _search_news(self, query):
        try:
            result = subprocess.run(['curl', '-s', '-x', 'http://127.0.0.1:7890', '--max-time', '8', f'https://www.google.com/search?q={query}&tbm=nws&tbs=qdr:d&num=5'], capture_output=True, text=True, timeout=12)
            text = result.stdout.lower() if result.stdout else ''
            text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            return text
        except Exception:
            return ''

    def _check_macro_sentiment(self):
        try:
            text = self._search_news('crypto+market+today')
            if not text: return 0.0
            bull = sum(1 for kw in ['surge', 'rally', 'bullish', 'breakout', '上涨', '暴涨', '利好'] if kw in text)
            bear = sum(1 for kw in ['crash', 'dump', 'bearish', 'plunge', '暴跌', '崩盘', '利空'] if kw in text)
            total = bull + bear
            return max(-1.0, min(1.0, (bull - bear) / total)) if total > 0 else 0.0
        except Exception:
            return 0.0

    def analyze_symbol(self, symbol):
        now = time.time()
        if symbol in self.cache and now - self.cache[symbol][0] < self.cache_ttl:
            return self.cache[symbol][1]
        names = self.SYMBOL_NAMES.get(symbol, [symbol.split('/')[0]])
        sentiment = 0.0; impact_level = 'LOW'; hits = 0
        for name in names:
            text = self._search_news(f'{name}+crypto+news')
            if not text: continue
            hb = sum(1 for kw in self.HIGH_IMPACT_BULL if kw in text)
            hr = sum(1 for kw in self.HIGH_IMPACT_BEAR if kw in text)
            mb = sum(1 for kw in self.MED_IMPACT_BULL if kw in text)
            mr = sum(1 for kw in self.MED_IMPACT_BEAR if kw in text)
            lb = sum(1 for kw in self.LOW_IMPACT_BULL if kw in text)
            lr = sum(1 for kw in self.LOW_IMPACT_BEAR if kw in text)
            bs = hb*3 + mb*2 + lb*1; brs = hr*3 + mr*2 + lr*1
            total = bs + brs
            if total > 0:
                sentiment += (bs - brs) / total; hits += 1
                if hb > 0 or hr > 0: impact_level = 'HIGH'
                elif (mb > 0 or mr > 0) and impact_level != 'HIGH': impact_level = 'MED'
        if hits > 0: sentiment = max(-1.0, min(1.0, sentiment / hits))
        self.cache[symbol] = (now, (sentiment, impact_level))
        direction = '利好' if sentiment > 0.2 else ('利空' if sentiment < -0.2 else '中性')
        log.info(f"📰 {symbol} 消息面: {direction} (情绪={sentiment:.2f}, 影响={impact_level})")
        return (sentiment, impact_level)

    def adjust_signal(self, signal, symbol):
        result = self.analyze_symbol(symbol)
        sentiment, impact_level = result if isinstance(result, tuple) else (result, 'LOW')
        if signal['action'] == 'HOLD' or sentiment == 0:
            signal['news_sentiment'] = round(sentiment, 2); signal['news_impact'] = impact_level; return signal
        max_adjust = 15 if impact_level == 'HIGH' else (10 if impact_level == 'MED' else 3)
        adjustment = max(-max_adjust, min(max_adjust, int(sentiment * max_adjust)))
        if impact_level == 'HIGH':
            signal['strength'] = max(0, min(100, signal['strength'] + adjustment))
            if signal['strength'] < CONFIG['trading'].get('min_signal_strength', 60): signal['action'] = 'HOLD'
        else:
            signal['strength'] = max(55, min(100, signal['strength'] + adjustment))
        signal['news_sentiment'] = round(sentiment, 2); signal['news_impact'] = impact_level
        return signal


class HotSymbolScanner:
    def __init__(self, exchange): self.ex = exchange
    def scan_hot_symbols(self, min_volume=5000000, top_n=5):
        try:
            markets = self.ex._retry_request(self.ex.exchange.fetch_markets)
            swap_syms = [m['symbol'] for m in markets if m.get('swap') and m.get('active') and '/USDT:' in m['symbol']]
            hot = []
            for i in range(0, min(len(swap_syms), 30), 5):
                for sym in swap_syms[i:i+5]:
                    try:
                        t = self.ex.exchange.fetch_ticker(sym)
                        vol = float(t.get('quoteVolume', 0)); chg = float(t.get('percentage', 0))
                        if vol >= min_volume: hot.append({'symbol': sym, 'volume': vol, 'change_24h': chg})
                    except: continue
                time.sleep(1)
            hot.sort(key=lambda x: x['volume'], reverse=True)
            tv = [h['symbol'] for h in hot[:top_n]]
            hot.sort(key=lambda x: abs(x['change_24h']), reverse=True)
            tm = [h['symbol'] for h in hot[:top_n]]
            new = [s for s in list(dict.fromkeys(tv + tm)) if s not in set(CONFIG['trading']['symbols'])][:3]
            if new: log.info(f"🔥 热门币: {new}")
            return new
        except Exception as e:
            log.warning(f"热门扫描失败: {e}"); return []


class AdaptiveConfig:
    def __init__(self):
        self.min_signal_strength = CONFIG['trading'].get('min_signal_strength', 60)
        self.last_adjust_time = 0; self.adjust_interval = 1800
    def adjust(self, bot):
        now = time.time()
        if now - self.last_adjust_time < self.adjust_interval: return
        self.last_adjust_time = now
        stats = bot.get_stats()
        if stats['total_trades'] < 10: return
        old = self.min_signal_strength; wr = stats['win_rate']
        if wr < 40: self.min_signal_strength = min(75, self.min_signal_strength + 5)
        elif wr < 50: self.min_signal_strength = min(70, self.min_signal_strength + 3)
        elif wr > 80: self.min_signal_strength = max(55, self.min_signal_strength - 3)
        elif wr > 70: self.min_signal_strength = max(58, self.min_signal_strength - 2)
        if self.min_signal_strength != old:
            log.info(f"🔧 自适应阈值: {old}→{self.min_signal_strength} (胜率{wr}%)")


class PaperTradingBot:
    def __init__(self, exchange, initial_balance=1000.0):
        self.ex = exchange; self.ta = TechnicalAnalysis(); self.sg = SignalGenerator()
        self.news = NewsSentimentAnalyzer(); self.hot_scanner = HotSymbolScanner(exchange); self.adaptive = AdaptiveConfig()
        self.balance = initial_balance; self.initial_balance = initial_balance
        self.positions = {}; self.trade_history = []; self.closed_trades = []; self.scan_count = 0
        self.daily_pnl = 0.0; self.daily_reset_date = datetime.now().strftime('%Y-%m-%d'); self.circuit_breaker = False
        self._load_state()

    def _load_state(self):
        if PAPER_TRADE_FILE.exists():
            with open(PAPER_TRADE_FILE, 'r') as f: state = json.load(f)
            self.balance = state.get('balance', self.balance)
            self.initial_balance = state.get('initial_balance', self.initial_balance)
            self.positions = state.get('positions', {})
            self.closed_trades = state.get('closed_trades', [])
            self.scan_count = state.get('scan_count', 0)
            self.daily_pnl = state.get('daily_pnl', 0)
            self.daily_reset_date = state.get('daily_reset_date', datetime.now().strftime('%Y-%m-%d'))
            log.info(f"📋 v2.1记录: {len(self.closed_trades)}笔已平仓, {len(self.positions)}持仓, {self.scan_count}次扫描")

    def _save_state(self):
        state = {'balance': self.balance, 'initial_balance': self.initial_balance, 'positions': self.positions, 'trade_history': self.trade_history, 'closed_trades': self.closed_trades, 'scan_count': self.scan_count, 'daily_pnl': self.daily_pnl, 'daily_reset_date': self.daily_reset_date, 'version': '2.1'}
        with open(PAPER_TRADE_FILE, 'w') as f: json.dump(state, f, indent=2, ensure_ascii=False)

    def _reset_daily(self):
        today = datetime.now().strftime('%Y-%m-%d')
        if today != self.daily_reset_date:
            self.daily_pnl = 0; self.daily_reset_date = today; self.circuit_breaker = False
            log.info(f"📅 新的一天 {today}")

    def _check_circuit_breaker(self):
        self._reset_daily()
        if self.daily_pnl < -(self.initial_balance * DAILY_LOSS_LIMIT_PCT):
            if not self.circuit_breaker:
                self.circuit_breaker = True
                log.warning(f"🚨 日亏损熔断！日亏{self.daily_pnl:.2f}U")
            return True
        return False

    def _check_positions(self, current_prices):
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]; cp = current_prices.get(symbol)
            if cp is None: continue
            entry = pos['entry']; side = pos['side']; sl = pos['stop_loss']; tp = pos['take_profit']
            closed = False; close_price = None; close_reason = ''
            if side == 'LONG': pnl_pct = (cp - entry) / entry * CONFIG['trading']['leverage']
            else: pnl_pct = (entry - cp) / entry * CONFIG['trading']['leverage']
            # 移动止盈
            if pos.get('trailing_active'):
                if side == 'LONG':
                    trail = entry * (1 + pos['max_pnl_pct'] / CONFIG['trading']['leverage'] - TRAILING_STOP_STEP / CONFIG['trading']['leverage'])
                    if cp <= trail: closed = True; close_price = cp; close_reason = '移动止盈'
                else:
                    trail = entry * (1 - pos['max_pnl_pct'] / CONFIG['trading']['leverage'] + TRAILING_STOP_STEP / CONFIG['trading']['leverage'])
                    if cp >= trail: closed = True; close_price = cp; close_reason = '移动止盈'
                if pnl_pct > pos.get('max_pnl_pct', 0): pos['max_pnl_pct'] = pnl_pct
            elif pnl_pct >= TRAILING_STOP_TRIGGER:
                pos['trailing_active'] = True; pos['max_pnl_pct'] = pnl_pct
                log.info(f"📈 {symbol} 移动止盈激活！盈利{pnl_pct*100:.1f}%")
            if not closed:
                if side == 'LONG' and cp <= sl: closed = True; close_price = sl; close_reason = '止损'
                elif side == 'SHORT' and cp >= sl: closed = True; close_price = sl; close_reason = '止损'
            if not closed:
                if side == 'LONG' and cp >= tp: closed = True; close_price = tp; close_reason = '止盈'
                elif side == 'SHORT' and cp <= tp: closed = True; close_price = tp; close_reason = '止盈'
            if closed:
                if side == 'LONG': final_pct = (close_price - entry) / entry
                else: final_pct = (entry - close_price) / entry
                lev_pct = final_pct * CONFIG['trading']['leverage']
                pnl = pos['margin'] * lev_pct
                self.balance += pos['margin'] + pnl; self.daily_pnl += pnl
                self.closed_trades.append({**pos, 'close_time': datetime.now().isoformat(), 'close_price': close_price, 'close_reason': close_reason, 'pnl': round(pnl, 4), 'pnl_pct': round(lev_pct * 100, 2), 'balance_after': round(self.balance, 2)})
                emoji = '🟢' if pnl > 0 else '🔴'
                log.info(f"{emoji} {close_reason} {symbol} {side}: 入场={entry} 平仓={close_price} 盈亏={pnl:.2f}U({lev_pct*100:.2f}%)")
                notify_trade_close(symbol, side, entry, close_price, close_reason, pnl, lev_pct * 100)
                del self.positions[symbol]; self._save_state()

    def get_stats(self):
        total = len(self.closed_trades)
        if total == 0:
            return {'total_trades': 0, 'win_rate': 0, 'win_count': 0, 'loss_count': 0, 'total_pnl': 0, 'avg_pnl_pct': 0, 'max_win': 0, 'max_loss': 0, 'balance': round(self.balance, 2), 'pnl_total': round(self.balance - self.initial_balance, 2), 'target_reached': False}
        wins = [t for t in self.closed_trades if t['pnl'] > 0]; losses = [t for t in self.closed_trades if t['pnl'] <= 0]
        return {'total_trades': total, 'win_rate': round(len(wins)/total*100, 1), 'win_count': len(wins), 'loss_count': len(losses), 'total_pnl': round(sum(t['pnl'] for t in self.closed_trades), 2), 'avg_pnl_pct': round(sum(t['pnl_pct'] for t in self.closed_trades)/total, 2), 'max_win': round(max(t['pnl'] for t in self.closed_trades), 2), 'max_loss': round(min(t['pnl'] for t in self.closed_trades), 2), 'balance': round(self.balance, 2), 'pnl_total': round(self.balance - self.initial_balance, 2), 'target_reached': total >= 100 and len(wins)/total >= 0.8}

    def analyze_symbol(self, symbol):
        tf = CONFIG['trading']['timeframes']
        log.info(f"📊 分析 {symbol} ...")
        time.sleep(API_DELAY_BETWEEN_REQUESTS)
        df_15m = self.ex.get_klines(symbol, tf['direction'], 100)
        time.sleep(API_DELAY_BETWEEN_REQUESTS)
        df_5m = self.ex.get_klines(symbol, tf['entry'], 200)
        time.sleep(API_DELAY_BETWEEN_REQUESTS)
        df_1m = self.ex.get_klines(symbol, tf['confirm'], 200)
        df_15m = self.ta.full_analysis(df_15m); df_5m = self.ta.full_analysis(df_5m); df_1m = self.ta.full_analysis(df_1m)
        signal = self.sg.generate_signal(df_15m, df_5m, df_1m)
        last = df_5m.iloc[-1]
        signal['symbol'] = symbol; signal['price'] = float(last['close']); signal['boll_position'] = float(last['boll_position'])
        signal['kdj_k'] = float(last['kdj_k']); signal['macd_above_zero'] = bool(last['macd_above_zero'])
        signal['rsi'] = float(last['rsi']); signal['vol_ratio'] = float(last['vol_ratio'])
        return signal

    def execute_signal(self, signal, current_prices):
        symbol = signal['symbol']; action = signal['action']; price = signal['price']
        risk = CONFIG['trading']['risk']; max_positions = CONFIG['trading'].get('max_positions', 3)
        if self._check_circuit_breaker():
            log.warning(f"🚨 日亏损熔断中，跳过 {symbol}"); return
        has_position = symbol in self.positions
        if len(self.positions) >= max_positions and action in ('LONG', 'SHORT') and not has_position:
            log.info(f"⚠️ 满仓({max_positions})，跳过 {symbol}"); return
        min_strength = self.adaptive.min_signal_strength
        if signal['strength'] < min_strength:
            log.info(f"⏸️ {symbol} 强度{signal['strength']}<阈值{min_strength}"); return

        # AI建议的杠杆和止盈止损
        ai_lev = signal.get('ai_leverage', CONFIG['trading']['leverage'])
        ai_sl = signal.get('ai_sl', risk['stop_loss_pct'])
        ai_tp = signal.get('ai_tp', risk['take_profit_pct'])
        leverage = min(ai_lev, 20)  # 安全上限

        if action == 'LONG' and not has_position:
            margin = self.balance * risk['max_position_pct']
            if margin < 1: return
            sp = round(price * (1 - ai_sl), 2); tp = round(price * (1 + ai_tp), 2)
            log.info(f"🟢 [纸面] 做多 {symbol} 强度={signal['strength']} 杠杆={leverage}x 原因={signal['reason']}")
            log.info(f"   入场={price} 止损={sp} 止盈={tp} 保证金={margin:.2f}U")
            self.positions[symbol] = {'side': 'LONG', 'entry': price, 'stop_loss': sp, 'take_profit': tp, 'margin': margin, 'amount': margin * leverage / price, 'time': datetime.now().isoformat(), 'reason': signal['reason'], 'strength': signal['strength'], 'leverage': leverage, 'trailing_active': False, 'max_pnl_pct': 0}
            self.balance -= margin; self._save_state()
            notify_trade_open(symbol, 'LONG', price, signal['strength'], signal['reason'], sp, tp, margin, leverage)
        elif action == 'SHORT' and not has_position:
            margin = self.balance * risk['max_position_pct']
            if margin < 1: return
            sp = round(price * (1 + ai_sl), 2); tp = round(price * (1 - ai_tp), 2)
            log.info(f"🔴 [纸面] 做空 {symbol} 强度={signal['strength']} 杠杆={leverage}x 原因={signal['reason']}")
            log.info(f"   入场={price} 止损={sp} 止盈={tp} 保证金={margin:.2f}U")
            self.positions[symbol] = {'side': 'SHORT', 'entry': price, 'stop_loss': sp, 'take_profit': tp, 'margin': margin, 'amount': margin * leverage / price, 'time': datetime.now().isoformat(), 'reason': signal['reason'], 'strength': signal['strength'], 'leverage': leverage, 'trailing_active': False, 'max_pnl_pct': 0}
            self.balance -= margin; self._save_state()
            notify_trade_open(symbol, 'SHORT', price, signal['strength'], signal['reason'], sp, tp, margin, leverage)

    def scan_and_trade(self):
        self.scan_count += 1; self._reset_daily()
        log.info("=" * 60)
        log.info(f"🔄 [v2.1+AI] 第{self.scan_count}次扫描 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        stats = self.get_stats()
        log.info(f"💰 余额: {self.balance:.2f}U | 已平仓: {stats['total_trades']}笔 | 胜率: {stats['win_rate']}% | 日盈亏: {self.daily_pnl:.2f}U")

        all_symbols = list(CONFIG['trading']['symbols'])
        if self.scan_count % 5 == 0:
            try:
                hot = self.hot_scanner.scan_hot_symbols()
                if hot: all_symbols.extend(hot)
            except: pass

        current_prices = {}
        for symbol in all_symbols:
            try:
                ticker = self.ex.exchange.fetch_ticker(symbol)
                current_prices[symbol] = float(ticker['last'])
            except: pass
            time.sleep(0.2)
        self._check_positions(current_prices)

        for i, symbol in enumerate(all_symbols):
            try:
                signal = self.analyze_symbol(symbol)
                if signal['action'] in ('LONG', 'SHORT'):
                    # AI增强决策（每3次扫描触发，节省API）
                    if self.scan_count % 3 == 0:
                        try:
                            ai_result = ai_enhanced_decision(symbol, signal, self)
                            if ai_result['action'] != signal['action']:
                                log.info(f"🤖 AI修正: {symbol} {signal['action']}→{ai_result['action']} (强度{signal['strength']}→{ai_result['strength']})")
                                signal['action'] = ai_result['action']; signal['strength'] = ai_result['strength']
                            if ai_result.get('leverage_override'): signal['ai_leverage'] = ai_result['leverage_override']
                            if ai_result.get('stop_loss_override'): signal['ai_sl'] = ai_result['stop_loss_override']
                            if ai_result.get('take_profit_override'): signal['ai_tp'] = ai_result['take_profit_override']
                        except Exception as e:
                            log.warning(f"🤖 AI增强失败(不影响交易): {e}")
                    signal = self.news.adjust_signal(signal, symbol)
                else:
                    signal['news_sentiment'] = 0.0

                extras = f" RSI={signal['rsi']:.0f} Vol={signal['vol_ratio']:.1f}x"
                log.info(f"  {symbol}: {signal['action']} 强度={signal['strength']} 价格={signal['price']:.2f} BOLL={signal['boll_position']:.2f} KDJ.K={signal['kdj_k']:.1f}{extras}" + (f" 消息面={signal.get('news_sentiment',0):.2f}" if signal['action'] != 'HOLD' else ""))
                if signal['action'] in ('LONG', 'SHORT'):
                    self.execute_signal(signal, current_prices)
            except Exception as e:
                log.error(f"❌ {symbol} 分析失败: {e}")
            if i < len(all_symbols) - 1:
                time.sleep(API_DELAY_BETWEEN_SYMBOLS)

        self.adaptive.adjust(self)

        # AI复盘（每10次扫描）
        if self.scan_count % 10 == 0 and stats['total_trades'] >= 5:
            try:
                close_reasons = defaultdict(int)
                for t in self.closed_trades: close_reasons[t.get('close_reason', '未知')] += 1
                lw = sum(1 for t in self.closed_trades if t['side'] == 'LONG' and t['pnl'] > 0)
                lt = sum(1 for t in self.closed_trades if t['side'] == 'LONG')
                sw = sum(1 for t in self.closed_trades if t['side'] == 'SHORT' and t['pnl'] > 0)
                st = sum(1 for t in self.closed_trades if t['side'] == 'SHORT')
                sp = defaultdict(lambda: {'w': 0, 'l': 0, 'pnl': 0})
                for t in self.closed_trades:
                    s = t['symbol']
                    if t['pnl'] > 0: sp[s]['w'] += 1
                    else: sp[s]['l'] += 1
                    sp[s]['pnl'] += t['pnl']
                sym_str = '\n'.join([f"  {s}: {d['w']}W/{d['l']}L PnL={d['pnl']:.2f}U" for s, d in sorted(sp.items(), key=lambda x: x[1]['pnl'], reverse=True)])
                review = review_trades({'total_trades': stats['total_trades'], 'win_rate': stats['win_rate'], 'pnl_total': stats['pnl_total'], 'max_win': stats['max_win'], 'max_loss': stats['max_loss'], 'long_win_rate': round(lw/lt*100,1) if lt else 0, 'short_win_rate': round(sw/st*100,1) if st else 0, 'symbol_performance': sym_str, 'current_positions': ', '.join([f"{s}:{p['side']}@{p['entry']}" for s,p in self.positions.items()]) or '无', 'close_reasons': dict(close_reasons)})
                if review.get('should_adjust_threshold') and 50 <= review.get('recommended_threshold', 60) <= 80:
                    self.adaptive.min_signal_strength = review['recommended_threshold']
                    log.info(f"🤖 AI建议阈值→{review['recommended_threshold']}")
            except Exception as e:
                log.warning(f"🤖 AI复盘失败: {e}")

        macro = self.news._check_macro_sentiment()
        if macro < -0.5: log.warning(f"⚠️ 宏观消息面极度利空(={macro:.2f})")
        stats = self.get_stats()
        log.info(f"📊 {stats['total_trades']}笔 | 胜率{stats['win_rate']}% | 盈亏{stats['pnl_total']}U | 阈值={self.adaptive.min_signal_strength}")
        self._save_state(); log.info("🔄 扫描结束")
        return stats

    def run_loop(self, interval_seconds=300):
        log.info(f"🚀 [v2.1+AI] 启动，每{interval_seconds}秒扫描")
        log.info(f"   初始: {self.initial_balance}U | 特性: RSI+移动止盈+日亏熔断+自适应+DeepSeek AI(4 Agent)")
        while True:
            try: self.scan_and_trade()
            except Exception as e: log.error(f"❌ 异常: {e}\n{traceback.format_exc()}")
            time.sleep(interval_seconds)


# ─── CLI ───
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='缠论短线OKX交易机器人 v2.1+AI')
    parser.add_argument('--paper', action='store_true'); parser.add_argument('--paper-loop', action='store_true')
    parser.add_argument('--stats', action='store_true'); parser.add_argument('--balance', action='store_true')
    parser.add_argument('--positions', action='store_true')
    parser.add_argument('--scan', action='store_true'); parser.add_argument('--loop', action='store_true')
    parser.add_argument('--interval', type=int, default=300)
    parser.add_argument('--capital', type=float, default=1000.0)
    args = parser.parse_args()
    ex = OKXExchange()

    if args.stats:
        bot = PaperTradingBot(ex, args.capital); s = bot.get_stats()
        print(f"\n📊 统计 (v2.1+AI): {s['total_trades']}笔 胜率{s['win_rate']}% 盈亏{s['pnl_total']}U")
    elif args.paper: PaperTradingBot(ex, args.capital).scan_and_trade()
    elif args.paper_loop: PaperTradingBot(ex, args.capital).run_loop(args.interval)
    elif args.balance: print(json.dumps(ex.get_balance(), indent=2))
    elif args.positions:
        for sym in CONFIG['trading']['symbols']:
            for p in ex.get_positions(sym):
                print(f"  {p['symbol']}: {p['side']} PnL={p['unrealized_pnl']}")
    elif args.scan: TradingBot(ex).scan_and_trade()
    elif args.loop: TradingBot(ex).run_loop(args.interval)
    else:
        print("用法: python trader_v2.py --paper-loop [--interval 300]")
