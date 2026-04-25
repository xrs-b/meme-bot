#!/usr/bin/env python3
"""
Copy Trading Engine for Meme Bot
Automatically copies trades from followed wallets
"""

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Callable, Any
from dataclasses import dataclass, field
import logging

from .models import (
    Token, Trade, WalletPosition, Chain, TradeAction,
    BotConfig, AlertMode, Signal
)
from .database import Database
from .alert_manager import AlertManager

logger = logging.getLogger(__name__)


@dataclass
class TrackedWallet:
    """Wallet being tracked for copy trading"""
    address: str
    chain: Chain
    label: str = ""
    total_trades: int = 0
    win_rate: float = 0.0
    avg_trade_size: float = 0.0
    pnl_30d: float = 0.0
    last_activity: datetime = field(default_factory=datetime.utcnow)
    is_active: bool = True


@dataclass 
class PendingCopyTrade:
    """A copy trade that has been detected but not yet executed"""
    source_wallet: str
    chain: Chain
    token_address: str
    token_symbol: str
    action: TradeAction
    source_amount: float  # Amount the source traded
    source_price: float
    detected_at: datetime
    expires_at: datetime  # Copy trade window


class CopyTrader:
    """
    Copy trading engine.
    Tracks wallets and automatically copies their trades.
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
        
        # Chain-specific trackers (set by chain adapters)
        self._trackers: Dict[Chain, Any] = {}
        
        # Trading engine reference
        self._trading_engine: Optional[Any] = None
        
        # Tracked wallets
        self._tracked_wallets: Dict[str, TrackedWallet] = {}
        
        # Pending copy trades
        self._pending_copy_trades: Dict[str, PendingCopyTrade] = {}
        
        # Settings
        self.enabled = config.copy_trade_enabled
        self.amount_percent = config.copy_trade_amount_percent  # % of source trade
        
        # Copy trade window (seconds)
        self.copy_window_seconds = 60
        
        # Running state
        self._running = False
        self._tasks: List[asyncio.Task] = []
        
        # Load previously tracked wallets
        for wallet in config.followed_wallets:
            self.add_wallet(wallet, Chain.SOLANA)  # Default to SOL, can be updated
    
    def set_trading_engine(self, engine: Any):
        """Set the trading engine for executing copy trades"""
        self._trading_engine = engine
    
    def set_tracker(self, chain: Chain, tracker: Any):
        """Set the chain-specific wallet tracker"""
        self._trackers[chain] = tracker
    
    def add_wallet(self, address: str, chain: Chain, label: str = ""):
        """Add a wallet to track"""
        key = f"{chain.value}:{address}"
        self._tracked_wallets[key] = TrackedWallet(
            address=address,
            chain=chain,
            label=label or f"Wallet {address[:8]}"
        )
        logger.info(f"Added wallet to track: {address[:8]}... on {chain.value}")
    
    def remove_wallet(self, address: str, chain: Chain):
        """Remove a wallet from tracking"""
        key = f"{chain.value}:{address}"
        self._tracked_wallets.pop(key, None)
        logger.info(f"Removed wallet from track: {address[:8]}...")
    
    def get_wallets(self) -> List[TrackedWallet]:
        """Get all tracked wallets"""
        return list(self._tracked_wallets.values())
    
    async def start(self):
        """Start the copy trading engine"""
        if not self.enabled:
            logger.info("Copy trading is disabled")
            return
        
        self._running = True
        
        # Start pending trade processor
        processor = asyncio.create_task(self._process_pending_trades())
        self._tasks.append(processor)
        
        # Start wallet activity checker
        checker = asyncio.create_task(self._check_wallet_activity())
        self._tasks.append(checker)
        
        logger.info("CopyTrader started")
    
    async def stop(self):
        """Stop the copy trading engine"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("CopyTrader stopped")
    
    async def _process_pending_trades(self):
        """Process pending copy trades"""
        while self._running:
            try:
                await asyncio.sleep(5)  # Check every 5 seconds
                
                if not self._running:
                    break
                
                now = datetime.utcnow()
                expired_trades = []
                
                for trade_id, pending in self._pending_copy_trades.items():
                    # Check if expired
                    if now > pending.expires_at:
                        expired_trades.append(trade_id)
                        continue
                    
                    # Execute copy trade if enabled
                    if self.enabled and self._trading_engine:
                        await self._execute_copy_trade(pending)
                        expired_trades.append(trade_id)
                
                # Remove processed/expired
                for trade_id in expired_trades:
                    self._pending_copy_trades.pop(trade_id, None)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pending trade processor error: {e}")
    
    async def _check_wallet_activity(self):
        """Periodically check wallet activity and stats"""
        while self._running:
            try:
                await asyncio.sleep(300)  # Every 5 minutes
                
                if not self._running:
                    break
                
                for key, wallet in list(self._tracked_wallets.items()):
                    tracker = self._trackers.get(wallet.chain)
                    if not tracker:
                        continue
                    
                    try:
                        # Update wallet stats
                        stats = await tracker.get_wallet_stats(wallet.address)
                        if stats:
                            wallet.total_trades = stats.get('total_trades', 0)
                            wallet.win_rate = stats.get('win_rate', 0)
                            wallet.avg_trade_size = stats.get('avg_trade_size', 0)
                            wallet.pnl_30d = stats.get('pnl_30d', 0)
                            wallet.last_activity = stats.get('last_activity', wallet.last_activity)
                    except Exception as e:
                        logger.error(f"Wallet stats error for {wallet.address[:8]}: {e}")
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Wallet activity checker error: {e}")
    
    async def _execute_copy_trade(self, pending: PendingCopyTrade):
        """Execute a copy trade"""
        if not self._trading_engine:
            return
        
        # Calculate our trade amount
        copy_amount = pending.source_amount * (self.amount_percent / 100)
        
        # Don't exceed position limits
        if copy_amount > self._trading_engine.config.max_position_per_token:
            copy_amount = self._trading_engine.config.max_position_per_token
        
        if copy_amount <= 0:
            return
        
        # Check if we already have a position in this
        existing = self._trading_engine.get_position(pending.token_address, pending.chain)
        
        if pending.action == TradeAction.BUY:
            # Create a synthetic signal for the copy trade
            signal = Signal(
                id=str(uuid.uuid4()),
                type=SignalType.WALLET_ACTIVITY,
                chain=pending.chain,
                token=Token(
                    symbol=pending.token_symbol,
                    name=pending.token_symbol,
                    address=pending.token_address,
                    chain=pending.chain
                ),
                message=f"👛 Copy trade from {pending.source_wallet[:8]}...",
                confidence=0.7,
                score=70,
                price_at_signal=pending.source_price,
                source_address=pending.source_wallet
            )
            
            # Only buy if not already holding
            if not existing:
                await self._trading_engine.execute_buy(
                    signal=signal,
                    amount=copy_amount
                )
                logger.info(f"Copy BUY executed: {pending.token_symbol}")
        
        elif pending.action == TradeAction.SELL and existing:
            # Follow sell if we have a position
            await self._trading_engine.execute_sell(
                token_address=pending.token_address,
                chain=pending.chain
            )
            logger.info(f"Copy SELL executed: {pending.token_symbol}")
        
        # Alert
        await self.alert_manager.alert_copy_trade(pending.source_wallet, Trade(
            id=str(uuid.uuid4()),
            chain=pending.chain,
            action=pending.action,
            token_address=pending.token_address,
            token_symbol=pending.token_symbol,
            amount_in=copy_amount,
            amount_out=0,
            price=pending.source_price,
            value_usd=copy_amount,
            tx_hash="",
            wallet_address=self.config.wallet_address,
            executed_at=datetime.utcnow()
        ))
    
    def on_wallet_activity(
        self,
        wallet_address: str,
        chain: Chain,
        token_address: str,
        token_symbol: str,
        action: TradeAction,
        amount: float,
        price: float
    ):
        """
        Called by wallet tracker when a tracked wallet makes a trade.
        Queues a copy trade if conditions are met.
        """
        if not self.enabled:
            return
        
        key = f"{chain.value}:{wallet_address}"
        if key not in self._tracked_wallets:
            return
        
        # Check if we should copy this trade
        if action == TradeAction.SELL:
            # Always follow sells to protect capital
            pass
        elif action == TradeAction.BUY:
            # Check position limits
            if self._trading_engine and not self._trading_engine._check_position_limits(
                Token(symbol=token_symbol, name=token_symbol, address=token_address, chain=chain),
                amount * self.amount_percent / 100
            ):
                return
        
        # Create pending copy trade
        trade_id = str(uuid.uuid4())
        self._pending_copy_trades[trade_id] = PendingCopyTrade(
            source_wallet=wallet_address,
            chain=chain,
            token_address=token_address,
            token_symbol=token_symbol,
            action=action,
            source_amount=amount,
            source_price=price,
            detected_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=self.copy_window_seconds)
        )
        
        logger.info(f"Queued copy trade: {action.value} {token_symbol} from {wallet_address[:8]}...")
    
    def enable(self):
        """Enable copy trading"""
        self.enabled = True
        logger.info("Copy trading enabled")
    
    def disable(self):
        """Disable copy trading"""
        self.enabled = False
        logger.info("Copy trading disabled")
    
    def set_copy_percent(self, percent: float):
        """Set the copy trade size as percentage of source"""
        self.amount_percent = max(1, min(100, percent))
        logger.info(f"Copy trade percent set to {self.amount_percent}%")
    
    def get_stats(self) -> Dict[str, any]:
        """Get copy trading stats"""
        wallets = self.get_wallets()
        return {
            'enabled': self.enabled,
            'tracked_wallets': len(wallets),
            'copy_percent': self.amount_percent,
            'pending_trades': len(self._pending_copy_trades),
            'wallets': [
                {
                    'address': w.address[:8] + '...',
                    'chain': w.chain.value,
                    'total_trades': w.total_trades,
                    'win_rate': f"{w.win_rate:.1f}%",
                    'pnl_30d': f"${w.pnl_30d:,.2f}",
                    'last_activity': w.last_activity.isoformat()
                }
                for w in wallets
            ]
        }
