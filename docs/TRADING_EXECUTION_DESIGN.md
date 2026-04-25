# Meme Bot 交易执行模块设计方案

> 作者：AI 交易顾问 + 风控管理员  
> 日期：2026-04-25  
> 定位：scan_worker2.py 的下游执行模块，**不改动信号推送器主干**

---

## 一、当前情况

### 信号推送器（已稳定运行）
- **脚本**：`scan_worker2.py`
- **推送阈值**：≥50 分
- **最近 Top 分数**：21~43（大量时间在阈值以下）
- **已推送信号**：1 条（P2 上线后）
- **推送渠道**：Telegram（中文格式，含评分/风险/合约地址）

### 交易执行（未实现）
- 依赖缺失：`jupiter-python-sdk`、`raydium-sdk` 未安装
- `trading_engine.py` 是骨架代码，`execute_buy/execute_sell` 返回 `None`
- 当前架构是**纯信号推送**，没有仓位管理

---

## 二、核心设计原则

### 2.1 Pump.fun 代币经济学（必须理解）

```
Bonding Curve 价格机制：
- 曲线越往上走 → 价格越高（越接近 $69k 市值 = 毕业线）
- 在 0%~30% 曲线买入 → 性价比最高，但不确定能否毕业
- 超过 60% 曲线 → 大概率能毕业，但买入价格已高

毕业后的价格行为（历史规律）：
- 冲到 $80k~$100k → 立即砸盘到 $30k~50k
- 真正的好机会：毕业瞬间 + 回调站稳后再次放量上涨

所以：
- NEW 阶段代币 = 彩票（低买高卖要快）
- MIGRATING 阶段代币 = 相对确定的机会（已有基础流动性）
```

### 2.2 仓位大小设计

| 代币阶段 | 建议每笔仓位 | 最大总仓位 | 逻辑 |
|----------|-------------|-----------|------|
| **NEW**（曲线 <30%）| ≤0.5% 资金 | ≤3% 资金 | 赌毕业概率，低金额广撒网 |
| **NEW**（曲线 30%~60%）| ≤1% 资金 | 同上 | 有一定确定性，可略增 |
| **MIGRATING** | ≤3% 资金 | ≤10% 资金 | 即将毕业，确定性最高 |

**资金基础定义（可配置）：**
- 假设总资金 = 10 SOL（按实际修改）
- NEW 阶段每笔最大 0.05 SOL（约 $10 @ $200/SOL）
- MIGRATING 每笔最大 0.3 SOL（约 $60 @ $200/SOL）

### 2.3 止盈止损设计

```
NEW 阶段（快进快出）：
┌─────────────────────────────────────────┐
│  买入价                                       │
│   └─ 止盈：+30%~50% → 立即卖出50%仓位 │
│   └─ 止盈：+80%~100% → 全部卖出           │
│   └─ 止损：-10% → 立即认赔离场             │
│   └─ 毕业触发：bonding=100% → 观察后再操作  │
└─────────────────────────────────────────┘

MIGRATING 阶段（可以多拿一会儿）：
┌─────────────────────────────────────────┐
│  买入价                                       │
│   └─ 止盈：+50%~100% → 分批卖出            │
│   └─ 止损：-15% → 认赔离场                  │
│   └─ 跟踪止损：价格从最高点回撤20% → 全部出  │
└─────────────────────────────────────────┘
```

### 2.4 风险控制红线（绝对不可突破）

```
1. 单笔最大亏损：不超过总资金的 0.5%（= 0.05 SOL @ 10 SOL总资金）
2. 单日最大亏损：不超过总资金的 3%（= 0.3 SOL @ 10 SOL总资金）
3. 同时最大持仓：5 个代币（NEW最多3个 + MIGRATING最多2个）
4. 24小时内最多交易：5 笔
5. 以下情况直接拒绝开仓：
   - 代币分数 < 65（即使推送了也不执行）
   - 同一代币 2 小时内已买过
   - 总持仓已超过总资金 10%
   - 24小时内亏损已达上限
```

---

## 三、系统架构

```
scan_worker2.py（信号推送器，完整独立）
    │
    │ Telegram 推送（只发信号，不改动）
    │
    ▼
trade_signal_listener.py（新建：监听 Telegram Bot 指令）
    │
    ├──→ 信号通过 Telegram Bot 接收（按钮点击："/buy [token]"）
    │       （或全自动模式：无人工确认直接执行）
    │
    ▼
trade_decision_engine.py（新建：决策引擎）
    │
    ├── 风险检查序列：
    │   ① 评分检查（≥65 才执行，≥85 才全自动）
    │   ② 资金检查（单笔/总持仓/日亏损上限）
    │   ③ 重复检查（同代币 2h 内是否已买）
    │   ④ 滑点估算（滑点 > 3% 则放弃）
    │   ⑤ 合约安全复核（复扫一次 advanced-info）
    │
    ├── 评分分级：
    │   ≥85 + MIGRATING → 全自动执行（Full Auto）
    │   ≥75 + MIGRATING → 半自动（Bot 提示，你确认）
    │   ≥65 + NEW/MIGRATING → 半自动（Bot 提示，你确认）
    │   <65 → 拒绝执行，只记录
    │
    ▼
swap_executor.py（新建：执行层）
    │
    ├── pump.fun 买入：通过 pump.fun SDK 或直接构造交易指令
    │   → onchainos CLI 可能不直接支持 swap，需要 Solana SDK
    │
    ├── Jupiter 限价单：适合 MIGRATING 阶段（已有 DEX 流动性）
    │
    └── 执行结果记录到 SQLite（买入价格/时间/数量/手续费）
        │
        ▼
position_manager.py（新建：仓位管理）
    │
    ├── 追踪所有活跃持仓（买入价/当前价/PnL/持币时长）
    │
    ├── 止盈止损触发检查（每 10 秒一次）
    │   ├── 价格达标 → 自动发送 Telegram 确认 → 执行
    │   └── 止损达标 → 即时执行，不等确认
    │
    └── 毕业检测：bonding=100% → 发 Telegram 通知你决策
        │
        ▼
risk_guardian.py（新建：风控守护）
    │
    ├── 每日亏损统计（UTC 0点重置）
    ├── 持仓上限检查
    ├── 同向交易频率限制
    └── 异常熔断（连续 3 笔亏损 → 禁止开仓 1 小时）
        │
        ▼
Telegram 通知
    │
    ├── 新信号到达 → 买入确认（带按钮：✅确认 / ❌拒绝）
    ├── 止盈触发 → 确认是否卖出（带按钮）
    ├── 止损触发 → 已自动执行（通知）
    ├── 毕业信号 → 通知你决策是否留仓
    └── 交易记录 → 每日汇总 PnL
```

---

## 四、全自动 vs 半自动详细设计

### 4.1 全自动（Full Auto）— 条件最严格

**触发条件（必须同时满足）：**
1. 代币评分 ≥ **85** 分
2. 代币阶段 = **MIGRATING**（已完成 bonding，有真实流动性）
3. 老鼠仓 top10 < **30%**
4. Dev 持仓 < **5%**
5. 过去 24h 内此 Dev 没有Rug记录
6. 当前总持仓 < 总资金的 **5%**
7. 今日亏损 < 总资金的 **2%**

**执行参数：**
- 仓位：总资金的 **2%**（@10 SOL = 0.2 SOL）
- 止盈：+80% → 全卖
- 止损：-12% → 全卖
- 跟踪止损：最高点回撤 15% → 全卖
- 毕业时：bonding=100% → 发通知，不自动操作

**全自动核心逻辑：**
```
if all_full_auto_conditions(token):
    execute_buy(amount=总资金*0.02, slippage=0.02)
    set_take_profit(+80%)
    set_stop_loss(-12%)
    set_trailing_stop(回撤15%)
    log_position(op_type="FULL_AUTO")
    send_telegram(f"✅ 全自动买入 {token.symbol} @ {entry_price}")
else:
    # 不满足全自动，降级到半自动
    send_telegram_confirm(token, reason="评分不够/风险超标")
```

### 4.2 半自动（Semi-Auto）— 你的决策权

**触发条件（满足任一即可）：**
1. 评分 ≥ 65 + MIGRATING
2. 评分 ≥ 65 + NEW（曲线 > 30%）
3. 评分 ≥ 75 + NEW（任意曲线）

**执行参数：**
- 仓位：总资金的 **0.5%~1%**（NEW = 保守；MIGRATING = 略高）
- 止盈/止损：NEW = +30%/-8%；MIGRATING = +50%/-12%
- 毕业处理：100% bonding = 发通知，你决定留还是走

**半自动核心逻辑：**
```
if semi_auto_eligible(token):
    send_telegram_with_buttons(
        f"📩 半自动信号\n"
        f"{token.name}（{token.symbol}）\n"
        f"评分：{score}/100\n"
        f"阶段：{stage}\n"
        f"建议仓位：{amount} SOL\n"
        f"止盈：+{tp}% | 止损：-{sl}%\n\n"
        f"@你的用户名 请点击确认",
        buttons=[("✅ 确认买入", f"/buy {token.address}"),
                 ("❌ 拒绝", f"/ignore {token.address}")]
    )
    # 等待你点击按钮
    # Bot 收到 /buy → 执行
    # 你没反应 → 5分钟后自动过期
else:
    discard_signal(token)
```

---

## 五、交易执行细节

### 5.1 Pump.fun 买入（NEW 阶段）

NEW 阶段代币只能在 pump.fun 内部交易，不能通过 Jupiter。

**技术方案（优先顺序）：**
1. **使用 onchainos swap**：`onchainos swap` CLI（如果支持 pump.fun）
2. **Pump.fun SDK**：Python SDK via `pumpfun` 包
3. **手动构造交易**：用 `solana` Python SDK 构造代币买卖指令

**滑点策略：**
- NEW 阶段：设置 5% 滑点（bonding 曲线价格变动快）
- MIGRATING 阶段：设置 2% 滑点（已有 DEX 池）

### 5.2 MIGRATING 阶段卖出（切换到 DEX）

MIGRATING 阶段代币同时存在于 pump.fun bonding 曲线 + DEX（PumpSwap 或 Raydium）。

**最优路径：**
1. 先查 Jupiter 获取最佳 DEX 报价：`jupiter swap --input-mint X --output-mint USDC`
2. 如果 Jupiter 无路由 → 直接查 PumpSwap
3. 执行 DEX 卖出（滑点 2%）

### 5.3 手续费预算

| 操作 | 预估费用 |
|------|---------|
| Pump.fun 买入 | ~0.0005 SOL（变更计算）|
| DEX 卖出（Jupiter/PumpSwap）| ~0.0005 SOL |
| 失败重试（1次）| ~0.001 SOL |
| **单笔总费用上限** | **0.002 SOL** |

---

## 六、仓位管理

### 6.1 持仓追踪表（SQLite）

```sql
CREATE TABLE positions (
    id TEXT PRIMARY KEY,
    token_address TEXT NOT NULL,
    symbol TEXT,
    chain TEXT,
    stage TEXT,  -- 'NEW' or 'MIGRATING'
    entry_price REAL,
    entry_time TEXT,
    quantity REAL,
    cost_sol REAL,      -- 花费了多少 SOL
    stop_loss REAL,     -- 止损价格
    take_profit REAL,   -- 止盈价格
    trailing_pct REAL,   -- 跟踪止损百分比
    highest_price REAL,  -- 最高价（用于跟踪止损）
    score INTEGER,       -- 入场时评分
    status TEXT,         -- 'OPEN', 'CLOSED', 'STOPPED'
    close_time TEXT,
    close_price REAL,
    pnl_sol REAL,
    pnl_pct REAL,
    note TEXT
);
```

### 6.2 每日风险统计

```sql
CREATE TABLE daily_stats (
    date TEXT PRIMARY KEY,  -- UTC date
    total_trades INTEGER,
    winning_trades INTEGER,
    losing_trades INTEGER,
    total_pnl_sol REAL,
    total_pnl_usd REAL,
    largest_win_sol REAL,
    largest_loss_sol REAL,
    current_streak TEXT,  -- 'W3' or 'L5'
    auto_trades INTEGER,
    manual_trades INTEGER
);
```

---

## 七、风控守护规则

```
Rule 1: 单日熔断（Daily Circuit Breaker）
  - 24h 亏损 ≥ 3% 总资金 → 禁止开新仓直到下一个 UTC 天
  - 连续 3 笔亏损 → 禁止开仓 1 小时

Rule 2: 仓位隔离（Position Isolation）
  - NEW 代币：最多同时 3 个
  - MIGRATING 代币：最多同时 2 个
  - 全部代币：总持仓价值 ≤ 总资金的 10%

Rule 3: 毕业卖出规则
  - 代币 bonding = 100%（毕业）→ 发 Telegram 通知你
  - 你决定留仓 → 可以设置"留仓观察"，不设止盈上限
  - 你没反应 30 分钟 → 自动按市价分批卖出

Rule 4: Dev Rug 检测（运行时）
  - 持仓期间，每 5 分钟检查一次 advanced-info
  - 如果 Dev 突增持仓 > 5% → 立即发警报
  - 如果 top10 holder 突增 > 10% → 立即止损

Rule 5: 反向交易限制
  - 亏损状态下（持仓浮亏 > 5%）→ 禁止在同一代币加仓
  - 同一代币最多补仓 1 次
```

---

## 八、Telegram 命令清单

| 命令 | 功能 |
|------|------|
| `/status` | 查看当前总仓位/今日盈亏/开仓数 |
| `/positions` | 列出所有活跃持仓及 PnL |
| `/close [token_address]` | 手动平仓（立即市价卖出）|
| `/buy [token_address] [amount_sol]` | 手动买入（绕过自动审核）|
| `/ignore [token_address]` | 忽略该信号（未来1小时内不再提示）|
| `/profit` | 查看历史盈亏统计 |
| `/settings` | 查看/修改每笔仓位上限 |
| `/pause` | 暂停所有自动交易（保留持仓）|
| `/resume` | 恢复自动交易 |

---

## 九、技术实现依赖

```txt
# 交易执行必需
solana >= 0.31.0        # Solana SDK
solders >= 0.18.0       # Solana 交易签名
solders.keypair          # 钱包密钥

# Jupiter DEX 聚合（用于 MIGRATING 阶段卖出）
# jupiter-python-sdk      # 暂未安装，需要 pip install

# Pump.fun 专用
# pumpfun                  # 暂未安装，需要 pip install

# 可选（如果 onchainos 支持 swap）
# onchainos CLI            # 已有，但 swap 子命令未验证

# 风控和通知
apscheduler >= 3.10    # 定时任务（止盈止损检查）
aiohttp >= 3.9.0        # 异步 HTTP（价格查询）
```

---

## 十、优先级实施路线图

```
Phase 1（基础）：手动确认执行
  ① 安装 Solana SDK + pumpfun SDK
  ② 重写 swap_executor.py（只做 pump.fun 买入）
  ③ 实现 position_manager.py（持仓追踪 + 止盈止损）
  ④ Telegram 命令：/status、/positions、/close

Phase 2（自动化）：
  ⑤ 实现半自动决策引擎（评分过滤 + 风险检查）
  ⑥ Telegram 买入确认按钮
  ⑦ 实现 risk_guardian.py（熔断/日限）
  ⑧ 历史统计和 PnL 报表

Phase 3（全自动，仅高分代币）：
  ⑨ 全自动路径（≥85分 + MIGRATING）
  ⑩ Dev rug 实时监控
  ⑪ 毕业卖出自动化

Phase 4（增强）：
  ⑫ Jupiter DEX 卖出（降低滑点）
  ⑬ 复制交易（跟单知名钱包）
  ⑭ 多链支持（BSCBNB）
```

---

## 十一、关于全自动 vs 半自动的最终建议

> **核心判断：Pump.fun 信号适合半自动，不适合全自动。**

原因：
1. **评分门槛高但不可靠**：即使 85 分代币仍有极高随机性
2. **DeFi 仓位需要人工止盈**：MEME 的止盈时机通常只有几分钟
3. **毕业决策复杂**：bonding=100% 后的走势没有固定规律
4. **你作为交易员的价值**：能判断哪些 meme 会火，这是 AI 替代不了的

**建议最终方案：**
- **Phase 1-2 做完后**：设置为**半自动优先**
- 只有满足全自动条件（≥85分 + MIGRATING + 低风险指标）才弹窗
- 其他信号一律发送 Telegram，你来决策

这样既保证了在真正好机会出现时不会被错过，又保持了人工审核的安全性。
