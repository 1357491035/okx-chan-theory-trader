#!/usr/bin/env python3
"""
DeepSeek AI Agent 模块 - 行情分析 + 大环境判断 + 仓位管理
接入DeepSeek API，为交易系统提供AI驱动的决策增强
"""

import json
import time
import logging
import requests
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEEPSEEK_API_KEY = "sk-30ba1e59d2c7490a8421415edc30e2f7"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-chat"

# 缓存控制
_cache = {}
_CACHE_TTL = 300  # 5分钟缓存


def _call_deepseek(system_prompt: str, user_prompt: str, max_tokens: int = 1500, temperature: float = 0.3) -> str:
    """调用DeepSeek API"""
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        resp = requests.post(DEEPSEEK_BASE_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error(f"❌ DeepSeek API调用失败: {e}")
        return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Agent 1: 行情分析师 (Market Analyst)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MARKET_ANALYST_SYSTEM = """你是一位专业的加密货币合约短线交易分析师。
基于提供的技术指标数据，给出简洁的分析结论。

输出格式（严格JSON）：
{
  "trend": "BULLISH/BEARISH/NEUTRAL",
  "confidence": 0-100,
  "key_levels": {"support": 数字, "resistance": 数字},
  "signals": ["信号1", "信号2"],
  "risk_warning": "风险提示，可为空",
  "suggestion": "做多/做空/观望"
}

只输出JSON，不要其他文字。"""


def analyze_market(symbol: str, indicators: dict) -> dict:
    """AI行情分析：基于技术指标给出趋势判断"""
    cache_key = f"market_{symbol}"
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key][0] < _CACHE_TTL:
        return _cache[cache_key][1]

    user_prompt = f"""分析 {symbol} 当前行情：
- 价格: {indicators.get('price', 'N/A')}
- BOLL位置: {indicators.get('boll_position', 'N/A')} (0=下轨, 1=上轨)
- KDJ.K: {indicators.get('kdj_k', 'N/A')} (超买>80, 超卖<20)
- MACD零轴上: {indicators.get('macd_above_zero', 'N/A')}
- RSI: {indicators.get('rsi', 'N/A')} (超买>70, 超卖<30)
- 成交量倍数: {indicators.get('vol_ratio', 'N/A')}x
- 5m信号: {indicators.get('signal_action', 'N/A')} 强度={indicators.get('signal_strength', 'N/A')}
- 原因: {indicators.get('signal_reason', 'N/A')}
- 持仓状态: {indicators.get('position_side', '无持仓')}

给出你的分析判断。"""

    result_text = _call_deepseek(MARKET_ANALYST_SYSTEM, user_prompt, max_tokens=800)
    try:
        # 提取JSON
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0]
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0]
        result = json.loads(result_text)
    except (json.JSONDecodeError, IndexError):
        result = {
            "trend": "NEUTRAL",
            "confidence": 0,
            "key_levels": {},
            "signals": [],
            "risk_warning": "AI解析失败，以技术面为准",
            "suggestion": "观望",
        }

    _cache[cache_key] = (now, result)
    log.info(f"🤖 AI行情分析 {symbol}: {result['trend']}(置信{result['confidence']}%) → {result['suggestion']}")
    if result.get('risk_warning'):
        log.info(f"   ⚠️ {result['risk_warning']}")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Agent 2: 大环境分析师 (Macro Analyst)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MACRO_ANALYST_SYSTEM = """你是一位加密货币市场宏观分析师。
基于当前市场数据，判断整体大环境是否适合开仓交易。

输出格式（严格JSON）：
{
  "market_regime": "RISK_ON/RISK_OFF/NEUTRAL",
  "fear_greed_estimate": 0-100,
  "narrative": "当前市场主线叙事",
  "risk_factors": ["风险1", "风险2"],
  "opportunity": "当前机会描述",
  "position_advice": "满仓/半仓/轻仓/空仓",
  "leverage_advice": "建议杠杆倍数"
}

只输出JSON，不要其他文字。"""


def analyze_macro(market_data: dict) -> dict:
    """AI大环境分析：判断是否适合交易"""
    cache_key = "macro"
    now = time.time()
    if cache_key in _cache and now - _cache[cache_key][0] < _CACHE_TTL:
        return _cache[cache_key][1]

    user_prompt = f"""分析当前加密货币市场大环境：
- BTC价格: ${market_data.get('btc_price', 'N/A')}
- ETH价格: ${market_data.get('eth_price', 'N/A')}
- BTC 24h涨跌: {market_data.get('btc_change_24h', 'N/A')}%
- 总持仓数: {market_data.get('total_positions', 0)}
- 账户余额: {market_data.get('balance', 'N/A')}U (初始{market_data.get('initial_balance', 'N/A')}U)
- 今日盈亏: {market_data.get('daily_pnl', 0)}U
- 近期胜率: {market_data.get('win_rate', 'N/A')}%
- 总交易笔数: {market_data.get('total_trades', 0)}
- 消息面情绪: {market_data.get('news_sentiment', '中性')}

给出大环境判断和仓位建议。"""

    result_text = _call_deepseek(MACRO_ANALYST_SYSTEM, user_prompt, max_tokens=1000)
    try:
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0]
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0]
        result = json.loads(result_text)
    except (json.JSONDecodeError, IndexError):
        result = {
            "market_regime": "NEUTRAL",
            "fear_greed_estimate": 50,
            "narrative": "数据不足",
            "risk_factors": ["AI解析失败"],
            "opportunity": "谨慎操作",
            "position_advice": "轻仓",
            "leverage_advice": "3x",
        }

    _cache[cache_key] = (now, result)
    log.info(f"🌍 AI大环境: {result['market_regime']} | 恐贪指数≈{result['fear_greed_estimate']} | 建议: {result['position_advice']} {result.get('leverage_advice', '')}x")
    if result.get('narrative'):
        log.info(f"   📌 叙事: {result['narrative']}")
    if result.get('risk_factors'):
        for rf in result['risk_factors'][:3]:
            log.info(f"   ⚠️ {rf}")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Agent 3: 仓位管理师 (Position Manager)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
POSITION_MANAGER_SYSTEM = """你是一位专业的仓位管理师。
基于账户状态和市场环境，给出具体的仓位管理建议。

输出格式（严格JSON）：
{
  "should_open": true/false,
  "position_size_pct": 0-100,
  "leverage": 数字,
  "stop_loss_pct": 数字,
  "take_profit_pct": 数字,
  "max_positions": 数字,
  "reasoning": "简短理由"
}

只输出JSON，不要其他文字。"""


def manage_position(account_data: dict, signal_data: dict, macro_data: dict) -> dict:
    """AI仓位管理：决定是否开仓、仓位大小、杠杆"""
    cache_key = "position"
    now = time.time()
    # 仓位管理缓存短一些，因为依赖实时数据
    if cache_key in _cache and now - _cache[cache_key][0] < 120:
        return _cache[cache_key][1]

    user_prompt = f"""基于以下信息给出仓位管理建议：

【账户状态】
- 余额: {account_data.get('balance', 'N/A')}U (初始{account_data.get('initial_balance', 'N/A')}U)
- 已用保证金: {account_data.get('used_margin', 0)}U
- 当前持仓数: {account_data.get('position_count', 0)}/{account_data.get('max_positions', 3)}
- 今日盈亏: {account_data.get('daily_pnl', 0)}U
- 近期胜率: {account_data.get('win_rate', 'N/A')}%

【交易信号】
- 币种: {signal_data.get('symbol', 'N/A')}
- 方向: {signal_data.get('action', 'N/A')}
- 信号强度: {signal_data.get('strength', 'N/A')}/100
- 原因: {signal_data.get('reason', 'N/A')}

【大环境】
- 市场状态: {macro_data.get('market_regime', 'NEUTRAL')}
- 恐贪指数: {macro_data.get('fear_greed_estimate', 50)}
- 仓位建议: {macro_data.get('position_advice', '轻仓')}

注意：
- 日亏损超过5%必须停止开仓
- 单笔风险不超过3%
- 总杠杆不超过20x
- 信号强度<60分不开仓"""

    result_text = _call_deepseek(POSITION_MANAGER_SYSTEM, user_prompt, max_tokens=600)
    try:
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0]
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0]
        result = json.loads(result_text)
    except (json.JSONDecodeError, IndexError):
        result = {
            "should_open": False,
            "position_size_pct": 5,
            "leverage": 5,
            "stop_loss_pct": 3,
            "take_profit_pct": 6,
            "max_positions": 3,
            "reasoning": "AI解析失败，保守处理",
        }

    # 安全校验：覆盖AI可能给出的危险值
    result['leverage'] = min(result.get('leverage', 5), 20)
    result['stop_loss_pct'] = min(result.get('stop_loss_pct', 3), 5)
    result['position_size_pct'] = min(result.get('position_size_pct', 10), 30)
    result['max_positions'] = min(result.get('max_positions', 3), 5)

    _cache[cache_key] = (now, result)
    should = "✅开仓" if result['should_open'] else "❌不开"
    log.info(f"💰 AI仓位决策: {should} | 仓位{result['position_size_pct']}% | 杠杆{result['leverage']}x | SL={result['stop_loss_pct']}% TP={result['take_profit_pct']}%")
    log.info(f"   💡 {result.get('reasoning', '')}")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Agent 4: 交易复盘师 (Trade Reviewer)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRADE_REVIEWER_SYSTEM = """你是一位交易复盘分析师。
基于交易历史数据，分析策略表现并给出优化建议。

输出格式（严格JSON）：
{
  "overall_assessment": "优秀/良好/一般/较差",
  "strengths": ["优势1", "优势2"],
  "weaknesses": ["弱点1", "弱点2"],
  "suggestions": ["建议1", "建议2", "建议3"],
  "should_adjust_threshold": true/false,
  "recommended_threshold": 数字,
  "preferred_side": "LONG/SHORT/BOTH"
}

只输出JSON，不要其他文字。"""


def review_trades(trade_data: dict) -> dict:
    """AI交易复盘：分析策略表现"""
    cache_key = "review"
    now = time.time()
    # 复盘缓存10分钟
    if cache_key in _cache and now - _cache[cache_key][0] < 600:
        return _cache[cache_key][1]

    if trade_data.get('total_trades', 0) < 5:
        return {
            "overall_assessment": "数据不足",
            "strengths": [],
            "weaknesses": [],
            "suggestions": ["继续积累交易数据"],
            "should_adjust_threshold": False,
            "recommended_threshold": 60,
            "preferred_side": "BOTH",
        }

    user_prompt = f"""复盘交易策略表现：

【总体统计】
- 总交易: {trade_data.get('total_trades', 0)}笔
- 胜率: {trade_data.get('win_rate', 0)}%
- 总盈亏: {trade_data.get('pnl_total', 0)}U
- 最大单笔盈利: {trade_data.get('max_win', 0)}U
- 最大单笔亏损: {trade_data.get('max_loss', 0)}U

【方向分析】
- 做多胜率: {trade_data.get('long_win_rate', 'N/A')}%
- 做空胜率: {trade_data.get('short_win_rate', 'N/A')}%

【币种表现】
{trade_data.get('symbol_performance', 'N/A')}

【当前持仓】
{trade_data.get('current_positions', '无')}

【平仓原因分布】
{trade_data.get('close_reasons', 'N/A')}

给出策略复盘和优化建议。"""

    result_text = _call_deepseek(TRADE_REVIEWER_SYSTEM, user_prompt, max_tokens=1000)
    try:
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0]
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0]
        result = json.loads(result_text)
    except (json.JSONDecodeError, IndexError):
        result = {
            "overall_assessment": "解析失败",
            "strengths": [],
            "weaknesses": [],
            "suggestions": ["继续观察"],
            "should_adjust_threshold": False,
            "recommended_threshold": 60,
            "preferred_side": "BOTH",
        }

    _cache[cache_key] = (now, result)
    log.info(f"📊 AI复盘: {result['overall_assessment']}")
    for s in result.get('suggestions', [])[:3]:
        log.info(f"   💡 {s}")
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 聚合入口：AI决策增强
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def ai_enhanced_decision(symbol: str, signal: dict, bot) -> dict:
    """
    AI增强决策入口：
    1. 行情分析师 → 技术面AI判断
    2. 大环境分析师 → 市场环境评估
    3. 仓位管理师 → 开仓/仓位/杠杆决策
    4. 综合决策 → 融合所有Agent意见
    """
    # 收集数据
    stats = bot.get_stats()
    
    # 获取BTC/ETH价格作为大环境参考
    try:
        btc_ticker = bot.ex.exchange.fetch_ticker('BTC/USDT:USDT')
        eth_ticker = bot.ex.exchange.fetch_ticker('ETH/USDT:USDT')
        btc_price = float(btc_ticker.get('last', 0))
        eth_price = float(eth_ticker.get('last', 0))
        btc_change = float(btc_ticker.get('percentage', 0))
    except Exception:
        btc_price = eth_price = 0
        btc_change = 0

    # Agent 1: 行情分析
    indicators = {
        'price': signal.get('price', 0),
        'boll_position': signal.get('boll_position', 0),
        'kdj_k': signal.get('kdj_k', 0),
        'macd_above_zero': signal.get('macd_above_zero', False),
        'rsi': signal.get('rsi', 0),
        'vol_ratio': signal.get('vol_ratio', 0),
        'signal_action': signal.get('action', 'HOLD'),
        'signal_strength': signal.get('strength', 0),
        'signal_reason': signal.get('reason', ''),
        'position_side': bot.positions.get(symbol, {}).get('side', '无持仓'),
    }
    market_analysis = analyze_market(symbol, indicators)

    # Agent 2: 大环境分析
    macro_data_input = {
        'btc_price': btc_price,
        'eth_price': eth_price,
        'btc_change_24h': round(btc_change, 2),
        'total_positions': len(bot.positions),
        'balance': stats['balance'],
        'initial_balance': bot.initial_balance,
        'daily_pnl': bot.daily_pnl,
        'win_rate': stats['win_rate'],
        'total_trades': stats['total_trades'],
        'news_sentiment': signal.get('news_sentiment', 0),
    }
    macro_analysis = analyze_macro(macro_data_input)

    # Agent 3: 仓位管理
    account_data = {
        'balance': stats['balance'],
        'initial_balance': bot.initial_balance,
        'used_margin': sum(p['margin'] for p in bot.positions.values()),
        'position_count': len(bot.positions),
        'max_positions': CONFIG['trading'].get('max_positions', 3),
        'daily_pnl': bot.daily_pnl,
        'win_rate': stats['win_rate'],
    }
    position_decision = manage_position(account_data, signal, macro_analysis)

    # 综合决策
    # AI大环境RISK_OFF时，降低信号强度
    ai_adjusted_strength = signal.get('strength', 0)
    ai_action = signal.get('action', 'HOLD')
    
    if macro_analysis.get('market_regime') == 'RISK_OFF':
        ai_adjusted_strength -= 15
        if ai_adjusted_strength < 60:
            ai_action = 'HOLD'
            log.info(f"🤖 AI否决: 大环境RISK_OFF，{symbol}信号被压制")
    
    if macro_analysis.get('market_regime') == 'RISK_ON':
        ai_adjusted_strength += 5
    
    # AI行情分析与技术面方向冲突时，降低强度
    if market_analysis.get('trend') == 'BEARISH' and ai_action == 'LONG':
        ai_adjusted_strength -= 10
        log.info(f"🤖 AI预警: {symbol}技术面做多但AI看空，信号强度-10")
    elif market_analysis.get('trend') == 'BULLISH' and ai_action == 'SHORT':
        ai_adjusted_strength -= 10
        log.info(f"🤖 AI预警: {symbol}技术面做空但AI看多，信号强度-10")

    # 仓位管理否决
    if not position_decision.get('should_open', True) and ai_action in ('LONG', 'SHORT'):
        ai_adjusted_strength = 0
        ai_action = 'HOLD'
        log.info(f"🤖 AI否决: 仓位管理师建议不开仓 {symbol}")

    return {
        'action': ai_action,
        'strength': max(0, min(100, ai_adjusted_strength)),
        'market_analysis': market_analysis,
        'macro_analysis': macro_analysis,
        'position_decision': position_decision,
        'leverage_override': position_decision.get('leverage'),
        'stop_loss_override': position_decision.get('stop_loss_pct'),
        'take_profit_override': position_decision.get('take_profit_pct'),
    }


# 导入CONFIG
import json
CONFIG_PATH = Path(__file__).parent / 'config.json'
with open(CONFIG_PATH, 'r') as f:
    CONFIG = json.load(f)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 开仓推送通知
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NOTIFY_FILE = Path(__file__).parent / 'notifications.json'
NOTIFY_LOG = Path(__file__).parent / 'notified_ids.json'


def notify_trade_open(symbol: str, side: str, price: float, strength: int, reason: str, 
                      stop_loss: float, take_profit: float, margin: float, leverage: int):
    """开仓通知：写入通知队列，等日历任务推送给用户"""
    import uuid
    notification = {
        'id': str(uuid.uuid4())[:8],
        'type': 'OPEN',
        'symbol': symbol,
        'side': side,
        'price': price,
        'strength': strength,
        'reason': reason,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'margin': round(margin, 2),
        'leverage': leverage,
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    
    # 追加到通知文件
    notifications = []
    if NOTIFY_FILE.exists():
        try:
            with open(NOTIFY_FILE, 'r') as f:
                notifications = json.load(f)
        except:
            notifications = []
    
    notifications.append(notification)
    # 只保留最近50条
    notifications = notifications[-50:]
    
    with open(NOTIFY_FILE, 'w') as f:
        json.dump(notifications, f, indent=2, ensure_ascii=False)
    
    log.info(f"📢 开仓通知已记录: {side} {symbol} @ {price}")


def notify_trade_close(symbol: str, side: str, entry: float, close_price: float, 
                       close_reason: str, pnl: float, pnl_pct: float):
    """平仓通知"""
    import uuid
    notification = {
        'id': str(uuid.uuid4())[:8],
        'type': 'CLOSE',
        'symbol': symbol,
        'side': side,
        'entry': entry,
        'close_price': close_price,
        'close_reason': close_reason,
        'pnl': round(pnl, 4),
        'pnl_pct': round(pnl_pct, 2),
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    
    notifications = []
    if NOTIFY_FILE.exists():
        try:
            with open(NOTIFY_FILE, 'r') as f:
                notifications = json.load(f)
        except:
            notifications = []
    
    notifications.append(notification)
    notifications = notifications[-50:]
    
    with open(NOTIFY_FILE, 'w') as f:
        json.dump(notifications, f, indent=2, ensure_ascii=False)
    
    emoji = '🟢' if pnl > 0 else '🔴'
    log.info(f"📢 平仓通知已记录: {emoji} {close_reason} {symbol} 盈亏={pnl:.2f}U")


def get_new_notifications():
    """获取未推送的通知（供日历任务调用）"""
    if not NOTIFY_FILE.exists():
        return []
    
    # 已推送的ID
    notified = []
    if NOTIFY_LOG.exists():
        try:
            with open(NOTIFY_LOG, 'r') as f:
                notified = json.load(f)
        except:
            notified = []
    
    # 读取所有通知
    try:
        with open(NOTIFY_FILE, 'r') as f:
            notifications = json.load(f)
    except:
        return []
    
    # 筛选未推送的
    new_notifs = [n for n in notifications if n['id'] not in notified]
    
    # 标记为已推送
    all_ids = list(set(notified + [n['id'] for n in new_notifs]))
    with open(NOTIFY_LOG, 'w') as f:
        json.dump(all_ids[-200:], f)  # 保留最近200个ID
    
    return new_notifs
