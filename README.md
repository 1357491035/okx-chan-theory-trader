# ChanTrader-OKX 🚀

基于**缠论 + MACD + BOLL + KDJ**三指标共振的加密货币合约短线交易系统，支持 OKX 交易所，内置纸面交易验证和消息面分析。

## 核心特性

- **缠论级别联动**：15分钟定方向 → 5分钟找背驰 → 1分钟确认入场
- **三指标共振**：MACD背驰/金叉死叉 + BOLL轨道位置 + KDJ超买超卖，多信号打分（满分100），≥60分才开仓
- **三级消息面分析**：HIGH（黑天鹅/重大事件 ±15分）> MED（巨鲸/名人/政策 ±10分）> LOW（一般新闻 ±3分），以技术面为基础，消息面按影响力调整
- **动态热门币发现**：自动扫描 OKX 高成交额/高涨幅合约，捕捉爆发机会
- **纸面交易模式**：真实行情 + 虚拟下单，100笔胜率≥80%后才切入实盘
- **自动总结优化**：分币种胜率、方向分析、策略优化建议
- **严格风控**：3%止损、6%止盈、最多3个持仓、单仓≤10%资金

## 信号系统

### 做多条件（得分≥60）

| 信号 | 分值 |
|------|------|
| 15m趋势向上 | 30 |
| 5m MACD零轴上金叉 | 25 |
| 5m底背驰 | 20 |
| 5m BOLL下轨/回踩中轨 | 15 |
| 5m KDJ低位金叉 | 15 |
| 5m KDJ低位拐头(J<20→>20) | 12 |
| 1m确认信号 | 10 |

### 做空条件（得分≥60）

| 信号 | 分值 |
|------|------|
| 15m趋势向下 | 30 |
| 5m MACD零轴下死叉 | 25 |
| 5m顶背驰 | 20 |
| 5m BOLL上轨 | 15 |
| 5m KDJ高位死叉 | 15 |
| 5m KDJ高位拐头(J>80→<80) | 12 |
| 1m确认信号 | 10 |

### 消息面三级影响

| 级别 | 触发条件 | 调整幅度 | 说明 |
|------|----------|----------|------|
| HIGH | 黑客攻击、跑路、下架、ETF批准等 | ±15分 | 可压信号到HOLD，技术面失效 |
| MED | 巨鲸抛售/买入、名人动作、监管 | ±10分 | 显著调整但不压到HOLD |
| LOW | 一般涨跌新闻 | ±3分 | 微调，不动方向 |

## 安装

```bash
git clone https://github.com/YOUR_USERNAME/ChanTrader-OKX.git
cd ChanTrader-OKX
pip install ccxt ta pandas
```

## 配置

复制配置模板并填入你的 OKX API 信息：

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
  "okx": {
    "api_key": "YOUR_API_KEY",
    "api_secret": "YOUR_SECRET_KEY",
    "passphrase": "YOUR_PASSPHRASE",
    "testnet": false
  },
  "trading": {
    "symbols": [
      "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
      "DOGE/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT",
      "AVAX/USDT:USDT", "LINK/USDT:USDT", "DOT/USDT:USDT",
      "UNI/USDT:USDT", "APT/USDT:USDT", "ARB/USDT:USDT",
      "OP/USDT:USDT", "PEPE/USDT:USDT", "WIF/USDT:USDT",
      "FIL/USDT:USDT", "NEAR/USDT:USDT", "SUI/USDT:USDT"
    ],
    "leverage": 5,
    "risk": {
      "max_position_pct": 0.1,
      "stop_loss_pct": 0.03,
      "take_profit_pct": 0.06,
      "max_daily_loss_pct": 0.05
    },
    "max_positions": 3,
    "min_signal_strength": 60
  }
}
```

> ⚠️ 如在中国大陆使用，需配置 HTTP 代理。系统默认使用 `http://127.0.0.1:7890`，可在 `trader.py` 中修改 `proxies` 配置。

## 使用方法

### 纸面交易（推荐先用这个验证策略）

```bash
# 单次扫描
python3 trader.py --paper

# 持续运行（每5分钟扫描一次）
python3 trader.py --paper-loop --interval 300

# 查看统计
python3 trader.py --stats

# 设置初始资金
python3 trader.py --paper --capital 2000
```

### 实盘交易（⚠️ 谨慎使用）

```bash
# 查看余额
python3 trader.py --balance

# 查看持仓
python3 trader.py --positions

# 单次扫描
python3 trader.py --scan

# 持续运行
python3 trader.py --loop --interval 300
```

## 交易流程

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  15m 定方向  │───▶│  5m 找背驰  │───▶│  1m 确认入场 │
│  趋势判断    │    │  信号打分    │    │  精确时机    │
└─────────────┘    └─────────────┘    └─────────────┘
                         │
                    ┌────▼────┐
                    │ 消息面调整│
                    │ HIGH/MED/LOW │
                    └────┬────┘
                         │
                    ┌────▼────┐
                    │ ≥60分开仓│
                    │ 带止盈止损│
                    └─────────┘
```

## 风控规则

- **止损**：3% 无条件走，不扛单
- **止盈**：6% 落袋，不贪
- **仓位**：单仓 ≤ 10% 资金，最多同时 3 个持仓
- **杠杆**：默认 5x（可在 config.json 调整）
- **日最大亏损**：5%

## 技术指标详解

### MACD（核心背驰判断）
- **ABC三段法**：A段力度最大，B段回调拉黄白线回0轴，C段柱子面积<A段即背驰
- **零轴上金叉**：强买信号（+25分）
- **零轴下金叉**：弱反弹（+15分）
- **面积估算**：柱子伸长变慢时，已出面积×2预判

### BOLL（轨道定位）
- **BOLL位置 < 0.2**：下轨附近，做多加分
- **BOLL位置 > 0.8**：上轨附近，做空加分
- **收口**：变盘信号，收口越窄越久开口力度越大
- **中轨回踩不破**：二买信号

### KDJ（动量确认）
- **J < 0 拐头**：低位拐头，强买入（+12分）
- **J > 100 拐头**：高位拐头，强卖出（+12分）
- **K < 20 金叉**：超卖金叉（+15分）
- **K > 80 死叉**：超买死叉（+15分）
- **50线**：多空分水岭

## 缠论核心概念

- **笔**：相邻顶底分型之间的连线
- **线段**：至少3笔构成
- **中枢**：至少3个次级别线段重叠区域
- **一类买点**：趋势背驰点（底背驰）
- **二类买点**：回踩不破前低
- **三类买点**：突破中枢回踩不回中枢
- **级别联动**：大级别定方向，小级别找买卖点

## ⚠️ 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。加密货币合约交易具有极高风险，可能导致全部本金损失。使用本系统进行实盘交易的一切后果由使用者自行承担。

## License

MIT
