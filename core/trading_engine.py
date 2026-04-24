#!/usr/bin/env python3
"""
Trading Engine for Meme Bot
Handles trade execution, position management, and one-click liquidation
"""

import asyncio
import uuid
from datetime import datetime
from typing import Dict, Optional, List, Any
from dataclasses import dataclass
import logging

from .models import (
    Token, Trade, WalletPosition, Chain, TradeAction, 
    BotConfig, AlertMode, Signal
)
from .database import Database
from .alert_manager import AlertManager

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Trading position"""
    token_address: str
    token_symbol: str
    chain: Chain
    
    quantity: float  # Token amount
    avg_buy_price: float
    current_price: float
    
    amount_in: float  # SOL/BNB spent
    value_now: float  # Current USD value
    pnl_percent: float
    pnl_usd: float
    
    signal_id: Optional[str]
    opened_at: datetime


class TradingEngine:
    """
    Unified trading engine for SOL and BSC chains.
    Handles buy/sell execution and position tracking.
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
        
        # Chain-specific executors (set by chain adapters)
        self._executors: Dict[Chain, Any] = {}
        
        # Local position cache
        self._positions: Dict[str, Position] = {}  # key = f"{chain}:{token_address}"
        
        # Mode
        self.alert_mode = config.alert_mode
    
    def set_executor(self, chain: Chain, executor: Any):
        """Set the chain-specific trade executor"""
        self._executors[chain] = executor
    
    async def execute_buy(
        self,
        signal: Signal,
        amount: Optional[float] = None,
        slippage: Optional[float] = None
    ) -> Optional[Trade]:
        """
        Execute a buy order based on a signal.
        Returns the executed trade or None if failed.
        """
        chain = signal.chain
        executor = self._executors.get(chain)
        
        if not executor:
            logger.error(f"No executor for chain {chain}")
            return None
        
        # Use config defaults if not specified
        amount = amount or self.config.default_amount_per_trade
        slippage = slippage or self.config.default_slippage
        
        # Check position limits
        if not self._check_position_limits(signal.token, amount):
            logger.warning(f"Position limits exceeded for {signal.token.symbol}")
            return None
        
        try:
            # Execute the trade via chain executor
            result = await executor.execute_buy(
                token_address=signal.token.address,
                amount=amount,
                slippage=slippage
            )
            
            if not result or not result.get('success'):
                logger.error(f"Buy execution failed: {result}")
                return None
            
            # Create trade record
            trade = Trade(
                id=str(uuid.uuid4()),
                chain=chain,
                action=TradeAction.BUY,
                token_address=signal.token.address,
                token_symbol=signal.token.symbol,
                amount_in=amount,
                amount_out=result.get('amount_out', 0),
                price=result.get('price', 0),
                value_usd=result.get('value_usd', amount),
                tx_hash=result.get('tx_hash', ''),
                wallet_address=self.config.wallet_address,
                signal_id=signal.id,
                executed_at=datetime.utcnow()
            )
            
            # Save trade
            self.db.save_trade(trade)
            
            # Update local position
            self._update_position_from_trade(trade)
            
            # Alert
            await self.alert_manager.alert_trade(trade, self.alert_mode)
            
            logger.info(f"BUY executed: {trade.token_symbol} @ ${trade.price}")
            return trade
            
        except Exception as e:
            logger.error(f"Buy execution error: {e}")
            return None
    
    async def execute_sell(
        self,
        token_address: str,
        chain: Chain,
        quantity: Optional[float] = None,  # None = sell all
        slippage: Optional[float] = None
    ) -> Optional[Trade]:
        """Execute a sell order"""
        executor = self._executors.get(chain)
        
        if not executor:
            logger.error(f"No executor for chain {chain}")
            return None
        
        slippage = slippage or self.config.default_slippage
        
        # Get position info
        pos_key = f"{chain.value}:{token_address}"
        position = self._positions.get(pos_key)
        
        if not position:
            # Try to load from DB
            positions = self.db.get_wallet_positions(self.config.wallet_address, chain)
            for p in positions:
                if p.token_address == token_address:
                    position = self._load_position_from_db(p, chain)
                    break
        
        if not position:
            logger.error(f"No position found for {token_address}")
            return None
        
        sell_quantity = quantity or position.quantity
        
        try:
            result = await executor.execute_sell(
                token_address=token_address,
                quantity=sell_quantity,
                slippage=slippage
            )
            
            if not result or not result.get('success'):
                logger.error(f"Sell execution failed: {result}")
                return None
            
            # Calculate P&L
            buy_value = position.avg_buy_price * sell_quantity / position.current_price
            sell_value = result.get('value_usd', 0)
            pnl_percent = ((sell_value - buy_value) / buy_value * 100) if buy_value > 0 else 0
            pnl_usd = sell_value - buy_value
            
            trade = Trade(
                id=str(uuid.uuid4()),
                chain=chain,
                action=TradeAction.SELL,
                token_address=token_address,
                token_symbol=position.token_symbol,
                amount_in=sell_quantity,
                amount_out=result.get('amount_out', 0),  # SOL/BNB received
                price=result.get('price', 0),
                value_usd=sell_value,
                tx_hash=result.get('tx_hash', ''),
                wallet_address=self.config.wallet_address,
                signal_id=position.signal_id,
                executed_at=datetime.utcnow(),
                pnl_percent=pnl_percent,
                pnl_usd=pnl_usd
            )
            
            self.db.save_trade(trade)
            
            # Update position
            if quantity is None or quantity >= position.quantity:
                # Full sell - remove position
                self._positions.pop(pos_key, None)
            else:
                # Partial sell
                position.quantity -= quantity
                position.amount_in *= (1 - quantity / position.quantity)
            
            # Alert
            await self.alert_manager.alert_trade(trade, self.alert_mode)
            
            logger.info(f"SELL executed: {trade.token_symbol} P&L: {pnl_percent:.2f}%")
            return trade
            
        except Exception as e:
            logger.error(f"Sell execution error: {e}")
            return None
    
    async def liquidate_all(self, chain: Chain) -> List[Trade]:
        """Liquidate all positions on a chain"""
        trades = []
        chain_str = chain.value
        
        # Get all positions for this chain
        positions_to_liquidate = [
            (key, pos) for key, pos in self._positions.items()
            if key.startswith(chain_str)
        ]
        
        for key, position in positions_to_liquidate:
            trade = await self.execute_sell(
                token_address=position.token_address,
                chain=chain,
                quantity=None  # Sell all
            )
            if trade:
                trades.append(trade)
        
        logger.info(f"Liquidated {len(trades)} positions on {chain.value}")
        return trades
    
    async def liquidate_position(
        self, 
        token_address: str, 
        chain: Chain,
        quantity: Optional[float] = None
    ) -> Optional[Trade]:
        """Liquidate a specific position (or partial)"""
        return await self.execute_sell(token_address, chain, quantity)
    
    def _check_position_limits(self, token: Token, amount: float) -> bool:
        """Check if a new position would exceed limits"""
        pos_key = f"{token.chain.value}:{token.address}"
        
        # Check max per token
        if amount > self.config.max_position_per_token:
            return False
        
        # Check total position
        current_total = sum(
            pos.amount_in for pos in self._positions.values()
            if pos.chain == token.chain
        )
        if current_total + amount > self.config.max_total_position:
            return False
        
        return True
    
    def _update_position_from_trade(self, trade: Trade):
        """Update local position cache from a trade"""
        pos_key = f"{trade.chain.value}:{trade.token_address}"
        
        if trade.action == TradeAction.BUY:
            if pos_key in self._positions:
                pos = self._positions[pos_key]
                # Update average price
                total_cost = pos.amount_in + trade.amount_in
                total_qty = pos.quantity + trade.amount_out
                pos.avg_buy_price = (pos.avg_buy_price * pos.quantity + trade.price * trade.amount_out) / total_qty
                pos.quantity = total_qty
                pos.amount_in = total_cost
                pos.current_price = trade.price
                pos.value_now = pos.quantity * pos.current_price
            else:
                self._positions[pos_key] = Position(
                    token_address=trade.token_address,
                    token_symbol=trade.token_symbol,
                    chain=trade.chain,
                    quantity=trade.amount_out,
                    avg_buy_price=trade.price,
                    current_price=trade.price,
                    amount_in=trade.amount_in,
                    value_now=trade.value_usd,
                    pnl_percent=0,
                    pnl_usd=0,
                    signal_id=trade.signal_id,
                    opened_at=trade.executed_at
                )
    
    def _load_position_from_db(self, db_pos: WalletPosition, chain: Chain) -> Position:
        """Load position from database"""
        return Position(
            token_address=db_pos.token_address,
            token_symbol=db_pos.token_symbol,
            chain=chain,
            quantity=db_pos.balance,
            avg_buy_price=db_pos.avg_buy_price,
            current_price=db_pos.current_price,
            amount_in=db_pos.balance * db_pos.avg_buy_price,
            value_now=db_pos.value_usd,
            pnl_percent=db_pos.unrealized_pnl_percent,
            pnl_usd=db_pos.unrealized_pnl_usd,
            signal_id=None,
            opened_at=db_pos.updated_at
        )
    
    def get_positions(self, chain: Chain) -> List[Position]:
        """Get all positions for a chain"""
        chain_str = chain.value
        return [
            pos for key, pos in self._positions.items()
            if key.startswith(chain_str)
        ]
    
    def get_position(self, token_address: str, chain: Chain) -> Optional[Position]:
        """Get a specific position"""
        return self._positions.get(f"{chain.value}:{token_address}")
    
    def set_mode(self, mode: AlertMode):
        """Set the trading mode"""
        self.alert_mode = mode
        self.config.alert_mode = mode
        logger.info(f"Trading mode set to {mode.value}")
    
    async def sync_positions(self, chain: Chain):
        """Sync positions with blockchain (fetch real balances)"""
        executor = self._executors.get(chain)
        if not executor:
            return
        
        try:
            balances = await executor.get_wallet_balances(self.config.wallet_address)
            
            for token_address, balance in balances.items():
                if balance <= 0:
                    # Position closed
                    self._positions.pop(f"{chain.value}:{token_address}", None)
                    continue
                
                pos_key = f"{chain.value}:{token_address}"
                current_price = await executor.get_token_price(token_address)
                
                if pos_key in self._positions:
                    pos = self._positions[pos_key]
                    pos.current_price = current_price
                    pos.value_now = pos.quantity * current_price
                    pos.pnl_percent = ((current_price - pos.avg_buy_price) / pos.avg_buy_price * 100) if pos.avg_buy_price > 0 else 0
                    pos.pnl_usd = pos.value_now - pos.amount_in
                else:
                    # New position found on chain
                    self._positions[pos_key] = Position(
                        token_address=token_address,
                        token_symbol=token_address[:8],
                        chain=chain,
                        quantity=balance,
                        avg_buy_price=current_price,
                        current_price=current_price,
                        amount_in=balance * current_price,
                        value_now=balance * current_price,
                        pnl_percent=0,
                        pnl_usd=0,
                        signal_id=None,
                        opened_at=datetime.utcnow()
                    )
            
            logger.info(f"Synced {len(balances)} positions for {chain.value}")
            
        except Exception as e:
            logger.error(f"Position sync error: {e}")
