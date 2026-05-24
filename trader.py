#!/usr/bin/env python3
"""
缠论+MACD+BOLL+KDJ 欧意(OKX)合约短线交易系统
核心框架：15分钟定方向 → 5分钟找背驰 → 1分钟确认入场
支持纸面交易(paper)模式：真实行情+虚拟下单，验证策略胜率
"""

import json
import os
import time
import logging
import subprocess
import re
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

import ccxt
import pandas as pd
import ta

# ─── 日志配置 ───
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / 'trading.log', encoding='utf-8')
    ]
)
log = logging.getLogger(__name__)

# ─── 加载配置 ───
CONFIG_PATH = Path(__file__).parent / 'config.json'
with open(CONFIG_PATH, 'r') as f:
    CONFIG = json.load(f)

# 纸面交易记录文件
PAPER_TRADE_FILE = Path(__file__).parent / 'paper_trades.json'


class OKXExchange:
    """欧意(OKX)交易所连接器"""

    def __init__(self, api_key=None, api_secret=None, passphrase=None, testnet=True):
        self.exchange = ccxt.okx({
            'apiKey': api_key or CONFIG['okx']['api_key'],
            'secret': api_secret or CONFIG['okx']['api_secret'],
            'password': passphrase or CONFIG['okx']['passphrase'],
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',
            },
            'proxies': {
                'http': 'http://127.0.0.1:7890',
                'https': 'http://127.0.0.1:7890',
            },
        })
        if testnet and CONFIG['okx'].get('testnet', False):
            self.exchange.set_sandbox_mode(True)
            log.info("🔧 已启用OKX模拟盘模式(Demo Trading)")
        else:
            log.info("🔧 已启用OKX实盘模式")

    def get_balance(self):
        """获取USDT余额"""
        balance = self.exchange.fetch_balance()
        usdt = balance.get('USDT', {})
        return {
            'total': float(usdt.get('total', 0)),
            'free': float(usdt.get('free', 0)),
            'used': float(usdt.get('used', 0)),
        }

    def get_klines(self, symbol, timeframe, limit=200):
        """获取K线数据"""
        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.astype({'open': float, 'high': float, 'low': float, 'close': float, 'volume': float})
        return df

    def set_leverage(self, symbol, leverage):
        """设置杠杆"""
        try:
            pos_mode = CONFIG['trading'].get('position_mode', 'net')
            if pos_mode == 'net':
                params = {'mgnMode': 'isolated'}
            else:
                params = {'mgnMode': 'isolated', 'posSide': 'long'}
            self.exchange.set_leverage(leverage, symbol, params=params)
            if pos_mode != 'net':
                params_short = {'mgnMode': 'isolated', 'posSide': 'short'}
                self.exchange.set_leverage(leverage, symbol, params=params_short)
            log.info(f"📊 {symbol} 杠杆设置为 {leverage}x")
        except Exception as e:
            log.warning(f"设置杠杆失败: {e}")

    def place_market_order(self, symbol, side, amount):
        """市价下单（开仓）"""
        try:
            pos_mode = CONFIG['trading'].get('position_mode', 'net')
            params = {'tdMode': 'isolated'}
            if pos_mode != 'net':
                params['posSide'] = 'long' if side == 'buy' else 'short'
            order = self.exchange.create_order(symbol, 'market', side, amount, params=params)
            log.info(f"✅ 市价{side} {symbol} 数量{amount} 成交价≈{order.get('average', 'N/A')}")
            return order
        except Exception as e:
            log.error(f"❌ 下单失败: {e}")
            return None

    def place_stop_loss(self, symbol, side, amount, stop_price):
        """设置止损单"""
        try:
            pos_mode = CONFIG['trading'].get('position_mode', 'net')
            close_side = 'sell' if side == 'buy' else 'buy'
            params = {
                'tdMode': 'isolated',
                'ordType': 'conditional',
                'triggerPx': str(stop_price),
                'triggerPxType': 'last',
                'orderPx': '-1',
            }
            if pos_mode != 'net':
                params['posSide'] = 'long' if close_side == 'sell' else 'short'
            else:
                params['posSide'] = 'net'
            order = self.exchange.create_order(symbol, 'market', close_side, amount, params=params)
            log.info(f"🛡️ 止损单: {symbol} 触发价={stop_price}")
            return order
        except Exception as e:
            log.error(f"❌ 止损单失败: {e}")
            return None

    def place_take_profit(self, symbol, side, amount, take_price):
        """设置止盈单"""
        try:
            pos_mode = CONFIG['trading'].get('position_mode', 'net')
            close_side = 'sell' if side == 'buy' else 'buy'
            params = {
                'tdMode': 'isolated',
                'ordType': 'conditional',
                'triggerPx': str(take_price),
                'triggerPxType': 'last',
                'orderPx': '-1',
            }
            if pos_mode != 'net':
                params['posSide'] = 'long' if close_side == 'sell' else 'short'
            else:
                params['posSide'] = 'net'
            order = self.exchange.create_order(symbol, 'market', close_side, amount, params=params)
            log.info(f"🎯 止盈单: {symbol} 触发价={take_price}")
            return order
        except Exception as e:
            log.error(f"❌ 止盈单失败: {e}")
            return None

    def place_order_with_tp_sl(self, symbol, side, amount, stop_loss_price, take_profit_price):
        """开仓同时带止盈止损"""
        try:
            pos_mode = CONFIG['trading'].get('position_mode', 'net')
            params = {'tdMode': 'isolated'}
            if pos_mode != 'net':
                params['posSide'] = 'long' if side == 'buy' else 'short'
            params['attachAlgoOrds'] = [{
                'slTriggerPx': str(stop_loss_price),
                'slOrdPx': '-1',
                'tpTriggerPx': str(take_profit_price),
                'tpOrdPx': '-1',
            }]
            order = self.exchange.create_order(symbol, 'market', side, amount, params=params)
            log.info(f"✅ 市价{side} {symbol} 数量{amount} (带止盈止损)")
            return order
        except Exception as e:
            log.error(f"❌ 带止盈止损下单失败: {e}")
            return None

    def get_positions(self, symbol=None):
        """获取持仓"""
        positions = self.exchange.fetch_positions(symbols=[symbol] if symbol else None)
        result = []
        for p in positions:
            amt = float(p.get('contracts', 0) or 0)
            if amt > 0:
                result.append({
                    'symbol': p['symbol'],
                    'side': p['side'],
                    'amount': amt,
                    'entry_price': float(p.get('entryPrice', 0)),
                    'unrealized_pnl': float(p.get('unrealizedPnl', 0)),
                    'leverage': p.get('leverage', 1),
                })
        return result

    def close_position(self, symbol, side, amount):
        """平仓"""
        try:
            pos_mode = CONFIG['trading'].get('position_mode', 'net')
            close_side = 'sell' if side == 'long' else 'buy'
            params = {
                'tdMode': 'isolated',
                'reduceOnly': True,
            }
            if pos_mode != 'net':
                params['posSide'] = side
            order = self.exchange.create_order(symbol, 'market', close_side, amount, params=params)
            log.info(f"🔒 平仓 {symbol} {side} 数量{amount}")
            return order
        except Exception as e:
            log.error(f"❌ 平仓失败: {e}")
            return None

    def cancel_all_orders(self, symbol):
        """取消某标的所有挂单"""
        try:
            self.exchange.cancel_all_orders(symbol)
            log.info(f"🗑️ 已取消 {symbol} 所有挂单")
        except Exception as e:
            log.warning(f"取消挂单失败: {e}")


class TechnicalAnalysis:
    """技术指标计算：MACD + BOLL + KDJ + 缠论辅助"""

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
        k = [50.0]
        d = [50.0]
        for i in range(1, len(rsv)):
            k.append(2/3 * k[-1] + 1/3 * rsv.iloc[i])
            d.append(2/3 * d[-1] + 1/3 * k[-1])
        df['kdj_k'] = k
        df['kdj_d'] = d
        df['kdj_j'] = 3 * pd.Series(k) - 2 * pd.Series(d)
        df['kdj_golden_cross'] = (df['kdj_k'] > df['kdj_d']) & (df['kdj_k'].shift(1) <= df['kdj_d'].shift(1))
        df['kdj_death_cross'] = (df['kdj_k'] < df['kdj_d']) & (df['kdj_k'].shift(1) >= df['kdj_d'].shift(1))
        df['kdj_overbought'] = df['kdj_k'] > 80
        df['kdj_oversold'] = df['kdj_k'] < 20
        return df

    @staticmethod
    def calc_chanlun_simple(df):
        df = df.copy()
        df['hl_processed'] = False
        for i in range(1, len(df) - 1):
            if df.iloc[i]['high'] <= df.iloc[i-1]['high'] and df.iloc[i]['low'] >= df.iloc[i-1]['low']:
                if i >= 2 and df.iloc[i-2]['high'] < df.iloc[i-1]['high']:
                    df.iat[i, df.columns.get_loc('high')] = max(df.iloc[i]['high'], df.iloc[i-1]['high'])
                    df.iat[i, df.columns.get_loc('low')] = max(df.iloc[i]['low'], df.iloc[i-1]['low'])
                else:
                    df.iat[i, df.columns.get_loc('high')] = min(df.iloc[i]['high'], df.iloc[i-1]['high'])
                    df.iat[i, df.columns.get_loc('low')] = min(df.iloc[i]['low'], df.iloc[i-1]['low'])
        df['fractal_top'] = False
        df['fractal_bottom'] = False
        for i in range(1, len(df) - 1):
            if df.iloc[i]['high'] > df.iloc[i-1]['high'] and df.iloc[i]['high'] > df.iloc[i+1]['high']:
                df.iat[i, df.columns.get_loc('fractal_top')] = True
            if df.iloc[i]['low'] < df.iloc[i-1]['low'] and df.iloc[i]['low'] < df.iloc[i+1]['low']:
                df.iat[i, df.columns.get_loc('fractal_bottom')] = True
        df['macd_hist_abs'] = df['macd_hist'].abs()
        df['macd_area'] = df['macd_hist_abs'].rolling(5).sum()
        df['divergence_bull'] = (
            (df['low'] < df['low'].shift(10)) &
            (df['macd_area'] < df['macd_area'].shift(10)) &
            (df['macd_hist'] < 0)
        )
        df['divergence_bear'] = (
            (df['high'] > df['high'].shift(10)) &
            (df['macd_area'] < df['macd_area'].shift(10)) &
            (df['macd_hist'] > 0)
        )
        return df

    @classmethod
    def full_analysis(cls, df):
        df = cls.calc_macd(df)
        df = cls.calc_boll(df)
        df = cls.calc_kdj(df)
        df = cls.calc_chanlun_simple(df)
        return df


class SignalGenerator:
    """缠论+三指标共振信号生成器"""

    @staticmethod
    def generate_signal(df_15m, df_5m, df_1m):
        signal = {'action': 'HOLD', 'strength': 0, 'reason': ''}

        last_15m = df_15m.iloc[-1]

        if (last_15m['boll_mid_slope'] > 0 and
            last_15m['close'] > last_15m['boll_mid'] and
            last_15m['macd_above_zero']):
            trend = 'UP'
        elif (last_15m['boll_mid_slope'] < 0 and
              last_15m['close'] < last_15m['boll_mid'] and
              not last_15m['macd_above_zero']):
            trend = 'DOWN'
        else:
            trend = 'NEUTRAL'

        last_5m = df_5m.iloc[-1]
        prev_5m = df_5m.iloc[-2]

        bull_divergence_5m = last_5m['divergence_bull']
        bear_divergence_5m = last_5m['divergence_bear']
        macd_golden_5m = last_5m['macd_golden_cross']
        macd_death_5m = last_5m['macd_death_cross']
        boll_near_lower_5m = last_5m['boll_position'] < 0.2
        boll_near_upper_5m = last_5m['boll_position'] > 0.8
        boll_mid_support_5m = (prev_5m['close'] < prev_5m['boll_mid'] and
                               last_5m['close'] > last_5m['boll_mid'])
        kdj_golden_5m = last_5m['kdj_golden_cross']
        kdj_death_5m = last_5m['kdj_death_cross']
        kdj_oversold_5m = last_5m['kdj_k'] < 30
        kdj_overbought_5m = last_5m['kdj_k'] > 70

        last_1m = df_1m.iloc[-1]
        kdj_golden_1m = last_1m['kdj_golden_cross']
        kdj_death_1m = last_1m['kdj_death_cross']
        macd_golden_1m = last_1m['macd_golden_cross']
        macd_death_1m = last_1m['macd_death_cross']
        fractal_bottom_1m = last_1m['fractal_bottom']
        fractal_top_1m = last_1m['fractal_top']

        # 做多
        long_score = 0
        long_reasons = []
        if trend == 'UP':
            long_score += 30; long_reasons.append('15m趋势向上')
        elif trend == 'NEUTRAL':
            long_score += 10; long_reasons.append('15m趋势中性')
        if macd_golden_5m and last_5m['macd_above_zero']:
            long_score += 25; long_reasons.append('5m零轴上金叉')
        elif macd_golden_5m:
            long_score += 15; long_reasons.append('5m金叉')
        if bull_divergence_5m:
            long_score += 20; long_reasons.append('5m底背驰')
        if boll_near_lower_5m or boll_mid_support_5m:
            long_score += 15; long_reasons.append('5m BOLL下轨/回踩中轨')
        elif last_5m['boll_position'] < 0.35:
            long_score += 8; long_reasons.append('5m BOLL偏低')
        if kdj_golden_5m and kdj_oversold_5m:
            long_score += 15; long_reasons.append('5m KDJ低位金叉')
        elif kdj_golden_5m:
            long_score += 8; long_reasons.append('5m KDJ金叉')
        # 低位KDJ拐头（J<0后回升，强买入信号）
        if last_5m['kdj_j'] > 20 and prev_5m.get('kdj_j', 50) < 20:
            long_score += 12; long_reasons.append('5m KDJ低位拐头')
        if kdj_golden_1m or macd_golden_1m or fractal_bottom_1m:
            long_score += 10; long_reasons.append('1m确认信号')

        # 做空
        short_score = 0
        short_reasons = []
        if trend == 'DOWN':
            short_score += 30; short_reasons.append('15m趋势向下')
        elif trend == 'NEUTRAL':
            short_score += 10; short_reasons.append('15m趋势中性')
        if macd_death_5m and not last_5m['macd_above_zero']:
            short_score += 25; short_reasons.append('5m零轴下死叉')
        elif macd_death_5m:
            short_score += 15; short_reasons.append('5m死叉')
        if bear_divergence_5m:
            short_score += 20; short_reasons.append('5m顶背驰')
        if boll_near_upper_5m:
            short_score += 15; short_reasons.append('5m BOLL上轨')
        elif last_5m['boll_position'] > 0.7:
            short_score += 8; short_reasons.append('5m BOLL偏高')
        if kdj_death_5m and kdj_overbought_5m:
            short_score += 15; short_reasons.append('5m KDJ高位死叉')
        elif kdj_death_5m:
            short_score += 8; short_reasons.append('5m KDJ死叉')
        # 高位KDJ拐头（J>100后回落，强卖出信号）
        if last_5m['kdj_j'] < 80 and prev_5m.get('kdj_j', 50) > 80:
            short_score += 12; short_reasons.append('5m KDJ高位拐头')
        if kdj_death_1m or macd_death_1m or fractal_top_1m:
            short_score += 10; short_reasons.append('1m确认信号')

        if long_score >= 60 and long_score > short_score:
            signal['action'] = 'LONG'
            signal['strength'] = min(long_score, 100)
            signal['reason'] = ' | '.join(long_reasons)
        elif short_score >= 60 and short_score > long_score:
            signal['action'] = 'SHORT'
            signal['strength'] = min(short_score, 100)
            signal['reason'] = ' | '.join(short_reasons)

        return signal


class NewsSentimentAnalyzer:
    """消息面情绪分析：通过搜索新闻判断多空倾向，调整信号分数"""

    # 关键词情绪映射 - 分三级影响力
    # 高冲击：能直接改变行情走向（黑天鹅级别）
    HIGH_IMPACT_BEAR = {
        'hack', 'exploit', 'rug pull', 'sec lawsuit', 'ban crypto',
        'exchange collapsed', 'delisted', 'flash crash',
        '暴跌', '崩盘', '黑客攻击', '跑路', '下架', '封禁',
    }
    HIGH_IMPACT_BULL = {
        'etf approved', 'institutional buying', 'scc approval',
        'major partnership', 'listed on', 'whale accumulation massive',
        'ETF通过', '获批', '上线', '巨鲸增持',
    }
    # 中冲击：明显影响短期走势（鲸鱼/名人/政策）
    MED_IMPACT_BEAR = {
        'whale dump', 'whale sell', 'ceo sells', 'regulation crackdown',
        'sec investigation', 'country ban', 'elon sells', 'sells bitcoin',
        '巨鲸抛售', '抛售', '监管', '调查', '罚款',
    }
    MED_IMPACT_BULL = {
        'whale buy', 'whale accumulation', 'partnership announced',
        'upgrade completed', 'halving', 'elon buys', 'musk crypto',
        'country adopts', 'treasury buys',
        '巨鲸买入', '增持', '合作', '升级', '减半', '采用',
    }
    # 低冲击：一般性新闻
    LOW_IMPACT_BEAR = {
        'bearish', 'dump', 'plunge', 'correction', 'pullback',
        '利空', '下跌', '回调',
    }
    LOW_IMPACT_BULL = {
        'bullish', 'surge', 'rally', 'breakout', 'soar',
        '利好', '上涨', '突破',
    }
    # 币种名映射（用于搜索）
    SYMBOL_NAMES = {
        'BTC/USDT:USDT': ['Bitcoin', 'BTC'],
        'ETH/USDT:USDT': ['Ethereum', 'ETH'],
        'SOL/USDT:USDT': ['Solana', 'SOL'],
        'DOGE/USDT:USDT': ['Dogecoin', 'DOGE'],
        'XRP/USDT:USDT': ['XRP', 'Ripple'],
        'ADA/USDT:USDT': ['Cardano', 'ADA'],
        'AVAX/USDT:USDT': ['Avalanche', 'AVAX'],
        'LINK/USDT:USDT': ['Chainlink', 'LINK'],
        'DOT/USDT:USDT': ['Polkadot', 'DOT'],
        'UNI/USDT:USDT': ['Uniswap', 'UNI'],
        'APT/USDT:USDT': ['Aptos', 'APT'],
        'ARB/USDT:USDT': ['Arbitrum', 'ARB'],
        'OP/USDT:USDT': ['Optimism', 'OP'],
        'PEPE/USDT:USDT': ['PEPE'],
        'WIF/USDT:USDT': ['dogwifhat', 'WIF'],
        'FIL/USDT:USDT': ['Filecoin', 'FIL'],
        'NEAR/USDT:USDT': ['NEAR Protocol', 'NEAR'],
        'SUI/USDT:USDT': ['Sui', 'SUI'],
    }

    def __init__(self):
        self.cache = {}  # symbol -> (timestamp, sentiment)
        self.cache_ttl = 300  # 缓存5分钟

    def _search_news(self, query):
        """通过curl调用搜索API获取新闻"""
        try:
            result = subprocess.run(
                ['curl', '-s', '-x', 'http://127.0.0.1:7890',
                 '--max-time', '8',
                 f'https://www.google.com/search?q={query}&tbm=nws&tbs=qdr:d&num=5'],
                capture_output=True, text=True, timeout=12
            )
            text = result.stdout.lower() if result.stdout else ''
            # 去除HTML标签，只保留文本内容
            text = re.sub(r'<[^>]+>', ' ', text)
            # 去除script和style内容
            text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            return text
        except Exception:
            return ''

    def _check_macro_sentiment(self):
        """检查加密市场整体消息面（用于判断系统性风险）"""
        try:
            text = self._search_news('crypto+market+today')
            if not text:
                return 0.0
            bull_count = sum(1 for kw in ['surge', 'rally', 'bullish', 'breakout', '上涨', '暴涨', '利好'] if kw in text)
            bear_count = sum(1 for kw in ['crash', 'dump', 'bearish', 'plunge', '暴跌', '崩盘', '利空'] if kw in text)
            total = bull_count + bear_count
            if total > 0:
                return max(-1.0, min(1.0, (bull_count - bear_count) / total))
            return 0.0
        except Exception:
            return 0.0

    def analyze_symbol(self, symbol):
        """分析单个币种的消息面情绪，返回 -1 到 +1 的分数 + 影响级别"""
        now = time.time()
        if symbol in self.cache:
            ts, sent = self.cache[symbol]
            if now - ts < self.cache_ttl:
                return sent

        names = self.SYMBOL_NAMES.get(symbol, [symbol.split('/')[0]])
        sentiment = 0.0
        impact_level = 'LOW'  # LOW / MED / HIGH
        hits = 0

        for name in names:
            text = self._search_news(f'{name}+crypto+news')
            if not text:
                continue

            # 三级检测：高冲击优先
            high_bull = sum(1 for kw in self.HIGH_IMPACT_BULL if kw in text)
            high_bear = sum(1 for kw in self.HIGH_IMPACT_BEAR if kw in text)
            med_bull = sum(1 for kw in self.MED_IMPACT_BULL if kw in text)
            med_bear = sum(1 for kw in self.MED_IMPACT_BEAR if kw in text)
            low_bull = sum(1 for kw in self.LOW_IMPACT_BULL if kw in text)
            low_bear = sum(1 for kw in self.LOW_IMPACT_BEAR if kw in text)

            # 加权计算：高冲击3x，中冲击2x，低冲击1x
            bull_score = high_bull * 3 + med_bull * 2 + low_bull * 1
            bear_score = high_bear * 3 + med_bear * 2 + low_bear * 1
            total = bull_score + bear_score

            if total > 0:
                # 方向分 -1~+1
                s = (bull_score - bear_score) / total
                sentiment += s
                hits += 1
                # 记录最高影响级别
                if high_bull > 0 or high_bear > 0:
                    impact_level = 'HIGH'
                elif (med_bull > 0 or med_bear > 0) and impact_level != 'HIGH':
                    impact_level = 'MED'

        if hits > 0:
            sentiment = max(-1.0, min(1.0, sentiment / hits))

        # 缓存带影响级别
        self.cache[symbol] = (now, (sentiment, impact_level))
        direction = '利好' if sentiment > 0.2 else ('利空' if sentiment < -0.2 else '中性')
        log.info(f"📰 {symbol} 消息面: {direction} (情绪分={sentiment:.2f}, 影响={impact_level})")
        return (sentiment, impact_level)

    def adjust_signal(self, signal, symbol):
        """根据消息面调整信号：以技术面为基础，消息面按影响级别调整
        
        调整规则：
        - LOW影响：±3分，不改变方向
        - MED影响（巨鲸/名人/政策）：±10分，可改变强度但不压到HOLD
        - HIGH影响（黑天鹅/重大事件）：±15分，可以压到HOLD，因为这种行情技术面已失效
        """
        result = self.analyze_symbol(symbol)
        if isinstance(result, tuple):
            sentiment, impact_level = result
        else:
            sentiment, impact_level = result, 'LOW'

        if signal['action'] == 'HOLD' or sentiment == 0:
            signal['news_sentiment'] = round(sentiment, 2)
            signal['news_impact'] = impact_level
            return signal

        # 根据影响级别确定调整幅度
        if impact_level == 'HIGH':
            max_adjust = 15
        elif impact_level == 'MED':
            max_adjust = 10
        else:
            max_adjust = 3

        adjustment = int(sentiment * max_adjust)
        adjustment = max(-max_adjust, min(max_adjust, adjustment))

        old_strength = signal['strength']
        old_action = signal['action']

        if impact_level == 'HIGH':
            # 高冲击：可以压到HOLD（黑天鹅时技术面失效）
            signal['strength'] = max(0, min(100, signal['strength'] + adjustment))
            if signal['strength'] < CONFIG['trading'].get('min_signal_strength', 60):
                signal['action'] = 'HOLD'
                log.warning(f"📰⚡ 高冲击消息面：{symbol} {old_action}→HOLD (情绪分={sentiment:.2f}, 强度{old_strength}→{signal['strength']})")
        elif impact_level == 'MED':
            # 中冲击：显著调整但不压到HOLD
            signal['strength'] = max(55, min(100, signal['strength'] + adjustment))
            direction = '↑' if adjustment > 0 else '↓'
            log.info(f"📰🔶 中冲击消息面：{symbol} 信号强度 {old_strength}→{signal['strength']} ({direction}{abs(adjustment)})")
        else:
            # 低冲击：微调
            signal['strength'] = max(55, min(100, signal['strength'] + adjustment))
            if abs(adjustment) > 0:
                log.info(f"📰 低冲击消息面：{symbol} 信号强度 {old_strength}→{signal['strength']} ({'↑' if adjustment > 0 else '↓'}{abs(adjustment)})")

        # 标记消息面警告
        if sentiment < -0.5 and signal['action'] != 'HOLD':
            signal['reason'] = signal.get('reason', '') + f' | ⚠️消息面{impact_level}级偏空'
        elif sentiment > 0.5 and signal['action'] != 'HOLD':
            signal['reason'] = signal.get('reason', '') + f' | 📢消息面{impact_level}级偏多'

        signal['news_sentiment'] = round(sentiment, 2)
        signal['news_impact'] = impact_level
        return signal


class HotSymbolScanner:
    """动态发现OKX高热度/高涨幅币种"""

    def __init__(self, exchange: OKXExchange):
        self.ex = exchange

    def scan_hot_symbols(self, min_volume_usdt=5000000, top_n=5):
        """扫描OKX上成交额和涨幅排名靠前的合约币种"""
        try:
            markets = self.ex.exchange.fetch_markets()
            swap_symbols = [m['symbol'] for m in markets
                           if m.get('swap', False) and m.get('active', True)
                           and '/USDT:' in m['symbol']]

            hot = []
            batch_size = 10
            for i in range(0, min(len(swap_symbols), 50), batch_size):
                batch = swap_symbols[i:i+batch_size]
                for sym in batch:
                    try:
                        ticker = self.ex.exchange.fetch_ticker(sym)
                        vol = float(ticker.get('quoteVolume', 0))
                        change = float(ticker.get('percentage', 0))
                        if vol >= min_volume_usdt:
                            hot.append({
                                'symbol': sym,
                                'volume': vol,
                                'change_24h': change,
                                'price': float(ticker.get('last', 0)),
                            })
                    except Exception:
                        continue

            # 按成交额排序取top
            hot.sort(key=lambda x: x['volume'], reverse=True)
            top_volume = [h['symbol'] for h in hot[:top_n]]

            # 按涨幅排序取top（捕捉爆发币）
            hot.sort(key=lambda x: abs(x['change_24h']), reverse=True)
            top_movers = [h['symbol'] for h in hot[:top_n]]

            combined = list(dict.fromkeys(top_volume + top_movers))
            # 排除已在symbols列表中的
            existing = set(CONFIG['trading']['symbols'])
            new_hot = [s for s in combined if s not in existing][:3]

            if new_hot:
                log.info(f"🔥 发现热门币种: {new_hot}")
            return new_hot
        except Exception as e:
            log.warning(f"热门币种扫描失败: {e}")
            return []


class TradeSummary:
    """交易总结与策略优化建议"""

    @staticmethod
    def generate_summary(bot):
        """生成交易总结"""
        stats = bot.get_stats()
        lines = []
        lines.append(f"\n{'='*60}")
        lines.append(f"📋 纸面交易总结 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append(f"{'='*60}")

        # 基本统计
        lines.append(f"💰 虚拟余额: {stats['balance']:.2f} U (初始{bot.initial_balance:.0f}U)")
        lines.append(f"📊 已平仓: {stats['total_trades']}笔 | 胜率: {stats['win_rate']}%")
        if stats['total_trades'] > 0:
            lines.append(f"🟢 盈利: {stats['win_count']}笔 | 🔴 亏损: {stats['loss_count']}笔")
            lines.append(f"📈 最大盈利: +{stats['max_win']:.2f}U | 最大亏损: {stats['max_loss']:.2f}U")
            lines.append(f"💵 总盈亏: {stats['pnl_total']:.2f}U")

        # 当前持仓
        if bot.positions:
            lines.append(f"\n📦 当前持仓:")
            for sym, pos in bot.positions.items():
                lines.append(f"  {sym}: {pos['side']} 入场={pos['entry']} 保证金={pos['margin']:.2f}U")

        # 按币种分析
        if stats['total_trades'] >= 5:
            lines.append(f"\n📊 分币种胜率:")
            symbol_stats = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0})
            for t in bot.closed_trades:
                sym = t['symbol']
                if t['pnl'] > 0:
                    symbol_stats[sym]['wins'] += 1
                else:
                    symbol_stats[sym]['losses'] += 1
                symbol_stats[sym]['pnl'] += t['pnl']

            for sym, ss in sorted(symbol_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
                total = ss['wins'] + ss['losses']
                wr = ss['wins'] / total * 100 if total > 0 else 0
                emoji = '🟢' if ss['pnl'] > 0 else '🔴'
                lines.append(f"  {emoji} {sym}: {total}笔 胜率{wr:.0f}% 盈亏{ss['pnl']:.2f}U")

        # 按方向分析
        if stats['total_trades'] >= 5:
            long_wins = sum(1 for t in bot.closed_trades if t['side'] == 'LONG' and t['pnl'] > 0)
            long_total = sum(1 for t in bot.closed_trades if t['side'] == 'LONG')
            short_wins = sum(1 for t in bot.closed_trades if t['side'] == 'SHORT' and t['pnl'] > 0)
            short_total = sum(1 for t in bot.closed_trades if t['side'] == 'SHORT')
            lines.append(f"\n📊 方向分析:")
            if long_total > 0:
                lines.append(f"  做多: {long_total}笔 胜率{long_wins/long_total*100:.0f}%")
            if short_total > 0:
                lines.append(f"  做空: {short_total}笔 胜率{short_wins/short_total*100:.0f}%")

        # 优化建议
        suggestions = TradeSummary._get_suggestions(bot, stats)
        if suggestions:
            lines.append(f"\n🔧 优化建议:")
            for s in suggestions:
                lines.append(f"  - {s}")

        # 目标进度
        if stats['total_trades'] > 0:
            progress = min(stats['total_trades'] / 100 * 100, 100)
            lines.append(f"\n🎯 目标进度: {stats['total_trades']}/100笔 ({progress:.0f}%) | 需胜率≥80% 当前{stats['win_rate']}%")
            if stats.get('target_reached'):
                lines.append("🎉🎉🎉 目标达成！100笔胜率≥80%，可以切入实盘！")

        lines.append(f"{'='*60}")
        return '\n'.join(lines)

    @staticmethod
    def _get_suggestions(bot, stats):
        """根据交易历史生成优化建议"""
        suggestions = []
        if stats['total_trades'] < 10:
            return suggestions

        # 止损太频繁？
        sl_trades = [t for t in bot.closed_trades if t.get('close_reason') == '止损']
        tp_trades = [t for t in bot.closed_trades if t.get('close_reason') == '止盈']
        if len(sl_trades) > len(tp_trades) * 1.5:
            suggestions.append("止损触发过多，考虑适当放宽止损(当前3%)或提高信号阈值")

        # 做多/做空偏科
        long_total = sum(1 for t in bot.closed_trades if t['side'] == 'LONG')
        short_total = sum(1 for t in bot.closed_trades if t['side'] == 'SHORT')
        if long_total > 0 and short_total > 0:
            long_wr = sum(1 for t in bot.closed_trades if t['side'] == 'LONG' and t['pnl'] > 0) / long_total
            short_wr = sum(1 for t in bot.closed_trades if t['side'] == 'SHORT' and t['pnl'] > 0) / short_total
            if long_wr > short_wr + 0.2:
                suggestions.append(f"做多胜率({long_wr*100:.0f}%)远高于做空({short_wr*100:.0f}%)，优先做多信号")
            elif short_wr > long_wr + 0.2:
                suggestions.append(f"做空胜率({short_wr*100:.0f}%)远高于做多({long_wr*100:.0f}%)，优先做空信号")

        # 某些币种持续亏损
        symbol_pnl = defaultdict(float)
        symbol_count = defaultdict(int)
        for t in bot.closed_trades:
            symbol_pnl[t['symbol']] += t['pnl']
            symbol_count[t['symbol']] += 1
        for sym, pnl in symbol_pnl.items():
            if symbol_count[sym] >= 3 and pnl < -5:
                suggestions.append(f"{sym} 持续亏损({pnl:.2f}U/{symbol_count[sym]}笔)，考虑移除或降低仓位")

        # 胜率太低
        if stats['total_trades'] >= 20 and stats['win_rate'] < 50:
            suggestions.append("胜率低于50%，建议提高信号阈值到70分以上，宁缺毋滥")

        # 信号强度分析
        high_strength = [t for t in bot.closed_trades if t.get('strength', 0) >= 75]
        low_strength = [t for t in bot.closed_trades if t.get('strength', 0) < 70]
        if high_strength and low_strength:
            hs_wr = sum(1 for t in high_strength if t['pnl'] > 0) / len(high_strength)
            ls_wr = sum(1 for t in low_strength if t['pnl'] > 0) / len(low_strength)
            if hs_wr > ls_wr + 0.15:
                suggestions.append(f"高信号(≥75)胜率{hs_wr*100:.0f}% > 低信号(<70){ls_wr*100:.0f}%，建议提高最低信号阈值")

        return suggestions


class PaperTradingBot:
    """纸面交易机器人：用真实行情，虚拟下单，验证策略"""

    def __init__(self, exchange: OKXExchange, initial_balance=1000.0):
        self.ex = exchange
        self.ta = TechnicalAnalysis()
        self.sg = SignalGenerator()
        self.news = NewsSentimentAnalyzer()
        self.hot_scanner = HotSymbolScanner(exchange)

        # 虚拟账户
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.positions = {}  # symbol -> position info
        self.trade_history = []
        self.closed_trades = []
        self.scan_count = 0  # 扫描次数

        # 加载历史记录
        self._load_state()

    def _load_state(self):
        """加载历史交易记录"""
        if PAPER_TRADE_FILE.exists():
            with open(PAPER_TRADE_FILE, 'r') as f:
                state = json.load(f)
            self.balance = state.get('balance', self.balance)
            self.initial_balance = state.get('initial_balance', self.initial_balance)
            self.positions = state.get('positions', {})
            self.trade_history = state.get('trade_history', [])
            self.closed_trades = state.get('closed_trades', [])
            self.scan_count = state.get('scan_count', 0)
            log.info(f"📋 已加载纸面交易记录: {len(self.closed_trades)}笔已平仓, {len(self.positions)}个持仓, {self.scan_count}次扫描")

    def _save_state(self):
        """保存交易记录"""
        state = {
            'balance': self.balance,
            'initial_balance': self.initial_balance,
            'positions': self.positions,
            'trade_history': self.trade_history,
            'closed_trades': self.closed_trades,
            'scan_count': self.scan_count,
        }
        with open(PAPER_TRADE_FILE, 'w') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    def _check_positions(self, current_prices):
        """检查持仓是否触发止盈止损"""
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            current_price = current_prices.get(symbol)
            if current_price is None:
                continue

            entry = pos['entry']
            side = pos['side']
            sl = pos['stop_loss']
            tp = pos['take_profit']
            amount = pos['amount']

            closed = False
            close_price = None
            close_reason = ''

            if side == 'LONG':
                # 止损
                if current_price <= sl:
                    closed = True
                    close_price = sl
                    close_reason = '止损'
                # 止盈
                elif current_price >= tp:
                    closed = True
                    close_price = tp
                    close_reason = '止盈'
            elif side == 'SHORT':
                if current_price >= sl:
                    closed = True
                    close_price = sl
                    close_reason = '止损'
                elif current_price <= tp:
                    closed = True
                    close_price = tp
                    close_reason = '止盈'

            if closed:
                # 计算盈亏
                if side == 'LONG':
                    pnl_pct = (close_price - entry) / entry
                else:
                    pnl_pct = (entry - close_price) / entry

                # 加杠杆
                leveraged_pnl_pct = pnl_pct * CONFIG['trading']['leverage']
                position_value = pos['margin']
                pnl = position_value * leveraged_pnl_pct

                self.balance += position_value + pnl

                trade_result = {
                    **pos,
                    'close_time': datetime.now().isoformat(),
                    'close_price': close_price,
                    'close_reason': close_reason,
                    'pnl': round(pnl, 4),
                    'pnl_pct': round(leveraged_pnl_pct * 100, 2),
                    'balance_after': round(self.balance, 2),
                }
                self.closed_trades.append(trade_result)

                emoji = '🟢' if pnl > 0 else '🔴'
                log.info(f"{emoji} {close_reason}平仓 {symbol} {side}: 入场={entry} 平仓={close_price} 盈亏={pnl:.2f}USDT({leveraged_pnl_pct*100:.2f}%)")

                del self.positions[symbol]
                self._save_state()

    def get_stats(self):
        """获取交易统计"""
        total = len(self.closed_trades)
        if total == 0:
            return {
                'total_trades': 0,
                'win_rate': 0,
                'win_count': 0,
                'loss_count': 0,
                'total_pnl': 0,
                'avg_pnl_pct': 0,
                'max_win': 0,
                'max_loss': 0,
                'balance': self.balance,
                'pnl_total': round(self.balance - self.initial_balance, 2),
                'target_reached': False,
            }

        wins = [t for t in self.closed_trades if t['pnl'] > 0]
        losses = [t for t in self.closed_trades if t['pnl'] <= 0]
        total_pnl = sum(t['pnl'] for t in self.closed_trades)
        avg_pnl = sum(t['pnl_pct'] for t in self.closed_trades) / total
        max_win = max((t['pnl'] for t in self.closed_trades), default=0)
        max_loss = min((t['pnl'] for t in self.closed_trades), default=0)

        return {
            'total_trades': total,
            'win_rate': round(len(wins) / total * 100, 1),
            'win_count': len(wins),
            'loss_count': len(losses),
            'total_pnl': round(total_pnl, 2),
            'avg_pnl_pct': round(avg_pnl, 2),
            'max_win': round(max_win, 2),
            'max_loss': round(max_loss, 2),
            'balance': round(self.balance, 2),
            'pnl_total': round(self.balance - self.initial_balance, 2),
            'target_reached': total >= 100 and len(wins) / total >= 0.8
        }
        return stats

    def analyze_symbol(self, symbol):
        """分析单个标的"""
        tf = CONFIG['trading']['timeframes']
        log.info(f"📊 分析 {symbol} ...")
        df_15m = self.ex.get_klines(symbol, tf['direction'], 100)
        df_5m = self.ex.get_klines(symbol, tf['entry'], 200)
        df_1m = self.ex.get_klines(symbol, tf['confirm'], 200)

        df_15m = self.ta.full_analysis(df_15m)
        df_5m = self.ta.full_analysis(df_5m)
        df_1m = self.ta.full_analysis(df_1m)

        signal = self.sg.generate_signal(df_15m, df_5m, df_1m)

        last = df_5m.iloc[-1]
        signal['symbol'] = symbol
        signal['price'] = float(last['close'])
        signal['boll_position'] = float(last['boll_position'])
        signal['kdj_k'] = float(last['kdj_k'])
        signal['macd_above_zero'] = bool(last['macd_above_zero'])

        return signal

    def execute_signal(self, signal, current_prices):
        """虚拟执行交易信号"""
        symbol = signal['symbol']
        action = signal['action']
        price = signal['price']
        risk = CONFIG['trading']['risk']
        max_positions = CONFIG['trading'].get('max_positions', 3)

        # 检查是否已有持仓
        has_position = symbol in self.positions

        # 检查持仓数量上限
        if len(self.positions) >= max_positions and action in ('LONG', 'SHORT') and not has_position:
            log.info(f"⚠️ 已达最大持仓数({max_positions})，跳过 {symbol}")
            return

        if action == 'LONG' and not has_position:
            margin = self.balance * risk['max_position_pct']
            if margin < 1:
                log.warning(f"⚠️ {symbol} 保证金不足，跳过")
                return

            stop_price = round(price * (1 - risk['stop_loss_pct']), 2)
            take_price = round(price * (1 + risk['take_profit_pct']), 2)

            log.info(f"🟢 [纸面] 做多 {symbol} 强度={signal['strength']} 原因={signal['reason']}")
            log.info(f"   入场={price} 止损={stop_price} 止盈={take_price} 保证金={margin:.2f}U")

            self.positions[symbol] = {
                'side': 'LONG',
                'entry': price,
                'stop_loss': stop_price,
                'take_profit': take_price,
                'margin': margin,
                'amount': margin * CONFIG['trading']['leverage'] / price,
                'time': datetime.now().isoformat(),
                'reason': signal['reason'],
                'strength': signal['strength'],
            }
            self.balance -= margin
            self.trade_history.append({
                'time': datetime.now().isoformat(),
                'symbol': symbol,
                'action': 'LONG',
                'price': price,
                'stop_loss': stop_price,
                'take_profit': take_price,
                'margin': margin,
                'reason': signal['reason'],
                'strength': signal['strength'],
            })
            self._save_state()

        elif action == 'SHORT' and not has_position:
            margin = self.balance * risk['max_position_pct']
            if margin < 1:
                log.warning(f"⚠️ {symbol} 保证金不足，跳过")
                return

            stop_price = round(price * (1 + risk['stop_loss_pct']), 2)
            take_price = round(price * (1 - risk['take_profit_pct']), 2)

            log.info(f"🔴 [纸面] 做空 {symbol} 强度={signal['strength']} 原因={signal['reason']}")
            log.info(f"   入场={price} 止损={stop_price} 止盈={take_price} 保证金={margin:.2f}U")

            self.positions[symbol] = {
                'side': 'SHORT',
                'entry': price,
                'stop_loss': stop_price,
                'take_profit': take_price,
                'margin': margin,
                'amount': margin * CONFIG['trading']['leverage'] / price,
                'time': datetime.now().isoformat(),
                'reason': signal['reason'],
                'strength': signal['strength'],
            }
            self.balance -= margin
            self.trade_history.append({
                'time': datetime.now().isoformat(),
                'symbol': symbol,
                'action': 'SHORT',
                'price': price,
                'stop_loss': stop_price,
                'take_profit': take_price,
                'margin': margin,
                'reason': signal['reason'],
                'strength': signal['strength'],
            })
            self._save_state()

        else:
            log.info(f"⏸️ {symbol} 持有观望，信号={action}")

    def scan_and_trade(self):
        """扫描+虚拟交易（含消息面、动态币种、总结）"""
        self.scan_count += 1
        log.info("=" * 60)
        log.info(f"🔄 [纸面交易] 第{self.scan_count}次扫描 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        stats = self.get_stats()
        log.info(f"💰 虚拟余额: {self.balance:.2f} USDT | 已平仓: {stats['total_trades']}笔 | 胜率: {stats['win_rate']}%")

        # 动态发现热门币种（每5次扫描一次）
        all_symbols = list(CONFIG['trading']['symbols'])
        if self.scan_count % 5 == 0:
            try:
                hot_syms = self.hot_scanner.scan_hot_symbols()
                if hot_syms:
                    all_symbols.extend(hot_syms)
                    log.info(f"🔥 本次额外扫描热门币: {hot_syms}")
            except Exception as e:
                log.warning(f"热门币种扫描失败: {e}")

        # 先获取当前价格并检查止盈止损
        current_prices = {}
        for symbol in all_symbols:
            try:
                ticker = self.ex.exchange.fetch_ticker(symbol)
                current_prices[symbol] = float(ticker['last'])
            except Exception as e:
                log.warning(f"获取{symbol}价格失败: {e}")

        self._check_positions(current_prices)

        # 扫描信号
        for symbol in all_symbols:
            try:
                signal = self.analyze_symbol(symbol)
                # 只有技术面产生LONG/SHORT信号时才做消息面分析（节省时间）
                if signal['action'] in ('LONG', 'SHORT'):
                    signal = self.news.adjust_signal(signal, symbol)
                else:
                    signal['news_sentiment'] = 0.0
                log.info(
                    f"  {symbol}: 信号={signal['action']} 强度={signal['strength']} "
                    f"价格={signal['price']:.2f} BOLL={signal['boll_position']:.2f} "
                    f"KDJ.K={signal['kdj_k']:.1f} MACD零轴上={signal['macd_above_zero']}"
                    + (f" 消息面={signal.get('news_sentiment', 0):.2f}" if signal['action'] != 'HOLD' else "")
                )
                if signal['action'] in ('LONG', 'SHORT'):
                    self.execute_signal(signal, current_prices)
            except Exception as e:
                log.error(f"❌ {symbol} 分析失败: {e}")

        # 宏观消息面检查（每次扫描都做，用于判断系统性风险）
        macro_sentiment = self.news._check_macro_sentiment()
        if macro_sentiment < -0.5:
            log.warning(f"⚠️ 宏观消息面极度利空(情绪分={macro_sentiment:.2f})，考虑降低仓位或暂停开仓")

        # 打印最新统计
        stats = self.get_stats()
        log.info(f"📊 统计: {stats['total_trades']}笔 | 胜率{stats['win_rate']}% | 总盈亏{stats['pnl_total']}U")
        if stats['target_reached']:
            log.info("🎉🎉🎉 目标达成！100笔交易胜率≥80%，可以切入实盘！")

        # 每5次扫描或开仓/平仓时生成总结
        summary = TradeSummary.generate_summary(self)
        log.info(summary)
        self._save_state()
        log.info("🔄 扫描结束")

        return stats

    def run_loop(self, interval_seconds=300):
        """持续运行"""
        log.info(f"🚀 [纸面交易] 机器人启动，每{interval_seconds}秒扫描一次")
        log.info(f"   初始资金: {self.initial_balance} USDT | 目标: 100笔胜率≥80%")
        while True:
            try:
                self.scan_and_trade()
            except Exception as e:
                log.error(f"❌ 运行异常: {e}")
            time.sleep(interval_seconds)


class TradingBot:
    """缠论短线交易机器人（OKX实盘版）"""

    def __init__(self, exchange: OKXExchange):
        self.ex = exchange
        self.ta = TechnicalAnalysis()
        self.sg = SignalGenerator()
        self.positions = {}
        self.trade_history = []
        self.daily_pnl = 0.0
        self.daily_start_balance = 0.0

    def analyze_symbol(self, symbol):
        tf = CONFIG['trading']['timeframes']
        log.info(f"📊 分析 {symbol} ...")
        df_15m = self.ex.get_klines(symbol, tf['direction'], 100)
        df_5m = self.ex.get_klines(symbol, tf['entry'], 200)
        df_1m = self.ex.get_klines(symbol, tf['confirm'], 200)
        df_15m = self.ta.full_analysis(df_15m)
        df_5m = self.ta.full_analysis(df_5m)
        df_1m = self.ta.full_analysis(df_1m)
        signal = self.sg.generate_signal(df_15m, df_5m, df_1m)
        last = df_5m.iloc[-1]
        signal['symbol'] = symbol
        signal['price'] = float(last['close'])
        signal['boll_position'] = float(last['boll_position'])
        signal['kdj_k'] = float(last['kdj_k'])
        signal['macd_above_zero'] = bool(last['macd_above_zero'])
        return signal

    def execute_signal(self, signal):
        symbol = signal['symbol']
        action = signal['action']
        price = signal['price']
        risk = CONFIG['trading']['risk']

        current_positions = self.ex.get_positions(symbol)
        has_position = len(current_positions) > 0

        if action == 'LONG' and not has_position:
            balance = self.ex.get_balance()
            position_size = balance['free'] * risk['max_position_pct']
            try:
                market = self.ex.exchange.market(symbol)
                contract_size = market.get('contractSize', 1)
                notional = position_size * CONFIG['trading']['leverage']
                amount = int(notional / (price * contract_size))
                if amount < 1:
                    log.warning(f"⚠️ {symbol} 计算张数不足1张，跳过")
                    return
            except Exception:
                amount = round(position_size * CONFIG['trading']['leverage'] / price, 4)
                if amount < 0.001:
                    log.warning(f"⚠️ {symbol} 计算数量过小，跳过")
                    return

            log.info(f"🟢 做多信号: {symbol} 强度={signal['strength']} 原因={signal['reason']}")
            self.ex.set_leverage(symbol, CONFIG['trading']['leverage'])
            stop_price = round(price * (1 - risk['stop_loss_pct']), 2)
            take_price = round(price * (1 + risk['take_profit_pct']), 2)

            order = self.ex.place_order_with_tp_sl(symbol, 'buy', amount, stop_price, take_price)
            if not order:
                order = self.ex.place_market_order(symbol, 'buy', amount)
                if order:
                    self.ex.place_stop_loss(symbol, 'buy', amount, stop_price)
                    self.ex.place_take_profit(symbol, 'buy', amount, take_price)

            if order:
                self.positions[symbol] = {
                    'side': 'LONG', 'entry': price, 'amount': amount,
                    'stop_loss': stop_price, 'take_profit': take_price,
                    'time': datetime.now().isoformat(),
                }
                self.trade_history.append({
                    'time': datetime.now().isoformat(), 'symbol': symbol,
                    'action': 'LONG', 'price': price, 'amount': amount,
                    'stop_loss': stop_price, 'take_profit': take_price,
                    'reason': signal['reason'], 'strength': signal['strength'],
                })

        elif action == 'SHORT' and not has_position:
            balance = self.ex.get_balance()
            position_size = balance['free'] * risk['max_position_pct']
            try:
                market = self.ex.exchange.market(symbol)
                contract_size = market.get('contractSize', 1)
                notional = position_size * CONFIG['trading']['leverage']
                amount = int(notional / (price * contract_size))
                if amount < 1:
                    log.warning(f"⚠️ {symbol} 计算张数不足1张，跳过")
                    return
            except Exception:
                amount = round(position_size * CONFIG['trading']['leverage'] / price, 4)
                if amount < 0.001:
                    log.warning(f"⚠️ {symbol} 计算数量过小，跳过")
                    return

            log.info(f"🔴 做空信号: {symbol} 强度={signal['strength']} 原因={signal['reason']}")
            self.ex.set_leverage(symbol, CONFIG['trading']['leverage'])
            stop_price = round(price * (1 + risk['stop_loss_pct']), 2)
            take_price = round(price * (1 - risk['take_profit_pct']), 2)

            order = self.ex.place_order_with_tp_sl(symbol, 'sell', amount, stop_price, take_price)
            if not order:
                order = self.ex.place_market_order(symbol, 'sell', amount)
                if order:
                    self.ex.place_stop_loss(symbol, 'sell', amount, stop_price)
                    self.ex.place_take_profit(symbol, 'sell', amount, take_price)

            if order:
                self.positions[symbol] = {
                    'side': 'SHORT', 'entry': price, 'amount': amount,
                    'stop_loss': stop_price, 'take_profit': take_price,
                    'time': datetime.now().isoformat(),
                }
                self.trade_history.append({
                    'time': datetime.now().isoformat(), 'symbol': symbol,
                    'action': 'SHORT', 'price': price, 'amount': amount,
                    'stop_loss': stop_price, 'take_profit': take_price,
                    'reason': signal['reason'], 'strength': signal['strength'],
                })
        else:
            log.info(f"⏸️ {symbol} 持有观望，信号={action}")

    def scan_and_trade(self):
        log.info("=" * 60)
        log.info(f"🔄 扫描开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        balance = self.ex.get_balance()
        log.info(f"💰 余额: 总={balance['total']:.2f} 可用={balance['free']:.2f} USDT")
        for symbol in CONFIG['trading']['symbols']:
            try:
                signal = self.analyze_symbol(symbol)
                log.info(
                    f"  {symbol}: 信号={signal['action']} 强度={signal['strength']} "
                    f"价格={signal['price']:.2f} BOLL={signal['boll_position']:.2f} "
                    f"KDJ.K={signal['kdj_k']:.1f} MACD零轴上={signal['macd_above_zero']}"
                )
                if signal['action'] in ('LONG', 'SHORT'):
                    self.execute_signal(signal)
            except Exception as e:
                log.error(f"❌ {symbol} 分析失败: {e}")
        log.info("🔄 扫描结束")

    def run_loop(self, interval_seconds=300):
        log.info(f"🚀 交易机器人启动（OKX），每{interval_seconds}秒扫描一次")
        while True:
            try:
                self.scan_and_trade()
            except Exception as e:
                log.error(f"❌ 运行异常: {e}")
            time.sleep(interval_seconds)


# ─── CLI入口 ───
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='缠论短线OKX交易机器人')
    parser.add_argument('--scan', action='store_true', help='单次扫描(实盘)')
    parser.add_argument('--loop', action='store_true', help='持续运行(实盘)')
    parser.add_argument('--paper', action='store_true', help='纸面交易模式(模拟)')
    parser.add_argument('--paper-loop', action='store_true', help='纸面交易持续运行')
    parser.add_argument('--stats', action='store_true', help='查看纸面交易统计')
    parser.add_argument('--balance', action='store_true', help='查看余额')
    parser.add_argument('--positions', action='store_true', help='查看持仓')
    parser.add_argument('--interval', type=int, default=300, help='扫描间隔（秒）')
    parser.add_argument('--capital', type=float, default=1000.0, help='纸面交易初始资金(默认1000U)')
    args = parser.parse_args()

    ex = OKXExchange()

    if args.stats:
        bot = PaperTradingBot(ex, args.capital)
        stats = bot.get_stats()
        print("\n📊 纸面交易统计")
        print("=" * 40)
        print(f"  总交易数: {stats['total_trades']}")
        print(f"  胜率: {stats['win_rate']}%")
        if stats['total_trades'] > 0:
            print(f"  盈利笔数: {stats['win_count']}")
            print(f"  亏损笔数: {stats['loss_count']}")
            print(f"  最大单笔盈利: {stats['max_win']} U")
            print(f"  最大单笔亏损: {stats['max_loss']} U")
            print(f"  平均盈亏比: {stats['avg_pnl_pct']}%")
        print(f"  当前余额: {stats['balance']} U")
        print(f"  总盈亏: {stats['pnl_total']} U")
        if stats.get('target_reached'):
            print("\n🎉 目标达成！100笔胜率≥80%，可以切入实盘！")
        print()

    elif args.paper:
        bot = PaperTradingBot(ex, args.capital)
        bot.scan_and_trade()

    elif args.paper_loop:
        bot = PaperTradingBot(ex, args.capital)
        bot.run_loop(args.interval)

    elif args.balance:
        print(json.dumps(ex.get_balance(), indent=2))

    elif args.positions:
        for symbol in CONFIG['trading']['symbols']:
            positions = ex.get_positions(symbol)
            if positions:
                for p in positions:
                    print(f"  {p['symbol']}: {p['side']} 数量={p['amount']} 入场={p['entry_price']} PnL={p['unrealized_pnl']}")
            else:
                print(f"  {symbol}: 无持仓")

    elif args.scan:
        bot = TradingBot(ex)
        bot.scan_and_trade()

    elif args.loop:
        bot = TradingBot(ex)
        bot.run_loop(args.interval)

    else:
        print("用法:")
        print("  python trader.py --paper          # 纸面交易单次扫描")
        print("  python trader.py --paper-loop      # 纸面交易持续运行")
        print("  python trader.py --stats           # 查看纸面交易统计")
        print("  python trader.py --scan            # 实盘单次扫描")
        print("  python trader.py --loop            # 实盘持续运行")
        print("  python trader.py --balance         # 查看余额")
        print("  python trader.py --positions       # 查看持仓")
        print("  python trader.py --capital 2000    # 设置纸面交易初始资金")
