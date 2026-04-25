"""
交易决策引擎
负责信号 → 是否执行的判断
包含所有风控规则
"""
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional
from .config_manager import ConfigManager, BotMode
from .position_manager import PositionManager

logger = logging.getLogger(__name__)


class DecisionResult:
    """决策结果"""
    EXECUTE_AUTO = "EXECUTE_AUTO"       # 全自动执行
    CONFIRM = "CONFIRM"                 # 需要人工确认
    REJECT = "REJECT"                   # 拒绝执行
    IGNORED = "IGNORED"                 # 被忽略（重复/冷却中）
    
    def __init__(self, verdict: str, reason: str = "",
                 amount_sol: float = 0, stop_loss: float = 0,
                 take_profit: float = 0, position_id: str = "",
                 note: str = ""):
        self.verdict = verdict
        self.reason = reason
        self.amount_sol = amount_sol
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.position_id = position_id
        self.note = note


class DecisionEngine:
    """
    信号 → 交易决策
    核心风控逻辑
    """
    
    def __init__(self, config: ConfigManager, pm: PositionManager):
        self.config = config
        self.pm = pm
        # 被忽略的代币地址 → 过期时间戳
        self._ignored_tokens: dict[str, float] = {}
        # 连续亏损计数
        self._consecutive_losses = 0
        # 熔断到期时间
        self._circuit_broken_until: float = 0
    
    def ignore_token(self, address: str, ttl_seconds: int = 3600):
        """标记忽略某代币"""
        self._ignored_tokens[address] = (
            datetime.now(timezone.utc).timestamp() + ttl_seconds
        )
        logger.info(f"Ignored token {address} for {ttl_seconds}s")
    
    def _is_ignored(self, address: str) -> bool:
        """检查代币是否在冷却中"""
        if address in self._ignored_tokens:
            if datetime.now(timezone.utc).timestamp() < self._ignored_tokens[address]:
                return True
            del self._ignored_tokens[address]
        return False
    
    def _is_circuit_broken(self) -> bool:
        """检查是否触发熔断"""
        if datetime.now(timezone.utc).timestamp() < self._circuit_broken_until:
            return True
        return False
    
    def _check_daily_loss_limit(self) -> bool:
        """检查今日亏损是否超限"""
        stats = self.pm.get_daily_stats()
        daily_loss = stats["total_pnl_sol"]
        limit = self.config.config.position.total_funds_sol * (
            self.config.config.risk.daily_loss_limit_pct / 100
        )
        return daily_loss <= -abs(limit)
    
    async def evaluate(self, token: dict, score: int) -> DecisionResult:
        """
        评估一个信号是否应该执行交易
        token: 代币信息（含 address, symbol, stage, bonding 等）
        score: 信号评分（0-100）
        
        返回：DecisionResult
        """
        address = token["address"]
        stage = token.get("stage", "NEW")
        
        # ─── 前置检查 ────────────────────────────────────────
        
        # 检查 Bot 模式
        if not self.config.is_trading_enabled():
            return DecisionResult(
                DecisionResult.REJECT,
                reason="Bot 处于信号模式（未开启交易）"
            )
        
        # 检查熔断
        if self._is_circuit_broken():
            remaining = int(self._circuit_broken_until - datetime.now(timezone.utc).timestamp())
            return DecisionResult(
                DecisionResult.REJECT,
                reason=f"熔断中，还剩 {remaining} 秒"
            )
        
        # 检查日亏损限制
        if self._check_daily_loss_limit():
            return DecisionResult(
                DecisionResult.REJECT,
                reason="今日亏损已达上限，禁止开仓"
            )
        
        # 检查忽略列表
        if self._is_ignored(address):
            return DecisionResult(
                DecisionResult.IGNORED,
                reason=f"代币 {address} 在冷却中"
            )
        
        # 检查评分阈值
        if score < self.config.config.min_execute_threshold:
            return DecisionResult(
                DecisionResult.REJECT,
                reason=f"评分 {score} < {self.config.config.min_execute_threshold}，拒绝执行"
            )
        
        # 检查是否已有持仓
        existing = self.pm.get_position_by_token(address)
        if existing:
            return DecisionResult(
                DecisionResult.IGNORED,
                reason=f"已持有 {token.get('symbol', '?')}（{existing.stage}），跳过"
            )
        
        # 检查同时持仓上限
        if self.pm.get_open_count() >= 5:
            return DecisionResult(
                DecisionResult.REJECT,
                reason="同时持仓已达上限（5个）"
            )
        
        if stage == "NEW" and self.pm.get_open_count("NEW") >= 3:
            return DecisionResult(
                DecisionResult.REJECT,
                reason="NEW 阶段持仓已达上限（3个）"
            )
        
        if stage == "MIGRATING" and self.pm.get_open_count("MIGRATING") >= 2:
            return DecisionResult(
                DecisionResult.REJECT,
                reason="MIGRATING 阶段持仓已达上限（2个）"
            )
        
        # 检查资金上限
        position_amount = self.config.config.position.position_sol(
            stage, token.get("bonding", 0)
        )
        total_cost = self.pm.get_total_cost_sol()
        max_total = self.config.config.position.total_funds_sol * (
            self.config.config.position.max_total_position_pct / 100
        )
        
        if total_cost + position_amount > max_total:
            return DecisionResult(
                DecisionResult.REJECT,
                reason=f"仓位超限（{total_cost:.2f} + {position_amount:.2f} > {max_total:.2f} SOL）"
            )
        
        # ─── 评分分层 ────────────────────────────────────────
        
        # 计算止盈止损
        if stage == "NEW":
            stop_loss = self.config.config.risk.stop_loss_pct
            take_profit = self.config.config.risk.new_take_profit_pct
        else:
            stop_loss = self.config.config.risk.stop_loss_pct * 1.2
            take_profit = self.config.config.risk.migrating_take_profit_pct
        
        # 全自动条件：≥85分 + MIGRATING + 低风险
        auto_threshold = self.config.config.auto_threshold
        semi_threshold = self.config.config.semi_auto_threshold
        
        is_migrating = stage == "MIGRATING"
        is_high_score = score >= auto_threshold
        is_mid_score = score >= semi_threshold
        
        # 风险指标检查（仅影响全自动）
        dev_holding = token.get("dev_holding", 0)
        top10 = token.get("top10", 0)
        is_safe = (dev_holding < 5 and top10 < 30)
        
        if is_high_score and is_migrating and is_safe:
            return DecisionResult(
                DecisionResult.EXECUTE_AUTO,
                reason=f"✅ 全自动执行（{score}分 MIGRATING）",
                amount_sol=position_amount,
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_id=f"pos_{address[:8]}_{int(datetime.now(timezone.utc).timestamp())}"
            )
        
        if is_mid_score:
            return DecisionResult(
                DecisionResult.CONFIRM,
                reason=f"📩 半自动确认（{score}分 {stage}）",
                amount_sol=position_amount,
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_id=f"pos_{address[:8]}_{int(datetime.now(timezone.utc).timestamp())}"
            )
        
        return DecisionResult(
            DecisionResult.REJECT,
            reason=f"评分不足（{score}分）"
        )
    
    def on_trade_result(self, pnl_sol: float, was_successful: bool):
        """
        通知决策引擎交易结果（用于更新风控状态）
        """
        if was_successful:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.config.config.risk.consecutive_loss_limit:
                self._circuit_broken_until = (
                    datetime.now(timezone.utc).timestamp()
                    + self.config.config.risk.circuit_break_cooldown_minutes * 60
                )
                logger.warning(
                    f"Circuit breaker triggered! {self._consecutive_losses} consecutive losses. "
                    f"Trading disabled for {self.config.config.risk.circuit_break_cooldown_minutes} min"
                )
    
    def cleanup_ignored(self):
        """清理过期的忽略记录"""
        now = datetime.now(timezone.utc).timestamp()
        expired = [addr for addr, ts in self._ignored_tokens.items() if now >= ts]
        for addr in expired:
            del self._ignored_tokens[addr]
