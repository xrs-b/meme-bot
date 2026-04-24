# 🐸 Meme Bot - Cross-Chain Meme Coin Trading Bot

监控 SOL 链和 BSC 链上的 Meme 币，支持跟单、交易、一键清仓、Telegram 告警。

## 功能特性

- 📡 **新币监控** - 实时发现新上线的 Meme 币
- 💧 **流动性监控** - 监控池子变化，发现埋伏机会
- 📈 **交易量预警** - 异动检测，提前发现潜力币
- 👛 **跟单交易** - 自动跟随聪明钱钱包的操作
- 🤖 **自动交易** - 支持全自动买卖，通知+自动下单两种模式
- 💰 **一键清仓** - 总持仓或单币随时清仓
- 🔔 **Telegram 告警** - 实时推送信号和交易通知
- 💾 **数据持久化** - SQLite 存储，支持自动清理过期数据

## 支持的链

| 链 | DEX | 状态 |
|---|---|---|
| 🟣 Solana (SOL) | Jupiter, Raydium, Orca | ✅ |
| 🟠 BSC (BNB Chain) | PancakeSwap | ✅ |

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

复制配置文件并填入你的信息：

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
  "solana_rpc": "https://api.mainnet-beta.solana.com",
  "bsc_rpc": "https://bsc-dataseed.binance.org",
  "wallet_address": "你的钱包地址",
  "wallet_private_key": "你的私钥（注意安全！）",
  "telegram_bot_token": "Telegram Bot Token",
  "telegram_chat_id": "你的 Chat ID",
  "alert_mode": "notify_only",
  "copy_trade_enabled": false,
  "min_signal_score": 60
}
```

### 3. 运行

```bash
python meme_bot.py
```

## Telegram 命令

| 命令 | 说明 |
|---|---|
| `/status` | 机器人状态 |
| `/positions sol` | 查看 SOL 链持仓 |
| `/positions bsc` | 查看 BSC 链持仓 |
| `/signals sol` | 最近 SOL 信号 |
| `/stats sol` | SOL 链统计数据 |
| `/liquidate sol` | 清仓所有 SOL 持仓 |
| `/liquidate <token>` | 清仓指定代币 |
| `/mode auto` | 切换到自动交易模式 |
| `/mode notify` | 切换到通知模式 |
| `/copy on` | 开启跟单 |
| `/copy off` | 关闭跟单 |
| `/addwallet <地址> sol` | 添加跟单钱包 |
| `/help` | 帮助信息 |

## 项目结构

```
meme-bot/
├── core/                    # 核心模块
│   ├── models.py            # 数据模型
│   ├── database.py          # SQLite 数据库
│   ├── alert_manager.py     # Telegram 告警
│   ├── signal_detector.py   # 信号检测引擎
│   ├── trading_engine.py    # 交易引擎
│   └── copy_trader.py       # 跟单引擎
├── solana/                  # Solana 适配器
│   └── adapter.py           # Solana 链交互
├── bsc/                     # BSC 适配器
│   └── adapter.py           # BSC 链交互
├── meme_bot.py              # 主程序入口
├── config.example.json      # 配置示例
└── requirements.txt         # 依赖
```

## 安全提醒

⚠️ **重要**：
- 切勿将私钥直接写在配置文件中，使用环境变量更安全
- 生产环境建议使用硬件钱包或专属交易钱包
- 自动交易模式有风险，请先在通知模式下观察信号质量
- Meme 币波动极大，请务必设置止损

## 信号评分

信号评分 0-100，分数越高机会越好：

| 分数 | 评级 | 说明 |
|---|---|---|
| 80+ | 🟢 极好 | 强烈建议关注 |
| 60-79 | 🟡 良好 | 可以考虑 |
| 40-59 | 🟠 一般 | 观望 |
| <40 | 🔴 差 | 忽略 |

## 开发说明

基于开源项目改进，参考了：
- Trojan (SOL 跟单机器人)
- GMGN AI (钱包追踪)
- 各链的 DEX SDK（Jupiter、Raydium、PancakeSwap）

## License

MIT License - 仅供参考学习，使用风险自担。
