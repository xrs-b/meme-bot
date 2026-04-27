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
    
    def __init__(self, config: ConfigManager, pm: PositionManager, executor=None):
        self.config = config
        self.pm = pm
        self.executor = executor
        # 被忽略的代币地址 → 过期时间戳
        self._ignored_tokens: dict[str, float] = {}
        # 连续亏损计数
        self._consecutive_losses = 0
        # 熔断到期时间
        self._circuit_broken_until: float = 0
        # 余额不足提示已发送标记
        self._balance_alert_sent: bool = False
    
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
        核心规则：余额 < min_balance_sol → 自动切仅推送模式
        """
        address = token["address"]
        stage = token.get("stage", "NEW")
        min_balance = self.config.config.risk.min_balance_sol
        amount_per = self.config.config.risk.amount_per_trade_sol
        take_profit = self.config.config.risk.take_profit_pct
        stop_loss = self.config.config.risk.stop_loss_pct

        # 检查 Bot 模式
        if not self.config.is_trading_enabled():
            return DecisionResult(DecisionResult.REJECT, reason="Bot 处于信号模式")

        # 检查熔断
        if self._is_circuit_broken():
            remaining = int(self._circuit_broken_until - datetime.now(timezone.utc).timestamp())
            return DecisionResult(DecisionResult.REJECT, reason=f"熔断中，还剩 {remaining} 秒")

        # ─── 余额检查（核心）───────────────────────────────
        if self.executor:
            sol_balance = await self.executor.get_sol_balance()
            if sol_balance < min_balance:
                self.config.set_mode(BotMode.SIGNAL_ONLY)
                if not self._balance_alert_sent:
                    self._balance_alert_sent = True
                    logger.warning(f"余额 {sol_balance:.4f} SOL < {min_balance} SOL，已自动切换为仅推送模式")
                return DecisionResult(DecisionResult.REJECT,
                    reason=f"余额 {sol_balance:.4f} SOL < {min_balance} SOL，已自动切仅推送")
            else:
                if self._balance_alert_sent:
                    self._balance_alert_sent = False
                    logger.info(f"余额恢复 {sol_balance:.4f} SOL >= {min_balance} SOL")

        # 检查忽略列表
        if self._is_ignored(address):
            return DecisionResult(DecisionResult.IGNORED, reason="代币在冷却中")

        # 检查评分阈值
        if score < self.config.config.min_execute_threshold:
            return DecisionResult(DecisionResult.REJECT, reason=f"评分 {score} < {self.config.config.min_execute_threshold}，拒绝")

        # 检查是否已有持仓
        existing = self.pm.get_position_by_token(address)
        if existing:
            return DecisionResult(DecisionResult.IGNORED, reason=f"已持有 {token.get('symbol', '?')}，跳过")

        # 持仓上限（5个合约）
        if self.pm.get_open_count() >= 5:
            return DecisionResult(DecisionResult.REJECT, reason="同时持仓已达上限（5个）")

        # 评分达标 → 半自动确认
        semi_threshold = self.config.config.semi_auto_threshold
        if score >= semi_threshold:
            return DecisionResult(
                DecisionResult.CONFIRM,
                reason=f"半自动确认（{score}分 {stage}）",
                amount_sol=amount_per,
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_id=f"pos_{address[:8]}_{int(datetime.now(timezone.utc).timestamp())}"
            )

        return DecisionResult(DecisionResult.REJECT, reason=f"评分不足（{score}分）")
    
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
