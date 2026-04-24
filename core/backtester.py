#!/usr/bin/env python3
"""
Backtester for Meme Bot
Historical analysis of signal quality and trading strategies
"""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import defaultdict
import statistics

from .models import Signal, Trade, Chain, SignalType, BotConfig
from .database import Database

import logging
logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Results from a backtest run"""
    total_signals: int = 0
    signals_traded: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    
    total_pnl_usd: float = 0.0
    total_pnl_percent: float = 0.0
    avg_pnl_percent: float = 0.0
    best_trade_percent: float = 0.0
    worst_trade_percent: float = 0.0
    
    win_rate: float = 0.0
    avg_win_percent: float = 0.0
    avg_loss_percent: float = 0.0
    profit_factor: float = 0.0
    
    # Timing stats
    avg_hold_time_minutes: float = 0.0
    max_hold_time_hours: float = 0.0
    
    # Signal quality
    signals_by_score: Dict[int, Dict] = field(default_factory=dict)
    score_distribution: Dict[str, int] = field(default_factory=dict)
    
    # Chain breakdown
    sol_stats: Dict = field(default_factory=dict)
    bsc_stats: Dict = field(default_factory=dict)
    
    # Time-based analysis
    hourly_returns: Dict[int, List[float]] = field(default_factory=dict)
    daily_returns: Dict[str, float] = field(default_factory=dict)
    
    # Detailed trade log
    trade_log: List[Dict] = field(default_factory=list)


@dataclass
class Backtester:
    """
    Backtesting engine for meme coin trading signals.
    Analyzes historical signals and simulated trades.
    """
    
    def __init__(self, db: Database, config: BotConfig):
        self.db = db
        self.config = config
        
        # Backtest parameters
        self.initial_capital = 1000.0  # Starting capital in USD
        self.position_size = 10.0       # Per trade in USD
        self.slippage = 0.5             # % slippage assumption
    
    async def run_backtest(
        self,
        chain: Optional[Chain] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        min_score: int = 0,
        strategies: Optional[List[str]] = None
    ) -> BacktestResult:
        """
        Run a backtest over a date range.
        
        Args:
            chain: Filter by chain (SOL/BSC), None for both
            start_date: Start of backtest period
            end_date: End of backtest period
            min_score: Minimum signal score to consider
            strategies: List of strategy names to test
        """
        # Default dates: last 7 days
        if end_date is None:
            end_date = datetime.utcnow()
        if start_date is None:
            start_date = end_date - timedelta(days=7)
        
        result = BacktestResult()
        
        # Get signals for period
        signals = self._get_signals_for_period(chain, start_date, end_date, min_score)
        result.total_signals = len(signals)
        
        # Get trades for period
        trades = self._get_trades_for_period(chain, start_date, end_date)
        
        # Analyze signals by score
        result.score_distribution = self._analyze_score_distribution(signals)
        result.signals_by_score = self._analyze_signals_by_score(signals)
        
        # Simulate trades if no real trades
        if not trades and strategies:
            trades = await self._simulate_trades(signals, strategies)
        
        result.trades = len(trades)
        
        # Calculate P&L
        if trades:
            pnl_data = self._calculate_pnl(trades)
            for key, value in pnl_data.items():
                setattr(result, key, value)
            
            result.trade_log = self._build_trade_log(trades)
        
        # Time-based analysis
        result.hourly_returns = self._analyze_hourly_returns(trades)
        result.daily_returns = self._analyze_daily_returns(trades)
        
        # Chain breakdown
        result.sol_stats = self._get_chain_stats(trades, Chain.SOLANA)
        result.bsc_stats = self._get_chain_stats(trades, Chain.BSC)
        
        return result
    
    def _get_signals_for_period(
        self,
        chain: Optional[Chain],
        start: datetime,
        end: datetime,
        min_score: int
    ) -> List[Signal]:
        """Get signals from database for a period"""
        # Get all recent signals and filter
        if chain:
            signals = self.db.get_recent_signals(chain, hours=9999, min_score=min_score)
        else:
            sol_signals = self.db.get_recent_signals(Chain.SOLANA, hours=9999, min_score=min_score)
            bsc_signals = self.db.get_recent_signals(Chain.BSC, hours=9999, min_score=min_score)
            signals = sol_signals + bsc_signals
        
        # Filter by date range
        filtered = [s for s in signals if start <= s.detected_at <= end]
        
        return filtered
    
    def _get_trades_for_period(
        self,
        chain: Optional[Chain],
        start: datetime,
        end: datetime
    ) -> List[Trade]:
        """Get trades from database for a period"""
        trades = []
        
        if chain is None or chain == Chain.SOLANA:
            sol_trades = self.db.get_recent_trades(Chain.SOLANA, hours=9999)
            trades.extend(sol_trades)
        
        if chain is None or chain == Chain.BSC:
            bsc_trades = self.db.get_recent_trades(Chain.BSC, hours=9999)
            trades.extend(bsc_trades)
        
        # Filter by date
        filtered = [t for t in trades if start <= t.executed_at <= end]
        
        return filtered
    
    async def _simulate_trades(
        self,
        signals: List[Signal],
        strategies: List[str]
    ) -> List[Trade]:
        """
        Simulate trades based on signals and strategies.
        This is a simplified simulation - real backtesting would need price feeds.
        """
        simulated_trades = []
        
        for signal in signals:
            if 'basic' in strategies or 'all' in strategies:
                # Basic strategy: trade on signals above threshold
                if signal.score >= self.config.min_signal_score:
                    # Simulate a trade
                    trade = self._simulate_trade_from_signal(signal)
                    if trade:
                        simulated_trades.append(trade)
            
            if 'aggressive' in strategies:
                # Aggressive: trade all signals with any score
                trade = self._simulate_trade_from_signal(signal, position_size=self.position_size * 2)
                if trade:
                    simulated_trades.append(trade)
            
            if 'conservative' in strategies:
                # Conservative: only very high score signals
                if signal.score >= 80:
                    trade = self._simulate_trade_from_signal(signal, position_size=self.position_size * 0.5)
                    if trade:
                        simulated_trades.append(trade)
        
        return simulated_trades
    
    def _simulate_trade_from_signal(
        self,
        signal: Signal,
        position_size: Optional[float] = None
    ) -> Optional[Trade]:
        """Simulate a trade from a signal (simplified)"""
        # This would need real price data to be accurate
        # Using placeholder calculations
        size = position_size or self.position_size
        
        # Assume we buy at signal price
        buy_price = signal.price_at_signal
        if buy_price <= 0:
            return None
        
        # Simulate exit price (with slippage and random outcome)
        # In real backtest, would use actual price history
        slippage_factor = 1 + (self.slippage / 100)
        
        # Random outcome based on historical win rate assumption (40%)
        import random
        is_win = random.random() < 0.4
        
        if is_win:
            # Win: 50-200% gain
            exit_factor = random.uniform(1.5, 3.0)
        else:
            # Loss: 10-50% loss
            exit_factor = random.uniform(0.5, 0.9)
        
        exit_price = buy_price * exit_factor * slippage_factor
        pnl_percent = (exit_price - buy_price) / buy_price * 100
        pnl_usd = size * (pnl_percent / 100)
        
        return Trade(
            id=f"sim_{signal.id}",
            chain=signal.chain,
            action="buy" if pnl_percent > 0 else "sell",
            token_address=signal.token.address,
            token_symbol=signal.token.symbol,
            amount_in=size,
            amount_out=size / buy_price,
            price=buy_price,
            value_usd=size,
            tx_hash="simulated",
            wallet_address="backtest",
            signal_id=signal.id,
            executed_at=signal.detected_at,
            pnl_percent=pnl_percent,
            pnl_usd=pnl_usd
        )
    
    def _calculate_pnl(self, trades: List[Trade]) -> Dict[str, Any]:
        """Calculate P&L statistics from trades"""
        if not trades:
            return {}
        
        sell_trades = [t for t in trades if t.action.value == 'sell' and t.pnl_percent is not None]
        
        if not sell_trades:
            return {'trades': len(trades)}
        
        wins = [t for t in sell_trades if t.pnl_percent > 0]
        losses = [t for t in sell_trades if t.pnl_percent <= 0]
        
        total_pnl = sum(t.pnl_usd for t in sell_trades)
        total_pnl_percent = sum(t.pnl_percent for t in sell_trades)
        
        return {
            'trades': len(sell_trades),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': len(wins) / len(sell_trades) * 100 if sell_trades else 0,
            'total_pnl_usd': total_pnl,
            'total_pnl_percent': total_pnl_percent,
            'avg_pnl_percent': total_pnl_percent / len(sell_trades) if sell_trades else 0,
            'best_trade_percent': max(t.pnl_percent for t in sell_trades) if sell_trades else 0,
            'worst_trade_percent': min(t.pnl_percent for t in sell_trades) if sell_trades else 0,
            'avg_win_percent': statistics.mean([t.pnl_percent for t in wins]) if wins else 0,
            'avg_loss_percent': statistics.mean([t.pnl_percent for t in losses]) if losses else 0,
            'profit_factor': abs(sum(t.pnl_percent for t in wins) / sum(t.pnl_percent for t in losses)) if losses and sum(t.pnl_percent for t in losses) != 0 else 0
        }
    
    def _analyze_signals_by_score(self, signals: List[Signal]) -> Dict[int, Dict]:
        """Analyze signal outcomes by score bucket"""
        buckets = {
            '90-100': [],
            '80-89': [],
            '70-79': [],
            '60-69': [],
            '50-59': [],
            '40-49': [],
            '<40': []
        }
        
        for s in signals:
            if s.score >= 90:
                buckets['90-100'].append(s)
            elif s.score >= 80:
                buckets['80-89'].append(s)
            elif s.score >= 70:
                buckets['70-79'].append(s)
            elif s.score >= 60:
                buckets['60-69'].append(s)
            elif s.score >= 50:
                buckets['50-59'].append(s)
            elif s.score >= 40:
                buckets['40-49'].append(s)
            else:
                buckets['<40'].append(s)
        
        return {
            bucket: {
                'count': len(sigs),
                'avg_liquidity': statistics.mean([s.liquidity_at_signal for s in sigs]) if sigs else 0,
                'avg_volume': statistics.mean([s.volume_24h_at_signal for s in sigs]) if sigs else 0
            }
            for bucket, sigs in buckets.items()
        }
    
    def _analyze_score_distribution(self, signals: List[Signal]) -> Dict[str, int]:
        """Get count of signals per score range"""
        dist = defaultdict(int)
        for s in signals:
            dist[f"{s.score}"] += 1
        return dict(dist)
    
    def _analyze_hourly_returns(self, trades: List[Trade]) -> Dict[int, List[float]]:
        """Analyze returns by hour of day"""
        hourly = defaultdict(list)
        for t in trades:
            if t.pnl_percent is not None:
                hour = t.executed_at.hour
                hourly[hour].append(t.pnl_percent)
        return dict(hourly)
    
    def _analyze_daily_returns(self, trades: List[Trade]) -> Dict[str, float]:
        """Analyze returns by day"""
        daily = defaultdict(float)
        for t in trades:
            if t.pnl_percent is not None:
                day = t.executed_at.strftime('%Y-%m-%d')
                daily[day] += t.pnl_usd
        return dict(daily)
    
    def _get_chain_stats(self, trades: List[Trade], chain: Chain) -> Dict[str, Any]:
        """Get stats for a specific chain"""
        chain_trades = [t for t in trades if t.chain == chain]
        sells = [t for t in chain_trades if t.action.value == 'sell']
        
        return {
            'total_trades': len(chain_trades),
            'completed_trades': len(sells),
            'total_pnl': sum(t.pnl_usd for t in sells) if sells else 0,
            'win_rate': len([t for t in sells if t.pnl_percent > 0]) / len(sells) * 100 if sells else 0
        }
    
    def _build_trade_log(self, trades: List[Trade]) -> List[Dict]:
        """Build detailed trade log"""
        return [
            {
                'id': t.id,
                'chain': t.chain.value,
                'symbol': t.token_symbol,
                'action': t.action.value,
                'amount': t.amount_in,
                'pnl_percent': t.pnl_percent,
                'pnl_usd': t.pnl_usd,
                'executed_at': t.executed_at.isoformat(),
                'signal_id': t.signal_id
            }
            for t in trades
        ]
    
    def format_backtest_report(self, result: BacktestResult) -> str:
        """Format backtest result as a readable report"""
        lines = [
            "📊 *回测报告*\n",
            "━━━━━━ 总体统计 ━━━━━━",
            f"📡 总信号数: `{result.total_signals}`",
            f"📈 交易次数: `{result.trades}`",
            f"🪙 盈利交易: `{result.wins}`",
            f"🪙 亏损交易: `{result.losses}`",
            f"📊 胜率: `{result.win_rate:.1f}%`",
            "",
            "━━━━━━ P&L 详情 ━━━━━━",
            f"💰 总收益: `${result.total_pnl_usd:+.2f}`",
            f"📈 总收益率: `{result.total_pnl_percent:+.2f}%`",
            f"📊 平均单笔收益: `{result.avg_pnl_percent:+.2f}%`",
            f"🏆 最佳交易: `{result.best_trade_percent:+.2f}%`",
            f"💔 最差交易: `{result.worst_trade_percent:+.2f}%`",
            "",
            "━━━━━━ 风险指标 ━━━━━━",
            f"📈 平均盈利: `{result.avg_win_percent:+.2f}%`",
            f"📉 平均亏损: `{result.avg_loss_percent:+.2f}%`",
            f"⚖️ 盈亏比: `{result.profit_factor:.2f}`",
            "",
        ]
        
        if result.sol_stats:
            lines.extend([
                "━━━━━━ SOL 链 ━━━━━━",
                f"🟣 交易数: `{result.sol_stats.get('total_trades', 0)}`",
                f"📊 胜率: `{result.sol_stats.get('win_rate', 0):.1f}%`",
                f"💰 P&L: `${result.sol_stats.get('total_pnl', 0):+.2f}`",
                ""
            ])
        
        if result.bsc_stats:
            lines.extend([
                "━━━━━━ BSC 链 ━━━━━━",
                f"🟠 交易数: `{result.bsc_stats.get('total_trades', 0)}`",
                f"📊 胜率: `{result.bsc_stats.get('win_rate', 0):.1f}%`",
                f"💰 P&L: `${result.bsc_stats.get('total_pnl', 0):+.2f}`",
                ""
            ])
        
        # Score distribution
        if result.signals_by_score:
            lines.extend([
                "━━━━━━ 信号评分分布 ━━━━━━"
            ])
            for bucket, data in result.signals_by_score.items():
                if data['count'] > 0:
                    lines.append(f"{bucket}: `{data['count']}` 条信号")
        
        return "\n".join(lines)
