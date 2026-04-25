#!/usr/bin/env python3
"""
Risk Manager for Meme Bot
Handles position limits, stop losses, take profits, and risk rules
"""

import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
import logging

from .models import Position, Chain, Signal, BotConfig, AlertMode
from .database import Database
from .alert_manager import AlertManager

logger = logging.getLogger(__name__)


@dataclass
class RiskRule:
    """A risk management rule"""
    name: str
    description: str = ""
    enabled: bool = True
    # For position-level rules
    max_position_size: float = 0.0      # Max SOL/BNB per position
    max_total_position: float = 0.0     # Max total SOL/BNB across all positions
    stop_loss_percent: float = 0.0      # Stop loss % (e.g., 20 = 20% loss)
    take_profit_percent: float = 0.0   # Take profit % (e.g., 100 = 100% gain)
    # For signal-level rules
    min_liquidity: float = 0.0          # Min pool liquidity
    min_volume_24h: float = 0.0         # Min 24h volume
    max_market_cap: float = 0.0         # Max market cap to consider
    # For time-based rules
    max_position_age_hours: float = 0.0 # Auto-close after X hours
    # For drawdown
    max_daily_loss: float = 0.0         # Max daily loss % before stop


@dataclass
class RiskStatus:
    """Current risk status"""
    total_exposure: float = 0.0        # Total SOL/BNB deployed
    daily_pnl: float = 0.0             # Today's P&L
    daily_loss: float = 0.0             # Today's loss
    positions_at_risk: int = 0          # Positions in drawdown
    rules_triggered: List[str] = field(default_factory=list)
    last_check: datetime = field(default_factory=datetime.utcnow)


class RiskManager:
    """
    Advanced risk management system.
    Evaluates trades and positions against configurable risk rules.
    """
    
    def __init__(
        self,
        db: Database,
        alert_manager: AlertManager,
        config: BotConfig
    ):
        self.db = db
        self.alert_manager = alert_manager
        self.config = config
        
        # Default risk rules
        self.rules = self._get_default_rules()
        
        # Override with config if provided
        self._apply_config()
        
        # Risk status
        self._status = RiskStatus()
        
        # Callbacks for when risk rules trigger
        self._on_liquidation: Optional[Callable] = None
        self._on_stop_loss: Optional[Callable] = None
        
        # Running state
        self._running = False
        self._tasks: List[asyncio.Task] = []
        
        # Position history for drawdown tracking
        self._daily_loss_reset = datetime.utcnow().date()
    
    def _get_default_rules(self) -> Dict[str, RiskRule]:
        """Get default risk rules"""
        return {
            'max_position_size': RiskRule(
                name="Max Position Size",
                max_position_size=1.0,  # 1 SOL/BNB per position
                description="Maximum size per single position"
            ),
            'max_total_position': RiskRule(
                name="Max Total Exposure",
                max_total_position=10.0,  # 10 SOL/BNB total
                description="Maximum total capital deployed"
            ),
            'stop_loss': RiskRule(
                name="Stop Loss",
                stop_loss_percent=20.0,  # 20% stop loss
                description="Auto-sell when position loses 20%"
            ),
            'take_profit': RiskRule(
                name="Take Profit",
                take_profit_percent=100.0,  # 100% take profit (2x)
                description="Auto-sell when position gains 100%"
            ),
            'min_liquidity': RiskRule(
                name="Min Liquidity",
                min_liquidity=1000.0,
                description="Ignore pools with less than $1K liquidity"
            ),
            'min_volume': RiskRule(
                name="Min 24h Volume",
                min_volume_24h=5000.0,
                description="Ignore tokens with less than $5K daily volume"
            ),
            'max_position_age': RiskRule(
                name="Max Position Age",
                max_position_age_hours=24.0,
                description="Auto-close positions older than 24h"
            ),
            'max_daily_loss': RiskRule(
                name="Max Daily Loss",
                max_daily_loss=30.0,
                description="Stop trading if daily loss exceeds 30%"
            ),
            'rug_pull_protection': RiskRule(
                name="Rug Pull Protection",
                enabled=True,
                description="Auto-sell if liquidity drops 50%+"
            ),
            'whale_watch': RiskRule(
                name="Whale Watch",
                enabled=True,
                description="Alert on large unusual trades (>10% of pool)"
            ),
        }
    
    def _apply_config(self):
        """Apply configuration to rules"""
        self.rules['max_position_size'].max_position_size = self.config.max_position_per_token
        self.rules['max_total_position'].max_total_position = self.config.max_total_position
        self.rules['min_liquidity'].min_liquidity = self.config.min_liquidity
        self.rules['min_volume'].min_volume_24h = self.config.min_volume_24h
    
    def set_callbacks(
        self,
        on_liquidation: Optional[Callable] = None,
        on_stop_loss: Optional[Callable] = None
    ):
        """Set callbacks for risk events"""
        self._on_liquidation = on_liquidation
        self._on_stop_loss = on_stop_loss
    
    async def start(self):
        """Start the risk manager"""
        self._running = True
        
        # Start risk monitoring loop
        monitor = asyncio.create_task(self._risk_monitor_loop())
        self._tasks.append(monitor)
        
        logger.info("RiskManager started")
        await self._alert_rules_status()
    
    async def stop(self):
        """Stop the risk manager"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("RiskManager stopped")
    
    async def _risk_monitor_loop(self):
        """Main risk monitoring loop"""
        while self._running:
            try:
                # Check every 30 seconds
                await asyncio.sleep(30)
                
                if not self._running:
                    break
                
                # Reset daily tracking if new day
                today = datetime.utcnow().date()
                if today > self._daily_loss_reset:
                    self._daily_loss_reset = today
                    self._status.daily_loss = 0.0
                    self._status.daily_pnl = 0.0
                
                # Check position rules
                await self._check_position_rules()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Risk monitor error: {e}")
    
    async def _check_position_rules(self):
        """Check rules against all positions"""
        # This would be called with actual positions from trading engine
        pass
    
    async def _alert_rules_status(self):
        """Send current rules status to Telegram"""
        lines = ["⚙️ *Risk Rules Active*\n"]
        
        for key, rule in self.rules.items():
            if not rule.enabled:
                continue
            
            status = "✅" if rule.enabled else "❌"
            
            if key == 'max_position_size':
                lines.append(f"{status} {rule.name}: `{rule.max_position_size} SOL/BNB`")
            elif key == 'max_total_position':
                lines.append(f"{status} {rule.name}: `{rule.max_total_position} SOL/BNB`")
            elif key == 'stop_loss':
                lines.append(f"{status} {rule.name}: `{rule.stop_loss_percent}%`")
            elif key == 'take_profit':
                lines.append(f"{status} {rule.name}: `{rule.take_profit_percent}%`")
            elif key == 'min_liquidity':
                lines.append(f"{status} {rule.name}: `${rule.min_liquidity:,.0f}`")
            elif key == 'min_volume':
                lines.append(f"{status} {rule.name}: `${rule.min_volume_24h:,.0f}`")
            elif key == 'max_position_age':
                lines.append(f"{status} {rule.name}: `{rule.max_position_age_hours}h`")
            elif key == 'max_daily_loss':
                lines.append(f"{status} {rule.name}: `{rule.max_daily_loss}%`")
        
        await self.alert_manager.send_message("\n".join(lines))
    
    # ============ PRE-TRADE RISK CHECKS ============
    
    def check_signal_risk(self, signal: Signal) -> Dict[str, Any]:
        """
        Check if a signal passes risk rules before trading.
        Returns dict with 'approved' bool and 'reasons' list.
        """
        approved = True
        reasons = []
        warnings = []
        
        # Check liquidity
        if signal.liquidity_at_signal < self.rules['min_liquidity'].min_liquidity:
            approved = False
            reasons.append(f"❌ 流动性过低: ${signal.liquidity_at_signal:,.0f} < ${self.rules['min_liquidity'].min_liquidity:,.0f}")
        elif signal.liquidity_at_signal < self.rules['min_liquidity'].min_liquidity * 2:
            warnings.append(f"⚠️ 流动性偏低: ${signal.liquidity_at_signal:,.0f}")
        
        # Check volume
        if signal.volume_24h_at_signal < self.rules['min_volume'].min_volume_24h:
            approved = False
            reasons.append(f"❌ 交易量过低: ${signal.volume_24h_at_signal:,.0f} < ${self.rules['min_volume'].min_volume_24h:,.0f}")
        
        # Check market cap
        market_cap = signal.metadata.get('market_cap_usd', 0)
        if self.rules['max_market_cap'].max_market_cap > 0 and market_cap > self.rules['max_market_cap'].max_market_cap:
            approved = False
            reasons.append(f"❌ 市值过高: ${market_cap:,.0f} > ${self.rules['max_market_cap'].max_market_cap:,.0f}")
        
        # Check score threshold
        if signal.score < self.config.min_signal_score:
            approved = False
            reasons.append(f"❌ 评分过低: {signal.score} < {self.config.min_signal_score}")
        
        return {
            'approved': approved,
            'reasons': reasons,
            'warnings': warnings,
            'score': signal.score
        }
    
    def check_position_risk(self, position: Position) -> Dict[str, Any]:
        """
        Check if a position should be liquidated based on risk rules.
        Returns dict with 'action' (none/stop_loss/take_profit/timeout) and details.
        """
        action = 'none'
        reason = ""
        urgency = 'low'
        
        # Check stop loss
        if position.pnl_percent <= -self.rules['stop_loss'].stop_loss_percent:
            action = 'stop_loss'
            reason = f"触发止损: {position.pnl_percent:.1f}% (规则: -{self.rules['stop_loss'].stop_loss_percent}%)"
            urgency = 'high'
        
        # Check take profit
        elif position.pnl_percent >= self.rules['take_profit'].take_profit_percent:
            action = 'take_profit'
            reason = f"触及止盈: {position.pnl_percent:+.1f}% (规则: +{self.rules['take_profit'].take_profit_percent}%)"
            urgency = 'medium'
        
        # Check max age
        elif self.rules['max_position_age'].max_position_age_hours > 0:
            age_hours = (datetime.utcnow() - position.opened_at).total_seconds() / 3600
            if age_hours >= self.rules['max_position_age'].max_position_age_hours:
                action = 'timeout'
                reason = f"仓位超时: {age_hours:.1f}h (规则: {self.rules['max_position_age'].max_position_age_hours}h)"
                urgency = 'low'
        
        return {
            'action': action,
            'reason': reason,
            'urgency': urgency,
            'pnl_percent': position.pnl_percent
        }
    
    def check_total_exposure(self, additional_amount: float, chain: Chain) -> Dict[str, Any]:
        """
        Check if adding more exposure would breach total limits.
        """
        current_total = self._status.total_exposure
        proposed_total = current_total + additional_amount
        max_total = self.rules['max_total_position'].max_total_position
        
        if proposed_total > max_total:
            return {
                'approved': False,
                'reason': f"总仓位超限: {proposed_total:.2f} > {max_total} SOL/BNB",
                'current': current_total,
                'max': max_total,
                'available': max_total - current_total
            }
        
        return {
            'approved': True,
            'current': current_total,
            'max': max_total,
            'available': max_total - proposed_total
        }
    
    def check_daily_loss_limit(self) -> Dict[str, Any]:
        """
        Check if daily loss limit has been breached.
        """
        daily_loss_pct = (self._daily_loss / self._status.total_exposure * 100) if self._status.total_exposure > 0 else 0
        
        if daily_loss_pct >= self.rules['max_daily_loss'].max_daily_loss:
            return {
                'breached': True,
                'daily_loss_percent': daily_loss_pct,
                'limit': self.rules['max_daily_loss'].max_daily_loss,
                'action': 'stop_trading'
            }
        
        return {
            'breached': False,
            'daily_loss_percent': daily_loss_pct,
            'limit': self.rules['max_daily_loss'].max_daily_loss
        }
    
    def update_position_pnl(self, pnl_percent: float, pnl_usd: float):
        """Update P&L tracking for daily loss calculation"""
        self._status.daily_pnl += pnl_usd
        
        if pnl_usd < 0:
            self._status.daily_loss += abs(pnl_usd)
            self._status.positions_at_risk += 1
        
        # Check if we need to alert or stop
        limit_check = self.check_daily_loss_limit()
        if limit_check['breached']:
            logger.warning(f"Daily loss limit breached: {limit_check['daily_loss_percent']:.1f}%")
            # Would trigger trading halt
    
    # ============ RISK REPORTING ============
    
    def get_risk_status(self) -> RiskStatus:
        """Get current risk status"""
        return self._status
    
    def get_risk_report(self) -> str:
        """Get a formatted risk status report"""
        lines = ["📊 *Risk Status*\n"]
        
        lines.append(f"💰 总敞口: `{self._status.total_exposure:.2f} SOL/BNB`")
        lines.append(f"📈 今日P&L: `{self._status.daily_pnl:+.2f} USD`")
        lines.append(f"📉 今日亏损: `{self._status.daily_loss:+.2f} USD`")
        lines.append(f"⚠️ 亏损仓位: `{self._status.positions_at_risk}`")
        
        if self._status.rules_triggered:
            lines.append(f"\n🚨 触发的规则:")
            for rule in self._status.rules_triggered:
                lines.append(f"  • {rule}")
        
        return "\n".join(lines)
    
    # ============ RULE MANAGEMENT ============
    
    def update_rule(self, rule_name: str, **kwargs):
        """Update a risk rule setting"""
        if rule_name in self.rules:
            for key, value in kwargs.items():
                if hasattr(self.rules[rule_name], key):
                    setattr(self.rules[rule_name], key, value)
            logger.info(f"Updated risk rule: {rule_name}")
    
    def enable_rule(self, rule_name: str):
        """Enable a risk rule"""
        if rule_name in self.rules:
            self.rules[rule_name].enabled = True
            logger.info(f"Enabled risk rule: {rule_name}")
    
    def disable_rule(self, rule_name: str):
        """Disable a risk rule"""
        if rule_name in self.rules:
            self.rules[rule_name].enabled = False
            logger.info(f"Disabled risk rule: {rule_name}")
    
    def get_rules(self) -> Dict[str, RiskRule]:
        """Get all risk rules"""
        return self.rules
