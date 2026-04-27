"""
交易模块配置管理器
管理 Bot 模式（推送 / 推送+交易）和所有交易参数
"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum

CONFIG_PATH = "/root/.openclaw/workspace/meme-bot/config.json"


class BotMode(Enum):
    SIGNAL_ONLY = "signal_only"      # 只推送信号，不交易
    SIGNAL_AND_TRADE = "signal_and_trade"  # 推送 + 自动/半自动执行


@dataclass
class PositionSizeConfig:
    """仓位大小配置"""
    # 资金基础（用于计算百分比）
    total_funds_sol: float = 10.0          # 总资金（SOL）
    
    # NEW 阶段（Bonding Curve < 30%）
    new_small_position_pct: float = 0.5     # 每笔 % 总资金
    new_medium_position_pct: float = 1.0    # 每笔 % 总资金（曲线 30%~60%）
    
    # MIGRATING 阶段
    migrating_position_pct: float = 3.0    # 每笔 % 总资金
    
    # 最大持仓上限
    max_total_position_pct: float = 10.0   # 总持仓上限 % 总资金
    max_new_positions: int = 3             # NEW 最大同时持仓数
    max_migrating_positions: int = 2       # MIGRATING 最大同时持仓数
    
    def position_sol(self, stage: str, bonding_pct: float = 0) -> float:
        """根据阶段计算实际 SOL 仓位"""
        total = self.total_funds_sol
        if stage == "MIGRATING":
            return total * (self.migrating_position_pct / 100)
        elif bonding_pct < 30:
            return total * (self.new_small_position_pct / 100)
        else:
            return total * (self.new_medium_position_pct / 100)


@dataclass
class RiskConfig:
    """风控参数"""
    # 余额门槛（低于此值自动切仅推送模式）
    min_balance_sol: float = 1.0
    
    # 每笔交易金额（SOL）
    amount_per_trade_sol: float = 1.0
    
    # 止盈（盈利到此百分比立即全卖）
    take_profit_pct: float = 35.0
    
    # 止损 %（0 = 不设置止损）
    stop_loss_pct: float = 0.0
    
    # 跟踪止损（从最高点回撤多少触发，0 = 不使用）
    trailing_stop_pct: float = 0.0
    
    # 日内风控
    daily_loss_limit_pct: float = 3.0      # 24h 最大亏损 % 总资金
    
    # 熔断
    consecutive_loss_limit: int = 3         # 连续亏损 N 笔 → 禁止开仓 1 小时
    circuit_break_cooldown_minutes: int = 60


@dataclass
class TradingConfig:
    """完整交易配置"""
    mode: str = BotMode.SIGNAL_ONLY.value
    
    # 评分阈值
    auto_threshold: int = 85              # ≥85 分 → 全自动执行（MIGRATING）
    semi_auto_threshold: int = 65          # ≥65 分 → 半自动（发送确认按钮）
    min_execute_threshold: int = 65         # <65 分 → 拒绝执行
    
    # 滑点
    slippage_new_pct: float = 5.0          # NEW 阶段滑点容忍
    slippage_migrating_pct: float = 2.0    # MIGRATING 阶段滑点容忍
    
    # 指令过期时间（秒）
    confirm_timeout_seconds: int = 300       # 5 分钟不确认则过期
    
    position: PositionSizeConfig = field(default_factory=PositionSizeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)


class ConfigManager:
    """配置文件读写"""
    
    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = config_path
        self.config = self._load()
    
    def _load(self) -> TradingConfig:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path) as f:
                    raw = json.load(f)
                return self._from_dict(raw)
            except Exception:
                pass
        return TradingConfig()
    
    def _from_dict(self, raw: dict) -> TradingConfig:
        # 安全反序列化（忽略未知字段）
        def _safe(data, cls):
            if data is None:
                return cls()
            known = {}
            for field_name in [f.name for f in cls.__dataclass_fields__.values()]:
                if field_name in data:
                    known[field_name] = data[field_name]
            return cls(**known)
        
        pos = _safe(raw.get("trading_position"), PositionSizeConfig)
        risk = _safe(raw.get("trading_risk"), RiskConfig)
        tc = TradingConfig(
            mode=raw.get("bot_mode", BotMode.SIGNAL_ONLY.value),
            auto_threshold=raw.get("auto_threshold", 85),
            semi_auto_threshold=raw.get("semi_auto_threshold", 65),
            min_execute_threshold=raw.get("min_execute_threshold", 65),
            slippage_new_pct=raw.get("slippage_new_pct", 5.0),
            slippage_migrating_pct=raw.get("slippage_migrating_pct", 2.0),
            confirm_timeout_seconds=raw.get("confirm_timeout_seconds", 300),
            position=pos,
            risk=risk,
        )
        return tc
    
    def save(self):
        raw = {
            "bot_mode": self.config.mode,
            "auto_threshold": self.config.auto_threshold,
            "semi_auto_threshold": self.config.semi_auto_threshold,
            "min_execute_threshold": self.config.min_execute_threshold,
            "slippage_new_pct": self.config.slippage_new_pct,
            "slippage_migrating_pct": self.config.slippage_migrating_pct,
            "confirm_timeout_seconds": self.config.confirm_timeout_seconds,
            "trading_position": asdict(self.config.position),
            "trading_risk": asdict(self.config.risk),
        }
        with open(self.config_path, "w") as f:
            json.dump(raw, f, indent=2)
    
    def set_mode(self, mode: BotMode):
        self.config.mode = mode.value
        self.save()
    
    def set_total_funds(self, sol: float):
        self.config.position.total_funds_sol = sol
        self.save()
    
    @property
    def mode(self) -> BotMode:
        return BotMode(self.config.mode)
    
    def is_trading_enabled(self) -> bool:
        return self.config.mode == BotMode.SIGNAL_AND_TRADE.value
